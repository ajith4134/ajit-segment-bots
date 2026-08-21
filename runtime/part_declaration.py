"""What every part declares about itself. One shape, no privileged parts (T-1).

Three of these fields are new in phase 0 and are added to all 321 blueprint entries
by the edit in Task 13: resource_class (section 7), and the pair rate_risk and
skipped_tick_effect that section 6 requires before a part may be throttled at all.
"""

from __future__ import annotations

import ast
import enum
import json
import pathlib
from dataclasses import dataclass

BLUEPRINT_PATH = pathlib.Path(__file__).resolve().parent.parent / "docs" / "features.json"


class ResourceClass(enum.StrEnum):
    """How the governor should allocate to this part (section 7)."""

    # Blocked in epoll_wait, costs nothing while idle: shared pool, generous concurrency.
    IO_BOUND = "io-bound"
    # Pinned cores, BLAS threads = 1, because the governor owns parallelism.
    COMPUTE_BOUND = "compute-bound"
    # One memory controller, one NUMA node. Two are never co-scheduled: they divide
    # a fixed pipe rather than adding throughput. Rolling-window statistics live here,
    # which is the workload this project runs most.
    BANDWIDTH_BOUND = "bandwidth-bound"


class RateRisk(enum.StrEnum):
    """Section 6 (a): does a lower rate change the number, or only when it arrives?"""

    LATENCY_ONLY = "latency-only"
    CHANGES_THE_ANSWER = "changes-the-answer"


class SkippedTickEffect(enum.StrEnum):
    """Section 6 (b): does a skipped tick corrupt an invariant, or merely delay it?"""

    DELAYS = "delays"
    CORRUPTS = "corrupts"


@dataclass(frozen=True)
class PartDeclaration:
    """One part's contract, as the blueprint declares it."""

    part_id: str
    consumes: tuple[str, ...]
    produces: tuple[str, ...]
    resource_class: ResourceClass
    rate_risk: RateRisk
    skipped_tick_effect: SkippedTickEffect


def may_enter_rate_ladder(declaration: PartDeclaration) -> bool:
    """Section 6: only latency-risk-only on both counts may be throttled.

    Everything else gets a reserved floor instead. The two errors are not symmetric
    -- refusing to throttle something throttleable costs an eviction, which is
    visible and recoverable; throttling something unthrottleable costs a number that
    is wrong while still looking healthy.
    """
    return (
        declaration.rate_risk is RateRisk.LATENCY_ONLY
        and declaration.skipped_tick_effect is SkippedTickEffect.DELAYS
    )


# The name every built part's own module exposes its real wiring under (RL-070).
# T-1 says every feature is the same shape, so there is one place a built part
# states its wiring, not one convention per author -- this constant is that place.
PART_DECLARATION_ATTRIBUTE = "PART_DECLARATION"


class ModuleDeclaresNoWiring(Exception):
    """A part's own source carries no usable PART_DECLARATION.

    Raised for every way that can be true: the assignment is absent, it is not
    a PartDeclaration(...) call, one of its arguments is not a literal, or the
    file does not parse at all. All of these are the same fact from the
    checker's point of view: this part's wiring cannot be read, so it must not
    be read as agreeing with the blueprint.
    """


# The bare name of the constructor call a PART_DECLARATION assignment must use.
# Checked by name only (see _call_target_name) because this module is never
# imported to find out what name resolves to -- the source text is the only
# thing available, on purpose (see read_declaration_from_source).
_DECLARATION_CALL_NAME = "PartDeclaration"

# The field order load_declaration_from_blueprint already uses. A source-level
# PART_DECLARATION is required to name every one of these as a keyword -- never
# positionally, so the checker never has to guess which value is which field.
_DECLARATION_FIELDS = (
    "part_id",
    "consumes",
    "produces",
    "resource_class",
    "rate_risk",
    "skipped_tick_effect",
)


def _call_target_name(node: ast.expr) -> str | None:
    """The bare name a call expression's callee reads as in source.

    `PartDeclaration(...)` and `part_declaration.PartDeclaration(...)` both
    read as "PartDeclaration" -- there is no import to resolve, because
    resolving it would mean running the file this function exists to avoid
    running.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def read_declaration_from_source(source_path: pathlib.Path) -> PartDeclaration:
    """A built part's real wiring, read WITHOUT importing or executing its file
    (RL-067, RL-070).

    Generating a dashboard must never run part code. Once real parts exist,
    importing one to read its wiring would run whatever that part's import does
    -- open a socket, spawn a thread, start a forkserver -- every time the board
    is regenerated, and a part that hangs or crashes on import would take the
    board generator down with it, exactly when a broken part is most in need of
    being seen. So this parses the source with `ast` and reads PART_DECLARATION
    as data, never as code.

    This is a rule on every built part, not a limitation of this reader: a
    part's PART_DECLARATION must be written as a literal `PartDeclaration(...)`
    call, every field a keyword, every value a literal -- no computation, no
    name resolved at runtime, no f-string. A declaration that cannot be read
    without running the program is a declaration this checker cannot trust, and
    T-1 (every feature is the same shape) is what makes that reasonable to
    demand of every part alike.
    """
    try:
        source_text = source_path.read_text()
    except OSError as failure:
        raise ModuleDeclaresNoWiring(f"{source_path} could not be read: {failure}") from failure

    try:
        tree = ast.parse(source_text, filename=str(source_path))
    except SyntaxError as failure:
        raise ModuleDeclaresNoWiring(f"{source_path} does not parse: {failure}") from failure

    assignment = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == PART_DECLARATION_ATTRIBUTE for t in node.targets)
        ),
        None,
    )
    if assignment is None:
        raise ModuleDeclaresNoWiring(
            f"{source_path} has no {PART_DECLARATION_ATTRIBUTE} assignment -- a built part must "
            f"state its own real wiring in this shape before RL-067 can check it"
        )

    call = assignment.value
    if not isinstance(call, ast.Call) or _call_target_name(call.func) != _DECLARATION_CALL_NAME:
        raise ModuleDeclaresNoWiring(
            f"{source_path}: {PART_DECLARATION_ATTRIBUTE} is not a {_DECLARATION_CALL_NAME}(...) call"
        )
    if call.args:
        raise ModuleDeclaresNoWiring(
            f"{source_path}: {PART_DECLARATION_ATTRIBUTE} passes positional arguments -- every field "
            f"must be a keyword so the checker never has to guess which value is which"
        )

    fields: dict[str, object] = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            raise ModuleDeclaresNoWiring(
                f"{source_path}: {PART_DECLARATION_ATTRIBUTE} passes **kwargs -- every field must be "
                f"a literal keyword argument, not expanded from elsewhere"
            )
        try:
            fields[keyword.arg] = ast.literal_eval(keyword.value)
        except (ValueError, SyntaxError) as failure:
            raise ModuleDeclaresNoWiring(
                f"{source_path}: {PART_DECLARATION_ATTRIBUTE}'s {keyword.arg!r} argument is not a "
                f"literal ({failure}) -- a built part's declaration must be written as literal "
                f"values, never computed or resolved at runtime"
            ) from failure

    missing = [name for name in _DECLARATION_FIELDS if name not in fields]
    if missing:
        raise ModuleDeclaresNoWiring(
            f"{source_path}: {PART_DECLARATION_ATTRIBUTE} is missing {', '.join(missing)}"
        )

    try:
        return PartDeclaration(
            part_id=fields["part_id"],
            consumes=tuple(fields["consumes"]),
            produces=tuple(fields["produces"]),
            resource_class=ResourceClass(fields["resource_class"]),
            rate_risk=RateRisk(fields["rate_risk"]),
            skipped_tick_effect=SkippedTickEffect(fields["skipped_tick_effect"]),
        )
    except (TypeError, ValueError) as failure:
        raise ModuleDeclaresNoWiring(
            f"{source_path}: {PART_DECLARATION_ATTRIBUTE} has an invalid field value ({failure})"
        ) from failure


def load_declaration_from_blueprint(part_id: str) -> PartDeclaration:
    """Read one part's declaration from docs/features.json.

    The blueprint is the single source of truth: code follows the registry, never
    the other way round.
    """
    registry = json.loads(BLUEPRINT_PATH.read_text())
    for feature in registry["features"]:
        if feature["id"] == part_id:
            return PartDeclaration(
                part_id=part_id,
                consumes=tuple(feature["consumes"]),
                produces=tuple(feature["produces"]),
                resource_class=ResourceClass(feature["resource_class"]),
                rate_risk=RateRisk(feature["rate_risk"]),
                skipped_tick_effect=SkippedTickEffect(feature["skipped_tick_effect"]),
            )
    raise KeyError(
        f"'{part_id}' is not in {BLUEPRINT_PATH}. A part that is not in the blueprint is "
        f"not a part -- a design change is a blueprint edit first, then code."
    )
