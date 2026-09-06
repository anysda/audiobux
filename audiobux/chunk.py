"""Chapters into pieces the speech model voices in one pass.

A long pass sounds wet and metallic: on the same passage, pieces of 5-8 s scored
UTMOS 3.52 and pieces of 14 s and more 3.01. So a piece holds whole sentences
while they fit `room` seconds of speech (about 10 s for real). A longer sentence
is cut where a reader pauses: at a syntagm boundary the language model marked
with `|` (a comma among them counts a little nearer), then at plain punctuation,
and only a sentence with neither is cut at its middle word.

Every piece carries the boundaries inside it, [token index, kind], for setting its
pauses, and the pause after it, all from the speaker's own reading.
"""

import re
from collections.abc import Callable
from typing import Any

from .takes import PAUSE

Seconds = Callable[[str], float]

PAUSE_CHAPTER = PAUSE["chapter"]
STOP = re.compile(r"[.!?…][»\"”)]*$")
COMMA = re.compile(r"[,;:—–][»\"”)]*$")
DASH = ("—", "–")
HEADING_WORDS = 12


def chunks(chapters: list[dict[str, Any]], seconds: Seconds, room: float) -> list[dict[str, Any]]:
    """[{chapter, paragraph, text, bounds, pause}]; `seconds(text)` is how long the voice
    makes a text. An empty paragraph (a listing not told) is not voiced; the heading is
    the first paragraph voiced."""
    result: list[dict[str, Any]] = []
    for ch, chapter in enumerate(chapters):
        first = len(result)
        for n, paragraph in enumerate(chapter["paragraphs"]):
            heading = len(result) == first and len(paragraph.split()) <= HEADING_WORDS
            pieces = split(paragraph, seconds, room)
            for i, (text, bounds, after) in enumerate(pieces):
                if i == len(pieces) - 1:
                    after = "head" if heading else "para"
                result.append({"chapter": ch, "paragraph": n, "text": text, "bounds": bounds, "pause": PAUSE[after]})
        if result and result[-1]["chapter"] == ch:
            result[-1]["pause"] = PAUSE_CHAPTER
    return result


def split(paragraph: str, seconds: Seconds, room: float) -> list[tuple[str, list[list[Any]], str]]:
    """[(text, [[token index, kind]], kind of the boundary after it)] of one paragraph."""
    tokens: list[str] = []
    marked: set[int] = set()
    for t in paragraph.replace("|", " | ").split():
        if t == "|":
            if tokens:
                marked.add(len(tokens) - 1)
        else:
            tokens.append(t)
    if not tokens:
        return []
    kinds = [_kind(t, i in marked) for i, t in enumerate(tokens)]

    def fits(ix: list[int]) -> bool:
        return seconds(" ".join(tokens[i] for i in ix)) <= room

    def cut(ix: list[int]) -> list[list[int]]:
        if fits(ix) or len(ix) < 2:
            return [ix]
        inner = range(len(ix) - 1)
        at = (
            [k for k in inner if kinds[ix[k]] in ("comma", "syn")]
            or [k for k in inner if COMMA.search(tokens[ix[k]]) or tokens[ix[k + 1]] in DASH]
            or list(inner)
        )
        k = min(at, key=lambda k: abs(k + 1 - len(ix) / 2) * (0.7 if kinds[ix[k]] == "comma" else 1))
        if kinds[ix[k]] is None:
            kinds[ix[k]] = "comma" if COMMA.search(tokens[ix[k]]) or tokens[ix[k + 1]] in DASH else "syn"
        return cut(ix[: k + 1]) + cut(ix[k + 1 :])

    sentences: list[list[int]] = []
    cur: list[int] = []
    for i in range(len(tokens)):
        cur.append(i)
        if kinds[i] == "stop" or i == len(tokens) - 1:
            sentences.append(cur)
            cur = []
    pieces: list[list[int]] = []
    for sentence in sentences:
        for part in cut(sentence):
            if pieces and kinds[pieces[-1][-1]] == "stop" and fits(pieces[-1] + part):
                pieces[-1] = pieces[-1] + part
            else:
                pieces.append(part)
    out = []
    for ix in pieces:
        bounds = [[j, kinds[i]] for j, i in enumerate(ix[:-1]) if kinds[i]]
        out.append((" ".join(tokens[i] for i in ix), bounds, kinds[ix[-1]] or "syn"))
    return out


def _kind(token: str, marked: bool) -> str | None:
    if STOP.search(token):
        return "stop"
    if marked:
        return "comma" if COMMA.search(token) else "syn"
    return None
