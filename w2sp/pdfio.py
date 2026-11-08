# -*- coding: utf-8 -*-
"""PDF reading layer, built on pypdfium2 (PDFium).

Replaces PyMuPDF for everything that needs to *look at* a PDF: rendering
pages, extracting text, and locating/extracting the images on a page.

Licence: pypdfium2 is Apache-2.0 / BSD-3-Clause, PDFium is BSD-3-Clause.
No AGPL code is involved.

Coordinate convention
--------------------
Everything in this module speaks the TOP-LEFT origin system (y grows
downward), matching how people think about a page and how PDFium reports
object bounds. The writer (pdfedit.py) converts to PDF's bottom-left system
at the last moment.
"""
from __future__ import annotations

import io

import pypdfium2 as pdfium

# PDFium object type constants
OBJ_TEXT = 1
OBJ_PATH = 2
OBJ_IMAGE = 3
OBJ_SHADING = 4
OBJ_FORM = 5


class Rect:
    """Minimal rectangle, top-left origin (matches the old PyMuPDF semantics)."""

    __slots__ = ("x0", "y0", "x1", "y1")

    def __init__(self, x0, y0, x1, y1):
        self.x0, self.y0, self.x1, self.y1 = float(x0), float(y0), float(x1), float(y1)

    @property
    def width(self):
        return self.x1 - self.x0

    @property
    def height(self):
        return self.y1 - self.y0

    @property
    def area(self):
        return max(0.0, self.width) * max(0.0, self.height)

    def contains(self, x, y) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1

    def intersects(self, other) -> bool:
        return not (other.x1 < self.x0 or other.x0 > self.x1
                    or other.y1 < self.y0 or other.y0 > self.y1)

    def as_tuple(self):
        return (self.x0, self.y0, self.x1, self.y1)

    def __repr__(self):
        return f"Rect({self.x0:.1f}, {self.y0:.1f}, {self.x1:.1f}, {self.y1:.1f})"


class ImageObject:
    """A placed image on a page."""

    __slots__ = ("page_index", "rect", "matrix", "obj", "w_px", "h_px")

    def __init__(self, page_index, rect, matrix, obj, w_px, h_px):
        self.page_index = page_index
        self.rect = rect
        self.matrix = matrix
        self.obj = obj
        self.w_px = w_px
        self.h_px = h_px

    def pixels(self):
        """Extracted pixels as a PIL image, transparency flattened onto white."""
        try:
            from PIL import Image

            bm = self.obj.get_bitmap(render=False)
            pil = bm.to_pil()
            if pil.mode in ("RGBA", "LA"):
                rgba = pil.convert("RGBA")
                white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                return Image.alpha_composite(white, rgba).convert("RGB")
            return pil.convert("RGB")
        except Exception:
            return None

    def __repr__(self):
        return (f"ImageObject(p{self.page_index + 1}, {self.rect.width:.1f}x"
                f"{self.rect.height:.1f}, {self.w_px}x{self.h_px}px)")


class Page:
    """Thin wrapper adding the conveniences the pipeline needs."""

    def __init__(self, doc: "Document", index: int):
        self._doc = doc
        self.index = index
        self._page = doc._pdf[index]

    def size(self) -> tuple[float, float]:
        """(width, height) in points."""
        return self._page.get_size()

    def rect(self) -> Rect:
        w, h = self.size()
        return Rect(0, 0, w, h)

    def render(self, dpi: int = 150, clip: Rect | None = None):
        """Render to a PIL image (RGB), optionally limited to `clip`.

        PDFium's `crop` parameter takes margins in *points* from the page
        edges, which is both simpler and exact. Passing a wrong rect here
        silently yields a valid-looking image of the wrong area (measured:
        a 170 dpi crop of Figure 1 came back as an empty white block while
        the page itself rendered perfectly).

        crop = (left, bottom, right, top) margins, bottom-left origin, so a
        top-left-origin rect (x0,y0,x1,y1) becomes
        (x0, page_h - y1, page_w - x1, y0).
        """
        pw, ph = self.size()
        kwargs = {}
        if clip is not None:
            kwargs["crop"] = (max(0.0, clip.x0),
                              max(0.0, ph - clip.y1),
                              max(0.0, pw - clip.x1),
                              max(0.0, clip.y0))
        bmp = self._page.render(scale=dpi / 72.0, **kwargs)
        return bmp.to_pil().convert("RGB")

    def text(self) -> str:
        """All text on the page, in extraction order."""
        try:
            return self._page.get_textpage().get_text_range()
        except Exception:
            return ""

    def words(self) -> list[tuple]:
        """Words with their boxes: [(x0, y0, x1, y1, text), ...], top-left origin.

        PDFium exposes characters, not words, so characters are grouped into
        runs separated by whitespace. Grouping keeps the verification cheap:
        the pipeline compares a few hundred words per page, not thousands of
        characters.
        """
        out = []
        try:
            tp = self._page.get_textpage()
            n = tp.count_chars()
            h = self.size()[1]

            cur = None          # [x0, y0, x1, y1, chars]
            for i in range(n):
                try:
                    ch = tp.get_text_range(i, 1)
                except Exception:
                    continue
                if ch in ("\r", "\n"):
                    if cur:
                        out.append(self._finish_word(cur))
                        cur = None
                    continue
                box = tp.get_charbox(i)          # (l, b, r, t) bottom-left
                if ch.strip() == "":
                    if cur:
                        out.append(self._finish_word(cur))
                        cur = None
                    continue
                x0, x1 = box[0], box[2]
                y0, y1 = h - box[3], h - box[1]
                if cur is None:
                    cur = [x0, y0, x1, y1, ch]
                else:
                    # a big horizontal gap also ends a word
                    if x0 - cur[2] > (cur[3] - cur[1]) * 0.8:
                        out.append(self._finish_word(cur))
                        cur = [x0, y0, x1, y1, ch]
                    else:
                        cur[0] = min(cur[0], x0)
                        cur[1] = min(cur[1], y0)
                        cur[2] = max(cur[2], x1)
                        cur[3] = max(cur[3], y1)
                        cur[4] += ch
            if cur:
                out.append(self._finish_word(cur))
        except Exception:
            pass
        return out

    @staticmethod
    def _finish_word(cur):
        x0, y0, x1, y1, text = cur
        return (round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1), text)

    def images(self) -> list[ImageObject]:
        """Every placed image on the page, with its placement rect."""
        out = []
        try:
            for obj in self._page.get_objects():
                if obj.type != OBJ_IMAGE:
                    continue
                try:
                    m = obj.get_matrix()      # a b c d e f  (bottom-left)
                    b = obj.get_bounds()      # (left, bottom, right, top)
                    w_px, h_px = obj.get_px_size()
                except Exception:
                    continue
                h = self.size()[1]
                rect = Rect(b[0], h - b[3], b[2], h - b[1])
                if rect.width < 20 or rect.height < 20:
                    continue              # 1x1 placeholders, stray masks
                out.append(ImageObject(self.index, rect, m, obj, w_px, h_px))
        except Exception:
            pass
        return out


class Document:
    """Open a PDF for reading."""

    def __init__(self, path: str):
        self.path = path
        self._pdf = pdfium.PdfDocument(path)
        self.page_count = len(self._pdf)

    def page(self, index: int) -> Page:
        return Page(self, index)

    def __iter__(self):
        for i in range(self.page_count):
            yield self.page(i)

    def close(self):
        try:
            self._pdf.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
