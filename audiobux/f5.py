"""F5-TTS_RUSSIAN (Misha24-10), the v4_winter checkpoint.

The voice is cloned from a short sample: a wav of 5-12 seconds and its exact
text, with stress marks if the book text has them. The model was trained on
text with `+` before the stressed vowel, so the sample's text is taken from
`<sample>_plus.txt` when it is there.
"""

import random
import tempfile
from collections.abc import Generator
from pathlib import Path

import numpy as np
import soundfile as sf

REPO = "Misha24-10/F5-TTS_RUSSIAN"
REVISION = "ea166adeae4c80ec5ee423a671e2bdb83906cf84"
CHECKPOINT = "F5TTS_v1_Base_v4_winter/model_212000.safetensors"
VOCAB = "F5TTS_v1_Base/vocab.txt"
ARCH = {
    "dim": 1024,
    "depth": 22,
    "heads": 16,
    "ff_mult": 2,
    "text_dim": 512,
    "text_mask_padding": True,
    "qk_norm": None,
    "conv_layers": 4,
    "pe_attn_head": None,
}
# the author points to F5-TTS's own gradio app, these are its defaults, but for the
# speed: at 1.0 the voice says 5.9 syllables a second against the speaker's own 5.2
INFER = {"nfe_step": 32, "cfg_strength": 2.0, "sway_sampling_coef": -1.0, "speed": 0.9}
SEED = 42
# One piece is one pass of the model; the chunk step makes pieces of whole sentences
# that fit `room` seconds of speech: the sample and the text together within BUDGET.
# A longer pass sounds wet and metallic (UTMOS 3.52 at 5-8 s, 3.01 at 14 s and more).
BUDGET = 20.0
EDGE = 0.03
# F5 gives a piece the sample's seconds per byte of text, but it starts every piece
# with up to a second of the pause after the sample's last sentence. A long piece
# hardly notices; a heading has nothing left for its last word, and it is cut off.
SHORT = 3.0
PAD = 1.5
ENDS = (".", "!", "?", "…")


class F5Tts:
    rate = 24000

    def __init__(self, voice: Path, voice_text: str, models: Path) -> None:
        self.voice, self.models = Path(voice), Path(models)
        self.voice_text = voice_text or _sample_text(self.voice)
        self._model = None

    def synth(self, text: str, seed: int = 0) -> tuple[np.ndarray, int]:
        self._load()
        return trim(self._pass(text.replace("|", " "), seed), self.rate), self.rate

    @property
    def room(self) -> float:
        """Seconds of speech one piece may have."""
        self._load_sample()
        return BUDGET - self._ref_seconds

    def seconds(self, text: str) -> float:
        self._load_sample()
        return _seconds(text, self._ref_seconds, self._ref_text)

    def _pass(self, text: str, seed: int) -> np.ndarray:
        import torch
        from f5_tts.infer.utils_infer import infer_batch_process

        random.seed(SEED + seed)
        np.random.seed(SEED + seed)
        torch.manual_seed(SEED + seed)
        torch.cuda.manual_seed_all(SEED + seed)
        text, duration = _fit(text, self._ref_seconds, self._ref_text)
        wave, _, _ = next(
            infer_batch_process(
                self._audio,
                self._ref_text,
                [text],
                self._model,
                self._vocoder,
                mel_spec_type="vocos",
                progress=None,
                device=self._device,
                fix_duration=duration,
                **INFER,
            )
        )
        return np.asarray(wave, dtype=np.float32)

    def synth_many(self, texts: list[str], seed: int = 0) -> Generator[tuple[np.ndarray, int], None, None]:
        """synth() of every piece, in order. Pieces batched into one pass of the model
        gave the same CER and length but no speed on an RTX 3090: one piece already
        fills the card, so they are voiced one by one."""
        for text in texts:
            yield self.synth(text, seed)

    def _load_sample(self) -> None:
        if getattr(self, "_ref", None) is None:
            self._ref, self._ref_text = _prepare(self.voice, self.voice_text)
            self._ref_seconds: float = sf.info(self._ref).duration

    def _load(self) -> None:
        if self._model:
            return
        from f5_tts.infer.utils_infer import load_model, load_vocoder
        from f5_tts.model import DiT
        from huggingface_hub import hf_hub_download

        cache = self.models / "hf"
        checkpoint = hf_hub_download(REPO, CHECKPOINT, revision=REVISION, cache_dir=cache)
        vocab = hf_hub_download(REPO, VOCAB, revision=REVISION, cache_dir=cache)
        self._model = load_model(DiT, ARCH, checkpoint, mel_spec_type="vocos", vocab_file=vocab)
        self._vocoder = load_vocoder(vocoder_name="vocos")
        self._load_sample()
        import torch
        import torchaudio

        self._audio = torchaudio.load(self._ref)
        self._device = "cuda" if torch.cuda.is_available() else "cpu"


def _seconds(text: str, ref_seconds: float, ref_text: str) -> float:
    """How long F5 makes the text: the sample's seconds per byte, slowed by the speed."""
    return ref_seconds * len(text.encode("utf-8")) / len(ref_text.encode("utf-8")) / INFER["speed"]


def _fit(text: str, ref_seconds: float, ref_text: str) -> tuple[str, float | None]:
    """Every part ends with a mark, or F5 trails off; a short one also gets room for
    its leading pause. The duration counts the sample too, as F5 wants it."""
    seconds = _seconds(text, ref_seconds, ref_text)
    if not text.endswith(ENDS + (",", ";", ":", "—")):
        text += "."
    if seconds >= SHORT:
        return text, None
    return text, ref_seconds + seconds + PAD


def trim(wave: np.ndarray, rate: int, keep: float = EDGE, floor_db: float = 42) -> np.ndarray:
    """Silence off both edges down to `keep` seconds, with short fades so the cut never clicks."""
    frame = int(0.01 * rate)
    n = len(wave) // frame
    if n < 3:
        return wave
    energy = 20 * np.log10(np.sqrt((wave[: n * frame].reshape(n, frame) ** 2).mean(1) + 1e-12))
    loud = np.flatnonzero(energy > energy.max() - floor_db)
    a = max(0, loud[0] * frame - int(keep * rate))
    b = min(len(wave), (loud[-1] + 1) * frame + int(keep * rate))
    wave = wave[a:b].copy()
    fade = min(int(0.008 * rate), len(wave) // 4)
    wave[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
    wave[len(wave) - fade :] *= np.linspace(1, 0, fade, dtype=np.float32)
    return wave


def _sample_text(voice: Path) -> str:
    plus = voice.with_name(voice.stem + "_plus.txt")
    return (plus if plus.exists() else voice.with_suffix(".txt")).read_text(encoding="utf-8").strip()


def _prepare(voice: Path, text: str) -> tuple[str, str]:
    """Silence trimmed off the sample's edges. F5's own preparation also cuts a long
    sample to 12 seconds but keeps the whole text, and then they no longer match."""
    from f5_tts.infer.utils_infer import remove_silence_edges
    from pydub import AudioSegment

    audio = remove_silence_edges(AudioSegment.from_file(voice)) + AudioSegment.silent(duration=50)
    path = Path(tempfile.mkdtemp()) / "voice.wav"
    audio.export(path, format="wav")
    if not text.endswith(". "):
        text += " " if text.endswith(".") else ". "
    return str(path), text
