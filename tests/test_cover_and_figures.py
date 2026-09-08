import pymupdf
from conftest import cover_jpeg

from audiobux import figures, models
from audiobux.formats import pdf
from audiobux.text import MARK, mark


def test_first_page_of_text_is_not_a_cover(tmp_path):
    doc = pymupdf.open()
    text = doc.new_page()
    text.insert_textbox(text.rect + (50, 50, -50, -50), "Глава первая. " * 60, fontname="cour")
    picture = doc.new_page()
    picture.insert_image(picture.rect, stream=cover_jpeg())
    assert not pdf.looks_like_cover(doc[0])
    assert pdf.looks_like_cover(doc[1])


def test_pdf_figure_becomes_a_mark_with_its_picture_and_caption(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 80), "Text before the figure.")
    shape = page.new_shape()
    for k in range(4):
        shape.draw_rect(pymupdf.Rect(100 + 90 * k, 150, 170 + 90 * k, 220))
        shape.draw_line((170 + 90 * k, 185), (190 + 90 * k, 185))
    shape.finish(color=(0, 0, 0))
    shape.commit()
    page.insert_text((140, 190), "Box")
    page.insert_text((72, 260), "Figure 1.1. Four boxes")
    page.insert_text((72, 300), "Text after the figure.")
    found = []
    blocks = pdf.page_blocks(page, 0, set(), tmp_path, found)
    marks = [b for b in blocks if MARK.match(b)]
    assert marks == [mark(0)]
    assert not any("Box" in b for b in blocks), "labels inside the figure go with it"
    assert blocks.index(marks[0]) == 1
    assert found[0]["caption"].startswith("Figure 1.1") and (tmp_path / found[0]["image"]).exists()


def test_descriptions_replace_marks_and_decorations_vanish():
    chapters = [{"title": "Глава", "paragraphs": ["Перед рисунком.", mark(0), "Между.", mark(1)]}]
    found = [{}, {}]
    figures.near(chapters, found)
    assert found[0]["near"] == "Перед рисунком." and found[1]["near"] == "Между."
    told = [figures.spoken("**На рисунке 1.1** схема: три поддомена связаны с ядром"), figures.spoken("-")]
    assert told == ["На рисунке 1.1 схема: три поддомена связаны с ядром.", ""]
    assert figures.put(chapters, told)[0]["paragraphs"] == ["Перед рисунком.", told[0], "Между."]


def test_figures_are_shown_to_the_vision_model(tmp_path):
    (tmp_path / "f.png").write_bytes(b"png")

    class Eyes:
        def __init__(self):
            self.seen = []

        def ask(self, system, user, image=None):
            self.seen.append((user, image))
            return "На рисунке 1.1 схема, на которой три поддомена связаны с ядром системы"

        def map(self, fn, items):
            return [fn(x) for x in items]

    eyes = Eyes()
    told = figures.Figures(eyes).describe(
        [{"image": "f.png", "caption": "Рис. 1.1. Поддомены"}, {"text": "a | b"}], tmp_path
    )
    assert told[0].startswith("На рисунке 1.1") and len(told) == 2
    assert eyes.seen[0][1] == b"png" and "Рис. 1.1" in eyes.seen[0][0]
    assert "a | b" in eyes.seen[1][0] and eyes.seen[1][1] is None


def test_synth_many_keeps_the_order_of_pieces():
    tts = models.ToneTts()
    texts = ["Один.", "Второй кусок подлиннее.", "Три."]
    assert [len(s) for s, _ in tts.synth_many(texts)] == [len(tts.synth(t)[0]) for t in texts]
