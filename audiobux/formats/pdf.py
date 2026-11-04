"""PDF: chapters from the text layer and the bookmarks, or the printed contents when
there are none; figures cut out of the pages with a mark in the text; a scan comes
out as page images for OCR."""

import re
from collections import Counter
from pathlib import Path
from typing import Any

import pymupdf

from ..text import CAPTION, FIGURE_SIDE, LISTING, mark
from . import contents
from .document import Document
from .pages import from_pages, has_text_layer, top_levels

# a figure smaller than this share of the page, or narrower or lower in points, is
# a decoration; drawings need a few strokes to be a diagram or a table, a single
# frame around a note is text
MIN_SHARE = 0.03
MIN_SIDE = 40
MIN_STROKES = 4
GAP = 12
# a font is monospaced when its letters, a few different ones at least, are all as wide;
# a block mostly in it, of more than one line, is a listing
MONO_LETTERS, MONO_SPREAD, MONO_SHARE = 6, 0.03, 0.8


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
        rows = [page_rows(page) for page in doc.pages()]
        listing = contents.listing_pages(rows)
        skip = contents.not_text(rows, listing)
        cases = contents.Cases(b for page in pages for b in page)
        book.outline = book.outline or contents.printed(rows, listing, skip, cases)
        if not book.outline:
            skip |= contents.front(rows, skip)
            book.outline = contents.headings(rows, skip)
        book.outline = [(level, cases.shown(title), n) for level, title, n in book.outline]
        logos = repeated_images(doc)
        mono = monospaced(doc)
        pages = [
            [] if n in skip else page_blocks(page, n, logos, out, book.figures, mono)
            for n, page in enumerate(doc.pages())
        ]
        book.chapters = from_pages(pages, top_levels(book.outline, depth))
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


def page_rows(page: pymupdf.Page) -> list[contents.Row]:
    """The printed lines of a page in reading order with the size of their type; words
    spread apart along one line come together."""
    rows: list[contents.Row] = []
    blocks = [(pymupdf.Rect(b["bbox"]), b) for b in page.get_text("dict")["blocks"] if not b["type"]]
    for block in reading_order(blocks, page.rect.width):
        lines: list[list[dict[str, Any]]] = []
        for line in sorted(block["lines"], key=lambda ln: (ln["bbox"][3], ln["bbox"][0])):
            if lines and abs(line["bbox"][3] - lines[-1][0]["bbox"][3]) < 3:
                lines[-1].append(line)
            else:
                lines.append([line])
        for row in lines:
            row.sort(key=lambda ln: ln["bbox"][0])
            text = " ".join(" ".join("".join(s["text"] for s in ln["spans"]) for ln in row).split())
            if text:
                rows.append((text, max(s["size"] for ln in row for s in ln["spans"])))
    return rows


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


def monospaced(doc: pymupdf.Document) -> set[str]:
    """The fonts code is set in: their letters all of one width."""
    widths: dict[str, list[float]] = {}
    letters: dict[str, set[str]] = {}
    for page in doc.pages():
        for block in page.get_text("rawdict")["blocks"]:
            for span in (s for line in block.get("lines", []) for s in line["spans"]):
                for c in span["chars"]:
                    if c["c"].isalpha() and span["size"]:
                        widths.setdefault(span["font"], []).append((c["bbox"][2] - c["bbox"][0]) / span["size"])
                        letters.setdefault(span["font"], set()).add(c["c"])
    return {
        font
        for font, w in widths.items()
        if len(letters[font]) >= MONO_LETTERS and max(w) - min(w) <= MONO_SPREAD * max(w)
    }


def _listings(page: pymupdf.Page, mono: set[str]) -> set[tuple[float, ...]]:
    """The boxes of the blocks of a page set in a monospaced font."""
    found = set()
    for block in page.get_text("dict")["blocks"]:
        spans = [s for line in block.get("lines", []) for s in line["spans"]]
        chars = sum(len(s["text"].strip()) for s in spans)
        code = sum(len(s["text"].strip()) for s in spans if s["font"] in mono)
        if len(block.get("lines", [])) > 1 and chars and code >= MONO_SHARE * chars:
            found.add(_box(block["bbox"]))
    return found


def page_blocks(
    page: pymupdf.Page, n: int, skip: set[str], out: Path, figures: list[dict[str, Any]], mono: set[str]
) -> list[str]:
    """The text blocks of a page in reading order with figure marks among them;
    the text inside a figure (its labels) goes with the figure; a listing is marked."""
    code = _listings(page, mono) if mono else set()
    blocks = [
        (pymupdf.Rect(b[:4]), LISTING + b[4] if _box(b[:4]) in code else b[4])
        for b in page.get_text("blocks", sort=True)
        if b[6] == 0
    ]
    regions = page_regions(page, skip)
    items: list[tuple[pymupdf.Rect, str | pymupdf.Rect]] = [
        (rect, text) for rect, text in blocks if not any(_inside(rect, region) for region in regions)
    ]
    items += [(region, region) for region in regions]
    texts: list[str] = []
    for item in reading_order(items, page.rect.width):
        if isinstance(item, str):
            texts.append(item)
            continue
        name = f"figures/{n + 1:04}-{regions.index(item) + 1}.png"
        (out / "figures").mkdir(exist_ok=True)
        zoom = min(150 / 72, FIGURE_SIDE / max(item.width, item.height))
        page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=item + (-4, -4, 4, 4)).save(out / name)
        figures.append({"image": name, "caption": _caption(blocks, item), "page": n})
        texts.append(mark(len(figures) - 1))
    return texts


def reading_order[T](items: list[tuple[pymupdf.Rect, T]], width: float) -> list[T]:
    """Blocks in the order they are read: down the page, and where two columns stand
    side by side, the left one before the right one. A block across the middle (a
    heading, a wide figure) ends such a stretch."""
    mid, gap = width / 2, width / 50
    out: list[T] = []
    stretch: list[tuple[pymupdf.Rect, T]] = []

    def flush() -> None:
        left = [it for it in stretch if it[0].x1 <= mid + gap]
        right = [it for it in stretch if it[0].x1 > mid + gap]
        beside = any(a.y0 < b.y1 and b.y0 < a.y1 for a, _ in left for b, _ in right)
        by_y = sorted(left, key=lambda it: it[0].y0) + sorted(right, key=lambda it: it[0].y0)
        out.extend(t for _, t in (by_y if beside else sorted(stretch, key=lambda it: it[0].y0)))
        stretch.clear()

    for rect, item in sorted(items, key=lambda it: it[0].y0):
        if rect.x0 < mid - gap and rect.x1 > mid + gap:
            flush()
            out.append(item)
        else:
            stretch.append((rect, item))
    flush()
    return out


def _box(bbox: Any) -> tuple[float, ...]:
    return tuple(round(x, 1) for x in bbox)


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
