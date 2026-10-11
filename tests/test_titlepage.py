import json
import shutil

import pymupdf
import pytest

from audiobux import cover, models, pipeline, titlepage
from audiobux.book import Book
from audiobux.config import Settings
from audiobux.formats.document import Document
from audiobux.quiet import QuietHours
from audiobux.titlepage import find, from_name, printed, shown
from audiobux.worker import about

ALTOV = "И_тут_появился_изобретатель_Г_С_Альтов_1987_600M.pdf"
TITLE_PAGE = "Г. Альтов\nИ ТУТ появился\nИЗОБРЕТАТЕЛЬ"
IMPRINT = "Альтов Г. С.\nИ тут появился изобретатель: Научно-популяр\xadная книга. Дет. лит., 1987."


class Model:
    """Answers with what it is given and remembers what it was asked."""

    def __init__(self, reply="", fails=False):
        self.reply, self.fails, self.asked, self.closed = reply, fails, [], 0

    def ask(self, system, user, json_reply=False, image=None):
        self.asked.append(user)
        if self.fails:
            raise RuntimeError("llama-server exited with 1")
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply, ensure_ascii=False)

    def close(self):
        self.closed += 1


def pdf(path, pages, title="", author=""):
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_htmlbox(page.rect + (40, 40, -40, -40), "".join(f"<p>{line}</p>" for line in text.splitlines()))
    doc.set_metadata({"title": title, "author": author})
    doc.save(path)
    return path


@pytest.fixture
def altov(tmp_path):
    body = [f"Глава {n}. Задача про изобретателя, который думает над ней долго и упорно." for n in range(1, 9)]
    return pdf(tmp_path / ALTOV, [TITLE_PAGE, "", *body, IMPRINT, ""])


def test_metadata_come_first_and_the_model_is_not_asked(altov):
    model = Model({"title": "И тут появился изобретатель", "author": "Г. Альтов"})
    doc = Document(title="Изобретатель", author="Генрих Альтов")
    assert find(doc, altov, ALTOV, model, lambda: None) == ("Изобретатель", "Генрих Альтов")
    assert model.asked == []


def test_title_page_comes_before_the_file_name(altov):
    waited = []
    model = Model({"title": "И тут появился изобретатель", "author": "Г. Альтов"})
    model.ask = lambda *a, ask=model.ask, **k: (waited.append("asked"), ask(*a, **k))[1]
    found = find(Document(), altov, ALTOV, model, lambda: waited.append("wait"))
    assert found == ("И тут появился изобретатель", "Г. Альтов")
    assert waited == ["wait", "asked"], "the quiet hours are kept before the card is used"
    assert "ИЗОБРЕТАТЕЛЬ" in model.asked[0] and "Альтов Г. С." in model.asked[0], "the first and the last pages"


def test_each_field_comes_from_where_it_is_found(altov):
    """The metadata have the title only: the author is read on the title page."""
    model = Model({"title": "Совсем другое", "author": "Г. Альтов"})
    assert find(Document(title="Изобретатель"), altov, ALTOV, model, lambda: None) == ("Изобретатель", "Г. Альтов")


def test_junk_metadata_are_passed_over(altov):
    model = Model({"title": "И тут появился изобретатель", "author": "Г. Альтов"})
    doc = Document(title="Microsoft Word - altov_final.doc", author="Unknown")
    assert find(doc, altov, ALTOV, model, lambda: None) == ("И тут появился изобретатель", "Г. Альтов")


def test_what_the_model_made_up_is_dropped(altov):
    model = Model({"title": "Как стать изобретателем", "author": "Генрих Альтшуллер"})
    assert find(Document(), altov, ALTOV, model, lambda: None) == ("И тут появился изобретатель Г С Альтов", "")


@pytest.mark.parametrize("reply", ["", "не знаю", "[]", {"title": None, "author": 5}])
def test_a_reply_without_an_answer_leaves_the_file_name(altov, reply):
    assert find(Document(), altov, ALTOV, Model(reply), lambda: None)[0] == "И тут появился изобретатель Г С Альтов"


def test_a_model_that_fails_leaves_the_file_name(altov):
    assert find(Document(), altov, ALTOV, Model(fails=True), lambda: None)[0].startswith("И тут появился")


def test_a_scan_or_no_model_takes_the_file_name(altov):
    model = Model({"title": "И тут появился изобретатель", "author": "Г. Альтов"})
    assert find(Document(pages=["pages/0001.png"]), altov, ALTOV, model, lambda: None)[1] == ""
    assert find(Document(), altov, ALTOV, None, lambda: None)[1] == ""
    assert model.asked == []


def test_a_book_without_text_is_not_sent(tmp_path):
    empty = pdf(tmp_path / "book.pdf", ["", ""])
    model = Model({"title": "Книга", "author": "Автор"})
    assert find(Document(), empty, "book.pdf", model, lambda: None) == ("book", "")
    assert model.asked == []


def test_long_pages_are_cut_and_only_the_ends_are_read(tmp_path):
    long = pdf(tmp_path / "long.pdf", [f"страница{n} " + "слово " * 300 for n in range(12)])
    model = Model()
    find(Document(), long, "long.pdf", model, lambda: None)
    sent = model.asked[0]
    assert [f"страница{n}" in sent for n in range(12)] == [True] * 3 + [False] * 6 + [True] * 3
    assert len(sent) <= 6 * (titlepage.PAGE_CHARS + 2)


def test_djvu_pages_are_read_too(chekhov_djvu):
    pages = titlepage.end_pages(chekhov_djvu)
    assert 0 < len(pages) <= 2 * titlepage.PAGES and all(pages)


@pytest.mark.parametrize(
    "name, expected",
    [
        (ALTOV, ("И тут появился изобретатель Г С Альтов", "")),
        ("Author - Title.pdf", ("Title", "Author")),
        ("title_2019.epub", ("title", "")),
        ("book.fb2", ("book", "")),
        ("Чехов - Рассказы (1987) [12 MB].djvu", ("Рассказы", "Чехов")),
        ("ВОЙНА_И_МИР.pdf", ("Война и мир", "")),
        ("1984.epub", ("1984", "")),
        ("Стругацкие - Пикник - на обочине.pdf", ("Пикник - на обочине", "Стругацкие")),
    ],
)
def test_file_name_is_cleaned(name, expected):
    assert from_name(name) == expected


def test_capitals_of_a_title_page_become_the_usual_case():
    assert shown("И ТУТ появился ИЗОБРЕТАТЕЛЬ") == "И тут появился изобретатель"
    assert shown("Изучаем SQL") == "Изучаем SQL"
    assert shown("") == ""


def test_caps_author_is_written_as_a_name(altov):
    model = Model({"title": "", "author": "Г. АЛЬТОВ"})
    assert find(Document(), altov, ALTOV, model, lambda: None)[1] == "Г. Альтов"


def test_printed_is_lenient_about_case_hyphens_and_spaces():
    assert printed("И тут появился изобретатель", "И ТУТ появился\nИЗОБРЕ-\nТАТЕЛЬ")
    assert printed("Научно-популярная", "Научно-популяр\xadная")
    assert printed("Кондакова", "Ольг и К о н д а к о в а")
    assert not printed("Генрих Альтов", TITLE_PAGE)
    assert not printed("", TITLE_PAGE) and not printed("—", TITLE_PAGE)


def test_who_and_what_cleans_the_file_name():
    assert cover.who_and_what("", "", ALTOV) == ("", "И тут появился изобретатель Г С Альтов")
    assert cover.who_and_what("", "Чехов - Рассказы", "Чехов - Рассказы.pdf") == ("Чехов", "Рассказы")
    assert cover.who_and_what("Г. Альтов", "Изобретатель", ALTOV) == ("Г. Альтов", "Изобретатель")


def test_extract_puts_the_title_page_into_the_text(tmp_path, altov):
    settings = Settings(data=tmp_path / "data", llm="", accent="", asr="", ocr="")
    m = models.load(settings)
    m.llm = model = Model({"title": "И тут появился изобретатель", "author": "Г. Альтов"})
    book = Book.create(tmp_path / "work", ALTOV, lambda p: shutil.copyfile(altov, p))
    pipeline.extract_text(book, pipeline.Context(settings, m, QuietHours("")))
    text = book.read("text.json")
    assert (text["title"], text["author"]) == ("И тут появился изобретатель", "Г. Альтов")
    assert about(book) == {"title": "И тут появился изобретатель", "author": "Г. Альтов"}
    assert model.closed, "the card is free again for the next step"
