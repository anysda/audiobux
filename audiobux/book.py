"""A book's working directory: the source file, step outputs and done marks."""

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any


class Book:
    def __init__(self, path: Path) -> None:
        self.dir = Path(path)

    @classmethod
    def create(cls, work: Path, name: str, fetch: Callable[[Path], object]) -> "Book":
        """Working directory for a book file; reused if it is already there.
        `fetch(path)` writes the source file to `path` the first time."""
        book = cls(Path(work) / slug(Path(name).stem))
        book.dir.mkdir(parents=True, exist_ok=True)
        target = book.dir / ("source" + Path(name).suffix.lower())
        if not target.exists():
            fetch(book.dir / "source.part")
            (book.dir / "source.part").rename(target)
        (book.dir / "name").write_text(name)
        return book

    @property
    def name(self) -> str:
        f = self.dir / "name"
        return f.read_text() if f.exists() else self.dir.name

    @property
    def source(self) -> Path:
        return next(p for p in self.dir.glob("source.*") if p.suffix != ".part")

    def path(self, name: str) -> Path:
        return self.dir / name

    def done(self, step: str) -> bool:
        return (self.dir / f"{step}.done").exists()

    def mark(self, step: str) -> None:
        (self.dir / f"{step}.done").touch()

    def read(self, name: str) -> Any:
        return json.loads(self.path(name).read_text())

    def write(self, name: str, data: object) -> None:
        tmp = self.path(name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        os.replace(tmp, self.path(name))


def slug(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name).strip("_")[:120] or "book"
