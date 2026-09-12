"""Model slots of the pipeline.

Each slot is a small interface: a model only has to implement the same method.
OCR is a vision language model reading the page image; TTS is F5-TTS_RUSSIAN,
or a tone for tests.
"""

from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from .cleanup import Editor
from .config import Settings
from .figures import Figures
from .llm import Llm
from .speech import Speech


class Ocr(Protocol):
    def page_text(self, image: Path) -> str: ...


class Tts(Protocol):
    @property
    def room(self) -> float:
        """Seconds of speech one piece may have."""

    def seconds(self, text: str) -> float:
        """How long the voice makes the text."""

    def synth_many(self, texts: list[str], seed: int = 0) -> Generator[tuple[np.ndarray, int], None, None]:
        """Mono float32 samples and their sample rate for every text, yielded in order;
        another seed gives other takes, and a model may voice several texts at once."""


OCR_PROMPT = """Ты переписываешь страницу русской книги с картинки в текст.
Перепиши весь текст страницы дословно, ничего не исправляй, не пересказывай и не добавляй.
Абзацы разделяй пустой строкой, строки внутри абзаца склей, переносы слов убери.
Заголовок главы или раздела пиши отдельной строкой, начиная её с "# ".
Колонтитулы, номера страниц и подписи к картинкам пропусти.
Если на странице есть рисунок, схема, диаграмма или таблица, которые важны для смысла,
на их месте напиши отдельной строкой "[Рисунок: ...]" с описанием по-русски в одно-три
предложения для чтения вслух: что изображено и что это показывает, например
"[Рисунок: На рисунке схема: заказ проходит три этапа ...]". Таблицу перескажи так же,
начиная с "В таблице". Листинг кода не переписывай, а перескажи одной фразой, что он
делает: "[Рисунок: В листинге ...]". Иконки, логотипы, линейки и украшения не описывай.
Если на странице нет текста, ответь пустой строкой."""

# the long side of the page the model sees; more costs tokens and reads no better
OCR_SIDE = 1800


class VlmOcr:
    """A page image read by a vision language model behind llama.cpp."""

    def __init__(self, llm: Llm) -> None:
        self.llm = llm

    def page_text(self, image: Path) -> str:
        reply = self.llm.ask(OCR_PROMPT, "Текст страницы:", image=_page_png(image))
        return reply.strip().removeprefix("```").removesuffix("```").strip()

    def close(self) -> None:
        self.llm.close()


def _page_png(image: Path) -> bytes:
    import pymupdf

    pix = pymupdf.Pixmap(str(image))
    while max(pix.width, pix.height) > OCR_SIDE:
        pix.shrink(1)
    png: bytes = pix.tobytes("png")
    return png


class ToneTts:
    """A beep per chunk, as long as the text would take to read."""

    rate = 24000
    chars_per_second = 15

    def __init__(self, silent: bool = False) -> None:
        self.silent = silent

    room = 15.0

    def seconds(self, text: str) -> float:
        return len(text) / self.chars_per_second

    def synth_many(self, texts: list[str], seed: int = 0) -> Generator[tuple[np.ndarray, int], None, None]:
        for text in texts:
            yield self.synth(text, seed)

    def synth(self, text: str, seed: int = 0) -> tuple[np.ndarray, int]:
        n = int(self.rate * max(len(text) / self.chars_per_second, 0.2))
        if self.silent:
            return np.zeros(n, np.float32), self.rate
        t = np.arange(n) / self.rate
        wave = 0.3 * np.sin(2 * np.pi * 220 * t)
        fade = min(n // 2, self.rate // 50)
        wave[:fade] *= np.linspace(0, 1, fade)
        wave[n - fade :] *= np.linspace(1, 0, fade)
        return wave.astype(np.float32), self.rate


@dataclass
class Models:
    ocr: Ocr | None
    editor: Editor
    speech: Speech
    tts: Tts
    llm: Llm | None = None
    figures: "Figures | None" = None

    def release_llm(self) -> None:
        """Frees the card for the speech model."""
        for llm in {id(x): x for x in (self.llm, getattr(self.ocr, "llm", None)) if x}.values():
            llm.close()


def load(settings: Settings) -> Models:
    tts: Tts
    if settings.tts == "f5":
        from .f5 import F5Tts

        tts = F5Tts(settings.voice, settings.voice_text, settings.models)
    elif settings.tts in ("tone", "silence"):
        tts = ToneTts(silent=settings.tts == "silence")
    else:
        raise ValueError(f"unknown TTS engine: {settings.tts}")
    # one llama-server for every step when the text and the vision model are the same
    vision = (
        Llm(
            settings.ocr,
            settings.models,
            parallel=8,
            context=8192,
            vision=settings.ocr_vision,
            placement=("--fit", "on"),
        )
        if settings.ocr
        else None
    )
    llm: Llm | None
    if settings.llm and vision and settings.llm == settings.ocr:
        llm = vision
    else:
        llm = Llm(settings.llm, settings.models) if settings.llm else None
    return Models(
        ocr=VlmOcr(vision) if vision else None,
        editor=Editor(llm, settings.fix_max_change),
        speech=Speech(llm, settings.accent, settings.stress == "plus", settings.models),
        tts=tts,
        llm=llm,
        figures=Figures(vision, llm) if vision else None,
    )
