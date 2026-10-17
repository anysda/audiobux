"""The book as an EPUB, put next to the audiobook.

The text is the corrected reading text of the `fix` step: digits stay digits, no
stress marks. Its blocks come from `reading`: chapters are those of the m4b, deeper
headings those of the book's outline. The table of contents lists the chapters in
one level, as the player does. Where the audiobook reads a figure description, the
EPUB shows the picture cut from the book, with the description as its alt text and
the book's caption under it; a listing is shown as code, as the book prints it. The
cover is the m4b's cover. A source that is a reading copy itself (an EPUB) is not
rebuilt: it goes out as it came in.
"""

import html
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from ebooklib import epub

from . import reading
from .book import Book
from .cover import who_and_what

STYLE = """body { margin: 0 4%; }
h1, h2, h3, h4, h5, h6 { text-align: left; hyphens: none; }
p { text-indent: 1.5em; margin: 0; text-align: justify; }
pre { margin: 0.8em 0; white-space: pre-wrap; font-size: 0.85em; }
figure { margin: 1em 0; text-align: center; }
figure img { max-width: 100%; }
figcaption { font-size: 0.9em; font-style: italic; text-indent: 0; }
"""


def build(book: Book) -> Path:
    """`book.epub` in the book's folder."""
    target = book.path("book.epub")
    tmp = book.path("book.epub.tmp")
    data = book.read("fixed.json")
    if data.get("keep_source"):
        shutil.copyfile(book.source, tmp)
    else:
        found = book.read("figures.json") if book.path("figures.json").exists() else []
        cover = book.path("cover.jpg")
        write(data, found, book.dir, tmp, book.name, cover if cover.exists() else None)
    os.replace(tmp, target)
    return target


def write(
    data: dict[str, Any],
    figures: list[dict[str, Any]],
    folder: Path,
    target: Path,
    name: str,
    cover: Path | None = None,
) -> None:
    """An EPUB 3 of `fixed.json` data; figure images are paths inside `folder`."""
    author, title = who_and_what(data.get("author", ""), data.get("title", ""), name)
    book = epub.EpubBook()
    book.set_identifier("urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"audiobook:{name}:{title}")))
    book.set_title(title)
    book.set_language("ru")
    if author:
        book.add_author(author)
    if cover:
        book.set_cover("images/cover.jpg", cover.read_bytes())
        page = book.get_item_with_id("cover")
        assert page is not None  # set_cover has just added it
        page.is_linear = True  # read first; a hidden page must be linked to
    style = epub.EpubItem(uid="style", file_name="style.css", media_type="text/css", content=STYLE)
    book.add_item(style)

    toc: list[epub.Link] = []
    docs: list[epub.EpubHtml] = []
    for n, chapter in enumerate(reading.chapters(data, figures, folder, name), 1):
        file = f"text/{n:04}.xhtml"
        body: list[str] = []
        for block in chapter.blocks:
            if block.kind == "h":
                tag = f"h{min(block.level, 6)}"
                body.append(f"<{tag}>{html.escape(block.text)}</{tag}>")
            elif block.kind == "figure":
                assert block.figure is not None
                body.append(_figure(book, folder, block.figure, block.text))
            elif block.kind == "code":
                body.append(f"<pre><code>{html.escape(block.text)}</code></pre>")
            else:
                body.append(f"<p>{html.escape(block.text)}</p>")
        doc = epub.EpubHtml(title=chapter.title or title, file_name=file, lang="ru")
        doc.content = "\n".join(body)
        doc.add_link(href="../style.css", rel="stylesheet", type="text/css")
        book.add_item(doc)
        docs.append(doc)
        if chapter.title:
            toc.append(epub.Link(file, chapter.title, f"toc{n}"))

    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = (["cover"] if cover else []) + ["nav", *docs]
    epub.write_epub(str(target), book)


def _figure(book: epub.EpubBook, folder: Path, f: dict[str, Any], said: str) -> str:
    source = folder / f["image"]
    file = "images/" + re.sub(r"[^\w.-]+", "_", f["image"].replace("/", "-"))
    kind = "image/jpeg" if source.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    book.add_item(epub.EpubItem(uid=Path(file).stem, file_name=file, media_type=kind, content=source.read_bytes()))
    caption = f.get("caption", "").strip()
    tail = f"<figcaption>{html.escape(caption)}</figcaption>" if caption else ""
    return f'<figure><img src="../{file}" alt="{html.escape(said, quote=True)}"/>{tail}</figure>'
