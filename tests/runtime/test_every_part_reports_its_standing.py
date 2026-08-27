"""A part that has numbers about itself must put them on the board.

Measured 2026-08-27 on the live spine: `order-book-reader` had published
3,248,775 order-book snapshots and its standing on the board was `{}`. Six
parts read that way, all in market-data-feed, and every one of them defines a
`describe_*` that says exactly what it has been doing -- built, tested, and
passed to `run_part` by nothing.

That is Rule 8's failure in its quietest form. A part with no standing cannot be
told from a part doing nothing: the board shows the same blank for a reader
carrying three million messages and for one whose socket died an hour ago, and
`judge_is_working` calls both NOT MEASURED. The counters existed the whole time.

Two of the six -- `feed-gap-detector` and `feed-coverage-auditor` -- have a
`run_<part>` wrapper that wires the standing correctly, and a `start_part` that
calls `run_part` itself and omits it. So the rule is over every `run_part` call
in the module, not over the wrapper alone: a second call path is exactly how the
standing went missing.
"""

from __future__ import annotations

import ast
import pathlib

PARTS_ROOT = pathlib.Path(__file__).resolve().parents[2] / "parts"


def part_modules() -> list[pathlib.Path]:
    return sorted(
        path
        for path in PARTS_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts and path.name != "__init__.py"
    )


def describes_itself(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.FunctionDef) and node.name.startswith("describe_")
        for node in tree.body
    )


def run_part_calls_without_a_standing(tree: ast.Module) -> int:
    unwired = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = node.func
        if not (isinstance(called, ast.Name) and called.id == "run_part"):
            continue
        if not any(keyword.arg == "read_standing" for keyword in node.keywords):
            unwired += 1
    return unwired


def test_a_part_that_describes_itself_puts_that_on_every_run_path():
    missing = []
    for path in part_modules():
        tree = ast.parse(path.read_text())
        if not describes_itself(tree):
            continue
        unwired = run_part_calls_without_a_standing(tree)
        if unwired:
            missing.append(f"{path.relative_to(PARTS_ROOT.parent)}: {unwired} run_part call(s)")

    assert not missing, (
        "these parts count what they do and report none of it, so the board cannot "
        "tell them from a part doing nothing:\n  " + "\n  ".join(missing)
    )
