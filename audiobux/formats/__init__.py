"""The book formats the service takes, told by the content of the file.

`KINDS` is the one list of formats: the upload check, the file picker and the
worker all take it from here. This module stays light (the standard library only)
so that taking the list costs nothing; the reader of a format, `formats/<name>.py`
with `read(path, out, depth) -> Document`, is imported only to read a book.
"""

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

from .document import Document

# the first bytes of a file that its signature is looked for in
HEAD = 1024


@dataclass(frozen=True)
class Kind:
    name: str  # the reader is the module of this name in this package
    extensions: tuple[str, ...]
    magic: bytes
    offset: int = 0

    def matches(self, head: bytes) -> bool:
        return head[self.offset : self.offset + len(self.magic)] == self.magic

    def read(self, path: Path, out: Path, depth: int) -> Document:
        doc: Document = import_module(f"{__name__}.{self.name}").read(path, out, depth)
        return doc


KINDS = [Kind("pdf", (".pdf",), b"%PDF-"), Kind("epub", (".epub",), b"PK\x03\x04")]


def extensions() -> tuple[str, ...]:
    return tuple(e for kind in KINDS for e in kind.extensions)


def detect(path: Path) -> Kind | None:
    """The format of a file by what is in it; the name of the file does not count."""
    with path.open("rb") as f:
        head = f.read(HEAD)
    return next((kind for kind in KINDS if kind.matches(head)), None)


def read(path: Path, out: Path, depth: int) -> Document:
    """The book in `path`; its pictures and page images go into `out`, chapters
    follow the outline down to `depth`."""
    kind = detect(path)
    if kind is None:
        raise ValueError(f"unsupported format: {path.name}")
    return kind.read(path, out, depth)
