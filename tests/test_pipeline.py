import json
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
from conftest import cover_jpeg

from audiobux import m4b


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_chapters", "-show_format", "-show_streams", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)


def test_m4b_has_chapters_cover_and_metadata(tmp_path):
    wavs, pieces = [], []
    for ch in range(3):
        for k in range(2):
            wav = tmp_path / f"{ch}{k}.wav"
            t = np.arange(24000) / 24000
            sf.write(wav, 0.2 * np.sin(2 * np.pi * (200 + 100 * ch) * t), 24000, subtype="PCM_16")
            wavs.append(wav)
            pieces.append({"chapter": ch, "pause": 0.5 if k == 0 else 2.0})
    (tmp_path / "cover.jpg").write_bytes(cover_jpeg())
    out = tmp_path / "book.m4b"
    m4b.assemble(
        wavs,
        pieces,
        ["Один", "Два", "Три = 3"],
        out,
        {"title": "Книга", "artist": "Автор", "genre": "Audiobook"},
        tmp_path / "cover.jpg",
    )

    info = probe(out)
    assert [c["tags"]["title"] for c in info["chapters"]] == ["Один", "Два", "Три = 3"]
    assert [round(float(c["start_time"]), 1) for c in info["chapters"]] == [0.0, 4.5, 9.0]
    assert info["format"]["tags"]["title"] == "Книга" and info["format"]["tags"]["artist"] == "Автор"
    assert any(s.get("disposition", {}).get("attached_pic") for s in info["streams"])
    assert abs(float(info["format"]["duration"]) - 13.5) < 0.2
