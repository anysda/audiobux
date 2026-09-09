import json

from audiobux import cleanup


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
