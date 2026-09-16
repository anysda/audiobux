import json
import shutil

from test_pipeline import CountingTts, context, new_book

from audiobux import cleanup, pipeline, speech
from audiobux.speech import Speech
from audiobux.takes import Heard, words


class FakeLlm:
    """Answers from a table; counts what it was asked."""

    def __init__(self, replies=None, latin=None):
        self.replies, self.latin, self.asked = replies or {}, latin or {}, []
        self.running = False

    def ask(self, system, user, json_reply=False):
        self.running = True
        self.asked.append(user)
        if json_reply:
            return json.dumps({w: self.latin[w] for w in user.split("\n") if w in self.latin})
        return self.replies.get(user, user)

    def map(self, fn, items):
        return [fn(x) for x in items]

    def close(self):
        self.running = False


class Stresses:
    def process_all(self, text, skip_regex=None):
        return text.replace("замок", "з+амок").replace("(", " (").replace("«", "")


def test_rules_mend_layout_leftovers():
    chapters = [
        [
            "Подход Domain- Driven Design описан в книге Эванса году1. Изменчи- вость велика.",
            "Рис. 1.2. Схема поддоменов",
            "Бизнес-логика и бизнес-правила, а также бизнесценности компании и",
            "продолжение абзаца после разрыва страницы (источник: https://example.com/a).",
            "2 Evans, E. (2003). Domain-Driven Design. Retrieved from https://dddcommunity.org",
            "Подробности на сайте https://www.dddcommunity.org/library/evans.",
        ]
    ]
    assert cleanup.by_rules(chapters) == [
        {
            "paragraphs": [
                "Подход Domain-Driven Design описан в книге Эванса году. Изменчивость велика.",
                "Бизнес-логика и бизнес-правила, а также бизнес-ценности компании и "
                "продолжение абзаца после разрыва страницы.",
                "Подробности на сайте dddcommunity.org.",
            ],
            "listings": {},
        }
    ]


def test_model_fix_is_kept_only_when_close():
    good = "Модель предметной области помогает команде понимать бизнес друг друга."
    bad = "Здесь модель написала совсем другой текст про погоду."
    llm = FakeLlm({good + "!": good, bad: "Совершенно иное содержание, ничего общего с исходным абзацем."})
    editor = cleanup.Editor(llm, max_change=0.1)
    assert editor.fix([[good + "!", bad]]) == [{"paragraphs": [good, bad], "listings": {}}]


def test_clean_text_layer_asks_the_model_only_about_broken_paragraphs():
    clean = "Модель предметной области помогает команде понимать бизнес."
    broken = "Модель предметнои облаcти помогает."  # Latin c, a wrong letter
    llm = FakeLlm()
    cleanup.Editor(llm).fix([[clean, broken]], everything=False)
    assert llm.asked == [broken]
    llm.asked.clear()
    cleanup.Editor(llm).fix([[clean, broken]])
    assert llm.asked == [clean, broken]


def test_suspicious_paragraphs():
    assert not cleanup.suspicious("Поддомены бывают основными, вспомогательными и типовыми.")
    assert cleanup.suspicious("Поддомены бывают оcновными.")
    assert cleanup.suspicious("Т а б л и ц а сравнения поддоменов.")
    assert cleanup.suspicious("Поддомены быва- ют основными.")
    assert cleanup.suspicious("Поддомены бывают ¤ основными.")
    assert cleanup.suspicious("Поддомены бывают кракозябрными.")


def test_code_listing_is_told_not_read():
    before = "Агрегат проверяет инварианты перед сохранением:"
    code = ["public class Order {\n    void Add(Item item) { items.Add(item); }", "}"]
    after = "Так агрегат защищает свои данные."
    told = "В листинге класс заказа добавляет товар в свой список."
    kept = {"1": "public class Order {\n    void Add(Item item) { items.Add(item); }\n\n}"}

    class Teller(FakeLlm):
        def ask(self, system, user, json_reply=False):
            self.asked.append(user)
            return told if "Листинг:" in user else user

    llm = Teller()
    fixed = cleanup.Editor(llm).fix([[before, *code, after]], everything=False)
    assert fixed == [{"paragraphs": [before, told, after], "listings": kept}]
    listing = [a for a in llm.asked if "Листинг:" in a]
    # the model sees the listing flat, a line per block, as before the code was kept
    assert listing == [
        f"Абзац перед листингом: {before}\nЛистинг:\n"
        "public class Order { void Add(Item item) { items.Add(item); }\n}"
    ]

    llm.asked.clear()
    told = "-"
    assert cleanup.Editor(llm).fix([[before, *code, after]], everything=False) == [
        {"paragraphs": [before, "", after], "listings": kept}
    ]
    assert cleanup.by_rules([[before, *code, after]]) == [{"paragraphs": [before, "", after], "listings": kept}]


def test_numbers_by_model_checked_against_rules(tmp_path):
    ok = "В 2003 году вышла книга."
    wrong = "Глава 5 о моделях."
    garbled = "К 2020 году."
    llm = FakeLlm(
        {
            ok: "В две тысячи третьем году вышла книга.",
            wrong: "Пятая глава о моделях и ещё много добавленных моделью слов.",
            garbled: "К двум тысячому двадцатьому году.",
        }
    )
    s = Speech(llm, accent="")
    out = s.prepare(
        [[ok, wrong, garbled, "Версия 3.11, т. е. новая.", "Схема на рис. 2, данные в табл. 3."]],
        tmp_path / "latin.txt",
    )
    assert out == [
        [
            "В две тысячи третьем году вышла книга.",
            "Глава пять о моделях.",
            "К две тысячи двадцатом году.",
            "Версия три точка одиннадцать, то есть новая.",
            "Схема на рисунке два, данные в таблице три.",
        ]
    ]


def test_latin_dictionary_is_written_once_and_editable(tmp_path):
    path = tmp_path / "latin.txt"
    llm = FakeLlm(latin={"DDD": "ди-ди-д+и", "Java": "дж+ава", "Kafka": "к+афка"})
    s = Speech(llm, accent="")
    assert s.prepare([["Пишем DDD на Java."]], path) == [["Пишем ди-ди-д+и на дж+ава."]]
    assert speech.read_dictionary(path) == {"DDD": "ди-ди-д+и", "Java": "дж+ава"}

    path.write_text(path.read_text().replace("дж+ава", "дж+ава-маш+ина"))
    llm.asked.clear()
    assert s.prepare([["Пишем DDD на Java и Kafka."]], path) == [["Пишем ди-ди-д+и на дж+ава-маш+ина и к+афка."]]
    assert llm.asked == ["Kafka"], "only new words go to the model"


def test_stress_marks_and_brackets_read_as_commas(tmp_path):
    text = "Это замок (старый), «большой»."
    s = Speech(accent="turbo3.1")
    s._accentizer = Stresses()
    assert s.prepare([[text]], tmp_path / "latin.txt") == [["Это з+амок, старый, большой."]]
    s.stress = False
    assert s.prepare([[text]], tmp_path / "latin.txt") == [["Это замок, старый, большой."]]


def test_stress_mark_kept_where_accentizer_puts_back_yo():
    assert (
        speech._stressed("Нужно четкое, твердое «да».", "Н+ужно ч+ёткое, тв+ёрдое д+а.")
        == "Н+ужно ч+ёткое, тв+ёрдое «д+а»."
    )


def test_cer_ignores_stress_case_digits_and_latin():
    latin = {"DDD": "ди-ди-д+и"}
    assert pipeline.cer("Вс+ё о ди-ди-д+и, три гл+авы.", "Все о DDD 3 главы", latin) == 0
    assert pipeline.cer("один два три", "один два") > 0.15


def heard(text):
    """Whisper's words of a clean take: each as long as its letters."""
    out, t = [], 0.0
    for w in words(text):
        out.append((w, t, t + 0.07 * len(w)))
        t += 0.07 * len(w) + 0.05
    return Heard(text, out)


class Ears:
    """Hears the text of a piece only from the given take on."""

    def __init__(self, texts, good_from_seed):
        self.texts, self.good_from, self.calls = texts, good_from_seed, 0

    def listen(self, wav):
        self.calls += 1
        return Heard("") if self.calls < self.good_from else heard(self.texts[0])

    def listen_many(self, wavs):
        return [self.listen(w) for w in wavs]


def test_bad_take_is_voiced_again_then_reported(tmp_path, chekhov_epub):
    tts = CountingTts()
    ctx = context(tmp_path, tts)
    book = new_book(tmp_path, chekhov_epub)
    pipeline.run(book, ctx, until="tts")
    pieces = book.read("chunks.json")
    first = book.read("chunks.json")[:1]
    book.write("chunks.json", first)

    tts.calls = 0
    ctx.models.listener = Ears([first[0]["text"]], good_from_seed=2)
    pipeline.verify(book, ctx)
    assert tts.calls == 1 and book.read("verify.json") == []

    assert book.read("ready/00000.json")["seed"] == 1
    calls = tts.calls
    pipeline.verify(book, ctx)
    assert tts.calls == calls, "a checked piece is not voiced again after a restart"

    shutil.rmtree(book.path("ready"))
    for take in book.path("tts").glob("*.s*.wav"):
        take.unlink()
    tts.calls = 0
    ctx.models.listener = Ears([first[0]["text"]], good_from_seed=99)
    pipeline.verify(book, ctx)
    report = book.read("verify.json")
    assert tts.calls == ctx.settings.verify_attempts - 1
    assert [r["piece"] for r in report] == [0] and report[0]["cer"] == 1.0
    assert len(pieces) > 1


class QuietHoursOnce:
    """Quiet hours at the first look, then never again; remembers whether the model was running then."""

    def __init__(self, llm):
        self.llm, self.left, self.model_running = llm, 60, None

    def check(self):
        return self.left

    def wait(self):
        self.model_running, self.left = self.llm.running, 0


def test_language_model_lives_only_inside_its_step(tmp_path, chekhov_epub):
    ctx = context(tmp_path, CountingTts())
    book = new_book(tmp_path, chekhov_epub)
    pipeline.run(book, ctx, until="figures")
    llm = FakeLlm()
    ctx.models.llm, ctx.models.editor, ctx.models.speech = llm, cleanup.Editor(llm), Speech(llm, accent="")

    pipeline.fix(book, ctx)
    assert llm.asked and not llm.running

    ctx.quiet = QuietHoursOnce(llm)
    llm.running = True
    pipeline.normalize(book, ctx)
    assert ctx.quiet.model_running is False, "stopped before quiet hours"
    assert not llm.running


class Stutterer(Ears):
    """Hears a repeated syllable in the first take, then the text."""

    def listen(self, wav):
        self.calls += 1
        h = heard(self.texts[0])
        if self.calls == 1:
            w, t0, _ = h.words[1]
            h.words.insert(1, (w[:1], t0 - 0.05, t0))
        return h


def test_stutter_is_voiced_again(tmp_path, chekhov_epub):
    tts = CountingTts()
    ctx = context(tmp_path, tts)
    book = new_book(tmp_path, chekhov_epub)
    pipeline.run(book, ctx, until="tts")
    first = book.read("chunks.json")[:1]
    book.write("chunks.json", first)
    tts.calls = 0
    ctx.models.listener = Stutterer([first[0]["text"]], good_from_seed=0)
    pipeline.verify(book, ctx)
    assert tts.calls == 1 and book.read("verify.json") == []
    assert book.read("ready/00000.json")["seed"] == 1


def test_reader_pauses_marked_only_when_words_stay():
    text = "Когда он пришёл дом+ой, было уже поздно и все спали."
    llm = FakeLlm({text.replace("+", ""): "Когда он пришёл домой, | было уже поздно и все спали. |"})
    assert Speech(llm, accent="")._marked(text) == "Когда он пришёл дом+ой, | было уже поздно и все спали."
    llm = FakeLlm({text.replace("+", ""): "Когда он вернулся домой, | было уже поздно и все спали."})
    assert Speech(llm, accent="")._marked(text) == text
    short = "Глава первая."
    llm = FakeLlm()
    assert Speech(llm, accent="")._marked(short) == short and not llm.asked
