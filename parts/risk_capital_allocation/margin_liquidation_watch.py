"""margin-liquidation-watch: zero the limit when equity nears the maintenance margin.

The one limiter whose failure is not a loss but the end of the segment. A
liquidation does not merely close a position at a bad price -- it closes it at the
venue's price, takes the maintenance margin, and does it while nothing in this
system is deciding anything.

It watches two distances and takes the worse:

- **Price distance**, per position: how far the market is from that position's
  own liquidation price, as a fraction.
- **Equity distance**, per account: how much of the balance plus unrealised
  result remains above the total maintenance requirement.

Neither alone is enough. A single position can be inches from liquidation while
the account looks healthy, and an account can be one bad tick from a margin call
while every individual position looks fine.

**Missing inputs stop trading rather than being assumed away.** A position whose
liquidation price is unknown is treated as being at the danger threshold, because
the alternative -- assuming it is safe -- is the assumption that costs the account.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.price_frames import levels_in
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.risk_types import NO_RISK_ALLOWED, RiskLimit

PART_ID = "margin-liquidation-watch"

PART_DECLARATION = PartDeclaration(
    part_id="margin-liquidation-watch",
    consumes=("position", "account-balance", "liquidation-price", "symbol-price-frame"),
    produces=("risk-limit", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

SAFE = "safe"
NEAR_LIQUIDATION = "near-liquidation"
NEAR_MARGIN_CALL = "near-margin-call"
UNKNOWN_DISTANCE = "unknown-distance"


@dataclass
class WatchStanding:
    positions_watched: int = 0
    closest_price_distance: float | None = None
    equity_headroom_fraction: float | None = None
    zero_limits_issued: int = 0
    unknown_liquidation_prices: int = 0
    state: str = SAFE
    closest_symbol: str | None = None


class MarginLiquidationWatch:
    """Stops new risk when any position, or the account, is close to being closed for us."""

    def __init__(
        self,
        danger_price_distance: float,
        danger_equity_headroom: float,
        allowed_fraction_when_safe: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < danger_price_distance < 1.0:
            raise ValueError("the danger distance must be a fraction of price in (0, 1)")
        self._danger_distance = danger_price_distance
        self._danger_headroom = danger_equity_headroom
        self._allowed = allowed_fraction_when_safe
        self._now_ns = now_ns
        self._distances: dict[tuple[str, str], float | None] = {}
        self._equity: float | None = None
        self._maintenance_requirement: float | None = None
        self.standing = WatchStanding()

    def observe_liquidation_price(
        self, venue_id: str, symbol: str, liquidation_price: float | None, mark_price: float | None
    ) -> None:
        """How far this position is from being closed by the venue.

        None for either input means the distance is unknown, and unknown is
        recorded as unknown rather than dropped -- a position nobody can measure
        is exactly the one to stop adding risk beside.
        """
        key = (venue_id, symbol)
        if liquidation_price is None or not mark_price:
            self._distances[key] = None
            self.standing.unknown_liquidation_prices += 1
        else:
            self._distances[key] = abs(mark_price - liquidation_price) / mark_price
        self.standing.positions_watched = len(self._distances)

    def observe_position_closed(self, venue_id: str, symbol: str) -> None:
        self._distances.pop((venue_id, symbol), None)
        self.standing.positions_watched = len(self._distances)

    def observe_account(self, equity: float, maintenance_requirement: float) -> None:
        """Balance plus unrealised, against what the venue requires be maintained."""
        self._equity = equity
        self._maintenance_requirement = maintenance_requirement

    def read_limit(self) -> RiskLimit:
        known = {key: distance for key, distance in self._distances.items() if distance is not None}
        unknown = [key for key, distance in self._distances.items() if distance is None]

        closest_key, closest = None, None
        if known:
            closest_key, closest = min(known.items(), key=lambda item: item[1])
            self.standing.closest_price_distance = closest
            self.standing.closest_symbol = closest_key[1]

        headroom = None
        if self._equity is not None and self._maintenance_requirement is not None:
            if self._equity > 0:
                headroom = (self._equity - self._maintenance_requirement) / self._equity
            else:
                headroom = 0.0
            self.standing.equity_headroom_fraction = headroom

        if unknown:
            self.standing.state = UNKNOWN_DISTANCE
            return self._stop(
                f"{len(unknown)} open position(s) have no measurable liquidation price; "
                f"assuming they are safe is the assumption that ends a segment"
            )
        if closest is not None and closest <= self._danger_distance:
            self.standing.state = NEAR_LIQUIDATION
            return self._stop(
                f"{closest_key[1]} is {closest:.2%} from its liquidation price, inside the "
                f"{self._danger_distance:.2%} danger distance"
            )
        if headroom is not None and headroom <= self._danger_headroom:
            self.standing.state = NEAR_MARGIN_CALL
            return self._stop(
                f"equity is {headroom:.2%} above the maintenance requirement, inside the "
                f"{self._danger_headroom:.2%} danger headroom"
            )

        self.standing.state = SAFE
        return RiskLimit(
            limiter=PART_ID,
            fraction_of_allotment=self._allowed,
            reason=(
                f"nearest liquidation is {closest:.2%} away"
                if closest is not None
                else "no leveraged position is open"
            ),
            is_binding=False,
            decided_at_ns=self._now_ns(),
        )

    def _stop(self, reason: str) -> RiskLimit:
        self.standing.zero_limits_issued += 1
        return RiskLimit(
            limiter=PART_ID,
            fraction_of_allotment=NO_RISK_ALLOWED,
            reason=reason,
            is_binding=True,
            decided_at_ns=self._now_ns(),
        )


def describe_margin(watch: MarginLiquidationWatch) -> dict:
    return {
        "part_id": PART_ID,
        "state": watch.standing.state,
        "positions_watched": watch.standing.positions_watched,
        "closest_price_distance": watch.standing.closest_price_distance,
        "closest_symbol": watch.standing.closest_symbol,
        "equity_headroom_fraction": watch.standing.equity_headroom_fraction,
        "unknown_liquidation_prices": watch.standing.unknown_liquidation_prices,
        "zero_limits_issued": watch.standing.zero_limits_issued,
    }


def run_margin_liquidation_watch(
    watch: MarginLiquidationWatch, control_socket, read_positions_and_account, publish_limit,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_positions_and_account(watch)
        publish_limit(watch.read_limit())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_margin(watch),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Liquidation prices come from liquidation-price-tracker; the mark is the
    latest trade for the symbol; the account's equity and maintenance
    requirement come from the segment's balance, the requirement being the
    maintenance rate over the notional the watch can see in its positions. A
    position whose quantity goes to zero is forgotten.
    """
    from runtime.input_assembly import Batch, LatestByKey

    positions = Batch(read=context.bus.reader("position"))
    balances = LatestByKey(read=context.bus.reader("account-balance"), key_of=lambda b: b.segment)
    liquidations = LatestByKey(read=context.bus.reader("liquidation-price"), key_of=lambda l: (l.venue_id, l.symbol))
    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    publish_limits = context.bus.publisher_for("risk-limit")
    segment = str(context.setting("segment_id").value)
    maintenance_rate = context.number("maintenance_margin_rate")
    watch = MarginLiquidationWatch(
        danger_price_distance=context.number("liquidation_danger_price_distance"),
        danger_equity_headroom=context.number("liquidation_danger_equity_headroom"),
        allowed_fraction_when_safe=context.number("risk_allowed_fraction_when_clear"),
    )
    marks: dict[tuple[str, str], float] = {}
    notional: dict[tuple[str, str], float] = {}

    def read_positions_and_account(_watch) -> None:
        for trade in levels_in(trades.payloads()):
                marks[(trade.venue_id, trade.symbol)] = trade.price
        for position in positions.payloads():
            key = (position.venue_id, position.symbol)
            if position.quantity == 0:
                notional.pop(key, None)
                watch.observe_position_closed(position.venue_id, position.symbol)
            else:
                notional[key] = abs(position.quantity) * position.average_entry_price
        for key, liquidation in liquidations.mapping().items():
            if key in notional:
                watch.observe_liquidation_price(
                    key[0], key[1], liquidation.liquidation_price, marks.get(key)
                )
        balance = balances.mapping().get(segment)
        if balance is not None:
            watch.observe_account(balance.equity, maintenance_rate * sum(notional.values()))

    return run_margin_liquidation_watch(
        watch=watch,
        control_socket=context.control_socket,
        read_positions_and_account=read_positions_and_account,
        publish_limit=lambda limit: publish_limits((limit,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
