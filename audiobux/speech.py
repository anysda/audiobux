"""Text into what a speech model can read without guessing.

Numbers, dates and abbreviations become words: a language model picks the
right case where there are digits, the rule-based rutextnorm checks it and
covers the rest. Latin words are read from the book's own dictionary,
`latin.txt` in the book's directory: the language model writes it once, a person
may correct it by hand, and the next run takes it as it is. Stress marks come
from RUAccent as `+` before the stressed vowel.
"""

import logging
import re
from collections import Counter
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

from rapidfuzz.distance import Levenshtein

from .cleanup import _known, _morph
from .llm import Llm, parse_json
from .takes import words

log = logging.getLogger(__name__)

LATIN = re.compile(r"(?<![\w+])[A-Za-z][A-Za-z0-9]*(?:['’][A-Za-z]+)?#?")
DIGITS = re.compile(r"\d")
DOTTED = re.compile(r"\b(\d+)((?:\.\d+)+)\b")
WORD = re.compile(r"[\w+]+(?:-[\w+]+)*")
# one stress per word at most, `эс-кью-+эль` counts as one word
CYRILLIC_READING = re.compile(r"^[а-яё\-]*\+?[а-яё\-]+(?: [а-яё\-]*\+?[а-яё\-]+)*$")
ROMAN = re.compile(r"X{0,3}(IX|IV|V?I{0,3})")
CAPS = re.compile(r"\b[^\W\d_]+(?:-[^\W\d_]+)*\b")
SENTENCE_START = re.compile(r"(?:^|[.!?…])\W*$")
# a caps word this short that is no ordinary word is an abbreviation: СССР, ТРИЗ, NASA
ABBREVIATION_LETTERS = 5
# ...or a caps word the book keeps writing so among ordinary text
ABBREVIATION_REPEATS = 3
SIGNS = {
    "+": "плюс",
    "-": "минус",
    "−": "минус",
    "=": "равно",
    "×": "умножить на",
    "*": "умножить на",
    "÷": "разделить на",
    "≈": "приблизительно равно",
    "≠": "не равно",
    "<": "меньше",
    ">": "больше",
    "≤": "меньше или равно",
    "≥": "больше или равно",
}
# numbers and one-letter variables joined by signs: `2 + 2 = 4`, `x−1`, `a = -5`
OPERAND = r"\(*[-−+]?(?:\d+(?:[.,]\d+)?|(?<!\w)[A-Za-zα-ωΑ-Ω](?!\w))\)*"
FORMULA = re.compile(rf"{OPERAND}(?:\s*[-−+=×*÷≈≠<>≤≥]\s*{OPERAND})+")
# between words a sign is read only where it cannot be anything else
WORD_SIGN = re.compile(
    r"(?<=[\w)»\"])\s+([+=−×÷≈≠])\s+(?=[\w(«\"?])|(?<=\w)=(?=\w)|(?<=\w)\+(?=[^\W\d_аеёиоуыэюяАЕЁИОУЫЭЮЯ])"
)

ABBREVIATIONS = {
    r"\bна\s+рис\.": "на рисунке",
    r"\bк\s+рис\.": "к рисунку",
    r"\bсм\.\s*рис\.": "смотрите рисунок",
    r"\bрис\.": "рисунок",
    r"\bв\s+табл\.": "в таблице",
    r"\bсм\.\s*табл\.": "смотрите таблицу",
    r"\bтабл\.": "таблица",
    r"\bт\.\s?е\.": "то есть",
    r"\bт\.\s?к\.": "так как",
    r"\bт\.\s?д\.": "так далее",
    r"\bт\.\s?п\.": "тому подобное",
    r"\bи\s+др\.": "и другие",
    r"\bи\s+пр\.": "и прочее",
    r"\bсм\.": "смотрите",
    r"\bнапр\.": "например",
}

NUMBERS_PROMPT = """Текст книги готовят к озвучке. Перепиши абзац так, чтобы все числа,
даты, годы, номера, проценты, римские цифры и сокращения были записаны словами
в том падеже и роде, которых требует фраза. Остальные слова, их порядок и знаки
препинания не меняй, латиницу оставь как есть. Цифр в ответе быть не должно.
Ответь только текстом абзаца."""

LATIN_PROMPT = """Ты помогаешь озвучить русскую книгу. Для каждого слова латиницей напиши,
как его произносит по-русски знающий человек: кириллицей, одним словом или
несколькими через пробел, в каждом слове один знак + перед ударной гласной.
Названия, имена и термины читай так, как их принято говорить в русской речи.
Аббревиатуры, которые читают по буквам, пиши буквами через дефис с одним
ударением на последней. Примеры: Java -> дж+ава, Domain -> дом+ейн,
Microsoft -> майкрос+офт, SQL -> эс-кью-+эль, DDD -> ди-ди-д+и, AWS -> эй-дабл-ю-+эс,
JSON -> дж+ейсон, Kubernetes -> кубер+нетис.
Ответь JSON-объектом {"слово": "произношение"} для всех слов из списка."""


MARK_PROMPT = (
    "Ты опытный диктор аудиокниг. Тебе дают абзац. Верни тот же текст слово в слово, с теми же знаками, "
    "но вставь символ | там, где диктор при чтении вслух делает короткую смысловую паузу внутри предложения "
    "(граница синтагмы: перед придаточным, после вводного оборота, между группой подлежащего и длинным "
    "сказуемым, перед причастным или деепричастным оборотом, в перечислении). Не ставь | там, где паузы "
    "нет, даже если там запятая: например, в коротком однородном перечислении или перед «и» в короткой "
    "фразе. Не ставь | в конце предложения. Синтагма обычно 3-8 слов. Ничего не добавляй, не объясняй, "
    "только текст."
)
# a heading or a short line has no pause inside worth asking about
MARK_MIN_WORDS = 7


class Speech:
    def __init__(
        self,
        llm: Llm | None = None,
        accent: str = "turbo3.1",
        stress: bool = True,
        models: Path = Path("/models"),
        max_change: float = 0.35,
    ) -> None:
        self.llm, self.accent, self.stress = llm, accent, stress
        self.models, self.max_change = Path(models), max_change
        self._accentizer: Any = None

    def prepare(
        self, chapters: list[list[str]], dictionary: Path, wait: Callable[[], None] = lambda: None
    ) -> list[list[str]]:
        abbreviations = _abbreviations(chapters)
        chapters = [[_expand(p, abbreviations) for p in paragraphs] for paragraphs in chapters]
        latin = self.dictionary(chapters, dictionary)
        out = []
        for paragraphs in chapters:
            wait()
            if self.llm:
                paragraphs = self.llm.map(self._numbers, paragraphs)
            out.append([self._read(p, latin) for p in paragraphs])
        return out

    def mark(self, chapters: list[list[str]], wait: Callable[[], None] = lambda: None) -> list[list[str]]:
        """` | ` where a reader pauses inside a sentence, put by the language model into the
        prepared text. The chunk step cuts a long sentence only there, and the pause after
        such a boundary gets a reader's length."""
        if not self.llm:
            return chapters
        out = []
        for paragraphs in chapters:
            wait()
            out.append(self.llm.map(self._marked, paragraphs))
        return out

    def _marked(self, text: str) -> str:
        """The text with the model's bars, or as it was when the model changed any word."""
        tokens = text.split()
        if len(tokens) < MARK_MIN_WORDS:
            return text
        assert self.llm is not None
        reply = re.sub(r"<think>.*?</think>", "", self.llm.ask(MARK_PROMPT, text.replace("+", "")), flags=re.S)
        mine: list[str] = []
        bars: set[int] = set()
        for t in reply.replace("|", " | ").split():
            if t == "|":
                if mine:
                    bars.add(len(mine) - 1)
            else:
                mine.append(t)
        if [words(t) for t in tokens] != [words(t) for t in mine]:
            log.info("marks rejected: %r -> %r", text[:80], reply[:80])
            return text
        bars.discard(len(tokens) - 1)
        return " ".join(t + " |" if i in bars else t for i, t in enumerate(tokens))

    def dictionary(self, chapters: list[list[str]], path: Path) -> dict[str, str]:
        """Latin words of the book and how to say them; asks the model only for new words."""
        known = read_dictionary(path)
        found = {w for ps in chapters for p in ps for w in LATIN.findall(p)}
        # II, IV, XIV: rutextnorm reads them as ordinals in the right case
        new = sorted({w for w in found if len(w) < 2 or not ROMAN.fullmatch(w)} - known.keys())
        if new and self.llm:
            batches = [new[i : i + 40] for i in range(0, len(new), 40)]
            for reply in self.llm.map(self._transcribe, batches):
                known.update(reply)
            write_dictionary(path, known)
        return known

    def _transcribe(self, batch: list[str]) -> dict[str, str]:
        assert self.llm is not None
        reply = parse_json(self.llm.ask(LATIN_PROMPT, "\n".join(batch), json_reply=True))
        return {
            w: r.strip().lower()
            for w, r in reply.items()
            if w in batch and isinstance(r, str) and CYRILLIC_READING.match(r.strip().lower())
        }

    def _numbers(self, text: str) -> str:
        """The model's version where it agrees with the rules up to word endings."""
        if not DIGITS.search(text):
            return text
        assert self.llm is not None
        reply = self.llm.ask(NUMBERS_PROMPT, text)
        reference = _rules(_without_latin(text))
        new_words = set(re.findall(r"[а-яё]+", reply.lower())) - set(re.findall(r"[а-яё]+", text.lower()))
        if (
            DIGITS.search(reply)
            or LATIN.findall(reply) != LATIN.findall(text)
            or not all(map(_known, new_words))
            or Levenshtein.distance(_without_latin(reply), reference) > self.max_change * len(reference)
        ):
            log.info("model reply rejected: %r -> %r", text[:80], reply[:80])
            return text
        return reply

    def _read(self, text: str, latin: dict[str, str]) -> str:
        text = LATIN.sub(lambda m: latin.get(m.group(), m.group()), text)
        text = _rules(text)
        if self.accent:
            text = _stressed(text, self._accentizer_().process_all(text, skip_regex=r"\S*\+\S*"))
        if not self.stress:
            text = text.replace("+", "")
        return _spoken(text)

    def _accentizer_(self) -> Any:
        if self._accentizer is None:
            from ruaccent import RUAccent

            self._accentizer = RUAccent()
            self._accentizer.load(
                omograph_model_size=self.accent, use_dictionary=True, workdir=str(self.models / "ruaccent")
            )
            _feed_token_types(self._accentizer.accent_model)
        return self._accentizer


def _feed_token_types(model: Any) -> None:
    """The current accent model for words outside the dictionary wants token_type_ids,
    and RUAccent's tokenizer does not give them."""
    if "token_type_ids" not in {i.name for i in model.session.get_inputs()}:
        return
    tokenize = model.tokenizer

    def with_types(text: str, **kw: Any) -> Any:
        out = tokenize(text, **kw)
        out.setdefault("token_type_ids", out["input_ids"] * 0)
        return out

    model.tokenizer = with_types


def read_dictionary(path: Path) -> dict[str, str]:
    """`Word = произнош+ение` per line; lines starting with # are comments."""
    if not path.exists():
        return {}
    pairs = (
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    return {w.strip(): r.strip() for w, r in pairs if w.strip() and r.strip()}


def write_dictionary(path: Path, words: dict[str, str]) -> None:
    lines = [f"{w} = {r}" for w, r in sorted(words.items(), key=lambda kv: kv[0].lower())]
    path.write_text("# как читать слова латиницей, можно править руками\n" + "\n".join(lines) + "\n")


def plain(text: str, latin: dict[str, str] | None = None) -> str:
    """Text reduced to bare lowercase words, for comparing what was said with what was meant."""
    text = LATIN.sub(lambda m: (latin or {}).get(m.group(), m.group()), _expand(text))
    text = _rules(text).lower().replace("ё", "е").replace("+", "")
    return " ".join(re.findall(r"\w+", text))


def _expand(text: str, abbreviations: frozenset[str] = frozenset()) -> str:
    for pattern, spoken in ABBREVIATIONS.items():
        text = re.sub(pattern, spoken, text)
    # versions and section numbers: `3.11` -> `3 точка 11`
    text = DOTTED.sub(lambda m: m.group(1) + m.group(2).replace(".", " точка "), text)
    return _signs(_uncapped(text, abbreviations))


def _uncapped(text: str, abbreviations: frozenset[str] = frozenset()) -> str:
    """Caps the voice reads as shouting become plain words, capital only where a sentence
    starts; an abbreviation and a Roman number stay as they are."""
    found = list(CAPS.finditer(text))
    loud = [_shouted(m.group(), abbreviations) for m in found]
    # a one-letter word before a shouted one is part of the shout (`ХИТРОСТЬ И ФИЗИКА`),
    # unless it is an initial: `Н. ЛИНЬКОВА`
    for i in reversed(range(len(found) - 1)):
        word = found[i].group()
        if len(word) == 1 and word.isupper() and loud[i + 1]:
            loud[i] = text[found[i].end()] != "."
    for m, shout in reversed(list(zip(found, loud, strict=True))):
        if shout:
            word = m.group().lower()
            if SENTENCE_START.search(text, 0, m.start()):
                word = word.capitalize()
            text = text[: m.start()] + word + text[m.end() :]
    return text


def _shouted(word: str, abbreviations: frozenset[str]) -> bool:
    return (
        len(word) > 1
        and word.isupper()
        and not ROMAN.fullmatch(word)
        and word not in abbreviations
        and not _abbreviation(word)
    )


@cache
def _abbreviation(word: str) -> bool:
    """Short and no ordinary word by the dictionary: СССР, ТРИЗ, NASA."""
    if len(word.replace("-", "")) > ABBREVIATION_LETTERS:
        return False
    return not _known(word) or "Abbr" in _morph().parse(word.lower())[0].tag


def _abbreviations(chapters: list[list[str]]) -> frozenset[str]:
    """Short caps words the book writes in caps among ordinary text more often than in
    lowercase, and not once or twice for emphasis: ИКР of a book on invention is no `икр`,
    though the dictionary knows the word."""
    caps: Counter[str] = Counter()
    lower: Counter[str] = Counter()
    for paragraph in (p for paragraphs in chapters for p in paragraphs):
        found = CAPS.findall(paragraph)
        among = not all(w.isupper() for w in found)
        caps.update(w for w in found if among and w.isupper())
        lower.update(w.lower() for w in found if not w.isupper())
    return frozenset(
        w
        for w, n in caps.items()
        if len(w.replace("-", "")) <= ABBREVIATION_LETTERS and n >= ABBREVIATION_REPEATS and n > lower[w.lower()]
    )


def _signs(text: str) -> str:
    """Signs of a formula said as words. A hyphen between numbers is a range and stays,
    unless the formula has other signs or a variable; a `+` before a vowel is a stress
    mark; a sign with nothing to join is not read."""

    def formula(m: re.Match[str]) -> str:
        part = m.group()
        if not re.search(r"[−+=×*÷≈≠<>≤≥]|(?<!\w)[a-z](?!\w)", part):
            return part
        return " ".join(re.sub(r"([-−+=×*÷≈≠<>≤≥])", lambda s: f" {SIGNS[s.group(1)]} ", part).split())

    text = FORMULA.sub(formula, text)
    text = WORD_SIGN.sub(lambda m: f" {SIGNS[m.group(1) or m.group()]} ", text)
    # `+5 °C`, but not a phone number: the rules read `+7 999 123-45-67` themselves
    text = re.sub(r"(?<![\w+])\+(?=\d)(?![\d\s()-]{7})", "плюс ", text)
    return re.sub(r"\s*(?<!\S)[+=](?!\S)\s*", " ", text).strip()


def _rules(text: str) -> str:
    from rutextnorm import normalize_russian

    spoken: str = normalize_russian(text)
    return spoken


def _stressed(text: str, accented: str) -> str:
    """Stress marks moved onto the original text: RUAccent loses brackets, quotes and spaces.
    It also puts back the ё that books print as е (`четкое` -> `ч+ёткое`), so words are
    matched with ё read as е and its version is taken."""
    marks: list[str] = [m.group() for m in WORD.finditer(accented)]
    at = 0

    def bare(word: str) -> str:
        return word.replace("+", "").replace("ё", "е").replace("Ё", "Е")

    def put(m: re.Match[str]) -> str:
        nonlocal at
        word = m.group()
        for k in range(at, min(at + 4, len(marks))):
            if bare(marks[k]) == bare(word):
                at = k + 1
                return marks[k]
        return word

    return WORD.sub(put, text)


def _spoken(text: str) -> str:
    """Brackets read as a pause between commas, quotes are not read at all."""
    text = re.sub(r"\s*\(([^()]*)\)", r", \1,", text)
    text = re.sub(r"[«»“”„\"]", "", text)
    text = re.sub(r",\s*([,.?!:;])", r"\1", text)
    text = re.sub(r"—\s*,", "—", text)
    text = re.sub(r",\s*—", " —", text)
    return re.sub(r"\s+", " ", text).strip().lstrip(",").strip()


def _without_latin(text: str) -> str:
    return re.sub(r"\s+", " ", LATIN.sub("", text)).strip()
