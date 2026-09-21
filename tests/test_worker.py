import json
import queue
import shutil
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from audiobux import contract, fetch, models, pipeline, worker
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
    assert state(w, book_id)["title"] == "Чехов - Рассказы"


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
    assert w.status()["quiet"] == {"window": "22:00-06:00", "tz": "Europe/Moscow", "active": True}


def subscriber(w: Worker):
    q = queue.Queue()
    w.subscribers.add(q)
    return q


def drain(q) -> list:
    got = []
    while not q.empty():
        got.append(q.get())
    return got


def test_events_are_json_lines():
    line = contract.notice(contract.Event(type="deleted", id="ab"))
    assert line.endswith(b"\n") and json.loads(line) == {"type": "deleted", "id": "ab"}
