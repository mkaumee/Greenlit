#!/usr/bin/env python3
"""Fail the build if anything outside the clock module reads real time.

Ruff's banned-api rules catch the common spellings. This catches the ones that
slip past them — aliased imports, ``from time import time``, and
``import datetime`` followed by ``datetime.datetime.now()``.

Run it as ``uv run python scripts/check_no_wallclock.py`` or via ``make check``.

Why bother with both: Hard Rule 2 is the one whose violation is invisible.
Wall-clock code works perfectly on a laptop in live mode and only misbehaves
when the demo clock is running at 21600x, which is the exact moment a judge is
watching.
"""

import ast
import sys
from pathlib import Path
from typing import override

REPO_ROOT = Path(__file__).resolve().parent.parent

ALLOWED: frozenset[Path] = frozenset(
    {
        REPO_ROOT / "orchestrator" / "src" / "orchestrator" / "clock.py",
        REPO_ROOT / "scripts" / "check_no_wallclock.py",
    }
)
"""The clock implementation is the one sanctioned reader of real time."""

SEARCH_DIRS = ("contracts", "orchestrator", "main-agent", "scripts")

DATETIME_ATTRS = frozenset({"now", "utcnow", "today"})
TIME_ATTRS = frozenset({"time", "monotonic", "perf_counter", "time_ns", "monotonic_ns"})


class WallClockVisitor(ast.NodeVisitor):
    """Tracks how time modules were imported, then flags calls through them."""

    path: Path
    findings: list[tuple[int, str]]
    _datetime_modules: set[str]  # `import datetime`
    _datetime_classes: set[str]  # `from datetime import datetime`
    _time_modules: set[str]  # `import time`
    _bare_time_funcs: set[str]  # `from time import time`

    def __init__(self, path: Path) -> None:
        self.path = path
        self.findings = []
        self._datetime_modules = set()
        self._datetime_classes = set()
        self._time_modules = set()
        self._bare_time_funcs = set()

    @override
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            if alias.name == "datetime":
                self._datetime_modules.add(bound)
            elif alias.name == "time":
                self._time_modules.add(bound)
        self.generic_visit(node)

    @override
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name
            if node.module == "datetime" and alias.name == "datetime":
                self._datetime_classes.add(bound)
            elif node.module == "time" and alias.name in TIME_ATTRS:
                self._bare_time_funcs.add(bound)
        self.generic_visit(node)

    @override
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func

        # `time()` imported directly from the time module.
        if isinstance(func, ast.Name) and func.id in self._bare_time_funcs:
            self._flag(node.lineno, f"{func.id}()")

        elif isinstance(func, ast.Attribute):
            root = _root_name(func)

            # `datetime.now()` where datetime is the class.
            if (
                isinstance(func.value, ast.Name)
                and func.value.id in self._datetime_classes
                and func.attr in DATETIME_ATTRS
            ):
                self._flag(node.lineno, f"{func.value.id}.{func.attr}()")

            # `datetime.datetime.now()` where datetime is the module.
            elif (
                root in self._datetime_modules
                and func.attr in DATETIME_ATTRS
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "datetime"
            ):
                self._flag(node.lineno, f"{root}.datetime.{func.attr}()")

            # `time.time()` and friends.
            elif (
                isinstance(func.value, ast.Name)
                and func.value.id in self._time_modules
                and func.attr in TIME_ATTRS
            ):
                self._flag(node.lineno, f"{func.value.id}.{func.attr}()")

        self.generic_visit(node)

    def _flag(self, lineno: int, expr: str) -> None:
        self.findings.append((lineno, expr))


def _root_name(node: ast.Attribute) -> str | None:
    """Leftmost identifier of a dotted expression, if it is a plain name chain."""
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        current = current.value
    return current.id if isinstance(current, ast.Name) else None


def scan(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        print(f"{path}: could not parse ({exc})", file=sys.stderr)
        return []
    visitor = WallClockVisitor(path)
    visitor.visit(tree)
    return visitor.findings


def main() -> int:
    violations: list[str] = []

    for directory in SEARCH_DIRS:
        base = REPO_ROOT / directory
        if not base.is_dir():
            continue  # main-agent lives on the other branch until we merge
        for path in sorted(base.rglob("*.py")):
            if path.resolve() in ALLOWED or ".venv" in path.parts:
                continue
            for lineno, expr in scan(path):
                rel = path.relative_to(REPO_ROOT)
                violations.append(f"  {rel}:{lineno}  {expr}")

    if violations:
        print("Wall-clock time is banned outside orchestrator/clock.py.\n")
        print("\n".join(violations))
        print(
            "\nUse clock.now() instead. Simulated time is what every stored "
            "timestamp is measured in, and a wall-clock read desynchronises "
            "from it the moment demo mode runs. See CLAUDE.md."
        )
        return 1

    print("No wall-clock reads outside the clock module.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
