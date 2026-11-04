"""The worker: books from the queue, one at a time, and the socket the web process asks.

The models are fetched by the fetch service (see `fetch`): until they are in
place the queue waits. They load on first use and are let go after every step and
for the quiet hours, so an idle worker holds nothing on the card. Each book's progress lives in
`work/<id>/` (see `pipeline`), so a restarted worker, or a failed book tried
again, goes on from the step and the piece it stopped at.
"""

import contextlib
import gc
import hashlib
import json
import logging
import math
import os
import queue
import shutil
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import contract, fetch, formats, pipeline, reading, refresh
from .book import Book
from .config import Settings
from .contract import Fault
from .library import Library

log = logging.getLogger(__name__)

# seconds between looks at the queue when nothing wakes the worker
IDLE = 60
# progress goes to the database and to the events at most this often
PROGRESS_EVERY = 5.0

STEPS_RU = {
    "extract": "извлечение текста",
    "ocr": "распознавание сканов",
    "figures": "описание рисунков",
    "fix": "правка текста",
    "epub": "сборка epub",
    "normalize": "подготовка текста к чтению",
    "chunk": "нарезка на куски",
    "tts": "озвучка",
    "verify": "проверка на слух",
    "assemble": "сборка m4b",
    "sync": "привязка текста к звуку",
    "publish": "перенос в библиотеку",
}

# what a step takes: (seconds, seconds per 1000 characters of the book's text), as a
# 132-page book of 256 639 characters took them on the service's own card, 3 h in all
COST = {
    "extract": (3.0, 0.07),
    "ocr": (0.0, 0.0),
    "figures": (0.0, 0.0),
    "fix": (0.0, 2.85),
    "epub": (0.0, 0.0),
    "normalize": (20.0, 5.65),
    "chunk": (2.0, 0.0),
    "tts": (0.0, 22.37),
    "verify": (0.0, 7.51),
    "assemble": (0.0, 3.57),
    "sync": (3.0, 0.0),
}
# characters of a scanned page until it is read, as in that book; seconds to read a
# page and to tell a figure, guessed: neither has been timed yet
PAGE_CHARS, PAGE_OCR, FIGURE = 1950, 30.0, 4.0
# seconds of work at the measured speed counted in with the book's own, so that its
# first seconds do not throw the estimate about
PRIOR = 60.0


class Stopped(BaseException):
    """The book being voiced was deleted. Not an `Exception`, so no step catches it."""


class Worker:
    def __init__(self, settings: Settings, ctx: pipeline.Context, hub: fetch.Hub | None = None) -> None:
        self.settings, self.ctx = settings, ctx
        # a book is taken once the models are in place, which wakes the queue
        self.fetch = fetch.Fetch(settings, hub, on_ready=ctx.quiet.interrupt)
        self.library = Library(settings)
        self.library.requeue()
        # taking the next book and deleting one never cross
        self.lock = threading.Lock()
        self.current: tuple[str, Book] | None = None
        self.deleted = False
        self.subscribers: set[queue.Queue[contract.Event]] = set()
        self.clock = time.monotonic
        self._saved = 0.0
        # seconds the book has been worked on, not counting pauses; when the step began
        # and when the count was last brought up to date, None while paused
        self._worked = self._began = 0.0
        self._tick: float | None = None
        self._step, self._percent = "extract", 0
        self._lazy = _lazy_attributes(ctx.models)
        quiet = ctx.quiet
        quiet.on_check, quiet.on_pause, quiet.on_resume = self._check, self._pause, self._resume

    def run(self, until_idle: bool = False) -> None:
        """Voices the queue once the models are in place; with `until_idle`, returns once it is empty."""
        self.fetch.start()
        while True:
            left = self.ctx.quiet.seconds_left()
            with self.lock:
                row = None if left or not self.fetch.ready else self.library.next()
                if row:
                    self.current, self.deleted = (row["id"], self._workdir(row["id"], row["name"])), False
            if self.current:
                self.voice(*self.current)
                continue
            # with the queue empty, the books finished by an older reading are read again
            stale = None if left or not self.fetch.ready else self.library.stale(refresh.VERSION)
            if stale:
                self.refresh(stale)
                continue
            if until_idle and not self.library.queue():
                return
            self.ctx.quiet.sleep(left or IDLE)

    def voice(self, book_id: str, book: Book) -> None:
        row = self.library.get(book_id)
        self._worked, self._percent = (row["worked"], row["percent"]) if row else (0.0, 0)
        self._tick = self.clock()
        self._set(book_id, state="running")
        self._lazy = _lazy_attributes(self.ctx.models)  # nothing is loaded between books
        step = "extract"
        try:
            for s in pipeline.STEPS:
                if not book.done(s.name):
                    step = s.name
                    if applies(book, step):
                        self._begin(book_id, book, step)
                    pipeline.run(book, self.ctx, until=step)
                    self.unload()  # the next step loads its own model
            step = "publish"
            self._publish(book_id, book)
            log.info("%s: done", book_id)
        except Stopped:
            log.info("%s: deleted while being voiced, stopped", book_id)
        except Exception as e:
            log.exception("%s: failed at %s", book_id, step)
            self._set(book_id, state="failed", step=step, error=error_text(step, e), eta=None)
        finally:
            self._tick = None
            with self.lock:
                deleted, self.current = self.deleted, None
            if deleted:
                self.library.remove_files(book_id)
            self.unload()

    def refresh(self, row: Any) -> None:
        """A finished book gets the title, the author and the chapters the reading of
        today finds, around the same sound. Whatever goes wrong, the book stays as it was
        and is not read again until the reading changes once more."""
        book_id = row["id"]
        folder = self.settings.library / book_id
        work = self.settings.work / f"{book_id}.refresh"
        fields: dict[str, object] = {"parse": refresh.VERSION}
        log.info("%s: reading again", book_id)
        try:
            # read with the name of its file, as the first time; a book finished before
            # that name was kept is read with none, the title found is not taken for it
            name = "" if row["name"] is None else file_name(row["name"], next(folder.glob("source.*")))
            found = refresh.run(folder, name, work, self.ctx, (row["title"], row["author"]))
            fields |= {"title": found["title"], "author": found["author"]}
            if "chapters" in found:
                fields["chapters"] = json.dumps(found["chapters"], ensure_ascii=False)
                fields["duration"] = found["duration"]
            log.info("%s: read again, %d chapters", book_id, len(found.get("chapters", [])))
        except Exception:
            log.exception("%s: reading again failed, the book stays as it was", book_id)
        finally:
            shutil.rmtree(work, ignore_errors=True)
            self.unload()
        self._set(book_id, **fields)

    def unload(self) -> None:
        """Takes every model off the card; each one loads itself again when used."""
        unload(self.ctx.models, self._lazy)

    def _workdir(self, book_id: str, name: str) -> Book:
        """The book's working directory, its source linked from the library."""
        book = Book(self.settings.work / book_id)
        book.dir.mkdir(exist_ok=True)
        source = next((self.settings.library / book_id).glob("source.*"))
        link = book.dir / source.name
        if not link.is_symlink():
            link.symlink_to(os.path.relpath(source, book.dir))
            book.path("name").write_text(file_name(name, source))
        return book

    def _begin(self, book_id: str, book: Book, step: str) -> None:
        """What the steps before have made is shown: the title, the chapters, the cover."""
        self._accrue()
        self._step, self._began = step, self._worked
        cover, shown = book.path("cover.jpg"), self.settings.library / book_id / "cover.jpg"
        if cover.exists() and not shown.exists():
            shutil.copyfile(cover, book.path("shown.jpg"))
            os.replace(book.path("shown.jpg"), shown)
        chapters = [contract.Chapter(title=t, start=None) for t in outline(book)]
        self._progress(book_id, book, about(book), step=step, chapters=json.dumps(chapters, ensure_ascii=False))

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
            percent=100,
            eta=0,
            worked=self._worked,
            parse=refresh.VERSION,
            duration=made["duration"],
            chapters=json.dumps(made["chapters"], ensure_ascii=False),
        )
        shutil.rmtree(book.dir, ignore_errors=True)

    # the checkpoints of the pipeline, see `quiet`

    def _check(self) -> None:
        if self.deleted:
            raise Stopped
        if self.current and self.clock() - self._saved >= PROGRESS_EVERY:
            self._progress(*self.current)

    def _pause(self) -> None:
        self.unload()
        if self.current:
            self._accrue()
            self._tick = None
            self._progress(*self.current, state="paused")

    def _resume(self) -> None:
        if self.current:
            self._tick = self.clock()
            self._set(self.current[0], state="running")

    def _accrue(self) -> None:
        if self._tick is not None:
            now = self.clock()
            self._worked += now - self._tick
            self._tick = now

    def _progress(self, book_id: str, book: Book, more: dict[str, str] | None = None, **fields: object) -> None:
        self._accrue()
        self._saved = self.clock()
        total, done = pieces(book)
        models = self.ctx.models
        cost = plan(book, set() if models.listener or models.aligner else {"verify"})
        eta = None
        if cost:
            percent, eta = estimate(book, cost, self._step, self._worked - self._began, self._worked)
            self._percent = max(self._percent, percent)
        self._set(
            book_id,
            pieces_done=done,
            pieces_total=total,
            percent=self._percent,
            eta=eta,
            worked=self._worked,
            **(more or {}),
            **fields,
        )

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
        if method == "book.retry":
            return self.retry(params["id"])
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

    def retry(self, book_id: str) -> contract.Book:
        """A failed book goes back to the queue and on from the step it failed at."""
        if self._row(book_id)["state"] != "failed":
            raise Fault("conflict")
        self._set(book_id, state="queued", error=None)
        self.ctx.quiet.interrupt()
        return self.library.info(self._row(book_id))

    def status(self) -> contract.Status:
        quiet = self.ctx.quiet
        done, total = self.fetch.progress()
        return contract.Status(
            models=contract.Models(ready=self.fetch.ready, done=done, total=total, failed=self.fetch.failed),
            card=card(),
            queue=self.library.queue(),
            quiet=contract.Quiet(window=quiet.spec or None, active=bool(quiet.seconds_left())),
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
    # a caller with a timeout is turned away at once (EAGAIN) by a Unix socket whose
    # queue is full, not kept waiting: the queue takes a burst, not socketserver's five
    request_queue_size = socket.SOMAXCONN
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


def file_name(name: str, source: Path) -> str:
    """The name of a book's file, as the reading takes it: it stands in for a title when
    the file has none of its own."""
    return (name or "Без названия") + source.suffix


def about(book: Book) -> dict[str, str]:
    """Title and author as the book's text has them, once it has been read."""
    for name in ("text.json", "scan.json"):
        if book.path(name).exists():
            data = book.read(name)
            return {"title": data.get("title", ""), "author": data.get("author", "")}
    return {}


def outline(book: Book) -> list[str]:
    """The titles of the chapters as the m4b will have them, once the text is read."""
    for name in ("normalized.json", "fixed.json", "described.json", "text.json"):
        if book.path(name).exists():
            return reading.titles(book.read(name), book.name)
    return []


def applies(book: Book, step: str) -> bool:
    """Whether the book has anything for `step` to do: a scan to read, figures to tell.
    A step with nothing to do is not shown."""
    if step == "ocr":
        return book.path("scan.json").exists()
    if step == "figures":
        return book.path("text.json").exists() and bool(book.read("text.json").get("figures"))
    return True


def pieces(book: Book, folder: str = "tts") -> tuple[int, int]:
    """(pieces in the book, pieces voiced, or heard with `folder="ready"`); a second
    take (`00012.s1.wav`) is not another piece."""
    if not book.path("chunks.json").exists():
        return 0, 0
    total = len(book.read("chunks.json"))
    done = sum(1 for p in book.path(folder).glob("*.wav") if p.stem.isdigit())
    return total, min(done, total)


def plan(book: Book, skip: set[str]) -> dict[str, float] | None:
    """Seconds each step takes for this book at the measured speed, None until its text is read."""
    scan = book.read("scan.json") if book.path("scan.json").exists() else None
    if book.path("text.json").exists():
        text = book.read("text.json")
        chars = sum(len(p) for c in text["chapters"] for p in c["paragraphs"])
        figures = len(text.get("figures", []))
    elif scan:
        chars, figures = len(scan["pages"]) * PAGE_CHARS, 0
    else:
        return None
    cost = {s: 0.0 if s in skip else base + per * chars / 1000 for s, (base, per) in COST.items()}
    cost["ocr"] = len(scan["pages"]) * PAGE_OCR if scan else 0.0
    cost["figures"] = figures * FIGURE
    return cost


def estimate(book: Book, cost: dict[str, float], step: str, spent: float, worked: float) -> tuple[int, int]:
    """(percent, seconds left) of a book `worked` seconds into, `spent` of them on `step`.
    The pieces voiced or heard tell how far `tts` and `verify` are; any other step is
    as far as its time at the book's speed so far says, never quite at its end."""
    names = [s.name for s in pipeline.STEPS]
    before = sum(cost[n] for n in names[: names.index(step)])
    total = sum(cost.values())
    if step in ("tts", "verify"):
        count, done = pieces(book, "tts" if step == "tts" else "ready")
        share = done / count if count else 0.0
        speed = (worked + PRIOR) / (before + share * cost[step] + PRIOR)
        left = total - before - share * cost[step]
    else:
        speed = (worked - spent + PRIOR) / (before + PRIOR)
        x = spent / (speed * cost[step]) if cost[step] else 0.0
        share = _eased(x)
        # a step going on past its time is a slower card
        speed = max(speed, (worked + PRIOR) / (before + cost[step] + PRIOR))
        left = total - before - min(x, 1) * cost[step]
    return min(99, int(100 * (before + share * cost[step]) / total)), round(speed * left)


def _eased(x: float) -> float:
    """`x` up to 0.9, then on towards 1 without reaching it."""
    return x if x <= 0.9 else 1 - 0.1 * math.exp(-(x - 0.9) / 0.1)


def error_text(step: str, error: Exception) -> str:
    """What went wrong, in words for the listener: no paths, no trace."""
    if isinstance(error, NotImplementedError) and step == "ocr":
        what = "это скан без текстового слоя, а распознавание сканов не подключено."
    elif step == "chunk" and "no text" in str(error):
        what = "в книге не нашлось текста, который можно прочитать."
    elif step == "extract":
        what = "файл не удалось прочитать: возможно, он повреждён или защищён."
    else:
        what = "сбой во время работы. Подробности в журнале worker."
    return f"Этап «{STEPS_RU.get(step, step)}»: {what}"


def card() -> contract.Card | None:
    """The first video card as nvidia-smi sees it."""
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
        name, used, total, load = (v.strip() for v in out.splitlines()[0].split(","))
        return contract.Card(name=name, memory_used=int(used), memory_total=int(total), load=int(load))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def unload(models: object, lazy: dict[int, set[str]]) -> None:
    """A model is closed if it can be, and anything it loaded on first use (the
    attributes that were None or missing when it was made) is let go."""
    for model in vars(models).values():
        if model is None:
            continue
        close = getattr(model, "close", None)
        if callable(close):
            close()
        keep = lazy.get(id(model))
        if keep is not None:
            for key in list(vars(model)):
                if key not in keep:
                    setattr(model, key, None)
    gc.collect()
    if "torch" in sys.modules:
        torch = sys.modules["torch"]
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def _lazy_attributes(models: object) -> dict[int, set[str]]:
    """For every model, the attributes it was made with (all but those still None)."""
    return {
        id(m): {k for k, v in vars(m).items() if v is not None}
        for m in vars(models).values()
        if m is not None and hasattr(m, "__dict__")
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
