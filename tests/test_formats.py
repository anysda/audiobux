import os
import re
import sys
import zipfile
from pathlib import Path

import pytest
from conftest import stories

from audiobux import formats
from audiobux.formats import Document, Kind
from audiobux.formats.pages import from_pages, has_text_layer, top_levels

TITLES = [t for t, _ in stories()]


def letters(texts) -> str:
    return "".join(c for c in " ".join(texts).lower() if c.isalpha())


def check_stories(chapters):
    assert [c["title"] for c in chapters] == TITLES
    for chapter, (title, paragraphs) in zip(chapters, stories(), strict=True):
        assert chapter["paragraphs"][0] == title
        assert letters(chapter["paragraphs"]) == letters([title, *paragraphs])


def test_epub_chapters_from_toc(chekhov_epub, tmp_path):
    book = formats.read(chekhov_epub, tmp_path, 2)
    assert book.title == "Рассказы" and book.author == "Антон Чехов"
    check_stories(book.chapters)
    assert book.chapters[1]["paragraphs"][1:] == stories()[1][1]
    assert book.cover and book.keep_source


def test_pdf_chapters_from_bookmarks(chekhov_pdf, tmp_path):
    book = formats.read(chekhov_pdf, tmp_path, 2)
    check_stories(book.chapters)
    for p in (p for c in book.chapters for p in c["paragraphs"]):
        assert not re.search(r"^\d+ \||\| \d+$", p)
    assert book.first_page
    assert book.cover is None, "a first page of text is not a cover"


def test_pdf_scan_goes_to_ocr(chekhov_scan, tmp_path):
    book = formats.read(chekhov_scan, tmp_path, 2)
    assert not book.chapters
    assert [t for _, t, _ in top_levels(book.outline, 2)] == TITLES
    assert all((tmp_path / p).exists() for p in book.pages)


def test_garbage_text_layer_is_not_text():
    page = ["ÐÑÐ»Ð¸ Ð¿ÑÐ¾Ð¸Ð·Ð²ÐµÐ´ÐµÐ½Ð¸Ðµ ÑÐ²Ð»ÑÐµÑÑÑ Ð¿ÐµÑÐµÐ²Ð¾Ð´Ð¾Ð¼ Ð¸Ð»Ð¸ Ð¸Ð½ÑÐ¼"]
    assert not has_text_layer([page] * 10)
    assert not has_text_layer([["12"]] * 10)
    assert has_text_layer([[" ".join(stories()[0][1][:2])]] * 10)


def test_page_cleanup():
    pages = [
        ["Глава 1 | 7", "Первый абзац с пере-\nносом и про-\nдолжением", "7"],
        ["8 | Глава 1", "на следующей странице.", "Второй абзац.", "8"],
        ["Глава 1 | 9", "Третий абзац.", "9"],
    ]
    chapters = from_pages(pages, [(1, "Глава 1", 0)])
    assert chapters[0]["paragraphs"] == [
        "Глава 1",
        "Первый абзац с переносом и продолжением на следующей странице.",
        "Второй абзац.",
        "Третий абзац.",
    ]


# Telling a format by what is in the file


@pytest.fixture
def samples(chekhov_pdf, chekhov_djvu, chekhov_epub, chekhov_scan, chekhov_djvu_scan):
    return {"pdf": [chekhov_pdf, chekhov_scan], "djvu": [chekhov_djvu, chekhov_djvu_scan], "epub": [chekhov_epub]}


def test_extension_does_not_make_a_format(tmp_path, samples):
    text = tmp_path / "Книга.pdf"
    text.write_text("Это просто текст, а не PDF.\n" * 10)
    assert formats.detect(text) is None
    with pytest.raises(ValueError, match="unsupported format"):
        formats.read(text, tmp_path, 2)
    renamed = tmp_path / "book.epub"
    renamed.write_bytes(samples["pdf"][0].read_bytes())
    assert formats.read(renamed, tmp_path, 2).chapters


def test_epub_with_mimetype_anywhere_and_packed(tmp_path, chekhov_epub):
    """Some EPUBs put `mimetype` after other entries and compress it."""
    shuffled = tmp_path / "shuffled.epub"
    with zipfile.ZipFile(chekhov_epub) as src, zipfile.ZipFile(shuffled, "w", zipfile.ZIP_DEFLATED) as dst:
        names = src.namelist()
        assert names[0] == "mimetype"
        for name in [*names[1:], "mimetype"]:
            dst.writestr(name, src.read(name))
    found = formats.detect(shuffled)
    assert found is not None and found.name == "epub"
    check_stories(formats.read(shuffled, tmp_path, 2).chapters)


# A format added by a module and a line in the list, nothing else


MAGIC = b"TALE\n"


def read(path: Path, out: Path, depth: int) -> Document:
    """A made-up format: `# Title` starts a chapter, other lines are paragraphs."""
    book = Document(title="Сказки", author="Народ")
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        if line.startswith("# "):
            book.chapters.append({"title": line[2:], "paragraphs": [line[2:]]})
        elif line.strip():
            book.chapters[-1]["paragraphs"].append(line)
    return book


@pytest.fixture
def tale(tmp_path, monkeypatch) -> Path:
    monkeypatch.setitem(sys.modules, "audiobux.formats.tale", sys.modules[__name__])
    monkeypatch.setattr(formats, "KINDS", [*formats.KINDS, Kind("tale", (".tale",), MAGIC)])
    path = tmp_path / "Сказки.tale"
    lines = [f"# {title}\n" + "\n".join(paragraphs[:2]) for title, paragraphs in stories()]
    path.write_bytes(MAGIC + "\n".join(lines).encode())
    return path


BOOK = os.environ.get("AUDIOBUX_TEST_BOOK", "")


@pytest.mark.skipif(not Path(BOOK).is_file(), reason="AUDIOBUX_TEST_BOOK is not set")
def test_real_book(tmp_path):
    chapters = formats.read(Path(BOOK), tmp_path, 2).chapters
    assert len(chapters) >= 10
    assert all(c["paragraphs"] for c in chapters)
    for p in (p for c in chapters for p in c["paragraphs"]):
        assert not re.search(r"^\d+\s*\||\|\s*\d+$", p), p
