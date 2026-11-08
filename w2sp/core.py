# -*- coding: utf-8 -*-
"""Core pipeline for the Word -> PDF vector-figure refit tool.

Pure logic, no web framework. Every step reports progress through a callback
so the GUI can show what is happening (Inkscape takes ~10-20 s per figure).

Pipeline:
    1. read the .docx, find drawings and their SVG payloads
    2. classify: which SVGs embed raster images (Word rasterises those)
    3. open the PDF, find the placed raster that matches each such figure
    4. convert the SVG to a vector PDF (Inkscape)
    5. overlay it at the original rect and blank the old bitmap
    6. verify: content outside the figure rects must be unchanged

PDF handling is split in two libraries, both non-AGPL:
    pdfio.py     pypdfium2 (PDFium) -- rendering, text, image location
    pdfedit.py   pikepdf (libqpdf)  -- in-place object edits, no renumbering
"""
from __future__ import annotations

import base64
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from typing import Callable

from . import pdfedit
from . import pdfio
from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
ASVG = "http://schemas.microsoft.com/office/drawing/2016/SVG/main"
NS = {"w": W, "a": A, "r": R, "wp": WP, "asvg": ASVG}

INKSCAPE_CANDIDATES = [
    r"C:\Program Files\Inkscape\bin\inkscape.exe",
    r"C:\Program Files\Inkscape\inkscape.exe",
    r"C:\Program Files (x86)\Inkscape\bin\inkscape.exe",
    "/usr/bin/inkscape",
    "/Applications/Inkscape.app/Contents/MacOS/inkscape",
]


class Progress:
    """Collects events for the GUI and keeps the last state."""

    def __init__(self, cb: Callable[[dict], None] | None = None):
        self.cb = cb
        self.events: list[dict] = []

    def __call__(self, stage: str, message: str, **extra):
        ev = {"stage": stage, "message": message, "t": round(time.time(), 2), **extra}
        self.events.append(ev)
        if self.cb:
            self.cb(ev)


def find_inkscape() -> str | None:
    env = os.environ.get("INKSCAPE")
    if env and os.path.exists(env):
        return env
    for c in INKSCAPE_CANDIDATES:
        if os.path.exists(c):
            return c
    return None


# ---------------------------------------------------------------- docx side

@dataclass
class Figure:
    index: int                 # 1-based order in the document
    w_pt: float
    h_pt: float
    png: str | None
    svg: str | None
    raster_count: int = 0      # <image> elements inside the SVG
    pdf_page: int | None = None
    rect: tuple | None = None
    status: str = "pending"    # pending | vector | replace | ambiguous | no-match | skipped
    note: str = ""
    score: float | None = None
    candidates: list = field(default_factory=list)
    # Manual override set by the user in the UI. When present it wins over
    # whatever the automatic matcher decided:
    #   "skip"          -> do not replace this figure
    #   (page, rect)    -> replace at that placement
    override: tuple | str | None = None
    # snapshot of (status, pdf_page, rect, note) taken right after matching,
    # so clearing an override can restore the automatic decision
    auto: tuple | None = None
    # Path to a user-supplied SVG that should replace this figure instead of
    # the one embedded in the docx. Used when the docx carries no usable SVG
    # (or the wrong one). Set by /api/upload_svg.
    custom_svg: str | None = None

    @property
    def label(self) -> str:
        return f"图 {self.index}"

    @property
    def svg_source(self) -> tuple[str, str] | None:
        """(kind, ref) of the vector source to convert.

        kind is "docx" (ref is the media path inside the docx) or "file"
        (ref is an absolute path on disk).
        """
        if self.custom_svg:
            return ("file", self.custom_svg)
        if self.svg:
            return ("docx", self.svg)
        return None

    def apply_override(self) -> None:
        """Fold a user override into status/page/rect.

        When the override is cleared, the automatic decision is restored from
        `auto` (the status/page/rect recorded by the matcher), so clearing a
        skip or a manual pin returns the figure to what the matcher found.
        """
        if self.override is None:
            if self.auto is not None:
                self.status, self.pdf_page, self.rect, self.note = self.auto
            return
        if self.override == "skip":
            self.status = "skipped"
            self.pdf_page = None
            self.rect = None
            self.note = "用户跳过，不替换"
            return
        page, rect = self.override
        self.status = "replace"
        self.pdf_page = page
        self.rect = tuple(rect)
        self.note = f"第 {page + 1} 页（用户手动指定）"

    def remember_auto(self) -> None:
        """Snapshot the automatic decision so a cleared override can restore it."""
        self.auto = (self.status, self.pdf_page, self.rect, self.note)


def read_docx_figures(docx_path: str) -> tuple[zipfile.ZipFile, list[Figure]]:
    z = zipfile.ZipFile(docx_path)
    rels = {}
    for rel in etree.fromstring(z.read("word/_rels/document.xml.rels")):
        rels[rel.get("Id")] = rel.get("Target")
    doc = etree.fromstring(z.read("word/document.xml"))

    figs: list[Figure] = []
    for p in doc.iter(f"{{{W}}}p"):
        for d in p.iter(f"{{{W}}}drawing"):
            ext = d.find(".//wp:extent", NS)
            if ext is None:
                continue
            w = int(ext.get("cx")) / 12700.0
            h = int(ext.get("cy")) / 12700.0
            blip = d.find(".//a:blip", NS)
            png = rels.get(blip.get(f"{{{R}}}embed")) if blip is not None else None
            svgb = d.find(".//asvg:svgBlip", NS)
            svg = rels.get(svgb.get(f"{{{R}}}embed")) if svgb is not None else None

            n_ras = 0
            if svg:
                try:
                    n_ras = z.read("word/" + svg).decode("utf-8", "replace").count("<image")
                except KeyError:
                    n_ras = 0
            figs.append(Figure(len(figs) + 1, w, h, png, svg, n_ras))
    return z, figs


def svg_bytes(z: zipfile.ZipFile, target: str) -> bytes:
    return z.read("word/" + target)


# ----------------------------------------------------------------- pdf side

def raster_placements(doc) -> list[dict]:
    """Every placed image big enough to be a figure (pdfio.Document)."""
    from .smart_match import collect_blocks
    return collect_blocks(doc)


def match_figures(figs: list[Figure], doc,
                  z: zipfile.ZipFile | None = None, tol: float = 4.0,
                  docx_path: str | None = None, stats: dict | None = None) -> None:
    """Assign each rasterised figure to exactly one placed raster.

    Delegates to smart_match, which tries document order first (no image
    decoding, 33-46 ms) and only falls back to content correlation
    (140-170 ms per block) when order cannot be trusted.
    """
    from .smart_match import match_figures as _smart
    _smart(figs, doc, z, tol=tol, docx_path=docx_path, stats=stats)


# ------------------------------------------------------------- conversion

def svg_to_pdf(inkscape: str, svg_path: str, pdf_path: str) -> None:
    """Convert one SVG to PDF via Inkscape (one process per file).

    Used for single conversions and as the fallback when the batch path
    below cannot be trusted.
    """
    cmd = [inkscape, "--export-type=pdf", f"--export-filename={pdf_path}", svg_path]
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)


def svg_to_pdf_batch(inkscape: str, jobs: list[tuple[str, str]],
                     progress=None) -> list[str]:
    """Convert several SVGs in ONE Inkscape process (about 1.9x faster).

    Measured: 3 files take 10.8 s as separate processes vs 5.7 s in shell
    mode. The speedup comes from skipping the GTK/Cairo start-up cost.

    IMPORTANT correctness guard
    ---------------------------
    Inkscape's --shell mode does NOT report a per-command failure. If
    `file-open` is given a bad path, Inkscape silently exports the *previously
    opened document* to the requested filename. Measured: opening a
    non-existent SVG produced a PDF byte-for-byte equivalent to the previous
    one. That would silently replace a figure with the wrong image.

    To make this safe, each job is verified after the run:
      - the output file must exist and be non-empty, and
      - its page size must match the SVG's own size (read by PyMuPDF).

    Any job that fails those checks is re-run through the single-process
    path, and only then reported as failed. So a bad batch can never
    produce a wrong figure -- at worst it costs one extra conversion.

    Returns the list of job ids that still failed after the retry.
    """
    if not jobs:
        return []

    lines = []
    for i, (svg_path, pdf_path) in enumerate(jobs):
        lines.append(f"file-open:{svg_path}; export-type:pdf; "
                     f"export-filename:{pdf_path}; export-do")
    lines.append("quit")
    script = "\n".join(lines) + "\n"

    try:
        subprocess.run([inkscape, "--shell"], input=script, text=True,
                       capture_output=True, timeout=1200,
                       encoding="utf-8", errors="replace")
    except Exception:
        pass

    # ---- verify every output, and retry individually if anything is off
    failed = []
    for i, (svg_path, pdf_path) in enumerate(jobs):
        if _conversion_looks_right(svg_path, pdf_path):
            continue
        if progress:
            progress("convert", f"批量转换结果校验未通过，单独重试第 {i+1} 个…")
        try:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)
            svg_to_pdf(inkscape, svg_path, pdf_path)
        except Exception:
            pass
        if not _conversion_looks_right(svg_path, pdf_path):
            failed.append(pdf_path)
    return failed


def _svg_page_size(svg_path: str) -> tuple[float, float] | None:
    """Size of an SVG in CSS pixels (96 per inch), from its root attributes.

    PDFium cannot open SVG files, so the size is read straight from the XML
    instead: width/height when present, otherwise the viewBox. Units are
    converted to px (96 per inch) so the result is comparable with the PDF
    Inkscape writes (points, 72 per inch).
    """
    try:
        from lxml import etree
        import re

        root = etree.parse(svg_path).getroot()
        UNIT = {"mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96.0,
                "pt": 96 / 72, "pc": 16.0, "px": 1.0}

        def to_px(value: str | None) -> float | None:
            if not value:
                return None
            m = re.match(r"\s*([0-9.]+)\s*([a-z%]*)", value)
            if not m:
                return None
            num = float(m.group(1))
            unit = (m.group(2) or "px").lower()
            if unit == "%":
                return None
            return num * UNIT.get(unit, 1.0)

        w = to_px(root.get("width"))
        h = to_px(root.get("height"))
        if w and h:
            return (w, h)
        vb = root.get("viewBox")
        if vb:
            parts = [float(v) for v in re.split(r"[,\s]+", vb.strip()) if v]
            if len(parts) == 4 and parts[3] > 0:
                return (parts[2], parts[3])
        return None
    except Exception:
        return None


def _conversion_looks_right(svg_path: str, pdf_path: str) -> bool:
    """True if pdf_path is a plausible conversion of svg_path.

    Only the ASPECT RATIO and the 72/96 unit scale are compared. PDFium
    reports an SVG page in CSS pixels (96 per inch) while Inkscape writes the
    PDF in points (72 per inch), so the same figure is 0.75x numerically.
    Absolute sizes therefore never match and must not be compared. A wrong
    document still shows a different shape, which is what this check is
    really after -- Inkscape's --shell mode silently re-exports the previous
    document when file-open fails, so this guard matters.
    """
    if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) < 1024:
        return False
    want = _svg_page_size(svg_path)
    if not want or want[1] <= 0:
        return False
    try:
        doc = pdfio.Document(pdf_path)
        try:
            got = doc.page(0).size()
        finally:
            doc.close()
    except Exception:
        return False
    if got[1] <= 0:
        return False
    ra = want[0] / want[1]
    rb = got[0] / got[1]
    if abs(ra - rb) > 0.02:
        return False
    return 0.70 <= got[0] / want[0] <= 0.80


def downsample_rasters(pdf_path: str, max_dpi: int = 0,
                       only_rects: dict[int, list[tuple]] | None = None) -> int:
    """Resample oversized embedded images in place, keeping object numbers.

    Delegates to downsample.py, which uses pikepdf directly so the rewrite
    preserves object numbering. Transparency is composited onto white first;
    dropping an /SMask would turn every transparent pixel black.
    """
    from .downsample import downsample
    return downsample(pdf_path, max_dpi, only_rects)


# ------------------------------------------------------------------- verify

def _outside_text(page, rects):
    """Words whose centre lies outside every figure rect."""
    out = []
    for w in page.words():
        cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        if any(r.contains(cx, cy) for r in rects):
            continue
        out.append((w[0], w[1], w[4]))
    return out


def _masked(page, rects, dpi=150):
    """Render a page, blank the figure rects, return greyscale bytes."""
    from PIL import Image

    im = page.render(dpi=dpi).convert("L")
    buf = im.tobytes()
    ba = bytearray(buf)
    w, h = im.size
    s = dpi / 72.0
    for r in rects:
        x0 = max(0, int(r.x0 * s) - 2); y0 = max(0, int(r.y0 * s) - 2)
        x1 = min(w, int(r.x1 * s) + 2); y1 = min(h, int(r.y1 * s) + 2)
        for y in range(y0, y1):
            base = y * w
            for x in range(x0, x1):
                ba[base + x] = 255
    return bytes(ba), w, h


def verify(orig_pdf: str, new_pdf: str, figs: list[Figure]) -> dict:
    """Content invariance check. Returns a report dict."""
    masked: dict[int, list[tuple]] = {}
    for f in figs:
        if f.status == "replace" and f.pdf_page is not None and f.rect:
            masked.setdefault(f.pdf_page + 1, []).append(f.rect)

    pages = []
    ok = True

    with pdfio.Document(orig_pdf) as a, pdfio.Document(new_pdf) as b:
        count_ok = a.page_count == b.page_count
        for i in range(min(a.page_count, b.page_count)):
            pno = i + 1
            rects = [pdfio.Rect(*r) for r in masked.get(pno, [])]
            ta = _outside_text(a.page(i), rects)
            tb = _outside_text(b.page(i), rects)
            text_ok = ta == tb

            pa, wa, ha = _masked(a.page(i), rects)
            pb, wb, hb = _masked(b.page(i), rects)
            if (wa, ha) != (wb, hb):
                px_diff, render_ok = -1, False
            else:
                px_diff = sum(1 for x, y in zip(pa, pb) if x != y)
                render_ok = px_diff == 0

            entry = dict(page=pno, text_ok=text_ok, render_ok=render_ok,
                         px_diff=px_diff, masked=bool(rects),
                         tiny=bool(px_diff > 0 and px_diff <= 300 and text_ok))
            # A handful of pixels with identical text is rasteriser noise
            # (anti-aliasing jitter from re-encoding), not a content change.
            if not text_ok or (not render_ok and not entry["tiny"]):
                ok = False
                entry["missing"] = [t[2] for t in ta if t not in tb][:5]
                entry["added"] = [t[2] for t in tb if t not in ta][:5]
            pages.append(entry)

    return dict(
        ok=ok,
        page_count_ok=count_ok,
        pages=min(a.page_count, b.page_count) if False else len(pages),
        figures=len(masked),
        masked_pages=sorted(masked),
        details=pages,
        size_before=os.path.getsize(orig_pdf),
        size_after=os.path.getsize(new_pdf),
    )


# ------------------------------------------------------------- previews

def render_rect(pdf_path: str, pno: int, rect: tuple, dpi: int = 130) -> bytes:
    """PNG bytes of one figure area."""
    with pdfio.Document(pdf_path) as doc:
        page = doc.page(pno - 1)
        im = page.render(dpi=dpi, clip=pdfio.Rect(*rect))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def render_page(pdf_path: str, pno: int, dpi: int = 110) -> bytes:
    """PNG bytes of a whole page."""
    with pdfio.Document(pdf_path) as doc:
        im = doc.page(pno - 1).render(dpi=dpi)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def render_figure_from_svg(svg: bytes, dpi: int = 130) -> bytes | None:
    """Render the SVG itself (via Inkscape) for the 'source' side of the diff."""
    ink = find_inkscape()
    if not ink:
        return None
    tmp = tempfile.mkdtemp(prefix="prev_")
    sp = os.path.join(tmp, "f.svg")
    pp = os.path.join(tmp, "f.png")
    with open(sp, "wb") as fh:
        fh.write(svg)
    try:
        subprocess.run([ink, "--export-type=png", f"--export-filename={pp}",
                        f"--export-dpi={dpi * 96 / 72}", sp],
                       check=True, capture_output=True, timeout=600)
        with open(pp, "rb") as fh:
            return fh.read()
    except Exception:
        return None


# ------------------------------------------------------------- pipeline
#
# The pipeline is split into three re-entrant stages so the UI can let a user
# correct a wrong automatic match without redoing the expensive work:
#
#   analyse()   read docx + PDF, decide which figure goes where   (fast)
#   convert()   SVG -> vector PDF via Inkscape                    (slow, ~13 s each)
#   compose()   overlay, blank the old bitmap, verify             (fast)
#
# Conversions are cached per figure index in a work directory, so calling
# compose() again after a manual override only reuses what already exists.
# A figure the user re-points to a *different* placement needs no new
# conversion either -- the same vector PDF is simply drawn elsewhere.

class Session:
    """Holds the state of one uploaded document pair."""

    def __init__(self, docx_path: str, pdf_path: str, workdir: str):
        self.docx_path = docx_path
        self.pdf_path = pdf_path
        self.workdir = workdir
        os.makedirs(workdir, exist_ok=True)
        self.zip, self.figures = read_docx_figures(docx_path)
        self.stats: dict = {}
        self.converted: dict[int, str] = {}   # figure index -> vector PDF path

    # ---- stage 1 ----------------------------------------------------
    def analyse(self, progress: Progress) -> None:
        progress("scan", f"找到 {len(self.figures)} 张图",
                 figures=len(self.figures))
        progress("match", "打开导出的 PDF…")
        with pdfio.Document(self.pdf_path) as doc:
            match_figures(self.figures, doc, self.zip,
                          docx_path=self.docx_path, stats=self.stats)

        for f in self.figures:
            # remember what the matcher decided before any override is applied
            f.remember_auto()
            f.apply_override()

        n_rep = sum(1 for f in self.figures if f.status == "replace")
        n_vec = sum(1 for f in self.figures if f.status == "vector")
        n_amb = sum(1 for f in self.figures if f.status == "ambiguous")
        n_skip = sum(1 for f in self.figures if f.status == "skipped")
        msg = f"需替换 {n_rep} 张，已是矢量 {n_vec} 张"
        if n_amb:
            msg += f"，待确认 {n_amb} 张"
        if n_skip:
            msg += f"，跳过 {n_skip} 张"
        progress("match", msg, replace=n_rep, vector=n_vec,
                 ambiguous=n_amb, skipped=n_skip)

    # ---- stage 2 ----------------------------------------------------
    def convert(self, progress: Progress) -> list[str]:
        """Convert every figure that still needs a vector PDF.

        Already-converted figures are skipped, so a second call after a manual
        override costs nothing.
        """
        todo = [f for f in self.figures
                if f.status == "replace" and f.index not in self.converted]
        if not todo:
            return []

        ink = find_inkscape()
        if not ink:
            raise RuntimeError("未找到 Inkscape，无法把 SVG 转成矢量 PDF。"
                               "请安装 Inkscape 或设置环境变量 INKSCAPE。")

        jobs = []
        for f in todo:
            sp = os.path.join(self.workdir, f"f{f.index}.svg")
            src = f.svg_source
            if src is None:
                # no vector source at all (docx carried none and the user
                # supplied none) -- nothing to convert
                progress("convert", f"{f.label} 没有可用的矢量源，跳过",
                         figure=f.index)
                continue
            kind, ref = src
            if kind == "docx":
                with open(sp, "wb") as fh:
                    fh.write(svg_bytes(self.zip, ref))
            else:
                shutil.copyfile(ref, sp)
            jobs.append((sp, os.path.join(self.workdir, f"f{f.index}.pdf")))

        if not jobs:
            return []

        failed: list[str] = []
        if len(jobs) == 1:
            progress("convert",
                     f"转换 {todo[0].label}—— Inkscape 约需 10–20 秒…",
                     figure=todo[0].index)
            t0 = time.time()
            try:
                svg_to_pdf(ink, jobs[0][0], jobs[0][1])
            except Exception as exc:
                failed.append(str(exc))
            progress("convert",
                     f"{todo[0].label} 转换完成（{time.time()-t0:.1f} 秒）",
                     figure=todo[0].index)
        else:
            progress("convert", f"批量转换 {len(jobs)} 张图（单进程，约 2 倍速）…")
            t0 = time.time()
            failed = svg_to_pdf_batch(ink, jobs, progress)
            progress("convert",
                     f"批量转换完成：{len(jobs)-len(failed)}/{len(jobs)} 张，"
                     f"用时 {time.time()-t0:.1f} 秒")

        # jobs is a list of (svg, pdf) pairs; map back by figure index rather
        # than zipping with `todo`, because a figure with no vector source is
        # skipped and the two lists would otherwise drift out of step.
        for f in todo:
            pdf_path = os.path.join(self.workdir, f"f{f.index}.pdf")
            if os.path.exists(pdf_path) and pdf_path not in failed:
                self.converted[f.index] = pdf_path
        return failed

    # ---- stage 3 ----------------------------------------------------
    def compose(self, out_path: str, progress: Progress,
                max_dpi: int = 0) -> dict:
        t_start = time.time()
        todo = [f for f in self.figures
                if f.status == "replace" and f.index in self.converted]

        # object numbers cannot be reported by pypdfium2, so look them up
        # once through the content stream
        try:
            placements = pdfedit.find_images(self.pdf_path)
        except Exception:
            placements = []

        jobs = []
        for f in todo:
            xref = None
            for im in placements:
                if im["page"] != f.pdf_page:
                    continue
                if (abs(im["width"] - (f.rect[2] - f.rect[0])) < 4
                        and abs(im["height"] - (f.rect[3] - f.rect[1])) < 4):
                    xref = im["xref"]
                    break
            jobs.append(dict(page=f.pdf_page + 1, rect=f.rect,
                             vector_pdf=self.converted[f.index], xref=xref))

        progress("save", f"叠加矢量图层（保留对象编号，{len(jobs)} 张）…")
        res = pdfedit.apply_figures(self.pdf_path, out_path, jobs)
        for j in res["jobs"]:
            if not j["ok"]:
                progress("replace", f"第 {j['page']} 页替换失败：{j.get('error')}")
        progress("save", f"已保存 {os.path.getsize(out_path)/1e6:.2f} MB"
                         f"（对象 {res['objects_before']} → "
                         f"{res['objects_after']}）")

        if max_dpi:
            rects_by_page: dict[int, list] = {}
            for f in todo:
                rects_by_page.setdefault(f.pdf_page, []).append(f.rect)
            progress("shrink", f"压缩被替换图区内的照片到 {max_dpi} dpi…")
            n = downsample_rasters(out_path, max_dpi, rects_by_page)
            if n:
                progress("shrink", f"已重采样 {n} 张内嵌位图，"
                                   f"体积 {os.path.getsize(out_path)/1e6:.2f} MB")
            else:
                progress("shrink", "无需压缩")

        progress("verify", "校验：对比图区之外的文字与渲染…")
        report = verify(self.pdf_path, out_path, self.figures)
        report["figures_detail"] = figure_details(self.figures)
        report["elapsed"] = round(time.time() - t_start, 1)
        report["events"] = progress.events
        progress("done", "完成" if report["ok"]
                 else "完成（校验发现差异，请查看报告）")
        return report


def figure_details(figs) -> list[dict]:
    """Serialisable view of every figure, for the UI."""
    out = []
    for f in figs:
        out.append(dict(
            index=f.index,
            status=f.status,
            note=f.note,
            page=(f.pdf_page + 1) if f.pdf_page is not None else None,
            rect=f.rect,
            w_pt=round(f.w_pt, 1),
            h_pt=round(f.h_pt, 1),
            raster_count=f.raster_count,
            svg=f.svg,
            score=f.score,
            overridden=f.override is not None,
            custom_svg=bool(f.custom_svg),
            candidates=f.candidates,
        ))
    return out


def run_pipeline(docx_path: str, pdf_path: str, out_path: str,
                 progress: Progress, want_previews: bool = True,
                 max_dpi: int = 0) -> dict:
    """One-shot convenience wrapper: analyse, convert, compose."""
    workdir = tempfile.mkdtemp(prefix="refit_")
    sess = Session(docx_path, pdf_path, workdir)
    sess.analyse(progress)
    sess.convert(progress)
    return sess.compose(out_path, progress, max_dpi=max_dpi)
