"""A finished book read again by the reading of today, its sound left as it is.

The reading of a book (title and author, chapters) gets better from version to
version; the voice takes hours. A book finished by an older reading is brought up to
date without being voiced again: its source gives the title, the author and the
chapter headings once more, and the headings are found in the text the book was
voiced from, as `sync.json` has it with its times. The text before the first of them
(a title page, a table of contents read out) is a chapter of its own, named after the
book. The m4b gets the new tags and chapter marks around the same sound, the EPUB is
built again from the same text; the pieces of speech are not needed, nor are they kept.
"""

import itertools
import json
import logging
import os
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from . import epub, formats, m4b, reading, sync, titlepage
from .book import Book
from .formats.document import Chapter
from .pipeline import Context

log = logging.getLogger(__name__)

# the reading a book is done with; a finished book with a smaller one is read again
VERSION = 2
# A heading is told from its line in a table of contents by what follows it: the first
# paragraphs of the chapter are looked for among the blocks after it (an older reading
# may have put a page's blocks out of order), by their first characters
FOLLOWING, AMONG, OPENING = 3, 8, 100
# how close those paragraphs must be, on average, to the closest of those blocks
FOLLOWING_MATCH = 60

Timed = list[dict[str, Any]]  # sentences with their t0 and t1


def run(folder: Path, name: str, work: Path, ctx: Context) -> dict[str, Any]:
    """Reads the book in `folder` again; `work` is for what is made on the way. Returns
    its title, author and, if it has a `sync.json`, chapters with their start; the files
    in `folder` are replaced only once everything is made."""
    work.mkdir(parents=True, exist_ok=True)
    source = next(folder.glob("source.*"))
    doc = formats.read(source, work, ctx.settings.toc_depth)
    try:
        title, author = titlepage.find(doc, source, name, ctx.models.llm, ctx.quiet.wait)
    finally:
        ctx.models.release_llm()
    found: dict[str, Any] = {"title": title, "author": author}
    if not (folder / "sync.json").exists():
        return found
    old = json.loads((folder / "sync.json").read_text())
    data, voiced = regroup(old, [] if doc.pages else doc.chapters, title, author, doc.outline)
    figures = [{"image": b["src"], "said": b["alt"]} for ch in old["chapters"] for b in ch["blocks"] if b.get("src")]
    synced = retimed(reading.chapters(data, figures, folder, name), voiced, old["duration"])
    titles = reading.titles(data, name)
    found["chapters"] = [
        {"title": titles[n], "start": c["t0"]} for n, c in enumerate(synced["chapters"]) if n == 0 or c["t1"] > c["t0"]
    ]
    made = Book(work)
    made.write("sync.json", synced)
    ready = ["sync.json"]
    if not doc.keep_source:
        cover = folder / "cover.jpg"
        try:
            epub.write(data, figures, folder, made.path("book.epub"), name, cover if cover.exists() else None)
            ready.append("book.epub")
        except Exception:
            log.exception("%s: the EPUB is left as it was", name)
    if (folder / "book.m4b").exists():
        meta = {"title": title, "artist": author, "album": title, "genre": "Audiobook"}
        m4b.retag(folder / "book.m4b", made.path("book.m4b"), meta, found["chapters"], old["duration"])
        ready.append("book.m4b")
    for file in ready:
        os.replace(made.path(file), folder / file)
    return found


def regroup(
    old: dict[str, Any], chapters: list[Chapter], title: str, author: str, outline: list[tuple[int, str, int]]
) -> tuple[dict[str, Any], list[list[Timed]]]:
    """The voiced text of `old` (a `sync.json`) cut into `chapters` at their headings:
    the data `reading` takes, a paragraph for each old block, and the timed sentences of
    each paragraph. A heading of the old reading nobody heard is left out."""
    blocks = [b for ch in old["chapters"] for b in ch["blocks"] if b["type"] != "h" or b["t0"] is not None]
    texts = [_text(b) for b in blocks]
    cuts = [(0, "")]
    for at, heading in headings(texts, chapters):
        if at == 0:
            cuts[0] = (0, heading)
        else:
            cuts.append((at, heading))
    out: list[dict[str, Any]] = []
    voiced: list[list[Timed]] = []
    for (start, heading), (end, _) in zip(cuts, [*cuts[1:], (len(blocks), "")], strict=True):
        part = blocks[start:end]
        listings = {str(k): b["text"] for k, b in enumerate(part) if b["type"] == "code"}
        out.append({"title": heading, "paragraphs": [_said(b) for b in part], "listings": listings})
        voiced.append([_timed(b) for b in part])
    return {"title": title, "author": author, "chapters": out, "outline": outline}, voiced


def headings(texts: list[str], chapters: list[Chapter]) -> list[tuple[int, str]]:
    """(index in `texts`, title) of each chapter heading found, in order: a text that
    reads as the title, or as its first line, or starts with it, with the chapter's first
    paragraphs among the texts after it. A line of a table of contents has other titles
    after it."""
    stream: list[str] = []
    wanted: list[tuple[str, list[str], int]] = []  # a title, its lines, where its text starts in the stream
    for ch in chapters:
        title, paragraphs = ch["title"], ch["paragraphs"]
        lines = _lines(title, paragraphs)
        if title:
            wanted.append((title, [title, *lines[:1]], len(stream) + len(lines)))
        stream += paragraphs
    found: list[tuple[int, str]] = []
    after = 0
    for title, lines, at in wanted:
        mine = _openings(stream[at : at + FOLLOWING])
        best, score = None, float(FOLLOWING_MATCH)
        for i in range(after, len(texts)):
            if not (any(reading.same(texts[i], x) for x in lines) or _key(texts[i]).startswith(_key(title))):
                continue
            theirs = _openings(texts[i + 1 : i + 1 + AMONG])
            got = sum(max((fuzz.ratio(m, t) for t in theirs), default=0) for m in mine) / len(mine) if mine else 100
            if got >= score:  # of two alike the later one: a table of contents comes first
                best, score = i, got
        if best is not None:
            found.append((best, title))
            after = best + 1
    return found


def retimed(chapters: list[reading.Chapter], voiced: list[list[Timed]], duration: float) -> dict[str, Any]:
    """`sync.json` of the chapters, each block with the times of the paragraph that voices it.
    A chapter ends where the next one heard starts; the first one starts with the sound."""
    out: list[dict[str, Any]] = []
    for chapter, said in zip(chapters, voiced, strict=True):
        blocks: list[dict[str, Any]] = []
        for block in chapter.blocks:
            timed = said[block.said] if block.said is not None else []
            if block.kind == "p":
                blocks.append({"type": "p", "sentences": timed})
                continue
            t0, t1 = _span(timed)
            if block.kind == "figure":
                assert block.figure is not None
                item = {"type": "figure", "src": block.figure["image"], "alt": block.text}
            else:
                item = {"type": block.kind, "text": block.text}
            blocks.append(item | {"t0": t0, "t1": t1})
        out.append({"title": chapter.title, "blocks": blocks})
    end = duration
    spans: list[tuple[float, float]] = []
    for n in reversed(range(len(out))):
        heard = [t for b in out[n]["blocks"] for s in b.get("sentences", [b]) if (t := s["t0"]) is not None]
        begin = 0.0 if n == 0 else min(heard[0] if heard else end, end)
        spans.append((begin, end))
        end = begin
    timed_chapters = [c | {"t0": t0, "t1": t1} for c, (t0, t1) in zip(out, reversed(spans), strict=True)]
    return {"version": sync.VERSION, "duration": duration, "chapters": timed_chapters}


def _text(block: dict[str, Any]) -> str:
    if block["type"] == "p":
        return " ".join(s["text"] for s in block["sentences"])
    return str(block.get("alt") or block.get("text") or "")


def _said(block: dict[str, Any]) -> str:
    """The paragraph of a block for `reading`: a listing nobody heard has none."""
    return "" if block["type"] == "code" and block["t0"] is None else _text(block)


def _timed(block: dict[str, Any]) -> Timed:
    if block["type"] == "p":
        return list(block["sentences"])
    return [{"text": _text(block), "t0": block["t0"], "t1": block["t1"]}]


def _span(timed: Timed) -> tuple[float | None, float | None]:
    starts = [s["t0"] for s in timed if s["t0"] is not None]
    ends = [s["t1"] for s in timed if s["t1"] is not None]
    return (starts[0], ends[-1]) if starts and ends else (None, None)


def _lines(title: str, paragraphs: list[str]) -> list[str]:
    """The heading as the text has it at the start of the chapter, maybe in more than one line."""
    whole = _key(title)
    return list(itertools.takewhile(lambda p: bool(k := _key(p)) and k in whole, paragraphs))


def _key(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def _openings(texts: list[str]) -> list[str]:
    return [t[:OPENING].lower() for t in texts]
