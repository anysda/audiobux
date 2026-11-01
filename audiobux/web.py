"""The web process: the API of the interface and the interface itself.

It does nothing with books itself: it does not open them, sees neither the
models nor the work in progress nor the card, and goes nowhere on the network.
Everything about books it asks the worker on the socket (`contract`); it only
takes uploaded files into `incoming/`, gives out what is in `library/` and keeps
where the listener is (`listening`) in its own folder.

With no sign-in, any site open in the browser could reach the service, so `Guard`
lets in only requests for this host and changes only from its own pages.
"""

import ipaddress
import json
import os
import re
import stat
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any, cast
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi import Path as Param
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import contract, formats
from .config import Settings
from .listening import Listening, Place

BookId = Annotated[str, Param(pattern=f"^{contract.ID.pattern}$", description="the book's id, 32 hex digits")]
FileName = Annotated[str, Query(max_length=1024, description="the file's name: the title, if the book has none")]

# the worker's errors as HTTP answers; the detail is the error code itself
STATUS = {"not-found": 404, "conflict": 409, "not-a-book": 415, "too-large": 413, "unavailable": 503}
FIGURE = re.compile(r"[0-9a-z_-]{1,64}\.jpg")

# no inline scripts or styles, nothing from elsewhere, never inside a frame
CSP = "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
SECURITY = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
]
# a name in Host: an IPv6 address in brackets or anything without a colon, then the port
HOST = re.compile(r"(?:\[(?P<v6>[0-9a-f:.]+)\]|(?P<name>[^:\[\]/@]+))(?::\d{1,5})?")


class Problem(BaseModel):
    detail: str = Field(description="error code: " + ", ".join(sorted([*STATUS, "bad-request", "forbidden"])))


class Position(BaseModel):
    seconds: float = Field(ge=0, allow_inf_nan=False, description="seconds from the start of the m4b")


class File(FileResponse):
    """A file with Range. A single range only: several are asked by no player, and
    the whole file is the answer HTTP allows a server that does not do them."""

    max_ranges = 1


def _problems(*codes: int) -> dict[int | str, dict[str, Any]]:
    return {code: {"model": Problem} for code in codes}


def _file_answer(media_type: str) -> dict[int | str, dict[str, Any]]:
    return {200: {"description": "the file", "content": {media_type: {}}}, **_problems(404)}


EVENTS = """Server-sent events of the worker: `event: book` with `data` an object
`{"type": "book", "book": Book}` when a book is added, moves on or changes state;
`event: deleted` with `{"type": "deleted", "id": "<id>"}`; and a `: ping` comment
every 15 s while nothing happens."""
UPLOAD = {
    "requestBody": {
        "required": True,
        "description": "the file itself, sent as it is; its format is told by its content",
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    }
}


def create(settings: Settings) -> FastAPI:
    app = FastAPI(
        title="audiobux",
        version="0.1.0",
        description="A book file in, an audiobook with chapters and its text out. A request for another host, "
        "or a POST, PUT or DELETE without the service's own Origin, is refused with 403.",
        # the API pages load scripts from elsewhere, which the CSP does not let in
        docs_url=None,
        redoc_url=None,
    )
    app.add_exception_handler(RequestValidationError, _invalid)
    app.add_middleware(Guard, hosts=frozenset(h.strip().lower() for h in settings.hosts.split(",") if h.strip()))
    socket = settings.worker_socket
    library = settings.library
    places = Listening(settings.web / "listening.db")
    limit = settings.upload_mb << 20

    def ask(method: str, **params: str) -> Any:
        try:
            return contract.call(socket, method, **params)
        except contract.Fault as f:
            raise HTTPException(STATUS.get(f.error, 502), f.error) from None

    def details(book_id: str) -> contract.BookDetails:
        return cast(contract.BookDetails, ask("book.get", id=book_id))

    @app.post(
        "/api/books",
        status_code=201,
        summary="Upload a book file",
        responses={
            200: {"description": "the same file was there already: that book, `known` true"},
            **_problems(400, 413, 415, 503),
        },
        openapi_extra=UPLOAD,
    )
    async def upload(request: Request, response: Response, name: FileName = "") -> contract.Submitted:
        """The file goes to `incoming/` under a fresh id and only there; it is gone again
        when it is too large, not a book, cut off, or the worker does not take it."""
        if int(request.headers.get("content-length", "0")) > limit:
            raise HTTPException(413, "too-large")
        upload_id = uuid.uuid4().hex
        path = settings.incoming / upload_id
        try:
            await _receive(request, path, limit)
            if await run_in_threadpool(formats.detect, path) is None:
                raise HTTPException(415, "not-a-book")
            submitted: contract.Submitted = await run_in_threadpool(
                ask, "book.submit", upload_id=upload_id, title=_title(name)
            )
        finally:
            path.unlink(missing_ok=True)
        if submitted["known"]:
            response.status_code = 200
        return submitted

    @app.get("/api/books", summary="All books, the queue included", responses=_problems(503))
    def books() -> list[contract.Book]:
        return cast(list[contract.Book], ask("library.list"))

    @app.get("/api/books/{book_id}", summary="A book with its chapters", responses=_problems(404, 503))
    def book(book_id: BookId) -> contract.BookDetails:
        return details(book_id)

    @app.delete("/api/books/{book_id}", status_code=204, summary="Delete a book", responses=_problems(404, 503))
    def delete(book_id: BookId) -> Response:
        """A book being voiced stops; its files go, and where the listener was in it."""
        ask("book.delete", id=book_id)
        places.forget(book_id)
        return Response(status_code=204)

    @app.post(
        "/api/books/{book_id}/retry",
        summary="Try a failed book again, from the step it failed at",
        responses=_problems(404, 409, 503),
    )
    def retry(book_id: BookId) -> contract.Book:
        return cast(contract.Book, ask("book.retry", id=book_id))

    @app.get(
        "/api/books/{book_id}/cover",
        summary="The cover; 404 when the book has none",
        response_class=Response,
        responses=_file_answer("image/jpeg"),
    )
    def cover(book_id: BookId) -> Response:
        return _file(library / book_id / "cover.jpg", "image/jpeg")

    @app.get(
        "/api/books/{book_id}/audio",
        summary="The audiobook to play, with Range",
        response_class=Response,
        responses={
            **_file_answer("audio/mp4"),
            206: {"description": "the asked range"},
            416: {"description": "the range starts past the end"},
        },
    )
    def audio(book_id: BookId) -> Response:
        return _file(library / book_id / "book.m4b", "audio/mp4")

    @app.get(
        "/api/books/{book_id}/m4b",
        summary="Download the m4b, named by the title",
        response_class=Response,
        responses={**_file_answer("audio/mp4"), **_problems(503)},
    )
    def m4b(book_id: BookId) -> Response:
        return _file(library / book_id / "book.m4b", "audio/mp4", _download(details(book_id)["title"], ".m4b"))

    @app.get(
        "/api/books/{book_id}/epub",
        summary="Download the EPUB, named by the title",
        response_class=Response,
        responses={**_file_answer("application/epub+zip"), **_problems(503)},
    )
    def epub(book_id: BookId) -> Response:
        name = _download(details(book_id)["title"], ".epub")
        return _file(library / book_id / "book.epub", "application/epub+zip", name)

    @app.get(
        "/api/books/{book_id}/sync",
        summary="The text of the book with the times it is heard at (sync.json, version 1)",
        response_class=Response,
        responses=_file_answer("application/json"),
    )
    def sync(book_id: BookId) -> Response:
        """Given as the worker wrote it: chapters of `h`, `p`, `code` and `figure` blocks;
        a figure's `src` is `figures/<name>.jpg`, under this book's `figures` route."""
        return _file(library / book_id / "sync.json", "application/json")

    @app.get(
        "/api/books/{book_id}/figures/{name}",
        summary="A figure of the book's text",
        response_class=Response,
        responses=_file_answer("image/jpeg"),
    )
    def figure(book_id: BookId, name: Annotated[str, Param(description="`[0-9a-z_-]{1,64}.jpg`")]) -> Response:
        if not FIGURE.fullmatch(name):
            raise HTTPException(404, "not-found")
        return _file(library / book_id / "figures" / name, "image/jpeg")

    @app.get("/api/listening", summary="Where the listener is in every book listened to")
    def listening() -> list[Place]:
        return places.all()

    @app.get("/api/books/{book_id}/position", summary="Where the listener is", responses=_problems(404, 503))
    def position(book_id: BookId) -> Place:
        details(book_id)
        return places.get(book_id)

    @app.put("/api/books/{book_id}/position", summary="Remember where the listener is", responses=_problems(404, 503))
    def move(book_id: BookId, body: Position) -> Place:
        details(book_id)
        return places.move(book_id, body.seconds)

    @app.put("/api/books/{book_id}/listened", summary="Mark the book listened", responses=_problems(404, 503))
    def listened(book_id: BookId) -> Place:
        details(book_id)
        return places.mark(book_id, True)

    @app.delete("/api/books/{book_id}/listened", summary="Mark the book not listened", responses=_problems(404, 503))
    def unlistened(book_id: BookId) -> Place:
        details(book_id)
        return places.mark(book_id, False)

    @app.get(
        "/api/status",
        summary="Models, the card, the queue, quiet hours and the formats taken",
        responses=_problems(503),
    )
    def status() -> contract.Status:
        return cast(contract.Status, ask("status.read"))

    @app.get(
        "/api/events",
        summary="Live progress of the books",
        response_class=StreamingResponse,
        responses={
            200: {"description": EVENTS, "content": {"text/event-stream": {"schema": {"type": "string"}}}},
            **_problems(503),
        },
    )
    async def events() -> StreamingResponse:
        try:
            stream = await contract.subscribe(socket)
        except contract.Fault as f:
            raise HTTPException(STATUS.get(f.error, 502), f.error) from None
        # nothing between the worker and the page may keep the stream back or keep it
        headers = {"cache-control": "no-cache", "x-accel-buffering": "no"}
        return StreamingResponse(_sse(stream), media_type="text/event-stream", headers=headers)

    @app.get("/{path:path}", include_in_schema=False)
    def page(path: str) -> Response:
        """The interface: its files as they are, and its page for any other path, which
        the interface routes itself."""
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "not-found")
        root = settings.frontend.resolve()
        target = (root / path).resolve()
        if target.is_relative_to(root) and target.is_file() and target != root / "index.html":
            return FileResponse(target)
        if not (root / "index.html").is_file():
            raise HTTPException(404, "not-found")
        # the page names the scripts of this build: a phone keeping an old one keeps the old interface
        return FileResponse(root / "index.html", headers={"cache-control": "no-store"})

    return app


class Guard:
    """Every answer gets the security headers, refusals and errors included, and an
    answer of the API that is not a file is not kept by the browser (`_fresh`). A request
    is refused with 403 when its Host is not this service (DNS rebinding), or when it
    changes something and its Origin is missing or another site (CSRF)."""

    def __init__(self, app: ASGIApp, hosts: frozenset[str]) -> None:
        self.app, self.hosts = app, hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def secured(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message["headers"] = [*message.get("headers", ()), *SECURITY, *_fresh(scope["path"], message)]
            await send(message)

        headers = Headers(scope=scope)
        host = headers.get("host", "").lower()
        changes = scope["method"] not in ("GET", "HEAD", "OPTIONS")
        if not self.ours(host) or (changes and not _same_origin(headers.get("origin"), host)):
            await JSONResponse({"detail": "forbidden"}, 403)(scope, receive, secured)
            return
        try:
            await self.app(scope, receive, secured)
        except Exception:
            if not started:  # the server's own 500 would go out without the headers
                await JSONResponse({"detail": "internal"}, 500)(scope, receive, secured)
            raise

    def ours(self, host: str) -> bool:
        """An IP address, localhost or a name in `AUDIOBUX_HOSTS`, any port."""
        found = HOST.fullmatch(host)
        if not found:
            return False
        if found["v6"]:
            return _is_ip(found["v6"])
        name = found["name"]
        return name == "localhost" or name in self.hosts or _is_ip(name)


def _fresh(path: str, message: Message) -> list[tuple[bytes, bytes]]:
    """What the API tells of the books is asked anew every time: an answer kept by the
    browser shows a book where it was. Files carry their own validators and are left be."""
    told = {name.lower() for name, _ in message.get("headers", ())}
    if not path.startswith("/api/") or told & {b"cache-control", b"etag"}:
        return []
    return [(b"cache-control", b"no-store")]


def _invalid(request: Request, error: Exception) -> JSONResponse:
    """FastAPI's own 422 without the values sent: nothing sent comes back, and a value
    such as NaN would not even go into JSON."""
    errors = cast(RequestValidationError, error).errors()
    return JSONResponse({"detail": [{k: e[k] for k in ("loc", "msg", "type")} for e in errors]}, 422)


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _same_origin(origin: str | None, host: str) -> bool:
    if not origin:
        return False
    url = urlsplit(origin)
    return url.scheme in ("http", "https") and url.netloc.lower() == host


async def _receive(request: Request, path: Path, limit: int) -> None:
    """The body into `path`, at most `limit` bytes of it; a fresh file, written as it comes."""
    size = 0
    try:
        with path.open("xb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, "too-large")
                f.write(chunk)
    except ClientDisconnect:
        raise HTTPException(400, "bad-request") from None


def _title(name: str) -> str:
    """The title a file's name gives: the name without the format's extension."""
    lower = name.lower()
    ext = next((e for e in formats.extensions() if lower.endswith(e)), "")
    return contract.title(name[: len(name) - len(ext)])


def _download(title: str, ext: str) -> str:
    """A file name from the title: letters, digits, spaces and plain punctuation only."""
    name = " ".join(re.sub(r"[^\w .,()-]+", " ", title).split()).strip(" .")
    return (name[:100].strip() or "book") + ext


def _file(path: Path, media_type: str, download: str | None = None) -> Response:
    try:
        found = os.stat(path)
    except FileNotFoundError:
        raise HTTPException(404, "not-found") from None
    if not stat.S_ISREG(found.st_mode):
        raise HTTPException(404, "not-found")
    return File(path, media_type=media_type, filename=download, stat_result=found)


async def _sse(stream: AsyncIterator[contract.Event]) -> AsyncIterator[str]:
    async for event in stream:
        if event["type"] == "ping":
            yield ": ping\n\n"
        else:
            yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
