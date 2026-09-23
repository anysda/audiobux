"""sync.json and listings, on a small book voiced by beeps and timed by a fake aligner."""

import zipfile

import pymupdf
import pytest
import soundfile as sf
from test_pipeline import context, new_book, probe

from audiobux import cleanup, pipeline, reading, sync, web
from audiobux.config import Settings
from audiobux.takes import Word

CODE = "public class Order {\n    void Add(Item item) { items.Add(item); }\n}"
TOLD = "В листинге класс заказа добавляет товар в свой список."
SAID = "На рисунке 1.1 схема заказа и его товаров, связанных между собой."
LONG = (
    "Агрегат хранит все товары заказа, проверяет, что сумма не превышает лимит покупателя, "
    "что каждый товар есть на складе, что скидки не складываются сверх разрешённого, и только "
    "после этого сохраняет заказ целиком, одной транзакцией, чтобы никто не увидел его наполовину "
    "записанным и не принял решение по неполным данным о покупке."
)
CHAPTERS = [
    {
        "title": "Глава 1",
        "paragraphs": [
            "Глава 1",
            "Заказ собирает товары. Второе предложение чуть длиннее первого.",
            CODE,
            "Так агрегат защищает свои данные.",
            "[[figure 0]]",
            "Да.",
        ],
    },
    {"title": "Глава 2", "paragraphs": ["Глава 2", LONG, "Конец второй главы."]},
    {"title": "Глава 3", "paragraphs": ["int x = 1; return x;"]},
]


class Teller:
    """Tells the first listing, finds the second one not worth a word, leaves the text."""

    running = False

    def ask(self, system, user, json_reply=False):
        if "Листинг:" in user:
            return TOLD if "Order" in user else "-"
        return user

    def map(self, fn, items):
        return [fn(x) for x in items]

    def close(self):
        pass


class Describer:
    def describe(self, found, folder, wait):
        return [SAID]


class Aligner:
    """Words one after another over the take; it is not sure of `второе`."""

    def align(self, wave, rate, text_words):
        step = len(wave) / rate / len(text_words)
        return [
            Word(w, k * step + 0.1 * step, (k + 0.9) * step, 0.0 if w == "второе" else 0.9)
            for k, w in enumerate(text_words)
        ]


@pytest.fixture(scope="module")
def voiced(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("sync")
    source = tmp_path / "Автор - Книга.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    book = new_book(tmp_path, source)
    (book.dir / "figures").mkdir()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 200), 1)
    pix.clear_with(200)
    pix.save(book.path("figures/0001.png"))
    book.write(
        "text.json",
        {
            "title": "Книга",
            "author": "Автор",
            "outline": [],
            "chapters": CHAPTERS,
            "figures": [{"image": "figures/0001.png", "caption": "Рис. 1.1. Заказ"}],
        },
    )
    book.mark("extract")
    book.mark("ocr")
    ctx = context(tmp_path)
    llm = Teller()
    ctx.models.editor, ctx.models.figures, ctx.models.aligner = cleanup.Editor(llm), Describer(), Aligner()
    pipeline.run(book, ctx)
    return book


def spans(data):
    """Every time of the sync in order, chapters apart."""
    for chapter in data["chapters"]:
        for block in chapter["blocks"]:
            for item in block.get("sentences", [block]):
                yield item["t0"], item["t1"]


def test_schema(voiced):
    data = voiced.read("sync.json")
    assert set(data) == {"version", "duration", "chapters"} and data["version"] == 1
    assert [c["title"] for c in data["chapters"]] == ["Глава 1", "Глава 2", "Глава 3"]
    assert all(list(c) == ["title", "t0", "t1", "blocks"] for c in data["chapters"])
    first = data["chapters"][0]["blocks"]
    assert [b["type"] for b in first] == ["h", "p", "code", "p", "figure", "p"]
    assert set(first[0]) == {"type", "text", "t0", "t1"} and first[0]["text"] == "Глава 1"
    assert [s["text"] for s in first[1]["sentences"]] == [
        "Заказ собирает товары.",
        "Второе предложение чуть длиннее первого.",
    ]
    assert first[2] == first[2] | {"text": CODE} and first[2]["t0"] is not None
    figure = first[4]
    assert set(figure) == {"type", "src", "alt", "t0", "t1"} and figure["alt"] == SAID
    assert figure["src"] == "figures/0001.jpg" and voiced.path(figure["src"]).read_bytes()[:2] == b"\xff\xd8"
    assert web.FIGURE.fullmatch(figure["src"].removeprefix("figures/"))
    # the listing nobody tells: the only block of chapter 3, and no time
    last = data["chapters"][2]
    assert last["blocks"] == [{"type": "h", "text": "Глава 3", "t0": None, "t1": None}] + [
        {"type": "code", "text": "int x = 1; return x;", "t0": None, "t1": None}
    ]


def test_every_heard_block_has_a_time_and_only_those_have_none(voiced):
    data = voiced.read("sync.json")
    for chapter in data["chapters"][:2]:
        for block in chapter["blocks"]:
            for item in block.get("sentences", [block]):
                assert item["t0"] is not None and item["t1"] is not None, item


def test_times_go_forward_and_stay_inside(voiced):
    data = voiced.read("sync.json")
    now = 0.0
    for t0, t1 in spans(data):
        if t0 is None:
            continue
        assert now <= t0 <= t1 <= data["duration"]
        now = t1
    starts = [c["t0"] for c in data["chapters"]]
    assert starts == sorted(starts) and all(c["t0"] <= c["t1"] <= data["duration"] for c in data["chapters"])
    assert abs(data["duration"] - float(probe(voiced.path("book.m4b"))["format"]["duration"])) < 0.2


def test_chapters_start_where_the_m4b_has_them(voiced):
    data = voiced.read("sync.json")
    marks = probe(voiced.path("book.m4b"))["chapters"]
    assert [m["tags"]["title"] for m in marks] == ["Глава 1", "Глава 2"]
    second = data["chapters"][1]
    assert second["t0"] > 0 and abs(second["t0"] - float(marks[1]["start_time"])) < 0.002
    assert data["chapters"][0]["t1"] == second["t0"]
    # its heading is said at the very start of it
    assert abs(second["blocks"][0]["t0"] - second["t0"]) < 0.5
    # a chapter nobody hears is a point at the end
    assert data["chapters"][2]["t0"] == data["chapters"][2]["t1"] == data["duration"]


def test_listing_is_code_in_the_epub_and_only_its_telling_is_said(voiced):
    with zipfile.ZipFile(voiced.path("book.epub")) as z:
        text = "".join(z.read(n).decode() for n in z.namelist() if n.endswith(".xhtml"))
    assert "<pre><code>public class Order {\n    void Add(Item item) { items.Add(item); }\n}</code></pre>" in text
    assert "int x = 1; return x;" in text and TOLD not in text
    said = " ".join(p["text"] for p in voiced.read("chunks.json"))
    assert "листинге класс заказа" in said
    assert not any(mark in said for mark in ("{", "}", "Order", "public", "return"))


def test_a_sentence_of_one_word(voiced):
    pieces = voiced.read("chunks.json")
    (i,) = [i for i, p in enumerate(pieces) if p["chapter"] == 0 and p["paragraph"] == 5]
    start = voiced.read("chapters.json")["pieces"][i]
    length = sf.info(voiced.path(f"ready/{i:05}.wav")).duration
    (one,) = voiced.read("sync.json")["chapters"][0]["blocks"][5]["sentences"]
    # the aligner put the only word from 10 % to 90 % of the take
    assert one["text"] == "Да."
    assert one["t0"] == pytest.approx(start + 0.1 * length, abs=0.002)
    assert one["t1"] == pytest.approx(start + 0.9 * length, abs=0.002)


def test_a_sentence_cut_between_pieces(voiced):
    pieces = voiced.read("chunks.json")
    starts = voiced.read("chapters.json")["pieces"]
    cut = [i for i, p in enumerate(pieces) if p["chapter"] == 1 and p["paragraph"] == 1]
    assert len(cut) >= 2, "the long sentence is voiced in two pieces or more"
    sentence = voiced.read("sync.json")["chapters"][1]["blocks"][1]["sentences"]
    assert [s["text"] for s in sentence] == [LONG]
    assert starts[cut[0]] <= sentence[0]["t0"] < starts[cut[1]] < sentence[0]["t1"] < starts[cut[-1] + 1]


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


def test_a_figure_gets_a_name_the_web_serves(tmp_path):
    source = tmp_path / "Книга.epub"
    source.write_bytes(b"")
    book = new_book(tmp_path, source)
    (book.dir / "figures").mkdir()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 30, 20), 1)
    pix.clear_with(200)
    pix.save(book.path("figures/Рис 1.2 Заказ.png"))
    src = sync._picture(book, {"image": "figures/Рис 1.2 Заказ.png"})
    assert src and web.FIGURE.fullmatch(src.removeprefix("figures/")) and book.path(src).exists()


def test_the_cli_copies_the_sync_and_its_figures_out(tmp_path, monkeypatch, voiced):
    from audiobux import __main__ as cli

    def run(book, ctx, until):
        # the voiced book's results, as if this run had made them
        for name in ("book.epub", "book.m4b", "sync.json", "figures/0001.jpg", "figures/0001.png"):
            book.path(name).parent.mkdir(exist_ok=True)
            book.path(name).write_bytes(voiced.path(name).read_bytes())
        return book.path("book.m4b")

    monkeypatch.setattr(pipeline, "run", run)
    monkeypatch.setattr(cli, "context", lambda settings: None)
    source = tmp_path / "Книга.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    out = tmp_path / "out"
    cli.voice_one(Settings(data=tmp_path / "data"), source, None, out)
    assert {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()} == {
        "Книга.epub",
        "Книга.m4b",
        "Книга/sync.json",
        "Книга/figures/0001.jpg",
    }
    assert (out / "Книга/sync.json").read_bytes() == voiced.path("sync.json").read_bytes()


def test_m4b_and_sync_name_an_untitled_chapter_alike(tmp_path):
    source = tmp_path / "Автор - Книга.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    book = new_book(tmp_path, source)
    untitled = [
        {"title": "", "paragraphs": ["Предисловие без заголовка, просто текст."]},
        {"title": "Глава 2", "paragraphs": ["Глава 2", "Конец второй главы."]},
        {"title": "", "paragraphs": ["Ещё одна глава без заголовка."]},
    ]
    book.write("text.json", {"title": "Книга", "author": "Автор", "outline": [], "chapters": untitled, "figures": []})
    book.mark("extract")
    book.mark("ocr")
    ctx = context(tmp_path)
    ctx.models.editor, ctx.models.aligner = cleanup.Editor(Teller()), Aligner()
    m4b = pipeline.run(book, ctx)
    in_m4b = [c["tags"].get("title", "") for c in probe(m4b)["chapters"]]
    in_sync = [c["title"] for c in book.read("sync.json")["chapters"]]
    assert in_m4b == in_sync == [c["title"] for c in book.read("chapters.json")["chapters"]]
    assert in_sync == ["Книга", "Глава 2", ""]
