"""Every call a part makes to its own object passes the arguments that method takes.

The second class of defect that only appears the day a producer starts. On
2026-08-25, the hour `symbol-profile-store` first ran, four parts crashed on the
first profile that arrived:

    BullExitPlanProposer.observe_symbol_profile() missing 2 required positional
    arguments: 'symbol' and 'price_step'
    BearFeatureBuilder has no attribute 'observe_symbol_profile'

Each call site passed the whole payload where the method wanted a venue, a symbol
and a number. Every one of them had been written months earlier, sat in a part
that was running and reporting healthy, and could not fail until something
published the input.

`check_payload_reads.py` proves a read names a field the producer carries. This
proves a call names a method the object has, and passes it the arguments that
method takes -- a bug a type checker would find, in the one shape this codebase
produces it: a part builds its own worker object and drives it from `start_part`.

**What it checks, exactly.** Inside each part module: a name bound by assignment
to `ClassName(...)`, where the class is defined in that same module, is followed
through to every `name.method(...)` call on it. The method must exist on the
class, and the call must be one the signature accepts -- counting positional
parameters, defaults, `*args` and keywords.

**What it does not check.** An object handed in as a parameter, one built by a
factory, or a method inherited from a base class in another module: those are
reported as unfollowed rather than as clean.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
import sys

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parent.parent
PARTS_ROOT = REPOSITORY_ROOT / "parts"

if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


@dataclasses.dataclass(frozen=True)
class BadCall:
    """One call a part makes that its own object cannot answer."""

    source_file: str
    line: int
    class_name: str
    method: str
    problem: str


@dataclasses.dataclass(frozen=True)
class MethodSignature:
    """What one method will accept, counted rather than typed."""

    name: str
    required: int
    accepted: int | None  # None when *args makes it unbounded
    keywords: frozenset[str]

    def refuse(self, positional: int, keywords: frozenset[str]) -> str | None:
        unknown = keywords - self.keywords
        if unknown:
            return f"does not take keyword(s) {', '.join(sorted(unknown))}"
        supplied = positional + len(keywords & self.keywords)
        if supplied < self.required:
            return f"takes {self.required} argument(s) and {supplied} were given"
        if self.accepted is not None and positional > self.accepted:
            return f"takes at most {self.accepted} positional argument(s) and {positional} were given"
        return None


def signatures_of(class_node: ast.ClassDef) -> dict[str, MethodSignature]:
    """Every method this class defines, and what each will accept.

    `self` is dropped: the call site never supplies it. A property is recorded
    with no arguments so that calling one is reported rather than passed over.
    """
    signatures: dict[str, MethodSignature] = {}
    for node in class_node.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        arguments = node.args
        positional = [argument.arg for argument in arguments.posonlyargs + arguments.args][1:]
        defaults = len(arguments.defaults)
        required = max(0, len(positional) - defaults)
        keyword_only = {argument.arg for argument in arguments.kwonlyargs}
        signatures[node.name] = MethodSignature(
            name=node.name,
            required=required,
            accepted=None if arguments.vararg is not None else len(positional),
            keywords=frozenset(positional) | keyword_only,
        )
    return signatures


class PartCallChecker(ast.NodeVisitor):
    """Follows a locally built object to the calls made on it."""

    def __init__(self, source_file: str, classes: dict[str, dict[str, MethodSignature]]) -> None:
        self.source_file = source_file
        self.classes = classes
        self.bad: list[BadCall] = []
        self.checked = 0
        self._class_of: dict[str, str] = {}

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            return
        name = node.targets[0].id
        self._class_of.pop(name, None)
        value = node.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            if value.func.id in self.classes:
                self._class_of[name] = value.func.id

    def visit_Call(self, node: ast.Call) -> None:
        self.generic_visit(node)
        if not isinstance(node.func, ast.Attribute) or not isinstance(node.func.value, ast.Name):
            return
        class_name = self._class_of.get(node.func.value.id)
        if class_name is None:
            return
        methods = self.classes[class_name]
        method = node.func.attr
        if any(isinstance(argument, ast.Starred) for argument in node.args):
            return  # unpacked arguments cannot be counted; not a claim either way
        if method not in methods:
            # Only a class with no base classes elsewhere can be judged on this:
            # an inherited method is not in this module's own body.
            if self.classes[class_name].get("__has_a_base__") is None:
                self.bad.append(BadCall(
                    self.source_file, node.lineno, class_name, method,
                    "is not a method this class defines",
                ))
            return
        self.checked += 1
        keywords = frozenset(
            keyword.arg for keyword in node.keywords if keyword.arg is not None
        )
        if any(keyword.arg is None for keyword in node.keywords):
            return  # **kwargs at the call site; the count is not knowable here
        refusal = methods[method].refuse(len(node.args), keywords)
        if refusal is not None:
            self.bad.append(BadCall(
                self.source_file, node.lineno, class_name, method, refusal,
            ))


def check_one_module(path: pathlib.Path) -> tuple[list[BadCall], int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    classes: dict[str, dict[str, MethodSignature]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and not node.bases:
            classes[node.name] = signatures_of(node)
    checker = PartCallChecker(str(path.relative_to(REPOSITORY_ROOT)), classes)
    checker.visit(tree)
    return checker.bad, checker.checked


def check_every_part(parts_root: pathlib.Path = PARTS_ROOT) -> tuple[list[BadCall], int, int]:
    bad: list[BadCall] = []
    checked = 0
    modules = 0
    for path in sorted(parts_root.rglob("*.py")):
        if path.name.startswith("_"):
            continue
        modules += 1
        found, counted = check_one_module(path)
        bad.extend(found)
        checked += counted
    return bad, checked, modules


def main() -> int:
    bad, checked, modules = check_every_part()
    print("part calls -- every call a part makes to its own object")
    print()
    print(f"  modules parsed  {modules:>6}")
    print(f"  calls followed  {checked:>6}")
    print(f"  calls refused   {len(bad):>6}")
    print()
    for call in sorted(bad, key=lambda c: (c.source_file, c.line)):
        print(f"  {call.source_file}:{call.line}: {call.class_name}.{call.method} {call.problem}")
    if bad:
        print()
        print(f"  {len(bad)} call(s) an object cannot answer.")
        return 1
    print("  every followed call names a method its object has, with arguments it takes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
