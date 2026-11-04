"""EPUB: chapters from the table of contents, paragraphs from the block elements,
pictures and tables in place with a mark in the text. The EPUB itself goes out as
the reading copy."""

import copy
import logging
import posixpath
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import ebooklib
import lxml.html
import pymupdf
from ebooklib import epub as ebook

from ..text import LISTING, code_lines, mark
from .document import Chapter, Document
from .pages import spaces

log = logging.getLogger(__name__)

BLOCK_TAGS = {"p", "div", "li", "blockquote", "pre", "dd", "dt", "h1", "h2", "h3", "h4", "h5", "h6"}


def read(path: Path, out: Path, depth: int) -> Document:
    book = ebook.read_epub(str(path), {"ignore_ncx": False})
    starts: dict[tuple[str, str | None], str] = {}
    for title, href in _flat_toc(book.toc, depth):
        file, _, anchor = unquote(href).partition("#")
        starts.setdefault((posixpath.normpath(file), anchor or None), title)

    cover = _cover(book)
    chapters: list[Chapter] = []
    figures: list[dict[str, Any]] = []
    for idref, _ in book.spine:
        item = book.get_item_with_id(idref)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        file = posixpath.normpath(item.get_name())
        root = lxml.html.fromstring(item.get_content())
        if (file, None) in starts:
            chapters.append({"title": starts[(file, None)], "paragraphs": []})
        elif not starts:
            chapters.append({"title": _first_heading(root) or f"{len(chapters) + 1}", "paragraphs": []})
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            anchor = el.get("id")
            if anchor and (file, anchor) in starts:
                chapters.append({"title": starts[(file, anchor)], "paragraphs": []})
            if not chapters or any(a.tag == "table" for a in el.iterancestors()):
                continue
            if el.tag in ("img", "image", "table"):
                figure = _figure(book, file, el, out, len(figures), cover)
                if figure:
                    figures.append(figure)
                    chapters[-1]["paragraphs"].append(mark(len(figures) - 1))
            elif el.tag == "pre" and not any(a.tag == "pre" for a in el.iterancestors()):
                if code := code_lines(_pre_text(el)):
                    chapters[-1]["paragraphs"].append(LISTING + code)
            elif (
                el.tag in BLOCK_TAGS
                and not any(c.tag in BLOCK_TAGS for c in el.iterdescendants() if isinstance(c.tag, str))
                and not any(a.tag == "pre" for a in el.iterancestors())
            ):
                if text := spaces(el.text_content()):
                    chapters[-1]["paragraphs"].append(text)

    return Document(
        title=_meta(book, "title"),
        author=_meta(book, "creator"),
        chapters=[c for c in chapters if c["paragraphs"]],
        figures=figures,
        cover=_jpeg(cover),
        keep_source=True,
    )


def _pre_text(el: lxml.html.HtmlElement) -> str:
    """A listing's text as printed: a line break inside it is a new line, a block in it a line of its own."""
    el = copy.deepcopy(el)
    if len(el) and not (el.text or "").strip():  # the space before `<code>` is only the layout of the file
        el.text = ""
    for inner in el.iterdescendants():
        if isinstance(inner.tag, str) and (inner.tag == "br" or inner.tag in BLOCK_TAGS):
            inner.tail = "\n" + (inner.tail or "")
    return str(el.text_content())


def _figure(
    book: ebook.EpubBook, file: str, el: lxml.html.HtmlElement, out: Path, n: int, cover: str | bytes | None
) -> dict[str, Any] | None:
    """A picture big enough to mean something, or a table as text; None otherwise."""
    if el.tag == "table":
        rows = [" | ".join(spaces(c.text_content()) for c in tr if isinstance(c.tag, str)) for tr in el.iter("tr")]
        text = "\n".join(r for r in rows if r.strip(" |"))
        return (
            {"text": text, "caption": spaces(" ".join(c.text_content() for c in el.iter("caption")))} if text else None
        )
    src = el.get("src") or el.get("{http://www.w3.org/1999/xlink}href") or el.get("href") or ""
    item = book.get_item_with_href(posixpath.normpath(posixpath.join(posixpath.dirname(file), unquote(src))))
    if item is None or item.get_content() == cover:
        return None
    try:
        pix = pymupdf.Pixmap(item.get_content())
        if min(pix.width, pix.height) < 100:
            return None
        if pix.alpha or pix.n > 3:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix) if pix.n - pix.alpha > 3 else pymupdf.Pixmap(pix, 0)
        (out / "figures").mkdir(exist_ok=True)
        name = f"figures/{n + 1:04}.png"
        pix.save(out / name)
    except Exception as e:
        log.warning("figure skipped: %s", e)
        return None
    caption = el.getparent().findtext(".//figcaption") if el.getparent() is not None else ""
    return {"image": name, "caption": spaces(caption or el.get("alt") or "")}


def _flat_toc(toc: Iterable[Any], depth: int, level: int = 1) -> Iterator[tuple[str, str]]:
    for entry in toc:
        if isinstance(entry, tuple):
            section, children = entry
            if level <= depth and getattr(section, "href", None):
                yield section.title, section.href
            yield from _flat_toc(children, depth, level + 1)
        elif level <= depth:
            yield entry.title, entry.href


def _first_heading(root: lxml.html.HtmlElement) -> str | None:
    for tag in ("h1", "h2", "h3"):
        for el in root.iter(tag):
            if text := spaces(el.text_content()):
                return text
    return None


def _meta(book: ebook.EpubBook, field: str) -> str:
    values = book.get_metadata("DC", field)
    return values[0][0].strip() if values else ""


def _cover(book: ebook.EpubBook) -> str | bytes | None:
    for item in book.get_items_of_type(ebooklib.ITEM_COVER):
        return item.get_content()
    for _, attrs in book.get_metadata("OPF", "cover"):
        found = book.get_item_with_id(attrs.get("content", ""))
        if found is not None:
            return found.get_content()
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        if "cover" in item.get_name().lower():
            return item.get_content()
    return None


def _jpeg(data: str | bytes | None) -> bytes | None:
    if not data:
        return None
    try:
        pix = pymupdf.Pixmap(data)
        if pix.alpha:
            pix = pymupdf.Pixmap(pix, 0)
        jpeg: bytes = pix.tobytes("jpg")
    except Exception as e:
        log.warning("cover skipped: %s", e)
        return None
    return jpeg
