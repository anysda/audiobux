import pytest
from test_text import FakeLlm

from audiobux import speech
from audiobux.speech import Speech

SAID = [
    # caps read as plain words, capital where a sentence starts
    ("ЭТО НЕВОЗМОЖНО", "Это невозможно"),
    ("«ЭТО НЕВОЗМОЖНО...»", "Это невозможно..."),
    ("Он крикнул: «ЭТО НЕВОЗМОЖНО!»", "Он крикнул: это невозможно!"),
    ("ОН ПРИШЁЛ ДОМОЙ. БЫЛО ПОЗДНО, НО СВЕТ ГОРЕЛ.", "Он пришёл домой. Было поздно, но свет горел."),
    ("Это была ЁЛКА.", "Это была ёлка."),
    ("это ОЧЕНЬ важно", "это очень важно"),
    ("КТО-ТО ПРИШЁЛ. ПОТОМ ЕЩЁ", "Кто-то пришёл. Потом ещё"),
    ("ХИТРОСТЬ И ФИЗИКА", "Хитрость и физика"),
    ("Г. Альтов И ТУТ появился", "Г. Альтов и тут появился"),
    ("Н. П. ЛИНЬКОВА", "Н. П. Линькова"),
    ("Задача 7. ОХ, УЖ ЭТИ СЫЩИКИ...", "Задача семь. Ох, уж эти сыщики..."),
    # abbreviations and Roman numbers stay for the rules to read
    ("ВСЁ ДЛЯ СССР И США", "Всё для эс эс эс эр И США"),
    ("ТРИЗ, ЭВМ и ДНК", "ТРИЗ, ЭВМ и дэ эн ка"),
    ("Людовик XIV пришёл", "Людовик четырнадцатый пришёл"),
    ("Протокол HTTP и NASA", "Протокол эйч ти ти пи и наса"),
    ("INTERNATIONAL BUSINESS", "интернатионал бусинесс"),
    # signs of a formula said as words
    ("2 + 2 = 4", "два плюс два равно четыре"),
    ("a + b = c", "а плюс б равно к"),
    ("x−1", "кс минус один"),
    ("x-1", "кс минус один"),
    ("x = -5", "кс равно минус пять"),
    ("−5 + 3 = −2", "минус пять плюс три равно минус два"),
    ("3 × 4", "три умножить на четыре"),
    ("3*4", "три умножить на четыре"),
    ("8 ÷ 2", "восемь разделить на два"),
    ("a < b", "а меньше б"),
    ("π ≈ 3,14", "пи приблизительно равно три целых и четырнадцать сотых"),
    ("ЛОДКА+ЛОДКА = ?", "Лодка плюс лодка равно ?"),
    ("«Мешок + воздух» — механизм", "Мешок плюс воздух — механизм"),
    ("+5 °C", "плюс пять градусов цельсия"),
    # a sign with nothing to join is not read
    ("+ пункт списка", "пункт списка"),
    ("Схема: + вход, = выход", "Схема: вход, выход"),
    # what is no formula reads as before
    ("-5 °C", "минус пять градусов цельсия"),
    ("−5 °C", "минус пять градусов цельсия"),
    ("1941-1945", "тысяча девятьсот сорок один-тысяча девятьсот сорок пять"),
    ("5–10 минут", "пять десять минут"),
    ("10-15%", "десять-пятнадцать процентов"),
    ("кто-то из Ростов-на-Дону", "кто-то из Ростов-на-Дону"),
    ("Он ушёл, — сказал он.", "Он ушёл — сказал он."),
    ("+7 999 123-45-67", "плюс семь девятьсот девяносто девять сто двадцать три сорок пять шестьдесят семь"),
    ("12.05.1941", "двенадцать точка ноль пять точка тысяча девятьсот сорок один"),
    ("Т-34 и Ту-154", "Т-тридцать четыре и Ту-сто пятьдесят четыре"),
    ("", ""),
]


@pytest.mark.parametrize(("text", "said"), SAID)
def test_text_as_the_voice_gets_it(tmp_path, text, said):
    assert Speech(accent="").prepare([[text]], tmp_path / "latin.txt") == [[said]]


def test_abbreviation_the_book_keeps_in_caps_stays_in_a_caps_heading(tmp_path):
    prose = [
        "Сформулируем ИКР задачи.",
        "Но ИКР недостижим.",
        "Отступим от ИКР.",
        "Он НЕ пришёл, не позвонил, не написал.",
    ]
    out = Speech(accent="").prepare([["КУРС НА ИКР", "ОН НЕ ПРИШЁЛ", *prose]], tmp_path / "latin.txt")
    assert out[0][:3] == ["Курс на ИКР", "Он не пришёл", "Сформулируем ИКР задачи."]
    assert out[0][-1] == "Он не пришёл, не позвонил, не написал.", "a word stressed in caps is no abbreviation"


def test_one_caps_word_is_emphasis_not_abbreviation():
    assert speech._abbreviations([["Под ЁЛКОЙ лежит ИКР."]]) == frozenset()
    assert speech._abbreviations([["ИКР один.", "ИКР два.", "ИКР три."]]) == {"ИКР"}
    assert speech._abbreviations([]) == frozenset()


def test_shouted_latin_is_looked_up_as_said(tmp_path):
    path = tmp_path / "latin.txt"
    llm = FakeLlm(latin={"International": "интерн+эшнл", "NASA": "н+аса"})
    assert Speech(llm, accent="").prepare([["INTERNATIONAL и NASA."]], path) == [["интерн+эшнл и н+аса."]]
    assert speech.read_dictionary(path) == {"International": "интерн+эшнл", "NASA": "н+аса"}


def test_reading_text_matches_what_was_said():
    """Sync compares the reader's text with the voice through `plain`: the case and the
    signs must not stand between them."""
    assert speech.plain("ЛОДКА + ЛОДКА = ?") == speech.plain("Лодка плюс лодка равно?") == "лодка плюс лодка равно"
    assert speech.plain("2 + 2 = 4") == "два плюс два равно четыре"
    assert speech.plain("ЭТО НЕВОЗМОЖНО") == speech.plain("это невозможно")


def test_stress_mark_is_no_plus():
    assert speech._signs("Это з+амок, +это н+ужно.") == "Это з+амок, +это н+ужно."
    assert speech.plain("Это з+амок, эс-кью-+эль.") == "это замок эс кью эль"


def test_rules_are_settled_on_their_own_output():
    for text, _ in SAID:
        once = speech._expand(text)
        assert speech._expand(once) == once


def test_long_caps_paragraph():
    shout = "ЭТО НЕВОЗМОЖНО. " * 200
    assert speech._expand(shout) == ("Это невозможно. " * 200).strip()
