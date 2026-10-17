"""The reading text of a book as blocks in order: headings, paragraphs, listings and
figures, each with the index of the paragraph that voices it. The EPUB is drawn
from these blocks and the text is timed against the sound by them, so both see the
same book.

Chapters are those of the m4b; headings deeper than that come from the book's
outline, a paragraph that reads as the next outline entry is a heading. A paragraph
close to the description of the next figure with a picture is that figure. A listing
is shown as code; its telling, when there is one, is only heard. A chapter heading
printed in several lines is one heading, voiced by the first of them.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from . import titlepage
from .cover import who_and_what

log = logging.getLogger(__name__)

# how close a paragraph must be to a figure description to stand for that figure
# (fix may have mended a few letters of it)
FIGURE_MATCH = 85
# outline entries looked ahead when one is missing from the text
LOOKAHEAD = 3
# lines a chapter heading may be printed in
HEADING_LINES = 3


@dataclass
class Block:
    kind: str  # h, p, code, figure
    text: str  # as the reader sees it; for a figure, its description
    said: int | None  # the paragraph that voices it; None when it is not heard
    level: int = 0  # of a heading: 1 for the top level of the outline
    figure: dict[str, Any] | None = None


@dataclass
class Chapter:
    title: str  # "" when the chapter has no heading of its own
    blocks: list[Block]


def titles(data: dict[str, Any], name: str) -> list[str]:
    """The title of each chapter, as the m4b and the reader both show it: an untitled
    first chapter is named after the book, a later one stays untitled. The bare name
    of the file is no title."""
    _, title = who_and_what(data.get("author", ""), data.get("title", ""), name)
    title = "" if titlepage.bare(title, name) else title
    return [c["title"] or (title if n == 0 else "") for n, c in enumerate(data["chapters"])]


def chapters(data: dict[str, Any], figures: list[dict[str, Any]], folder: Path, name: str) -> list[Chapter]:
    """The chapters of `fixed.json` data; figure images are paths inside `folder`.
    A chapter with a title starts with its heading block."""
    outline = Outline(data.get("outline", []))
    pictures = Pictures(figures, folder)
    out: list[Chapter] = []
    for chapter, heading in zip(data["chapters"], titles(data, name), strict=True):
        paragraphs: list[str] = chapter["paragraphs"]
        listings: dict[str, str] = chapter.get("listings", {})
        blocks: list[Block] = []
        first = 0
        if heading:
            first = _opening(paragraphs, listings, heading)  # the heading as the text has it
            blocks.append(Block("h", heading, 0 if first else None, outline.take(heading) or 1))
        for k in range(first, len(paragraphs)):
            p = paragraphs[k]
            if str(k) in listings:
                blocks.append(Block("code", listings[str(k)], k if p else None))
            elif (inner := outline.take(p)) is not None:
                blocks.append(Block("h", p, k, inner))
            elif (figure := pictures.take(p)) is not None:
                blocks.append(Block("figure", p, k, figure=figure))
            else:
                blocks.append(Block("p", p, k))
        out.append(Chapter(heading, blocks))
    if pictures.left:
        log.warning("%s: %d figures not found in the text, left out", name, pictures.left)
    return out


class Outline:
    """The book's outline, (level, title, page) in order; each title is taken once."""

    def __init__(self, entries: list[Sequence[Any]]) -> None:
        self.entries = [(int(e[0]), str(e[1])) for e in entries if str(e[1]).strip()]
        self.top = min((lvl for lvl, _ in self.entries), default=1)
        self.at = 0

    def take(self, text: str) -> int | None:
        """The level of the outline entry `text` reads as, 1 for the top one, or None."""
        if len(text) > 200:
            return None
        for i in range(self.at, min(self.at + LOOKAHEAD, len(self.entries))):
            if same(text, self.entries[i][1]):
                self.at = i + 1
                return self.entries[i][0] - self.top + 1
        return None


class Pictures:
    """Figures with a picture, in book order; a paragraph close to the next one's
    description is that figure."""

    def __init__(self, figures: list[dict[str, Any]], folder: Path) -> None:
        self.figures = [f for f in figures if f.get("image") and f.get("said") and (folder / f["image"]).exists()]
        self.at = 0

    @property
    def left(self) -> int:
        return len(self.figures) - self.at

    def take(self, text: str) -> dict[str, Any] | None:
        for i in range(self.at, min(self.at + LOOKAHEAD, len(self.figures))):
            if fuzz.ratio(text, self.figures[i]["said"]) >= FIGURE_MATCH:
                self.at = i + 1
                return self.figures[i]
        return None


def _opening(paragraphs: list[str], listings: dict[str, str], heading: str) -> int:
    """How many first paragraphs the heading is printed in, 0 when the text does not
    open with it; a heading broken in lines is one heading."""
    for n in range(1, min(HEADING_LINES, len(paragraphs)) + 1):
        if str(n - 1) in listings:
            break
        if same(" ".join(paragraphs[:n]), heading):
            return n
    return 0


def same(text: str, title: str) -> bool:
    a, b = _key(text), _key(title)
    if not a or not b:
        return False
    return a == b or (min(len(a), len(b)) >= 8 and fuzz.ratio(a, b) >= 92)


def _key(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())
