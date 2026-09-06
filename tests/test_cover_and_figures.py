import pymupdf
from conftest import cover_jpeg

from audiobux import models
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


def test_synth_many_keeps_the_order_of_pieces():
    tts = models.ToneTts()
    texts = ["Один.", "Второй кусок подлиннее.", "Три."]
    assert [len(s) for s, _ in tts.synth_many(texts)] == [len(tts.synth(t)[0]) for t in texts]
