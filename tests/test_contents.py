from pathlib import Path

import pymupdf
import pytest

from audiobux import formats, reading
from audiobux.formats.contents import Cases
from audiobux.formats.pdf import reading_order

BODY = (
    "Мы жили тогда у самого моря, и каждый вечер сосед рассказывал нам о том, как устроены машины. "
    "Он говорил, что лента, которую придумал Мёбиус, имеет одну сторону, а курс на ИКР ведёт к решению. "
    "Мы слушали его и спорили до темноты, потому что задача казалась нам совсем простой. "
)
SHIFT = 3  # page index = printed number + SHIFT

# (level, title in the contents, title in the text); a part takes a page of its own
BOOK = [
    (1, "С ЧЕГО НАЧИНАЕТСЯ ТЕОРИЯ", "С чего начинается теория"),
    (2, "«ОТО НЕВОЗМОЖНО...»", "«ЭТО НЕВОЗМОЖНО...»"),
    (2, "ЛЕНТА, КОТОРУЮ ПРИДУМАЛ МЕБИУС", "ЛЕНТА, КОТОРУЮ ПРИДУМАЛ МЁБИУС"),
    (2, "ПОПРОБУЙТЕ СВОИ СИЛЫ", "ПОПРОБУЙТЕ СВОИ СИЛЫ"),
    (1, "НАУКА ИЗОБРЕТАТЬ", "Наука изобретать"),
    (2, "КАК РЕШАТЬ ЗАДАЧИ, | которых еще нет", "КАК РЕШАТЬ ЗАДАЧИ, | КОТОРЫХ ЕЩЕ НЕТ"),
    (2, "КУРС - НА ИКР", "КУРС - НА ИКР"),
    (2, "ПОПРОБУЙТЕ СВОИ СИЛЫ", "ПОПРОБУЙТЕ СВОИ СИЛЫ"),
]
SHOWN = [
    "С чего начинается теория",
    "«Это невозможно»",
    "Лента, которую придумал Мёбиус",
    "Попробуйте свои силы",
    "Наука изобретать",
    "Как решать задачи, которых еще нет",
    "Курс - на ИКР",
    "Попробуйте свои силы",
]


def page_of(doc: pymupdf.Document, lines: list[tuple[str, float]]) -> None:
    """A page with each line a block of its own, as a typeset page has them."""
    page = doc.new_page(width=420, height=595)
    y = 40.0
    for text, size in lines:
        spare, _ = page.insert_htmlbox(pymupdf.Rect(40, y, 380, 560), f'<p style="font-size:{size}pt">{text}</p>')
        y = 560 - spare + 4


def body(words: int = 1) -> list[tuple[str, float]]:
    return [(BODY, 10)] * words


def layout() -> tuple[list[list[tuple[str, float]]], list[int]]:
    """The text pages of the book after the front matter, and the index of each entry
    among them."""
    pages: list[list[tuple[str, float]]] = []
    starts = []
    for level, _, title in BOOK:
        starts.append(len(pages))
        heading = [(t, 24 if level == 1 else 10) for t in title.split(" | ")]
        if level == 1:
            pages.append(heading)
        else:
            pages += [heading + body(), body(3)]
    return pages, starts


def altov(path: Path, numbers: bool = True, bookmarks: bool = False) -> Path:
    """A book like a scanned Soviet one: a title page, the imprint, two pages of
    contents in which OCR mixed up letters, the text, the imprint again."""
    doc = pymupdf.open()
    page_of(doc, [("Г. Альтов", 18), ("И ТУТ ПОЯВИЛСЯ", 29), ("ИЗОБРЕТАТЕЛЬ", 29)])
    page_of(doc, [("ББК 30у", 8), ("Научный консультант Н. П. Линькова", 8), ("ISBN 5-08-000123-4", 8)])
    text, starts = layout()
    front = 4  # title, imprint, two pages of contents
    lines: list[tuple[str, float]] = [("С О Д Е Р Ж А Н И Е", 13)]
    for (level, title, _), start in zip(BOOK, starts, strict=True):
        parts = title.split(" | ")
        lines += [(p, 10 if level == 1 else 8) for p in parts]
        if level == 2 and numbers:
            lines.append((str(front + start - SHIFT), 10))
    page_of(doc, lines[:10])
    page_of(doc, [*lines[10:], ("3", 10)])
    for page in text:
        page_of(doc, page)
    page_of(doc, [("Подписано в печать 16.12.86. Тираж 100 000 экз. Заказ № 1358.", 8)])
    if bookmarks:
        doc.set_toc(
            [
                [level, f"Закладка {i}", front + start + 1]
                for i, ((level, _, _), start) in enumerate(zip(BOOK, starts, strict=True))
            ]
        )
    doc.save(path)
    return path


def check_text(book):
    """No page of the front matter is said; every page of the text is."""
    said = " ".join(p for c in book.chapters for p in c["paragraphs"])
    for junk in ("С О Д Е Р Ж", "ISBN", "ББК", "Тираж", "ПОЯВИЛСЯ"):
        assert junk not in said
    assert said.count("Мы жили тогда") == 4 * sum(1 for level, _, _ in BOOK if level == 2)


def test_printed_contents_give_the_chapters(tmp_path):
    book = formats.read(altov(tmp_path / "book.pdf"), tmp_path, 2)
    _, starts = layout()
    assert [(level, page) for level, _, page in book.outline] == [
        (e[0], 4 + s) for e, s in zip(BOOK, starts, strict=True)
    ]
    assert [c["title"] for c in book.chapters] == SHOWN
    first = book.chapters[1]["paragraphs"]
    assert first[0] == "«ЭТО НЕВОЗМОЖНО...»" and first[1].startswith("Мы жили тогда у самого моря")
    assert book.chapters[0]["paragraphs"] == ["С чего начинается теория"]
    check_text(book)


def test_chapters_to_the_depth(tmp_path):
    book = formats.read(altov(tmp_path / "book.pdf"), tmp_path, 1)
    assert [c["title"] for c in book.chapters] == ["С чего начинается теория", "Наука изобретать"]
    check_text(book)


def test_contents_without_page_numbers(tmp_path):
    book = formats.read(altov(tmp_path / "book.pdf", numbers=False), tmp_path, 2)
    _, starts = layout()
    assert [page for _, _, page in book.outline] == [4 + s for s in starts]
    assert [c["title"] for c in book.chapters] == SHOWN
    check_text(book)


def test_bookmarks_come_first(tmp_path):
    book = formats.read(altov(tmp_path / "book.pdf", bookmarks=True), tmp_path, 2)
    assert [c["title"] for c in book.chapters] == [f"Закладка {i}" for i in range(len(BOOK))]
    check_text(book)


def test_headings_by_type(tmp_path):
    """No contents: a chapter opens a page in large type; a section in a size found
    inside pages too starts no chapter, nor does a running header."""
    doc = pymupdf.open()
    page_of(
        doc,
        [
            ("ГЛАВА 5", 14),
            ("Реализация", 21),
            ("простой бизнес-логики", 21),
            *body(2),
            ("Транзакционный сценарий", 16),
            *body(),
        ],
    )
    page_of(doc, [("Глава 5. Реализация простой бизнес-логики | 91", 8), ("Реализация", 16), *body(3)])
    page_of(doc, [("92 | Часть II. Тактический замысел", 8), *body(3)])
    page_of(doc, [("Глава 6", 14), ("Сложная бизнес-логика", 21), *body(3)])
    doc.save(tmp_path / "ddd.pdf")
    book = formats.read(tmp_path / "ddd.pdf", tmp_path, 2)
    assert [c["title"] for c in book.chapters] == [
        "Глава 5. Реализация простой бизнес-логики",
        "Глава 6. Сложная бизнес-логика",
    ]
    first = book.chapters[0]["paragraphs"]
    assert "Транзакционный сценарий" in first and "Реализация" in first
    assert not any("|" in p for c in book.chapters for p in c["paragraphs"])


def test_parts_and_chapters_by_type(tmp_path):
    """No contents: parts in large type over chapters in capitals of the body size."""
    text, _ = layout()
    doc = pymupdf.open()
    for page in text:
        page_of(doc, page)
    doc.save(tmp_path / "book.pdf")
    book = formats.read(tmp_path / "book.pdf", tmp_path, 2)
    assert [level for level, _, _ in book.outline] == [e[0] for e in BOOK]
    assert [c["title"] for c in book.chapters] == SHOWN


def test_no_contents_and_no_headings(tmp_path):
    """One untitled chapter; the title page before the text is left out."""
    doc = pymupdf.open()
    page_of(doc, [("Г. Альтов", 18), ("И тут появился изобретатель", 18)])
    for _ in range(3):
        page_of(doc, body(3))
    doc.save(tmp_path / "plain.pdf")
    book = formats.read(tmp_path / "plain.pdf", tmp_path, 2)
    assert book.outline == []
    assert len(book.chapters) == 1 and book.chapters[0]["title"] == ""
    assert book.chapters[0]["paragraphs"][0].startswith("Мы жили")


def test_columns_are_read_left_first():
    r = pymupdf.Rect
    items = [
        (r(180, 50, 300, 60), "heading"),
        (r(250, 80, 440, 300), "right top"),
        (r(40, 81, 235, 400), "left"),
        (r(250, 300, 440, 400), "right bottom"),
        (r(40, 420, 440, 500), "wide"),
        (r(40, 510, 200, 520), "short line"),
        (r(250, 530, 440, 540), "signature"),
    ]
    assert reading_order(items, 480) == [
        "heading",
        "left",
        "right top",
        "right bottom",
        "wide",
        "short line",
        "signature",
    ]


@pytest.mark.parametrize(
    ("printed", "shown"),
    [
        ("«ЭТО НЕВОЗМОЖНО...»", "«Это невозможно»"),
        ("ЗАДАЧИ, ЗАДАЧИ, ЗАДАЧИ...", "Задачи, задачи, задачи"),
        ("ЛЕНТА, КОТОРУЮ ПРИДУМАЛ МЁБИУС", "Лента, которую придумал Мёбиус"),
        ("КУРС - НА ИКР", "Курс - на ИКР"),
        ("ЧАСТЬ II", "Часть II"),
        ("ГЛАВА 5. Реализация бизнес-логики", "Глава 5. Реализация бизнес-логики"),
        ("ШАГ ( ВМЕСТО ПОСЛЕСЛОВИЯ )", "Шаг (Вместо послесловия)"),
        ("Введение", "Введение"),
        ("", ""),
    ],
)
def test_titles_in_the_usual_case(printed, shown):
    assert Cases([BODY]).shown(printed) == shown


def test_the_file_name_is_no_chapter_title():
    untitled = {"chapters": [{"title": ""}, {"title": ""}]}
    assert reading.titles(untitled | {"title": "scan_0042"}, "scan_0042.pdf") == ["", ""]
    assert reading.titles(untitled | {"title": "Изучаем DDD"}, "scan_0042.pdf") == ["Изучаем DDD", ""]
    assert (
        reading.titles(untitled | {"title": "Хононов - Изучаем DDD"}, "Хононов - Изучаем DDD.pdf")[0] == "Изучаем DDD"
    )


def test_the_cleaned_file_name_is_no_chapter_title():
    untitled = {"chapters": [{"title": ""}, {"title": ""}]}
    for title, name in [
        ("scan 0042", "scan_0042.pdf"),
        ("Книга", "Книга_2019_12MB.pdf"),
        ("Книга_2019_12MB", "Книга_2019_12MB.pdf"),
        ("", "Книга (2019, 600M).djvu"),
        ("2019", "2019.pdf"),
    ]:
        assert reading.titles(untitled | {"title": title}, name) == ["", ""], name
    assert reading.titles(untitled | {"title": "Изучаем DDD"}, "Книга_2019_12MB.pdf") == ["Изучаем DDD", ""]
    named = reading.titles(untitled | {"title": "Книга"}, "Хононов_-_Книга_2019.pdf")
    assert named == ["Книга", ""]
