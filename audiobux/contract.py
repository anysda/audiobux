"""The worker's socket: what the web process may ask and what it gets back.

One UTF-8 JSON object and a line feed per Unix connection, then the answer and the
connection is closed; `events.subscribe` keeps it open and sends an event a line.

Request: `{"method": <one of METHODS>, "params": <exactly the fields of the method>}`.
Answer: `{"ok": true, "result": ...}` or `{"ok": false, "error": <one of ERRORS>}`.

The caller names books and uploads only by id, 32 lowercase hex digits: no paths,
file names or commands are taken, the worker finds `incoming/<upload_id>` itself
and tells the format by the file's signature. The one other parameter is the
title `book.submit` gives a book whose file has none of its own: at most
`TITLE_LIMIT` characters, control characters and slashes dropped (`title`), empty
when there is none. Anything else, an extra field included, is `bad-request`.
An error is only its code: nothing the caller sent comes back. A request is at
most `REQUEST_LIMIT` bytes and must arrive within `READ_TIMEOUT` seconds, else
the connection is closed.

This module is the standard library only: the web process imports it.
"""

import asyncio
import json
import re
import socket
import unicodedata
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

REQUEST_LIMIT = 4096
ANSWER_LIMIT = 16 * 1024 * 1024
READ_TIMEOUT = 3.0
# how long the caller waits for an answer; submitting a big file hashes it first
ANSWER_TIMEOUT = 60.0
# an idle event stream sends a ping this often, so a gone reader is noticed
PING_EVERY = 15.0

ID = re.compile(r"[0-9a-f]{32}")
TITLE_LIMIT = 200

# method -> its parameters, every one an id but the title
METHODS: dict[str, tuple[str, ...]] = {
    "book.submit": ("upload_id", "title"),
    "book.delete": ("id",),
    "book.retry": ("id",),
    "library.list": (),
    "book.get": ("id",),
    "status.read": (),
    "events.subscribe": (),
}

Error = Literal[
    "bad-request",  # not JSON, not an object, a field missing or extra, an id of the wrong form
    "unknown-method",
    "too-large",
    "timeout",
    "not-found",  # no such book or upload
    "not-a-book",  # the upload is not in any format the worker reads
    "conflict",  # retry of a book that has not failed
    "unavailable",  # the worker does not answer (said by the caller's side)
    "internal",
]

State = Literal["queued", "running", "paused", "done", "failed"]


class Fault(Exception):
    def __init__(self, error: Error) -> None:
        super().__init__(error)
        self.error: Error = error


class Book(TypedDict):
    id: str
    title: str
    author: str
    format: str
    state: State
    # the pipeline step running or failed, null when there is none
    step: str | None
    pieces_done: int
    pieces_total: int
    percent: int
    # what went wrong, for the listener; never a path or a trace
    error: str | None
    added: float
    updated: float
    # seconds of the m4b once it is done
    duration: float | None
    has_epub: bool
    has_cover: bool


class Chapter(TypedDict):
    title: str
    start: float


class BookDetails(Book):
    chapters: list[Chapter]


class Submitted(TypedDict):
    id: str
    # the same file was there already: the answer is that book, the upload is dropped
    known: bool


class Done(TypedDict):
    id: str


class Models(TypedDict):
    ready: bool
    # bytes downloaded and to download while the models are being fetched, else null
    done: int | None
    total: int | None


class Card(TypedDict):
    name: str
    memory_used: int
    memory_total: int
    load: int


class Quiet(TypedDict):
    # HH:MM-HH:MM, null when the card works round the clock
    window: str | None
    tz: str
    active: bool


class Format(TypedDict):
    name: str
    extensions: list[str]


class Status(TypedDict):
    models: Models
    # the video card, null when there is none or it is not seen
    card: Card | None
    queue: int
    quiet: Quiet
    formats: list[Format]


class Event(TypedDict):
    type: Literal["book", "deleted", "ping"]
    book: NotRequired[Book]
    id: NotRequired[str]


def parse(line: bytes) -> tuple[str, dict[str, str]]:
    """The method and parameters of a request line, checked; `Fault` otherwise."""
    try:
        request = json.loads(line)
    except (UnicodeDecodeError, ValueError):
        raise Fault("bad-request") from None
    if type(request) is not dict or set(request) != {"method", "params"}:
        raise Fault("bad-request")
    method, params = request["method"], request["params"]
    if type(method) is not str or type(params) is not dict:
        raise Fault("bad-request")
    if method not in METHODS:
        raise Fault("unknown-method")
    if set(params) != set(METHODS[method]):
        raise Fault("bad-request")
    for name, value in params.items():
        if type(value) is not str:
            raise Fault("bad-request")
        if name == "title":
            if len(value) > TITLE_LIMIT:
                raise Fault("bad-request")
            params[name] = title(value)
        elif not ID.fullmatch(value):
            raise Fault("bad-request")
    return method, params


def title(text: str) -> str:
    """A title as the book may carry it: no control characters, no slashes, at most
    `TITLE_LIMIT` characters; it is a name to show, never a path."""
    kept = "".join(c for c in text if c not in "/\\" and not unicodedata.category(c).startswith("C"))
    return kept[:TITLE_LIMIT].strip()


def request(method: str, **params: str) -> bytes:
    return json.dumps({"method": method, "params": params}).encode() + b"\n"


def answer(result: object) -> bytes:
    return notice({"ok": True, "result": result})


def notice(message: object) -> bytes:
    """One line of the stream: an answer or an event."""
    return json.dumps(message, ensure_ascii=False).encode() + b"\n"


def refusal(error: Error) -> bytes:
    return json.dumps({"ok": False, "error": error}).encode() + b"\n"


def call(path: Path, method: str, **params: str) -> Any:
    """Asks the worker; the result, or `Fault` with the worker's error."""
    with _connect(path, method, params) as conn:
        return _result(_line(conn.makefile("rb")))


async def subscribe(path: Path) -> AsyncIterator[Event]:
    """The worker's events as they come, pings included, until the worker goes.
    Connects at once: `Fault` here, not on the first event, when the worker is not there.
    Asynchronous, so that a reader waiting for the next event holds no thread."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path, limit=ANSWER_LIMIT), READ_TIMEOUT)
    except (OSError, TimeoutError):
        raise Fault("unavailable") from None
    try:
        writer.write(request("events.subscribe"))
        _result(await _next(reader, ANSWER_TIMEOUT))
    except BaseException:
        writer.close()
        raise
    return _events(reader, writer)


async def _events(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> AsyncIterator[Event]:
    try:
        while True:
            event: Event = json.loads(await _next(reader, PING_EVERY * 3))
            yield event
    except Fault:
        return  # the worker went, or stopped sending even pings
    finally:
        writer.close()


async def _next(reader: asyncio.StreamReader, timeout: float) -> bytes:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout)
    except (OSError, TimeoutError, ValueError):  # ValueError: a line over the limit
        raise Fault("unavailable") from None
    if not line.endswith(b"\n"):
        raise Fault("unavailable")
    return line


def _connect(path: Path, method: str, params: dict[str, str]) -> socket.socket:
    """A connection with the request sent."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(ANSWER_TIMEOUT)
    try:
        conn.connect(str(path))
        conn.sendall(request(method, **params))
    except OSError:
        conn.close()
        raise Fault("unavailable") from None
    return conn


def _line(stream: Any) -> bytes:
    try:
        line: bytes = stream.readline(ANSWER_LIMIT)
    except OSError:
        raise Fault("unavailable") from None
    if not line.endswith(b"\n"):
        raise Fault("unavailable")
    return line


def _result(line: bytes) -> Any:
    reply = json.loads(line)
    if not reply["ok"]:
        raise Fault(reply["error"])
    return reply["result"]
