import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path

import numpy as np
import pymupdf
import pytest
import soundfile as sf
from conftest import cover_jpeg, stories
from test_pipeline import context, probe
from test_worker import MSK, Clock, make, state, upload

from audiobux import m4b, reading, refresh
from audiobux.quiet import QuietHours

TITLES = [t for t, _ in stories()]


def sound(path: Path) -> str:
    """The md5 of the audio stream as it is in the file, not decoded."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a", "-c", "copy", "-f", "md5", "-"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def voiced(tmp_path, chekhov_pdf):
    """A worker with the PDF voiced by the reading of today."""
    w = make(tmp_path)
    book_id = w.submit(upload(w, chekhov_pdf), "Чехов - Рассказы")["id"]
    w.run(until_idle=True)
    return w, book_id


def aged(w, book_id: str) -> None:
    """The book as an older reading left it: one chapter named by the file, headings read
    out as paragraphs, the m4b with that one chapter, the reading version 0."""
    folder = w.settings.library / book_id
    now = json.loads((folder / "sync.json").read_text())
    blocks = []
    for ch in now["chapters"]:
        for b in ch["blocks"]:
            if b["type"] == "h":
                if b["t0"] is None:
                    continue
                b = {"type": "p", "sentences": [{"text": b["text"].upper(), "t0": b["t0"], "t1": b["t1"]}]}
            blocks.append(b)
    head = {"type": "h", "text": "Чехов - Рассказы", "t0": None, "t1": None}
    one = {"title": "Чехов - Рассказы", "t0": 0.0, "t1": now["duration"], "blocks": [head, *blocks]}
    (folder / "sync.json").write_text(json.dumps(now | {"chapters": [one]}, ensure_ascii=False))
    chapters = [{"title": "Чехов - Рассказы", "start": 0.0}]
    m4b.retag(folder / "book.m4b", folder / "old.m4b", {"title": "Чехов - Рассказы"}, chapters, now["duration"])
    (folder / "old.m4b").replace(folder / "book.m4b")
    w.library.update(book_id, parse=0, title="Чехов - Рассказы", author="", chapters=json.dumps(chapters))


def test_a_book_of_an_older_reading_gets_its_chapters_around_the_same_sound(tmp_path, chekhov_pdf):
    w, book_id = voiced(tmp_path, chekhov_pdf)
    folder = w.settings.library / book_id
    before = json.loads(state(w, book_id)["chapters"])
    assert state(w, book_id)["parse"] == refresh.VERSION, "a book voiced today is not read again"
    heard = sound(folder / "book.m4b")
    aged(w, book_id)
    assert w.library.stale(refresh.VERSION)["id"] == book_id

    w.run(until_idle=True)

    got = state(w, book_id)
    assert (got["state"], got["title"], got["author"], got["parse"]) == ("done", "Рассказы", "Чехов", refresh.VERSION)
    chapters = json.loads(got["chapters"])
    assert [c["title"] for c in chapters] == [c["title"] for c in before] == TITLES
    assert [c["start"] for c in chapters] == [round(c["start"], 3) for c in before]
    info = probe(folder / "book.m4b")
    assert [c["tags"]["title"] for c in info["chapters"]] == TITLES
    assert [round(float(c["start_time"]), 1) for c in info["chapters"]] == [round(c["start"], 1) for c in before]
    assert info["format"]["tags"]["title"] == "Рассказы" and info["format"]["tags"]["artist"] == "Чехов"
    assert any(s.get("disposition", {}).get("attached_pic") for s in info["streams"]), "the cover stays"
    assert sound(folder / "book.m4b") == heard, "the sound is the same, not encoded again"
    synced = json.loads((folder / "sync.json").read_text())
    assert [c["title"] for c in synced["chapters"]] == TITLES
    assert all(
        c["blocks"][0] | {"t0": 0} == {"type": "h", "text": c["title"], "t0": 0, "t1": c["blocks"][0]["t1"]}
        for c in synced["chapters"]
    )
    assert synced["chapters"][0]["t0"] == 0 and synced["chapters"][-1]["t1"] == synced["duration"]
    assert not list(w.settings.work.iterdir()), "nothing is left in work/"


def test_a_restarted_worker_does_not_read_again(tmp_path, chekhov_pdf, monkeypatch):
    w, book_id = voiced(tmp_path, chekhov_pdf)
    aged(w, book_id)
    w.run(until_idle=True)
    calls = []
    monkeypatch.setattr(refresh, "run", lambda *a: calls.append(a))
    again = make(tmp_path)
    again.run(until_idle=True)
    assert calls == [] and state(again, book_id)["title"] == "Рассказы"


def test_a_failed_reading_leaves_the_book_as_it_was(tmp_path, chekhov_pdf, monkeypatch, caplog):
    w, book_id = voiced(tmp_path, chekhov_pdf)
    aged(w, book_id)
    folder = w.settings.library / book_id
    files = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}

    def broken(folder, name, work, ctx, had):
        (work / "half").mkdir(parents=True)
        raise RuntimeError("bad file")

    monkeypatch.setattr(refresh, "run", broken)
    with caplog.at_level(logging.ERROR):
        w.run(until_idle=True)
    got = state(w, book_id)
    assert (got["state"], got["title"], got["error"], got["parse"]) == (
        "done",
        "Чехов - Рассказы",
        None,
        refresh.VERSION,
    )
    assert {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()} == files, "still to be heard as it was"
    assert "reading again failed" in caplog.text and "bad file" in caplog.text
    assert not list(w.settings.work.iterdir())


def test_no_reading_again_in_quiet_hours(tmp_path, chekhov_pdf):
    clock = Clock(datetime(2026, 11, 3, 9, 0, tzinfo=MSK))
    w = make(tmp_path, quiet=QuietHours("00:00-08:00"), clock=clock)
    book_id = w.submit(upload(w, chekhov_pdf), "Чехов - Рассказы")["id"]
    w.run(until_idle=True)
    aged(w, book_id)
    clock.now = datetime(2026, 11, 4, 3, 0, tzinfo=MSK)
    w.run(until_idle=True)
    assert state(w, book_id)["parse"] == 0
    clock.now = datetime(2026, 11, 4, 8, 30, tzinfo=MSK)
    w.run(until_idle=True)
    assert state(w, book_id)["parse"] == refresh.VERSION


def plain_pdf(path: Path, paragraphs: list[str]) -> None:
    """A PDF with no bookmarks and no headings: one chapter."""
    writer = pymupdf.DocumentWriter(str(path))
    story = pymupdf.Story("".join(f"<p>{p}</p>" for p in paragraphs))
    rect = pymupdf.paper_rect("a5")
    more = True
    while more:
        device = writer.begin_page(rect)
        more, _ = story.place(rect + (40, 60, -40, -50))
        story.draw(device)
        writer.end_page()
    writer.close()


def old_sync(paragraphs: list[str], name: str) -> dict:
    blocks = [
        {"type": "p", "sentences": [{"text": p, "t0": float(n), "t1": n + 0.9}]} for n, p in enumerate(paragraphs)
    ]
    head = {"type": "h", "text": name, "t0": None, "t1": None}
    return {
        "version": 1,
        "duration": float(len(paragraphs)),
        "chapters": [{"title": name, "t0": 0.0, "t1": 9.0, "blocks": [head, *blocks]}],
    }


def titled_pdf(path: Path) -> None:
    """One chapter with no heading of its own, the title and the author in the metadata."""
    plain_pdf(path, stories()[0][1])
    with pymupdf.open(path) as doc:
        doc.set_metadata({"title": "Рассказы", "author": "Антон Чехов"})
        doc.saveIncr()


def chapter_titles(w, book_id: str) -> tuple[list[str], ...]:
    """The chapters of a book as the list, the sync and the m4b have them."""
    folder = w.settings.library / book_id
    synced = json.loads((folder / "sync.json").read_text())
    return (
        [c["title"] for c in json.loads(state(w, book_id)["chapters"])],
        [c["title"] for c in synced["chapters"]],
        [c["tags"]["title"] for c in probe(folder / "book.m4b")["chapters"]],
    )


@pytest.mark.parametrize("named", ["Чехов - Рассказы", "scan_0001", ""])
def test_reading_again_finds_what_the_first_reading_did(tmp_path, named):
    """The file's name is the one it came with, not the title found in the book since:
    a chapter named after the book keeps that name."""
    source = tmp_path / "book.pdf"
    titled_pdf(source)
    w = make(tmp_path / "w")
    book_id = w.submit(upload(w, source), named)["id"]
    w.run(until_idle=True)
    first = chapter_titles(w, book_id)
    assert first == (["Рассказы"],) * 3
    w.library.update(book_id, parse=0)

    w.run(until_idle=True)

    got = state(w, book_id)
    assert (got["title"], got["author"], got["parse"]) == ("Рассказы", "Антон Чехов", refresh.VERSION)
    assert chapter_titles(w, book_id) == first


def test_a_book_finished_before_its_file_name_was_kept(tmp_path):
    """No name to tell the title from: the title found is the first chapter's; the title
    and the author nothing in the book gives stay as they were, the title being the
    first chapter's too, as nothing tells it came from the file's name."""
    source = tmp_path / "book.pdf"
    titled_pdf(source)
    plain = tmp_path / "plain.pdf"
    plain_pdf(plain, stories()[1][1])
    w = make(tmp_path / "w")
    titled, untitled = (w.submit(upload(w, s), "scan_0001")["id"] for s in (source, plain))
    w.run(until_idle=True)
    assert chapter_titles(w, untitled) == ([""],) * 3
    for book_id in (titled, untitled):
        w.library.update(book_id, parse=0, name=None)
    w.library.update(untitled, author="Чехов")

    w.run(until_idle=True)

    assert chapter_titles(w, titled) == (["Рассказы"],) * 3
    got = state(w, untitled)
    assert (got["title"], got["author"], got["parse"]) == ("scan 0001", "Чехов", refresh.VERSION)
    assert chapter_titles(w, untitled) == (["scan 0001"],) * 3


def test_a_book_without_chapters_and_without_its_m4b(tmp_path):
    """One chapter named after the book; with the m4b gone the chapters still come, nothing is made up."""
    _, paragraphs = stories()[0]
    folder = tmp_path / "book"
    folder.mkdir()
    plain_pdf(folder / "source.pdf", paragraphs)
    (folder / "sync.json").write_text(json.dumps(old_sync(paragraphs, "Чехов_-_Рассказы_1990")))

    found = refresh.run(folder, "Чехов - Рассказы.pdf", tmp_path / "work", context(tmp_path))

    assert found == {"title": "Рассказы", "author": "Чехов", "chapters": [{"title": "Рассказы", "start": 0.0}]}
    assert not (folder / "book.m4b").exists()
    synced = json.loads((folder / "sync.json").read_text())
    assert [c["title"] for c in synced["chapters"]] == ["Рассказы"]
    blocks = synced["chapters"][0]["blocks"]
    assert blocks[0] == {"type": "h", "text": "Рассказы", "t0": None, "t1": None}
    assert [b["sentences"][0]["t0"] for b in blocks[1:]] == [float(n) for n in range(len(paragraphs))]
    assert (folder / "book.epub").exists()


def test_without_a_sync_only_the_title_and_the_author(tmp_path):
    folder = tmp_path / "book"
    folder.mkdir()
    plain_pdf(folder / "source.pdf", stories()[0][1])
    found = refresh.run(folder, "Чехов - Рассказы.pdf", tmp_path / "work", context(tmp_path))
    assert found == {"title": "Рассказы", "author": "Чехов"}
    assert sorted(p.name for p in folder.iterdir()) == ["source.pdf"]


def test_a_table_of_contents_read_out_is_not_where_the_chapters_start():
    """Titles read out one after another at the front; in the body a heading may be glued
    to the text after it, or its blocks put out of order; a title the text never says is
    not found, and its text stays with the chapter before."""
    (t1, p1), (t2, p2), (t3, p3) = stories()
    texts = ["Рассказы", t1.upper(), "3", t2.upper(), "17", t3.upper(), "40", "Издание для проверки."]
    body1 = len(texts)
    texts += [t1.upper(), p1[1], p1[0], *p1[2:]]
    body2 = len(texts)
    texts += [f"{t2.upper()} {p2[0]}", *p2[1:]]
    texts += p3
    chapters = [{"title": t, "paragraphs": [t.upper(), *p]} for t, p in ((t1, p1), (t2, p2), (t3, p3))]
    assert refresh.headings(texts, chapters) == [(body1, t1), (body2, t2)]


def test_a_heading_in_two_lines_and_one_at_the_very_start():
    (t1, p1), (t2, p2), _ = stories()
    title = f"{t2} (вместо послесловия)"
    texts = [t1, *p1, t2.upper(), "(Вместо послесловия)", *p2]
    chapters = [
        {"title": t1, "paragraphs": [t1, *p1]},
        {"title": title, "paragraphs": [t2, "(вместо послесловия)", *p2]},
    ]
    assert refresh.headings(texts, chapters) == [(0, t1), (len(p1) + 1, title)]
    old = old_sync(texts, "x")
    data, said = refresh.regroup(old, chapters, "Рассказы", "Чехов", [])
    assert [c["title"] for c in data["chapters"]] == [t1, title], "no chapter before the first one"
    assert [len(s) for s in said] == [len(p1) + 1, len(p2) + 2]


def test_listings_figures_and_silent_blocks_keep_their_times(tmp_path):
    (tmp_path / "figures").mkdir()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), 0)
    pix.save(tmp_path / "figures/fig_1.jpg")
    blocks = [
        {"type": "p", "sentences": [{"text": "ГЛАВА ПЕРВАЯ", "t0": 0.0, "t1": 1.0}]},
        {"type": "code", "text": "print(1)", "t0": None, "t1": None},
        {"type": "code", "text": "x = 2", "t0": 1.0, "t1": 2.0},
        {"type": "figure", "src": "figures/fig_1.jpg", "alt": "На рисунке круг", "t0": 2.0, "t1": 3.0},
        {"type": "p", "sentences": [{"text": "Конец.", "t0": None, "t1": None}]},
    ]
    old = {"version": 1, "duration": 3.0, "chapters": [{"title": "", "t0": 0.0, "t1": 3.0, "blocks": blocks}]}
    chapters = [{"title": "Глава первая", "paragraphs": ["Глава первая", "x = 2"]}]
    data, said = refresh.regroup(old, chapters, "Книга", "", [])
    figures = [{"image": "figures/fig_1.jpg", "said": "На рисунке круг"}]
    synced = refresh.retimed(reading.chapters(data, figures, tmp_path, "Книга.pdf"), said, 3.0)
    assert synced["chapters"][0]["blocks"] == [
        {"type": "h", "text": "Глава первая", "t0": 0.0, "t1": 1.0},
        {"type": "code", "text": "print(1)", "t0": None, "t1": None},
        {"type": "code", "text": "x = 2", "t0": 1.0, "t1": 2.0},
        {"type": "figure", "src": "figures/fig_1.jpg", "alt": "На рисунке круг", "t0": 2.0, "t1": 3.0},
        {"type": "p", "sentences": [{"text": "Конец.", "t0": None, "t1": None}]},
    ]


def test_retag_keeps_the_sound_and_the_cover(tmp_path):
    wav = tmp_path / "a.wav"
    t = np.arange(48000) / 24000
    sf.write(wav, 0.2 * np.sin(2 * np.pi * 300 * t), 24000, subtype="PCM_16")
    (tmp_path / "cover.jpg").write_bytes(cover_jpeg())
    first = tmp_path / "first.m4b"
    m4b.assemble([wav], [{"chapter": 0, "pause": 0.5}], ["Одна"], first, {"title": "x"}, tmp_path / "cover.jpg")
    out = tmp_path / "out.m4b"
    chapters = [{"title": "Начало", "start": 0.0}, {"title": "Конец = 2", "start": 1.25}]
    m4b.retag(first, out, {"title": "Книга", "artist": "Автор"}, chapters, 2.5)
    info = probe(out)
    assert [(c["tags"]["title"], float(c["start_time"])) for c in info["chapters"]] == [
        ("Начало", 0.0),
        ("Конец = 2", 1.25),
    ]
    assert info["format"]["tags"]["artist"] == "Автор"
    assert any(s.get("disposition", {}).get("attached_pic") for s in info["streams"])
    assert sound(out) == sound(first) and info["format"]["duration"] == probe(first)["format"]["duration"]
