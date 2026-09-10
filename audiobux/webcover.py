"""Cover from the web, through the isolated cover finder (coverfinder/).

The finder gets only the author and title and answers with image bytes or
nothing. Those bytes are untrusted: they are used only if they decode as a
JPEG, PNG or WebP of sane size and shape, and only re-encoded as a plain JPEG
with no metadata. Nothing else from the finder is read, logged or kept.
"""

import io
import os
import urllib.parse
import urllib.request
import warnings

from PIL import Image

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 40_000_000
MIN_SIDE = 200
ASPECT = (0.9, 2.0)  # height / width
TIMEOUT = 120


def fetch(author: str, title: str, url: str | None = None) -> bytes | None:
    url = os.environ.get("AUDIOBUX_COVER_URL", "") if url is None else url
    if not url or not title.strip():
        return None
    query = urllib.parse.urlencode({"author": author, "title": title})
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/cover?{query}", timeout=TIMEOUT) as r:
            if r.status != 200:
                return None
            data = r.read(MAX_BYTES + 1)
    except Exception:
        return None
    return clean(data)


def clean(data: bytes) -> bytes | None:
    """Untrusted image bytes to a plain JPEG, or None."""
    if not data or len(data) > MAX_BYTES:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=("JPEG", "PNG", "WEBP")) as im:
                w, h = im.size
                if w * h > MAX_PIXELS or min(w, h) < MIN_SIDE or not ASPECT[0] <= h / w <= ASPECT[1]:
                    return None
                im.load()
                if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
                    rgba = im.convert("RGBA")
                    pixels = Image.new("RGB", im.size, "white")
                    pixels.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    pixels = im.convert("RGB")
    except Exception:
        return None
    plain = Image.new("RGB", pixels.size)
    plain.paste(pixels)
    out = io.BytesIO()
    plain.save(out, "JPEG", quality=90)
    return out.getvalue()
