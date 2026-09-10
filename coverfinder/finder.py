"""Cover finder: author and title in, cover image bytes or nothing out.

Runs in its own container with no secrets and no book files. Asks LitRes
(Russian catalogue) and Open Library, takes the first cover whose book matches
the title and author, and answers GET /cover?author=..&title=.. with the image
bytes (200) or an empty 404. No text from the web ever leaves this process,
and nothing is logged.
"""

import json
import re
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

MAX_IMAGE = 10 * 1024 * 1024
MAX_JSON = 2 * 1024 * 1024
MAX_QUERY = 300
TIMEOUT = 15
ALLOWED = ("litres.ru", "openlibrary.org", "archive.org")
UA = "Mozilla/5.0 (X11; Linux x86_64) audiobux-cover"


def allowed(url: str) -> bool:
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ""
    return p.scheme == "https" and any(host == d or host.endswith("." + d) for d in ALLOWED)


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed(newurl):
            raise ValueError("redirect off the list")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_Redirect)


def get(url: str, limit: int) -> bytes:
    if not allowed(url):
        raise ValueError("host off the list")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with _opener.open(req, timeout=TIMEOUT) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("too big")
    return data


def words(s: str) -> set[str]:
    return set(re.findall(r"\w+", s.lower().replace("ё", "е")))


def title_ok(want: str, got: str) -> bool:
    w, g = words(want), words(got)
    if not w or not g:
        return False
    small, big = sorted((w, g), key=len)
    return small <= big


def author_ok(want: str, names: list[str]) -> bool:
    w = {x for x in words(want) if len(x) >= 3}
    if not w:
        return True
    return bool(w & words(" ".join(names)))


def is_image(data: bytes) -> bool:
    return data.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def litres(author: str, title: str):
    q = urllib.parse.urlencode({"q": f"{author} {title}".strip(), "types": "text_book", "limit": 10})
    data = json.loads(get("https://api.litres.ru/foundation/api/search?" + q, MAX_JSON))
    for item in data["payload"]["data"]:
        book = item.get("instance") or {}
        names = [p.get("full_name") or "" for p in book.get("persons") or [] if p.get("role") == "author"]
        cover = book.get("cover_url") or ""
        if cover.startswith("/") and title_ok(title, book.get("title") or "") and author_ok(author, names):
            yield "https://www.litres.ru" + cover


def openlibrary(author: str, title: str):
    # Open Library takes the author only as an exact phrase, so it is checked here.
    q = urllib.parse.urlencode({"title": title, "fields": "title,author_name,cover_i", "limit": 20})
    data = json.loads(get("https://openlibrary.org/search.json?" + q, MAX_JSON))
    for book in data.get("docs") or []:
        cover = book.get("cover_i")
        if (
            isinstance(cover, int)
            and title_ok(title, book.get("title") or "")
            and author_ok(author, book.get("author_name") or [])
        ):
            yield f"https://covers.openlibrary.org/b/id/{cover}-L.jpg?default=false"


def find(author: str, title: str) -> bytes | None:
    for source in (litres, openlibrary):
        try:
            for url in source(author, title):
                try:
                    data = get(url, MAX_IMAGE)
                except Exception:
                    continue
                if is_image(data):
                    return data
        except Exception:
            continue
    return None


class Handler(BaseHTTPRequestHandler):
    timeout = 30

    def do_GET(self):
        image = None
        try:
            url = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(url.query)
            author = q.get("author", [""])[0].strip()
            title = q.get("title", [""])[0].strip()
            if url.path == "/cover" and title and len(author) <= MAX_QUERY and len(title) <= MAX_QUERY:
                image = find(author, title)
        except Exception:
            image = None
        if image:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(image)))
            self.end_headers()
            self.wfile.write(image)
        else:
            self.send_error(404)

    def send_error(self, code, message=None, explain=None):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("", 8080), Handler).serve_forever()
