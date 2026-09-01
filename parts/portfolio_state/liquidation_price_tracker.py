"""liquidation-price-tracker: the price at which this bot's own position is force-closed."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.price_frames import levels_in
from runtime.price_staleness import ObservedPrice
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import LONG

PART_ID = "liquidation-price-tracker"

PART_DECLARATION = PartDeclaration(
    part_id="liquidation-price-tracker",
    consumes=("position", "leverage-choice", "symbol-price-frame"),
    produces=("liquidation-price", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)


@dataclass(frozen=True)
class LiquidationPrice:
    """Where this position dies, and how far away that is right now."""

    venue_id: str
    symbol: str
    direction: str
    entry_price: float
    leverage: float
    maintenance_margin_rate: float
    liquidation_price: float | None
    distance_fraction: float | None
    reason: str
    observed_at_ns: int


@dataclass
class TrackerStanding:
    positions_tracked: int = 0
    without_leverage: int = 0
    # Readings that were only possible because the position carried its own
    # leverage. Counted because "the selector answered" and "the position
    # remembered" are different facts about how this number was reached.
    leverage_from_the_position_itself: int = 0
    without_margin_rate: int = 0
    computed: int = 0
    closest_distance: float | None = None


class LiquidationPriceTracker:
    """Computes the force-close price from entry, leverage and maintenance margin.

    Isolated-margin arithmetic, which is what this project trades: the position
    dies when its own margin is exhausted, so nothing outside the position enters
    the sum. A cross-margin account would need the whole balance and this would
    be the wrong formula rather than an approximate one -- so it says which it is.

    Every input must be present. A liquidation price computed from an assumed
    maintenance rate is a number that looks exactly like a real one and is the
    single most dangerous kind of wrong here.
    """

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._positions: dict[tuple[str, str], object] = {}
        self._leverage: dict[tuple[str, str], float] = {}
        self._margin_rates: dict[tuple[str, str], float] = {}
        self._prices: dict[tuple[str, str], float] = {}
        self.standing = TrackerStanding()

    def observe_position(self, position) -> None:
        key = (position.venue_id, position.symbol)
        if position.is_flat:
            self._positions.pop(key, None)
        else:
            self._positions[key] = position
        self.standing.positions_tracked = len(self._positions)

    def set_leverage(self, venue_id: str, symbol: str, leverage: float) -> None:
        self._leverage[(venue_id, symbol)] = leverage

    def set_maintenance_margin_rate(self, venue_id: str, symbol: str, rate: float) -> None:
        self._margin_rates[(venue_id, symbol)] = rate

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        """One print, kept with the venue's own time for it.

        `at_ns` has no default. A price with no age cannot be told apart from a
        price that stopped arriving, which is how a symbol frozen for 56 minutes
        was traded on 2026-08-23.
        """
        self._prices[(venue_id, symbol)] = ObservedPrice(
            price=price, observed_at_ns=at_ns
        )

    def compute(self, venue_id: str, symbol: str) -> LiquidationPrice | None:
        key = (venue_id, symbol)
        position = self._positions.get(key)
        if position is None:
            return None

        # The live choice first, then what the position itself was opened at.
        # **`leverage-selector` answers only while an intent is being formed**, so
        # a position restored from a checkpoint -- or one older than this process
        # -- has no choice behind it and could never be measured: on the live spine
        # at 15:01 on 2026-08-26 it refused 7,041 of 12 open positions' readings
        # for exactly this, `margin-liquidation-watch` stopped new risk on each of
        # those symbols, and `position-sizer` refused most of what it saw. The
        # position now carries the leverage its margin was posted at, off the fill
        # that opened it.
        leverage = self._leverage.get(key)
        if leverage is None:
            leverage = getattr(position, "leverage", None)
            if leverage is not None:
                self.standing.leverage_from_the_position_itself += 1
        rate = self._margin_rates.get(key)
        if leverage is None or leverage <= 0:
            self.standing.without_leverage += 1
            return self._unknown(
                position, leverage or 0.0, rate or 0.0,
                "no leverage-choice for this position and none recorded on the position itself",
            )
        if rate is None:
            self.standing.without_margin_rate += 1
            return self._unknown(position, leverage, 0.0, "the venue's maintenance margin rate is unknown")

        entry = position.average_entry_price
        # Isolated margin: the position is liquidated once its loss consumes the
        # initial margin less what must remain as maintenance.
        move = entry * (1.0 / leverage - rate)
        liquidation = entry - move if position.direction == LONG else entry + move
        self.standing.computed += 1

        observed = self._prices.get(key)
        current = None if observed is None else observed.price
        distance = None
        if current:
            distance = abs(current - liquidation) / current
            if self.standing.closest_distance is None or distance < self.standing.closest_distance:
                self.standing.closest_distance = distance

        return LiquidationPrice(
            venue_id=venue_id,
            symbol=symbol,
            direction=position.direction,
            entry_price=entry,
            leverage=leverage,
            maintenance_margin_rate=rate,
            liquidation_price=liquidation,
            distance_fraction=distance,
            reason="isolated margin, from entry, leverage and maintenance rate",
            observed_at_ns=self._now_ns(),
        )

    def _unknown(self, position, leverage, rate, reason) -> LiquidationPrice:
        return LiquidationPrice(
            venue_id=position.venue_id,
            symbol=position.symbol,
            direction=position.direction,
            entry_price=position.average_entry_price,
            leverage=leverage,
            maintenance_margin_rate=rate,
            liquidation_price=None,
            distance_fraction=None,
            reason=reason,
            observed_at_ns=self._now_ns(),
        )

    def compute_all(self) -> tuple[LiquidationPrice, ...]:
        return tuple(
            price
            for venue, symbol in sorted(self._positions)
            if (price := self.compute(venue, symbol)) is not None
        )


def describe_liquidations(tracker: LiquidationPriceTracker) -> dict:
    return {
        "part_id": PART_ID,
        "positions_tracked": tracker.standing.positions_tracked,
        "computed": tracker.standing.computed,
        "without_leverage": tracker.standing.without_leverage,
        "leverage_from_the_position_itself": tracker.standing.leverage_from_the_position_itself,
        "without_margin_rate": tracker.standing.without_margin_rate,
        "closest_distance_fraction": tracker.standing.closest_distance,
    }


def run_liquidation_price_tracker(
    tracker: LiquidationPriceTracker, control_socket, read_inputs, publish_liquidations,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_inputs(tracker)
        publish_liquidations(tracker.compute_all())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_liquidations(tracker),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Positions from the reconciler, leverage from the selector's choice per
    symbol, the mark from the latest trade, and the maintenance rate from the
    one setting every liquidation distance is built from. The full book is
    recomputed once per health interval -- a liquidation price moves with the
    position, not with every print -- but a symbol whose position or leverage
    just changed is computed and published immediately, on the same tick,
    never held back by that throttle.

    Without the immediate path, a position opened between two health-interval
    sweeps had no liquidation price of its own to check against -- only
    whatever this part last published for that key, which could describe a
    position that closed minutes or hours earlier, or none at all. That is
    exactly how a live BTCUSDC short was liquidated 58ms after it opened, at
    395.95 against an entry of 78,769.4: paper-liquidation-simulator watched
    it against a stale liquidation-price message left over from an earlier
    position on the same symbol, and the mismatch made the stop line sit
    almost at zero -- trivially touched by any real print. A position's own
    liquidation price must exist before anything is allowed to check a price
    against it, not up to health_interval_seconds later.
    """
    import time as _time

    from runtime.input_assembly import Batch

    positions = Batch(read=context.bus.reader("position"))
    choices = Batch(read=context.bus.reader("leverage-choice"))
    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    publish_liquidations = context.bus.publisher_for("liquidation-price")
    maintenance_rate = context.number("maintenance_margin_rate")
    tracker = LiquidationPriceTracker()
    last_compute = [float("-inf")]

    def read_inputs(_tracker) -> tuple[tuple[str, str], ...]:
        changed_keys = []
        for position in positions.payloads():
            tracker.observe_position(position)
            tracker.set_maintenance_margin_rate(position.venue_id, position.symbol, maintenance_rate)
            changed_keys.append((position.venue_id, position.symbol))
        for choice in choices.payloads():
            tracker.set_leverage(choice.venue_id, choice.symbol, choice.leverage)
            changed_keys.append((choice.venue_id, choice.symbol))
        for trade in levels_in(trades.payloads()):
                tracker.observe_price(
                    trade.venue_id, trade.symbol, trade.price, trade.observed_at_ns
                )
        return tuple(changed_keys)

    def tick() -> None:
        changed_keys = read_inputs(tracker)
        now = _time.monotonic()
        if now - last_compute[0] >= context.health_interval_seconds:
            computed = tracker.compute_all()
            last_compute[0] = now
        else:
            # Not due for the full sweep -- but a key that just changed cannot
            # wait for one. compute() returns None for a key with nothing
            # tracked (closed to flat since), which correctly publishes
            # nothing rather than a stale reading for a position that is gone.
            seen: set[tuple[str, str]] = set()
            computed = []
            for key in changed_keys:
                if key in seen:
                    continue
                seen.add(key)
                one = tracker.compute(*key)
                if one is not None:
                    computed.append(one)
        if computed:
            publish_liquidations(tuple(computed))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_liquidations(tracker),
    )
