import numpy as np

from audiobux import f5

REF_TEXT = "х" * 150 + ". "
REF_SECONDS = 12.6


def test_short_piece_gets_end_mark_and_room_for_last_word():
    text, duration = f5._fit("Глав+а п+ервая", REF_SECONDS, REF_TEXT)
    assert text == "Глав+а п+ервая."
    speech = REF_SECONDS * len("Глав+а п+ервая".encode()) / len(REF_TEXT.encode()) / f5.INFER["speed"]
    assert duration == REF_SECONDS + speech + f5.PAD


def test_short_piece_keeps_its_own_end_mark():
    assert f5._fit("Н+ет!", REF_SECONDS, REF_TEXT)[0] == "Н+ет!"


def test_long_part_gets_end_mark_and_free_length():
    text = "Он д+олго сто+ял у окн+а, гл+ядя на м+окрые кр+ыши, и д+умал о з+автрашнем дн+е"
    assert f5._fit(text, REF_SECONDS, REF_TEXT) == (text + ".", None)


def test_trim_keeps_speech_and_a_little_air():
    rate = 1000
    wave = np.concatenate([np.zeros(500), np.ones(300) * 0.5, np.zeros(700)]).astype(np.float32)
    out = f5.trim(wave, rate, keep=0.03)
    assert len(out) == 300 + 2 * 30
    assert out[0] == 0 and out[len(out) // 2] == 0.5
