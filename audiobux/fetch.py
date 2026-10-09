"""The models the settings need, fetched before the first book, with the bytes counted.

Every model still loads itself on first use (`f5`, `takes`, `llm`, `models.Whisper`,
RUAccent in `speech`) and would download what it misses then. Fetching it all here,
into the same folders and by the same names, means no book stalls on a download
and the interface can show how far it is. A source that is all in place gets a note
in `models/.fetched/` with its files, so a restart with the models there touches
neither the network nor the files.

Every model is pinned: the revision on the hub and the sha256 of each file, as the
service was tried with them. A file with other bytes is not in place: it is fetched
again, before and after a download alike.
"""

import fnmatch
import functools
import hashlib
import inspect
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from tqdm import tqdm

from . import f5, takes
from .config import Settings
from .llm import hub_file
from .runtime import Runtime

log = logging.getLogger(__name__)

# tqdm is generic in its stubs only
if TYPE_CHECKING:
    _Bar = tqdm[Any]
else:
    _Bar = tqdm

# what F5-TTS's load_vocoder takes into the default Hugging Face cache; the name lives
# inside that function, there is nothing to import
VOCODER = "charactr/vocos-mel-24khz"
VOCODER_FILES = ("config.yaml", "pytorch_model.bin")
# what transformers reads of the aligner at its revision: the processor's json and the weights
ALIGNER_FILES = ("*.json", "pytorch_model.bin")
# what faster_whisper.utils.download_model takes of a model's repo, a list inside that function
WHISPER_FILES = ("config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*")
# seconds before trying again after a failure, the last one for every failure after
PAUSES = (10, 30, 60, 300)
# the revision of each model's repo, then its files as `sha256sum` prints them
PINNED = f"""
{f5.REPO} {f5.REVISION}
2a05f992e00af9b0bd3800a8d23e78d520dbd705284ed2eedb5f4bd29398fa3c  F5TTS_v1_Base/vocab.txt
a8b94156ad14cee4ad7e0451673b8a7ac633c0bc099964e49bdaa54223b09566  F5TTS_v1_Base_v4_winter/model_212000.safetensors
charactr/vocos-mel-24khz 0feb3fdd929bcd6649e0e7c5a688cf7dd012ef21
da9033922f969a47f0c160010226919e59f27761fd5066f3828d46de6650b0fc  config.yaml
97ec976ad1fd67a33ab2682d29c0ac7df85234fae875aefcc5fb215681a91b2a  pytorch_model.bin
{takes.ALIGNER} {takes.ALIGNER_REVISION}
3cde6d3da15a0573fd80c8c730960acd712cbafef3d1e32809eee9dbf30520ce  alphabet.json
9f02bcac826a79be1bc20cff20fb2ed830f3538ec282c65050f9195d61499deb  config.json
ca5999a45e98bb76ea87a461ba28a23ad32a5bb9f733b8e0f6546ff38b6c612d  preprocessor_config.json
d1cdb1a7921de7d363f967a9b0101a713602e109dba62b6f3f9ae2e0b2df0c1c  pytorch_model.bin
bb7068de1150661a10b55f9e4b12a0e77af8bf91f5e45e1b58afaf1d0e17f675  special_tokens_map.json
3def5e6b6c6fcbe9a674e58b8f7f9e608742414db0188ec50bb3ac565612f0d7  vocab.json
unsloth/Qwen3.6-35B-A3B-GGUF a483e9e6cbd595906af30beda3187c2663a1118c
ac0e2c1189e055faa36eff361580e79c5bd6f8e76bffb4ce547f167d53e31a61  Qwen3.6-35B-A3B-UD-Q4_K_M.gguf
8971ee4f331ff0a4c609374f32984b3d4e6dc086c0aa35f1d637fad1829e887f  mmproj-F16.gguf
mobiuslabsgmbh/faster-whisper-large-v3-turbo 0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf
b0253ea6c0d3bea6b1e19e91a02acfd3b53f4467362efcb5a3e6b16c9b3a9b7e  config.json
e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da  model.bin
7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711  preprocessor_config.json
297b13372ac43916285644fb9687add3cc62ee2a1adb60da3dc25cc94c1871fd  tokenizer.json
c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1  vocabulary.json
ruaccent/accentuator b78ae5ea1e62beaf138bed1865cd8c3b0b5ca855
aa460ebba90de00fbbf3d41d121961f605b98667e45efb7920f127473b15515e  dictionary/accents.json.gz
8395664000b80c1afe09bfea3650945b0933482b8e3dee5bb9d429eb18c44935  dictionary/accents_nn.json.gz
04a9e81c68d65f65ba493fe0110f99e79087548c2beeec3032e2b66e28706f36  dictionary/omographs.json.gz
74a749fac2a9fb82b4faebac3bb901217686bcd6365294dc3e304a2c44a2530d  dictionary/rule_engine/accents.json
89a4c3c7529e299df58a0460589bf658a4f5427d9678ab78720320cf9ad3f3da  dictionary/rule_engine/forms.json
c4ee777bbbab87f9eac838f370ad92974e079d02b21903e480c54b5f0c8c60d1  dictionary/yo_homographs.json.gz
a19fa89a964a0691d9fe4ee384783e3934904891843d8f59a1c480d67947a82a  dictionary/yo_words.json.gz
bf2b3ef3ff7a0aa6e4250aa4e9c8ed568e25f825deebdb12dee1b46b785ba9fc  koziev/rulemma/rulemma.dat
61c0ae4ea718fc85000284dcbf798fb51657c41f3db3ae029e8bebfb33075f44  koziev/rulemma/rulemma.py
68f14d0db5bc92e4da7363ca6c2c34812b18a87bdfe07714329bcfd711bdbe11  koziev/rupostagger/__init__.py
a06848e656bef642aafb4440c03554fa78f2f32dde92ea66f3f86ce9977b167e  koziev/rupostagger/database/ruword2tags.db
c8c19d1cd92855bc281b36ae80c3b10ed20d7f0aeeca54467c8fd00df319c614  koziev/rupostagger/rupostagger.config
21b7b0bfd7427b5fdc1604052176db8aa3b139b3ce03be440cfce48536f8e5ef  koziev/rupostagger/rupostagger.model
111b8c22db18407fb34bcd6e00b3c2245165de98ddec0fdb40862d82af864530  koziev/rupostagger/rupostagger.py
331fdd0d9d10025bbebfc09ae648b21399d9f73cd168e1eeb76c10d6b0a31c00  koziev/rupostagger/rusyllab.py
dde47b5f1d48ff899887ac07812dcabd2966e48e84646f3065bfd06627c2af58  koziev/rupostagger/ruword2tags.dat
2858ad021915f3fe5bf9589ab1773f8c2cdd664f053f3f6bf317fec3fa3ed23b  koziev/rupostagger/ruword2tags.py
47e69d9ae19f2a82e21b1c70f6a4bbfb1abc5759e98b2e67d009c5e9d7af18c9  nn/nn_accent/big.onnx
e377f159a8fc8673e211f100d4a237d5fe3dceb3b6789a5a738bf50bda23be49  nn/nn_accent/config.json
4e393144e45626f6f1062a0784ef06f921b97321a8e7b87ac2a09a892286500a  nn/nn_accent/model.onnx
31dd1c06997b2c5097b558829e46b4e91a5ef8e5c6bea07dfc67e48b3bbc8775  nn/nn_accent/ort_config.json
2acac3fb89054158ccbeff3c52744ca93979f738002709c0aea001597213024b  nn/nn_accent/special_tokens_map.json
efea18a18af2809330eb0f431ab86979304d2559571e887de7b6359e3f18f0d8  nn/nn_accent/tokenizer_config.json
242882b2f27800e49a6babf46867dabf6dc9b6535b600776a68815fe4d4f4382  nn/nn_accent/vocab.txt
071c01df9ba59b64b9d0d9af0eaac5412a5fc558d540a214ada4ee7531d38096  nn/nn_omograph/turbo3.1/added_tokens.json
fa47e006d2a15a164d36e0303b5f8344a795f2108a3e55724133a87710216136  nn/nn_omograph/turbo3.1/config.json
120c75258d87ba748b2025f517819fd0e0386e8d31cc6b29a21f900d395dd825  nn/nn_omograph/turbo3.1/merges.txt
2cb6a174c4cdb45bd3132b4f7c8a3779fc4b6869863180ed7d0e421bcd453dbd  nn/nn_omograph/turbo3.1/model.onnx
06e405a36dfe4b9604f484f6a1e619af1a7f7d09e34a8555eb0b77b66318067f  nn/nn_omograph/turbo3.1/special_tokens_map.json
eb84a77af38a91f6f327486c7827d8541f8d446250f8d408909999b6be817afa  nn/nn_omograph/turbo3.1/tokenizer.json
f159df92d0b0be03e1ceb0768222aa4c21dcfd37c5763b6b395b6864e023c7fb  nn/nn_omograph/turbo3.1/tokenizer_config.json
ba028ba89c029a473ac369716b4d52bb252a38004fc5dd085d18f4ada4a760b3  nn/nn_omograph/turbo3.1/vocab.json
aa0638edf56254f123e593e96f034c2b5e68303ac207b29f64f0878dc4c22631  nn/nn_stress_usage_predictor/config.json
3d547500637b4ddfec8880ed6d1405fd50ee9d3f0131ef8a2a69dcf961dbefeb  nn/nn_stress_usage_predictor/model.onnx
b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3  nn/nn_stress_usage_predictor/special_tokens_map.json
ce51cde5df60ecb167abec4c90db11360482150628f7a91f3e316df6d6ff6f6f  nn/nn_stress_usage_predictor/tokenizer.json
4466c2e475c434d35bce0f4a46f918730155386b50582fa3742b7b7d67897e96  nn/nn_stress_usage_predictor/tokenizer_config.json
f056a69b097422652053bf87565c35543e5d81540ca4b7dddd28de4157a969e0  nn/nn_stress_usage_predictor/vocab.txt
adf7fd2c67be1498071f447ec4a23dbe84904c49f95d1d3e6c7d9df269b9a2e9  nn/nn_yo_homograph_resolver/config.json
42cc85bf0c4b319dfe3d89fa17b162a92fd5e1c651a657cb3d5f44978d4e70ac  nn/nn_yo_homograph_resolver/model.onnx
b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3  nn/nn_yo_homograph_resolver/special_tokens_map.json
f2bf174f2fe62ff11c28d56555f9c0cf16b24e162ef53bcf42ff85ebcf6ebfa8  nn/nn_yo_homograph_resolver/tokenizer.json
804b8a4b4a02c9d38af8c88706206718759061ff9a102ad8483e9ac00011188c  nn/nn_yo_homograph_resolver/tokenizer_config.json
9276b81ff34cd9560563a476de6735abf5ed9a3871131644992230dc4e630056  nn/nn_yo_homograph_resolver/vocab.txt
"""


@dataclass(frozen=True)
class Pin:
    revision: str
    sha256: dict[str, str]


def _pins(text: str) -> dict[str, Pin]:
    pins: dict[str, Pin] = {}
    for line in text.strip().splitlines():
        first, second = line.split()
        if "/" in first:
            repo, pins[first] = first, Pin(second, {})
        else:
            pins[repo].sha256[second] = first
    return pins


PINS = _pins(PINNED)


def revision(repo: str) -> str | None:
    """The revision a loader asks the hub for: the pinned one, or the latest for a model
    the settings name outside the pins."""
    pin = PINS.get(repo)
    return pin.revision if pin else None


def whisper_repo(asr: str) -> str:
    """The repo faster-whisper takes a model of this size or name from. A pinned one is
    known without faster-whisper, which may not be installed yet (see `runtime`); one
    outside the pins has no revision to ask for either way."""
    pinned = [repo for repo in PINS if repo.endswith("/faster-whisper-" + asr)]
    if pinned:
        return pinned[0]
    try:
        from faster_whisper.utils import _MODELS
    except ModuleNotFoundError:
        return asr
    return str(_MODELS.get(asr, asr))


@dataclass(frozen=True)
class File:
    repo: str
    name: str
    revision: str | None = None
    # a Hugging Face cache folder (None is the default one), or a plain folder as RUAccent keeps
    cache: str | None = None
    local: str | None = None
    # what the bytes must hash to; None for a model outside the pins
    sha256: str | None = None


@dataclass(frozen=True)
class Source:
    """One model's files in one repo: named, in folders, or matching names at the top."""

    repo: str
    names: tuple[str, ...] = ()
    folders: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    revision: str | None = None
    cache: str | None = None
    local: str | None = None

    @property
    def key(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]

    def file(self, name: str) -> File:
        pin = PINS.get(self.repo)
        sha = pin.sha256.get(name) if pin and pin.revision == self.revision else None
        return File(self.repo, name, self.revision, self.cache, self.local, sha)

    def takes(self, name: str) -> bool:
        folder, _, base = name.rpartition("/")
        top = not folder and any(fnmatch.fnmatch(base, p) for p in self.patterns)
        return name in self.names or folder in self.folders or top


class Hub(Protocol):
    def files(self, source: Source) -> list[File]:
        """The source's files; may ask the network."""

    def measure(self, file: File) -> tuple[int, bool]:
        """(size in bytes, whether it is in place already); may ask the network."""

    def path(self, file: File) -> Path | None:
        """Where the file is in place, without the network; None when it is not."""

    def download(self, file: File, seen: Callable[[int], None]) -> None:
        """Puts the file in place; `seen` gets the bytes of it there so far."""


class HuggingFace:
    """The Hugging Face hub, the way the loaders themselves call it."""

    def files(self, source: Source) -> list[File]:
        names = list(source.names)
        if source.folders:
            from huggingface_hub import HfFileSystem

            fs = HfFileSystem()
            for folder in source.folders:
                for e in fs.ls(f"{source.repo}/{folder}", revision=source.revision):
                    if isinstance(e, dict) and e["type"] == "file":
                        # `org/name/...`, or `org/name@revision/...` at a revision
                        names.append(e["name"].split("/", 2)[2])
        if source.patterns:
            from huggingface_hub import HfApi

            top = [n for n in HfApi().list_repo_files(source.repo, revision=source.revision) if "/" not in n]
            names += [n for n in top if any(fnmatch.fnmatch(n, p) for p in source.patterns)]
        return [source.file(n) for n in names]

    def measure(self, file: File) -> tuple[int, bool]:
        from huggingface_hub import hf_hub_download

        info = hf_hub_download(**_where(file), dry_run=True)
        assert not isinstance(info, str)
        return info.file_size, not info.will_download

    def path(self, file: File) -> Path | None:
        if file.local:
            where = Path(file.local) / file.name
            return where if where.is_file() else None
        from huggingface_hub import try_to_load_from_cache

        found = try_to_load_from_cache(file.repo, file.name, file.cache, file.revision)
        return Path(found) if isinstance(found, str) else None

    def download(self, file: File, seen: Callable[[int], None]) -> None:
        from huggingface_hub import hf_hub_download

        hf_hub_download(**_where(file), tqdm_class=_counting(seen))


def _where(file: File) -> dict[str, Any]:
    return {
        "repo_id": file.repo,
        "filename": file.name,
        "revision": file.revision,
        "cache_dir": file.cache,
        "local_dir": file.local,
    }


class _Nowhere:
    def write(self, text: str) -> None:
        pass


def _counting(seen: Callable[[int], None]) -> type[_Bar]:
    """A progress bar that draws nothing and tells `seen` how far it is. The hub may run
    two for one file (bytes over the network, the file put together), so it is the
    further one that counts."""

    class Counting(_Bar):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **{**kwargs, "disable": False, "file": _Nowhere()})

        def update(self, n: float | None = 1) -> bool | None:
            shown = super().update(n)
            seen(int(self.n))
            return shown

    return Counting


def needed(s: Settings) -> list[Source]:
    """What the models of `models.load` will ask for with these settings."""
    hf = str(s.models / "hf")
    found: list[Source] = []
    if s.tts == "f5":
        found += [
            Source(f5.REPO, names=(f5.CHECKPOINT, f5.VOCAB), revision=f5.REVISION, cache=hf),
            Source(VOCODER, names=VOCODER_FILES),
            Source(takes.ALIGNER, patterns=ALIGNER_FILES, revision=takes.ALIGNER_REVISION, cache=hf),
        ]
    # (spec, the model it belongs to), as `Llm` takes them; a path to a file is not fetched
    specs = [(s.ocr, s.ocr), (s.ocr_vision, s.ocr)] if s.ocr else []
    specs += [(s.llm, s.llm)] if s.llm else []
    for repo, name in dict.fromkeys(hub_file(*x) for x in specs if x[0] and not Path(x[0]).is_file()):
        found.append(Source(repo, names=(name,), cache=hf))
    if s.asr and not Path(s.asr).is_dir():
        found.append(Source(whisper_repo(s.asr), patterns=WHISPER_FILES, cache=str(s.models / "whisper")))
    if s.accent:
        found += _accent(s.accent, s.models / "ruaccent")
    return [x if x.revision else replace(x, revision=revision(x.repo)) for x in found]


def _accent(size: str, workdir: Path) -> list[Source]:
    """RUAccent's folders, as its load() lists them: the dictionary and the models into
    the workdir, the parser it takes from Koziev into its own package folder."""
    import ruaccent
    from ruaccent import RUAccent

    accent = RUAccent()
    repo = inspect.signature(RUAccent.load).parameters["repo"].default
    paths = [*accent.accentuator_paths, accent.omograph_models_paths[size]]
    found = [Source(repo, folders=tuple(p.strip("/") for p in paths), local=str(workdir))]
    package = Path(ruaccent.__file__).parent
    # the image brings it; RUAccent fetches it only when the folder is not there
    if not (package / "koziev").exists():
        found.append(Source(repo, folders=tuple(p.strip("/") for p in accent.koziev_paths), local=str(package)))
    return found


class Fetch:
    """Installs the runtime the models run on, then fetches what `needed` lists, in a
    thread of its own, trying again after a failure. What the models need is known only
    with the runtime in place: RUAccent tells it."""

    def __init__(
        self,
        settings: Settings,
        hub: Hub | None = None,
        on_ready: Callable[[], None] = lambda: None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.hub = hub or HuggingFace()
        self.settings, self.notes = settings, settings.models / ".fetched"
        self.on_ready, self.sleep = on_ready, sleep
        self.runtime = Runtime(Path(settings.runtime), settings.data / "runtime") if settings.runtime else None
        if self.runtime and self.runtime.ready:
            self.runtime.activate()
        self.pending = self._pending() if self._installed else []
        # bytes in place and bytes in all, once the files are known
        self.done = 0
        self.total: int | None = None
        self._part = 0
        self._thread: threading.Thread | None = None

    @property
    def _installed(self) -> bool:
        return self.runtime is None or self.runtime.ready

    @property
    def ready(self) -> bool:
        return self._installed and not self.pending

    def progress(self) -> tuple[int | None, int | None]:
        """(done, total) bytes while installing and fetching, else (None, None); the
        runtime's bytes count in, ahead of the models'."""
        r = self.runtime
        if self.ready:
            return None, None
        if r and not r.ready:
            return (None, None) if r.total is None else (r.done, r.total)
        if self.total is None:
            return None, None
        ahead = (r.total or 0) if r else 0
        return ahead + min(self.done + self._part, self.total), ahead + self.total

    def start(self) -> None:
        if not self.ready and self._thread is None:
            self._thread = threading.Thread(target=self.run, name="fetch", daemon=True)
            self._thread.start()

    def run(self) -> None:
        if self.runtime and not self.runtime.ready:
            self._again(self.runtime.install)
            self.runtime.activate()
            self.pending = self._pending()
        plan = self._again(self._plan)
        self.done = sum(size for files in plan.values() for _, size, there in files if there)
        self.total = sum(size for files in plan.values() for _, size, _ in files)
        log.info("models: %.1f of %.1f GB in place", self.done / 1e9, self.total / 1e9)
        for source, files in plan.items():
            for file, size, there in files:
                if not there:
                    self._again(functools.partial(self._download, file, size))
            self._note(source, [f for f, _, _ in files])
            self.pending.remove(source)
        log.info("models: all in place")
        self.on_ready()

    def _pending(self) -> list[Source]:
        return [s for s in needed(self.settings) if not self._noted(s)]

    def _plan(self) -> dict[Source, list[tuple[File, int, bool]]]:
        plan: dict[Source, list[tuple[File, int, bool]]] = {}
        for source in self.pending:
            plan[source] = []
            for file in self._files(source):
                size, there = self.hub.measure(file)
                if there and not self._sound(file):
                    log.warning("models: %s/%s is not the pinned file, fetching it again", file.repo, file.name)
                    self._drop(file)
                    there = False
                plan[source].append((file, size, there))
        return plan

    def _files(self, source: Source) -> list[File]:
        """A pinned source's files are known without the hub."""
        pin = PINS.get(source.repo)
        if pin and pin.revision == source.revision:
            return [source.file(n) for n in pin.sha256 if source.takes(n)]
        return self.hub.files(source)

    def _download(self, file: File, size: int) -> None:
        def seen(n: int) -> None:
            self._part = max(self._part, min(n, size))

        log.info("models: %s/%s, %.1f GB", file.repo, file.name, size / 1e9)
        try:
            self.hub.download(file, seen)
        finally:
            self._part = 0
        if not self._sound(file):
            self._drop(file)
            raise ValueError(f"{file.repo}/{file.name} came with a sha256 other than the pinned one")
        self.done += size

    def _sound(self, file: File) -> bool:
        """In place and, for a pinned file, the very bytes."""
        path = self.hub.path(file)
        if path is None:
            return False
        if file.sha256 is None:
            return True
        with path.open("rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest() == file.sha256

    def _drop(self, file: File) -> None:
        """The file and, in a hub cache, the blob it links to: the hub would take it as there."""
        path = self.hub.path(file)
        if path is not None:
            path.resolve().unlink(missing_ok=True)
            path.unlink(missing_ok=True)

    def _again[T](self, fn: Callable[[], T]) -> T:
        """fn() until it works: the network comes back, the worker does not fall."""
        failures = 0
        while True:
            try:
                return fn()
            except Exception as e:
                pause = PAUSES[min(failures, len(PAUSES) - 1)]
                failures += 1
                log.warning("fetch: %s: %s, again in %d s", type(e).__name__, e, pause)
                self.sleep(pause)

    def _noted(self, source: Source) -> bool:
        try:
            files = [File(**f) for f in json.loads((self.notes / f"{source.key}.json").read_text())]
        except (OSError, ValueError, TypeError):
            return False
        # a note from other pins, or from before them, says nothing of the bytes
        return all(f == source.file(f.name) and self.hub.path(f) is not None for f in files)

    def _note(self, source: Source, files: list[File]) -> None:
        self.notes.mkdir(parents=True, exist_ok=True)
        tmp = self.notes / f"{source.key}.tmp"
        tmp.write_text(json.dumps([asdict(f) for f in files], indent=1))
        tmp.replace(self.notes / f"{source.key}.json")
