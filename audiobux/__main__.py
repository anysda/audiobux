import argparse
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
    one = sub.add_parser("book", help="voice a single local file, without the queue")
    one.add_argument("file", type=Path)
    one.add_argument("--until")
    one.add_argument("--out", type=Path, help="copy the m4b, the epub and <name>/ with the sync.json here")
    args = parser.parse_args()
    settings = Settings.from_env()

    if args.cmd == "worker":
        from . import worker

        # docker stop sends SIGTERM; exiting normally lets the models close.
        # Whatever was voiced so far is on disk and the next start goes on from there.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        queue = worker.Worker(settings, context(settings))
        queue.serve()
        queue.run()
    else:
        voice_one(settings, args.file, args.until, args.out)


def context(settings: Settings) -> "Context":
    from . import models, pipeline
    from .quiet import QuietHours

    return pipeline.Context(settings, models.load(settings), QuietHours(settings.quiet_hours, settings.tz))


def voice_one(settings: Settings, file: Path, until: str | None, out: Path | None) -> None:
    from . import pipeline
    from .book import Book

    if until and until not in [s.name for s in pipeline.STEPS]:
        sys.exit(f"--until: one of {', '.join(s.name for s in pipeline.STEPS)}")
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
