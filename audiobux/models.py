"""Model slots of the pipeline.

Each slot is a small interface: a model only has to implement the same method.
TTS is a tone for now, as long as the text would take to read.
"""

from collections.abc import Generator
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .config import Settings


class Tts(Protocol):
    @property
    def room(self) -> float:
        """Seconds of speech one piece may have."""

    def seconds(self, text: str) -> float:
        """How long the voice makes the text."""

    def synth_many(self, texts: list[str], seed: int = 0) -> Generator[tuple[np.ndarray, int], None, None]:
        """Mono float32 samples and their sample rate for every text, yielded in order;
        another seed gives other takes, and a model may voice several texts at once."""


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
    tts: Tts


def load(settings: Settings) -> Models:
    tts: Tts
    if settings.tts in ("tone", "silence"):
        tts = ToneTts(silent=settings.tts == "silence")
    else:
        raise ValueError(f"unknown TTS engine: {settings.tts}")
    return Models(tts=tts)
