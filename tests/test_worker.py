import asyncio
import itertools
import json
import queue
import shutil
import socket
import sqlite3
import threading
import time
import typing
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from audiobux import contract, fetch, library, models, pipeline, worker
from audiobux.book import Book
from audiobux.config import Settings
from audiobux.contract import Fault
from audiobux.quiet import QuietHours
from audiobux.worker import Worker

MSK = ZoneInfo("Europe/Moscow")


class Clock:
    def __init__(self, start: datetime):
        self.now, self.slept = start, 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept += seconds
        self.now += timedelta(seconds=seconds)


class ShortTts(models.ToneTts):
    """Silence a hundred times shorter than reading would take: m4b builds fast."""

    chars_per_second = 1500

    def __init__(self):
        super().__init__(silent=True)


class CountingTts(ShortTts):
    """Counts the pieces; `on_call(n)` runs before the n-th one, and may raise."""

    def __init__(self, on_call=None):
        super().__init__()
        self.calls, self.texts, self.on_call = 0, [], on_call

    def synth(self, text, seed=0):
        if self.on_call:
            self.on_call(self.calls + 1)
        self.calls += 1
        self.texts.append(text)
        return super().synth(text)


class LazyTts(CountingTts):
    """Loads its 'model' on first use, like the real ones do; each piece takes `step` of the clock."""

    def __init__(self, clock, step=timedelta(seconds=20)):
        super().__init__()
        self.clock, self.step = clock, step
        self.loads, self.times = 0, []
        self._model = None

    def synth(self, text, seed=0):
        if self._model is None:
            self._model = object()
            self.loads += 1
        self.times.append(self.clock())
        self.clock.now += self.step
        return super().synth(text)


def settings_for(tmp_path, sock=None) -> Settings:
    return Settings(data=tmp_path / "data", socket=str(sock or ""), llm="", accent="", asr="", ocr="")


def make(tmp_path, tts=None, quiet=None, clock=None, sock=None, hub=None) -> Worker:
    settings = settings_for(tmp_path, sock)
    m = models.load(settings)
    m.tts = tts or CountingTts()
    if quiet is None:
        quiet = QuietHours("")
        quiet.sleep = lambda s: None
    if clock:
        quiet.clock, quiet.sleep = clock, clock.sleep
    return Worker(settings, pipeline.Context(settings, m, quiet), hub)


def upload(w: Worker, source: Path | bytes) -> str:
    """What the web process does with an upload: a file under a fresh id in incoming/."""
    upload_id = uuid.uuid4().hex
    target = w.settings.incoming / upload_id
    if isinstance(source, bytes):
        target.write_bytes(source)
    else:
        shutil.copyfile(source, target)
    return upload_id


def submit(w: Worker, source: Path | bytes) -> str:
    return w.submit(upload(w, source))["id"]


def state(w: Worker, book_id: str) -> dict:
    row = w.library.get(book_id)
    return dict(row) if row else {}


@pytest.fixture
def served(tmp_path, sock):
    """A worker answering on its socket; nothing is voiced unless the test runs it."""
    w = make(tmp_path, sock=sock)
    server = w.serve()
    yield w
    server.shutdown()
    server.server_close()


def raw(path: Path, data: bytes, half_close: bool = True) -> bytes:
    """Sends bytes as they are and reads everything until the worker closes."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(10)
        conn.connect(str(path))
        conn.sendall(data)
        if half_close:
            conn.shutdown(socket.SHUT_WR)
        got = b""
        try:
            while chunk := conn.recv(65536):
                got += chunk
        except ConnectionResetError:
            pass  # closed on unread bytes: what came before still counts
        return got


# the flow


def test_book_goes_from_incoming_to_the_library(tmp_path, sock, chekhov_epub):
    # round the clock: an empty setting means no quiet hours, 3 at night included
    clock = Clock(datetime(2026, 11, 3, 3, 0, tzinfo=MSK))
    tts = LazyTts(clock)
    w = make(tmp_path, tts, clock=clock, sock=sock)
    server = w.serve()
    try:
        upload_id = upload(w, chekhov_epub)
        answer = contract.call(w.settings.worker_socket, "book.submit", upload_id=upload_id, title="")
        assert answer["known"] is False and contract.ID.fullmatch(answer["id"])
        book_id = answer["id"]
        assert not (w.settings.incoming / upload_id).exists(), "the upload is taken out of incoming/"
        assert contract.call(w.settings.worker_socket, "book.get", id=book_id)["state"] == "queued"

        w.run(until_idle=True)

        folder = w.settings.library / book_id
        assert (folder / "book.m4b").stat().st_size > 0
        assert (folder / "book.epub").read_bytes() == chekhov_epub.read_bytes()
        assert (folder / "source.epub").read_bytes() == chekhov_epub.read_bytes()
        assert not (w.settings.work / book_id).exists(), "work in progress goes once the book is in the library"
        assert tts.times[0] == datetime(2026, 11, 3, 3, 0, tzinfo=MSK)

        got = contract.call(w.settings.worker_socket, "book.get", id=book_id)
        assert (got["state"], got["step"], got["percent"], got["error"]) == ("done", None, 100, None)
        assert (got["title"], got["author"], got["has_epub"]) == ("Рассказы", "Антон Чехов", True)
        assert got["pieces_done"] == got["pieces_total"] == tts.calls
        assert got["duration"] > 0 and len(got["chapters"]) > 1 and got["chapters"][0]["start"] == 0
        listed = contract.call(w.settings.worker_socket, "library.list")
        assert [b["id"] for b in listed] == [book_id] and "chapters" not in listed[0]
    finally:
        server.shutdown()
        server.server_close()


def test_the_same_file_again_is_the_same_book(tmp_path, chekhov_epub):
    """The simple way: a file already in the library is that book, not a new one."""
    w = make(tmp_path)
    book_id = submit(w, chekhov_epub)
    w.run(until_idle=True)

    again = upload(w, chekhov_epub)
    assert w.submit(again) == {"id": book_id, "known": True}
    assert not list(w.settings.incoming.iterdir()) and not list(w.settings.work.glob("*.upload"))
    assert w.library.queue() == 0 and len(w.library.books()) == 1

    # once deleted it is no longer known: the same file is a new book
    w.delete(book_id)
    assert w.submit(upload(w, chekhov_epub))["known"] is False


def test_a_book_without_a_title_takes_the_one_it_came_with(tmp_path, chekhov_pdf):
    """The PDF has no title of its own: the one from the file's name stays, its author part split off."""
    w = make(tmp_path)
    book_id = w.submit(upload(w, chekhov_pdf), "Чехов - Рассказы")["id"]
    assert state(w, book_id)["title"] == "Чехов - Рассказы", "the list shows it while the book waits"
    w.run(until_idle=True)
    assert (state(w, book_id)["title"], state(w, book_id)["author"]) == ("Рассказы", "Чехов")


def test_books_are_voiced_in_the_order_they_came(tmp_path, chekhov_epub, chekhov_pdf):
    w = make(tmp_path)
    first, second = submit(w, chekhov_pdf), submit(w, chekhov_epub)
    order = []
    voice = w.voice
    w.voice = lambda book_id, book: (order.append(book_id), voice(book_id, book))
    w.run(until_idle=True)
    assert order == [first, second]
    assert state(w, first)["state"] == state(w, second)["state"] == "done"


def test_restart_goes_on_from_the_last_piece(tmp_path, chekhov_epub):
    def power_cut(n):
        if n == 6:
            raise SystemExit("container stopped")

    w = make(tmp_path, CountingTts(power_cut))
    book_id = submit(w, chekhov_epub)
    with pytest.raises(SystemExit):
        w.run(until_idle=True)
    assert state(w, book_id)["state"] == "running"

    tts = CountingTts()
    restarted = make(tmp_path, tts)
    assert state(restarted, book_id)["state"] == "queued", "after a restart the book waits its turn again"
    restarted.run(until_idle=True)

    row = state(restarted, book_id)
    assert row["state"] == "done" and (restarted.settings.library / book_id / "book.m4b").exists()
    assert row["pieces_total"] > 5 and tts.calls == row["pieces_total"] - 5


def test_failed_book_says_why_without_paths_and_the_queue_goes_on(tmp_path, chekhov_scan, chekhov_epub):
    w = make(tmp_path)
    scan, book = submit(w, chekhov_scan), submit(w, chekhov_epub)

    w.run(until_idle=True)

    failed = w.library.info(w.library.get(scan))
    assert (failed["state"], failed["step"]) == ("failed", "ocr")
    assert "распознавание сканов" in failed["error"] and "не подключено" in failed["error"]
    assert "/" not in failed["error"] and str(tmp_path) not in failed["error"]
    assert state(w, book)["state"] == "done"

    w.run(until_idle=True)
    assert state(w, scan)["state"] == "failed", "a failed book waits to be retried"


def test_retry_goes_on_from_the_step_that_failed(tmp_path, chekhov_epub, monkeypatch):
    def card_lost(n):
        if n == 4:
            raise RuntimeError("CUDA error: out of memory at /opt/secret/f5.py")

    tts = CountingTts(card_lost)
    w = make(tmp_path, tts)
    book_id = submit(w, chekhov_epub)
    w.run(until_idle=True)

    row = state(w, book_id)
    assert (row["state"], row["step"]) == ("failed", "tts")
    assert row["error"] == "Этап «озвучка»: сбой во время работы. Подробности в журнале worker."
    assert Book(w.settings.work / book_id).done("chunk"), "the work so far stays for the retry"
    with pytest.raises(Fault) as refused:
        w.retry(uuid.uuid4().hex)
    assert refused.value.error == "not-found"

    tts.on_call = None
    assert w.retry(book_id)["state"] == "queued"
    with pytest.raises(Fault) as refused:
        w.retry(book_id)
    assert refused.value.error == "conflict", "only a failed book is retried"
    seen = []
    run = pipeline.run

    def steps(book, ctx, until=None):
        seen.append(until)
        return run(book, ctx, until)

    monkeypatch.setattr(pipeline, "run", steps)
    w.run(until_idle=True)

    assert seen[0] == "tts", "the steps before the failed one are not run again"
    assert state(w, book_id)["state"] == "done" and state(w, book_id)["error"] is None
    total = state(w, book_id)["pieces_total"]
    assert tts.calls == total, "the three pieces voiced before the failure are not voiced again"


# quiet hours


def test_a_book_that_comes_in_quiet_hours_waits_for_them_to_end(tmp_path, chekhov_epub):
    clock = Clock(datetime(2026, 11, 3, 3, 0, tzinfo=MSK))
    tts = LazyTts(clock)
    w = make(tmp_path, tts, QuietHours("00:00-08:00"), clock)
    book_id = submit(w, chekhov_epub)
    states = []
    sleep = w.ctx.quiet.sleep
    w.ctx.quiet.sleep = lambda s: (states.append(state(w, book_id)["state"]), sleep(s))

    w.run(until_idle=True)

    assert states and set(states) == {"queued"}, "not started at all in quiet hours"
    assert min(tts.times) >= datetime(2026, 11, 3, 8, 0, tzinfo=MSK)
    assert state(w, book_id)["state"] == "done"


def test_quiet_hours_across_midnight_pause_and_resume_at_the_same_piece(tmp_path, chekhov_epub):
    clock = Clock(datetime(2026, 11, 2, 23, 59, tzinfo=MSK))
    tts = LazyTts(clock)
    w = make(tmp_path, tts, QuietHours("23:59-00:30"), clock)
    book_id = submit(w, chekhov_epub)
    clock.now = datetime(2026, 11, 2, 23, 58, tzinfo=MSK)
    paused = []
    sleep = w.ctx.quiet.sleep

    def asleep(seconds):
        if w.current and not paused:
            row = state(w, book_id)
            paused.append((row["state"], row["pieces_done"], tts._model, w.current[1].read("chunks.json")))
        sleep(seconds)

    w.ctx.quiet.sleep = asleep
    w.run(until_idle=True)

    assert paused, "quiet hours came while the book was being voiced"
    (was, done, model, chunks) = paused[0]
    assert was == "paused" and model is None, "paused, with the model off the card"
    assert 0 < done < len(chunks), "between pieces"
    assert tts.texts == [c["text"] for c in chunks], "every piece once, in order: on from the same piece"
    assert tts.times[done - 1] < datetime(2026, 11, 2, 23, 59, tzinfo=MSK)
    assert tts.times[done] >= datetime(2026, 11, 3, 0, 30, tzinfo=MSK)
    assert tts.loads == 2 and state(w, book_id)["state"] == "done"


def test_quiet_hours_checkpoint():
    clock = Clock(datetime(2026, 11, 2, 7, 59, 30, tzinfo=MSK))
    quiet = QuietHours("00:00-08:00")
    quiet.clock, quiet.sleep = clock, clock.sleep
    calls = []
    quiet.on_pause, quiet.on_check = lambda: calls.append("pause"), lambda: calls.append("check")
    quiet.on_resume = lambda: calls.append("resume")
    quiet.wait()
    assert calls == ["check", "pause", "check", "resume"] and clock.slept == 30
    quiet.wait()
    assert calls == ["check", "pause", "check", "resume", "check"]


def test_interrupt_wakes_a_sleep():
    quiet = QuietHours("")
    quiet.interrupt()
    started = datetime.now()
    quiet.sleep(30)
    assert datetime.now() - started < timedelta(seconds=5)


# deleting


def test_delete_while_voicing_stops_and_cleans_up(tmp_path, chekhov_epub, chekhov_pdf):
    w = make(tmp_path)
    book_id, other = submit(w, chekhov_epub), submit(w, chekhov_pdf)
    events = subscriber(w)

    def deleted_now(n):
        if n == 3 and w.current and w.current[0] == book_id:
            w.delete(book_id)

    w.ctx.models.tts.on_call = deleted_now
    w.run(until_idle=True)

    assert w.library.get(book_id) is None
    assert not (w.settings.library / book_id).exists() and not (w.settings.work / book_id).exists()
    assert state(w, other)["state"] == "done", "the next book goes on"
    seen = drain(events)
    assert {"type": "deleted", "id": book_id} in seen
    assert not [e for e in seen if e.get("book", {}).get("id") == book_id and e["book"]["state"] == "done"]


def test_delete_during_quiet_hours_stops_the_paused_book(tmp_path, chekhov_epub):
    clock = Clock(datetime(2026, 11, 2, 23, 58, tzinfo=MSK))
    tts = LazyTts(clock)
    w = make(tmp_path, tts, QuietHours("23:59-08:00"), clock)
    book_id = submit(w, chekhov_epub)
    sleep = w.ctx.quiet.sleep

    def asleep(seconds):
        if w.library.get(book_id):
            assert state(w, book_id)["state"] == "paused"
            w.delete(book_id)
        else:
            sleep(seconds)

    w.ctx.quiet.sleep = asleep
    w.run(until_idle=True)

    assert w.library.get(book_id) is None and not (w.settings.work / book_id).exists()
    assert not (w.settings.library / book_id).exists() and clock.now.hour == 23, "stopped at once, not in the morning"


def test_delete_a_queued_or_unknown_book(tmp_path, chekhov_epub):
    w = make(tmp_path)
    book_id = submit(w, chekhov_epub)
    assert w.delete(book_id) == {"id": book_id}
    assert not (w.settings.library / book_id).exists() and w.library.queue() == 0
    with pytest.raises(Fault) as refused:
        w.delete(book_id)
    assert refused.value.error == "not-found"


def test_nothing_stays_on_the_card_between_books(tmp_path, chekhov_epub, chekhov_scan):
    tts = LazyTts(Clock(datetime(2026, 11, 2, 12, 0, tzinfo=MSK)))
    w = make(tmp_path, tts)
    submit(w, chekhov_epub)
    w.run(until_idle=True)
    assert tts.loads == 1 and tts._model is None

    scan = submit(w, chekhov_scan)
    w.ctx.models.ocr = LazyOcr()
    w.run(until_idle=True)
    assert state(w, scan)["state"] == "failed"
    assert w.ctx.models.ocr._model is None, "a failed book lets its models go too"


class LazyOcr:
    def __init__(self):
        self._model = None

    def page_text(self, image):
        self._model = object()
        raise RuntimeError("out of memory")


# the socket


NO_UPLOAD = b"0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize(
    ("request_line", "error"),
    [
        (b'{"method": "book.burn", "params": {}}\n', "unknown-method"),
        (b'{"method": "status.read", "params": {}, "as": "root"}\n', "bad-request"),
        (b'{"method": "library.list", "params": {"all": "1"}}\n', "bad-request"),
        (b'{"method": "book.get", "params": {}}\n', "bad-request"),
        (b'{"method": "book.get", "params": {"id": "../../etc/passwd"}}\n', "bad-request"),
        (b'{"method": "book.submit", "params": {"upload_id": "/etc/passwd", "title": ""}}\n', "bad-request"),
        (b'{"method": "book.submit", "params": {"upload_id": "book.pdf", "title": ""}}\n', "bad-request"),
        (b'{"method": "book.submit", "params": {"upload_id": "../' + NO_UPLOAD + b'", "title": ""}}\n', "bad-request"),
        (b'{"method": "book.submit", "params": {"upload_id": "' + NO_UPLOAD + b'"}}\n', "bad-request"),
        (b'{"method": "book.submit", "params": {"upload_id": "' + NO_UPLOAD + b'", "title": 7}}\n', "bad-request"),
        (
            b'{"method": "book.submit", "params": {"upload_id": "'
            + NO_UPLOAD
            + b'", "title": "'
            + b"x" * 201
            + b'"}}\n',
            "bad-request",
        ),
        (
            b'{"method": "book.submit", "params": {"upload_id": "'
            + NO_UPLOAD
            + b'", "title": "'
            + b"x" * 200
            + b'"}}\n',
            "not-found",
        ),
        (b'{"method": "book.get", "params": {"id": "0123456789ABCDEF0123456789ABCDEF"}}\n', "bad-request"),
        (b'{"method": "book.get", "params": {"id": 7}}\n', "bad-request"),
        (b'{"method": ["status.read"], "params": {}}\n', "bad-request"),
        (b'["status.read"]\n', "bad-request"),
        (b"status.read\n", "bad-request"),
        (b"\xff\xfe\n", "bad-request"),
        (b'{"method": "status.read", "params": {}}', "bad-request"),  # no line feed
        (b'{"method": "book.get", "params": {"id": "0123456789abcdef0123456789abcdef"}}\n', "not-found"),
    ],
)
def test_the_socket_refuses_with_the_code_alone(served, request_line, error):
    reply = raw(served.settings.worker_socket, request_line)
    assert reply == contract.refusal(error), "the code and nothing the caller sent"


def test_a_request_too_large_is_refused_and_the_connection_closed(served):
    huge = b'{"method": "book.get", "params": {"id": "' + b"a" * contract.REQUEST_LIMIT + b'"}}\n'
    reply = raw(served.settings.worker_socket, huge + huge, half_close=False)
    assert reply == contract.refusal("too-large")


def test_a_silent_caller_is_cut_off(served, monkeypatch):
    monkeypatch.setattr(worker._Handler, "timeout", 0.2)
    assert raw(served.settings.worker_socket, b'{"method": ', half_close=False) == contract.refusal("timeout")


def test_submit_tells_the_format_by_the_content(served):
    path = served.settings.worker_socket
    text = upload(served, "Это просто текст, а не PDF.\n".encode() * 100)
    with pytest.raises(Fault) as refused:
        contract.call(path, "book.submit", upload_id=text, title="")
    assert refused.value.error == "not-a-book"
    assert not (served.settings.incoming / text).exists(), "a refused upload is dropped"
    assert not list(served.settings.work.iterdir()) and served.library.books() == []


def test_submit_takes_only_a_plain_file_in_incoming(served, tmp_path):
    path, incoming = served.settings.worker_socket, served.settings.incoming
    secret = tmp_path / "secret.epub"
    secret.write_bytes(b"PK\x03\x04 private")
    linked, folder, missing = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
    (incoming / linked).symlink_to(secret)
    (incoming / folder).mkdir()
    for upload_id in (linked, folder, missing):
        with pytest.raises(Fault) as refused:
            contract.call(path, "book.submit", upload_id=upload_id, title="")
        assert refused.value.error == "not-found"
    assert secret.exists() and served.library.books() == []


def test_status_and_events(served, chekhov_epub):
    path = served.settings.worker_socket
    status = contract.call(path, "status.read")
    assert status["queue"] == 0 and status["models"]["ready"] is True
    assert status["quiet"] == {"window": None, "active": False}
    kinds = {f["name"]: f["extensions"] for f in status["formats"]}
    assert ".epub" in kinds["epub"] and ".pdf" in kinds["pdf"] and ".djvu" in kinds["djvu"]
    assert status["card"] is None or set(status["card"]) == {"name", "memory_used", "memory_total", "load"}

    async def follow():
        events = await contract.subscribe(path)
        book_id = contract.call(path, "book.submit", upload_id=upload(served, chekhov_epub), title="")["id"]
        first = await anext(events)
        assert first["type"] == "book" and first["book"]["id"] == book_id and first["book"]["state"] == "queued"
        assert contract.call(path, "status.read")["queue"] == 1
        contract.call(path, "book.delete", id=book_id)
        assert await anext(events) == {"type": "deleted", "id": book_id}
        await events.aclose()

    asyncio.run(follow())


def test_the_title_is_cleaned_by_the_socket(served, chekhov_epub):
    """Control characters and slashes go, whoever sent them: a title is never a path."""
    path = served.settings.worker_socket
    sent = " ../x\\y\n\x00\u202eКнига\t "
    book_id = contract.call(path, "book.submit", upload_id=upload(served, chekhov_epub), title=sent)["id"]
    assert contract.call(path, "book.get", id=book_id)["title"] == "..xyКнига"


def fake_worker(path: Path, *lines: bytes, hold: float = 0.0) -> threading.Thread:
    """One connection answered with `lines`, kept open `hold` seconds, then closed."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)

    def serve():
        with server, server.accept()[0] as conn:
            conn.recv(4096)
            conn.sendall(b"".join(lines))
            time.sleep(hold)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread


async def read_all(path: Path) -> list:
    events = await contract.subscribe(path)
    return [e async for e in events]


def test_events_end_when_the_worker_goes(sock):
    deleted = contract.notice(contract.Event(type="deleted", id="ab"))
    fake_worker(sock, contract.answer({}), deleted)
    assert asyncio.run(asyncio.wait_for(read_all(sock), 10)) == [{"type": "deleted", "id": "ab"}]


def test_events_end_when_the_worker_falls_silent(sock, monkeypatch):
    """Not even a ping for three of their periods: the worker hangs, the reader is let go."""
    monkeypatch.setattr(contract, "PING_EVERY", 0.05)
    thread = fake_worker(sock, contract.answer({}), hold=2)
    assert asyncio.run(asyncio.wait_for(read_all(sock), 1)) == []
    thread.join()


def test_an_absent_worker_is_unavailable(tmp_path, sock):
    with pytest.raises(Fault) as refused:
        contract.call(sock, "status.read")
    assert refused.value.error == "unavailable"
    with pytest.raises(Fault) as refused:
        asyncio.run(contract.subscribe(sock))
    assert refused.value.error == "unavailable"


def test_the_queue_waits_for_the_models(tmp_path, chekhov_epub, monkeypatch):
    from test_fetch import Hub

    where = tmp_path / "models"
    where.mkdir()
    monkeypatch.setattr(fetch, "needed", lambda s: [fetch.Source("test/m", names=("m.bin",), local=str(where))])
    gate = threading.Event()
    hub = Hub({"m.bin": 1000}, gate=gate)
    w = make(tmp_path, quiet=QuietHours(""), hub=hub)  # a real sleep, woken when the models are in place
    hub.fetch = w.fetch
    assert w.status()["models"] == {"ready": False, "done": None, "total": None}
    book_id = submit(w, chekhov_epub)
    voicing = threading.Thread(target=w.run, kwargs={"until_idle": True})
    voicing.start()
    deadline = time.monotonic() + 10
    while w.fetch.total is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert w.status()["models"] == {"ready": False, "done": 0, "total": 1000}
    assert state(w, book_id)["state"] == "queued"
    gate.set()
    voicing.join(30)
    assert not voicing.is_alive()
    assert state(w, book_id)["state"] == "done"
    assert w.status()["models"] == {"ready": True, "done": None, "total": None}


def test_quiet_hours_in_the_status(tmp_path):
    clock = Clock(datetime(2026, 11, 3, 1, 0, tzinfo=MSK))
    w = make(tmp_path, quiet=QuietHours("22:00-06:00"), clock=clock)
    assert w.status()["quiet"] == {"window": "22:00-06:00", "active": True}


def subscriber(w: Worker):
    q = queue.Queue()
    w.subscribers.add(q)
    return q


def drain(q) -> list:
    got = []
    while not q.empty():
        got.append(q.get())
    return got


def test_error_text_never_carries_the_exception():
    secret = "/data/library/abc/source.pdf"
    for step in ("extract", "ocr", "chunk", "tts", "assemble"):
        assert secret not in worker.error_text(step, RuntimeError(secret))
    assert "не нашлось текста" in worker.error_text("chunk", ValueError("no text found in the book"))
    assert "не удалось прочитать" in worker.error_text("extract", ValueError(secret))


def test_events_are_json_lines():
    line = contract.notice(contract.Event(type="deleted", id="ab"))
    assert line.endswith(b"\n") and json.loads(line) == {"type": "deleted", "id": "ab"}


def test_a_stale_socket_is_replaced(tmp_path, sock):
    sock.write_text("left from a crash")
    w = make(tmp_path, sock=sock)
    server = w.serve()
    try:
        assert sock.stat().st_mode & 0o777 == 0o600
        assert contract.call(sock, "library.list") == []
    finally:
        server.shutdown()
        server.server_close()


def test_two_callers_at_once(served):
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(contract.call(served.settings.worker_socket, "library.list")))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [[]] * 8


def test_callers_wait_while_the_worker_is_slow_to_take_them(tmp_path, sock):
    """A burst of callers while the worker takes no connections: each is let in and answered."""
    w = make(tmp_path, sock=sock)
    server = w.serve()
    server.shutdown()  # the socket listens, nothing takes its connections
    conns = []
    try:
        for _ in range(64):
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conns.append(conn)
            conn.settimeout(contract.ANSWER_TIMEOUT)
            conn.connect(str(w.settings.worker_socket))
            conn.sendall(contract.request("library.list"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        assert [json.loads(conn.makefile("rb").readline()) for conn in conns] == [{"ok": True, "result": []}] * 64
    finally:
        for conn in conns:
            conn.close()
        server.shutdown()
        server.server_close()


# progress

# the steps of a 132-page book of 256 639 characters as they went on the card, seconds
ALTOV = {
    "extract": 21,
    "ocr": 0,
    "figures": 0,
    "fix": 731,
    "epub": 0,
    "normalize": 1471,
    "chunk": 0,
    "tts": 5740,
    "verify": 1928,
    "assemble": 915,
    "sync": 3,
}
ALTOV_PIECES = 1400


def standing_in(w, clock, slower=1.0, pieces=ALTOV_PIECES, chars=256639, seconds=ALTOV):
    """The steps of the pipeline with their outputs made up and their time taken off
    `clock`, ten seconds at a time, a checkpoint between; takes are heard, as on the card."""
    w.ctx.models.listener = object()

    def step(name):
        def run(book, ctx):
            if name == "extract":
                half = "а" * (chars // 2)
                book.write(
                    "text.json",
                    {
                        "title": "Альтов",
                        "author": "Г. Альтов",
                        "chapters": [
                            {"title": "Первая", "paragraphs": [half]},
                            {"title": "Вторая", "paragraphs": [half]},
                        ],
                    },
                )
                book.path("cover.jpg").write_bytes(b"\xff\xd8cover")
            if name == "chunk":
                book.write("chunks.json", [{"text": "кусок"}] * pieces)
            folder = {"tts": "tts", "verify": "ready"}.get(name)
            if folder:
                book.path(folder).mkdir(exist_ok=True)
            ticks = max(1, round(seconds[name] * slower / 10))
            for k in range(ticks):
                ctx.quiet.wait()
                clock.now += timedelta(seconds=seconds[name] * slower / ticks)
                for i in range(pieces * k // ticks, pieces * (k + 1) // ticks) if folder else ():
                    book.path(f"{folder}/{i:05}.wav").touch()
                    if folder == "ready":
                        book.path(f"tts/{i:05}.s1.wav").touch()
            if name == "assemble":
                book.write("chapters.json", {"duration": 20940.0, "chapters": [{"title": "Первая", "start": 0.0}]})
                book.path("book.m4b").write_bytes(b"m4b")

        return pipeline.Step(name, run)

    return [step(s.name) for s in pipeline.STEPS]


def watched(w, clock):
    """(seconds since the start, the book) at every event the worker sends."""
    seen, start, emit = [], clock.now, w._emit
    w.clock = lambda: clock.now.timestamp()

    def record(event):
        if event["type"] == "book":
            seen.append(((clock.now - start).total_seconds(), event["book"]))
        emit(event)

    w._emit = record
    return seen


def test_progress_of_a_whole_book(tmp_path, chekhov_epub, monkeypatch):
    clock = Clock(datetime(2026, 11, 3, 9, 0, tzinfo=MSK))
    w = make(tmp_path, quiet=QuietHours(""), clock=clock)
    monkeypatch.setattr(pipeline, "STEPS", standing_in(w, clock))
    submit(w, chekhov_epub)
    seen = watched(w, clock)

    w.run(until_idle=True)

    running = [b for _, b in seen if b["state"] == "running"]
    percents = [b["percent"] for b in running]
    assert percents == sorted(percents) and max(percents) == 99, "never back, 100 only when done"
    assert seen[-1][1]["state"] == "done" and (seen[-1][1]["percent"], seen[-1][1]["eta"]) == (100, 0)
    fixing = [b["percent"] for b in running if b["step"] == "fix"]
    assert fixing[-1] >= 6 and all(b["percent"] >= fixing[-1] for b in running if b["step"] == "normalize")
    assert len(set(fixing)) > 5, "on before there are pieces to count"
    assert len({b["percent"] for b in running if b["step"] == "tts"}) > 40, "the voicing goes on smoothly"
    assert not {b["step"] for b in running} & {"ocr", "figures"}, "no scan, no figures: those steps are not shown"
    etas = [b["eta"] for b in running]
    first = next(i for i, e in enumerate(etas) if e is not None)
    assert first > 0 and running[first - 1]["step"] == "extract", "no estimate until the text is read"
    assert all(e is not None for e in etas[first:])
    assert abs(etas[first] - sum(ALTOV.values()) + ALTOV["extract"]) < 600, "the book's time, near enough"
    assert all(a + 1 >= b for a, b in itertools.pairwise(etas[first:])), "at this speed only down"
    same = [
        (b - a) for (a, x), (b, y) in itertools.pairwise(seen) if (x["step"], x["state"]) == (y["step"], y["state"])
    ]
    assert min(same) >= 5 and max(same) <= 15, "within a step, every 5 to 15 s"


def test_eta_follows_a_slower_card(tmp_path, chekhov_epub, monkeypatch):
    clock = Clock(datetime(2026, 11, 3, 9, 0, tzinfo=MSK))
    w = make(tmp_path, quiet=QuietHours(""), clock=clock)
    monkeypatch.setattr(pipeline, "STEPS", standing_in(w, clock, slower=2))
    submit(w, chekhov_epub)
    seen = watched(w, clock)

    w.run(until_idle=True)

    total = 2 * sum(ALTOV.values())
    for t, book in seen:
        if book["state"] == "running" and t > total / 3:
            assert abs(book["eta"] - (total - t)) < 0.25 * (total - t) + 120, (t, book["eta"])


def test_eta_stands_still_in_quiet_hours(tmp_path, chekhov_epub, monkeypatch):
    clock = Clock(datetime(2026, 11, 3, 0, 0, tzinfo=MSK))
    w = make(tmp_path, quiet=QuietHours("01:00-08:00"), clock=clock)
    monkeypatch.setattr(pipeline, "STEPS", standing_in(w, clock))
    book_id = submit(w, chekhov_epub)
    clock.now = datetime(2026, 11, 2, 23, 0, tzinfo=MSK)
    seen = watched(w, clock)

    w.run(until_idle=True)

    paused = [b for _, b in seen if b["state"] == "paused"]
    assert paused and paused[0]["step"] == "tts"
    assert {(b["percent"], b["eta"]) for b in paused} == {(paused[0]["percent"], paused[0]["eta"])}
    last = max(i for i, (_, b) in enumerate(seen) if b["state"] == "paused")
    after = next(b for _, b in seen[last:] if b["state"] == "running")
    assert abs(after["eta"] - paused[0]["eta"]) <= 15, "seven hours of pause are not seven hours of work"
    assert state(w, book_id)["state"] == "done" and state(w, book_id)["worked"] < sum(ALTOV.values()) + 1


def test_the_cover_and_the_chapters_are_there_before_the_book_is(tmp_path, chekhov_epub, monkeypatch):
    clock = Clock(datetime(2026, 11, 3, 9, 0, tzinfo=MSK))
    w = make(tmp_path, quiet=QuietHours(""), clock=clock)
    monkeypatch.setattr(pipeline, "STEPS", standing_in(w, clock))
    book_id = submit(w, chekhov_epub)
    seen, check = [], w._check
    w.ctx.quiet.on_check = lambda: (check(), seen.append(w.library.details(w.library.get(book_id))))

    w.run(until_idle=True)

    fixing = next(b for b in seen if b["step"] == "fix")
    assert fixing["state"] == "running" and fixing["has_cover"] and fixing["title"] == "Альтов"
    assert fixing["author"] == "Г. Альтов"
    assert fixing["chapters"] == [{"title": "Первая", "start": None}, {"title": "Вторая", "start": None}]
    assert (w.settings.library / book_id / "cover.jpg").read_bytes() == b"\xff\xd8cover"
    done = w.library.details(w.library.get(book_id))
    assert done["chapters"] == [{"title": "Первая", "start": 0.0}] and done["has_cover"]


def test_a_step_is_shown_only_when_the_book_has_something_for_it(tmp_path):
    book = Book(tmp_path)
    assert not worker.applies(book, "ocr") and not worker.applies(book, "figures") and worker.applies(book, "tts")
    book.write("scan.json", {"pages": []})
    assert worker.applies(book, "ocr")
    book.write("text.json", {"chapters": [], "figures": []})
    assert not worker.applies(book, "figures")
    book.write("text.json", {"chapters": [], "figures": [{"image": "figures/a.png"}]})
    assert worker.applies(book, "figures")


def test_a_second_take_is_not_another_piece(tmp_path):
    book = Book(tmp_path)
    assert worker.pieces(book) == (0, 0)
    book.write("chunks.json", [{"text": "а"}] * 3)
    (tmp_path / "tts").mkdir()
    for name in ("00000.wav", "00000.s1.wav", "00000.s2.wav", "00001.tmp.wav", "00002.wav"):
        (tmp_path / "tts" / name).touch()
    assert worker.pieces(book) == (3, 2)
    assert worker.pieces(book, "ready") == (3, 0)


def test_the_estimate_of_one_step(tmp_path):
    book = Book(tmp_path)
    assert worker.plan(book, set()) is None, "nothing to go by before the text is read"
    book.write("scan.json", {"title": "", "author": "", "outline": [], "pages": ["p1.jpg", "p2.jpg"]})
    scan = worker.plan(book, set())
    assert scan and scan["ocr"] == 2 * worker.PAGE_OCR and scan["tts"] > 0
    book.path("scan.json").unlink()
    book.write("text.json", {"chapters": [{"title": "", "paragraphs": ["а" * 100000]}], "figures": [{}] * 3})
    cost = worker.plan(book, {"verify"})
    assert cost and cost["verify"] == 0 and cost["figures"] == 3 * worker.FIGURE
    assert round(cost["tts"]) == 2237
    total = sum(cost.values())
    before = cost["extract"] + cost["figures"]
    start, eta = worker.estimate(book, cost, "fix", 0, before)
    assert eta == round(total - before) and start == int(100 * before / total)
    half = worker.estimate(book, cost, "fix", cost["fix"] / 2, before + cost["fix"] / 2)
    over = worker.estimate(book, cost, "fix", cost["fix"] * 5, before + cost["fix"] * 5)
    assert start < half[0] < over[0] < 100 * (before + cost["fix"]) / total, "never past its step"
    assert abs(half[1] - (eta - cost["fix"] / 2)) <= 1
    assert over[1] > half[1], "a step going on and on is a slower card"
    end = worker.estimate(book, cost, "sync", 0, total - cost["sync"])
    assert end == (99, round(cost["sync"]))


def test_a_database_from_before_gets_the_new_columns(tmp_path):
    settings = settings_for(tmp_path)
    settings.db.parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(settings.db)
    old.executescript(library.SCHEMA)
    old.execute("insert into books (id, sha, format, added, updated) values ('a', 's', 'epub', 0, 0)")
    old.execute(
        "insert into books (id, sha, format, title, state, added, updated) values ('b', 't', 'pdf', 'X', 'done', 0, 0)"
    )
    old.commit()
    old.close()
    lib = library.Library(settings)
    assert (lib.info(lib.get("a"))["percent"], lib.info(lib.get("a"))["eta"]) == (0, None)
    assert (lib.get("a")["name"], lib.get("b")["name"]) == ("", None), "a finished book's file name is not known"
    lib.update("a", name="Чехов - Рассказы")
    again = library.Library(settings)  # and again, with the columns already there
    assert again.get("a")["name"] == "Чехов - Рассказы"


def test_the_contract_names_the_steps_in_their_order():
    assert list(typing.get_args(contract.Step)) == [s.name for s in pipeline.STEPS] + ["publish"]
    assert list(worker.STEPS_RU) == list(typing.get_args(contract.Step))
    assert set(worker.COST) == {s.name for s in pipeline.STEPS}
