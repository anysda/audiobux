import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    # everything the service keeps: uploads, the library, work in progress, models, the database
    data: Path = Path("data")
    # the worker's socket; empty means `<data>/run/worker.sock`
    socket: str = ""
    # HH:MM-HH:MM when the card rests, across midnight too; empty means round the clock
    quiet_hours: str = ""
    toc_depth: int = 2
    tts: str = "tone"
    voice: Path = Path("voice/m1.wav")
    voice_text: str = ""
    llm: str = "unsloth/Qwen3.6-35B-A3B-GGUF:Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
    ocr: str = "unsloth/Qwen3.6-35B-A3B-GGUF:Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
    ocr_vision: str = "mmproj-F16.gguf"
    fix_max_change: float = 0.1
    accent: str = "turbo3.1"
    stress: str = "plus"
    asr: str = "large-v3-turbo"
    verify_cer: float = 0.15
    verify_attempts: int = 3
    # the largest upload in MiB: a scanned book with pictures runs to a few hundred
    upload_mb: int = 1024
    # host names the web process answers to besides IP addresses and localhost, comma-separated
    hosts: str = ""
    # the built interface the web process serves; nothing is served when it is not there
    frontend: Path = Path("frontend/dist")
    # the lock of what the models run on, installed into <data>/runtime on the first start
    # (see `runtime`); empty means it is all in this Python already
    runtime: str = ""

    @property
    def incoming(self) -> Path:
        """Uploads as the web process wrote them, before the worker takes them."""
        return self.data / "incoming"

    @property
    def library(self) -> Path:
        return self.data / "library"

    @property
    def work(self) -> Path:
        return self.data / "work"

    @property
    def models(self) -> Path:
        return self.data / "models"

    @property
    def web(self) -> Path:
        """The one folder the web process writes to besides incoming/: where the listener is."""
        return self.data / "web"

    @property
    def db(self) -> Path:
        return self.data / "audiobux.db"

    @property
    def worker_socket(self) -> Path:
        return Path(self.socket) if self.socket else self.data / "run" / "worker.sock"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        def get(name: str, default: object) -> str:
            return env.get("AUDIOBUX_" + name.upper(), str(default))

        d = cls()
        return cls(
            data=Path(get("data", d.data)),
            socket=get("socket", d.socket),
            quiet_hours=get("quiet_hours", d.quiet_hours),
            toc_depth=int(get("toc_depth", d.toc_depth)),
            tts=get("tts", d.tts),
            voice=Path(get("voice", d.voice)),
            voice_text=get("voice_text", d.voice_text),
            llm=get("llm", d.llm),
            ocr=get("ocr", d.ocr),
            ocr_vision=get("ocr_vision", d.ocr_vision),
            fix_max_change=float(get("fix_max_change", d.fix_max_change)),
            accent=get("accent", d.accent),
            stress=get("stress", d.stress),
            asr=get("asr", d.asr),
            verify_cer=float(get("verify_cer", d.verify_cer)),
            verify_attempts=int(get("verify_attempts", d.verify_attempts)),
            upload_mb=int(get("upload_mb", d.upload_mb)),
            hosts=get("hosts", d.hosts),
            frontend=Path(get("frontend", d.frontend)),
            runtime=get("runtime", d.runtime),
        )
