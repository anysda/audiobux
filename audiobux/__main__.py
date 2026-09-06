import argparse
import logging
import shutil
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
    one = sub.add_parser("book", help="voice a single local file, without the queue")
    one.add_argument("file", type=Path)
    one.add_argument("--until")
    one.add_argument("--out", type=Path, help="copy the m4b here")
    args = parser.parse_args()
    settings = Settings.from_env()

    voice_one(settings, args.file, args.until, args.out)


def context(settings: Settings) -> "Context":
    from . import models, pipeline

    return pipeline.Context(settings, models.load(settings))


def voice_one(settings: Settings, file: Path, until: str | None, out: Path | None) -> None:
    from . import pipeline
    from .book import Book

    if until and until not in [s.name for s in pipeline.STEPS]:
        sys.exit(f"--until: one of {', '.join(s.name for s in pipeline.STEPS)}")
    book = Book.create(settings.work, file.name, lambda p: shutil.copyfile(file, p))
    result = pipeline.run(book, context(settings), until)
    print(book.dir)
    if result and out:
        out.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(result, out / (file.stem + ".m4b"))


if __name__ == "__main__":
    main()
