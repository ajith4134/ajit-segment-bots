"""What every part declares about itself. One shape, no privileged parts (T-1).

Three of these fields are new in phase 0 and are added to all 321 blueprint entries
by the edit in Task 13: resource_class (section 7), and the pair rate_risk and
skipped_tick_effect that section 6 requires before a part may be throttled at all.
"""

from __future__ import annotations

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
