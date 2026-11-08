# -*- coding: utf-8 -*-
"""Figure matching: document order first, image content only when needed.

Design rationale (measured on real manuscripts)
-----------------------------------------------
Collecting the PDF rasters and their placement rects costs 33-46 ms for the
whole document. Building an image-content signature costs 140-170 ms PER
BLOCK -- 20 blocks would take ~3 s, 100 blocks ~14 s. Document order is
therefore the right primary signal, with content correlation demoted to a
verification step that only runs when order cannot be trusted.

FAST PATH (no image decoding at all)
------------------------------------
Document order is reliable when two conditions hold:

  A. every drawing is inline (wp:inline), so Word lays them out in flow
  B. the number of PDF rasters equals the number of rasterised figures

When both hold, the k-th figure maps to the k-th raster in reading order.
Each pairing is still sanity-checked against the display size; if every pair
agrees within a few points, the mapping is accepted and no image is decoded.

SLOW PATH (content correlation)
-------------------------------
If A or B fails, or any size disagrees, the matcher falls back to comparing
32x32 thumbnails. Both sides are composited onto white first: the docx
preview PNG is RGBA whose RGB under transparency is black, and a PDF image
with an /SMask likewise has black under its transparent pixels. Comparing
one composited and one raw image inverts the picture and yields a large
NEGATIVE correlation for the correct pair (measured -0.809 before the fix,
+1.000 after).

Assignment is global and one-to-one (Hungarian), so two figures can never
claim the same raster.
"""
from __future__ import annotations

import io
import zipfile

try:
    import numpy as np
    from scipy.optimize import linear_sum_assignment
    _HAVE_SCIPY = True
except Exception:                                   # pragma: no cover
    _HAVE_SCIPY = False

THUMB = 32

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"


# ------------------------------------------------------------ preconditions

def all_drawings_inline(docx_path: str) -> bool:
    """True if every drawing in the document is inline (in flow)."""
    try:
        from lxml import etree

        with zipfile.ZipFile(docx_path) as z:
            root = etree.fromstring(z.read("word/document.xml"))
        floating = sum(
            1 for d in root.iter(f"{{{W}}}drawing")
            if d.find(f".//{{{WP}}}anchor", {"wp": WP}) is not None
        )
        return floating == 0
    except Exception:
        return False


def collect_blocks(doc) -> list[dict]:
    """Placed rasters big enough to be figures, in reading order.

    doc is a pdfio.Document (not PyMuPDF).
    """
    blocks = []
    for page in doc:
        for im in page.images():
            blocks.append(dict(
                pno=page.index,
                rect=im.rect,
                xref=None,          # filled by the caller when needed
                w_px=im.w_px,
                h_px=im.h_px,
                obj=im,
            ))
    blocks.sort(key=lambda b: (b["pno"], round(b["rect"].y0, 1),
                               round(b["rect"].x0, 1)))
    for i, b in enumerate(blocks):
        b["order"] = i
    return blocks


# ------------------------------------------------------------ signatures

def _flatten_white(pil):
    """Composite transparency onto white so both sides share one baseline."""
    from PIL import Image

    if pil.mode in ("RGBA", "LA"):
        rgba = pil.convert("RGBA")
        white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(white, rgba).convert("L")
    return pil.convert("L")


def _thumb_from_pil(pil) -> list[float] | None:
    if pil is None:
        return None
    try:
        from PIL import Image

        im = _flatten_white(pil).resize((THUMB, THUMB), Image.LANCZOS)
        px = list(im.getdata())
        mean = sum(px) / len(px)
        sd = (sum((p - mean) ** 2 for p in px) / len(px)) ** 0.5 or 1.0
        return [(p - mean) / sd for p in px]
    except Exception:
        return None


def _thumb_from_bytes(data: bytes | None) -> list[float] | None:
    if not data:
        return None
    try:
        from PIL import Image

        return _thumb_from_pil(Image.open(io.BytesIO(data)))
    except Exception:
        return None


def _corr(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / len(a)


def _png_bytes(z: zipfile.ZipFile | None, target: str | None) -> bytes | None:
    if z is None or not target:
        return None
    try:
        return z.read("word/" + target)
    except KeyError:
        return None


# ------------------------------------------------------------ matching

def match_figures(figs, doc, z: zipfile.ZipFile | None = None,
                  tol: float = 4.0, docx_path: str | None = None,
                  stats: dict | None = None) -> None:
    """Assign each rasterised figure to exactly one PDF raster.

    Sets status (replace | vector | ambiguous | no-match), pdf_page, rect,
    score, note and candidates on each figure. stats, if given, receives
    {"path": "order"|"content", "decoded": int} for reporting.

    Every figure also gets `candidates`: a ranked list of the PDF rasters it
    could plausibly be, so the UI can offer manual selection when the
    automatic decision is not good enough.
    """
    blocks = collect_blocks(doc)

    for f in figs:
        if not f.raster_count:
            f.status = "vector"
            f.note = "SVG 未内嵌位图，Word 已按矢量导出"

    need = [f for f in figs if f.raster_count]
    if not need:
        if stats is not None:
            stats.update(path="none", decoded=0)
        return
    if not blocks:
        for f in need:
            f.status = "no-match"
            f.note = "PDF 中没有可匹配的位图（可能已是矢量）"
            f.candidates = []
        if stats is not None:
            stats.update(path="none", decoded=0)
        return

    # ---- FAST PATH: document order + size agreement -------------------
    inline = all_drawings_inline(docx_path) if docx_path else False
    counts_match = len(blocks) == len(need)

    if inline and counts_match:
        pairs = list(zip(need, blocks))
        sizes_ok = all(
            abs(b["rect"].width - f.w_pt) < tol
            and abs(b["rect"].height - f.h_pt) < tol
            for f, b in pairs
        )
        if sizes_ok:
            for f, b in pairs:
                f.status = "replace"
                f.pdf_page = b["pno"]
                f.rect = b["rect"].as_tuple()
                f.score = 1.0
                f.note = (f"第 {b['pno']+1} 页"
                          f"（文档顺序 #{b['order']+1}，尺寸吻合）")
                f.candidates = [dict(
                    page=b["pno"] + 1,
                    rect=b["rect"].as_tuple(),
                    order=b["order"] + 1,
                    w_px=b["w_px"], h_px=b["h_px"],
                    score=1.0, content=None, size_ok=True,
                )]
            if stats is not None:
                stats.update(path="order", decoded=0)
            return

    # ---- SLOW PATH: content correlation -------------------------------
    b_sig = [_thumb_from_pil(b["obj"].pixels()) for b in blocks]
    f_sig = [_thumb_from_bytes(_png_bytes(z, f.png)) for f in need]
    if stats is not None:
        stats.update(path="content", decoded=len(blocks) + len(need))

    n, m = len(need), len(blocks)
    parts = []
    for i, f in enumerate(need):
        row = []
        for j, b in enumerate(blocks):
            size_ok = (abs(b["rect"].width - f.w_pt) < tol
                       and abs(b["rect"].height - f.h_pt) < tol)
            content = _corr(f_sig[i], b_sig[j])
            order = -0.04 * abs(b["order"] - i) if counts_match else 0.0
            row.append(dict(s=(1.0 if size_ok else 0.0) + content + order,
                            size_ok=size_ok, content=content))
        parts.append(row)

    if _HAVE_SCIPY:
        score = np.array([[parts[i][j]["s"] for j in range(m)]
                          for i in range(n)])
        ri, ci = linear_sum_assignment(-score)
        assigned = {i: j for i, j in zip(ri, ci)}
    else:
        pairs = sorted(
            ((parts[i][j]["s"], i, j) for i in range(n) for j in range(m)),
            reverse=True,
        )
        assigned, taken = {}, set()
        for s, i, j in pairs:
            if i in assigned or j in taken:
                continue
            assigned[i] = j
            taken.add(j)

    # every figure gets its full ranked candidate list, so the UI can offer
    # alternatives even when the automatic pick is accepted
    for i, f in enumerate(need):
        ranked = sorted(range(m), key=lambda k: -parts[i][k]["s"])
        f.candidates = [dict(
            page=blocks[j]["pno"] + 1,
            rect=blocks[j]["rect"].as_tuple(),
            order=blocks[j]["order"] + 1,
            w_px=blocks[j]["w_px"], h_px=blocks[j]["h_px"],
            score=round(parts[i][j]["s"], 3),
            content=(round(parts[i][j]["content"], 3)
                     if parts[i][j]["content"] is not None else None),
            size_ok=parts[i][j]["size_ok"],
        ) for j in ranked]

    for i, f in enumerate(need):
        j = assigned.get(i)
        if j is None:
            f.status = "no-match"
            f.note = "没有分配到可用的位图"
            continue
        p, b = parts[i][j], blocks[j]
        row_alt = max([parts[i][k]["s"] for k in range(m) if k != j], default=-9)
        col_alt = max([parts[k][j]["s"] for k in range(n) if k != i], default=-9)
        margin = p["s"] - max(row_alt, col_alt)

        strong = p["content"] >= 0.60 or (p["size_ok"] and p["content"] >= 0.30)
        if strong and (margin >= 0.10 or p["content"] >= 0.85):
            f.status = "replace"
            f.pdf_page = b["pno"]
            f.rect = b["rect"].as_tuple()
            f.score = round(p["s"], 3)
            how = []
            if p["content"] >= 0.60:
                how.append(f"图像相似度 {p['content']:.2f}")
            if p["size_ok"]:
                how.append("尺寸吻合")
            f.note = f"第 {b['pno']+1} 页（{'，'.join(how) or '综合评分'}）"
        else:
            f.status = "ambiguous"
            f.note = (f"最佳候选证据不足（相似度 {p['content']:.2f}，"
                      f"领先 {margin:.2f}），不做猜测")
