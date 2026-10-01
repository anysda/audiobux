"""The outline of a PDF without bookmarks: its printed contents matched to the pages
of the text, or else the headings that open pages, told by their type. And the pages
that are not the book's text: the contents, the title and the imprint."""

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

from rapidfuzz import fuzz, process

Row = tuple[str, float]  # a printed line of a page and the size of its type
Entry = tuple[int, str, int]  # (level, title, page index)

CONTENTS = {"содержание", "оглавление", "contents", "tableofcontents"}
IMPRINT = re.compile(
    r"ISBN|ББК|УДК|[Тт]ираж|\bScan\b|Подписано в печать|Все права защищены|All rights reserved|Copyright|©"
    r"|\d{10}\s*[-—–]\s*\d{3}"
)
NUMBERED = re.compile(r"^(.*?\S)(?:\s*[.…·_]{2,}\s*|\s+)(\d{1,4})$")
LABEL = re.compile(r"^(глава|часть|раздел|chapter|part)\s+\S+$", re.IGNORECASE)
ROMAN = re.compile(r"^[IVXLC]+$")
MATCH = 85  # how close a line of the text must be to a title of the contents (OCR mixes letters up)
LARGE = 1.25  # type this much larger than the body is a heading
SHORT = 30  # words: a page with fewer before the text starts is a title page
IMPRINT_WORDS = 250


def listing_pages(rows: list[list[Row]]) -> list[int]:
    """The pages of the printed contents: one headed «Содержание» and those after it
    that read as a listing too."""
    for n, page in enumerate(rows):
        if any(_key(t) in CONTENTS for t, _ in page[:3]) and _listing(page):
            found = [n]
            while found[-1] + 1 < len(rows) and _listing(rows[found[-1] + 1]):
                found.append(found[-1] + 1)
            return found
    return []


def not_text(rows: list[list[Row]], listing: list[int]) -> set[int]:
    """Pages to leave out of the text: the contents and the imprint (publisher, ISBN,
    print run, the scanner's mark)."""
    imprint = {n for n, page in enumerate(rows) if _words(page) < IMPRINT_WORDS and IMPRINT.search(_text(page))}
    return set(listing) | imprint


def front(rows: list[list[Row]], skip: set[int]) -> set[int]:
    """The short pages before the text starts: the title and its back. A page of one
    line is a part's heading."""
    out: set[int] = set()
    for n, page in enumerate(rows):
        if n in skip or len(page) == 1:
            continue
        if _words(page) >= SHORT:
            break
        out.add(n)
    return out


def printed(rows: list[list[Row]], listing: list[int], skip: set[int], cases: "Cases") -> list[Entry]:
    """The entries of the printed contents at the pages they open. The page numbers of
    the contents are shifted to page indices by where the titles are found; of the two
    spellings of a title, the one with more words of the book's text is kept."""
    entries = _entries(rows[n] for n in listing)
    if len(entries) < 2:
        return []
    levels = _levels([size for _, size, _ in entries])
    windows = [(n, text) for n, page in enumerate(rows) if n not in skip for text in _windows(page)]
    keys = [_key(text) for _, text in windows]
    hits: list[list[tuple[int, float, str]]] = []  # (page, score, text) by page
    for title, _, _ in entries:
        want = _key(title)
        found = process.extract(want, keys, scorer=fuzz.ratio, score_cutoff=MATCH, limit=None) if len(want) > 3 else []
        best: dict[int, tuple[int, float, str]] = {}
        for _, score, i in found:
            n, text = windows[i]
            if n not in best or (score, -len(text)) > (best[n][1], -len(best[n][2])):
                best[n] = (n, score, text)
        hits.append(sorted(best.values()))
    shifts = Counter(s for (_, _, num), h in zip(entries, hits, strict=True) if num for s in {h[0] - num for h in h})
    shift = shifts.most_common(1)[0][0] if shifts else None
    wanted = [num + shift if num is not None and shift is not None else None for _, _, num in entries]
    out: list[Entry] = []
    for i, (title, _, _) in enumerate(entries):
        low = out[-1][2] if out else 0
        if (expect := wanted[i]) is not None:
            near = [h for h in hits[i] if abs(h[0] - expect) <= 1 and h[0] >= low]
            hit = min(near, key=lambda h: (abs(h[0] - expect), -h[1])) if near else None
            page = hit[0] if hit else expect
        else:
            high = next((w for w in wanted[i + 1 :] if w is not None), len(rows))
            hit = next((h for h in hits[i] if low <= h[0] <= high), None)
            if hit is None:
                continue
            page = hit[0]
        if hit and cases.known(hit[2]) >= cases.known(title):
            title = hit[2]
        if low <= page < len(rows):
            out.append((levels[i], title, page))
    return out


def headings(rows: list[list[Row]], skip: set[int]) -> list[Entry]:
    """Headings that open pages: lines in large type, or in capitals of any size; the
    larger the type, the higher the level. A size that heads sections inside pages too
    does not start chapters."""
    body = _body_size(rows)
    running = Counter(_key(t) for page in rows for t, _ in page[:1] + page[-1:])
    found: list[tuple[float, str, int]] = []
    inside: set[int] = set()
    for n, page in enumerate(rows):
        if n in skip:
            continue
        top = page[:]
        while top and (_page_number(top[0][0]) or running[_key(top[0][0])] >= 3 or len(_key(top[0][0])) < 3):
            top.pop(0)
        head: list[Row] = []
        for text, size in top[:3]:
            if size < body * LARGE and not _caps(text):
                break
            head.append((text, size))
        size = max((s for _, s in head), default=0)
        if head and (len(head) < len(top) or size >= body * LARGE) and sum(len(t) for t, _ in head) <= 150:
            found.append((size, _join(head), n))
        inside |= {round(s) for t, s in top[len(head) :] if s >= body * LARGE and len(t) <= 80}
    found = [f for f in found if f[0] < body * LARGE or round(f[0]) not in inside]
    levels = _levels([size for size, _, _ in found])
    return [(level, title, n) for level, (_, title, n) in zip(levels, found, strict=True)]


class Cases:
    """How the running text writes its words, to set a title printed in capitals in
    the usual case: names and abbreviations keep their capitals."""

    def __init__(self, texts: Iterable[str]) -> None:
        self.seen: dict[str, Counter[str]] = {}
        for text in texts:
            if not any(c.islower() for c in text):
                continue
            for m in re.finditer(r"[^\W\d_]+", text):
                before = text[: m.start()].rstrip()[-1:]
                if before in ("", ".", "!", "?", "…", "«", '"', "—", "-"):
                    continue  # the start of a sentence is capital anyway
                word = m[0]
                case = "upper" if len(word) > 1 and word.isupper() else "title" if word[0].isupper() else "lower"
                for k in {word.lower(), _stem(word)}:
                    self.seen.setdefault(k, Counter())[case] += 1

    def shown(self, title: str) -> str:
        """The title as shown: no dots trailing it, words in capitals in their usual case,
        the first word capital."""
        title = re.sub(r"[\s.…]+(?=[»\"”]?$)", "", title.strip())
        title = re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", title))
        capitals = not any(c.islower() for c in title)
        out, start = [], True
        for part in re.split(r"([^\W\d_]+)", title):
            if part and part[0].isalpha():
                if part.isupper() and (capitals or len(part) > 1) and not ROMAN.match(part):
                    case = self._case(part)
                    part = part if case == "upper" else part.capitalize() if case == "title" or start else part.lower()
                start = False
            elif re.search(r"[.(]\s*$", part):
                start = True
            out.append(part)
        return "".join(out)

    def known(self, title: str) -> float:
        """The share of the words of `title` the text has."""
        words = re.findall(r"[^\W\d_]+", title)
        return sum(1 for w in words if w.lower() in self.seen) / max(len(words), 1)

    def _case(self, word: str) -> str:
        for k in (word.lower(), _stem(word)):
            if k in self.seen:
                return self.seen[k].most_common(1)[0][0]
        return "lower"


def _entries(pages: Iterable[list[Row]]) -> list[tuple[str, float, int | None]]:
    """(title, size of type, page number or None) of the lines of the contents; a
    number on a line of its own belongs to the title above it."""
    out: list[list[Any]] = []
    last = 0
    for page in pages:
        for text, size in page:
            text = text.strip()
            if _key(text) in CONTENTS:
                continue
            if text.isdigit():
                if out and out[-1][2] is None and int(text) >= last:  # else the number of the contents page
                    out[-1][2] = last = int(text)
                continue
            m = NUMBERED.match(text)
            title, num = (m[1], int(m[2])) if m and _key(m[1]) and int(m[2]) >= last else (text, None)
            if out and out[-1][2] is None and (title[:1].islower() or title.startswith("(")):
                out[-1][0] += " " + title  # the title goes on
                out[-1][2] = num
            elif _key(title):
                out.append([title, size, num])
            last = num or last
    return [(t, s, n) for t, s, n in out]


def _levels(sizes: list[float]) -> list[int]:
    """Levels by size of type: the largest is 1; sizes less than a point apart are one."""
    steps: list[float] = []
    for s in sorted(set(sizes), reverse=True):
        if not steps or steps[-1] - s >= 1:
            steps.append(s)
    return [1 + min(sum(1 for t in steps if t > s + 0.5), 2) for s in sizes]


def _windows(page: list[Row]) -> list[str]:
    """Short lines and runs of up to three of them: a title in the text may wrap."""
    out = []
    for i in range(len(page)):
        text = ""
        for line, _ in page[i : i + 3]:
            if len(line) > 120:
                break
            text = f"{text} {line}".strip()
            out.append(text)
    return out


def _join(head: list[Row]) -> str:
    texts = [re.sub(r"\s+", " ", t).strip() for t, _ in head]
    if len(texts) > 1 and LABEL.match(texts[0]):
        return f"{texts[0]}. {' '.join(texts[1:])}"
    return " ".join(texts)


def _listing(page: list[Row]) -> bool:
    """A page that reads as contents: many lines end with a page number, or nearly all
    of them are short and start with a capital."""
    lines = [t.strip() for t, _ in page if t.strip()]
    if len(lines) < 4:
        return False
    numbered = sum(1 for t in lines if t.isdigit() or NUMBERED.match(t))
    titles = sum(1 for t in lines if len(t) <= 80 and not t[0].islower())
    return numbered >= 0.3 * len(lines) or titles >= 0.9 * len(lines)


def _body_size(rows: list[list[Row]]) -> float:
    weight: Counter[float] = Counter()
    for page in rows:
        for text, size in page:
            weight[round(size * 2) / 2] += len(text)
    return weight.most_common(1)[0][0] if weight else 0.0


def _caps(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 4 and len(text) <= 80 and all(c.isupper() for c in letters)


def _page_number(text: str) -> bool:
    return bool(re.match(r"^\d+$|^\d+\s*\||.*\|\s*\d+$", text.strip()))


def _text(page: list[Row]) -> str:
    return "\n".join(t for t, _ in page)


def _words(page: list[Row]) -> int:
    return len(re.findall(r"[^\W\d_]{2,}", _text(page)))


def _stem(word: str) -> str:
    return word.lower()[:5]


def _key(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())
