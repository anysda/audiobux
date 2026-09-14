"""sync.json and listings, on a small book voiced by beeps and timed by a fake aligner."""

from audiobux import reading, sync


def test_a_word_the_aligner_missed_gets_the_gap_around_it():
    times = sync.word_times(["раз", "второе", "три"], [[0.0, 1.0], None, [3.0, 4.0]], 5.0)
    assert times == [(0.0, 1.0), (1.0, 3.0), (3.0, 4.0)]
    # nothing aligned: the whole take by letters
    assert sync.word_times(["аа", "б"], None, 3.0) == [(0.0, 2.0), (2.0, 3.0)]


def test_sentences_keep_initials_and_quotes_together():
    text = "Об этом писал В. Хононов. «Так ли это?» Да! 2 главы спустя."
    assert sync.sentences(text) == ["Об этом писал В. Хононов.", "«Так ли это?»", "Да!", "2 главы спустя."]


def test_a_voiced_chapter_heading_is_its_first_paragraph(tmp_path):
    data = {"title": "К", "chapters": [{"title": "Глава 1", "paragraphs": ["ГЛАВА 1", "Текст."], "listings": {}}]}
    (chapter,) = reading.chapters(data, [], tmp_path, "К.pdf")
    assert [(b.kind, b.text, b.said) for b in chapter.blocks] == [("h", "Глава 1", 0), ("p", "Текст.", 1)]
