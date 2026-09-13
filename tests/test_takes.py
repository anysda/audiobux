import numpy as np

from audiobux import takes
from audiobux.takes import PAUSE, Word

RATE = 24000


def recording(parts):
    """[(seconds, sounding)] into a wave: a tone where it sounds, zeros elsewhere."""
    out = []
    for seconds, sounding in parts:
        n = int(seconds * RATE)
        out.append(0.3 * np.sin(2 * np.pi * 200 * np.arange(n) / RATE) if sounding else np.zeros(n))
    return np.concatenate(out).astype(np.float32)


# раз [0.30 s pause] два-с-закрытием-внутри [0.30 s pause] три (unsure)
WAVE = recording([(0.5, True), (0.3, False), (0.3, True), (0.1, False), (0.4, True), (0.3, False), (0.5, True)])
ALIGNED = [Word("раз", 0.0, 0.5, 0.9), Word("два", 0.8, 1.6, 0.9), Word("три", 1.9, 2.4, 0.1)]


def lengths(wave):
    return sorted(round(e - s, 2) for s, e in takes.silences(wave, RATE))


def test_pause_set_at_a_boundary_closure_inside_a_word_untouched():
    out, info = takes.retime(WAVE, RATE, ALIGNED, {0: "comma"})
    assert info == {"bounds": 1, "set": 1, "squeezed": 0, "kept_in_word": 1, "unsure": 1}
    assert lengths(WAVE) == [0.1, 0.3, 0.3]
    assert lengths(out) == [0.1, 0.3, PAUSE["comma"]]
    assert abs(len(out) / RATE - (len(WAVE) / RATE + PAUSE["comma"] - 0.3)) < 0.01


def test_gap_without_a_boundary_squeezed():
    out, info = takes.retime(WAVE, RATE, ALIGNED, {})
    assert info["squeezed"] == 1 and info["set"] == 0
    assert lengths(out) == [takes.SQUEEZE, 0.1, 0.3]


def test_word_broken_by_a_long_silence_found():
    assert takes.broken(WAVE, RATE, ALIGNED) == [("два", 1.1, 0.1)]


def spelled(word, start, end, gap, at):
    """A word whose first `at` letters fill start..gap[0] and the rest gap[1]..end."""

    def spread(part, a, b):
        step = (b - a) / len(part)
        return [(c, a + k * step, a + (k + 1) * step) for k, c in enumerate(part)]

    return Word(word, start, end, 0.9, spread(word[:at], start, gap[0]) + spread(word[at:], gap[1], end))


def test_closure_before_a_stop_is_not_a_break():
    # ве-с[тишина 120 мс]-ти: the closure of т, the speaker's own reading has them up to 150 ms
    wave = recording([(0.5, True), (0.3, False), (0.3, True), (0.12, False), (0.4, True), (0.5, True)])
    word = spelled("вести", 0.8, 1.62, (1.1, 1.22), 3)
    assert [c for c, a, b in word.letters if b <= 1.1][-1] == "с"
    assert takes.broken(wave, RATE, [Word("раз", 0.0, 0.5, 0.9), word]) == []


def test_silence_between_vowels_or_a_long_closure_is_a_break():
    wave = recording([(0.5, True), (0.3, False), (0.3, True), (0.1, False), (0.4, True), (0.5, True)])
    vowels = spelled("моаин", 0.8, 1.6, (1.1, 1.2), 2)
    assert takes.broken(wave, RATE, [Word("раз", 0.0, 0.5, 0.9), vowels]) == [("моаин", 1.1, 0.1)]
    long = recording([(0.5, True), (0.3, False), (0.3, True), (0.2, False), (0.4, True), (0.5, True)])
    word = spelled("вести", 0.8, 1.7, (1.1, 1.3), 3)
    assert takes.broken(long, RATE, [Word("раз", 0.0, 0.5, 0.9), word]) == [("вести", 1.1, 0.2)]
    short = recording([(0.5, True), (0.3, False), (0.3, True), (0.04, False), (0.4, True), (0.5, True)])
    assert takes.broken(short, RATE, [Word("раз", 0.0, 0.5, 0.9), Word("два", 0.8, 1.54, 0.9)]) == []


TEXT = takes.words("Это совсем не так, и вот почему он ушёл.")


def clean(ws, stretch=None):
    out, t = [], 0.0
    for w in ws:
        d = 0.07 * len(w) if w != stretch else 1.5
        out.append((w, t, t + d))
        t += d + 0.05
    return out


def test_clean_take_has_no_stutter():
    assert takes.stutters(TEXT, clean(TEXT)) == (0, [])


def test_repeated_syllable_is_a_stutter():
    count, why = takes.stutters(TEXT, clean(["это", "с", *TEXT[1:]]))
    assert count == 1 and "«с»" in why[0]


def test_syllable_glued_to_a_word_is_a_stutter():
    count, why = takes.stutters(TEXT, clean(["это", "сосовсем", *TEXT[2:]]))
    assert count == 1 and "сосовсем" in why[0]


def test_stretched_word_is_a_stutter_but_the_last_one_may_ring():
    count, why = takes.stutters(TEXT, clean(TEXT, stretch="так"))
    assert count == 1 and "так" in why[0]
    assert takes.stutters(TEXT, clean(TEXT, stretch="ушел"))[0] == 0


def test_compound_split_by_whisper_and_a_digit_are_not_stutters():
    text = takes.words("Задачи сложны и труднореализуемы, но их один ряд.")
    heard = clean(["задачи", "сложны", "и", "трудно", "реализуемы", "но", "их", "1", "ряд"])
    heard[7] = ("1", heard[7][1], heard[7][1] + 0.7)
    assert takes.stutters(text, heard) == (0, [])


def test_misheard_word_is_not_a_stutter():
    assert takes.stutters(TEXT, clean(["это", "совсем", "не", "там", *TEXT[4:]]))[0] == 0


def test_piece_words_map_bounds_to_words():
    ws, bounds = takes.piece_words("Он пришёл, сел. Тихо-тихо.", [[1, "comma"], [2, "stop"]])
    assert ws == ["он", "пришел", "сел", "тихо", "тихо"]
    assert bounds == {1: "comma", 2: "stop"}
