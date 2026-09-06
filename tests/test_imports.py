"""Import boundaries of the package: no cycles, the model slots know nothing of book
formats or the worker around them, the formats know nothing of models and the steps
after them, the list of formats and the socket contract are light enough for any
process, and the web process reaches nothing of the worker but the contract."""

import ast
from pathlib import Path

PACKAGE = Path(__file__).parent.parent / "audiobux"
# what the formats may not reach
OUTSIDE_FORMATS = {"models", "llm", "f5", "takes", "speech", "chunk", "cleanup", "figures", "pipeline", "worker"}


def modules() -> dict[str, Path]:
    """Dotted name inside the package -> file; a subpackage is named by its `__init__`."""
    found = {}
    for path in PACKAGE.rglob("*.py"):
        parts = path.relative_to(PACKAGE).with_suffix("").parts
        found[".".join(parts[:-1] if parts[-1] == "__init__" else parts)] = path
    return found


def imports(name: str, path: Path) -> tuple[set[str], set[str]]:
    """(package modules, outside top-level modules) that `name` imports, at the top or
    inside a function."""
    inside, outside = set(), set()
    package = name.split(".") if path.name == "__init__.py" else name.split(".")[:-1]
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.level:
            base = package[: len(package) - node.level + 1]
            prefix = [*base, *(node.module.split(".") if node.module else [])]
            inside |= {".".join(prefix)} if node.module else set()
            inside |= {".".join([*prefix, a.name]) for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("audiobux."):
                inside.add(node.module.removeprefix("audiobux."))
            else:
                outside.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("audiobux."):
                    inside.add(a.name.removeprefix("audiobux."))
                else:
                    outside.add(a.name.split(".")[0])
    return inside, outside


def graph() -> dict[str, set[str]]:
    """Module -> the package modules it imports."""
    known = modules()
    return {name: imports(name, path)[0] & set(known) for name, path in known.items()}


def reach(edges: dict[str, set[str]], start: str) -> set[str]:
    seen: set[str] = set()
    todo = [start]
    while todo:
        for nxt in edges[todo.pop()] - seen:
            seen.add(nxt)
            todo.append(nxt)
    return seen


def is_format(name: str) -> bool:
    return name.split(".")[0] == "formats"


def test_no_cycles() -> None:
    edges = graph()
    looped = sorted(m for m in edges if m in reach(edges, m))
    assert not looped, f"modules in an import cycle: {looped}"


def test_formats_know_nothing_of_models_and_steps() -> None:
    edges = graph()
    readers = [m for m in edges if is_format(m)]
    assert len(readers) > 3
    for name in readers:
        assert not reach(edges, name) & OUTSIDE_FORMATS, name


def test_the_check_sees_heavy_imports_and_package_paths(tmp_path) -> None:
    """The checks above find what they look for: a parent import and a heavy library."""
    probe = tmp_path / "probe.py"
    probe.write_text("import pymupdf\nfrom ..text import mark\nfrom . import pages\n")
    inside, outside = imports("formats.probe", probe)
    assert inside == {"text", "text.mark", "formats.pages"} and outside == {"pymupdf"}
