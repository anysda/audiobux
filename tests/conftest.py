import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pymupdf
import pytest
from ebooklib import epub

DATA = Path(__file__).parent / "data"


def stories() -> list[tuple[str, list[str]]]:
    """Three Chekhov stories: [(title, paragraphs)]."""
    result = []
    for part in (DATA / "chekhov.txt").read_text(encoding="utf-8").split("# ")[1:]:
        title, _, body = part.partition("\n")
        result.append((title.strip(), [p.strip() for p in body.split("\n\n") if p.strip()]))
    return result


def cover_jpeg() -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 450), 0)
    pix.clear_with(180)
    return pix.tobytes("jpeg")


@pytest.fixture(scope="session")
def chekhov_epub(tmp_path_factory) -> Path:
    """Front page outside the TOC, one story in its own file, two sharing a file behind anchors."""
    book = epub.EpubBook()
    book.set_identifier("chekhov-stories")
    book.set_title("Рассказы")
    book.set_language("ru")
    book.add_author("Антон Чехов")
    book.set_cover("cover.jpg", cover_jpeg())

    def html(title, paragraphs, anchor=None):
        attr = f' id="{anchor}"' if anchor else ""
        return f"<h2{attr}>{title}</h2>" + "".join(f"<p>{p}</p>" for p in paragraphs)

    (t1, p1), (t2, p2), (t3, p3) = stories()
    front = epub.EpubHtml(
        title="Титул", file_name="text/front.xhtml", lang="ru", content="<h1>Рассказы</h1><p>Издание для проверки.</p>"
    )
    one = epub.EpubHtml(title=t1, file_name="text/one.xhtml", lang="ru", content=html(t1, p1))
    two = epub.EpubHtml(
        title=t2, file_name="text/two.xhtml", lang="ru", content=html(t2, p2, "s2") + html(t3, p3, "s3")
    )
    for item in (front, one, two):
        book.add_item(item)
    book.toc = [
        epub.Link("text/one.xhtml", t1, "one"),
        epub.Link("text/two.xhtml#s2", t2, "s2"),
        epub.Link("text/two.xhtml#s3", t3, "s3"),
    ]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = [front, one, two]
    path = tmp_path_factory.mktemp("epub") / "Чехов - Рассказы.epub"
    epub.write_epub(str(path), book)
    return path


def _pdf_pages(writer_path: Path) -> list[int]:
    """Writes the stories to a PDF, each from a new page; returns the first page of each."""
    starts = []
    writer = pymupdf.DocumentWriter(str(writer_path))
    page_no = 0
    rect = pymupdf.paper_rect("a5")
    where = rect + (40, 60, -40, -50)
    for title, paragraphs in stories():
        starts.append(page_no)
        story = pymupdf.Story(f"<h2>{title}</h2>" + "".join(f"<p>{p}</p>" for p in paragraphs))
        more = True
        while more:
            device = writer.begin_page(rect)
            more, _ = story.place(where)
            story.draw(device)
            writer.end_page()
            page_no += 1
    writer.close()
    return starts


@pytest.fixture(scope="session")
def chekhov_pdf(tmp_path_factory) -> Path:
    """Text PDF with bookmarks and running headers like «20 | Title»."""
    folder = tmp_path_factory.mktemp("pdf")
    raw = folder / "raw.pdf"
    starts = _pdf_pages(raw)
    doc = pymupdf.open(raw)
    titles = [t for t, _ in stories()]
    for n, page in enumerate(doc):
        if n in starts:
            continue
        current = titles[max(i for i, s in enumerate(starts) if s <= n)]
        text = f"{n + 1} | {current}" if n % 2 else f"{current} | {n + 1}"
        page.insert_htmlbox(pymupdf.Rect(40, 20, page.rect.width - 40, 45), f"<p>{text}</p>")
    doc.set_toc([[1, t, s + 1] for t, s in zip(titles, starts, strict=True)])
    path = folder / "Чехов - Рассказы.pdf"
    doc.save(path)
    return path


@pytest.fixture(scope="session")
def chekhov_scan(tmp_path_factory, chekhov_pdf) -> Path:
    """The same book as page pictures only, bookmarks kept."""
    src = pymupdf.open(chekhov_pdf)
    doc = pymupdf.open()
    for page in src:
        out = doc.new_page(width=page.rect.width, height=page.rect.height)
        out.insert_image(out.rect, pixmap=page.get_pixmap(dpi=100))
    doc.set_toc(src.get_toc())
    path = tmp_path_factory.mktemp("scan") / "Чехов - скан.pdf"
    doc.save(path)
    return path


def _djvu(folder: Path, pdf: Path, text: bool) -> Path:
    src = pymupdf.open(pdf)
    pages = []
    for n, page in enumerate(src):
        pix = page.get_pixmap(dpi=100)
        pnm, djvu = folder / f"{n}.pnm", folder / f"{n}.djvu"
        pix.save(pnm)
        subprocess.run(["c44", str(pnm), str(djvu)], check=True, capture_output=True)
        if text:
            w, h = pix.width, pix.height
            paras = " ".join(f'(para 0 0 {w} {h} "{_sexp(b[4])}")' for b in page.get_text("blocks"))
            txt = folder / f"{n}.txt"
            txt.write_text(f"(page 0 0 {w} {h} {paras})\n")
            subprocess.run(["djvused", str(djvu), "-e", f"select 1; set-txt {txt}", "-s"], check=True)
        pages.append(str(djvu))
    path = folder / "Чехов - Рассказы.djvu"
    subprocess.run(["djvm", "-c", str(path), *pages], check=True)
    outline = folder / "outline.txt"
    entries = " ".join(f'("{_sexp(t)}" "#{p}")' for _, t, p in src.get_toc())
    outline.write_text(f"(bookmarks {entries})\n")
    subprocess.run(["djvused", str(path), "-e", f"set-outline {outline}", "-s"], check=True)
    return path


def _sexp(s: str) -> str:
    return re.sub(r'(["\\])', r"\\\1", s)


@pytest.fixture(scope="session")
def chekhov_djvu(tmp_path_factory, chekhov_pdf) -> Path:
    if not shutil.which("djvused"):
        pytest.skip("djvulibre is not installed")
    return _djvu(tmp_path_factory.mktemp("djvu"), chekhov_pdf, text=True)


@pytest.fixture(scope="session")
def chekhov_djvu_scan(tmp_path_factory, chekhov_pdf) -> Path:
    if not shutil.which("djvused"):
        pytest.skip("djvulibre is not installed")
    return _djvu(tmp_path_factory.mktemp("djvu_scan"), chekhov_pdf, text=False)


@pytest.fixture
def sock():
    """A Unix socket path is at most 108 bytes: pytest's own directories are too deep."""
    folder = Path(tempfile.mkdtemp(prefix="ab-"))
    yield folder / "w.sock"
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture(autouse=True)
def online(monkeypatch):
    """`fetch.offline` turns the hub off for the whole process: each test starts with it on."""
    import huggingface_hub.constants

    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("DISABLE_SAFETENSORS_CONVERSION", "0")
    monkeypatch.setattr(huggingface_hub.constants, "HF_HUB_OFFLINE", False)
