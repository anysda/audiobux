"""The worker: books from the queue, one at a time, and the socket the web process asks.

The models load on first use. Each book's progress lives in
`work/<id>/` (see `pipeline`), so a restarted worker, or a failed book tried
again, goes on from the step and the piece it stopped at.
"""

import contextlib
import hashlib
import json
import logging
import os
import queue
import shutil
import socketserver
import stat
import threading
import time
from pathlib import Path
from typing import Any

from . import contract, formats, pipeline
from .book import Book
from .config import Settings
from .contract import Fault
from .library import Library

log = logging.getLogger(__name__)

# seconds between looks at the queue when nothing wakes the worker
IDLE = 60
# progress goes to the database and to the events at most this often
PROGRESS_EVERY = 5.0


class Stopped(BaseException):
    """The book being voiced was deleted. Not an `Exception`, so no step catches it."""


class Worker:
    def __init__(self, settings: Settings, ctx: pipeline.Context) -> None:
        self.settings, self.ctx = settings, ctx
        self.library = Library(settings)
        self.library.requeue()
        # taking the next book and deleting one never cross
        self.lock = threading.Lock()
        self.current: tuple[str, Book] | None = None
        self.deleted = False
        self.subscribers: set[queue.Queue[contract.Event]] = set()
        self._saved = 0.0
        quiet = ctx.quiet
        quiet.on_check, quiet.on_pause, quiet.on_resume = self._check, self._pause, self._resume

    def run(self, until_idle: bool = False) -> None:
        """Voices the queue; with `until_idle`, returns once it is empty."""
        while True:
            left = self.ctx.quiet.seconds_left()
            with self.lock:
                row = None if left else self.library.next()
                if row:
                    self.current, self.deleted = (row["id"], self._workdir(row["id"], row["title"])), False
            if self.current:
                self.voice(*self.current)
                continue
            if until_idle and not self.library.queue():
                return
            self.ctx.quiet.sleep(left or IDLE)

    def voice(self, book_id: str, book: Book) -> None:
        self._set(book_id, state="running")
        step = "extract"
        try:
            for s in pipeline.STEPS:
                if not book.done(s.name):
                    step = s.name
                    self._set(book_id, step=step)
                    pipeline.run(book, self.ctx, until=step)
                    self._progress(book_id, book, about(book))
            step = "publish"
            self._publish(book_id, book)
            log.info("%s: done", book_id)
        except Stopped:
            log.info("%s: deleted while being voiced, stopped", book_id)
        except Exception as e:
            log.exception("%s: failed at %s", book_id, step)
            self._set(book_id, state="failed", step=step, error=f"{step}: {e}")
        finally:
            with self.lock:
                deleted, self.current = self.deleted, None
            if deleted:
                self.library.remove_files(book_id)

    def _workdir(self, book_id: str, title: str) -> Book:
        """The book's working directory, its source linked from the library."""
        book = Book(self.settings.work / book_id)
        book.dir.mkdir(exist_ok=True)
        source = next((self.settings.library / book_id).glob("source.*"))
        link = book.dir / source.name
        if not link.is_symlink():
            link.symlink_to(os.path.relpath(source, book.dir))
            # the name stands in for a title when the file has none of its own
            book.path("name").write_text((title or "Без названия") + source.suffix)
        return book

    def _publish(self, book_id: str, book: Book) -> None:
        folder = self.settings.library / book_id
        # the figures of the sync, the sync, the EPUB and the cover first, so they are
        # there by the time the m4b is
        if book.path("sync.json").exists():
            for chapter in book.read("sync.json")["chapters"]:
                for src in (b["src"] for b in chapter["blocks"] if b.get("src")):
                    (folder / src).parent.mkdir(exist_ok=True)
                    os.replace(book.path(src), folder / src)
        for name in ("sync.json", "book.epub", "cover.jpg", "book.m4b"):
            if book.path(name).exists():
                os.replace(book.path(name), folder / name)
        made = book.read("chapters.json")
        total, _ = pieces(book)
        self._set(
            book_id,
            state="done",
            step=None,
            error=None,
            pieces_done=total,
            pieces_total=total,
            duration=made["duration"],
            chapters=json.dumps(made["chapters"], ensure_ascii=False),
        )
        shutil.rmtree(book.dir, ignore_errors=True)

    # the checkpoints of the pipeline, see `quiet`

    def _check(self) -> None:
        if self.deleted:
            raise Stopped
        if self.current and time.monotonic() - self._saved >= PROGRESS_EVERY:
            self._progress(*self.current)

    def _pause(self) -> None:
        if self.current:
            self._progress(*self.current, state="paused")

    def _resume(self) -> None:
        if self.current:
            self._set(self.current[0], state="running")

    def _progress(self, book_id: str, book: Book, more: dict[str, str] | None = None, **fields: object) -> None:
        self._saved = time.monotonic()
        total, done = pieces(book)
        self._set(book_id, pieces_done=done, pieces_total=total, **(more or {}), **fields)

    def _set(self, book_id: str, **fields: object) -> None:
        if self.library.update(book_id, **fields):
            row = self.library.get(book_id)
            if row:
                self._emit(contract.Event(type="book", book=self.library.info(row)))

    def _emit(self, event: contract.Event) -> None:
        for q in list(self.subscribers):
            # a reader this far behind loses events rather than the worker its memory
            if q.qsize() < 1000:
                q.put(event)

    # the socket

    def serve(self) -> socketserver.UnixStreamServer:
        """Starts answering on the socket in the background."""
        path = self.settings.worker_socket
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        server = _Server(str(path), _Handler)
        server.worker = self
        os.chmod(path, 0o600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def answer(self, method: str, params: dict[str, str]) -> object:
        if method == "book.submit":
            return self.submit(params["upload_id"], params["title"])
        if method == "book.delete":
            return self.delete(params["id"])
        if method == "library.list":
            return [self.library.info(row) for row in self.library.books()]
        if method == "book.get":
            return self.library.details(self._row(params["id"]))
        return self.status()

    def submit(self, upload_id: str, title: str = "") -> contract.Submitted:
        """The upload becomes a book in the queue, or the known book with the same file.
        `title` (checked by `contract.parse`) is the book's until its text says otherwise."""
        upload = self.settings.incoming / upload_id
        try:
            if not stat.S_ISREG(upload.lstat().st_mode):
                raise Fault("not-found")
            # out of the web process's reach before anything looks into it
            taken = self.settings.work / f"{upload_id}.upload"
            os.replace(upload, taken)
        except FileNotFoundError:
            raise Fault("not-found") from None
        try:
            kind = formats.detect(taken) if stat.S_ISREG(taken.lstat().st_mode) else None
            if kind is None:
                raise Fault("not-a-book")
            sha = _sha256(taken)
            with self.lock:
                known = self.library.with_sha(sha)
                if known:
                    return contract.Submitted(id=known["id"], known=True)
                book_id = self.library.add(sha, kind.name, title)
                folder = self.settings.library / book_id
                folder.mkdir()
                os.replace(taken, folder / ("source" + kind.extensions[0]))
        finally:
            taken.unlink(missing_ok=True)
        log.info("%s: queued, %s", book_id, kind.name)
        self._set(book_id)
        self.ctx.quiet.interrupt()
        return contract.Submitted(id=book_id, known=False)

    def delete(self, book_id: str) -> contract.Done:
        """Gone at once from the list; a book being voiced stops at the next piece."""
        with self.lock:
            self._row(book_id)
            self.library.forget(book_id)
            if self.current and self.current[0] == book_id:
                self.deleted = True
                self.ctx.quiet.interrupt()
            else:
                self.library.remove_files(book_id)
        self._emit(contract.Event(type="deleted", id=book_id))
        return contract.Done(id=book_id)

    def status(self) -> contract.Status:
        quiet = self.ctx.quiet
        return contract.Status(
            queue=self.library.queue(),
            quiet=contract.Quiet(window=quiet.spec or None, tz=self.settings.tz, active=bool(quiet.seconds_left())),
            formats=[contract.Format(name=k.name, extensions=list(k.extensions)) for k in formats.KINDS],
        )

    def stream(self, out: Any) -> None:
        """Events to one reader until it goes."""
        q: queue.Queue[contract.Event] = queue.Queue()
        self.subscribers.add(q)
        try:
            out.write(contract.answer({}))
            while True:
                try:
                    event = q.get(timeout=contract.PING_EVERY)
                except queue.Empty:
                    event = contract.Event(type="ping")
                out.write(contract.notice(event))
        except OSError:
            pass
        finally:
            self.subscribers.discard(q)

    def _row(self, book_id: str) -> Any:
        row = self.library.get(book_id)
        if row is None:
            raise Fault("not-found")
        return row


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    worker: Worker


class _Handler(socketserver.StreamRequestHandler):
    timeout = contract.READ_TIMEOUT
    server: _Server

    def handle(self) -> None:
        with contextlib.suppress(OSError):  # the caller went away
            self.wfile.write(self._answer())

    def _answer(self) -> bytes:
        try:
            line = self.rfile.readline(contract.REQUEST_LIMIT + 1)
        except TimeoutError:
            return contract.refusal("timeout")
        if len(line) > contract.REQUEST_LIMIT:
            # the rest is not read: the connection closes on it
            return contract.refusal("too-large")
        try:
            if not line.endswith(b"\n"):
                raise Fault("bad-request")
            method, params = contract.parse(line)
            if method == "events.subscribe":
                self.server.worker.stream(self.wfile)
                return b""
            return contract.answer(self.server.worker.answer(method, params))
        except Fault as f:
            return contract.refusal(f.error)
        except Exception:
            log.exception("socket: %s failed", method)
            return contract.refusal("internal")


def about(book: Book) -> dict[str, str]:
    """Title and author as the book's text has them, once it has been read."""
    for name in ("text.json", "scan.json"):
        if book.path(name).exists():
            data = book.read(name)
            return {"title": data.get("title", ""), "author": data.get("author", "")}
    return {}


def pieces(book: Book) -> tuple[int, int]:
    """(pieces in the book, pieces voiced)."""
    if not book.path("chunks.json").exists():
        return 0, 0
    total = len(book.read("chunks.json"))
    done = sum(1 for p in book.path("tts").glob("*.wav") if not p.name.endswith(".tmp.wav"))
    return total, min(done, total)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
