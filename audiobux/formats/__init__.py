"""The book formats the service takes, told by the content of the file.

`KINDS` is the one list of formats: the upload check, the file picker and the
worker all take it from here. This module stays light (the standard library only)
so that taking the list costs nothing; the reader of a format, `formats/<name>.py`
with `read(path, out, depth) -> Document`, is imported only to read a book.
"""

import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

from .document import Document

# the first bytes of a file that its signature is looked for in
HEAD = 1024


def is_epub(path: Path) -> bool:
    """A zip with the EPUB container, or with `mimetype` saying EPUB, wherever the
    entry sits and however it is packed."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "META-INF/container.xml" in names:
                return True
            info = z.getinfo("mimetype") if "mimetype" in names else None
            return info is not None and info.file_size < 64 and z.read(info).strip() == b"application/epub+zip"
    except (zipfile.BadZipFile, OSError):
        return False


@dataclass(frozen=True)
class Kind:
    name: str  # the reader is the module of this name in this package
    extensions: tuple[str, ...]
    magic: bytes
    offset: int = 0
    # the magic may sit anywhere in this many first bytes instead of at `offset`
    within: int = 0
    # a closer look into the file once the magic is there
    check: Callable[[Path], bool] | None = None

    def matches(self, head: bytes, path: Path) -> bool:
        if self.within:
            found = self.magic in head[: self.within]
        else:
            found = head[self.offset : self.offset + len(self.magic)] == self.magic
        return found and (self.check is None or self.check(path))

    def read(self, path: Path, out: Path, depth: int) -> Document:
        doc: Document = import_module(f"{__name__}.{self.name}").read(path, out, depth)
        return doc


KINDS = [
    # a PDF reader looks for the header in the first kilobyte, as the standard allows
    Kind("pdf", (".pdf",), b"%PDF-", within=1024),
    Kind("djvu", (".djvu",), b"AT&TFORM"),
    Kind("epub", (".epub",), b"PK\x03\x04", check=is_epub),
]


def extensions() -> tuple[str, ...]:
    return tuple(e for kind in KINDS for e in kind.extensions)


def detect(path: Path) -> Kind | None:
    """The format of a file by what is in it; the name of the file does not count."""
    with path.open("rb") as f:
        head = f.read(HEAD)
    return next((kind for kind in KINDS if kind.matches(head, path)), None)


def read(path: Path, out: Path, depth: int) -> Document:
    """The book in `path`; its pictures and page images go into `out`, chapters
    follow the outline down to `depth`."""
    kind = detect(path)
    if kind is None:
        raise ValueError(f"unsupported format: {path.name}")
    return kind.read(path, out, depth)
