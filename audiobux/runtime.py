"""What the models run on, installed by the worker on its first start: torch with CUDA,
F5-TTS, faster-whisper, RUAccent and the rest of `pylock.runtime.toml`, llama-server, and
the parser RUAccent takes from Koziev.

The image stays small; this part, some 4 GB, goes into `<data>/runtime/<hash>` beside
the models, the hash being that of the lock and the llama-server build. Every file is
pinned by its sha256, as the models are in `fetch`: a download with other bytes is
not taken. A new lock installs into a folder of its own and the old one goes once the
new is complete; a restart goes on from the last whole wheel.

The wheels are unpacked as they are into one folder put on the path, the way pip lays
them into site-packages; what the image has at the very version stays in the image.
"""

import ctypes
import fnmatch
import hashlib
import importlib.metadata
import logging
import os
import shutil
import site
import tarfile
import tomllib
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import requests

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Download:
    name: str
    url: str
    size: int
    sha256: str


# llama-server of the same build the models were tried with, built for CUDA 12
LLAMA = Download(
    "llama-b12063-bin-ubuntu-cuda-12.8-x64.tar.gz",
    "https://github.com/ggml-org/llama.cpp/releases/download/b12063/llama-b12063-bin-ubuntu-cuda-12.8-x64.tar.gz",
    187091879,
    "ae079baa6b9707fca197641080d365d6b5022218c8ea84b3edf726dcf89bff57",
)
# what of the release llama-server needs: itself and the libraries it finds beside it
LLAMA_FILES = (
    "llama-server",
    "libllama-server-impl.so",
    "libllama-common.so*",
    "libllama.so*",
    "libmtmd.so*",
    "libggml.so*",
    "libggml-base.so*",
    "libggml-cpu-*.so",
    "libggml-cuda.so",
)
# RUAccent takes its parser from Koziev into its own package folder: its load() fetches it
# from the hub, unpinned, whenever the folder is not there, and skips it when the folder is
# there, whole or not. The runtime brings it, at the revision of the RUAccent models.
ACCENT = "ruaccent/accentuator"
ACCENT_REVISION = "b78ae5ea1e62beaf138bed1865cd8c3b0b5ca855"
KOZIEV = tuple(
    Download(name, f"https://huggingface.co/{ACCENT}/resolve/{ACCENT_REVISION}/{name}", size, sha256)
    for name, size, sha256 in (
        ("koziev/rulemma/rulemma.dat", 16703198, "bf2b3ef3ff7a0aa6e4250aa4e9c8ed568e25f825deebdb12dee1b46b785ba9fc"),
        ("koziev/rulemma/rulemma.py", 11166, "61c0ae4ea718fc85000284dcbf798fb51657c41f3db3ae029e8bebfb33075f44"),
        ("koziev/rupostagger/__init__.py", 111, "68f14d0db5bc92e4da7363ca6c2c34812b18a87bdfe07714329bcfd711bdbe11"),
        (
            "koziev/rupostagger/database/ruword2tags.db",
            168816640,
            "a06848e656bef642aafb4440c03554fa78f2f32dde92ea66f3f86ce9977b167e",
        ),
        (
            "koziev/rupostagger/rupostagger.config",
            276,
            "c8c19d1cd92855bc281b36ae80c3b10ed20d7f0aeeca54467c8fd00df319c614",
        ),
        (
            "koziev/rupostagger/rupostagger.model",
            2417464,
            "21b7b0bfd7427b5fdc1604052176db8aa3b139b3ce03be440cfce48536f8e5ef",
        ),
        ("koziev/rupostagger/rupostagger.py", 7118, "111b8c22db18407fb34bcd6e00b3c2245165de98ddec0fdb40862d82af864530"),
        ("koziev/rupostagger/rusyllab.py", 18975, "331fdd0d9d10025bbebfc09ae648b21399d9f73cd168e1eeb76c10d6b0a31c00"),
        (
            "koziev/rupostagger/ruword2tags.dat",
            9683765,
            "dde47b5f1d48ff899887ac07812dcabd2966e48e84646f3065bfd06627c2af58",
        ),
        (
            "koziev/rupostagger/ruword2tags.py",
            18086,
            "2858ad021915f3fe5bf9589ab1773f8c2cdd664f053f3f6bf317fec3fa3ed23b",
        ),
    )
)
# the CUDA 12 libraries CTranslate2 opens by name, in the order they need each other
CUBLAS = ("cublas/lib/libcublasLt.so.12", "cublas/lib/libcublas.so.12")
# F5-TTS's model package imports its trainer on load, and the trainer's requirements are
# not installed (pyproject.toml): the line goes
PATCHES = {"f5_tts/model/__init__.py": "from f5_tts.model.trainer import Trainer\n"}
CHUNK = 1 << 20


def wheels(lock: dict[str, Any]) -> list[Download]:
    """The wheel of each package of the lock that the image does not have at its version.
    The lock is made for Linux on x86_64 and the one Python of the image, so its markers
    hold and the first wheel for x86_64 fits."""
    found = []
    for p in lock.get("packages", []):
        if _present(p["name"]) == p["version"]:
            continue
        if not p.get("wheels"):
            raise ValueError(f"{p['name']} {p['version']} has no wheel and the image does not have it")
        w = next((w for w in p["wheels"] if "x86_64" in w["url"]), p["wheels"][0])
        found.append(Download(w["url"].rsplit("/", 1)[1], w["url"], w["size"], w["hashes"]["sha256"]))
    return found


def _present(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def fetch_bytes(url: str) -> Iterator[bytes]:
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        yield from r.iter_content(CHUNK)


class Runtime:
    def __init__(self, lock: Path, root: Path, get: Callable[[str], Iterator[bytes]] = fetch_bytes) -> None:
        self.lock, self.root, self.get = lock, root, get
        text = lock.read_bytes()
        self.dir = root / hashlib.sha256(text + LLAMA.sha256.encode()).hexdigest()[:16]
        self.site, self.llama = self.dir / "site", self.dir / "llama"
        self.ready = (self.dir / "done").exists() and not self._koziev_missing()
        # bytes in place and in all, once install() has the list
        self.done = 0
        self.total: int | None = None

    def install(self) -> None:
        """Downloads what is not in place yet, checks it and unpacks it; may be called again
        after a failure and goes on from there."""
        noted = self.dir / "installed"
        self.dir.mkdir(parents=True, exist_ok=True)
        installed = set(noted.read_text().split()) if noted.exists() else set()
        # Koziev's files are in place by their bytes, whatever the note says
        missing = self._koziev_missing()
        installed = (installed - {d.name for d in KOZIEV}) | {d.name for d in KOZIEV if d not in missing}
        todo = [*wheels(tomllib.loads(self.lock.read_text())), LLAMA, *KOZIEV]
        self.total = sum(d.size for d in todo)
        self.done = sum(d.size for d in todo if d.name in installed)
        log.info("runtime: %.1f of %.1f GB in place", self.done / 1e9, self.total / 1e9)
        for d in todo:
            if d.name in installed:
                continue
            log.info("runtime: %s, %.1f MB", d.name, d.size / 1e6)
            part = self._download(d)
            if d is LLAMA:
                self._llama(part)
            elif d in KOZIEV:
                target = self.site / "ruaccent" / d.name
                target.parent.mkdir(parents=True, exist_ok=True)
                part.replace(target)
            else:
                self._wheel(part)
            part.unlink(missing_ok=True)
            with noted.open("a") as f:
                f.write(d.name + "\n")
        (self.dir / "done").touch()
        self.ready = True
        for old in self.root.iterdir():
            if old != self.dir:
                log.info("runtime: %s is of another lock, it goes", old.name)
                shutil.rmtree(old)
        log.info("runtime: in place")

    def activate(self) -> None:
        """Puts the runtime on this process's path, and llama-server and the CUDA 12 libraries
        on the path of what it starts. CTranslate2 opens cuBLAS by name at its first model,
        and this process's own library path is read once, at its start: cuBLAS is opened
        here first, and the later open finds it by that name."""
        site.addsitedir(str(self.site))
        os.environ["PATH"] = f"{self.llama}{os.pathsep}{os.environ.get('PATH', '')}"
        cuda = [self.site / "nvidia" / x / "lib" for x in ("cuda_runtime", "cublas")]
        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(
            [*map(str, cuda), *filter(None, [os.environ.get("LD_LIBRARY_PATH")])]
        )
        for lib in (self.site / "nvidia" / x for x in CUBLAS):
            if lib.exists():
                ctypes.CDLL(str(lib), mode=ctypes.RTLD_GLOBAL)

    def _koziev_missing(self) -> list[Download]:
        """Koziev's files not in RUAccent's package folder with the pinned bytes."""
        missing = []
        for d in KOZIEV:
            path = self.site / "ruaccent" / d.name
            if not path.is_file() or path.stat().st_size != d.size:
                missing.append(d)
                continue
            with path.open("rb") as f:
                if hashlib.file_digest(f, "sha256").hexdigest() != d.sha256:
                    missing.append(d)
        return missing

    def _download(self, d: Download) -> Path:
        part = self.dir / (d.name.replace("/", "_") + ".part")
        sha = hashlib.sha256()
        start = self.done
        try:
            with part.open("wb") as f:
                for chunk in self.get(d.url):
                    f.write(chunk)
                    sha.update(chunk)
                    self.done = min(self.done + len(chunk), start + d.size)
            if sha.hexdigest() != d.sha256:
                raise ValueError(f"{d.name} came with a sha256 other than the pinned one")
        except BaseException:
            part.unlink(missing_ok=True)
            self.done = start
            raise
        return part

    def _wheel(self, path: Path) -> None:
        """Unpacked as pip lays it out, without the scripts, static libraries and headers:
        nothing here builds against it or runs it from a shell."""
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                parts = PurePosixPath(info.filename).parts
                if parts[0].endswith(".data"):
                    if len(parts) < 3 or parts[1] not in ("purelib", "platlib"):
                        continue
                    parts = parts[2:]
                name = "/".join(parts)
                if info.is_dir() or name.endswith(".a") or "/include/" in f"/{name}":
                    continue
                target = self.site.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK)
                if name in PATCHES:
                    target.write_text(target.read_text().replace(PATCHES[name], ""))

    def _llama(self, path: Path) -> None:
        """llama-server and its libraries, the links between them kept."""
        self.llama.mkdir(parents=True, exist_ok=True)
        with tarfile.open(path) as tar:
            for member in tar.getmembers():
                base = PurePosixPath(member.name).name
                if (member.isfile() or member.issym()) and any(fnmatch.fnmatch(base, p) for p in LLAMA_FILES):
                    member.name = base
                    tar.extract(member, self.llama, filter="data")
