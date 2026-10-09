import hashlib
import importlib.metadata
import io
import os
import re
import sys
import tarfile
import threading
import tomllib
import zipfile
from functools import partial
from pathlib import Path

import pytest

from audiobux import fetch, runtime
from audiobux.config import Settings
from audiobux.fetch import Fetch, Source
from audiobux.runtime import Download, Runtime

ROOT = Path(__file__).parent.parent
TRAINER = "from f5_tts.model.trainer import Trainer\n"


def wheel(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return out.getvalue()


def llama() -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, data in [("llama-server", b"server"), ("libllama.so.0.8.0", b"lib"), ("llama-cli", b"cli")]:
            info = tarfile.TarInfo(f"llama-b1/{name}")
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("llama-b1/libllama.so.0")
        link.type, link.linkname = tarfile.SYMTYPE, "libllama.so.0.8.0"
        tar.addfile(link)
    return out.getvalue()


ALPHA = wheel(
    {
        "alpha/__init__.py": "A = 1\n",
        "alpha-1.0.dist-info/METADATA": "Name: alpha\nVersion: 1.0\n",
        "alpha-1.0.data/purelib/alpha_extra.py": "",
        "alpha-1.0.data/scripts/alpha": "#!/bin/sh\n",
        "nvidia/cu/include/cu.h": "",
        "nvidia/cu/lib/libcu.a": "",
        "nvidia/cu/lib/libcu.so.12": "",
    }
)
F5 = wheel(
    {"f5_tts/model/__init__.py": f"from f5_tts.model.cfm import CFM\n{TRAINER}from f5_tts.model.dit import DiT\n"}
)


class Net:
    """The files by URL, in small chunks; `bad` serves other bytes for a URL so many times first."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files, self.bad = files, {}
        self.asked: list[str] = []
        self.runtime: Runtime | None = None
        self.seen: list[int] = []

    def get(self, url: str):
        self.asked.append(url)
        data = self.files[url]
        if self.bad.get(url):
            self.bad[url] -= 1
            data = b"z" * len(data)
        for i in range(0, len(data), 7):
            yield data[i : i + 7]
            if self.runtime:
                self.seen.append(self.runtime.done)


def entry(name: str, version: str, data: bytes | None, url: str | None = None) -> str:
    text = f'[[packages]]\nname = "{name}"\nversion = "{version}"\n'
    if data is not None:
        sha = hashlib.sha256(data).hexdigest()
        text += f'wheels = [{{ url = "{url}", size = {len(data)}, hashes = {{ sha256 = "{sha}" }} }}]\n'
    return text + "\n"


@pytest.fixture
def net(monkeypatch):
    tgz = llama()
    monkeypatch.setattr(
        runtime, "LLAMA", Download("llama.tgz", "https://l/llama.tgz", len(tgz), hashlib.sha256(tgz).hexdigest())
    )
    return Net(
        {
            "https://w/alpha-1.0-py3-none-any.whl": ALPHA,
            "https://w/f5_tts-1.1.22-py3-none-any.whl": F5,
            "https://l/llama.tgz": tgz,
        }
    )


def lock(tmp_path: Path, *entries: str, name: str = "pylock.toml") -> Path:
    path = tmp_path / name
    path.write_text('lock-version = "1.0"\n\n' + "".join(entries))
    return path


def two(tmp_path: Path, name: str = "pylock.toml") -> Path:
    return lock(
        tmp_path,
        entry("alpha", "1.0", ALPHA, "https://w/alpha-1.0-py3-none-any.whl"),
        entry("f5-tts", "1.1.22", F5, "https://w/f5_tts-1.1.22-py3-none-any.whl"),
        name=name,
    )


def test_the_runtime_is_downloaded_checked_and_laid_out(tmp_path, net):
    r = Runtime(two(tmp_path), tmp_path / "runtime", net.get)
    net.runtime = r
    assert not r.ready and r.total is None
    r.install()
    assert r.ready and r.done == r.total == len(ALPHA) + len(F5) + runtime.LLAMA.size
    assert net.seen == sorted(net.seen) and 0 < net.seen[0] < net.seen[-1] <= r.total
    site = r.site
    assert (site / "alpha/__init__.py").read_text() == "A = 1\n"
    assert (site / "alpha-1.0.dist-info/METADATA").exists() and (site / "alpha_extra.py").exists()
    # the scripts, the static libraries and the headers stay out
    assert not (site / "alpha-1.0.data").exists() and not (site / "alpha").with_name("scripts").exists()
    assert sorted(p.name for p in (site / "nvidia/cu").rglob("*") if p.is_file()) == ["libcu.so.12"]
    # F5-TTS's model package loads without its trainer
    assert (site / "f5_tts/model/__init__.py").read_text() == (
        "from f5_tts.model.cfm import CFM\nfrom f5_tts.model.dit import DiT\n"
    )
    # llama-server, its library and the link to it, nothing else of the release
    assert sorted(p.name for p in r.llama.iterdir()) == ["libllama.so.0", "libllama.so.0.8.0", "llama-server"]
    assert (r.llama / "libllama.so.0").is_symlink() and os.access(r.llama / "llama-server", os.X_OK)
    assert not list(r.dir.glob("*.part"))


def test_a_second_start_downloads_nothing(tmp_path, net):
    Runtime(two(tmp_path), tmp_path / "runtime", net.get).install()
    asked = len(net.asked)
    again = Runtime(two(tmp_path), tmp_path / "runtime", net.get)
    assert again.ready and len(net.asked) == asked == 3


def test_other_bytes_are_not_taken_and_the_next_try_goes_on(tmp_path, net):
    r = Runtime(two(tmp_path), tmp_path / "runtime", net.get)
    net.bad["https://w/f5_tts-1.1.22-py3-none-any.whl"] = 1
    with pytest.raises(ValueError, match="sha256"):
        r.install()
    assert not r.ready and r.done == len(ALPHA) and not list(r.dir.glob("*.part"))
    assert not (r.site / "f5_tts").exists()
    r.install()
    # alpha was whole before the failure: it is not fetched again
    assert r.ready and net.asked.count("https://w/alpha-1.0-py3-none-any.whl") == 1


def test_a_broken_download_counts_back(tmp_path, net):
    r = Runtime(two(tmp_path), tmp_path / "runtime", net.get)

    def broken(url):
        yield b"x" * 5
        raise ConnectionError("Network is unreachable")

    r.get = broken
    with pytest.raises(ConnectionError):
        r.install()
    assert r.done == 0 and not list(r.dir.glob("*.part"))


def test_a_new_lock_installs_beside_and_the_old_goes_after(tmp_path, net):
    old = Runtime(two(tmp_path), tmp_path / "runtime", net.get)
    old.install()
    new = Runtime(
        lock(tmp_path, entry("alpha", "1.0", ALPHA, "https://w/alpha-1.0-py3-none-any.whl")), old.root, net.get
    )
    assert new.dir != old.dir and not new.ready
    net.bad["https://l/llama.tgz"] = 1
    with pytest.raises(ValueError):
        new.install()
    assert old.dir.exists(), "the old one stays until the new is whole"
    new.install()
    assert [p.name for p in old.root.iterdir()] == [new.dir.name]


def test_an_empty_lock_is_llama_server_alone(tmp_path, net):
    r = Runtime(lock(tmp_path), tmp_path / "runtime", net.get)
    r.install()
    assert net.asked == ["https://l/llama.tgz"] and r.total == runtime.LLAMA.size


def test_what_the_image_has_at_the_version_stays_in_the_image(tmp_path, net):
    version = importlib.metadata.version("pytest")
    path = lock(
        tmp_path,
        entry("pytest", version, ALPHA, "https://w/pytest.whl"),
        entry("pytest", "0.0.1", ALPHA, "https://w/alpha-1.0-py3-none-any.whl"),
    )
    assert [w.url for w in runtime.wheels(tomllib.loads(path.read_text()))] == ["https://w/alpha-1.0-py3-none-any.whl"]


def test_a_package_without_a_wheel_must_be_in_the_image(tmp_path):
    there = lock(tmp_path, entry("pytest", importlib.metadata.version("pytest"), None))
    assert runtime.wheels(tomllib.loads(there.read_text())) == []
    missing = lock(tmp_path, entry("encodec", "0.1.1", None))
    with pytest.raises(ValueError, match="encodec"):
        runtime.wheels(tomllib.loads(missing.read_text()))


def test_the_wheel_for_x86_64_is_taken(tmp_path):
    p = {
        "name": "lxml",
        "version": "0.0.1",
        "wheels": [
            {"url": "https://w/lxml-0.0.1-py3-none-any.whl", "size": 1, "hashes": {"sha256": "a"}},
            {"url": "https://w/lxml-0.0.1-cp312-manylinux_2_28_x86_64.whl", "size": 2, "hashes": {"sha256": "b"}},
        ],
    }
    assert [w.size for w in runtime.wheels({"packages": [p]})] == [2]


def test_activate_puts_it_on_the_paths(tmp_path, net, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    r = Runtime(two(tmp_path), tmp_path / "runtime", net.get)
    r.install()
    r.activate()
    assert str(r.site) == sys.path[-1]
    assert os.environ["PATH"] == f"{r.llama}:/usr/bin"
    assert os.environ["LD_LIBRARY_PATH"] == f"{r.site}/nvidia/cuda_runtime/lib:{r.site}/nvidia/cublas/lib"
    monkeypatch.delitem(sys.modules, "alpha", raising=False)
    assert importlib.import_module("alpha").A == 1


# the fetch waits for the runtime


def test_the_models_wait_for_the_runtime(tmp_path, net, monkeypatch):
    where = tmp_path / "models"
    where.mkdir()
    asked: list[bool] = []

    def needed(s):
        # what the models need is known with the runtime in place only
        asked.append(str(Path(s.data) / "runtime") in "".join(sys.path))
        return [Source("test/model", names=("a.bin",), local=str(where))]

    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(fetch, "needed", needed)
    monkeypatch.setattr(fetch, "Runtime", partial(Runtime, get=net.get))

    class Hub:
        def files(self, source):
            return [source.file(n) for n in source.names]

        def measure(self, file):
            return 10, (where / file.name).exists()

        def path(self, file):
            return where / file.name if (where / file.name).exists() else None

        def download(self, file, seen):
            seen(5)
            progress.append(f.progress())
            (where / file.name).write_bytes(b"x" * 10)

    progress: list[tuple[int | None, int | None]] = []
    s = Settings(data=tmp_path / "data", runtime=str(two(tmp_path)), llm="", ocr="", asr="", accent="")
    ready = threading.Event()
    f = Fetch(s, Hub(), on_ready=ready.set)
    assert not f.ready and f.pending == [] and asked == [] and f.progress() == (None, None)
    f.start()
    assert ready.wait(10) and f.ready and asked == [True]
    total = len(ALPHA) + len(F5) + runtime.LLAMA.size
    # the runtime's bytes go ahead of the models'
    assert progress == [(total + 5, total + 10)] and f.progress() == (None, None)
    # the next start: all in place, nothing downloaded or started
    asked_net = len(net.asked)
    again = Fetch(s, Hub())
    assert again.ready and len(net.asked) == asked_net
    again.start()
    assert again._thread is None


def test_progress_while_installing_is_the_runtimes(tmp_path, net, monkeypatch):
    monkeypatch.setattr(fetch, "Runtime", partial(Runtime, get=net.get))
    s = Settings(data=tmp_path / "data", runtime=str(two(tmp_path)), llm="", ocr="", asr="", accent="")
    f = Fetch(s, sleep=lambda _: None)
    assert f.runtime is not None
    f.runtime.total, f.runtime.done = 100, 40
    assert f.progress() == (40, 100)


def test_no_runtime_lock_means_it_is_all_here(tmp_path):
    f = Fetch(Settings(data=tmp_path / "data", tts="tone", llm="", ocr="", asr="", accent=""))
    assert f.runtime is None and f.ready


def test_a_pinned_whisper_is_known_without_faster_whisper(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", None)
    assert fetch.whisper_repo("large-v3-turbo") == "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    # outside the pins there is no revision to ask for anyway
    assert fetch.whisper_repo("medium") == "medium" and fetch.revision("medium") is None


# the locks


def packages(name: str) -> dict[str, str]:
    return {p["name"]: p["version"] for p in tomllib.loads((ROOT / name).read_text())["packages"]}


def test_the_worker_image_is_at_the_runtimes_versions():
    rt, worker = packages("pylock.runtime.toml"), packages("pylock.worker.toml")
    assert {n: rt.get(n) for n in worker} == worker


def test_the_voice_is_the_one_it_was_tried_with():
    rt = packages("pylock.runtime.toml")
    assert (rt["torch"], rt["torchaudio"], rt["f5-tts"]) == ("2.15.0", "2.11.1", "1.1.22")
    assert "triton" not in rt


def test_what_has_no_wheel_the_worker_image_brings_at_the_version():
    lock = tomllib.loads((ROOT / "pylock.runtime.toml").read_text())
    sdists = {p["name"]: p["version"] for p in lock["packages"] if not p.get("wheels")}
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert sdists and all(f"{n}=={v}" in dockerfile for n, v in sdists.items()), sdists


def test_the_web_image_is_fastapi_and_uvicorn_alone():
    web = packages("pylock.web.toml")
    assert {"fastapi", "uvicorn", "starlette", "pydantic"} <= set(web)
    assert not set(web) & {"numpy", "torch", "pymupdf", "requests"}
    assert re.search(r"musl", (ROOT / "pylock.web.toml").read_text())
