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

import json
import re
import unicodedata
from typing import Literal, NotRequired, TypedDict

REQUEST_LIMIT = 4096
READ_TIMEOUT = 3.0
# an idle event stream sends a ping this often, so a gone reader is noticed
PING_EVERY = 15.0

ID = re.compile(r"[0-9a-f]{32}")
TITLE_LIMIT = 200

# method -> its parameters, every one an id but the title
METHODS: dict[str, tuple[str, ...]] = {
    "book.submit": ("upload_id", "title"),
    "book.delete": ("id",),
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
