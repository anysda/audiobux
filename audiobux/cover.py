"""The cover of the m4b.

The book's own cover comes first: the EPUB cover image, or the first page of a PDF
or DjVu when it looks like a cover (extraction leaves it as `cover.jpg`). Without
one, `fetch` is asked by author and title; nothing found, the first page will do.
"""

from pathlib import Path

from . import webcover


def ensure(folder: Path, author: str, title: str, name: str) -> None:
    """Leaves `cover.jpg` in the book's folder if there is any cover to be had."""
    target = folder / "cover.jpg"
    if target.exists():
        return
    found = fetch(*who_and_what(author, title, name))
    if found:
        target.write_bytes(found)
        return
    page = folder / "page1.jpg"
    if page.exists():
        target.write_bytes(page.read_bytes())


def fetch(author: str, title: str) -> bytes | None:
    """A checked JPEG of the book's cover from outside, or None."""
    return webcover.fetch(author, title)


def who_and_what(author: str, title: str, name: str) -> tuple[str, str]:
    """Author and title: the book's metadata, or a file named `Автор - Название`."""
    stem = Path(name).stem
    if not author and " - " in stem:
        author, rest = stem.split(" - ", 1)
        if not title or title == stem:
            title = rest
    return author.strip(), (title or stem).strip()
