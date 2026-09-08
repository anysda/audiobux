"""Figures, diagrams and tables, told in words.

Extraction leaves a mark `[[figure N]]` where a figure stood and keeps its picture
(or, for an EPUB table, its text). The vision model looks at it and says in a
sentence or three what a listener should take from it; the description goes into
the text in place of the mark. Icons, logos and rules are not worth a word: the
model answers `-` for them and the mark is dropped.
"""

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .llm import Llm
from .text import FIGURE_SIDE, MARK

PROMPT = """Ты готовишь техническую книгу к озвучке. Тебе дают рисунок, схему, диаграмму
или таблицу из книги и подпись к ней. Опиши по-русски в одном-трёх предложениях, что
слушатель должен из неё узнать, так, чтобы это естественно звучало вслух. Начни с
"На рисунке" (для таблицы "В таблице"), если в подписи есть номер, назови его:
"На рисунке 1.2 схема: ...". Назови главное: какие части и как они связаны, что с чем
сравнивается. Не перечисляй все надписи, не используй списки, разметку и скобки.
Если это листинг кода, одной фразой скажи, что он делает, начиная с "В листинге".
Если картинка ничего не значит для текста (логотип, иконка, украшение, линейка),
ответь одним знаком -"""


class Figures:
    def __init__(self, vision: Llm, llm: Llm | None = None) -> None:
        self.vision, self.llm = vision, llm or vision

    def describe(
        self, figures: list[dict[str, Any]], folder: Path, wait: Callable[[], None] = lambda: None
    ) -> list[str]:
        """A description for every figure, "" where nothing is worth saying."""
        out: list[str] = []
        for i in range(0, len(figures), 8):
            wait()
            out += self.vision.map(lambda f: self._one(f, folder), figures[i : i + 8])
        return out

    def _one(self, figure: dict[str, Any], folder: Path) -> str:
        context = f"Подпись: {figure.get('caption') or 'нет'}\nТекст рядом: {figure.get('near', '')[:600]}"
        if figure.get("image"):
            reply = self.vision.ask(PROMPT, context, image=(folder / figure["image"]).read_bytes())
        else:
            reply = self.llm.ask(PROMPT, context + "\nТаблица:\n" + figure.get("text", "")[:3000])
        return spoken(reply)

    def close(self) -> None:
        self.vision.close()


def spoken(reply: str) -> str:
    """The reply as one paragraph to read; "" for a decoration."""
    text = re.sub(r"[*_#`>|]+", " ", reply)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    text = re.sub(r"\s+", " ", text).strip()
    if len(re.findall(r"[а-яё]", text.lower())) < 15:
        return ""
    return text if text.endswith((".", "!", "?", "…")) else text + "."


def put(chapters: list[dict[str, Any]], descriptions: list[str]) -> list[dict[str, Any]]:
    """Marks replaced with descriptions; a mark with nothing to say is dropped."""
    for chapter in chapters:
        paragraphs = []
        for p in chapter["paragraphs"]:
            m = MARK.match(p)
            if not m:
                paragraphs.append(p)
            elif int(m.group(1)) < len(descriptions) and descriptions[int(m.group(1))]:
                paragraphs.append(descriptions[int(m.group(1))])
        chapter["paragraphs"] = paragraphs
    return [c for c in chapters if c["paragraphs"]]


def near(chapters: list[dict[str, Any]], figures: list[dict[str, Any]]) -> None:
    """Gives every figure the paragraph before its mark, for the model to know the topic."""
    for chapter in chapters:
        before = ""
        for p in chapter["paragraphs"]:
            m = MARK.match(p)
            if m and int(m.group(1)) < len(figures):
                figures[int(m.group(1))]["near"] = before
            elif not m:
                before = p


# Scans: the picture behind a description the OCR model wrote


def scan_regions(image: Path) -> list[tuple[int, int, int, int]]:
    """Pixel boxes of the figures on a page image, top to bottom.

    A figure is a run of rows with something on them, unbroken and at least three
    text lines high (frames, fills and axes hold its rows together). A sparse line
    right above it or up to two letter heights below it (axis labels) goes with it;
    a full line of text does not, nor the short last line of a paragraph above."""
    import numpy as np
    import pymupdf

    pix = pymupdf.Pixmap(str(image))
    if pix.n - pix.alpha != 1:
        pix = pymupdf.Pixmap(pymupdf.csGRAY, pix)
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, : pix.width]
    ink = a < int(np.percentile(a, 95)) - 15  # darker than the paper
    bands: list[list[int]] = []
    start: int | None = None
    for y, filled in enumerate([*(ink.sum(1) > 2), False]):
        if filled and start is None:
            start = y
        elif not filled and start is not None:
            if bands and start - bands[-1][1] <= max(2, pix.height // 400):
                bands[-1][1] = y
            else:
                bands.append([start, y])
            start = None
    if not bands:
        return []
    line = sorted(bottom - top for top, bottom in bands)[len(bands) // 2]
    used = np.where(ink.any(0))[0]
    left, right = int(used[0]), int(used[-1]) + 1
    step = max(4, line // 2)

    def sparse(band: list[int]) -> bool:
        cols = ink[band[0] : band[1], left:right].any(0)
        bins = [cols[i : i + step].any() for i in range(0, len(cols), step)]
        return bool(sum(bins) < 0.6 * max(len(bins), 1))

    found: list[tuple[int, int, int, int]] = []
    for i, (y0, y1) in enumerate(bands):
        if not (3 * line <= y1 - y0 and 0.05 * pix.height <= y1 - y0 <= 0.95 * pix.height):
            continue
        j = i
        while j + 1 < len(bands) and bands[j + 1][0] - bands[j][1] <= 2 * line and sparse(bands[j + 1]):
            j += 1
        k = i
        while k > 0 and bands[k][0] - bands[k - 1][1] <= line and sparse(bands[k - 1]):
            k -= 1
        y0, y1 = bands[k][0], bands[j][1]
        cols = np.where(ink[y0:y1].any(0))[0]
        box = (int(cols[0]), y0, int(cols[-1]) + 1, y1)
        if not found or found[-1] != box:
            found.append(box)
    return found


def cut_scan(image: Path, n: int, said: list[str], folder: Path) -> list[dict[str, Any]]:
    """The figures of scanned page `n` for the descriptions the OCR model wrote on it,
    cut out as pictures. Only when the page shows as many figures as there are
    descriptions of pictures: a wrong picture is worse than none. A listing is text,
    not a picture."""
    import pymupdf

    said = [s for s in said if s and not s.startswith("В листинге")]
    boxes = scan_regions(image) if said else []
    if not said or len(boxes) != len(said):
        return []
    page = pymupdf.Pixmap(str(image))
    if page.alpha:
        page = pymupdf.Pixmap(page, 0)
    out: list[dict[str, Any]] = []
    (folder / "figures").mkdir(exist_ok=True)
    for k, ((x0, y0, x1, y1), text) in enumerate(zip(boxes, said, strict=True)):
        pad = max(4, page.width // 200)
        rect = pymupdf.IRect(max(x0 - pad, 0), max(y0 - pad, 0), min(x1 + pad, page.width), min(y1 + pad, page.height))
        crop = pymupdf.Pixmap(page.colorspace, rect, 0)
        crop.copy(page, rect)
        while max(crop.width, crop.height) > FIGURE_SIDE:
            crop.shrink(1)
        name = f"figures/scan-{n + 1:04}-{k + 1}.png"
        crop.save(folder / name)
        out.append({"image": name, "said": text, "page": n})
    return out
