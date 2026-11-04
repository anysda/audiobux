import argparse
import json
import logging
import shutil
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .config import Settings

if TYPE_CHECKING:
    from .pipeline import Context


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="audiobux")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("worker", help="voice the queue and answer the web process on the socket")
    sub.add_parser("fetch", help="install the runtime and fetch the models the worker runs on, then exit")
    web = sub.add_parser("web", help="the web interface and its API")
    web.add_argument("--host", default="0.0.0.0")
    web.add_argument("--port", type=int, default=8080)
    web.add_argument("--tls", type=Path, help="serve HTTPS with cert.pem and key.pem of this folder")
    password = sub.add_parser("password", help="set the sign-in password, read from stdin")
    password.add_argument("--keep", action="store_true", help="leave a password set before")
    sub.add_parser("openapi", help="print the API description (docs/openapi.json)")
    one = sub.add_parser("book", help="voice a single local file, without the queue")
    one.add_argument("file", type=Path)
    one.add_argument("--until")
    one.add_argument("--out", type=Path, help="copy the m4b, the epub and <name>/ with the sync.json here")
    args = parser.parse_args()
    settings = Settings.from_env()

    # each process imports only its own side: the web one has no pipeline to import
    if args.cmd == "web":
        import uvicorn

        from . import web as app

        tls = {"ssl_certfile": args.tls / "cert.pem", "ssl_keyfile": args.tls / "key.pem"} if args.tls else {}
        uvicorn.run(app.create(settings), host=args.host, port=args.port, proxy_headers=False, **tls)
    elif args.cmd == "password":
        from . import auth

        secret = sys.stdin.readline().rstrip("\n")
        if not secret:
            sys.exit("password: empty")
        try:
            print("set" if auth.set_password(settings.web / "auth.db", secret, args.keep) else "kept")
        except ValueError as e:
            sys.exit(f"password: {e}")
    elif args.cmd == "openapi":
        from . import web as app

        print(json.dumps(app.create(settings).openapi(), ensure_ascii=False, indent=2))
    elif args.cmd == "fetch":
        from . import fetch

        # the worker has no network of its own: all it runs on is put in place here
        fetch.Fetch(settings).run()
    elif args.cmd == "worker":
        from . import worker

        # docker stop sends SIGTERM; exiting normally lets the models close.
        # Whatever was voiced so far is on disk and the next start goes on from there.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        queue = worker.Worker(settings, context(settings))
        queue.serve()
        try:
            queue.run()
        finally:
            queue.unload()
    else:
        voice_one(settings, args.file, args.until, args.out)


def context(settings: Settings) -> "Context":
    from . import models, pipeline
    from .quiet import QuietHours

    return pipeline.Context(settings, models.load(settings), QuietHours(settings.quiet_hours))


def voice_one(settings: Settings, file: Path, until: str | None, out: Path | None) -> None:
    from . import fetch, pipeline
    from .book import Book
    from .runtime import Runtime

    if until and until not in [s.name for s in pipeline.STEPS]:
        sys.exit(f"--until: one of {', '.join(s.name for s in pipeline.STEPS)}")
    # the worker takes books while it installs the runtime (see `fetch`); one book waits for it
    if settings.runtime:
        runtime = Runtime(Path(settings.runtime), settings.data / "runtime")
        if not runtime.ready:
            runtime.install()
        runtime.activate()
    # the models are the ones the worker fetched (see `fetch`), none is downloaded here
    fetch.offline()
    book = Book.create(settings.work, file.name, lambda p: shutil.copyfile(file, p))
    result = pipeline.run(book, context(settings), until)
    print(book.dir)
    if out and book.path("book.epub").exists():
        out.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(book.path("book.epub"), out / (file.stem + ".epub"))
    if result and out:
        out.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(result, out / (file.stem + ".m4b"))
    if out and book.path("sync.json").exists():
        # a folder of its own: the sync's figures/ are named by page, and the next
        # book's would land on them
        (out / file.stem / "figures").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(book.path("sync.json"), out / file.stem / "sync.json")
        for chapter in book.read("sync.json")["chapters"]:
            for src in (b["src"] for b in chapter["blocks"] if b.get("src")):
                shutil.copyfile(book.path(src), out / file.stem / src)


if __name__ == "__main__":
    main()
