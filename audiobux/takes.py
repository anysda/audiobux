"""Pauses a reader gives the boundaries of the text, and the words every check counts."""

import re

# pause after a boundary, s: syntagm, comma, sentence, paragraph, heading, chapter
PAUSE = {"syn": 0.22, "comma": 0.40, "stop": 0.80, "para": 1.10, "head": 1.40, "chapter": 2.0}


def words(text: str) -> list[str]:
    """Bare lowercase words, `ё` read as `е`, a hyphen splits: the units every check counts."""
    return re.findall(r"[а-яa-z0-9]+", text.lower().replace("ё", "е").replace("+", ""))
