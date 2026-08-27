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

import math
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.risk_types import NO_RISK_ALLOWED, RiskLimit

PART_ID = "exposure-limiter"

PART_DECLARATION = PartDeclaration(
    part_id="exposure-limiter",
    consumes=(
        "account-balance", "correlation-cluster", "exposure-view", "position",
        "stop-adjustment", "trade-cluster",
    ),
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
    # Positions seen before any balance had arrived. Their exposure is unknown, not
    # zero: counted here so a limiter that has never been able to measure anything
    # says so, rather than publishing a full limit that looks like a measurement.
    positions_without_a_balance: int = 0
    allotment: float | None = None
    binding_by_cap: dict = field(default_factory=dict)
    largest_cluster_share: float = 0.0
    clusters_known: int = 0
    unclustered_symbols: int = 0
    # Open positions with no stop anybody has named, counted at their full
    # notional because a position with no stop resting can lose all of it. Named
    # apart from the total, so a board can tell a book that is risking a lot from
    # a book nobody has protected.
    positions_without_a_stop: int = 0


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
            # `inf` is a cap the operator has removed, and it is spelled as its
            # own value rather than as a large number: a cap of 1000% would still
            # bind somewhere and would read as an estimate of something. Zero is
            # still refused, because "no cap" and "a cap of nothing" are opposite
            # instructions and one of them refuses every trade while looking like
            # permission.
            if math.isnan(value) or (not 0.0 < value <= 1.0 and value != math.inf):
                raise ValueError(
                    f"the {name} cap must be a fraction of the allotment in (0, 1], "
                    f"or inf for no cap at all"
                )
        self._per_position = maximum_per_position_fraction
        self._total = maximum_total_fraction
        self._per_cluster = maximum_per_cluster_fraction
        self._now_ns = now_ns
        self._exposure: dict[tuple[str, str], float] = {}
        # What each position is worth, kept beside the fraction so a change of
        # allotment re-measures the book rather than reinterpreting old fractions.
        self._notional: dict[tuple[str, str], float] = {}
        # What each position loses if it goes to its stop, in quote currency, and
        # what the entry and quantity behind that were. The caps are the
        # operator's risk numbers -- "the most one position may risk" -- and this
        # part summed notional against them until 2026-08-26: a position sized to
        # risk 1% of the allotment at a 0.5% stop is about 200% of it in notional,
        # so twelve open positions read as 199% against a 5% cap and nothing new
        # was allowed on any symbol. Measured at 15:16 that day: position-sizer
        # refused 62,935 of 71,233 actionable intents with `refused_no_risk_allowed`.
        self._risk_quote: dict[tuple[str, str], float] = {}
        # The stop last named for a position, from `stop-adjustment`. A position
        # with no stop here is not counted as safe -- see `_risk_of`.
        self._stop_price: dict[tuple[str, str], float] = {}
        self._entry_price: dict[tuple[str, str], float] = {}
        self._quantity: dict[tuple[str, str], float] = {}
        self._allotment: float | None = None
        self._cluster_of: dict[str, str] = {}
        self.standing = ExposureStanding()

    def set_allotment(self, allotment: float) -> None:
        """What the caps are fractions of. Every position already seen is re-measured.

        Re-measured rather than left: the caps are fractions, so a book worth 400
        against an allotment of 10,000 is a different exposure the moment the
        allotment changes, and holding the old fraction would cap against a balance
        the account no longer has.
        """
        if allotment <= 0:
            return
        self.standing.allotment = allotment
        self._allotment = allotment
        self._exposure = {
            key: self._risk_of(key) / allotment
            for key, notional in self._notional.items()
            if notional > 0
        }

    def observe_stop(self, venue_id: str, symbol: str, stop_price: float | None) -> None:
        """Where this position's stop is, so what it risks can be measured.

        From `stop-adjustment`, which is what the parts that move a stop publish.
        A position that has never been offered one is absent here rather than at
        zero, and `_risk_of` counts it at its full notional.
        """
        key = (venue_id, symbol)
        if stop_price is None or stop_price <= 0:
            self._stop_price.pop(key, None)
        else:
            self._stop_price[key] = stop_price
        if key in self._notional and self._allotment:
            self._exposure[key] = self._risk_of(key) / self._allotment

    def _risk_of(self, key: tuple[str, str]) -> float:
        """What this position loses if it goes to its stop, in quote currency.

        **A position whose stop nobody can name is counted at its full notional.**
        Not excluded and not assumed small: a position with no stop resting can
        lose all of it, so its notional is its risk. On 2026-08-26 that was not a
        defensive default but the state of the book -- `stop-order-manager` had
        placed 0 stops against 12 open positions.
        """
        notional = self._notional.get(key, 0.0)
        entry = self._entry_price.get(key)
        quantity = self._quantity.get(key)
        stop = self._stop_price.get(key)
        if stop is None or entry is None or not quantity:
            self.standing.positions_without_a_stop = sum(
                1 for held in self._notional if held not in self._stop_price
            )
            return notional
        self.standing.positions_without_a_stop = sum(
            1 for held in self._notional if held not in self._stop_price
        )
        # Never more than the position is worth: a stop placed the wrong side of
        # the entry would otherwise measure as more risk than the whole position
        # can lose, and that is a fault in the stop rather than a bigger position.
        return min(abs(entry - stop) * abs(quantity), notional)

    def observe_position(
        self, venue_id: str, symbol: str, notional: float,
        entry_price: float | None = None, quantity: float | None = None,
    ) -> None:
        """One position's exposure, as what it is worth at what it cost.

        Given a notional rather than a fraction, because the producer of `position`
        publishes a quantity and an entry price and has never published a fraction.
        This part read one through a `getattr(position, "exposure_fraction", 0.0)`
        default until 2026-08-25, so every position was observed at zero, the sum
        of the book was zero, and the limit published was the full per-position cap
        on every tick since the part first ran.
        """
        self.standing.positions_seen += 1
        key = (venue_id, symbol)
        if notional <= 0:
            self._notional.pop(key, None)
            self._exposure.pop(key, None)
            self._entry_price.pop(key, None)
            self._quantity.pop(key, None)
            self._stop_price.pop(key, None)
            return
        self._notional[key] = notional
        if entry_price:
            self._entry_price[key] = entry_price
        if quantity:
            self._quantity[key] = quantity
        if self._allotment is None:
            # Not recorded as an exposure of zero: an unmeasurable position is
            # absent from the book rather than free, and the count says so.
            self.standing.positions_without_a_balance += 1
            return
        self._exposure[key] = self._risk_of(key) / self._allotment

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
        """Why this much and no more, in the words an operator asking would use.

        A removed cap is named as removed rather than printed: `{inf:.0%}` reads
        "inf%", which looks like a measurement of something nobody measured.
        """
        if binding_cap == PER_POSITION:
            return f"no single position may exceed {self._per_position:.0%} of the allotment"
        if binding_cap == TOTAL_GROSS:
            if self._total == math.inf:
                return (
                    f"{total_used:.0%} of the allotment is exposed and no total cap is set, "
                    f"so how many positions may be open at once is not limited here"
                )
            return (
                f"{total_used:.0%} of the allotment is already exposed against a "
                f"{self._total:.0%} total cap, leaving {allowed:.0%}"
            )
        if self._per_cluster == math.inf:
            return (
                f"cluster {cluster!r} holds {cluster_used:.0%} and no cluster cap is set"
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
        "positions_without_a_balance": limiter.standing.positions_without_a_balance,
        "positions_without_a_stop": limiter.standing.positions_without_a_stop,
        "allotment": limiter.standing.allotment,
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
    from runtime.input_assembly import Batch, LatestByKey

    positions = Batch(read=context.bus.reader("position"))
    balances = LatestByKey(
        read=context.bus.reader("account-balance"),
        key_of=lambda balance: balance.segment,
    )
    views = Batch(read=context.bus.reader("exposure-view"))
    # Where each open position's stop is, which is what turns a position into an
    # amount of risk. Read since 2026-08-26; before it this part summed notional
    # and judged it against the operator's risk caps.
    stops = Batch(read=context.bus.reader("stop-adjustment"))
    clusters = Batch(read=context.bus.reader("correlation-cluster"))
    trade_clusters = Batch(read=context.bus.reader("trade-cluster"))
    publish_limit = context.bus.publisher_for("risk-limit")
    segment = str(context.setting("segment_id").value)

    def read_exposure(limiter):
        # Exposure views and clusters are drained so a slow reader cannot fill an
        # inbox, and used where the limiter has somewhere to put them. Nothing
        # produces either in the first runs; the positions do the work.
        views.payloads()
        clusters.payloads()
        trade_clusters.payloads()
        balance = balances.mapping().get(segment)
        if balance is not None:
            limiter.set_allotment(balance.equity)
        for adjustment in stops.payloads():
            # Whichever stop is current: `new_stop` is where the position's stop
            # is after this decision, including the decision to leave it where it
            # was, so `previous_stop` is only read when there is no new one.
            limiter.observe_stop(
                adjustment.venue_id,
                adjustment.symbol,
                getattr(adjustment, "new_stop", None) or getattr(adjustment, "previous_stop", None),
            )
        for position in positions.payloads():
            limiter.observe_position(
                position.venue_id,
                position.symbol,
                abs(position.quantity) * position.average_entry_price,
                entry_price=position.average_entry_price,
                quantity=position.quantity,
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
