"""Pieces of speech into one m4b: chapters, cover, even loudness."""

import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

LOUDNESS = "I=-18:TP=-3:LRA=11"


def assemble(
    wavs: list[Path],
    pieces: list[dict[str, Any]],
    titles: list[str],
    out: Path,
    meta: dict[str, str],
    cover: Path | None = None,
    workdir: Path | None = None,
) -> dict[str, Any]:
    """`pieces[i]` gives the chapter and the pause after `wavs[i]`. Returns the length
    in seconds, the chapters with their start and the start of every piece, as they
    are in the m4b."""
    workdir = workdir or out.parent
    info = sf.info(wavs[0])
    rate, channels = info.samplerate, info.channels

    silence: dict[float, Path] = {}
    listing: list[Path] = []
    starts: dict[int, float] = {}
    begins: list[float] = []
    t = 0.0
    for wav, piece in zip(wavs, pieces, strict=True):
        starts.setdefault(piece["chapter"], t)
        begins.append(round(t, 3))
        t += sf.info(wav).duration
        listing.append(wav)
        pause = round(piece["pause"], 2)
        if pause not in silence:
            silence[pause] = workdir / f"silence_{pause}.wav"
            sf.write(silence[pause], np.zeros((int(rate * pause), channels), np.float32), rate, subtype="PCM_16")
        listing.append(silence[pause])
        t += int(rate * pause) / rate

    concat = workdir / "concat.txt"
    concat.write_text("".join(f"file '{p.resolve()}'\n" for p in listing))
    metafile = workdir / "chapters.txt"
    metafile.write_text(_ffmetadata(meta, titles, starts, t))

    source = ["-f", "concat", "-safe", "0", "-i", str(concat)]
    measured = _measure(source)
    loudnorm = f"loudnorm={LOUDNESS}"
    if measured:
        loudnorm += (
            f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}:linear=true"
        )

    cmd = ["ffmpeg", "-y", "-v", "error", *source, "-i", str(metafile)]
    maps = ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
    if cover and cover.exists():
        cmd += ["-i", str(cover)]
        maps += ["-map", "2:v", "-c:v", "copy", "-disposition:v", "attached_pic"]
    tmp = out.with_suffix(".part.m4b")
    cmd += [*maps, "-af", loudnorm, "-ar", str(rate), "-c:a", "aac", "-b:a", "64k"]
    cmd += ["-movflags", "+faststart", "-f", "ipod", str(tmp)]
    subprocess.run(cmd, check=True)
    tmp.rename(out)
    return {
        "duration": round(t, 3),
        "chapters": [{"title": titles[c], "start": round(starts[c], 3)} for c in sorted(starts)],
        "pieces": begins,
    }


def _measure(source: list[str]) -> dict[str, str] | None:
    """First loudnorm pass; None when there is nothing to measure (pure silence)."""
    measure = [*source, "-af", f"loudnorm={LOUDNESS}:print_format=json", "-f", "null", "-"]
    run = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *measure], check=True, capture_output=True, text=True)
    found = re.findall(r"\{[^{}]*\}", run.stderr)
    if not found:
        return None
    data: dict[str, str] = json.loads(found[-1])
    if not all(math.isfinite(float(data[k])) for k in ("input_i", "input_tp", "input_lra", "input_thresh")):
        return None
    return data


def _ffmetadata(meta: dict[str, str], titles: list[str], starts: dict[int, float], total: float) -> str:
    lines = [";FFMETADATA1"]
    for key in ("title", "artist", "album", "genre", "date"):
        if meta.get(key):
            lines.append(f"{key}={_escape(meta[key])}")
    order = sorted(starts)
    for ch, nxt in zip(order, [*order[1:], None], strict=True):
        end = starts[nxt] if nxt is not None else total
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={int(starts[ch] * 1000)}",
            f"END={int(end * 1000)}",
            f"title={_escape(titles[ch])}",
        ]
    return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return re.sub(r"([=;#\\\n])", r"\\\1", str(value))
