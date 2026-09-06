#!/usr/bin/env python3
"""Every input a part declares, and whether the part ever binds a reader for it.

    python3 dashboard/check_declared_inputs.py

The fourth checker, and it exists because the other three cannot see this. Found
2026-09-06 walking `opportunity-scanner`:
`expiry-day-zero-to-hero-detector` declared `broker-price-frame` and the type
appeared exactly once in its whole source file -- in the `PART_DECLARATION`
consumes tuple -- and nowhere else. `broker-price-level-sampler` was publishing
5,868 price frames at the time, so the audit board reported a wire whose producer
was healthy and whose consumer received nothing, which is indistinguishable from
a real delivery fault until somebody opens the file.

    check_contracts.py      the blueprint is coherent with itself (R-01)
    check_payload_reads.py  a field a part reads, some producer of that type carries
    check_part_calls.py     a call a part makes, its own object can answer
    this one               a type a part declares, the part actually binds a reader for

The first checks the declaration against itself and never opens a part's source.
The next two check the code against the declaration for types the code *does*
read. Nothing checked the declaration against the code for a type the code reads
**not at all**, so a declared input could sit unbound for as long as nobody
looked.

That gap costs more than a stray tuple entry, because the declaration is what the
whole wiring is computed from: `docs/features.json` derives every edge from
consumes/produces (R-01), so a declared-and-unread input is a wire on every
diagram, a row in the wiring explorer, and a `NOT CARRYING` line on the audit
board -- a fault reported against a producer doing its job perfectly. RL-067 says
a part's real consumes equal what the blueprint declares; this is that rule in
the direction nothing was enforcing.

**A part that binds a reader dynamically is reported as unverifiable, never as
clean.** Several parts do `context.bus.reader(data_type)` inside a loop over
their own consumed types, which is good code and impossible to check statically.
Counting those as passing would be exactly the kind of green-that-means-nothing
this project keeps finding (Rule 8: absence of evidence renders as its own
state), so they are named and counted separately.

The reverse direction is deliberately not checked here: a reader bound for a type
the part does not declare already fails `check_payload_reads.py`.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys
from dataclasses import dataclass, field

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parent


@dataclass(frozen=True)
class PartInputs:
    """One part's declared inputs beside the readers its source really binds."""

    part_id: str
    source: pathlib.Path
    declared: tuple[str, ...]
    bound_by_name: frozenset[str]
    dynamic_bindings: int

    @property
    def never_bound(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.declared) - self.bound_by_name))

    @property
    def is_statically_checkable(self) -> bool:
        return self.dynamic_bindings == 0


@dataclass
class Findings:
    parts_read: int = 0
    defects: list[PartInputs] = field(default_factory=list)
    unverifiable: list[PartInputs] = field(default_factory=list)

    @property
    def holds(self) -> bool:
        return not self.defects


def reader_bindings(tree: ast.AST) -> tuple[frozenset[str], int]:
    """Every literal handed to `.reader(...)`, and how many calls were not literal.

    Matched on the attribute name rather than on `context.bus`, because a part is
    free to hold the bus in a local and several do; what identifies the call is
    that it asks for a reader.
    """
    literal: set[str] = set()
    dynamic = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (isinstance(function, ast.Attribute) and function.attr == "reader"):
            continue
        if not node.args:
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            literal.add(argument.value)
        else:
            dynamic += 1
    return frozenset(literal), dynamic


def declared_consumes(tree: ast.AST) -> tuple[str, ...] | None:
    """The `consumes` tuple from the module's own PART_DECLARATION literal.

    Read from the source rather than from the blueprint on purpose: the point of
    this check is whether these two agree, so taking both from the same place
    would prove nothing.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "PART_DECLARATION"
            for target in node.targets
        ):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        for keyword in node.value.keywords:
            if keyword.arg != "consumes":
                continue
            try:
                return tuple(ast.literal_eval(keyword.value))
            except (ValueError, SyntaxError):
                return None
    return None


def declared_part_id(tree: ast.AST) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "PART_ID"
            for target in node.targets
        ):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    return None


def check_declared_inputs() -> Findings:
    findings = Findings()
    for source in sorted((PROJECT / "parts").rglob("*.py")):
        if source.name == "__init__.py":
            continue
        text = source.read_text(encoding="utf-8")
        # Only a launchable part has a reader to bind at all; a part with no
        # start_part cannot be switched on and is a different finding, already
        # counted by measure_diagram_coverage.py.
        if "\ndef start_part(" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        declared = declared_consumes(tree)
        if declared is None:
            continue
        findings.parts_read += 1
        literal, dynamic = reader_bindings(tree)
        part = PartInputs(
            part_id=declared_part_id(tree) or source.stem,
            source=source.relative_to(PROJECT),
            declared=declared,
            bound_by_name=literal,
            dynamic_bindings=dynamic,
        )
        if not part.never_bound:
            continue
        if part.is_statically_checkable:
            findings.defects.append(part)
        else:
            findings.unverifiable.append(part)
    return findings


def main() -> int:
    findings = check_declared_inputs()
    print("declared inputs -- every type a part consumes, and whether it binds a reader\n")
    print(f"  parts checked            {findings.parts_read:>5}")
    print(f"  declared but never bound {len(findings.defects):>5}")
    print(f"  bound dynamically        {len(findings.unverifiable):>5}  not statically checkable")

    if findings.unverifiable:
        print(
            "\n  These bind readers in a loop over their own consumed types, which is "
            "\n  good code and impossible to check here. Named rather than passed:"
        )
        for part in findings.unverifiable:
            print(f"    {part.part_id:<38} {len(part.never_bound)} unchecked")

    if findings.defects:
        print(f"\n  {len(findings.defects)} part(s) declare an input nothing ever reads:\n")
        for part in findings.defects:
            print(f"    {part.part_id} ({part.source})")
            for data_type in part.never_bound:
                print(f"        declares {data_type}, binds no reader for it")
        print(
            "\n  A declared input is a wire on every diagram (R-01) and a NOT CARRYING\n"
            "  line on the audit board, reported against a producer that is working.\n"
            "  Either bind it or stop declaring it -- both are blueprint edits first.\n"
            "  Rules: docs/contracts.md, RL-067."
        )
        return 1

    print("\n  every declared input has a reader bound for it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
