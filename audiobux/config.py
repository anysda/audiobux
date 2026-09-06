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
        return cls(data=Path(get("data", d.data)), toc_depth=int(get("toc_depth", d.toc_depth)), tts=get("tts", d.tts))
