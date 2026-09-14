"""The reading text timed against the m4b: `sync.json`, for a player that shows the
sentence being read.

Every block of `reading` is timed by the paragraph that voices it, and a paragraph
by the pieces it was cut into. A piece knows where it starts in the m4b and, when
its take was checked, where each of its words is (the forced alignment of the take
as it went into the book). The sentences the reader sees are matched to those words
word by word: the reader's sentence reduced to words as they are said (numbers and
Latin as the voice reads them), then aligned with the said words by edit distance.
A word the aligner was not sure of, or a piece nobody checked, gets its time shared
out by letters between the words around it.

Times are seconds from the start of the m4b, never back and never past its end.
A block nobody hears (a listing not told, a heading the text does not repeat) has
no time. Figures go out as JPEG files next to `sync.json`.
"""

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pymupdf
import soundfile as sf
from rapidfuzz.distance import Levenshtein

from . import reading, speech, takes
from .book import Book

log = logging.getLogger(__name__)

VERSION = 1
# the end of a sentence: its mark, closing quotes, then a space and a capital, a digit, a quote or a dash
END = re.compile(r"[.!?…]+[»\"”’)]*(?=\s+[«\"„“(—–\-A-ZА-ЯЁ0-9])")
INITIAL = re.compile(r"(?:^|\s)[A-ZА-ЯЁ]$")

_SILENT: tuple[float | None, float | None] = (None, None)  # a block nobody hears
Words = list[tuple[str, float, float]]  # said words with their start and end in the m4b


def write(book: Book, wavs: list[Path]) -> None:
    """`sync.json` and its figures in the book's folder; `wavs[i]` is piece i as it went into the m4b."""
    data = book.read("fixed.json")
    found = book.read("figures.json") if book.path("figures.json").exists() else []
    pieces = book.read("chunks.json")
    made = book.read("chapters.json")
    aligned = [book.read(f"ready/{i:05}.json").get("words") if _checked(book, i) else None for i in range(len(pieces))]
    said = said_words(pieces, made["pieces"], aligned, [sf.info(w).duration for w in wavs])
    starts: dict[int, float] = {}
    for piece, start in zip(pieces, made["pieces"], strict=True):
        starts.setdefault(piece["chapter"], start)
    chapters = reading.chapters(data, found, book.dir, book.name)
    latin = speech.read_dictionary(book.path("latin.txt"))
    book.write("sync.json", timed(chapters, said, starts, made["duration"], latin, lambda f: _picture(book, f)))


def _checked(book: Book, i: int) -> bool:
    return book.path(f"ready/{i:05}.json").exists()


def said_words(
    pieces: list[dict[str, Any]],
    starts: list[float],
    aligned: list[list[list[float] | None] | None],
    lengths: list[float],
) -> dict[tuple[int, int], Words]:
    """{(chapter, paragraph): the words said for it}. `aligned[i]` is [start, end] or None
    for each word of piece i, or None when the piece was not aligned; `lengths[i]` is its
    length in seconds."""
    out: dict[tuple[int, int], Words] = {}
    for piece, start, times, length in zip(pieces, starts, aligned, lengths, strict=True):
        words, _ = takes.piece_words(piece["text"], piece.get("bounds", []))
        spans = word_times(words, times, length)
        out.setdefault((piece["chapter"], piece["paragraph"]), []).extend(
            (w, start + a, start + b) for w, (a, b) in zip(words, spans, strict=True)
        )
    return out


def word_times(words: list[str], aligned: list[list[float] | None] | None, length: float) -> list[tuple[float, float]]:
    """The time of each word in its piece. A known time is kept; a run of words with
    none shares the gap around it by letters, the whole piece when nothing is known."""
    known: list[list[float] | None] = aligned if aligned and len(aligned) == len(words) else [None] * len(words)
    out: list[tuple[float, float]] = []
    j = 0
    while j < len(words):
        if (here := known[j]) is not None:
            out.append((float(here[0]), float(here[1])))
            j += 1
            continue
        k = j
        while k < len(words) and known[k] is None:
            k += 1
        after = known[k] if k < len(words) else None
        lo = out[-1][1] if out else 0.0
        hi = max(float(after[0]) if after is not None else length, lo)
        sizes = [len(w) for w in words[j:k]]
        at = lo
        for size in sizes:
            out.append((at, at + (hi - lo) * size / sum(sizes)))
            at = out[-1][1]
        j = k
    return out


def timed(
    chapters: list[reading.Chapter],
    said: dict[tuple[int, int], Words],
    starts: dict[int, float],
    duration: float,
    latin: dict[str, str],
    picture: Callable[[dict[str, Any]], str | None] = lambda f: None,
) -> dict[str, Any]:
    """The sync of `chapters`. `said[(chapter, paragraph)]` are the words the voice said
    for a paragraph, `starts[chapter]` is where its first piece starts, `picture(figure)`
    is the src of a figure."""
    clock = Clock(duration)
    out: list[dict[str, Any]] = []
    end = duration
    spans = []
    for ch in reversed(range(len(chapters))):
        # a chapter ends where the next heard one starts; one not heard is a point there
        begin = min(starts.get(ch, end), end)
        spans.append((round(begin, 3), round(end, 3)))
        end = begin
    for ch, (chapter, (c0, c1)) in enumerate(zip(chapters, reversed(spans), strict=True)):
        blocks = []
        for block in chapter.blocks:
            words = said.get((ch, block.said), []) if block.said is not None else []
            if block.kind == "p":
                parts, times = _sentences(block.text, words, latin, clock)
                blocks.append(
                    {
                        "type": "p",
                        "sentences": [{"text": t, "t0": a, "t1": b} for t, (a, b) in zip(parts, times, strict=True)],
                    }
                )
                continue
            t0, t1 = clock.span(words[0][1], words[-1][2]) if words else _SILENT
            item: dict[str, Any]
            if block.kind == "figure":
                assert block.figure is not None
                item = {"type": "figure", "src": picture(block.figure), "alt": block.text}
            else:
                item = {"type": block.kind, "text": block.text}
            blocks.append(item | {"t0": t0, "t1": t1})
        out.append({"title": chapter.title, "t0": c0, "t1": c1, "blocks": blocks})
    return {"version": VERSION, "duration": round(duration, 3), "chapters": out}


class Clock:
    """Times in order: a span never starts before the last one ended nor passes the end."""

    def __init__(self, duration: float) -> None:
        self.duration, self.now = duration, 0.0

    def span(self, t0: float, t1: float) -> tuple[float, float]:
        t0 = min(max(t0, self.now), self.duration)
        t1 = min(max(t1, t0), self.duration)
        self.now = t1
        return round(t0, 3), round(t1, 3)


def sentences(text: str) -> list[str]:
    """The sentences of a paragraph as the reader sees them; `В. Хононов` is not cut."""
    out, start = [], 0
    for m in END.finditer(text):
        if INITIAL.search(text, start, m.start()):
            continue
        out.append(text[start : m.end()].strip())
        start = m.end()
    if text[start:].strip():
        out.append(text[start:].strip())
    return out


def _sentences(
    text: str, words: list[tuple[str, float, float]], latin: dict[str, str], clock: Clock
) -> tuple[list[str], list[tuple[float | None, float | None]]]:
    """Each sentence of the paragraph with the time of its words among `words`."""
    parts = sentences(text)
    if not words:
        return parts, [_SILENT] * len(parts)
    mine: list[int] = []  # the sentence of each word the reader sees
    seen: list[str] = []
    for n, part in enumerate(parts):
        found = takes.words(speech.plain(part, latin))
        seen += found
        mine += [n] * len(found)
    hit: list[list[int]] = [[] for _ in parts]  # said words of each sentence
    for op in Levenshtein.opcodes(seen, [w for w, _, _ in words]):
        if op.tag in ("equal", "replace"):
            a0, a1, b0, b1 = op.src_start, op.src_end, op.dest_start, op.dest_end
            for k in range(a0, a1):
                hit[mine[k]].append(b0 + (k - a0) * (b1 - b0) // (a1 - a0))
    # a said word no sentence claims (an extra word of the voice) goes to the sentence before it
    sentence = [-1] * len(words)
    for n, got in enumerate(hit):
        for j in got:
            sentence[j] = n
    for j in range(len(words)):
        if sentence[j] < 0:
            sentence[j] = sentence[j - 1] if j else 0
            hit[sentence[j]].append(j)
    times: list[tuple[float | None, float | None]] = []
    for got in hit:
        if got:
            times.append(clock.span(words[min(got)][1], words[max(got)][2]))
        else:  # nothing of it said as such: a point where the voice is
            times.append(clock.span(clock.now, clock.now))
    return parts, times


def _picture(book: Book, figure: dict[str, Any]) -> str | None:
    """The figure as `figures/<name>.jpg` in the book's folder, the name in what the
    web serves (`[0-9a-z_-]{1,64}.jpg`) whatever format named the image."""
    name = f"figures/{re.sub(r'[^0-9a-z_-]', '_', Path(figure['image']).stem.lower())[:64] or 'figure'}.jpg"
    try:
        pix = pymupdf.Pixmap(str(book.path(figure["image"])))
        if pix.alpha:
            pix = pymupdf.Pixmap(pix, 0)
        if pix.n > 3:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        pix.save(book.path(name), jpg_quality=85)
    except Exception as e:
        log.warning("%s: figure %s left out: %s", book.name, figure["image"], e)
        return None
    return name
