"""Pages of text blocks into chapters: for PDF and DjVu with a text layer, and for
the pages OCR reads off a scan."""

import re
import string
import unicodedata
from collections import Counter
from itertools import pairwise

from rapidfuzz import fuzz

from ..text import CAPTION, ENDINGS, LISTING, MARK, code_lines, looks_like_code
from .document import Chapter

PUNCT = set(string.punctuation + "«»„“”‘’—–…№•·°§©®™×€₽\xad")
VOWELS = set("aeiouyаеёиоуыэюя")
# a word and the space after it, with the word that follows
PAIR = re.compile(r"(?<![^\W\d_])([^\W\d_]+) (?=([^\W\d_]+)(?![^\W\d_]))")


def top_levels(outline: list[tuple[int, str, int]], depth: int) -> list[tuple[int, str, int]]:
    """The entries of the outline that start chapters."""
    return [t for t in outline if t[0] <= depth]


def has_text_layer(pages: list[list[str]]) -> bool:
    """True when most pages carry text and that text looks like words."""
    if not pages:
        return False
    texts = ["".join(p) for p in pages]
    filled = sum(1 for t in texts if sum(not c.isspace() for c in t) >= 50)
    chars = [c for t in texts for c in t if not c.isspace()]
    if filled < len(pages) / 2 or not chars:
        return False
    bad = sum(1 for c in chars if not _readable(c)) / len(chars)
    words = list(re.findall(r"[^\W\d_]{3,}", " ".join(texts).lower()))
    voiced = sum(1 for w in words if VOWELS & set(w)) / max(len(words), 1)
    return bad < 0.1 and voiced > 0.7


def _readable(c: str) -> bool:
    if c.isdigit() or c in PUNCT:
        return True
    return c.isalpha() and unicodedata.name(c, "").split(" ")[0] in ("LATIN", "CYRILLIC", "GREEK")


def from_pages(pages: list[list[str]], toc: list[tuple[int, str, int]]) -> list[Chapter]:
    pages = strip_running(pages)
    paragraphs: list[tuple[int, str]] = []  # (page index, text)
    for n, blocks in enumerate(pages):
        for block in blocks:
            text = _join_lines(block)
            if not text:
                continue
            # a figure caption can sit in the middle of a sentence: the text goes on past it
            at = len(paragraphs) - 1
            while at > 0 and _aside(paragraphs[at][1]):
                at -= 1
            if paragraphs and not _aside(paragraphs[at][1]) and _continues(paragraphs[at][1], text):
                prev = paragraphs[at][1]
                glue = "" if prev.endswith("-") and text[0].islower() else " "
                paragraphs[at] = (paragraphs[at][0], (prev[:-1] if glue == "" else prev) + glue + text)
            else:
                paragraphs.append((n, text))

    mend = Mend([t for _, t in paragraphs if not t.startswith(LISTING)])
    paragraphs = [(n, t if t.startswith(LISTING) else mend(t)) for n, t in paragraphs]
    if not toc:
        return [{"title": "", "paragraphs": [t for _, t in paragraphs]}] if paragraphs else []

    starts: list[int] = []
    for _, title, page in toc:
        at = _find_heading(paragraphs, title, page)
        starts.append(max(at, starts[-1] if starts else 0))
    starts.append(len(paragraphs))
    chapters: list[Chapter] = []
    for (_, title, _), (a, b) in zip(toc, pairwise(starts), strict=True):
        if b > a:
            body = [t for _, t in paragraphs[a:b]]
            if not _matches(body[0], title):  # heading lost with the running headers: say it anyway
                body.insert(0, mend(spaces(title)))
            chapters.append({"title": mend(spaces(title)), "paragraphs": body})
    return chapters


class Mend:
    """Words the text layer broke with a space ("ЧЕТЫ РЕ ВОЗРАСТА") made whole: two
    pieces never seen in the book on their own, while together they are one of its
    words. Two words of the book stay two."""

    def __init__(self, texts: list[str]) -> None:
        words = Counter(w.lower() for t in texts for w in re.findall(r"[^\W\d_]+", t))
        pairs = Counter((m[1].lower(), m[2].lower()) for t in texts for m in PAIR.finditer(t))
        self.broken = {(a, b) for (a, b), n in pairs.items() if words[a + b] and words[a] == n == words[b]}

    def __call__(self, text: str) -> str:
        return PAIR.sub(lambda m: m[1] if (m[1].lower(), m[2].lower()) in self.broken else m[0], text)


def _aside(text: str) -> bool:
    """A caption or a figure mark: the text around it may be one sentence."""
    return bool(CAPTION.match(text) or MARK.match(text))


def strip_running(pages: list[list[str]]) -> list[list[str]]:
    """Drop running headers, footers and page numbers."""

    def edges(blocks: list[str]) -> set[int]:
        return {0, len(blocks) - 1} if blocks else set()

    seen: dict[str, int] = {}
    for blocks in pages:
        for i in edges(blocks):
            if MARK.match(blocks[i].strip()):
                continue
            key = _squash(blocks[i], digits=False)
            seen[key] = seen.get(key, 0) + 1

    result: list[list[str]] = []
    for blocks in pages:
        drop: set[int] = set()
        for i in edges(blocks):
            text = blocks[i].strip()
            if MARK.match(text):
                continue
            key = _squash(text, digits=False)
            if (
                not key
                or re.match(r"^\d+\s*\|", text)
                or re.search(r"\|\s*\d+$", text)
                or (seen[key] >= 3 and len(text) < 120)
            ):
                drop.add(i)
        result.append([b for i, b in enumerate(blocks) if i not in drop])
    return result


def _join_lines(block: str) -> str:
    """A block's lines into one paragraph; code keeps its lines and is marked a listing."""
    if block.startswith(LISTING):
        return LISTING + code_lines(block.removeprefix(LISTING))
    # a soft hyphen at the end of a line joins the word; at the end of the block it is a
    # hyphen, the word goes on in the next block
    text = re.sub(r"\xad\s*\n\s*", "", re.sub(r"\xad\s*$", "-", block)).replace("\xad", "")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text)
    text = re.sub(r"(\w)-\s*\n\s*(?=[a-zа-яё])", r"\1", text)
    return LISTING + code_lines(text) if looks_like_code(text) else spaces(text)


def _continues(prev: str, text: str) -> bool:
    return (
        not prev.endswith(ENDINGS) and text[0].islower() and LISTING not in (prev[: len(LISTING)], text[: len(LISTING)])
    )


def _matches(text: str, title: str) -> bool:
    """The text opens with the title; a letter or two may differ (OCR)."""
    want, got = _squash(title), _squash(text[:200])
    if len(got) < 4:
        return False
    return got.startswith(want[:40]) or want.startswith(got) or fuzz.ratio(want[:40], got[: len(want[:40])]) >= 85


def _find_heading(paragraphs: list[tuple[int, str]], title: str, page: int) -> int:
    first = None
    for i, (n, text) in enumerate(paragraphs):
        if n < page:
            continue
        if n > page:
            break
        first = i if first is None else first
        if _matches(text, title):
            return i
    if first is not None:
        return first
    return next((i for i, (n, _) in enumerate(paragraphs) if n >= page), len(paragraphs))


def _squash(text: str, digits: bool = True) -> str:
    return "".join(c for c in text.lower() if c.isalpha() or (digits and c.isdigit()))


def spaces(text: str) -> str:
    """One space between words, none inside brackets: "( вместо )" is "(вместо)"."""
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+\)", ")", re.sub(r"\(\s+", "(", text))
