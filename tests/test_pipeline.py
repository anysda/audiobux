import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from conftest import cover_jpeg, stories

from audiobux import m4b, models, pipeline
from audiobux.book import Book
from audiobux.config import Settings
from audiobux.pipeline import blocks, headings
from audiobux.quiet import QuietHours

TITLES = [t for t, _ in stories()]


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_chapters", "-show_format", "-show_streams", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)


class Night(QuietHours):
    """Night falls when told to; the first sleep brings the morning."""

    def __init__(self):
        super().__init__("")
        self.night, self.sleeps = False, 0
        self.sleep = self._sleep

    def seconds_left(self, now=None):
        return 3600.0 if self.night else 0.0

    def _sleep(self, seconds):
        self.sleeps += 1
        self.night = False


class CountingTts(models.ToneTts):
    def __init__(self, fail_after=None, on_call=None):
        super().__init__()
        self.calls, self.fail_after, self.on_call = 0, fail_after, on_call

    def synth(self, text, seed=0):
        if self.fail_after is not None and self.calls >= self.fail_after:
            raise RuntimeError("power cut")
        self.calls += 1
        if self.on_call:
            self.on_call(self.calls)
        return super().synth(text, seed)


def context(tmp_path, tts=None, quiet=None) -> pipeline.Context:
    settings = Settings(data=tmp_path / "data", llm="", accent="", asr="", ocr="")
    m = models.load(settings)
    if tts:
        m.tts = tts
    return pipeline.Context(settings, m, quiet or QuietHours(""))


def new_book(tmp_path, source: Path) -> Book:
    return Book.create(tmp_path / "work", source.name, lambda p: shutil.copyfile(source, p))


def test_m4b_has_chapters_cover_and_metadata(tmp_path):
    wavs, pieces = [], []
    for ch in range(3):
        for k in range(2):
            wav = tmp_path / f"{ch}{k}.wav"
            t = np.arange(24000) / 24000
            sf.write(wav, 0.2 * np.sin(2 * np.pi * (200 + 100 * ch) * t), 24000, subtype="PCM_16")
            wavs.append(wav)
            pieces.append({"chapter": ch, "pause": 0.5 if k == 0 else 2.0})
    (tmp_path / "cover.jpg").write_bytes(cover_jpeg())
    out = tmp_path / "book.m4b"
    m4b.assemble(
        wavs,
        pieces,
        ["Один", "Два", "Три = 3"],
        out,
        {"title": "Книга", "artist": "Автор", "genre": "Audiobook"},
        tmp_path / "cover.jpg",
    )

    info = probe(out)
    assert [c["tags"]["title"] for c in info["chapters"]] == ["Один", "Два", "Три = 3"]
    assert [round(float(c["start_time"]), 1) for c in info["chapters"]] == [0.0, 4.5, 9.0]
    assert info["format"]["tags"]["title"] == "Книга" and info["format"]["tags"]["artist"] == "Автор"
    assert any(s.get("disposition", {}).get("attached_pic") for s in info["streams"])
    assert abs(float(info["format"]["duration"]) - 13.5) < 0.2


def test_epub_to_m4b(tmp_path, chekhov_epub):
    book = new_book(tmp_path, chekhov_epub)
    result = pipeline.run(book, context(tmp_path))
    info = probe(result)
    assert [c["tags"]["title"] for c in info["chapters"]] == TITLES
    assert info["format"]["tags"]["title"] == "Рассказы"
    assert all(book.done(s.name) for s in pipeline.STEPS)


def test_rerun_continues_from_the_last_piece(tmp_path, chekhov_epub):
    book = new_book(tmp_path, chekhov_epub)
    with pytest.raises(RuntimeError):
        pipeline.run(book, context(tmp_path, CountingTts(fail_after=5)))
    assert book.done("chunk") and not book.done("tts")
    assert len(list(book.path("tts").glob("*.wav"))) == 5

    tts = CountingTts()
    pipeline.run(book, context(tmp_path, tts))
    assert tts.calls == len(book.read("chunks.json")) - 5
    assert book.path("book.m4b").exists()


def test_quiet_hours_pause_between_pieces(tmp_path, chekhov_epub):
    night = Night()

    def nightfall(calls):
        if calls == 3:
            night.night = True

    tts = CountingTts(on_call=nightfall)
    book = new_book(tmp_path, chekhov_epub)
    pipeline.run(book, context(tmp_path, tts, night), until="tts")
    assert night.sleeps == 1
    assert tts.calls == len(book.read("chunks.json"))


def test_scan_waits_for_ocr(tmp_path, chekhov_scan):
    book = new_book(tmp_path, chekhov_scan)
    with pytest.raises(NotImplementedError):
        pipeline.run(book, context(tmp_path))
    assert book.done("extract") and not book.done("ocr")


def test_scan_with_an_ocr_model(tmp_path, chekhov_scan, chekhov_pdf):
    import pymupdf

    pages = [page.get_text() for page in pymupdf.open(chekhov_pdf)]

    class PdfOcr:
        def page_text(self, image):
            return pages[int(image.stem) - 1].replace("\n", "\n\n")

    ctx = context(tmp_path)
    ctx.models.ocr = PdfOcr()
    book = new_book(tmp_path, chekhov_scan)
    pipeline.run(book, ctx, until="ocr")
    assert [c["title"] for c in book.read("text.json")["chapters"]] == TITLES


def test_scan_without_toc_gets_chapters_from_ocr_headings():
    first = blocks("# Глава 1\nПервый абзац,\nвторая строка.\n\nВторой абзац.")
    second = blocks("Конец первой главы.\n# Глава 2\n\nНачало второй.")
    assert first == ["# Глава 1", "Первый абзац,\nвторая строка.", "Второй абзац."]
    assert headings([first, second]) == [(1, "Глава 1", 0), (1, "Глава 2", 1)]
