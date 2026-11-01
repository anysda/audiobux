import asyncio
import dataclasses
import json
import uuid
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from test_worker import CountingTts, make, submit

from audiobux import contract, web
from audiobux.config import Settings

OPENAPI = Path(__file__).parent.parent / "docs" / "openapi.json"
NO_SUCH = "0123456789abcdef0123456789abcdef"
HOST = "10.1.2.3:8080"
ORIGIN = f"http://{HOST}"
M4B = bytes(range(256)) * 40
SYNC = b'{"version": 1, "chapters": [{"title": "\xd0\x93", "blocks": [{"type": "figure", "src": "figures/f-1.jpg"}]}]}'


def client_for(settings: Settings, **kw) -> TestClient:
    """A browser on the service's own page: its Host, its Origin."""
    return TestClient(web.create(settings), base_url=ORIGIN, headers={"origin": ORIGIN}, **kw)


@pytest.fixture
def worker(tmp_path, sock):
    w = make(tmp_path, sock=sock)
    server = w.serve()
    yield w
    server.shutdown()
    server.server_close()


@pytest.fixture
def client(worker):
    return client_for(worker.settings)


@pytest.fixture
def pdf(chekhov_pdf) -> bytes:
    return chekhov_pdf.read_bytes()


def finished(worker, source: Path, title: str = "Рассказы", **files: bytes) -> str:
    """A book as the worker leaves it once voiced, with these files in its folder."""
    book_id = submit(worker, source)
    worker.library.update(book_id, state="done", title=title, duration=1.0)
    folder = worker.settings.library / book_id
    for name, data in files.items():
        path = folder / name.replace("__", "/")
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
    return book_id


def incoming(worker) -> list[Path]:
    return list(worker.settings.incoming.iterdir())


def test_openapi_json_is_the_app():
    """docs/openapi.json is written by `audiobux openapi > docs/openapi.json`."""
    app = web.create(Settings())
    assert json.loads(OPENAPI.read_text()) == app.openapi()


# uploads


def test_upload_becomes_a_book_titled_by_the_file_name(client, worker, pdf):
    sent = client.post("/api/books", params={"name": "Чехов - Рассказы.PDF"}, content=pdf)
    assert sent.status_code == 201 and sent.json()["known"] is False
    book_id = sent.json()["id"]
    assert incoming(worker) == [], "the worker took the upload"
    listed = client.get("/api/books").json()
    assert [(b["id"], b["title"], b["format"], b["state"]) for b in listed] == [
        (book_id, "Чехов - Рассказы", "pdf", "queued")
    ]


def test_the_same_file_again_is_the_book_already_there(client, worker, pdf):
    first = client.post("/api/books", params={"name": "a.pdf"}, content=pdf).json()
    again = client.post("/api/books", params={"name": "b.pdf"}, content=pdf)
    assert (again.status_code, again.json()) == (200, {"id": first["id"], "known": True})
    assert incoming(worker) == [] and len(client.get("/api/books").json()) == 1


@pytest.mark.parametrize(
    ("name", "title"),
    [
        ("../x.pdf", "..x"),
        ("..\\..\\x.djvu", "....x"),
        ("a\nb\r\x00.epub", "ab"),
        ("я" * 500 + ".pdf", "я" * 200),
        ("", ""),
        ("Книга.fb2", "Книга.fb2"),
    ],
)
def test_the_file_name_is_a_title_never_a_path(client, worker, pdf, name, title):
    unique = pdf + b"\n%" + uuid.uuid4().hex.encode()
    book_id = client.post("/api/books", params={"name": name}, content=unique).json()["id"]
    assert client.get(f"/api/books/{book_id}").json()["title"] == title
    assert [p.name for p in worker.settings.library.iterdir()] == [book_id]
    assert [p.name for p in (worker.settings.library / book_id).iterdir()] == ["source.pdf"]


def test_a_name_longer_than_a_file_name_is_422(client, worker, pdf):
    assert client.post("/api/books", params={"name": "x" * 1025}, content=pdf).status_code == 422
    assert incoming(worker) == []


def test_text_named_pdf_is_not_a_book(client, worker):
    sent = client.post("/api/books", params={"name": "book.pdf"}, content="Просто текст.\n".encode() * 200)
    assert (sent.status_code, sent.json()) == (415, {"detail": "not-a-book"})
    assert incoming(worker) == [] and worker.library.books() == []


def test_too_large_is_413_and_dropped(worker, pdf):
    client = client_for(dataclasses.replace(worker.settings, upload_mb=1))
    big = pdf + b"\0" * ((1 << 20) - len(pdf) + 1)
    told = client.post("/api/books", content=big)
    assert (told.status_code, told.json()) == (413, {"detail": "too-large"})

    def untold():  # no Content-Length: counted as it comes
        for i in range(0, len(big), 1 << 16):
            yield big[i : i + (1 << 16)]

    counted = client.post("/api/books", content=untold())
    assert (counted.status_code, counted.json()) == (413, {"detail": "too-large"})
    assert incoming(worker) == [] and worker.library.books() == []
    assert client.post("/api/books", content=big[: 1 << 20]).status_code == 201, "the limit itself is taken"


def test_an_upload_cut_off_halfway_leaves_nothing(worker, pdf):
    app = web.create(worker.settings)
    seen = set()
    parts = [{"type": "http.request", "body": pdf[: len(pdf) // 2], "more_body": True}, {"type": "http.disconnect"}]

    async def receive():
        seen.update(p.name for p in incoming(worker))
        return parts.pop(0)

    async def send(message):
        pass

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/books",
        "raw_path": b"/api/books",
        "query_string": b"name=a.pdf",
        "root_path": "",
        "headers": [(b"host", HOST.encode()), (b"origin", ORIGIN.encode())],
        "client": ("10.1.2.9", 50000),
        "server": ("10.1.2.3", 8080),
    }
    asyncio.run(app(scope, receive, send))
    assert len(seen) == 1 and contract.ID.fullmatch(seen.pop()), "the half was on disk under a fresh id"
    assert incoming(worker) == [] and worker.library.books() == []


# books


def test_book_list_details_retry_and_delete(client, worker, chekhov_epub, chekhov_pdf):
    queued = submit(worker, chekhov_epub)
    failed = submit(worker, chekhov_pdf)
    worker.library.update(failed, state="failed", step="ocr", error="Этап «распознавание сканов»: ...")
    assert [b["id"] for b in client.get("/api/books").json()] == [queued, failed]

    got = client.get(f"/api/books/{failed}").json()
    assert (got["state"], got["step"], got["error"], got["chapters"]) == ("failed", "ocr", got["error"], [])
    assert got["error"].startswith("Этап") and got["percent"] == 0

    assert (client.post(f"/api/books/{queued}/retry").status_code, client.get(f"/api/books/{queued}").is_success) == (
        409,
        True,
    )
    again = client.post(f"/api/books/{failed}/retry")
    assert again.status_code == 200 and (again.json()["state"], again.json()["error"]) == ("queued", None)

    assert client.delete(f"/api/books/{queued}").status_code == 204
    assert client.get(f"/api/books/{queued}").json() == {"detail": "not-found"}
    for reply in (client.delete(f"/api/books/{queued}"), client.post(f"/api/books/{queued}/retry")):
        assert reply.status_code == 404
    assert [b["id"] for b in client.get("/api/books").json()] == [failed]


def test_ids_are_checked_before_the_worker_is_asked(client):
    for bad in ("..%2F..%2Fetc", "ABCDEF0123456789ABCDEF0123456789", "1234", NO_SUCH + "0"):
        assert client.get(f"/api/books/{bad}").status_code in (404, 422), bad
    assert client.get("/api/books/" + NO_SUCH.upper()).status_code == 422


# files


def test_cover_is_404_without_one(client, worker, chekhov_epub, chekhov_pdf):
    plain = finished(worker, chekhov_epub)
    with_cover = finished(worker, chekhov_pdf, **{"cover.jpg": b"\xff\xd8jpeg"})
    assert client.get(f"/api/books/{plain}/cover").json() == {"detail": "not-found"}
    got = client.get(f"/api/books/{with_cover}/cover")
    assert (got.status_code, got.headers["content-type"], got.content) == (200, "image/jpeg", b"\xff\xd8jpeg")
    assert client.get(f"/api/books/{with_cover}").json()["has_cover"] is True


@pytest.fixture
def m4b(worker, chekhov_epub) -> str:
    return finished(worker, chekhov_epub, **{"book.m4b": M4B})


@pytest.mark.parametrize(
    ("asked", "status", "content_range", "body"),
    [
        (None, 200, None, M4B),
        ("bytes=0-", 206, f"bytes 0-{len(M4B) - 1}/{len(M4B)}", M4B),
        ("bytes=100-199", 206, f"bytes 100-199/{len(M4B)}", M4B[100:200]),
        ("bytes=-500", 206, f"bytes {len(M4B) - 500}-{len(M4B) - 1}/{len(M4B)}", M4B[-500:]),
        (f"bytes={len(M4B) - 1}-", 206, f"bytes {len(M4B) - 1}-{len(M4B) - 1}/{len(M4B)}", M4B[-1:]),
        (f"bytes={len(M4B) - 1}-{len(M4B) + 99}", 206, f"bytes {len(M4B) - 1}-{len(M4B) - 1}/{len(M4B)}", M4B[-1:]),
        (f"bytes={len(M4B)}-", 416, f"bytes */{len(M4B)}", b""),
        # several parts: the whole file, as HTTP lets a server that does not do them answer
        ("bytes=0-9,20-29", 200, None, M4B),
    ],
)
def test_audio_with_range(client, m4b, asked, status, content_range, body):
    got = client.get(f"/api/books/{m4b}/audio", headers={"range": asked} if asked else {})
    assert got.status_code == status
    assert got.headers.get("content-range") == content_range
    if status != 416:
        assert got.content == body and got.headers["content-type"] == "audio/mp4"
        assert got.headers["accept-ranges"] == "bytes" and "content-disposition" not in got.headers


def test_audio_of_a_book_not_voiced_yet_is_404(client, worker, chekhov_epub):
    book_id = submit(worker, chekhov_epub)
    for what in ("audio", "m4b", "epub", "sync", "cover"):
        assert client.get(f"/api/books/{book_id}/{what}").status_code == 404, what


@pytest.mark.parametrize(
    ("title", "disposition"),
    [
        ("Рассказы: «избранное»/1", "attachment; filename*=utf-8''" + quote("Рассказы избранное 1.m4b")),
        ('Tales "of" a\nday...', "attachment; filename*=utf-8''Tales%20of%20a%20day.m4b"),
        ("../..", 'attachment; filename="book.m4b"'),
        ("", 'attachment; filename="book.m4b"'),
        ("Book_1-2", 'attachment; filename="Book_1-2.m4b"'),
    ],
)
def test_downloads_are_named_by_the_title_in_a_safe_form(client, worker, chekhov_epub, title, disposition):
    book_id = finished(worker, chekhov_epub, title, **{"book.m4b": M4B, "book.epub": b"PK epub"})
    got = client.get(f"/api/books/{book_id}/m4b")
    assert (got.status_code, got.content, got.headers["content-disposition"]) == (200, M4B, disposition)
    epub = client.get(f"/api/books/{book_id}/epub")
    assert (epub.headers["content-type"], epub.content) == ("application/epub+zip", b"PK epub")
    assert epub.headers["content-disposition"] == disposition.replace(".m4b", ".epub")


def test_sync_is_given_as_it_is(client, worker, chekhov_epub):
    book_id = finished(worker, chekhov_epub, **{"sync.json": SYNC})
    got = client.get(f"/api/books/{book_id}/sync")
    assert (got.status_code, got.headers["content-type"], got.content) == (200, "application/json", SYNC)


def test_figures_by_their_name_only(client, worker, chekhov_epub):
    book_id = finished(worker, chekhov_epub, **{"figures__f-1.jpg": b"\xff\xd8f", "secret.jpg": b"no", "x.png": b"no"})
    got = client.get(f"/api/books/{book_id}/figures/f-1.jpg")
    assert (got.status_code, got.headers["content-type"], got.content) == (200, "image/jpeg", b"\xff\xd8f")
    for bad in (
        "..%2Fsecret.jpg",
        "%2e%2e%2fsecret.jpg",
        "%2e%2e",
        "x.png",
        "F-1.jpg",
        "f-1.jpeg",
        "f-1.jpg%00",
        "a" * 65 + ".jpg",
        "nothing.jpg",
    ):
        reply = client.get(f"/api/books/{book_id}/figures/{bad}")
        assert reply.status_code == 404 and reply.content != b"no", bad


# listening


def test_position_and_listened(client, worker, chekhov_epub):
    book_id = submit(worker, chekhov_epub)
    assert client.get("/api/listening").json() == []
    start = {"id": book_id, "position": 0.0, "listened": False}
    assert client.get(f"/api/books/{book_id}/position").json() == start

    assert client.put(f"/api/books/{book_id}/position", json={"seconds": 12.5}).json() == start | {"position": 12.5}
    assert client.put(f"/api/books/{book_id}/listened").json() == start | {"position": 12.5, "listened": True}
    assert client.get("/api/listening").json() == [start | {"position": 12.5, "listened": True}]
    assert client.delete(f"/api/books/{book_id}/listened").json() == start | {"position": 12.5}
    assert client.get(f"/api/books/{book_id}/position").json() == start | {"position": 12.5}


def test_listening_survives_a_restart_of_the_web_process(worker, chekhov_epub):
    book_id = submit(worker, chekhov_epub)
    client_for(worker.settings).put(f"/api/books/{book_id}/position", json={"seconds": 3})
    assert client_for(worker.settings).get("/api/listening").json()[0]["position"] == 3
    assert (worker.settings.web / "listening.db").is_file()


def test_the_web_process_makes_its_own_folder(tmp_path, worker, chekhov_epub):
    book_id = submit(worker, chekhov_epub)
    clean = dataclasses.replace(worker.settings, data=tmp_path / "clean")  # nothing the worker made
    client = client_for(clean)
    assert client.put(f"/api/books/{book_id}/position", json={"seconds": 4}).status_code == 200
    assert client.get(f"/api/books/{book_id}/position").json()["position"] == 4


@pytest.mark.parametrize(
    "body", [b"-1", b"-0.001", b'"abc"', b"null", b'"NaN"', b"NaN", b"Infinity", b"[1]", b'"12"x', b""]
)
def test_a_position_that_is_not_seconds_is_422(client, worker, chekhov_epub, body):
    """And the answer does not carry what was sent: NaN would not even go into JSON."""
    book_id = submit(worker, chekhov_epub)
    sent = client.put(
        f"/api/books/{book_id}/position",
        content=b'{"seconds": ' + body + b"}",
        headers={"content-type": "application/json"},
    )
    assert sent.status_code == 422 and all(set(e) == {"loc", "msg", "type"} for e in sent.json()["detail"])
    assert client.get(f"/api/books/{book_id}/position").json()["position"] == 0


def test_listening_of_an_unknown_book_is_404(client):
    for method, path, body in (
        ("GET", "position", None),
        ("PUT", "position", {"seconds": 1}),
        ("PUT", "listened", None),
        ("DELETE", "listened", None),
    ):
        reply = client.request(method, f"/api/books/{NO_SUCH}/{path}", json=body)
        assert (reply.status_code, reply.json()) == (404, {"detail": "not-found"}), (method, path)
    assert client.get("/api/listening").json() == []


def test_delete_while_voicing(tmp_path, sock, chekhov_epub):
    """Deleted at the second piece: the book, its files and where the listener was are gone."""
    seen = {}

    def delete_now(n):
        if n == 2:
            book_id = seen["id"]
            assert client.put(f"/api/books/{book_id}/position", json={"seconds": 7}).status_code == 200
            assert client.get(f"/api/books/{book_id}").json()["state"] == "running"
            seen["deleted"] = client.delete(f"/api/books/{book_id}").status_code

    w = make(tmp_path, tts=CountingTts(on_call=delete_now), sock=sock)
    server = w.serve()
    try:
        client = client_for(w.settings)
        seen["id"] = book_id = client.post("/api/books", content=chekhov_epub.read_bytes()).json()["id"]
        w.run(until_idle=True)
        assert seen["deleted"] == 204 and w.ctx.models.tts.calls == 2, "stopped at the next piece"
        for what in ("", "/audio", "/sync", "/position", "/m4b", "/cover"):
            reply = client.get(f"/api/books/{book_id}{what}")
            assert (reply.status_code, reply.json()) == (404, {"detail": "not-found"}), what
        assert client.get("/api/listening").json() == [] and client.get("/api/books").json() == []
        assert not (w.settings.library / book_id).exists() and not (w.settings.work / book_id).exists()
    finally:
        server.shutdown()
        server.server_close()


# status and events


def test_status(client):
    status = client.get("/api/status").json()
    assert status["queue"] == 0 and {f["name"] for f in status["formats"]} == {"pdf", "djvu", "epub"}


def test_events_stream(client, worker, chekhov_epub):
    with client.stream("GET", "/api/events") as events:
        assert events.headers["content-type"].startswith("text/event-stream")
        assert events.headers["x-content-type-options"] == "nosniff"
        # a proxy that buffers or keeps the stream would hold the progress back
        assert events.headers["cache-control"] == "no-cache"
        assert events.headers["x-accel-buffering"] == "no"
        book_id = submit(worker, chekhov_epub)
        lines = events.iter_lines()
        assert next(lines) == "event: book"
        data = json.loads(next(lines).removeprefix("data: "))
        assert data["type"] == "book" and data["book"]["id"] == book_id
        assert next(lines) == ""
        worker.delete(book_id)
        assert [next(lines), next(lines)] == ["event: deleted", f'data: {{"type": "deleted", "id": "{book_id}"}}']


def test_a_ping_is_a_comment(client, worker, monkeypatch):
    monkeypatch.setattr(contract, "PING_EVERY", 0.05)
    with client.stream("GET", "/api/events") as events:
        assert next(events.iter_lines()) == ": ping"


# the worker away


def asked_of_the_worker(book_id: str) -> list[tuple[str, str, dict]]:
    return [
        ("GET", "/api/books", {}),
        ("GET", f"/api/books/{book_id}", {}),
        ("DELETE", f"/api/books/{book_id}", {}),
        ("POST", f"/api/books/{book_id}/retry", {}),
        ("GET", f"/api/books/{book_id}/m4b", {}),
        ("GET", f"/api/books/{book_id}/epub", {}),
        ("GET", f"/api/books/{book_id}/position", {}),
        ("PUT", f"/api/books/{book_id}/position", {"json": {"seconds": 1}}),
        ("PUT", f"/api/books/{book_id}/listened", {}),
        ("DELETE", f"/api/books/{book_id}/listened", {}),
        ("GET", "/api/status", {}),
        ("GET", "/api/events", {}),
    ]


def test_no_worker_is_503_and_the_files_are_still_there(tmp_path, chekhov_epub, pdf):
    w = make(tmp_path)  # its folders and a voiced book, but nobody on the socket
    book_id = finished(w, chekhov_epub, **{"book.m4b": M4B, "book.epub": b"PK", "sync.json": SYNC})
    client = client_for(dataclasses.replace(w.settings, socket=str(tmp_path / uuid.uuid4().hex[:8])))
    for method, path, extra in asked_of_the_worker(book_id):
        reply = client.request(method, path, **extra)
        assert (reply.status_code, reply.json()) == (503, {"detail": "unavailable"}), (method, path)

    sent = client.post("/api/books", params={"name": "a.pdf"}, content=pdf)
    assert (sent.status_code, sent.json()) == (503, {"detail": "unavailable"})
    assert incoming(w) == [], "an upload the worker did not take is dropped"
    # what the web process has itself goes on without the worker
    assert client.get(f"/api/books/{book_id}/audio", headers={"range": "bytes=0-9"}).content == M4B[:10]
    assert client.get(f"/api/books/{book_id}/sync").content == SYNC
    assert client.get("/api/listening").json() == []


# the interface


@pytest.fixture
def frontend(tmp_path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text('<!doctype html><script type="module" src="/assets/app.js"></script>')
    (root / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("no")
    return root


def test_the_interface_is_served_and_any_other_path_is_its_page(worker, frontend):
    client = client_for(dataclasses.replace(worker.settings, frontend=frontend))
    page = (frontend / "index.html").read_text()
    for path in ("/", "/books/abc", "/player?x=1", "/assets/missing.js", "/%2e%2e/secret.txt", "/..%2Fsecret.txt"):
        got = client.get(path)
        assert (got.status_code, got.text) == (200, page), path
        assert got.headers["content-type"].startswith("text/html")
    script = client.get("/assets/app.js")
    assert script.text == "console.log(1)" and "javascript" in script.headers["content-type"]
    for path in ("/api", "/api/", "/api/nothing", "/api/books/x/y/z"):
        assert client.get(path).json() == {"detail": "not-found"}, path


def test_the_page_and_the_answers_about_books_are_never_kept(client, worker, frontend, chekhov_epub):
    pages = client_for(dataclasses.replace(worker.settings, frontend=frontend))
    for path in ("/", "/index.html", "/books/abc"):
        assert pages.get(path).headers["cache-control"] == "no-store", path
    assert "no-store" not in pages.get("/assets/app.js").headers.get("cache-control", "")
    book_id = submit(worker, chekhov_epub)
    for path in ("/api/books", f"/api/books/{book_id}", "/api/status", "/api/listening", "/api/books/x"):
        assert client.get(path).headers["cache-control"] == "no-store", path
    moved = client.put(f"/api/books/{book_id}/position", json={"seconds": 1})
    assert moved.headers["cache-control"] == "no-store"


def test_files_of_a_book_keep_their_validators(client, m4b):
    got = client.get(f"/api/books/{m4b}/audio")
    assert got.status_code == 200 and got.headers["etag"]
    assert "cache-control" not in got.headers


def test_without_the_interface_built_it_is_404_and_the_api_works(worker, tmp_path):
    client = client_for(dataclasses.replace(worker.settings, frontend=tmp_path / "none"))
    for path in ("/", "/books/abc"):
        assert client.get(path).status_code == 404
    assert client.get("/api/books").json() == []


# the guard


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("10.1.2.3:8080", True),
        ("10.1.2.3", True),
        ("127.0.0.1:8080", True),
        ("[::1]:8080", True),
        ("[fe80::1]", True),
        ("localhost:8080", True),
        ("LOCALHOST", True),
        ("books.home:8080", True),
        ("Books.Home", True),
        ("evil.example", False),
        ("evil.example:8080", False),
        ("books.home.evil.example", False),
        ("10.1.2.3.evil.example", False),
        ("1.2.3.4.5", False),
        ("::1", False),
        ("[evil]:80", False),
        ("localhost@evil.example", False),
        ("", False),
    ],
)
def test_host_is_this_service_or_403(worker, host, allowed):
    client = client_for(dataclasses.replace(worker.settings, hosts=" books.home , other.home"))
    reply = client.get("/api/books", headers={"host": host})
    assert reply.status_code == (200 if allowed else 403), host
    if not allowed:
        assert reply.json() == {"detail": "forbidden"}


@pytest.mark.parametrize(
    "origin", [None, "", "null", "http://evil.example", "http://10.1.2.3:8081", "http://10.1.2.3", "ftp://" + HOST]
)
def test_a_change_from_another_origin_is_403(client, worker, chekhov_epub, pdf, origin):
    book_id = submit(worker, chekhov_epub)
    headers = {"origin": origin} if origin is not None else {}
    client.headers.pop("origin")
    for method, path, extra in (
        ("POST", "/api/books", {"content": pdf}),
        ("DELETE", f"/api/books/{book_id}", {}),
        ("POST", f"/api/books/{book_id}/retry", {}),
        ("PUT", f"/api/books/{book_id}/position", {"json": {"seconds": 1}}),
        ("PUT", f"/api/books/{book_id}/listened", {}),
        ("DELETE", f"/api/books/{book_id}/listened", {}),
    ):
        reply = client.request(method, path, headers=headers, **extra)
        assert (reply.status_code, reply.json()) == (403, {"detail": "forbidden"}), (method, path)
    assert [b["id"] for b in client.get("/api/books", headers=headers).json()] == [book_id], "reading needs no Origin"
    assert incoming(worker) == [] and client.get("/api/listening").json() == []


def test_the_own_origin_may_change(client, worker, chekhov_epub):
    book_id = submit(worker, chekhov_epub)
    https = client.put(f"/api/books/{book_id}/listened", headers={"origin": "https://" + HOST.upper()})
    assert https.status_code == 200


def test_security_headers_on_every_answer(worker, frontend, monkeypatch):
    client = client_for(dataclasses.replace(worker.settings, frontend=frontend), raise_server_exceptions=False)
    answers = [
        client.get("/api/status"),
        client.get("/"),
        client.get("/assets/app.js"),
        client.get(f"/api/books/{NO_SUCH}"),
        client.get("/api/books/x"),
        client.get("/api/books", headers={"host": "evil.example"}),
        client.post("/api/books", headers={"origin": "http://evil.example"}),
        client.put(f"/api/books/{NO_SUCH}/position", json={"seconds": -1}),
    ]
    monkeypatch.setattr(contract, "call", lambda *a, **k: 1 / 0)
    answers.append(client.get("/api/books"))
    assert [a.status_code for a in answers] == [200, 200, 200, 404, 422, 403, 403, 422, 500]
    for reply in answers:
        assert reply.headers["content-security-policy"] == (
            "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
        )
        assert reply.headers["x-content-type-options"] == "nosniff"
        assert reply.headers["referrer-policy"] == "no-referrer"
    assert answers[-1].json() == {"detail": "internal"}, "nothing of the error goes out"
