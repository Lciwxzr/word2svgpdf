# -*- coding: utf-8 -*-
"""Starlette server for the vector-figure refit tool.

The workflow is deliberately two-stage so the user can inspect the automatic
figure matching BEFORE paying for the slow Inkscape conversions:

    POST /api/upload        store the files, run analyse() only
    GET  /api/status        progress events + per-figure match details
    POST /api/override      pin a figure to a candidate, or skip it
    POST /api/convert       run convert() + compose(), produce the PDF
    GET  /api/download      the refitted PDF
    GET  /api/preview       PNG of a figure area in ORIGINAL or RESULT
    GET  /api/candidate     PNG thumbnail of one candidate placement

Everything the UI needs about a figure -- its status, the chosen placement,
and the ranked candidates with thumbnails -- comes from /api/status, so the
user can correct a wrong mapping and simply press convert again.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import uuid

from starlette.applications import Starlette
from starlette.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.routing import Route

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _job_dir(job_id: str) -> str:
    d = os.path.join(tempfile.gettempdir(), "vectorfig", job_id)
    os.makedirs(d, exist_ok=True)
    return d


async def index(request):
    with open(os.path.join(HERE, "static", "index.html"), encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


async def upload(request):
    """Store the files and run the analysis stage only.

    Analysis is fast (tens of milliseconds when document order is reliable),
    so the UI can show the match table immediately and let the user fix it
    before any Inkscape work starts.
    """
    form = await request.form()
    docx = form.get("docx")
    pdf = form.get("pdf")
    if docx is None or pdf is None:
        return JSONResponse({"error": "需要同时提供 .docx 和 .pdf"}, status_code=400)

    try:
        dpi = int(form.get("dpi", 0))
    except (TypeError, ValueError):
        dpi = 0
    if dpi < 0:
        dpi = 0

    job_id = uuid.uuid4().hex[:12]
    d = _job_dir(job_id)
    docx_path = os.path.join(d, "in.docx")
    pdf_path = os.path.join(d, "in.pdf")
    with open(docx_path, "wb") as fh:
        fh.write(await docx.read())
    with open(pdf_path, "wb") as fh:
        fh.write(await pdf.read())

    from .core import Progress, Session

    sess = Session(docx_path, pdf_path, os.path.join(d, "work"))
    job = dict(id=job_id, dir=d, docx=docx_path, pdf=pdf_path,
               out=os.path.join(d, "out.pdf"), dpi=dpi,
               session=sess,
               events=[], report=None, error=None,
               analysing=True, converting=False, ready=False,
               name_docx=docx.filename, name_pdf=pdf.filename)

    with JOBS_LOCK:
        JOBS[job_id] = job

    asyncio.get_running_loop().run_in_executor(None, _analyse, job_id)
    return JSONResponse({"job": job_id})


def _analyse(job_id: str) -> None:
    from .core import Progress

    job = JOBS[job_id]

    def cb(ev):
        job["events"].append(ev)

    try:
        job["session"].analyse(Progress(cb))
        job["report"] = dict(figures_detail=_details(job), ok=None)
    except Exception as exc:
        job["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        job["analysing"] = False


def _details(job) -> list[dict]:
    from .core import figure_details
    return figure_details(job["session"].figures)


def _convert_worker(job_id: str) -> None:
    from .core import Progress

    job = JOBS[job_id]

    def cb(ev):
        job["events"].append(ev)

    try:
        sess = job["session"]
        # re-analyse so any overrides take effect before converting
        sess.analyse(Progress(cb))
        sess.convert(Progress(cb))
        report = sess.compose(job["out"], Progress(cb), max_dpi=job["dpi"])
        report["figures_detail"] = _details(job)
        job["report"] = report
        job["ready"] = True
    except Exception as exc:
        job["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        job["converting"] = False


async def status(request):
    job_id = request.path_params["job"]
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return JSONResponse({
        "analysing": job["analysing"],
        "converting": job["converting"],
        "ready": job["ready"],
        "events": job["events"],
        "report": job["report"],
        "error": job["error"],
        "name_docx": job["name_docx"],
        "name_pdf": job["name_pdf"],
        "dpi": job["dpi"],
        "figures": _details(job),
    })


async def override(request):
    """Pin a figure to a candidate placement, clear the pin, or skip it.

    Body: {"index": int, "action": "pick"|"clear"|"skip",
           "page": int (1-based), "rect": [x0,y0,x1,y1]}
    """
    job_id = request.path_params["job"]
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "no such job"}, status_code=404)
    if job["converting"]:
        return JSONResponse({"error": "正在转换，请稍候"}, status_code=409)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)

    idx = int(body.get("index", 0))
    action = body.get("action", "pick")
    fig = next((f for f in job["session"].figures if f.index == idx), None)
    if fig is None:
        return JSONResponse({"error": f"no figure {idx}"}, status_code=404)

    if action == "clear":
        fig.override = None
    elif action == "skip":
        fig.override = "skip"
    else:
        page = int(body["page"]) - 1
        rect = tuple(float(v) for v in body["rect"])
        fig.override = (page, rect)

    # apply_override restores fig.auto when the override is cleared, so this
    # is safe to call in every branch.
    fig.apply_override()
    # a changed mapping invalidates the previously composed PDF
    job["ready"] = False
    job["report"] = dict(figures_detail=_details(job), ok=None)
    return JSONResponse({"ok": True, "figures": _details(job)})


async def convert(request):
    """Run the slow stage: Inkscape conversions, then compose + verify."""
    job_id = request.path_params["job"]
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "no such job"}, status_code=404)
    if job["converting"]:
        return JSONResponse({"error": "already converting"}, status_code=409)

    job["converting"] = True
    job["error"] = None
    job["events"] = []
    asyncio.get_running_loop().run_in_executor(None, _convert_worker, job_id)
    return JSONResponse({"ok": True})


async def download(request):
    job_id = request.path_params["job"]
    job = JOBS.get(job_id)
    if not job or not os.path.exists(job["out"]):
        return JSONResponse({"error": "not ready"}, status_code=404)
    base = os.path.splitext(job["name_pdf"])[0]
    return FileResponse(job["out"], media_type="application/pdf",
                        filename=f"{base}_vec.pdf")


def _png_response(data: bytes):
    return HTMLResponse(data, media_type="image/png")


async def preview(request):
    """?job=..&which=orig|result&page=1&rect=x0,y0,x1,y1"""
    job_id = request.query_params["job"]
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "no such job"}, status_code=404)

    which = request.query_params.get("which", "orig")
    page = int(request.query_params["page"])
    rect = [float(v) for v in request.query_params["rect"].split(",")]
    dpi = int(request.query_params.get("dpi", 130))

    path = job["pdf"] if which == "orig" else job["out"]
    if not os.path.exists(path):
        return JSONResponse({"error": "result not ready"}, status_code=404)

    from .core import render_rect
    return _png_response(render_rect(path, page, tuple(rect), dpi=dpi))


async def page_image(request):
    """?job=..&which=orig|result&page=1&dpi=110"""
    job_id = request.query_params["job"]
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "no such job"}, status_code=404)
    which = request.query_params.get("which", "orig")
    page = int(request.query_params["page"])
    dpi = int(request.query_params.get("dpi", 110))
    path = job["pdf"] if which == "orig" else job["out"]
    if not os.path.exists(path):
        return JSONResponse({"error": "result not ready"}, status_code=404)

    from .core import render_page
    return _png_response(render_page(path, page, dpi=dpi))


async def reference(request):
    """Reference image for a figure: the PNG Word rendered, from the docx.

    This is what the user compares against the candidates. The PDF raster is
    also available (which=pdf) but it is the low-dpi version Word exported,
    so the docx PNG is the better reference.
    """
    job_id = request.query_params["job"]
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "no such job"}, status_code=404)

    idx = int(request.query_params["index"])
    fig = next((f for f in job["session"].figures if f.index == idx), None)
    if fig is None:
        return JSONResponse({"error": f"no figure {idx}"}, status_code=404)

    which = request.query_params.get("which", "docx")
    if which == "pdf" and fig.rect and fig.pdf_page is not None:
        from .core import render_rect
        return _png_response(render_rect(job["pdf"], fig.pdf_page + 1,
                                         fig.rect, dpi=110))

    # docx preview PNG, downscaled so the browser gets something small
    try:
        data = job["session"].zip.read("word/" + fig.png)
    except Exception:
        return JSONResponse({"error": "no reference image"}, status_code=404)

    try:
        import io as _io

        from PIL import Image

        im = Image.open(_io.BytesIO(data))
        if im.mode in ("RGBA", "LA"):
            rgba = im.convert("RGBA")
            white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            im = Image.alpha_composite(white, rgba).convert("RGB")
        else:
            im = im.convert("RGB")
        im.thumbnail((640, 640), Image.LANCZOS)
        buf = _io.BytesIO()
        im.save(buf, format="PNG")
        return _png_response(buf.getvalue())
    except Exception:
        return _png_response(data)


async def upload_svg(request):
    """Attach a user-supplied SVG to a figure.

    Body: multipart with fields `index` and `svg`.
    The file is stored in the job directory; conversion then uses it instead
    of the SVG embedded in the docx. Any cached conversion for that figure is
    dropped so the next convert() re-runs.
    """
    job_id = request.path_params["job"]
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "no such job"}, status_code=404)
    if job["converting"]:
        return JSONResponse({"error": "正在转换，请稍候"}, status_code=409)

    form = await request.form()
    svg = form.get("svg")
    if svg is None:
        return JSONResponse({"error": "缺少 svg 文件"}, status_code=400)
    try:
        idx = int(form.get("index", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "index 无效"}, status_code=400)

    fig = next((f for f in job["session"].figures if f.index == idx), None)
    if fig is None:
        return JSONResponse({"error": f"no figure {idx}"}, status_code=404)

    path = os.path.join(job["dir"], f"custom_{idx}.svg")
    with open(path, "wb") as fh:
        fh.write(await svg.read())

    fig.custom_svg = path
    # the cached conversion is stale now
    job["session"].converted.pop(idx, None)
    job["ready"] = False

    return JSONResponse({"ok": True, "figures": _details(job)})


async def clear_svg(request):
    """Drop a user-supplied SVG and fall back to the docx one."""
    job_id = request.path_params["job"]
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "no such job"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)

    idx = int(body.get("index", 0))
    fig = next((f for f in job["session"].figures if f.index == idx), None)
    if fig is None:
        return JSONResponse({"error": f"no figure {idx}"}, status_code=404)

    fig.custom_svg = None
    job["session"].converted.pop(idx, None)
    job["ready"] = False
    return JSONResponse({"ok": True, "figures": _details(job)})


async def candidate(request):
    """Thumbnail of one candidate placement, from the ORIGINAL pdf.

    ?job=..&page=1&rect=x0,y0,x1,y1&dpi=80
    Used by the match table so a user can see what each candidate looks like
    before pinning a figure to it.
    """
    job_id = request.query_params["job"]
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "no such job"}, status_code=404)
    page = int(request.query_params["page"])
    rect = [float(v) for v in request.query_params["rect"].split(",")]
    dpi = int(request.query_params.get("dpi", 80))

    from .core import render_rect
    return _png_response(render_rect(job["pdf"], page, tuple(rect), dpi=dpi))


app = Starlette(routes=[
    Route("/", index),
    Route("/api/upload", upload, methods=["POST"]),
    Route("/api/status/{job}", status),
    Route("/api/override/{job}", override, methods=["POST"]),
    Route("/api/convert/{job}", convert, methods=["POST"]),
    Route("/api/download/{job}", download),
    Route("/api/preview", preview),
    Route("/api/page", page_image),
    Route("/api/candidate", candidate),
    Route("/api/reference", reference),
    Route("/api/upload_svg/{job}", upload_svg, methods=["POST"]),
    Route("/api/clear_svg/{job}", clear_svg, methods=["POST"]),
])


def pick_port(preferred: int = 8770, tries: int = 40) -> int:
    """Return a TCP port that can actually be bound on 127.0.0.1.

    A fixed port is not reliable in practice. Windows reserves whole ranges
    for Hyper-V / WSL / Docker and binding inside one fails with

        [Errno 13] an attempt was made to access a socket in a way
        forbidden by its access permissions

    measured on this machine, where 8672-8771 is reserved and the original
    choice of 8770 landed right in the middle of it. Ports can also be taken
    by another program (a stale server, a browser, a VPN client).

    So the preferred port is tried first, then a small range around it.
    """
    import socket

    candidates = [preferred] + [
        p for p in range(preferred + 10, preferred + 10 + tries * 2, 2)
    ]
    for port in candidates:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    raise SystemExit("no usable TCP port found on 127.0.0.1")


def main() -> None:
    import uvicorn

    port = int(os.environ.get("VECTORFIG_PORT", "0")) or pick_port()
    # run.py reads this line to open the browser on the real port.
    print(f"VECTORFIG_URL=http://127.0.0.1:{port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
