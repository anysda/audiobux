"""The chapter titles of a book, as the m4b and the reader both show them."""

from typing import Any

from .cover import who_and_what


def titles(data: dict[str, Any], name: str) -> list[str]:
    """The title of each chapter, as the m4b and the reader both show it: an untitled
    first chapter is named after the book, a later one stays untitled."""
    _, title = who_and_what(data.get("author", ""), data.get("title", ""), name)
    return [c["title"] or (title if n == 0 else "") for n, c in enumerate(data["chapters"])]
