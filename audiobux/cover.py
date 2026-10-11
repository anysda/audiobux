"""The cover of the m4b.

The book's own cover comes first: the EPUB cover image, or the first page of a PDF
or DjVu when it looks like a cover (extraction leaves it as `cover.jpg`). Without
one, `fetch` is asked by author and title; nothing found, the first page will do.
"""

from pathlib import Path

from . import titlepage, webcover


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
    """Author and title: the book's own, or what the name of the file says."""
    named_title, named_author = titlepage.from_name(name)
    if not title or title == Path(name).stem:
        title = named_title
    return (author or named_author).strip(), title.strip()
