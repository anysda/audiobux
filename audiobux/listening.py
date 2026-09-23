"""Where the listener is in every book: the web process's own small database.

The worker knows nothing of it. It lives in `<data>/web/listening.db`, the one
place the web process writes to besides `incoming/`; a book's row goes when the
book is deleted. Whether a book exists is the worker's word, asked before this.
"""

import sqlite3
import threading
from pathlib import Path
from typing import Any, TypedDict

SCHEMA = """
create table if not exists listening (
    id text primary key,
    position real not null default 0,
    listened integer not null default 0
);
"""


class Place(TypedDict):
    id: str
    # seconds from the start of the m4b
    position: float
    listened: bool


class Listening:
    def __init__(self, path: Path) -> None:
        self.path = path
        # opened on first use, so that making the app (for its API description) touches no disk
        self._db: sqlite3.Connection | None = None
        # one connection for the threads of the web process, taken in turn
        self._lock = threading.Lock()

    def _run(self, sql: str, args: tuple[object, ...] = ()) -> list[Any]:
        with self._lock:
            if self._db is None:
                # the web process may start before the worker has made the data folders
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
                self._db.executescript(SCHEMA)
            return self._db.execute(sql, args).fetchall()

    def all(self) -> list[Place]:
        rows = self._run("select id, position, listened from listening order by id")
        return [Place(id=i, position=p, listened=bool(heard)) for i, p, heard in rows]

    def get(self, book_id: str) -> Place:
        """A book never listened to is at its start."""
        rows = self._run("select position, listened from listening where id = ?", (book_id,))
        position, heard = rows[0] if rows else (0.0, 0)
        return Place(id=book_id, position=position, listened=bool(heard))

    def move(self, book_id: str, seconds: float) -> Place:
        self._run(
            "insert into listening (id, position) values (?, ?) on conflict (id) do update set position = ?",
            (book_id, seconds, seconds),
        )
        return self.get(book_id)

    def mark(self, book_id: str, listened: bool) -> Place:
        self._run(
            "insert into listening (id, listened) values (?, ?) on conflict (id) do update set listened = ?",
            (book_id, listened, listened),
        )
        return self.get(book_id)

    def forget(self, book_id: str) -> None:
        self._run("delete from listening where id = ?", (book_id,))
