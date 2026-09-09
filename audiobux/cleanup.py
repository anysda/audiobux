"""Text cleanup before reading aloud.

Layout leftovers go by rules: words broken at a line end, lost hyphens in
compound words, figure captions, tables, code, footnotes and links. What the
rules miss goes to a language model, one paragraph at a time; a reply that
changes the paragraph too much is thrown away and the paragraph stays as it was.
A clean text layer sends the model only the paragraphs that still look broken
(`suspicious`); OCR text goes to it whole. A code listing is not read out: it is
kept for the reading copy as the book prints it, and in its place the model tells
in a sentence what it does; without a model the listing is not voiced at all.
"""

import logging
import re
from collections import Counter
from collections.abc import Callable
from functools import cache
from typing import Any, TypedDict

from rapidfuzz.distance import Levenshtein

from .llm import Llm
from .text import CAPTION, ENDINGS, looks_like_code

log = logging.getLogger(__name__)

TABLE = re.compile(r"^(Таблица|Табл\.|Table)\s*\d")
FOOTNOTE = re.compile(r"^\d{1,3} \S")
FOOTNOTE_TAIL = re.compile(r"(?<=[.!?…»)]) \d{1,2} (?=[«\"A-ZА-ЯЁ])")
CITATION = re.compile(r"\b(1[89]|20)\d\d\b|https?://|Retrieved|Извлечено|\(n\.d\.\)")
MARKER = re.compile(r"(?<=[а-яё»)])\d{1,2}(?=[\s.,;:!?)]|$)")
URL = re.compile(r"https?://[^\s)»]+|www\.[^\s)»]+")
URL_ONLY = re.compile(r"\s*\((источник:\s*)?(https?://|www\.)[^)]*\)")
BROKEN = re.compile(r"\b(\w+)- (\w+)")
GLUED = re.compile(r"\b[а-яё]{6,}\b")
COMPOUND = re.compile(r"\b([а-яё]{3,})-[а-яё]{2,}")
INDEX_ENTRY = re.compile(r"[^\W\d_] \d{1,3}(?=[,\s]|$)")
LISTING = "[[code]] "
MIXED = re.compile(r"\b(?=\w*[а-яёА-ЯЁ])(?=\w*[a-zA-Z])\w+\b")
SPACED = re.compile(r"(?:\b\w ){3,}\w\b")
LEFT_BROKEN = re.compile(r"\w- \w|\w -\w")
ODD = re.compile(r"[^\w\s.,;:!?…«»„“”‘’'\"()\[\]{}<>/\\|@#$%^&*+=~`№—–\-•·°§©®™×€₽]")

PROMPT = """Ты корректор книги, которую потом озвучат вслух. Тебе дают один абзац,
извлечённый из PDF или распознанный со скана. Исправь только технические дефекты:
опечатки и ошибки распознавания, слова, разорванные переносом или пробелом,
слипшиеся слова, потерянный дефис в составных словах, мусорные символы, обрывки
колонтитулов, сломанную пунктуацию. Не перефразируй, не меняй слова и их порядок,
не правь стиль, не переводи, не раскрывай сокращения и числа, не трогай латиницу.
Если исправлять нечего, верни абзац без изменений. Ответь только текстом абзаца."""

LISTING_PROMPT = """Ты готовишь техническую книгу к озвучке. Тебе дают листинг кода из книги
и абзац перед ним. Скажи по-русски одним предложением, что делает этот код, начиная
со слов "В листинге", так, чтобы это естественно звучало вслух, без кода, имён в
кавычках и скобок. Если фрагмент пустяковый (одна строка, имя файла, вывод
программы), ответь одним знаком -"""


class Fixed(TypedDict):
    """A chapter's paragraphs to read; at the index of a listing, its telling or "".
    `listings` holds the code itself by that index, kept with its lines."""

    paragraphs: list[str]
    listings: dict[str, str]


class Editor:
    def __init__(self, llm: Llm | None = None, max_change: float = 0.1) -> None:
        self.llm, self.max_change = llm, max_change

    def fix(
        self, chapters: list[list[str]], wait: Callable[[], None] = lambda: None, everything: bool = True
    ) -> list[Fixed]:
        """`everything=False`: only the paragraphs the rules left looking broken go to the model."""
        fixed = by_rules(chapters)
        if self.llm:
            for chapter in fixed:
                wait()
                paragraphs, listings = chapter["paragraphs"], chapter["listings"]
                asks = [
                    i for i, p in enumerate(paragraphs) if str(i) in listings or (p and (everything or suspicious(p)))
                ]
                said = [_as_asked(chapter, i) for i in range(len(paragraphs))]
                replies = self.llm.map(
                    lambda a: self._listing(*a) if a[1].startswith(LISTING) else self._ask(a[1]),
                    [(said[i - 1] if i else "", said[i]) for i in asks],
                )
                for i, reply in zip(asks, replies, strict=True):
                    paragraphs[i] = reply
        return fixed

    def _listing(self, before: str, code: str) -> str:
        assert self.llm is not None
        reply = self.llm.ask(
            LISTING_PROMPT, f"Абзац перед листингом: {before[-600:]}\nЛистинг:\n{code[len(LISTING) :][:3000]}"
        )
        reply = re.sub(r"\s+", " ", re.sub(r"[*_#`]+", " ", reply)).strip()
        if not reply.startswith("В листинге") or len(reply) > 400:
            return ""
        return reply if reply.endswith((".", "!", "?", "…")) else reply + "."

    def _ask(self, text: str) -> str:
        if len(text) < 20 or not re.search("[а-яё]", text):
            return text
        assert self.llm is not None
        reply = self.llm.ask(PROMPT, text)
        if close_enough(text, reply, self.max_change):
            return reply
        log.info("model reply rejected: %r -> %r", text[:80], reply[:80])
        return text


def close_enough(before: str, after: str, limit: float) -> bool:
    return bool(after.strip()) and Levenshtein.distance(before, after) <= limit * len(before)


def suspicious(p: str) -> bool:
    """What the rules could not mend: odd symbols, letters of two alphabets in a word,
    s p a c e d letters, a word still broken by a hyphen, an unknown Russian word."""
    if ODD.search(p) or MIXED.search(p) or SPACED.search(p) or LEFT_BROKEN.search(p):
        return True
    return any(not _known(w) for w in re.findall(r"(?<![\w-])[а-яё]{4,}(?![\w-])", p))


def by_rules(chapters: list[list[str]]) -> list[Fixed]:
    """Layout leftovers dropped and words mended; a listing is kept whole, not to be read."""
    heads = _compound_heads(chapters)
    out: list[Fixed] = []
    for paragraphs in chapters:
        kept = [
            p if p.startswith(LISTING) else _fix_paragraph(p, heads) for p in _join_continued(_drop_layout(paragraphs))
        ]
        kept = [p for p in kept if p]
        out.append(
            {
                "paragraphs": ["" if p.startswith(LISTING) else p for p in kept],
                "listings": {str(i): p[len(LISTING) :] for i, p in enumerate(kept) if p.startswith(LISTING)},
            }
        )
    return out


def _as_asked(chapter: Fixed, i: int) -> str:
    """Paragraph `i` as the model is given it: a listing flat, one line per block of it."""
    code = chapter["listings"].get(str(i))
    if code is None:
        return chapter["paragraphs"][i]
    return LISTING + "\n".join(" ".join(block.split()) for block in code.split("\n\n"))


def _drop_layout(paragraphs: list[str]) -> list[str]:
    """Tables, captions, footnotes and the subject index go; the blocks of a listing
    become one `[[code]]` paragraph."""
    kept: list[str] = []
    in_table = in_code = False
    for block in paragraphs:
        p = " ".join(block.split())
        if TABLE.match(p):
            in_table = True
            continue
        if in_table and not _prose(p):
            continue
        in_table = False
        # a line without a Russian letter right after code (a closing brace) is code too
        code = looks_like_code(p) or (in_code and not re.search(r"[а-яёА-ЯЁ]", p))
        in_code = code
        if code:
            if kept and kept[-1].startswith(LISTING):
                kept[-1] += "\n\n" + block.strip()
            else:
                kept.append(LISTING + block.strip())
            continue
        if CAPTION.match(p) or FOOTNOTE.match(p) or _index(p):
            continue
        kept.append(p)
    return kept


def _join_continued(paragraphs: list[str]) -> list[str]:
    """A paragraph cut by a page break, a footnote or a figure goes back together."""
    out: list[str] = []
    for p in paragraphs:
        if out and p[:1].islower() and not out[-1].endswith(ENDINGS) and not out[-1].startswith(LISTING):
            out[-1] += " " + p
        else:
            out.append(p)
    return out


def _fix_paragraph(p: str, heads: set[str]) -> str:
    m = FOOTNOTE_TAIL.search(p)
    if m and CITATION.search(p, m.end()):
        p = p[: m.start()]
    p = URL_ONLY.sub("", p)
    p = URL.sub(lambda m: _host(m.group()), p)
    p = MARKER.sub("", p)
    p = BROKEN.sub(_mend, p)
    p = GLUED.sub(lambda m: _split_compound(m.group(), heads), p)
    return re.sub(r"\s+", " ", p).strip()


def _mend(m: re.Match[str]) -> str:
    """`Domain- Driven`, `изменчи- вость`, `бизнес- процесс`: a word broken at a line end."""
    left: str = m.group(1)
    right: str = m.group(2)
    if not re.search("[а-яёА-ЯЁ]", left + right):
        return f"{left}-{right}"
    whole = left + (right.lower() if not left.isupper() else right)
    if _known(whole):
        return whole
    if _known(left) and _known(right):
        return f"{left}-{right}"
    return whole


def _split_compound(word: str, heads: set[str]) -> str:
    """`бизнесценности` -> `бизнес-ценности` when the book writes other `бизнес-` words."""
    if _known(word):
        return word
    for head in sorted(heads, key=len, reverse=True):
        rest = word[len(head) :]
        if word.startswith(head) and len(rest) >= 3 and _known(rest):
            return f"{head}-{rest}"
    return word


def _compound_heads(chapters: list[list[str]]) -> set[str]:
    counts = Counter(m.group(1) for ps in chapters for p in ps for m in COMPOUND.finditer(p.lower()))
    return {h for h, n in counts.items() if n >= 2 and _known(h)}


def _prose(p: str) -> bool:
    return p.rstrip().endswith(ENDINGS) and len(p.split()) >= 5


def _index(p: str) -> bool:
    """A page of the subject index: `термин 42, 57 ◊ другой термин 39`."""
    words = p.split()
    return len(INDEX_ENTRY.findall(p)) >= 5 and len(INDEX_ENTRY.findall(p)) >= 0.15 * len(words)


def _host(url: str) -> str:
    """Only the site name is worth reading out: `https://www.site.com/a/b.pdf.` -> `site.com.`"""
    tail = url[len(url.rstrip(".,;:")) :]
    return re.sub(r"^(https?://)?(www\.)?", "", url[: len(url) - len(tail)]).split("/")[0] + tail


@cache
def _known(word: str) -> bool:
    known: bool = _morph().word_is_known(word.lower())
    return known


@cache
def _morph() -> Any:
    import pymorphy3

    return pymorphy3.MorphAnalyzer()
