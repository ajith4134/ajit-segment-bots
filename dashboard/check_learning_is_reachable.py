"""check-learning-is-reachable: every way a part takes in evidence has a caller.

The third checker, beside `check_payload_reads.py` and `check_part_calls.py`, and
it exists for the same reason both of those do: a defect that no test can see,
because the test calls the method itself.

`check_part_calls.py` asks whether every call names a method that exists. This
asks the other direction -- whether every method that exists to be called is
called. A part's `observe_*` and `record_*` methods are how evidence reaches it:
a price, a fill, an outcome. One with no caller anywhere in production is a part
that cannot learn the thing it was written to learn, and it reports no fault at
all, because nothing failed. It simply never happened.

Found on 2026-08-28 by tracing why `bull-feature-builder` had produced 0 complete
feature vectors in its entire life. Five features were missing on every vector,
and one of them -- `detector_hit_rate` -- was missing because
`SignalCalibrator.observe_outcome` had never been given a single outcome. Nine
scanner detectors define `observe_outcome`, the tests for all nine call it, and
**nothing in the running system does**: none of the nine declares an input that
carries an outcome, so no `start_part` could call it even if one wanted to. Every
detector reported `outcomes_learned: 0` and no probe anywhere called that a
fault.

The check is deliberately narrow. It looks at two verbs, because those are the
two this codebase uses for taking evidence in (510 `observe_*` methods and 33
`record_*` ones), and a checker that flagged every uncalled public method would
be a lint nobody reads. A method whose only callers are tests is reported as
uncalled, which is the entire point: that is precisely the state the nine
detectors were in.
"""

from __future__ import annotations

import ast
import pathlib
import sys

REPOSITORY = pathlib.Path(__file__).resolve().parent.parent

# Where production code lives. `tests/` is deliberately absent: a method the
# tests call and nothing else is exactly the defect being looked for.
PRODUCTION_ROOTS = ("parts", "runtime", "operate", "dashboard")

# The verbs that mean "take this in". Evidence arriving is what a part cannot
# report the absence of -- an input it never receives looks identical to an input
# that carried nothing.
EVIDENCE_VERBS = ("observe_", "record_")


def is_an_evidence_method(name: str) -> bool:
    return not name.startswith("_") and name.startswith(EVIDENCE_VERBS)


def python_files_under(root: str):
    directory = REPOSITORY / root
    if not directory.exists():
        return
    for path in sorted(directory.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def read_tree(path: pathlib.Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None


def evidence_methods_defined_in(tree: ast.Module, path: pathlib.Path) -> list[dict]:
    """Every `observe_*`/`record_*` method, with the class and line that defines it."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not is_an_evidence_method(item.name):
                continue
            found.append(
                {
                    "method": item.name,
                    "owner": node.name,
                    "file": str(path.relative_to(REPOSITORY)),
                    "line": item.lineno,
                }
            )
    return found


def calls_that_bring_evidence_in(tree: ast.Module) -> set[str]:
    """Method names called somewhere in this module that mean evidence arriving.

    Scoped to the module on purpose. A part imports no other part (T-4), so the
    only place that can hand a part's own object some evidence is that part's own
    `start_part`. Searching the whole repository by method name instead made the
    check useless in exactly the case that motivated it: nine scanner detectors
    define `observe_outcome` and none of them calls it, but `llm-model-picker`
    and `hypothesis-regime-tagger` define and call methods of the same name, so a
    repository-wide name match reported all nine as wired.

    One call shape is excluded: a method calling **its own name** on a
    collaborator. `SpreadReversionDetector.observe_outcome` calls
    `self._calibrator.observe_outcome(...)`, which is the evidence it was given
    leaving, not evidence arriving -- and counting it would report all nine dark
    detectors as wired for a second time.

    Only that shape. One evidence method calling a *different* one is a real
    chain: `BotWeightSampler.observe_scorecard` calls `self.observe_closed_trade`,
    and excluding every call written inside an evidence method flagged it as
    unreachable when it is reached on every scorecard.
    """
    delegating: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not is_an_evidence_method(node.name):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == node.name
            ):
                delegating.add(id(inner))
    arriving = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if id(node) in delegating:
            continue
        arriving.add(node.func.attr)
    return arriving


def check() -> int:
    defined: list[dict] = []
    unreachable = []

    for path in python_files_under("parts"):
        tree = read_tree(path)
        if tree is None:
            continue
        here = evidence_methods_defined_in(tree, path)
        defined.extend(here)
        reachable = calls_that_bring_evidence_in(tree)
        for entry in here:
            if entry["method"] not in reachable:
                unreachable.append(entry)

    print("learning is reachable -- every way a part takes in evidence has a caller\n")
    print(f"  evidence methods defined  {len(defined):6}")
    print(f"  with a caller             {len(defined) - len(unreachable):6}")
    print(f"  with none                 {len(unreachable):6}")

    if not unreachable:
        print("\n  every observe_/record_ method a part defines is called by something "
              "that runs")
        return 0

    print("\n  These are called by their tests and by nothing that runs. A part that")
    print("  cannot be given the evidence reports no fault: it simply never learns.\n")
    by_file: dict[str, list[dict]] = {}
    for entry in unreachable:
        by_file.setdefault(entry["file"], []).append(entry)
    for file_name in sorted(by_file):
        print(f"  {file_name}")
        for entry in sorted(by_file[file_name], key=lambda e: e["line"]):
            print(f"      line {entry['line']:>5}  {entry['owner']}.{entry['method']}")
    return 1


if __name__ == "__main__":
    sys.exit(check())
