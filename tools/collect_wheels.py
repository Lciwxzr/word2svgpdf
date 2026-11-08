# -*- coding: utf-8 -*-
"""Collect the project's dependencies into wheels\\ without a network.

Why this exists
---------------
`make_wheels.bat` uses `pip download`, which needs PyPI and is slow on a
throttled connection (measured ~100 KB/s: a full set takes many minutes).
When the dependencies are already installed somewhere on the machine, it is
much faster and completely offline to rebuild wheels from the installed
packages instead.

How
---
A wheel is just a zip with a specific layout. For each installed package this
script writes:

    <name>-<version>.dist-info/METADATA
    <name>-<version>.dist-info/WHEEL
    <name>-<version>.dist-info/RECORD
    <package files...>

That is enough for pip (and for the plain unzip used by make_portable.py) to
treat it as a normal wheel. Dependencies are resolved by reading METADATA's
Requires-Dist, so the closure is complete.

Usage:
    python collect_wheels.py [--python PATH] [--out wheels]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT_REQUIREMENTS = [
    "pypdfium2", "pikepdf", "starlette", "uvicorn",
    "python-multipart", "pillow", "lxml", "scipy",
]

# Names that differ between the import name and the distribution name.
DIST_ALIASES = {
    "python-multipart": "python_multipart",
    "pillow": "Pillow",
    "PIL": "Pillow",
}


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def dist_name_for(module: str) -> str:
    return DIST_ALIASES.get(module, module)


def get_metadata(py: str, dist: str) -> dict | None:
    """Read METADATA for a distribution using its own interpreter.

    importlib.metadata is case-insensitive on the distribution name, but the
    caller may pass the import name (PIL) rather than the distribution name
    (Pillow), so several spellings are tried.
    """
    candidates = [dist, dist.replace("-", "_"), dist.replace("_", "-")]
    code = (
        "import importlib.metadata as m,json\n"
        f"names={candidates!r}\n"
        "d=None\n"
        "for n in names:\n"
        "    try:\n"
        "        d=m.distribution(n); break\n"
        "    except Exception:\n"
        "        continue\n"
        "if d is None: raise SystemExit(3)\n"
        "md=d.metadata\n"
        "print(json.dumps({\n"
        "  'name': md.get('Name'),\n"
        "  'version': md.get('Version'),\n"
        "  'requires': md.get_all('Requires-Dist') or [],\n"
        "  'requires_python': md.get('Requires-Python'),\n"
        "  'summary': md.get('Summary'),\n"
        "  'license': md.get('License') or '',\n"
        "  'home': md.get('Home-page') or '',\n"
        "  'location': str(d.locate_file('')),\n"
        "  'files': [str(f) for f in (d.files or [])],\n"
        "}))"
    )
    r = subprocess.run([py, "-c", code], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    import json
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def parse_requirements(requires: list[str]) -> list[str]:
    """Extract bare distribution names from Requires-Dist entries."""
    out = []
    for r in requires:
        # "name (>=1.0) ; extra == 'x'"  ->  skip extras-only requirements
        if "extra ==" in r:
            continue
        m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", r)
        if m:
            out.append(m.group(1))
    return out


def collect_closure(py: str) -> dict[str, dict]:
    """Resolve the full dependency closure, metadata keyed by normalised name."""
    found: dict[str, dict] = {}
    queue = list(ROOT_REQUIREMENTS)
    while queue:
        name = queue.pop()
        key = norm(dist_name_for(name))
        if key in found:
            continue
        md = get_metadata(py, dist_name_for(name))
        if md is None:
            print(f"  ! {name}: metadata not found, skipped")
            continue
        found[key] = md
        for dep in parse_requirements(md["requires"]):
            if norm(dep) not in found:
                queue.append(dep)
    return found


def record_line(arcname: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"{arcname},sha256={digest.decode()},{len(data)}"


def build_wheel(md: dict, out_dir: str) -> str | None:
    """Repack an installed distribution as a wheel."""
    name = md["name"]
    version = md["version"]
    dist = norm(name).replace("-", "_")
    info = f"{dist}-{version}.dist-info"
    wheel_name = f"{dist}-{version}-py3-none-any.whl"
    target = os.path.join(out_dir, wheel_name)

    files = [f for f in md["files"] if f and not f.endswith(".pyc")]
    # dist-info files are written explicitly below; including them from the
    # installed tree would add duplicate zip entries and a warning per file.
    files = [f for f in files if ".dist-info/" not in f and ".egg-info/" not in f]
    if not files:
        return None

    site = md["location"]
    rows: list[str] = []
    written = 0

    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            for rel in files:
                src = os.path.join(site, rel)
                if not os.path.isfile(src):
                    continue
                with open(src, "rb") as fh:
                    data = fh.read()
                z.writestr(rel, data)
                rows.append(record_line(rel, data))
                written += 1

            meta = (
                f"Metadata-Version: 2.1\n"
                f"Name: {name}\n"
                f"Version: {version}\n"
                f"Summary: {md.get('summary') or ''}\n"
            )
            if md.get("requires_python"):
                meta += f"Requires-Python: {md['requires_python']}\n"
            if md.get("license"):
                meta += f"License: {md['license']}\n"
            if md.get("home"):
                meta += f"Home-page: {md['home']}\n"
            for r in md["requires"]:
                meta += f"Requires-Dist: {r}\n"
            z.writestr(f"{info}/METADATA", meta)
            rows.append(record_line(f"{info}/METADATA", meta.encode()))

            wh = (
                "Wheel-Version: 1.0\n"
                "Generator: collect_wheels.py\n"
                "Root-Is-Purelib: true\n"
                "Tag: py3-none-any\n"
            )
            z.writestr(f"{info}/WHEEL", wh)
            rows.append(record_line(f"{info}/WHEEL", wh.encode()))

            rows.append(f"{info}/RECORD,,")
            z.writestr(f"{info}/RECORD", "\n".join(rows) + "\n")

        return target if written else None
    except Exception as exc:
        print(f"  ! {name}: {exc}")
        if os.path.exists(target):
            os.remove(target)
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wheels"))
    args = ap.parse_args()

    py = args.python
    print(f"\n  Collecting wheels from {py}\n")

    closure = collect_closure(py)
    print(f"  resolved {len(closure)} distributions\n")

    os.makedirs(args.out, exist_ok=True)
    built = 0
    for key, md in sorted(closure.items()):
        target = build_wheel(md, args.out)
        if target:
            size = os.path.getsize(target) / 1e6
            print(f"  {os.path.basename(target):60} {size:6.1f} MB")
            built += 1

    total = sum(os.path.getsize(os.path.join(args.out, f))
                for f in os.listdir(args.out)) / 1e6
    print(f"\n  {built} wheels, {total:.1f} MB total in {args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
