"""The steps of a book, in order. Each step leaves its output and a done mark
in the book's directory, so a restarted run picks up where it stopped."""

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import soundfile as sf

from . import chunk, cover, formats, m4b, reading
from .book import Book
from .config import Settings
from .models import Models

log = logging.getLogger(__name__)


# pieces voiced or heard per round
ROUND = 32


@dataclass
class Context:
    settings: Settings
    models: Models


@dataclass
class Step:
    name: str
    run: Callable[[Book, Context], None]


def extract_text(book: Book, ctx: Context) -> None:
    doc = formats.read(book.source, book.dir, ctx.settings.toc_depth)
    for name, image in (("cover.jpg", doc.cover), ("page1.jpg", doc.first_page)):
        if image:
            book.path(name).write_bytes(image)
    title = doc.title or Path(book.name).stem
    cover.ensure(book.dir)
    data = {"title": title, "author": doc.author, "outline": doc.outline}
    if doc.pages:
        book.write("scan.json", data | {"pages": doc.pages})
    else:
        book.write(
            "text.json", data | {"chapters": doc.chapters, "figures": doc.figures, "keep_source": doc.keep_source}
        )


def split(book: Book, ctx: Context) -> None:
    data = book.read("text.json")
    tts = ctx.models.tts
    pieces = chunk.chunks(data["chapters"], tts.seconds, tts.room)
    if not pieces:
        raise ValueError("no text found in the book")
    book.write("chunks.json", pieces)


def tts(book: Book, ctx: Context) -> None:
    pieces = book.read("chunks.json")
    book.path("tts").mkdir(exist_ok=True)
    while todo := [i for i in range(len(pieces)) if not _wav(book, i).exists()][:ROUND]:
        _synth_all(book, ctx, todo, [pieces[i]["text"] for i in todo])


def assemble(book: Book, ctx: Context) -> None:
    data = book.read("text.json")
    pieces = book.read("chunks.json")
    meta = {"title": data["title"], "artist": data["author"], "album": data["title"], "genre": "Audiobook"}
    picture = book.path("cover.jpg")
    made = m4b.assemble(
        [_final(book, i) for i in range(len(pieces))],
        pieces,
        reading.titles(data, book.name),
        book.path("book.m4b"),
        meta,
        picture if picture.exists() else None,
        book.dir,
    )
    book.write("chapters.json", made)


def _wav(book: Book, i: int) -> Path:
    return book.path(f"tts/{i:05}.wav")


def _ready(book: Book, i: int) -> Path:
    return book.path(f"ready/{i:05}.wav")


def _final(book: Book, i: int) -> Path:
    """The checked take with its pauses set, or the raw one when nothing checks takes."""
    return _ready(book, i) if _ready(book, i).exists() else _wav(book, i)


def _synth_all(
    book: Book,
    ctx: Context,
    numbers: list[int],
    texts: list[str],
    seed: int = 0,
    stop: Callable[[], float] | None = None,
    path: Callable[[int], Path] | None = None,
) -> None:
    """Pieces voiced together; each is on disk as soon as it is ready. `stop()` true
    after a piece (quiet hours have come) ends the round there."""
    path = path or (lambda i: _wav(book, i))
    voiced = ctx.models.tts.synth_many(texts, seed)
    for i, (samples, rate) in zip(numbers, voiced, strict=True):
        tmp = book.path(f"tts/{i:05}.tmp.wav")
        sf.write(tmp, samples, rate, subtype="PCM_16")
        os.replace(tmp, path(i))
        if stop and stop():
            voiced.close()
            return


STEPS = [Step("extract", extract_text), Step("chunk", split), Step("tts", tts), Step("assemble", assemble)]


def run(book: Book, ctx: Context, until: str | None = None) -> Path | None:
    """Runs what is not done yet; returns the m4b once the book is finished."""
    for step in STEPS:
        if not book.done(step.name):
            log.info("%s: %s", book.name, step.name)
            step.run(book, ctx)
            book.mark(step.name)
        if step.name == until:
            return None
    return book.path("book.m4b")
