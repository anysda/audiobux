import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    # everything a run keeps: work in progress and models
    data: Path = Path("data")
    toc_depth: int = 2
    tts: str = "tone"
    llm: str = "unsloth/Qwen3.6-35B-A3B-GGUF:Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
    ocr: str = "unsloth/Qwen3.6-35B-A3B-GGUF:Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
    ocr_vision: str = "mmproj-F16.gguf"
    fix_max_change: float = 0.1
    accent: str = "turbo3.1"
    stress: str = "plus"

    @property
    def work(self) -> Path:
        return self.data / "work"

    @property
    def models(self) -> Path:
        return self.data / "models"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        def get(name: str, default: object) -> str:
            return env.get("AUDIOBUX_" + name.upper(), str(default))

        d = cls()
        return cls(
            data=Path(get("data", d.data)),
            toc_depth=int(get("toc_depth", d.toc_depth)),
            tts=get("tts", d.tts),
            llm=get("llm", d.llm),
            ocr=get("ocr", d.ocr),
            ocr_vision=get("ocr_vision", d.ocr_vision),
            fix_max_change=float(get("fix_max_change", d.fix_max_change)),
            accent=get("accent", d.accent),
            stress=get("stress", d.stress),
        )
