from datetime import datetime

import pytest
from conftest import stories

from audiobux import chunk
from audiobux.quiet import QuietHours


def seconds(text):
    return len(text.replace("|", "")) / 15


def test_pieces_keep_whole_sentences_and_all_text():
    chapters = [{"title": t, "paragraphs": p} for t, p in stories()]
    pieces = chunk.chunks(chapters, seconds, 10.0)
    assert all(seconds(p["text"]) <= 10.0 or " " not in p["text"] for p in pieces)
    for ch, (_, paragraphs) in enumerate(stories()):
        mine = [p["text"] for p in pieces if p["chapter"] == ch]
        assert " ".join(mine).split() == " ".join(paragraphs).split()
    assert sum(p["pause"] == chunk.PAUSE_CHAPTER for p in pieces) == 3


def test_sentences_packed_while_they_fit():
    text = "Первое предложение. Второе предложение! «Третье», — сказал он. Четвёртое?"
    parts = chunk.split(text, seconds, 3.0)
    assert [p for p, _, _ in parts] == ["Первое предложение. Второе предложение!", "«Третье», — сказал он. Четвёртое?"]
    assert parts[0][1] == [[1, "stop"]] and parts[0][2] == "stop"


def test_long_sentence_cut_where_the_reader_pauses():
    marked = "Он долго стоял у окна, глядя на мокрые крыши | и думая о том, что будет завтра утром."
    parts = chunk.split(marked, seconds, 4.0)
    assert [p for p, _, _ in parts] == [
        "Он долго стоял у окна, глядя на мокрые крыши",
        "и думая о том, что будет завтра утром.",
    ]
    assert parts[0][2] == "syn"
    plain = marked.replace(" |", "")
    parts = chunk.split(plain, seconds, 4.0)
    assert parts[0][0] == "Он долго стоял у окна," and parts[0][2] == "comma"
    assert " ".join(p for p, _, _ in parts) == plain


def test_marked_comma_wins_near_the_middle():
    text = "Раз два три четыре пять, | шесть семь | восемь девять десять одиннадцать."
    parts = chunk.split(text, seconds, 3.5)
    assert parts[0][0] == "Раз два три четыре пять,"
    assert all(seconds(p) <= 3.5 for p, _, _ in parts)


def test_bounds_inside_a_piece_keep_their_kind():
    parts = chunk.split("Он пришёл, | сел. Тихо.", seconds, 10.0)
    assert parts == [("Он пришёл, сел. Тихо.", [[1, "comma"], [2, "stop"]], "stop")]


def at(hour, minute=0, second=0):
    return datetime(2026, 11, 2, hour, minute, second)


def test_quiet_hours_through_midnight_run_by_the_server_clock():
    night = QuietHours("00:00-08:00")
    night.clock = lambda: at(0, 0)
    assert night.seconds_left() == 8 * 3600, "the start is inside"
    night.clock = lambda: at(23, 59, 59)
    assert night.seconds_left() == 0
    night.clock = lambda: at(7, 59, 30)
    assert night.seconds_left() == 30
    night.clock = lambda: at(8, 0)
    assert night.seconds_left() == 0, "the end is outside"


def test_quiet_hours_across_midnight():
    late = QuietHours("22:00-06:00")
    assert late.seconds_left(at(21, 59, 59)) == 0
    assert late.seconds_left(at(22, 0)) == 8 * 3600
    assert late.seconds_left(at(23, 0)) == 7 * 3600
    assert late.seconds_left(at(0, 0)) == 6 * 3600
    assert late.seconds_left(at(5, 59)) == 60
    assert late.seconds_left(at(6, 0)) == 0
    assert late.seconds_left(at(12, 0)) == 0


def test_empty_quiet_hours_mean_round_the_clock():
    for spec in ("", "  "):
        quiet = QuietHours(spec)
        quiet.clock = lambda: at(3, 0)
        assert quiet.seconds_left() == 0
        assert quiet.check() == 0


def test_a_wrong_window_is_told_plainly():
    with pytest.raises(ValueError, match="HH:MM-HH:MM"):
        QuietHours("ночью")


@pytest.mark.parametrize("spec", ["22-06", "22:00", "24:00-06:00", "22:60-06:00", "08:00-08:00", "8:00-9:00"])
def test_quiet_hours_take_only_two_different_times(spec):
    with pytest.raises(ValueError):
        QuietHours(spec)
