import hashlib
import threading
from pathlib import Path

import pytest

from audiobux import f5, fetch, models, takes
from audiobux.config import Settings
from audiobux.fetch import Fetch, Source


class Hub:
    """Files in a plain folder; `fail` says how many times a name fails first ("list"
    for the listing), and `gate` holds every download until it is set."""

    def __init__(self, sizes: dict[str, int], fail: dict[str, int] | None = None, gate=None, spoil=()):
        self.sizes, self.fail, self.gate, self.spoil = sizes, dict(fail or {}), gate, set(spoil)
        self.calls: list[str] = []
        self.seen: list[tuple[int | None, int | None]] = []
        self.fetch: Fetch | None = None

    def _maybe_fail(self, name: str, seen=None) -> None:
        if self.fail.get(name):
            self.fail[name] -= 1
            if seen:
                seen(self.sizes[name] // 3)
            raise ConnectionError("Network is unreachable")

    def files(self, source):
        self.calls.append("list")
        self._maybe_fail("list")
        return [source.file(n) for n in self.sizes]

    def measure(self, file):
        return self.sizes[file.name], self.path(file) is not None

    def path(self, file):
        where = Path(file.local) / file.name
        return where if where.is_file() else None

    def download(self, file, seen):
        self.calls.append(file.name)
        if self.gate:
            self.gate.wait(10)
        self._maybe_fail(file.name, seen)
        size = self.sizes[file.name]
        seen(size // 2)
        self.seen.append(self.fetch.progress())
        seen(size)
        seen(size // 3)  # a second bar of the same file, behind the first
        self.seen.append(self.fetch.progress())
        # a name in `spoil` comes with other bytes the first time
        spoilt = file.name in self.spoil
        self.spoil.discard(file.name)
        (Path(file.local) / file.name).write_bytes((b"y" if spoilt else b"x") * size)


def settings(tmp_path) -> Settings:
    return Settings(data=tmp_path / "data", llm="", ocr="", asr="", accent="")


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """One source of files in a folder of the test's own, whatever the settings say."""
    where = tmp_path / "models"
    where.mkdir()
    monkeypatch.setattr(fetch, "needed", lambda s: [Source("test/model", names=("a.bin", "b.bin"), local=str(where))])
    return where


def make(tmp_path, hub: Hub, sleeps: list | None = None) -> Fetch:
    f = Fetch(settings(tmp_path), hub, sleep=(sleeps if sleeps is not None else []).append)
    hub.fetch = f
    return f


def test_progress_counts_the_bytes_as_they_come(tmp_path, folder):
    (folder / "a.bin").write_bytes(b"x" * 100)  # in place from before
    hub = Hub({"a.bin": 100, "b.bin": 300})
    f = make(tmp_path, hub)
    assert not f.ready and f.progress() == (None, None)  # nothing is known before the listing
    f.run()
    # only the missing file is downloaded, the one in place counts as done from the start
    assert hub.calls == ["list", "b.bin"]
    assert hub.seen == [(250, 400), (400, 400)]
    assert f.ready and f.progress() == (None, None)


def test_a_lost_network_is_tried_again_and_the_download_goes_on(tmp_path, folder):
    hub = Hub({"a.bin": 100, "b.bin": 300}, fail={"list": 2, "b.bin": 1})
    sleeps: list[float] = []
    f = make(tmp_path, hub, sleeps)
    f.run()
    assert hub.calls == ["list", "list", "list", "a.bin", "b.bin", "b.bin"]
    assert sleeps == [10, 30, 10]  # the pause grows with failures in a row, and starts over
    assert f.ready
    assert (folder / "b.bin").stat().st_size == 300


def test_the_pause_stops_growing(tmp_path, folder):
    sleeps: list[float] = []
    make(tmp_path, Hub({"a.bin": 1}, fail={"list": 6}), sleeps).run()
    assert sleeps == [10, 30, 60, 300, 300, 300]


def test_models_in_place_are_not_fetched_again(tmp_path, folder):
    make(tmp_path, Hub({"a.bin": 1, "b.bin": 2})).run()

    class Offline(Hub):
        def files(self, source):
            raise AssertionError("the network is not asked")

        def download(self, file, seen):
            raise AssertionError("nothing is downloaded")

    again = make(tmp_path, Offline({"a.bin": 1, "b.bin": 2}))
    assert again.ready
    again.start()
    assert again._thread is None
    # a file gone from the folder makes the source fetched again
    (folder / "b.bin").unlink()
    hub = Hub({"a.bin": 1, "b.bin": 2})
    third = make(tmp_path, hub)
    assert not third.ready
    third.run()
    assert hub.calls == ["list", "b.bin"]


def test_start_runs_in_the_background_and_says_when_ready(tmp_path, folder):
    gate, woken = threading.Event(), threading.Event()
    hub = Hub({"a.bin": 1, "b.bin": 2}, gate=gate)
    f = Fetch(settings(tmp_path), hub, on_ready=woken.set)
    hub.fetch = f
    f.start()
    f.start()  # once
    assert not f.ready
    gate.set()
    assert woken.wait(10)
    assert f.ready


def test_the_settings_name_the_files_the_loaders_take():
    s = Settings(data=Path("/d"), tts="f5", asr="", accent="", llm="", ocr="org/vlm:v.gguf", ocr_vision="mm.gguf")
    found = fetch.needed(s)
    by_repo = {(x.repo, x.revision): x for x in found}
    assert by_repo[f5.REPO, f5.REVISION].names == (f5.CHECKPOINT, f5.VOCAB)
    assert by_repo[f5.REPO, f5.REVISION].cache == "/d/models/hf"
    assert (takes.ALIGNER, takes.ALIGNER_REVISION) in by_repo
    assert by_repo[fetch.VOCODER, fetch.revision(fetch.VOCODER)].cache is None  # F5's vocoder goes to the default cache
    assert [x.names for x in found if x.repo == "org/vlm"] == [("v.gguf",), ("mm.gguf",)]


def test_the_same_model_for_text_and_pages_is_fetched_once(tmp_path):
    gguf = tmp_path / "local.gguf"
    gguf.write_bytes(b"")
    same = Settings(data=tmp_path, asr="", accent="", llm="org/m:q.gguf", ocr="org/m:q.gguf", ocr_vision="")
    assert [x.names for x in fetch.needed(same)] == [("q.gguf",)]
    # a path to a file on the machine is not fetched
    assert fetch.needed(Settings(data=tmp_path, asr="", accent="", llm=str(gguf), ocr="")) == []


def test_tone_needs_nothing_and_is_ready_at_once(tmp_path):
    f = Fetch(settings(tmp_path))
    assert f.ready and f.progress() == (None, None)


def test_accents_and_hearing_come_from_their_libraries(tmp_path):
    s = Settings(data=tmp_path, llm="", ocr="", asr="large-v3-turbo", accent="turbo3.1")
    found = fetch.needed(s)
    whisper = next(x for x in found if x.cache == str(tmp_path / "models" / "whisper"))
    assert whisper.repo == "mobiuslabsgmbh/faster-whisper-large-v3-turbo" and "model.bin" in whisper.patterns
    accent = next(x for x in found if x.local == str(tmp_path / "models" / "ruaccent"))
    assert accent.repo == "ruaccent/accentuator"
    assert {"dictionary", "nn/nn_accent", "nn/nn_omograph/turbo3.1"} <= set(accent.folders)


def test_the_bar_counts_even_when_told_to_hide(tmp_path):
    seen: list[int] = []
    bar = fetch._counting(seen.append)(total=10, unit="B", disable=True)
    bar.update(4)
    bar.update(6)
    bar.close()
    assert seen == [4, 10]


def test_presence_is_told_without_the_network(tmp_path):
    hub = fetch.HuggingFace()
    (tmp_path / "here.txt").write_text("x")
    assert hub.path(fetch.File("a/b", "here.txt", local=str(tmp_path))) == tmp_path / "here.txt"
    assert hub.path(fetch.File("a/b", "gone.txt", local=str(tmp_path))) is None
    assert hub.path(fetch.File("a/b", "x.bin", revision="0" * 40, cache=str(tmp_path / "hf"))) is None


REVISION = "1" * 40


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def pinned(folder, monkeypatch):
    """The test's source pinned: a.bin is 100 bytes of x, b.bin 300."""
    monkeypatch.setattr(
        fetch, "PINS", {"test/model": fetch.Pin(REVISION, {"a.bin": sha(b"x" * 100), "b.bin": sha(b"x" * 300)})}
    )
    monkeypatch.setattr(
        fetch,
        "needed",
        lambda s: [Source("test/model", names=("a.bin", "b.bin"), revision=REVISION, local=str(folder))],
    )
    return folder


def test_a_pinned_file_with_other_bytes_is_fetched_again(tmp_path, pinned):
    (pinned / "a.bin").write_bytes(b"y" * 100)  # in place, but not the pinned file
    (pinned / "b.bin").write_bytes(b"x" * 300)
    hub = Hub({"a.bin": 100, "b.bin": 300}, spoil={"a.bin"})
    sleeps: list[float] = []
    f = make(tmp_path, hub, sleeps)
    assert not f.ready
    f.run()
    # the files are the pinned ones, nothing is listed; a download with other bytes is tried again
    assert hub.calls == ["a.bin", "a.bin"] and sleeps == [10]
    assert (pinned / "a.bin").read_bytes() == b"x" * 100
    assert f.ready


def test_a_note_from_before_the_pins_is_not_taken_on_trust(tmp_path, folder, monkeypatch):
    make(tmp_path, Hub({"a.bin": 100, "b.bin": 300})).run()
    (folder / "b.bin").write_bytes(b"y" * 300)
    monkeypatch.setattr(
        fetch, "PINS", {"test/model": fetch.Pin(REVISION, {"a.bin": sha(b"x" * 100), "b.bin": sha(b"x" * 300)})}
    )
    monkeypatch.setattr(
        fetch,
        "needed",
        lambda s: [Source("test/model", names=("a.bin", "b.bin"), revision=REVISION, local=str(folder))],
    )
    hub = Hub({"a.bin": 100, "b.bin": 300})
    again = make(tmp_path, hub)
    assert not again.ready
    again.run()
    assert hub.calls == ["b.bin"]
    # and the note made now is
    assert make(tmp_path, Hub({})).ready


def test_every_model_the_service_takes_is_pinned_file_by_file(tmp_path):
    for source in fetch.needed(Settings(data=tmp_path, tts="f5")):
        pin = fetch.PINS[source.repo]
        assert source.revision == pin.revision, source.repo
        names = [n for n in pin.sha256 if source.takes(n)]
        assert names and all(len(pin.sha256[n]) == 64 for n in names), source


def test_a_folder_is_listed_at_its_revision(monkeypatch):
    import huggingface_hub

    class Fs:
        def ls(self, path, revision=None):
            assert (path, revision) == ("org/m/nn", REVISION)
            return [{"type": "file", "name": f"org/m@{REVISION}/nn/a.onnx"}, {"type": "directory", "name": "x"}]

    monkeypatch.setattr(huggingface_hub, "HfFileSystem", Fs)
    files = fetch.HuggingFace().files(Source("org/m", folders=("nn",), revision=REVISION))
    assert [f.name for f in files] == ["nn/a.onnx"]


def test_the_loaders_ask_for_the_pinned_revision_on_disk(tmp_path, monkeypatch):
    import faster_whisper
    import huggingface_hub

    asked: list[tuple[str, str | None, bool]] = []

    def ask(repo, revision, local_files_only=False, **kw):
        asked.append((repo, revision, local_files_only))
        return repo

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", lambda repo, name, **kw: ask(repo, **kw))
    monkeypatch.setattr(faster_whisper, "WhisperModel", ask)
    s = Settings(data=tmp_path, tts="tone", accent="")
    m = models.load(s)
    m.llm._file(s.llm)
    m.listener._load()
    qwen = s.llm.partition(":")[0]
    assert asked == [
        (qwen, fetch.PINS[qwen].revision, True),
        (s.asr, fetch.PINS[fetch.whisper_repo(s.asr)].revision, True),
    ]


def hub_is_off() -> bool:
    """What the hub and transformers ask before going out, transformers before converting
    weights into safetensors."""
    import os

    from huggingface_hub import constants

    return constants.is_offline_mode() and os.environ["DISABLE_SAFETENSORS_CONVERSION"] == "1"


def test_offline_the_hub_takes_files_from_disk_and_asks_nothing(tmp_path):
    import huggingface_hub
    from huggingface_hub.errors import LocalEntryNotFoundError, OfflineModeIsEnabled
    from huggingface_hub.utils import get_session

    cache = tmp_path / "hf"
    there = cache / "models--org--m" / "snapshots" / REVISION / "model.bin"
    there.parent.mkdir(parents=True)
    there.write_bytes(b"x")
    assert not hub_is_off()
    fetch.offline()
    fetch.offline()  # again, as it comes
    assert hub_is_off()
    assert huggingface_hub.hf_hub_download("org/m", "model.bin", revision=REVISION, cache_dir=cache) == str(there)
    with pytest.raises(LocalEntryNotFoundError):  # missing is an error, not a download
        huggingface_hub.hf_hub_download("org/m", "config.json", revision=REVISION, cache_dir=cache)
    with pytest.raises(OfflineModeIsEnabled):
        get_session().get(f"https://huggingface.co/api/models/{takes.ALIGNER}")


def test_the_process_goes_offline_once_the_models_are_in_place(tmp_path, folder):
    class Watching(Hub):
        def download(self, file, seen):
            assert not hub_is_off(), "the fetch itself is online"
            super().download(file, seen)

    f = make(tmp_path, Watching({"a.bin": 1, "b.bin": 2}))
    f.run()
    assert f.ready and hub_is_off()


def test_a_start_with_the_models_in_place_goes_offline_at_once(tmp_path, folder):
    make(tmp_path, Hub({"a.bin": 1, "b.bin": 2})).run()
    from huggingface_hub import constants

    constants.HF_HUB_OFFLINE = False  # a new process
    again = make(tmp_path, Hub({"a.bin": 1, "b.bin": 2}))
    assert not hub_is_off()
    again.start()
    assert again._thread is None and hub_is_off()


def test_a_start_with_models_missing_stays_online_until_they_come(tmp_path, folder):
    gate = threading.Event()
    f = make(tmp_path, Hub({"a.bin": 1, "b.bin": 2}, gate=gate))
    f.start()
    assert not f.ready and not hub_is_off()
    gate.set()
    f._thread.join(10)
    assert f.ready and hub_is_off()


def test_offline_the_vocoder_f5_asks_from_main_is_the_pinned_one(tmp_path, monkeypatch):
    import huggingface_hub

    hub_download = huggingface_hub.hf_hub_download

    def download(repo_id, filename, revision, cache_dir, **kw):
        # what the hub leaves for a commit asked by its hash: its snapshot, no `main` to point at it
        where = Path(cache_dir) / f"models--{repo_id.replace('/', '--')}" / "snapshots" / revision / filename
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(filename)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    cache = tmp_path / "hub"
    pin = fetch.PINS[fetch.VOCODER].revision
    for name in fetch.VOCODER_FILES:
        fetch.HuggingFace().download(fetch.File(fetch.VOCODER, name, pin, str(cache)), lambda n: None)
    fetch.offline()
    for name in fetch.VOCODER_FILES:
        # as F5-TTS's load_vocoder asks: no revision
        found = Path(hub_download(fetch.VOCODER, name, cache_dir=cache))
        assert found.parent.name == pin and found.read_text() == name


def test_the_cli_voices_a_book_offline(tmp_path, monkeypatch):
    from audiobux import __main__ as cli
    from audiobux import pipeline

    seen: list[bool] = []
    monkeypatch.setattr(pipeline, "run", lambda book, ctx, until: seen.append(hub_is_off()))
    monkeypatch.setattr(cli, "context", lambda settings: None)
    source = tmp_path / "Книга.epub"
    source.write_bytes(b"x")
    cli.voice_one(Settings(data=tmp_path / "data"), source, None, None)
    assert seen == [True]
