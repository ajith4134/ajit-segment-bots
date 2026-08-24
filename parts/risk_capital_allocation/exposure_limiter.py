"""exposure-limiter: cap what may be risked across open positions and correlated bets.

Three caps, and the third is the one that actually saves accounts.

- **Per position**, so no single symbol can carry the segment.
- **Total gross exposure**, so a hundred small positions cannot add up to one
  enormous one.
- **Per correlation cluster**, because ten long altcoin positions are one long
  bitcoin position wearing ten names. Sized independently they each look modest,
  and they all lose together.

The cluster cap is what separates this from a naive limiter. A system that only
capped per position and total would happily hold its entire allowance in
instruments that move as one, and would discover the concentration only in the
drawdown.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.risk_types import NO_RISK_ALLOWED, RiskLimit

PART_ID = "exposure-limiter"

PART_DECLARATION = PartDeclaration(
    part_id="exposure-limiter",
    consumes=("position", "exposure-view", "correlation-cluster", "trade-cluster"),
    produces=("risk-limit", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PER_POSITION = "per-position"
TOTAL_GROSS = "total-gross"
PER_CLUSTER = "per-correlation-cluster"
UNCLUSTERED = "unclustered"


@dataclass
class ExposureStanding:
    positions_seen: int = 0
    limits_issued: int = 0
    binding_by_cap: dict = field(default_factory=dict)
    largest_cluster_share: float = 0.0
    clusters_known: int = 0
    unclustered_symbols: int = 0


class ExposureLimiter:
    """Reduces the allowed risk to whatever the tightest of three caps permits."""

    def __init__(
        self,
        maximum_per_position_fraction: float,
        maximum_total_fraction: float,
        maximum_per_cluster_fraction: float,
        now_ns=time.time_ns,
    ) -> None:
        for name, value in (
            ("per position", maximum_per_position_fraction),
            ("total", maximum_total_fraction),
            ("per cluster", maximum_per_cluster_fraction),
        ):
            if not 0.0 < value <= 1.0:
                raise ValueError(f"the {name} cap must be a fraction of the allotment in (0, 1]")
        self._per_position = maximum_per_position_fraction
        self._total = maximum_total_fraction
        self._per_cluster = maximum_per_cluster_fraction
        self._now_ns = now_ns
        self._exposure: dict[tuple[str, str], float] = {}
        self._cluster_of: dict[str, str] = {}
        self.standing = ExposureStanding()

    def observe_position(self, venue_id: str, symbol: str, exposure_fraction: float) -> None:
        """One position's exposure as a fraction of the segment's allotment."""
        self.standing.positions_seen += 1
        key = (venue_id, symbol)
        if exposure_fraction <= 0:
            self._exposure.pop(key, None)
        else:
            self._exposure[key] = exposure_fraction

    def set_correlation_cluster(self, symbol: str, cluster: str) -> None:
        """Which cluster a symbol belongs to, from the correlation part upstream."""
        self._cluster_of[symbol] = cluster
        self.standing.clusters_known = len(set(self._cluster_of.values()))

    def read_limit(self, symbol: str | None = None) -> RiskLimit:
        """The headroom left for a new position, by the tightest cap that applies."""
        self.standing.limits_issued += 1
        total_used = sum(self._exposure.values())
        total_headroom = max(0.0, self._total - total_used)

        cluster = self._cluster_of.get(symbol) if symbol else None
        if symbol is not None and cluster is None:
            # An unclustered symbol is treated as its own cluster rather than as
            # correlated with nothing: assuming independence is the assumption
            # that ends accounts, and it is the one nobody has evidence for.
            cluster = f"{UNCLUSTERED}:{symbol}"
            self.standing.unclustered_symbols += 1

        cluster_used = 0.0
        if cluster is not None:
            cluster_used = sum(
                exposure
                for (_, held_symbol), exposure in self._exposure.items()
                if self._cluster_of.get(held_symbol, f"{UNCLUSTERED}:{held_symbol}") == cluster
            )
            self.standing.largest_cluster_share = max(
                self.standing.largest_cluster_share, cluster_used
            )
        cluster_headroom = max(0.0, self._per_cluster - cluster_used)

        caps = {
            PER_POSITION: self._per_position,
            TOTAL_GROSS: total_headroom,
            PER_CLUSTER: cluster_headroom if cluster is not None else self._per_cluster,
        }
        binding_cap, allowed = min(caps.items(), key=lambda item: item[1])
        self.standing.binding_by_cap[binding_cap] = (
            self.standing.binding_by_cap.get(binding_cap, 0) + 1
        )

        return RiskLimit(
            limiter=PART_ID,
            fraction_of_allotment=allowed,
            reason=self._reason(binding_cap, allowed, total_used, cluster, cluster_used),
            is_binding=allowed <= NO_RISK_ALLOWED,
            decided_at_ns=self._now_ns(),
        )

    def _reason(self, binding_cap, allowed, total_used, cluster, cluster_used) -> str:
        if binding_cap == PER_POSITION:
            return f"no single position may exceed {self._per_position:.0%} of the allotment"
        if binding_cap == TOTAL_GROSS:
            return (
                f"{total_used:.0%} of the allotment is already exposed against a "
                f"{self._total:.0%} total cap, leaving {allowed:.0%}"
            )
        return (
            f"cluster {cluster!r} already holds {cluster_used:.0%} against a "
            f"{self._per_cluster:.0%} cap, leaving {allowed:.0%}"
        )

    @property
    def total_exposure(self) -> float:
        return sum(self._exposure.values())


def describe_exposure(limiter: ExposureLimiter) -> dict:
    return {
        "part_id": PART_ID,
        "positions_seen": limiter.standing.positions_seen,
        "open_positions": len(limiter._exposure),
        "total_exposure": limiter.total_exposure,
        "limits_issued": limiter.standing.limits_issued,
        "binding_by_cap": dict(limiter.standing.binding_by_cap),
        "clusters_known": limiter.standing.clusters_known,
        "largest_cluster_share": limiter.standing.largest_cluster_share,
        "unclustered_symbols_seen": limiter.standing.unclustered_symbols,
    }


def run_exposure_limiter(
    limiter: ExposureLimiter, control_socket, read_exposure, publish_limit,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_exposure(limiter)
        publish_limit(limiter.read_limit())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_exposure(limiter),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Publishes a limit every tick whether or not anything is open, because a limit
    is a level: the sizer needs to know what it may risk now, and silence would be
    indistinguishable from a limit of zero. With nothing open the limit is the full
    per-position fraction, which is the correct answer to "how much may I risk"
    when nothing is at risk yet.
    """
    from runtime.input_assembly import Batch

    positions = Batch(read=context.bus.reader("position"))
    views = Batch(read=context.bus.reader("exposure-view"))
    clusters = Batch(read=context.bus.reader("correlation-cluster"))
    trade_clusters = Batch(read=context.bus.reader("trade-cluster"))
    publish_limit = context.bus.publisher_for("risk-limit")

    def read_exposure(limiter):
        # Exposure views and clusters are drained so a slow reader cannot fill an
        # inbox, and used where the limiter has somewhere to put them. Nothing
        # produces either in the first runs; the positions do the work.
        views.payloads()
        clusters.payloads()
        trade_clusters.payloads()
        for position in positions.payloads():
            limiter.observe_position(
                position.venue_id, position.symbol, getattr(position, "exposure_fraction", 0.0)
            )

    return run_exposure_limiter(
        limiter=ExposureLimiter(
            maximum_per_position_fraction=context.number("risk_maximum_per_position_fraction"),
            maximum_total_fraction=context.number("risk_maximum_total_fraction"),
            maximum_per_cluster_fraction=context.number("risk_maximum_per_cluster_fraction"),
        ),
        control_socket=context.control_socket,
        read_exposure=read_exposure,
        publish_limit=lambda limit: publish_limit([limit]),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
