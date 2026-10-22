import shutil
import zipfile
from pathlib import Path

import lxml.html
import pymupdf
import pytest
from ebooklib import epub as ebook
from test_pipeline import context
from test_worker import make, submit

from audiobux import epub, figures, pipeline
from audiobux.book import Book

SAID = "На рисунке 1.1 схема: три блока стоят в ряд и соединены стрелками слева направо."
CAPTION = "Рис. 1.1. Три блока"
FIRST = ["Глава 1. Начало", "Блоки", "Как они связаны"]
CHAPTERS = [(FIRST[0], []), (FIRST[1], []), ("Глава 2. Дальше", [])]


@pytest.fixture(scope="session")
def blocks_pdf(tmp_path_factory) -> Path:
    """Two chapters, a section, a subsection below the audio chapters and a drawn figure with a caption."""
    doc = pymupdf.open()
    one = doc.new_page()
    text = (
        "Каждый из них отвечает за свою часть работы, и все они разные. Ниже показано, "
        "как они стоят друг за другом и передают работу дальше по цепочке."
    )
    y = 60

    def put(page, html, height=60):
        nonlocal y
        page.insert_htmlbox(pymupdf.Rect(60, y, page.rect.width - 60, y + height), html)
        y += height + 10

    put(one, "<h1>Глава 1. Начало</h1>", 40)
    put(one, f"<p>{text}</p>")
    put(one, "<h2>Блоки</h2>", 30)
    put(one, f"<p>{text}</p>")
    put(one, "<h3>Как они связаны</h3>", 30)
    one.draw_rect(pymupdf.Rect(90, y - 5, 500, y + 85), color=(0, 0, 0))
    for k in range(3):
        one.draw_rect(pymupdf.Rect(100 + 130 * k, y, 200 + 130 * k, y + 80), color=(0, 0, 0), fill=(0.8, 0.8, 0.8))
    for k in range(2):
        one.draw_line(pymupdf.Point(200 + 130 * k, y + 40), pymupdf.Point(230 + 130 * k, y + 40))
    y += 90
    put(one, f"<p>{CAPTION}</p>", 25)
    put(one, f"<p>{text}</p>")
    y = 60
    two = doc.new_page()
    put(two, "<h1>Глава 2. Дальше</h1>", 40)
    put(two, f"<p>{text}</p>")
    put(two, f"<p>{text}</p>")
    doc.set_toc([[1, FIRST[0], 1], [2, FIRST[1], 1], [3, FIRST[2], 1], [1, "Глава 2. Дальше", 2]])
    path = tmp_path_factory.mktemp("blocks") / "Иванов - Блоки.pdf"
    doc.save(path)
    return path


class Teller:
    """A vision model that has one thing to say about every figure."""

    def describe(self, found, folder, wait=lambda: None):
        return [SAID for _ in found]


def new_book(tmp_path, source: Path) -> Book:
    return Book.create(tmp_path / "work", source.name, lambda p: shutil.copyfile(source, p))


def read(path: Path) -> tuple[ebook.EpubBook, dict[str, lxml.html.HtmlElement]]:
    book = ebook.read_epub(str(path))
    docs = {
        i.get_name(): lxml.html.fromstring(i.get_content())
        for i in book.get_items_of_type(9)
        if i.get_name().startswith("text/")
    }
    return book, docs


def nav(path: Path) -> list:
    """The nav as nested [(title, children)]."""
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.endswith("nav.xhtml"))
        root = lxml.html.fromstring(z.read(name))

    def walk(ol):
        return [
            (li.xpath("./a|./span")[0].text_content().strip(), walk(li.find("ol")) if li.find("ol") is not None else [])
            for li in ol.findall("li")
        ]

    return walk(next(n for n in root.iter("nav") if "toc" in n.attrib.values()).find("ol"))


def test_pdf_book_gets_chapters_headings_figure_and_cover(tmp_path, blocks_pdf):
    ctx = context(tmp_path)
    ctx.models.figures = Teller()
    book = new_book(tmp_path, blocks_pdf)
    pipeline.run(book, ctx, until="epub")

    result, docs = read(book.path("book.epub"))
    assert result.get_metadata("DC", "title")[0][0] == "Блоки"
    assert result.get_metadata("DC", "creator")[0][0] == "Иванов"
    assert result.get_metadata("DC", "language")[0][0] == "ru"
    assert nav(book.path("book.epub")) == CHAPTERS, "the chapters of the m4b in one level, no deeper headings"

    assert len(docs) == 3, "the chapters of the m4b"
    chapter = docs["text/0002.xhtml"]
    assert [h.text_content() for h in chapter.iter("h2", "h3")] == FIRST[1:]
    pictures = list(chapter.iter("img"))
    assert len(pictures) == 1
    assert pictures[0].get("alt") == SAID
    assert chapter.find(".//figcaption").text_content() == CAPTION
    assert SAID not in [p.text_content() for p in chapter.iter("p")], "the picture stands for its description"
    picture = result.get_item_with_href(pictures[0].get("src").removeprefix("../"))
    assert pymupdf.Pixmap(picture.get_content()).width > 100
    texts = [e.text_content() for d in docs.values() for e in d.iter("h1", "h2", "h3", "p")]
    for title in [*FIRST, "Глава 2. Дальше"]:
        assert texts.count(title) == 1, f"{title}: the heading is not repeated by the text"

    with zipfile.ZipFile(book.path("book.epub")) as z:
        written = lxml.html.fromstring(z.read("EPUB/text/0002.xhtml"))
    assert [link.get("href") for link in written.iter("link")] == ["../style.css"]
    assert result.get_item_with_href("style.css") is not None
    covers = [i for i in result.get_items() if i.get_name() == "images/cover.jpg"]
    assert covers and covers[0].get_content() == book.path("cover.jpg").read_bytes(), "the cover of the m4b"


def test_epub_source_goes_out_as_it_came(tmp_path, chekhov_epub):
    book = new_book(tmp_path, chekhov_epub)
    pipeline.run(book, context(tmp_path), until="epub")
    assert book.path("book.epub").read_bytes() == chekhov_epub.read_bytes()


def test_text_is_the_fixed_text_not_the_spoken_one(tmp_path):
    data = {
        "title": "Книга",
        "author": "Автор",
        "outline": [],
        "chapters": [{"title": "", "paragraphs": ["В 2024 году было 15 блоков.", "Второй абзац & <знаки>."]}],
    }
    epub.write(data, [], tmp_path, tmp_path / "b.epub", "Автор - Книга.pdf")
    _, docs = read(tmp_path / "b.epub")
    doc = docs["text/0001.xhtml"]
    assert doc.find(".//h1").text_content() == "Книга", "a book without a TOC is one chapter named after it"
    assert [p.text_content() for p in doc.iter("p")] == data["chapters"][0]["paragraphs"]


def test_printed_contents_make_no_entries_of_the_nav(tmp_path):
    lines = ["Глава 1. Начало", "Раздел", "Глава 2. Конец"]
    data = {
        "title": "Книга",
        "author": "Автор",
        "outline": [(1, lines[0], 2), (2, lines[1], 2), (1, lines[2], 3)],
        "chapters": [
            {"title": "", "paragraphs": ["Содержание", *lines]},
            {"title": lines[0], "paragraphs": [lines[0], "Текст.", lines[1], "Ещё текст."]},
            {"title": lines[2], "paragraphs": [lines[2], "Последний текст."]},
        ],
    }
    epub.write(data, [], tmp_path, tmp_path / "b.epub", "Автор - Книга.pdf")
    assert nav(tmp_path / "b.epub") == [("Книга", []), (lines[0], []), (lines[2], [])]


def test_every_chapter_is_in_the_nav_the_first_one_named_like_the_book(tmp_path):
    data = {
        "title": "Книга",
        "author": "Автор",
        "outline": [],
        "chapters": [
            {"title": "", "paragraphs": ["Автор Книга", "Издательство"]},
            {"title": "Начало", "paragraphs": ["Начало", "Текст."]},
            {"title": "Конец", "paragraphs": ["Конец", "Последний текст."]},
        ],
    }
    # a book read again is named after its title, so the title is the file's name
    epub.write(data, [], tmp_path, tmp_path / "b.epub", "Книга.pdf")
    got = nav(tmp_path / "b.epub")
    assert got == [("Книга", []), ("Начало", []), ("Конец", [])]
    assert len(got) == len(data["chapters"]), "one entry for every chapter of the m4b"


def test_a_figure_the_text_lost_is_left_out(tmp_path):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 120), 0)
    pix.clear_with(200)
    (tmp_path / "figures").mkdir()
    pix.save(tmp_path / "figures" / "a.png")
    data = {"title": "Книга", "author": "", "chapters": [{"title": "Глава", "paragraphs": ["Просто текст главы."]}]}
    epub.write(data, [{"image": "figures/a.png", "said": SAID}], tmp_path, tmp_path / "b.epub", "Книга.pdf")
    result, docs = read(tmp_path / "b.epub")
    assert not list(docs["text/0001.xhtml"].iter("img"))
    assert not [i for i in result.get_items() if i.get_name().startswith("images/")]


def scan_page(tmp_path, figure: bool) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 60
    for k in range(18):
        if figure and k == 6:
            y += 10  # a book leaves room above a figure
            page.draw_rect(pymupdf.Rect(120, y, 470, y + 200), color=(0, 0, 0), fill=(0.85, 0.85, 0.85))
            page.draw_rect(pymupdf.Rect(150, y + 40, 260, y + 120), color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
            page.insert_text(pymupdf.Point(200, y + 214), "Низкая     Высокая", fontsize=9)
            y += 260
        page.insert_htmlbox(
            pymupdf.Rect(60, y, 540, y + 16),
            "<p>Строка обычного текста книги, набранная во всю ширину полосы набора.</p>",
        )
        y += 18
    path = tmp_path / ("figure.png" if figure else "text.png")
    page.get_pixmap(dpi=150).save(path)
    return path


def test_a_figure_is_found_on_a_scanned_page(tmp_path):
    boxes = figures.scan_regions(scan_page(tmp_path, True))
    assert len(boxes) == 1
    x0, y0, x1, y1 = (v * 72 / 150 for v in boxes[0])
    assert (
        y0 == pytest.approx(60 + 6 * 18 + 10, abs=4)
        and x0 == pytest.approx(120, abs=4)
        and x1 == pytest.approx(470, abs=4)
    )
    assert y1 >= 60 + 6 * 18 + 10 + 210, "axis labels right under the figure go with it"
    assert figures.scan_regions(scan_page(tmp_path, False)) == []


def test_a_scan_figure_is_cut_only_when_the_counts_agree(tmp_path):
    page = scan_page(tmp_path, True)
    assert figures.cut_scan(page, 0, [SAID, "В таблице ещё что-то."], tmp_path) == []
    assert figures.cut_scan(page, 0, ["В листинге цикл."], tmp_path) == []
    found = figures.cut_scan(page, 0, ["В листинге цикл.", SAID], tmp_path)
    assert [f["said"] for f in found] == [SAID]
    assert pymupdf.Pixmap(str(tmp_path / found[0]["image"])).width > 300


def test_scan_figures_reach_the_epub(tmp_path, blocks_pdf):
    src = pymupdf.open(blocks_pdf)
    scan = pymupdf.open()
    for page in src:
        out = scan.new_page(width=page.rect.width, height=page.rect.height)
        out.insert_image(out.rect, pixmap=page.get_pixmap(dpi=150))
    scan.set_toc(src.get_toc())
    path = tmp_path / "Иванов - Блоки скан.pdf"
    scan.save(path)
    replies = [
        "# Глава 1. Начало\n\nТекст первой главы о блоках.\n\n# Блоки\n\nЕщё текст.\n\n"
        f"# Как они связаны\n\n[Рисунок: {SAID}]\n\nТекст после рисунка.",
        "# Глава 2. Дальше\n\nТекст второй главы.",
    ]

    class Reader:
        def page_text(self, image):
            return replies[int(image.stem) - 1]

    ctx = context(tmp_path)
    ctx.models.ocr = Reader()
    book = new_book(tmp_path, path)
    pipeline.run(book, ctx, until="epub")
    assert [f["said"] for f in book.read("figures.json")] == [SAID]
    _, docs = read(book.path("book.epub"))
    assert [i.get("alt") for d in docs.values() for i in d.iter("img")] == [SAID]
    assert nav(book.path("book.epub")) == CHAPTERS, "the chapters of the m4b in one level, no deeper headings"


def test_epub_goes_next_to_the_m4b(tmp_path, chekhov_epub):
    w = make(tmp_path)
    book_id = submit(w, chekhov_epub)
    w.run(until_idle=True)
    folder = w.settings.library / book_id
    assert (folder / "book.m4b").exists()
    assert (folder / "book.epub").read_bytes() == chekhov_epub.read_bytes()
