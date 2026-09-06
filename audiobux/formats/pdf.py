"""PDF: chapters from the text layer and the bookmarks, figures cut out of the pages
with a mark in the text; a scan comes out as page images for OCR."""

import re
from collections import Counter
from pathlib import Path
from typing import Any

import pymupdf

from ..text import CAPTION, FIGURE_SIDE, mark
from .document import Document
from .pages import from_pages, has_text_layer, top_levels

# a figure smaller than this share of the page, or narrower or lower in points, is
# a decoration; drawings need a few strokes to be a diagram or a table, a single
# frame around a note is text
MIN_SHARE = 0.03
MIN_SIDE = 40
MIN_STROKES = 4
GAP = 12


def read(path: Path, out: Path, depth: int) -> Document:
    doc = pymupdf.open(path)
    pages = [[b[4] for b in page.get_text("blocks", sort=True) if b[6] == 0] for page in doc.pages()]
    outline = [(level, title, page - 1) for level, title, page in doc.get_toc() if page >= 1]
    first = doc[0].get_pixmap(dpi=100).tobytes("jpg")
    meta = doc.metadata or {}
    book = Document(
        title=meta.get("title") or "",
        author=meta.get("author") or "",
        outline=outline,
        cover=first if looks_like_cover(doc[0]) else None,
        first_page=first,
    )
    if has_text_layer(pages):
        skip = repeated_images(doc)
        pages = [page_blocks(page, n, skip, out, book.figures) for n, page in enumerate(doc.pages())]
        book.chapters = from_pages(pages, top_levels(outline, depth))
        return book
    (out / "pages").mkdir(exist_ok=True)
    for i, page in enumerate(doc.pages()):
        book.pages.append(f"pages/{i + 1:04}.png")
        page.get_pixmap(dpi=300).save(out / book.pages[-1])
    return book


def looks_like_cover(page: pymupdf.Page) -> bool:
    """A cover is mostly picture and little text; a first page of text is not one."""
    area = page.rect.get_area()
    pictures = sum(pymupdf.Rect(i["bbox"]).intersect(page.rect).get_area() for i in page.get_image_info())
    return pictures >= 0.4 * area or len(page.get_text().strip()) < 100


# Figures


def repeated_images(doc: pymupdf.Document) -> set[str]:
    """Pictures found on three pages or more: logos and ornaments of the layout."""
    seen: Counter[str] = Counter()
    for page in doc.pages():
        seen.update({i["digest"] for i in page.get_image_info(hashes=True)})
    return {d for d, n in seen.items() if n >= 3}


def page_regions(page: pymupdf.Page, skip: set[str]) -> list[pymupdf.Rect]:
    """Rectangles of the figures on a page."""
    area = page.rect.get_area()
    found: list[tuple[pymupdf.Rect, bool]] = [
        (pymupdf.Rect(i["bbox"]) & page.rect, True) for i in page.get_image_info(hashes=True) if i["digest"] not in skip
    ]
    paths = page.get_drawings()
    for rect in page.cluster_drawings(drawings=paths) if paths else []:
        strokes = sum(len(d["items"]) for d in paths if pymupdf.Rect(d["rect"]).intersects(rect + (-1, -1, 1, 1)))
        if strokes >= MIN_STROKES:
            found.append((pymupdf.Rect(rect) & page.rect, False))
    merged: list[pymupdf.Rect] = []
    for rect, _ in found:
        rect = pymupdf.Rect(rect)
        for other in merged[:]:
            if (other + (-GAP, -GAP, GAP, GAP)).intersects(rect):
                rect |= other
                merged.remove(other)
        merged.append(rect)
    return sorted(
        (
            r
            for r in merged
            if r.width >= MIN_SIDE
            and r.height >= MIN_SIDE
            and r.get_area() >= MIN_SHARE * area
            and r.get_area() < 0.95 * area
        ),
        key=lambda r: (r.y0, r.x0),
    )


def page_blocks(page: pymupdf.Page, n: int, skip: set[str], out: Path, figures: list[dict[str, Any]]) -> list[str]:
    """The text blocks of a page in reading order with figure marks among them;
    the text inside a figure (its labels) goes with the figure."""
    blocks = [(pymupdf.Rect(b[:4]), b[4]) for b in page.get_text("blocks", sort=True) if b[6] == 0]
    regions = page_regions(page, skip)
    if not regions:
        return [t for _, t in blocks]
    items: list[tuple[float, str]] = []
    for rect, text in blocks:
        if not any(_inside(rect, region) for region in regions):
            items.append((rect.y0, text))
    for k, region in enumerate(regions):
        name = f"figures/{n + 1:04}-{k + 1}.png"
        (out / "figures").mkdir(exist_ok=True)
        zoom = min(150 / 72, FIGURE_SIDE / max(region.width, region.height))
        page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=region + (-4, -4, 4, 4)).save(out / name)
        figures.append({"image": name, "caption": _caption(blocks, region), "page": n})
        items.append((region.y0, mark(len(figures) - 1)))
    items.sort(key=lambda it: it[0])
    return [t for _, t in items]


def _inside(r: pymupdf.Rect, region: pymupdf.Rect) -> bool:
    shared: float = (r & region).get_area()
    return shared >= 0.6 * max(r.get_area(), 1)


def _caption(blocks: list[tuple[pymupdf.Rect, str]], region: pymupdf.Rect) -> str:
    best, dist = "", 60.0
    for rect, text in blocks:
        text = re.sub(r"\s+", " ", text).strip()
        if not CAPTION.match(text):
            continue
        d = min(abs(rect.y0 - region.y1), abs(region.y0 - rect.y1))
        if d < dist:
            best, dist = text, d
    return best
