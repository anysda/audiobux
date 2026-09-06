"""The cover of the m4b.

The book's own cover comes first: the EPUB cover image, or the first page of a PDF
or DjVu when it looks like a cover (extraction leaves it as `cover.jpg`). Without
one, the first page will do.
"""

from pathlib import Path


def ensure(folder: Path) -> None:
    """Leaves `cover.jpg` in the book's folder if there is any cover to be had."""
    target = folder / "cover.jpg"
    page = folder / "page1.jpg"
    if not target.exists() and page.exists():
        target.write_bytes(page.read_bytes())


def who_and_what(author: str, title: str, name: str) -> tuple[str, str]:
    """Author and title: the book's metadata, or a file named `Автор - Название`."""
    stem = Path(name).stem
    if not author and " - " in stem:
        author, rest = stem.split(" - ", 1)
        if not title or title == stem:
            title = rest
    return author.strip(), (title or stem).strip()
