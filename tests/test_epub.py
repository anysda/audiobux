import zipfile
from pathlib import Path

import lxml.html
import pymupdf
import pytest
from ebooklib import epub as ebook

from audiobux import epub, figures

SAID = "На рисунке 1.1 схема: три блока стоят в ряд и соединены стрелками слева направо."


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
