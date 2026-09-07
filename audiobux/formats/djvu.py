"""DjVu through the djvulibre tools: chapters from the text layer and the outline;
a scan comes out as page images for OCR."""

import re
import subprocess
import tempfile
from pathlib import Path

import pymupdf

from .document import Document
from .pages import from_pages, has_text_layer, top_levels


def read(path: Path, out: Path, depth: int) -> Document:
    count = int(_run("djvused", "-e", "n", str(path)))
    pages: list[list[str]] = []
    for n in range(1, count + 1):
        text = _run("djvutxt", f"--page={n}", str(path))
        pages.append([b for b in re.split(r"[\x0b\x1d\x1f]|\n\s*\n", text) if b.strip()])
    outline = _outline(path)
    first_page = _page(path, 1, dpi=100).tobytes("jpg")
    book = Document(
        outline=outline,
        # a page of text is not a cover
        cover=first_page if len("".join(pages[0]).strip()) < 100 else None,
        first_page=first_page,
    )
    if has_text_layer(pages):
        book.chapters = from_pages(pages, top_levels(outline, depth))
        return book
    (out / "pages").mkdir(exist_ok=True)
    for n in range(1, count + 1):
        book.pages.append(f"pages/{n:04}.png")
        _page(path, n, dpi=300).save(out / book.pages[-1])
    return book


def _outline(path: Path) -> list[tuple[int, str, int]]:
    """(level, title, page index) from `djvused print-outline`."""
    tokens = re.findall(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+', _run("djvused", "-u", "-e", "print-outline", str(path)))
    level = 0
    entry: list[str] = []
    result: list[tuple[int, str, int]] = []
    for tok in tokens:
        if tok == "(":
            level += 1
            entry = []
        elif tok == ")":
            level -= 1
        else:
            entry.append(re.sub(r"\\(.)", r"\1", tok[1:-1]) if tok.startswith('"') else tok)
            if len(entry) == 2 and entry[1].startswith("#") and entry[1][1:].isdigit():
                result.append((level - 1, entry[0], int(entry[1][1:]) - 1))
    return result


def _page(path: Path, n: int, dpi: int) -> pymupdf.Pixmap:
    with tempfile.TemporaryDirectory() as tmp:
        pnm = Path(tmp) / "page.pnm"
        subprocess.run(["ddjvu", "-format=pnm", f"-page={n}", f"-scale={dpi}", str(path), str(pnm)], check=True)
        return pymupdf.Pixmap(str(pnm))


def _run(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
