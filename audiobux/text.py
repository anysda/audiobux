"""Patterns and sizes that extraction and the text steps after it share.

Kept apart from both: the format readers may not reach the language model that
`cleanup` and `figures` work with.
"""

import re
import textwrap

ENDINGS = tuple('.!?…:;»"”)')
CODE = re.compile(r"[{};=<>]|\(\s*\)|\b(public|private|class|void|var|return|new|null|string|int)\b")
CAPTION = re.compile(r"^(Рис\.|Рисунок|Таблица|Табл\.|Листинг|Figure|Table)\s*\d+([.\-]\d+)*\.?(\s|$)")
# the place of figure N of the book in its text, a paragraph of its own
MARK = re.compile(r"^\[\[figure (\d+)\]\]$")
# the long side of a figure picture the vision model sees, as for OCR
FIGURE_SIDE = 1800


def mark(n: int) -> str:
    return f"[[figure {n}]]"


def looks_like_code(text: str) -> bool:
    """Three marks of code or more, and few Russian letters."""
    letters = re.findall(r"[^\W\d_]", text)
    cyrillic = sum(1 for c in letters if "а" <= c.lower() <= "я" or c in "ёЁ")
    return len(CODE.findall(text)) >= 3 and cyrillic < 0.3 * max(len(letters), 1)


def code_lines(text: str) -> str:
    """Code with its lines and their indent, without blank lines and trailing spaces:
    a listing is shown as the book prints it."""
    lines = textwrap.dedent(text.expandtabs(4)).splitlines()
    return "\n".join(line.rstrip() for line in lines if line.strip()).strip()
