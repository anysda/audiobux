"""A listing from every kind of source: whole in the EPUB and the reader's text, only
its telling in the speech."""

import zipfile
from html import escape

import conftest
import pymupdf
import pytest
from ebooklib import epub as ebook
from test_pipeline import context, new_book

from audiobux import cleanup, pipeline
from audiobux.formats import pages

# no mark of code in it: only the source can tell it is a listing
CODE = "Given a customer with an open order\n  When the order is paid\n    Then a receipt is sent"
TOLD = "В листинге сценарий оплаты заказа."
TEXT = "Сценарий описывает оплату заказа. Он записан так, как его читает заказчик."
AFTER = "После оплаты заказ закрыт и больше не меняется."


class Teller:
    running = False

    def ask(self, system, user, json_reply=False):
        return TOLD if "Листинг:" in user else user

    def map(self, fn, items):
        return [fn(x) for x in items]

    def close(self):
        pass


def text_pdf(path, code=CODE):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_htmlbox(pymupdf.Rect(60, 60, 540, 120), f"<p>{TEXT}</p>")
    page.insert_htmlbox(pymupdf.Rect(60, 130, 540, 230), f"<pre>{escape(code)}</pre>")
    page.insert_htmlbox(pymupdf.Rect(60, 240, 540, 300), f"<p>{AFTER}</p>")
    doc.save(path)
    return path


def source_epub(path):
    book = ebook.EpubBook()
    book.set_identifier("listing")
    book.set_title("Книга")
    book.set_language("ru")
    book.add_author("Автор")
    lines = escape(CODE).split("\n")
    body = f"<p>{TEXT}</p><pre><code>{lines[0]}<br/>{chr(10).join(lines[1:])}</code></pre><p>{AFTER}</p>"
    item = ebook.EpubHtml(title="Глава", file_name="one.xhtml", lang="ru", content=f"<h1>Глава</h1>{body}")
    book.add_item(item)
    book.toc = [ebook.Link("one.xhtml", "Глава", "one")]
    book.spine = [item]
    book.add_item(ebook.EpubNcx())
    book.add_item(ebook.EpubNav())
    ebook.write_epub(str(path), book)
    return path


def scan_pdf(path):
    doc = pymupdf.open()
    doc.new_page()
    doc.save(path)
    return path


class Reader:
    """OCR that reads the listing off the page line by line."""

    def page_text(self, image):
        return f"{TEXT}\n\n[Листинг]\n```\n{CODE}\n```\n[/Листинг]\n\n{AFTER}"


def voiced(tmp_path, source, ocr=None):
    ctx = context(tmp_path)
    ctx.models.editor = cleanup.Editor(Teller())
    ctx.models.ocr = ocr
    book = new_book(tmp_path, source)
    pipeline.run(book, ctx)
    return book


def check(book, code=CODE, epub=True):
    (listing,) = [b for ch in book.read("sync.json")["chapters"] for b in ch["blocks"] if b["type"] == "code"]
    assert listing["text"] == code and listing["t0"] is not None, "shown whole and lit while its telling is heard"
    if epub:
        with zipfile.ZipFile(book.path("book.epub")) as z:
            text = "".join(z.read(n).decode() for n in z.namelist() if n.endswith("html"))
        assert f"<pre><code>{escape(code, quote=False)}</code></pre>" in text
    said = " ".join(p["text"] for p in book.read("chunks.json"))
    assert TOLD in said and "receipt" not in said and "Given" not in said
    assert "Сценарий описывает оплату" in said and "После оплаты" in said


def test_a_pdf_listing_is_told_by_its_monospaced_type(tmp_path):
    check(voiced(tmp_path, text_pdf(tmp_path / "Автор - Книга.pdf")))


def test_an_epub_pre_is_a_listing_with_its_lines(tmp_path):
    # the source is the reading copy: the reader's text and the speech are checked
    check(voiced(tmp_path, source_epub(tmp_path / "Автор - Книга.epub")), epub=False)


def test_a_scan_listing_is_read_off_the_page_and_told(tmp_path):
    check(voiced(tmp_path, scan_pdf(tmp_path / "Автор - Скан.pdf"), Reader()))


def test_a_djvu_text_layer_listing_is_told(tmp_path):
    if not conftest.shutil.which("djvused"):
        pytest.skip("djvulibre is not installed")
    code = "public class Order {\n    void Pay() { paid = true; }\n}"
    pdf = text_pdf(tmp_path / "raw.pdf", code)
    check(voiced(tmp_path, conftest._djvu(tmp_path, pdf, text=True)), code, epub=True)


def test_ocr_blocks_keep_a_listing_whole():
    reply = "Текст.\n\n[Листинг]\n# comment\n\n  x = 1\n[/Листинг]\n# Глава 2\n\n[Листинг]\nopen(\n"
    assert pipeline.blocks(reply) == ["Текст.", "[[code]] # comment\n\n  x = 1", "# Глава 2", "[[code]] open("]
    assert pipeline.blocks("[Листинг]\n \n[/Листинг]") == pipeline.blocks("[Листинг]") == []


def test_a_listing_is_not_glued_to_the_text_around_it():
    chapters = pages.from_pages([["Текст без точки", "[[code]] run()\n  stop()", "дальше текст."]], [])
    assert chapters[0]["paragraphs"] == ["Текст без точки", "[[code]] run()\n  stop()", "дальше текст."]


def test_a_marked_block_is_a_listing_whatever_it_reads_like():
    (fixed,) = cleanup.by_rules([["Абзац.", "[[code]] Привет, мир", "[[code]] пока", "Ещё абзац."]])
    assert fixed == {"paragraphs": ["Абзац.", "", "Ещё абзац."], "listings": {"1": "Привет, мир\n\nпока"}}
