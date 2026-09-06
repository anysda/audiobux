"""A book as the steps after extraction see it, whatever file it came from."""

from dataclasses import dataclass, field
from typing import Any, TypedDict


class Chapter(TypedDict):
    title: str
    paragraphs: list[str]


@dataclass
class Document:
    """A book with text comes as chapters; a scan comes as page images for OCR and
    no chapters. Pictures and page images are files the reader left in the book's
    folder, named relative to it."""

    title: str = ""  # "" when the file does not say: the file name will do
    author: str = ""
    chapters: list[Chapter] = field(default_factory=list)
    # a paragraph `text.mark(n)` stands for figures[n]: {"image": name} or, for a
    # table, {"text": ...}, with its "caption"
    figures: list[dict[str, Any]] = field(default_factory=list)
    # (level, title, page index) of the book's table of contents
    outline: list[tuple[int, str, int]] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    cover: bytes | None = None  # JPEG of the book's own cover
    first_page: bytes | None = None  # JPEG, the cover when nothing better is found
    # the source file itself is the reading copy: it is handed out as it came in
    keep_source: bool = False
