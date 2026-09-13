"""What a voiced piece sounds like, and its pauses set by the text.

Words are found in the recording letter by letter: a Russian wav2vec2 CTC model
is forced along the piece's own text, so the first and the last letter of every
word get a time and a confidence. Only a silence between two words is changed,
and only when both words are sure and lie on its sides with 40 ms to spare: a
closure inside a word (the stop before `т` in `существуют`) stays as it is. At a
boundary of the text the silence takes the length a reader gives it there (the
table is measured on the speaker's own reading), anywhere else it is cut down
to 50 ms; nothing is inserted into continuous speech.

The same alignment tells a word broken by a silence: 80 ms or more, or 160 ms
when a stop (п т к б д г ц ч) stands next to it, as the speaker's own reading
holds a closure up to 150 ms and any other silence in a word up to 70 ms. The
words heard by whisper tell a stutter: a word that is not in the text but starts
or repeats a word next to it, or a word stretched 2.5 times the piece's median.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from rapidfuzz.distance import Levenshtein

# pause after a boundary, s: syntagm, comma, sentence, paragraph, heading, chapter
PAUSE = {"syn": 0.22, "comma": 0.40, "stop": 0.80, "para": 1.10, "head": 1.40, "chapter": 2.0}
SQUEEZE = 0.05
GUARD = 0.04
SURE = 0.2
BREAK = 0.08
CLOSURE = 0.16
STOPS = set("пткбдгцч")
STRETCH = 2.5
FADE = 0.006
HOP = 0.02  # one CTC frame of wav2vec2

ALIGNER = "jonatasgrosman/wav2vec2-large-xlsr-53-russian"
ALIGNER_REVISION = "2329100508896c6d9b157019803ab5601e6f3406"


def words(text: str) -> list[str]:
    """Bare lowercase words, `ё` read as `е`, a hyphen splits: the units every check counts."""
    return re.findall(r"[а-яa-z0-9]+", text.lower().replace("ё", "е").replace("+", ""))


@dataclass
class Word:
    word: str
    start: float
    end: float
    sure: float
    letters: list[tuple[str, float, float]] = field(default_factory=list)


@dataclass
class Heard:
    """What whisper heard: the text and its words with times."""

    text: str
    words: list[tuple[str, float, float]] = field(default_factory=list)


class Aligner:
    """Letters of the text on the recording, by CTC forced alignment."""

    def __init__(self, models: Path) -> None:
        self.models = models
        self._model: Any = None

    def align(self, wave: np.ndarray, rate: int, text_words: list[str]) -> list[Word]:
        import torch
        import torchaudio.functional as F

        self._load()
        vocab = self._vocab
        x = _resample(wave, rate, 16000)
        inputs = self._processor(x, sampling_rate=16000, return_tensors="pt").input_values.to(self._device)
        with torch.no_grad():
            log_probs = torch.log_softmax(self._model(inputs).logits[0].float(), -1)
        tokens: list[int] = []
        owner: list[int] = []
        chars: list[str] = []
        for k, w in enumerate(text_words):
            letters = [c for c in w if c in vocab and c not in "|-"]
            if tokens and letters:
                tokens.append(vocab["|"])
                owner.append(-1)
                chars.append(" ")
            tokens += [vocab[c] for c in letters]
            owner += [k] * len(letters)
            chars += letters
        if not tokens or log_probs.shape[0] < len(tokens):
            return [Word(w, 0.0, 0.0, 0.0) for w in text_words]
        path, scores = F.forced_align(
            log_probs.unsqueeze(0), torch.tensor([tokens], device=self._device), blank=vocab["<pad>"]
        )
        spans = F.merge_tokens(path[0], scores[0].exp(), blank=vocab["<pad>"])
        out = []
        for k, w in enumerate(text_words):
            at = [i for i, o in enumerate(owner) if o == k]
            if not at:
                out.append(Word(w, 0.0, 0.0, 0.0))
                continue
            out.append(
                Word(
                    w,
                    spans[at[0]].start * HOP,
                    spans[at[-1]].end * HOP,
                    float(np.mean([spans[i].score for i in at])),
                    [(chars[i], spans[i].start * HOP, spans[i].end * HOP) for i in at],
                )
            )
        return out

    def close(self) -> None:
        self._model = None

    def _load(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import Wav2Vec2ForCTC, Wav2Vec2Processor

        cache = self.models / "hf"
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        self._processor = Wav2Vec2Processor.from_pretrained(ALIGNER, revision=ALIGNER_REVISION, cache_dir=cache)
        self._vocab = self._processor.tokenizer.get_vocab()
        self._model = (
            Wav2Vec2ForCTC.from_pretrained(ALIGNER, revision=ALIGNER_REVISION, cache_dir=cache).to(self._device).eval()
        )


def _resample(wave: np.ndarray, rate: int, to: int) -> np.ndarray:
    if rate == to:
        return wave
    import librosa

    return librosa.resample(wave, orig_sr=rate, target_sr=to)


def loudness(wave: np.ndarray, rate: int) -> np.ndarray:
    """dB of 10 ms frames, at 16 kHz as every threshold here was measured."""
    y = _resample(wave, rate, 16000)
    n = len(y) // 160
    return 20 * np.log10(np.sqrt((y[: n * 160].reshape(n, 160) ** 2).mean(1) + 1e-12))


def silences(wave: np.ndarray, rate: int, shortest: float = 0.03) -> list[tuple[float, float]]:
    """Silences inside the piece (not at its edges), 35 dB under its loud part."""
    db = loudness(wave, rate)
    if not len(db):
        return []
    quiet = np.r_[False, db < np.percentile(db, 95) - 35, False].astype(int)
    starts, ends = np.flatnonzero(np.diff(quiet) == 1), np.flatnonzero(np.diff(quiet) == -1)
    total = len(wave) / rate
    return [
        (a / 100, b / 100)
        for a, b in zip(starts, ends, strict=True)
        if (b - a) / 100 >= shortest and a / 100 > 0.02 and b / 100 < total - 0.02
    ]


def inside_word(start: float, end: float, aligned: list[Word]) -> Word | None:
    """The word whose letters lie on both sides of the silence's middle."""
    mid = (start + end) / 2
    for w in aligned:
        if w.sure > 0 and w.start < mid < w.end - HOP:
            return w
    return None


def broken(wave: np.ndarray, rate: int, aligned: list[Word]) -> list[tuple[str, float, float]]:
    """Words broken by a silence of BREAK or more, CLOSURE or more next to a stop:
    [(word, where, how long)]."""
    out = []
    for s, e in silences(wave, rate, BREAK):
        w = inside_word(s, e, aligned)
        if w and e - s < CLOSURE and beside_stop(s, e, w):
            continue
        if w:
            out.append((w.word, round(s, 2), round(e - s, 3)))
    return out


def beside_stop(start: float, end: float, word: Word) -> bool:
    """A stop is the last letter before the silence's middle or the first after it."""
    mid = (start + end) / 2
    before = [c for c, _, b in word.letters if b <= mid]
    after = [c for c, a, _ in word.letters if a >= mid]
    return bool((before and before[-1] in STOPS) or (after and after[0] in STOPS))


def retime(
    wave: np.ndarray, rate: int, aligned: list[Word], bounds: dict[int, str]
) -> tuple[np.ndarray, dict[str, int]]:
    """Pauses set by the text. `bounds` maps the index of a word to the boundary after
    it (syn, comma, stop). Returns the new wave and what was done."""
    edits, used, kept, unsure = [], set(), 0, 0
    for s, e in silences(wave, rate):
        mid = (s + e) / 2
        if inside_word(s, e, aligned):
            kept += 1
            continue
        before = max((i for i, w in enumerate(aligned) if w.sure > 0 and w.end <= mid), default=None)
        after = min((i for i, w in enumerate(aligned) if w.sure > 0 and w.start >= mid), default=None)
        if (
            before is None
            or after is None
            or after != before + 1
            or aligned[before].end > s + GUARD
            or aligned[after].start < e - GUARD
            or min(aligned[before].sure, aligned[after].sure) < SURE
        ):
            unsure += 1
            continue
        if before in bounds and before not in used:
            used.add(before)
            edits.append((s, e, PAUSE[bounds[before]]))
        elif e - s > SQUEEZE:
            edits.append((s, e, SQUEEZE))
    info = {
        "bounds": len(bounds),
        "set": len(used),
        "squeezed": sum(1 for *_, x in edits if x == SQUEEZE),
        "kept_in_word": kept,
        "unsure": unsure,
    }
    return apply(wave, rate, edits), info


def apply(wave: np.ndarray, rate: int, edits: list[tuple[float, float, float]]) -> np.ndarray:
    """Each (start, end) replaced by silence of the given length, with short fades at the cuts."""
    out, cur, fade = [], 0, int(FADE * rate)
    for s, e, new in sorted(edits):
        a, b = int(s * rate), int(e * rate)
        if a < cur:
            continue
        seg = wave[cur:a].copy()
        if len(seg) > 2 * fade:
            seg[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
            if cur > 0:
                seg[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
        out += [seg, np.zeros(int(new * rate), np.float32)]
        cur = b
    tail = wave[cur:].copy()
    if cur > 0 and len(tail) > fade:
        tail[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
    out.append(tail)
    return np.concatenate(out).astype(np.float32)


def stutters(
    text_words: list[str], heard: list[tuple[str, float, float]], loud: np.ndarray | None = None
) -> tuple[int, list[str]]:
    """Stutters of a take: an extra word that starts or repeats a word of the text next
    to it, a syllable glued to a word (`сосовсем`), a word stretched STRETCH times the
    median per letter. `loud` (10 ms frames, True where sounding) measures a word by its
    sounding frames, as whisper stretches a word's span over the pause after it."""
    heard = joined(heard, set(text_words))
    hyp = [w for w, _, _ in heard]
    found = []
    for op in Levenshtein.opcodes(text_words, hyp):
        tag, r0, r1, h0, h1 = op.tag, op.src_start, op.src_end, op.dest_start, op.dest_end
        if tag == "equal":
            continue
        near = text_words[max(0, r0 - 1) : r1 + 1]
        for x in hyp[h0:h1]:
            if tag == "insert" and any(x == n or (n.startswith(x) and x != n) for n in near):
                found.append(f"повтор «{x}» у «{' '.join(near)}»")
            if tag == "replace" and any(
                x != n and x.endswith(n) and n.startswith(x[: len(x) - len(n)]) for n in text_words[r0:r1]
            ):
                found.append(f"слог «{x}» вместо «{' '.join(text_words[r0:r1])}»")
    spans = [(t1 - t0) if loud is None else loud[int(t0 * 100) : int(t1 * 100)].sum() / 100 for _, t0, t1 in heard]
    timed = [(w, d) for (w, _, _), d in zip(heard[:-1], spans[:-1], strict=True) if not re.search(r"\d", w)]
    per = [d / len(w) for w, d in timed]
    if len(per) >= 4:
        median = float(np.median(per))
        for (w, d), v in zip(timed, per, strict=True):
            if v > STRETCH * median and d > 0.5:
                found.append(f"тянется «{w}» {d:.2f} с")
    return len(found), found


def joined(heard: list[tuple[str, float, float]], text: set[str]) -> list[tuple[str, float, float]]:
    """Two heard words glued back into one when together they are a word of the text:
    whisper splits a compound (`трудно реализуемы`), the voice did not."""
    out: list[tuple[str, float, float]] = []
    for w in heard:
        if out and out[-1][0] + w[0] in text:
            out[-1] = (out[-1][0] + w[0], out[-1][1], w[2])
        else:
            out.append(w)
    return out


def piece_words(text: str, bounds: list[list[Any]]) -> tuple[list[str], dict[int, str]]:
    """Words of a piece and {index of a word: boundary after it}; `bounds` are
    [token index, kind] over the piece's whitespace tokens."""
    kinds = {int(i): k for i, k in bounds}
    out, at = [], {}
    for i, token in enumerate(text.split()):
        out += words(token)
        if i in kinds and out:
            at[len(out) - 1] = kinds[i]
    at.pop(len(out) - 1, None)
    return out, at
