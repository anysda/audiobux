"""The books and their queue: one table in `audiobux.db`, files in `library/<id>/`.

A book's row is its place in the queue and its state; `library/<id>/` holds the
source as it came (`source.<ext>`) and, once the book is voiced, `book.m4b`,
`book.epub`, `cover.jpg`, `sync.json` and the figures it points to in `figures/`.
Work in progress lives apart, in `work/<id>/`.
"""

import json
import shutil
import sqlite3
import threading
import time
import uuid
from typing import Any

from .config import Settings
from .contract import Book, BookDetails

SCHEMA = """
create table if not exists books (
    id text primary key,
    sha text not null,
    format text not null,
    title text not null default '',
    author text not null default '',
    state text not null default 'queued',
    step text,
    pieces_done integer not null default 0,
    pieces_total integer not null default 0,
    error text,
    duration real,
    chapters text not null default '[]',
    added real not null,
    updated real not null
);
create index if not exists books_sha on books (sha);
"""
# columns that came later: a database made before them gets them on the next start
ADDED = {
    "percent": "integer not null default 0",
    "eta": "integer",
    "worked": "real not null default 0",
    # the version of the reading the book was finished with, see `refresh`
    "parse": "integer not null default 0",
}

UNFINISHED = "('queued', 'running', 'paused')"


class Library:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # the web process's own folder too: it is mounted into a container that may not make it
        for path in (settings.incoming, settings.library, settings.work, settings.web):
            path.mkdir(parents=True, exist_ok=True)
        # one connection for the worker and the socket threads, taken in turn
        self._lock = threading.Lock()
        self.db = sqlite3.connect(settings.db, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode=wal")
        self.db.executescript(SCHEMA)
        has = {row["name"] for row in self.db.execute("pragma table_info(books)")}
        for name, kind in ADDED.items():
            if name not in has:
                self.db.execute(f"alter table books add column {name} {kind}")

    def _rows(self, sql: str, args: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    def _one(self, sql: str, args: tuple[object, ...] = ()) -> sqlite3.Row | None:
        found = self._rows(sql, args)
        return found[0] if found else None

    def add(self, sha: str, kind: str, title: str) -> str:
        book_id = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self.db.execute(
                "insert into books (id, sha, format, title, added, updated) values (?, ?, ?, ?, ?, ?)",
                (book_id, sha, kind, title, now, now),
            )
        return book_id

    def get(self, book_id: str) -> sqlite3.Row | None:
        return self._one("select * from books where id = ?", (book_id,))

    def with_sha(self, sha: str) -> sqlite3.Row | None:
        return self._one("select * from books where sha = ?", (sha,))

    def books(self) -> list[sqlite3.Row]:
        return self._rows("select * from books order by added, rowid")

    def next(self) -> sqlite3.Row | None:
        """The book to voice: one already started first, then the one that came first."""
        return self._one(f"select * from books where state in {UNFINISHED} order by step is null, added, rowid limit 1")

    def stale(self, version: int) -> sqlite3.Row | None:
        """A finished book read by a reading older than `version`."""
        return self._one(
            "select * from books where state = 'done' and parse < ? order by added, rowid limit 1", (version,)
        )

    def queue(self) -> int:
        row = self._one(f"select count(*) from books where state in {UNFINISHED}")
        return int(row[0]) if row else 0

    def requeue(self) -> None:
        """After a restart nothing is running: started books wait their turn, first in line."""
        with self._lock:
            self.db.execute("update books set state = 'queued' where state in ('running', 'paused')")

    def update(self, book_id: str, **fields: object) -> bool:
        """False when the book is gone."""
        fields["updated"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            return self.db.execute(f"update books set {cols} where id = ?", (*fields.values(), book_id)).rowcount > 0

    def forget(self, book_id: str) -> None:
        with self._lock:
            self.db.execute("delete from books where id = ?", (book_id,))

    def remove_files(self, book_id: str) -> None:
        shutil.rmtree(self.settings.library / book_id, ignore_errors=True)
        shutil.rmtree(self.settings.work / book_id, ignore_errors=True)

    def info(self, row: sqlite3.Row) -> Book:
        folder = self.settings.library / row["id"]
        done, total = row["pieces_done"], row["pieces_total"]
        return Book(
            id=row["id"],
            title=row["title"],
            author=row["author"],
            format=row["format"],
            state=row["state"],
            step=row["step"],
            pieces_done=done,
            pieces_total=total,
            percent=100 if row["state"] == "done" else row["percent"],
            eta=row["eta"],
            error=row["error"],
            added=row["added"],
            updated=row["updated"],
            duration=row["duration"],
            has_epub=(folder / "book.epub").exists(),
            has_cover=(folder / "cover.jpg").exists(),
        )

    def details(self, row: sqlite3.Row) -> BookDetails:
        chapters: list[Any] = json.loads(row["chapters"])
        return BookDetails(**self.info(row), chapters=chapters)
