# -*- coding: utf-8 -*-
"""PDF writing layer, built on pikepdf (libqpdf).

Only pikepdf is used to modify documents, because it edits objects in place
and never renumbers them. Measured on the real manuscripts:

    PyMuPDF save()    object numbers 145 -> 215, every page dict rebuilt
    pikepdf  save()   object numbers 145 -> 145, 20/20 page content streams
                      byte-identical

Keeping object numbering means everything outside the touched page stays
exactly as Word/PDFMaker wrote it, which is the strongest form of "nothing
else changed" this tool can offer.

Licence: pikepdf is MPL-2.0, libqpdf is Apache-2.0. No AGPL involved.

Coordinate systems
------------------
PDF's native origin is the BOTTOM-LEFT corner, y growing upward.
The rest of this codebase (pdfio, smart_match, core) speaks TOP-LEFT, y
growing downward, because that is how pages are described in documents.

Passing a top-left rect straight to pikepdf's overlay API shifts artwork up
by roughly its own height. Measured on Figure 1: a ~40 pt offset that cut
off the "a" panel label and duplicated the molecule strip at the bottom.
`to_pdf_rect()` below is the single place that conversion happens.
"""
from __future__ import annotations

import pikepdf


def to_pdf_rect(rect, page_height: float) -> tuple[float, float, float, float]:
    """Top-left-origin rect -> PDF bottom-left-origin rect."""
    x0, y0, x1, y1 = rect
    return (float(x0), float(page_height - y1),
            float(x1), float(page_height - y0))


def _page_height(page) -> float:
    mb = [float(v) for v in page.MediaBox]
    return mb[3] - mb[1]


def _blank_image_stream(pdf, xref: int) -> bool:
    """Make an old bitmap fully transparent, in place.

    The bitmap must not be *deleted*: its resource name is still referenced by
    a "/Im0 Do" operator in the page content stream, and removing the resource
    makes readers complain

        cannot find XObject resource 'Im0'

    It must also not simply be shrunk to a 1x1 opaque pixel. Measured: that
    pixel is scaled up to the full figure rect and painted, covering every
    axis label and legend on the page (dark pixels jumped from 45k to 505k
    while the text was still in the content stream).

    The safe approach is to leave the image's geometry alone and make it
    invisible with an /SMask of zero. A zero mask means "fully transparent"
    everywhere, so the "Do" paints nothing.

    Note: writing raw bytes and deleting /Filter does not work -- pikepdf
    re-adds /FlateDecode on save and then tries to inflate the raw bytes,
    producing garbage. The stream is therefore written through pikepdf's own
    writer with a valid, uncompressed payload.
    """
    try:
        for obj in pdf.objects:
            if obj.objgen[0] != int(xref):
                continue
            if obj.get("/Subtype") != "/Image":
                return False

            w = int(obj.Width)
            h = int(obj.Height)
            # 8-bit grayscale mask, all zeros == fully transparent
            mask = pikepdf.Stream(pdf, bytes(w * h))
            mask.Type = pikepdf.Name("/XObject")
            mask.Subtype = pikepdf.Name("/Image")
            mask.Width = w
            mask.Height = h
            mask.ColorSpace = pikepdf.Name("/DeviceGray")
            mask.BitsPerComponent = 8

            obj.SMask = mask
            # a pre-multiplied /Decode array would defeat the zero mask
            if "/Decode" in obj:
                del obj["/Decode"]
            if "/Mask" in obj:
                del obj["/Mask"]
            return True
    except Exception:
        return False
    return False


def apply_figures(pdf_in: str, pdf_out: str, jobs: list[dict]) -> dict:
    """Overlay vector figures and blank the old bitmaps, preserving objects.

    jobs: [{"page": 1-based int,
            "rect": (x0, y0, x1, y1) in TOP-LEFT coordinates,
            "vector_pdf": path to a single-page vector PDF,
            "xref": old image xref to blank (optional)}]

    Returns a report: {"objects_before", "objects_after", "jobs": [...]}.

    Why not pikepdf's add_overlay()
    -----------------------------
    add_overlay imports the page as a Form XObject and then scales it to fit
    the target rect. Measured on Figure 1: the result kept the artwork but
    rendered EVERY text run invisible -- axis labels, legends and panel
    letters were all missing, while the text operators were still present in
    the content stream. The figure looked like a blank white block with
    floating curves.

    Building the Form XObject directly (Page.as_form_xobject + an explicit
    `cm` in an appended content stream) renders correctly. That is what this
    function does.
    """
    report = {"objects_before": None, "objects_after": None, "jobs": []}

    with pikepdf.open(pdf_in) as pdf:
        report["objects_before"] = len(pdf.objects)

        for job in jobs:
            entry = dict(page=job["page"], ok=False, error=None)
            try:
                page = pdf.pages[job["page"] - 1]
                height = _page_height(page)
                pdf_rect = to_pdf_rect(job["rect"], height)
                tx, ty, x1, y1 = pdf_rect
                tw, th = x1 - tx, y1 - ty

                with pikepdf.open(job["vector_pdf"]) as src:
                    form = pikepdf.Page(src.pages[0]).as_form_xobject()
                    form = pdf.copy_foreign(form)
                    name = page.add_resource(form, pikepdf.Name("/XObject"))

                    bbox = [float(v) for v in form.BBox]
                    bw = bbox[2] - bbox[0]
                    bh = bbox[3] - bbox[1]
                    if bw <= 0 or bh <= 0:
                        raise ValueError("vector PDF has an empty BBox")

                    # Fit the figure INSIDE the original bitmap's rectangle,
                    # preserving its aspect ratio.
                    #
                    # A non-uniform scale (tw/bw, th/bh) would stretch the
                    # artwork; worse, any scale larger than the box makes the
                    # figure bleed over neighbouring text, captions and other
                    # figures. So the scale is the smaller of the two ratios,
                    # and the result is centred in the box.
                    #
                    # Example: original box 700x900, vector 800x1200 ->
                    # sx = 700/800 = 0.875, sy = 900/1200 = 0.75
                    # s = 0.75 -> placed 600x900, exactly as tall as the box
                    # and narrower than it, so nothing is covered.
                    s = min(tw / bw, th / bh)
                    # centre the scaled figure within the original rect
                    dx = tx + (tw - bw * s) / 2.0
                    dy = ty + (th - bh * s) / 2.0

                    # The BBox may not start at the origin; shift by its lower
                    # left corner so the scaling is anchored correctly.
                    ox, oy = bbox[0], bbox[1]

                    stream = pikepdf.Stream(
                        pdf,
                        f"q {s:.6f} 0 0 {s:.6f} "
                        f"{dx - ox * s:.6f} {dy - oy * s:.6f} cm "
                        f"{name} Do Q".encode(),
                    )
                    # append: the new content paints on top of the old bitmap,
                    # which is then blanked below
                    page.Contents = pikepdf.Array([page.Contents, stream])

                    entry["scale"] = round(s, 6)
                    entry["placed"] = (round(bw * s, 2), round(bh * s, 2))
                    entry["box"] = (round(tw, 2), round(th, 2))

                if job.get("xref"):
                    entry["blanched"] = _blank_image_stream(pdf, job["xref"])

                entry["ok"] = True
                entry["rect"] = pdf_rect
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"[:200]
            report["jobs"].append(entry)

        pdf.save(
            pdf_out,
            object_stream_mode=pikepdf.ObjectStreamMode.disable,
            recompress_flate=False,
        )
        report["objects_after"] = len(pdf.objects)

    return report


def find_images(pdf_path: str) -> list[dict]:
    """Locate every placed image with its rect and object number.

    Walks the page content stream: a `cm` operator sets the transform, the
    following `Do` paints an XObject. That pairing gives both the exact
    placement rect (bottom-left origin) and the object number, which the
    neutralise step needs.

    Returns: [{"page": 0-based, "xref": int, "rect_pdf": (x0,y0,x1,y1),
               "width": float, "height": float}]
    """
    out = []
    with pikepdf.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages):
            try:
                mb = [float(v) for v in page.MediaBox]
                height = mb[3] - mb[1]
                xobjects = page.get("/Resources", {}).get("/XObject", {})
                ctm = None
                for operands, op in pikepdf.parse_content_stream(page):
                    name = str(op)
                    if name == "cm":
                        ctm = [float(v) for v in operands]
                    elif name == "Do" and ctm is not None:
                        key = str(operands[0])
                        obj = xobjects.get(key) if xobjects else None
                        if obj is not None and obj.get("/Subtype") == "/Image":
                            x0, y0 = ctm[4], ctm[5]
                            x1, y1 = x0 + ctm[0], y0 + ctm[3]
                            out.append(dict(
                                page=pno,
                                xref=obj.objgen[0],
                                rect_pdf=(x0, y0, x1, y1),
                                width=abs(ctm[0]),
                                height=abs(ctm[3]),
                            ))
                        ctm = None
            except Exception:
                continue
    return out
    with pikepdf.open(path) as pdf:
        return len(pdf.pages)


def page_sizes(path: str) -> list[tuple[float, float]]:
    out = []
    with pikepdf.open(path) as pdf:
        for page in pdf.pages:
            mb = [float(v) for v in page.MediaBox]
            out.append((mb[2] - mb[0], mb[3] - mb[1]))
    return out
