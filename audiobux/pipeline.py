"""The steps of a book, in order. Each step leaves its output and a done mark
in the book's directory, so a restarted run picks up where it stopped."""

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from rapidfuzz.distance import Levenshtein

from . import chunk, cover, epub, figures, formats, m4b, reading, speech, sync, takes
from .book import Book
from .config import Settings
from .formats.pages import from_pages, top_levels
from .models import Models
from .takes import Heard

log = logging.getLogger(__name__)

FIGURE_LINE = re.compile(r"^\[Рисунок:\s*(.*?)\]$", re.S)
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
    cover.ensure(book.dir, doc.author, title, book.name)
    data = {"title": title, "author": doc.author, "outline": doc.outline}
    if doc.pages:
        book.write("scan.json", data | {"pages": doc.pages})
    else:
        book.write(
            "text.json", data | {"chapters": doc.chapters, "figures": doc.figures, "keep_source": doc.keep_source}
        )


def ocr(book: Book, ctx: Context) -> None:
    if not book.path("scan.json").exists():
        return
    if ctx.models.ocr is None:
        raise NotImplementedError("the book is a scan and no OCR model is set")
    scan = book.read("scan.json")
    pages, found = [], []
    for n, name in enumerate(scan["pages"]):
        text = ctx.models.ocr.page_text(book.path(name))
        page = blocks(text)
        pages.append([described(b) for b in page])
        # the picture behind each description, for the EPUB
        found += figures.cut_scan(
            book.path(name), n, [described(b) for b in page if FIGURE_LINE.match(b.strip())], book.dir
        )
    toc = top_levels([tuple(t) for t in scan["outline"]], ctx.settings.toc_depth) or headings(pages)
    outline = scan.get("outline") or headings(pages)
    pages = [[b.lstrip("#").strip() for b in blocks] for blocks in pages]
    chapters = from_pages(pages, toc)
    if found:
        book.write("figures.json", found)
    book.write(
        "text.json", {"title": scan["title"], "author": scan["author"], "chapters": chapters, "outline": outline}
    )


def blocks(text: str) -> list[str]:
    """Paragraphs of an OCR reply; a `#` heading is a paragraph of its own."""
    out, lines = [], []
    for line in text.splitlines():
        if line.strip() and not line.startswith("#"):
            lines.append(line)
            continue
        if lines:
            out.append("\n".join(lines))
            lines = []
        if line.strip():
            out.append(line.strip())
    if lines:
        out.append("\n".join(lines))
    return out


def described(block: str) -> str:
    """`[Рисунок: На рисунке ...]` of the OCR reply: the description is read as it is."""
    m = FIGURE_LINE.match(block.strip())
    return m.group(1).strip() if m else block


def headings(pages: list[list[str]]) -> list[tuple[int, str, int]]:
    """A table of contents from the headings the OCR model marked with `#`."""
    return [
        (1, b.lstrip("#").strip(), n)
        for n, blocks in enumerate(pages)
        for b in blocks
        if b.startswith("#") and b.lstrip("#").strip()
    ]


def describe(book: Book, ctx: Context) -> None:
    """Figure marks of the text become descriptions told by the vision model."""
    data = book.read("text.json")
    found = data.pop("figures", [])
    told = []
    if found and ctx.models.figures:
        figures.near(data["chapters"], found)
        try:
            told = ctx.models.figures.describe(found, book.dir)
        finally:
            ctx.models.release_llm()
        book.write("figures.json", [f | {"said": t} for f, t in zip(found, told, strict=True)])
    data["chapters"] = figures.put(data["chapters"], told)
    book.write("described.json", data)


def fix(book: Book, ctx: Context) -> None:
    data = book.read("described.json")
    try:
        fixed = ctx.models.editor.fix(
            [c["paragraphs"] for c in data["chapters"]], everything=book.path("scan.json").exists()
        )
    finally:
        ctx.models.release_llm()
    for chapter, done in zip(data["chapters"], fixed, strict=True):
        chapter |= done
    book.write("fixed.json", data)


def make_epub(book: Book, ctx: Context) -> None:
    """The reading text as an EPUB, to go out with the m4b. The audiobook matters
    more: a book whose EPUB fails is voiced all the same."""
    try:
        epub.build(book)
    except Exception:
        log.exception("%s: no EPUB", book.name)


def normalize(book: Book, ctx: Context) -> None:
    """The text as it is said. A paragraph keeps its index, so a piece knows its place
    in the reading text: one with nothing to say (a listing not told) stays empty."""
    data = book.read("fixed.json")
    said = [[p for p in c["paragraphs"] if p] for c in data["chapters"]]
    try:
        ready = ctx.models.speech.prepare(said, book.path("latin.txt"))
        ready = ctx.models.speech.mark(ready)
    finally:
        ctx.models.release_llm()
    for chapter, paragraphs in zip(data["chapters"], ready, strict=True):
        told = iter(paragraphs)
        chapter["paragraphs"] = [next(told) if p else "" for p in chapter["paragraphs"]]
    book.write("normalized.json", data)


def split(book: Book, ctx: Context) -> None:
    data = book.read("normalized.json")
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


def verify(book: Book, ctx: Context) -> None:
    """Every piece is heard and its words found letter by letter. A take with a stutter,
    a word broken by a silence or too many wrong words is voiced again with another
    seed, up to `verify_attempts` takes; the best one gets its pauses set by the text
    and goes into ready/. A piece still wrong after that is logged and written into
    verify.json, and the book goes on."""
    if not ctx.models.listener and not ctx.models.aligner:
        return
    latin = speech.read_dictionary(book.path("latin.txt"))
    pieces = book.read("chunks.json")
    book.path("ready").mkdir(exist_ok=True)
    for at in range(0, len(pieces), ROUND):
        pending = [i for i in range(at, min(at + ROUND, len(pieces))) if not _ready(book, i).exists()]
        best: dict[int, dict[str, Any]] = {}
        for seed in range(ctx.settings.verify_attempts):
            if seed:
                todo = [i for i in pending if not _take(book, i, seed).exists()]
                _synth_all(
                    book, ctx, todo, [pieces[i]["text"] for i in todo], seed, path=partial(_take, book, seed=seed)
                )
            heard = (
                ctx.models.listener.listen_many([_take(book, i, seed) for i in pending])
                if ctx.models.listener
                else [None] * len(pending)
            )
            for i, h in zip(pending, heard, strict=True):
                take = _examine(book, ctx, pieces[i], _take(book, i, seed), h, latin) | {"seed": seed}
                if i not in best or take["score"] < best[i]["score"]:
                    best[i] = take
                if take["bad"]:
                    log.warning("%s: piece %d take %d: %s", book.name, i, seed + 1, "; ".join(take["why"]))
            pending = [i for i in pending if best[i]["bad"]]
            if not pending:
                break
        for i, take in sorted(best.items()):
            wave = take.pop("wave")
            tmp = book.path(f"ready/{i:05}.tmp.wav")
            sf.write(tmp, wave, take.pop("rate"), subtype="PCM_16")
            book.write(f"ready/{i:05}.json", take)
            os.replace(tmp, _ready(book, i))
            if take["bad"]:
                log.warning("%s: piece %d left as it is after %d takes", book.name, i, ctx.settings.verify_attempts)
    report = []
    for i in range(len(pieces)):
        take = book.read(f"ready/{i:05}.json") if book.path(f"ready/{i:05}.json").exists() else {}
        if take.get("bad"):
            report.append({"piece": i, "text": pieces[i]["text"]} | take)
    book.write("verify.json", report)


def _examine(
    book: Book, ctx: Context, piece: dict[str, Any], wav: Path, heard: Heard | None, latin: dict[str, str]
) -> dict[str, Any]:
    """A take checked and with its pauses set: score 0 when nothing is wrong."""
    wave, rate = sf.read(wav, dtype="float32")
    text_words, bounds = takes.piece_words(piece["text"], piece.get("bounds", []))
    why: list[str] = []
    error, stutter = 0.0, 0
    broken: list[tuple[str, float, float]] = []
    info: dict[str, int] = {}
    times: list[list[float] | None] = []
    if heard is not None:
        error = cer(piece["text"], heard.text, latin)
        if error > ctx.settings.verify_cer:
            why.append(f"CER {error:.2f}: {heard.text[:80]!r}")
        loud = takes.loudness(wave, rate)
        stutter, found = takes.stutters(text_words, heard.words, loud >= np.percentile(loud, 95) - 35)
        why += found
    if ctx.models.aligner and text_words:
        wave, info = takes.retime(wave, rate, ctx.models.aligner.align(wave, rate, text_words), bounds)
        aligned = ctx.models.aligner.align(wave, rate, text_words)
        broken = takes.broken(wave, rate, aligned)
        # where each word is in the take as it goes into the book, for the reading text
        times = [[round(w.start, 3), round(w.end, 3)] if w.sure > 0 else None for w in aligned]
        why += [f"разрыв «{w}» {d * 1000:.0f} мс на {t} с" for w, t, d in broken]
    bad = bool(stutter or broken or error > ctx.settings.verify_cer)
    score = 10 * (stutter + len(broken)) + (error > ctx.settings.verify_cer) + error
    return {
        "wave": wave,
        "rate": rate,
        "bad": bad,
        "score": round(score, 3),
        "cer": round(error, 3),
        "stutter": stutter,
        "broken": broken,
        "why": why,
        "heard": heard.text if heard is not None else "",
        "words": times,
    } | info


def cer(text: str, heard: str, latin: dict[str, str] | None = None) -> float:
    """Character error rate of what was heard against the text, words only."""
    expected = speech.plain(text, latin)
    return Levenshtein.distance(expected, speech.plain(heard, latin)) / max(len(expected), 1)


def assemble(book: Book, ctx: Context) -> None:
    data = book.read("normalized.json")
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


def make_sync(book: Book, ctx: Context) -> None:
    """`sync.json`: the reading text timed against the m4b."""
    sync.write(book, [_final(book, i) for i in range(len(book.read("chunks.json")))])


def _wav(book: Book, i: int) -> Path:
    return book.path(f"tts/{i:05}.wav")


def _take(book: Book, i: int, seed: int) -> Path:
    return _wav(book, i) if seed == 0 else book.path(f"tts/{i:05}.s{seed}.wav")


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


STEPS = [
    Step("extract", extract_text),
    Step("ocr", ocr),
    Step("figures", describe),
    Step("fix", fix),
    Step("epub", make_epub),
    Step("normalize", normalize),
    Step("chunk", split),
    Step("tts", tts),
    Step("verify", verify),
    Step("assemble", assemble),
    Step("sync", make_sync),
]


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
