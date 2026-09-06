"""No part declares an input it never binds a reader for.

The fourth class of defect, and the first one the other three checkers were
structurally unable to see. Found 2026-09-06 walking `opportunity-scanner`:
`expiry-day-zero-to-hero-detector` declared `broker-price-frame` and the type
appeared exactly once in its whole source -- in the PART_DECLARATION consumes
tuple -- and nowhere else. `broker-price-level-sampler` was publishing 5,868 of
them at the time, so the audit board reported a wire whose producer was healthy
and whose consumer received nothing, which is what a real delivery fault looks
like.

Five more parts were in the same state. R-01 computes every edge from
consumes/produces, so each was a wire on every diagram that no message could
ever cross.
"""

from __future__ import annotations

import ast

from dashboard.check_declared_inputs import (
    check_declared_inputs, declared_consumes, reader_bindings,
)

# opinion-arbiter declares `bot-maturity` and binds no reader for it. That is
# not an oversight and is deliberately not fixed: how proven a bot is, is
# exactly what an arbiter should weigh, the only producer
# (`edge-graduation-gate`) has published nothing, and four other parts consume
# it -- so deleting the declaration erases a design decision and binding it
# changes how trades are arbitrated while still receiving nothing. It is the
# operator's call (docs/proposals/a-declared-input-must-actually-be-read.md),
# and this test states the one open case by name rather than asserting a bare
# count that would go quietly green on the wrong day.
KNOWN_OPEN = {"opinion-arbiter": ("bot-maturity",)}


def test_no_part_declares_an_input_it_never_reads():
    findings = check_declared_inputs()
    unexpected = [
        part for part in findings.defects
        if KNOWN_OPEN.get(part.part_id) != part.never_bound
    ]
    assert not unexpected, "\n".join(
        f"{part.part_id} ({part.source}) declares {', '.join(part.never_bound)} "
        f"and binds no reader for it"
        for part in unexpected
    )


def test_the_one_open_case_is_still_open_and_still_only_one():
    """If the arbiter is resolved, this test is what says so -- and the entry
    above should be deleted rather than the assertion loosened."""
    findings = check_declared_inputs()
    assert {part.part_id: part.never_bound for part in findings.defects} == KNOWN_OPEN


def test_the_checker_actually_reads_the_parts():
    """A parser that stopped parsing would report zero defects and look clean."""
    findings = check_declared_inputs()
    assert findings.parts_read > 300


def test_a_dynamically_bound_part_is_reported_rather_than_passed():
    """Several parts bind readers in a loop over their own consumed types, which
    cannot be checked statically. Counting them as clean would be exactly the
    green-that-means-nothing this checker exists to stop (Rule 8)."""
    findings = check_declared_inputs()
    assert findings.unverifiable, "no part binds dynamically any more -- has the shape changed?"
    assert all(not part.is_statically_checkable for part in findings.unverifiable)
    # And none of them is silently in the defect list.
    assert not (
        {part.part_id for part in findings.unverifiable}
        & {part.part_id for part in findings.defects}
    )


# ---- the two readers this is built on ----------------------------------------

def test_a_literal_reader_is_seen_and_a_computed_one_is_counted_as_dynamic():
    tree = ast.parse(
        "def start_part(context):\n"
        "    a = context.bus.reader('market-data')\n"
        "    b = context.bus.reader(wanted)\n"
    )
    literal, dynamic = reader_bindings(tree)
    assert literal == {"market-data"}
    assert dynamic == 1


def test_a_reader_bound_off_a_local_bus_is_still_seen():
    """A part is free to hold the bus in a local, and several do -- what
    identifies the call is that it asks for a reader."""
    tree = ast.parse("def start_part(context):\n    bus = context.bus\n    r = bus.reader('fill')\n")
    literal, _ = reader_bindings(tree)
    assert literal == {"fill"}


def test_the_consumes_tuple_is_read_from_the_source_not_the_blueprint():
    """Taking both sides from the blueprint would prove nothing: the whole point
    is whether the source and the blueprint agree."""
    tree = ast.parse(
        "PART_DECLARATION = PartDeclaration(\n"
        "    part_id='x', consumes=('a', 'b'), produces=('c',),\n"
        ")\n"
    )
    assert declared_consumes(tree) == ("a", "b")


def test_a_module_with_no_declaration_is_skipped_not_guessed_at():
    assert declared_consumes(ast.parse("x = 1\n")) is None
