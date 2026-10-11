"""Title and author of a book. The file's metadata come first, unless they are
empty or an editor's leftover; then the title page and the imprint, read by the
language model; then the name of the file."""

import logging
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pymupdf

from . import formats
from .formats.document import Document
from .llm import Llm, parse_json

log = logging.getLogger(__name__)

# pages of text read at each end of the book, and characters taken from each
PAGES = 3
PAGE_CHARS = 800
# metadata an editor or a scanner leaves instead of the book's own
JUNK = re.compile(
    r"^(microsoft\s+\w+\s+-|untitled|unknown|безымянный|без названия|document\d*$)"
    r"|\.(docx?|rtf|odt|pdf|indd|tex|txt|djvu|fb2)$",
    re.I,
)
# in a file name: a year, a size like 600M or 12 MB, a bracket with either
NOISE = re.compile(
    r"[(\[]\s*[^)\]]*?(?:\d{4}|\d+\s*[kmgкмг]i?[bб]?)\s*[)\]]|\b(?:1[5-9]|20)\d\d\b|\b\d+\s*(?:[kmgкмг]i?[bб]?)\b", re.I
)

PROMPT = """Тебе дают текст первых и последних страниц книги: обложку, титульный лист,
выходные данные. Найди название книги и её автора, как они напечатаны в тексте,
но в обычном регистре: заглавная буква в начале названия и в именах. Автора пиши
как на титуле, например «И. Петров». Серию, издательство, город и год не пиши.
Чего нет в тексте, оставь пустой строкой. Ответь JSON-объектом
{"title": "название", "author": "автор"}."""


def find(doc: Document, source: Path, name: str, llm: Llm | None, wait: Callable[[], None]) -> tuple[str, str]:
    """(title, author) of the book; `wait` is called before the model is asked."""
    title, author = (v if v and not JUNK.search(v.strip()) else "" for v in (doc.title, doc.author))
    if (not title or not author) and llm and not doc.pages:
        text = "\n\n".join(t[:PAGE_CHARS] for t in end_pages(source))
        if text:
            wait()
            found = asked(llm, text)
            title, author = title or found[0], author or found[1]
    named = from_name(name)
    author = author.strip()
    return shown(title) or named[0], (author.title() if author.isupper() else author) or named[1]


def end_pages(source: Path) -> list[str]:
    """Text of the first and the last pages that have any."""
    if (kind := formats.detect(source)) and kind.name == "djvu":
        pages = subprocess.run(["djvutxt", str(source)], capture_output=True, text=True).stdout.split("\f")
    else:
        with pymupdf.open(source) as doc:
            pages = [page.get_text() for page in doc.pages()]
    full = [p.strip() for p in pages if p.strip()]
    return full if len(full) <= 2 * PAGES else full[:PAGES] + full[-PAGES:]


def asked(llm: Llm, text: str) -> tuple[str, str]:
    """(title, author) the model read in the text; what is not in the text is dropped."""
    try:
        reply = parse_json(llm.ask(PROMPT, text, json_reply=True))
    except Exception:
        log.exception("title page: the model did not answer")
        return "", ""
    title, author = (str(reply.get(k) or "").strip() for k in ("title", "author"))
    return (title if printed(title, text) else ""), (author if printed(author, text) else "")


def printed(value: str, text: str) -> bool:
    """Whether the words of `value` are in `text`, whatever its case, spaces and hyphens;
    one word in five may be misread."""
    words = [w for w in re.findall(r"\w+", _plain(value)) if len(w) > 1]
    squashed = re.sub(r"\W+", "", _plain(text))
    missed = sum(w not in squashed for w in words)
    return bool(words) and missed <= len(words) // 5


def from_name(name: str) -> tuple[str, str]:
    """(title, author) of a file named `Автор - Название`, or just the title; underscores,
    years and sizes are left out."""
    stem = Path(name).stem
    cleaned = " ".join(NOISE.sub(" ", stem.replace("_", " ")).split()).strip(" -.,")
    author, _, title = cleaned.partition(" - ") if " - " in cleaned else ("", "", cleaned)
    return shown(title) or stem, author.strip()


def bare(title: str, name: str) -> bool:
    """Whether `title` is nothing but the name of the file, as it is or cleaned, with
    no author to tell it from one."""
    named, author = from_name(name)
    return not author and title.strip() in (Path(name).stem, named)


def shown(title: str) -> str:
    """A title in capitals, wholly or mostly, in the usual case: a capital at the start."""
    words = re.findall(r"[^\W\d_]+", title)
    if sum(w.isupper() for w in words) * 2 <= len(words):
        return title.strip()
    lower = title.strip().lower()
    return lower[:1].upper() + lower[1:]


def _plain(text: str) -> str:
    return text.replace("\xad", "").replace("-\n", "").lower().replace("ё", "е")
