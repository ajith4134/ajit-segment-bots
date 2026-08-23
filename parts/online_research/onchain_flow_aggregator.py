"""onchain-flow-aggregator: net exchange flow, with its coverage attached.

Individual transfers are events; the aggregate over a window is the quantity people
actually reason with. Aggregation is also where the number becomes dangerous,
because a net flow figure looks equally authoritative whether it covers every known
exchange cluster or three of them -- **and a partial figure can have the opposite
sign to the complete one.** One large withdrawal from an uncovered cluster flips
net inflow to net outflow, and nothing in the number itself shows that.

So coverage travels with every figure, and a figure below the coverage floor is
published as unusable rather than published smaller. That is Rule 8 applied to an
aggregate: absence of evidence renders as its own state.

The window is a real window, not a running total. Flow over the last hour and flow
since the process started are different quantities, and the second one grows
monotonically until it means nothing. Transfers older than the window fall out.

Inflow and outflow are kept separately as well as netted. A quiet day and a day
where 400 million moved each way net to the same zero, and those are not the same
day -- the second one is somebody rotating and somebody else deciding.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.external_research_types import COMPLETE, OnchainFlow, PARTIAL, UNAVAILABLE
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "onchain-flow-aggregator"

PART_DECLARATION = PartDeclaration(
    part_id="onchain-flow-aggregator",
    consumes=(),
    produces=("onchain-flow", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

AGGREGATED = "aggregated"
COVERAGE_TOO_THIN = "too-few-clusters-covered-for-the-sign-to-be-trusted"
NOTHING_IN_WINDOW = "no-transfer-inside-the-window"


@dataclass(frozen=True)
class FlowReading:
    asset: str
    state: str
    flow: OnchainFlow | None
    transfers_in_window: int
    clusters_seen: tuple
    clusters_missing: tuple
    reason: str
    measured_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == AGGREGATED and self.flow is not None


@dataclass
class AggregatorStanding:
    transfers_admitted: int = 0
    transfers_expired_out: int = 0
    readings: int = 0
    usable_readings: int = 0
    thin_coverage_readings: int = 0
    sign_would_have_flipped: int = 0


class OnchainFlowAggregator:
    """Nets exchange inflow against outflow over a real window, with coverage."""

    def __init__(
        self,
        window_seconds: float,
        minimum_coverage_fraction: float,
        now_ns=time.time_ns,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError(
                "a running total since start grows monotonically until it means nothing; "
                "the window is a real window"
            )
        if not 0.0 < minimum_coverage_fraction <= 1.0:
            raise ValueError("the coverage floor is a fraction inside (0, 1]")
        self._window_seconds = window_seconds
        self._minimum_coverage = minimum_coverage_fraction
        self._now_ns = now_ns
        self._known_clusters: dict[str, set] = {}
        self._transfers: dict[str, list] = {}
        self.standing = AggregatorStanding()

    def declare_cluster(self, asset: str, cluster: str) -> None:
        """A known exchange cluster. Coverage is measured against what is declared."""
        self._known_clusters.setdefault(asset, set()).add(cluster)

    def observe_transfer(self, transfer, cluster: str | None = None) -> None:
        """Only transfers that change what can be sold move the flow figure."""
        if not (transfer.could_become_supply or transfer.leaves_the_market):
            return
        self._transfers.setdefault(transfer.asset, []).append(
            (transfer, cluster or transfer.to_venue_id or "unattributed")
        )
        self.standing.transfers_admitted += 1

    def measure(self, asset: str) -> FlowReading:
        self.standing.readings += 1
        now = self._now_ns()
        cutoff = now - int(self._window_seconds * 1e9)

        kept = []
        for transfer, cluster in self._transfers.get(asset, []):
            if transfer.confirmed_at_ns >= cutoff:
                kept.append((transfer, cluster))
            else:
                self.standing.transfers_expired_out += 1
        self._transfers[asset] = kept

        known = self._known_clusters.get(asset, set())
        seen = {cluster for _, cluster in kept}
        missing = tuple(sorted(known - seen))

        if not kept:
            return self._reading(
                asset, NOTHING_IN_WINDOW, None, 0, tuple(sorted(seen)), missing,
                f"no transfer inside the last {self._window_seconds / 3600.0:.1f}h. That is "
                f"a quiet window, not a zero net flow, and the two are read differently",
            )

        inflow = sum(
            self._size(transfer) for transfer, _ in kept if transfer.could_become_supply
        )
        outflow = sum(
            self._size(transfer) for transfer, _ in kept if transfer.leaves_the_market
        )

        clusters_known = len(known) if known else len(seen)
        clusters_covered = len(seen & known) if known else len(seen)
        coverage = clusters_covered / clusters_known if clusters_known else 0.0

        if coverage < self._minimum_coverage:
            self.standing.thin_coverage_readings += 1
            # Whether the missing clusters could flip the sign is exactly the
            # question, and the honest answer when coverage is thin is that they could.
            self.standing.sign_would_have_flipped += 1
            return self._reading(
                asset, COVERAGE_TOO_THIN, None, len(kept), tuple(sorted(seen)), missing,
                f"{clusters_covered} of {clusters_known} cluster(s) covered "
                f"({coverage:.0%}), below the {self._minimum_coverage:.0%} floor. One large "
                f"movement from {missing[0] if missing else 'an uncovered cluster'} would "
                f"reverse the sign, and nothing in the number would show it",
            )

        flow = OnchainFlow(
            asset=asset,
            window_seconds=self._window_seconds,
            inflow=inflow,
            outflow=outflow,
            clusters_covered=clusters_covered,
            clusters_known=clusters_known,
            completeness=COMPLETE if coverage >= 1.0 else PARTIAL,
            measured_at_ns=now,
        )
        self.standing.usable_readings += 1
        return self._reading(
            asset, AGGREGATED, flow, len(kept), tuple(sorted(seen)), missing,
            f"{flow.net_flow:+,.0f} net over {self._window_seconds / 3600.0:.1f}h from "
            f"{inflow:,.0f} in and {outflow:,.0f} out. Both sides are kept: a quiet window "
            f"and a heavily two-sided one net the same and are not the same day",
        )

    def _size(self, transfer) -> float:
        """Quote value where it exists, quantity otherwise, never mixed silently."""
        return transfer.quote_value if transfer.quote_value is not None else transfer.quantity

    def _reading(
        self, asset, state, flow, transfers, seen, missing, reason,
    ) -> FlowReading:
        return FlowReading(
            asset=asset, state=state, flow=flow, transfers_in_window=transfers,
            clusters_seen=seen, clusters_missing=missing, reason=reason,
            measured_at_ns=self._now_ns(),
        )


def describe_flow_aggregation(aggregator: OnchainFlowAggregator) -> dict:
    return {
        "part_id": PART_ID,
        "transfers_admitted": aggregator.standing.transfers_admitted,
        "transfers_expired_out_of_the_window": aggregator.standing.transfers_expired_out,
        "readings": aggregator.standing.readings,
        "usable_readings": aggregator.standing.usable_readings,
        "readings_refused_for_thin_coverage": aggregator.standing.thin_coverage_readings,
        "window_seconds": aggregator._window_seconds,
        "publishes_a_partial_figure_as_complete": False,
        "keeps_a_running_total_since_start": False,
    }


def run_onchain_flow_aggregator(
    aggregator: OnchainFlowAggregator, control_socket, read_assets, publish_flow,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for asset in read_assets():
            reading = aggregator.measure(asset)
            if reading.is_usable:
                publish_flow(reading.flow)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
