import os
import re
import sys
import zipfile
from pathlib import Path

import pytest
from conftest import stories
from test_pipeline import context, new_book, probe

from audiobux import formats, pipeline
from audiobux.formats import Document, Kind
from audiobux.formats.pages import Mend, from_pages, has_text_layer, spaces, top_levels

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


def test_djvu_with_text_layer(chekhov_djvu, tmp_path):
    book = formats.read(chekhov_djvu, tmp_path, 2)
    check_stories(book.chapters)


def test_djvu_without_text_layer(chekhov_djvu_scan, tmp_path):
    book = formats.read(chekhov_djvu_scan, tmp_path, 2)
    assert [t for _, t, _ in top_levels(book.outline, 2)] == TITLES
    assert len(book.pages) > 3 and all((tmp_path / p).exists() for p in book.pages)


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


def test_a_word_broken_by_a_space_is_made_whole():
    texts = ["ЧЕТЫ РЕ ВОЗРАСТА СИСТЕМ", "Четыре года и четыре дня.", "Старый дом стоял, старый дом", "домино"]
    mend = Mend(texts)
    assert mend(texts[0]) == "ЧЕТЫРЕ ВОЗРАСТА СИСТЕМ"
    assert mend("Четы ре возраста") == "Четыре возраста", "the case does not matter"
    assert mend(texts[2]) == texts[2], "two words of the book stay two, even when they make a third"
    assert Mend([]).broken == set() and Mend([""])("") == ""
    assert Mend(["четы ре", "четыре"]).broken == {("четы", "ре")}
    twice = Mend(["четы ре года", "четы ре дня", "четыре"])
    assert twice("четы ре года, четы ре дня") == "четыре года, четыре дня", "the same break again"
    seen = Mend(["четы ре", "четыре", "ре"])
    assert seen("четы ре") == "четы ре", "a piece seen on its own may be a word"


def test_a_word_hyphenated_across_blocks_and_pages_is_one_word():
    pages = [["Первый абзац о ре\xad", "шении задачи.", "Он пере\xad"], ["носит слово на другую страницу."]]
    (chapter,) = from_pages(pages, [])
    assert chapter["paragraphs"] == ["Первый абзац о решении задачи.", "Он переносит слово на другую страницу."]


def test_no_space_inside_brackets():
    assert spaces("( вместо  послесловия )") == "(вместо послесловия)"
    assert spaces("Б ( как в  начале)") == "Б (как в начале)"
    assert spaces("") == "" and spaces("()") == "()"


# Telling a format by what is in the file


@pytest.fixture
def samples(chekhov_pdf, chekhov_djvu, chekhov_epub, chekhov_scan, chekhov_djvu_scan):
    return {"pdf": [chekhov_pdf, chekhov_scan], "djvu": [chekhov_djvu, chekhov_djvu_scan], "epub": [chekhov_epub]}


def test_every_format_is_told_by_its_signature(samples):
    assert sorted(samples) == sorted(k.name for k in formats.KINDS)
    for name, paths in samples.items():
        for path in paths:
            found = formats.detect(path)
            assert found is not None and found.name == name, path


def test_extension_does_not_make_a_format(tmp_path, samples):
    text = tmp_path / "Книга.pdf"
    text.write_text("Это просто текст, а не PDF.\n" * 10)
    assert formats.detect(text) is None
    with pytest.raises(ValueError, match="unsupported format"):
        formats.read(text, tmp_path, 2)
    renamed = tmp_path / "book.epub"
    renamed.write_bytes(samples["pdf"][0].read_bytes())
    assert formats.read(renamed, tmp_path, 2).chapters


def test_pdf_header_after_some_junk(tmp_path, chekhov_pdf):
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"\x00junk before the header\r\n" * 20 + chekhov_pdf.read_bytes())
    found = formats.detect(junk)
    assert found is not None and found.name == "pdf"
    check_stories(formats.read(junk, tmp_path, 2).chapters)
    late = tmp_path / "late.pdf"
    late.write_bytes(b"x" * 1024 + chekhov_pdf.read_bytes())
    assert formats.detect(late) is None, "the header is looked for in the first kilobyte only"


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


def test_zip_that_is_not_an_epub(tmp_path):
    plain = tmp_path / "book.epub"
    with zipfile.ZipFile(plain, "w") as z:
        z.writestr("mimetype", "application/zip")
        z.writestr("text.txt", "Просто архив.")
    assert formats.detect(plain) is None
    broken = tmp_path / "broken.epub"
    broken.write_bytes(b"PK\x03\x04" + bytes(100))
    assert formats.detect(broken) is None


def test_extensions_come_from_the_list():
    assert formats.extensions() == (".pdf", ".djvu", ".epub")


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


def test_made_up_format_goes_all_the_way_to_m4b(tmp_path, tale):
    assert ".tale" in formats.extensions()
    book = new_book(tmp_path, tale)
    result = pipeline.run(book, context(tmp_path))
    info = probe(result)
    assert [c["tags"]["title"] for c in info["chapters"]] == TITLES
    assert info["format"]["tags"]["title"] == "Сказки"
    assert book.path("book.epub").exists(), "a reading copy is built for it"


BOOK = os.environ.get("AUDIOBUX_TEST_BOOK", "")


@pytest.mark.skipif(not Path(BOOK).is_file(), reason="AUDIOBUX_TEST_BOOK is not set")
def test_real_book(tmp_path):
    chapters = formats.read(Path(BOOK), tmp_path, 2).chapters
    assert len(chapters) >= 10
    assert all(c["paragraphs"] for c in chapters)
    for p in (p for c in chapters for p in c["paragraphs"]):
        assert not re.search(r"^\d+\s*\||\|\s*\d+$", p), p
