"""participation-capped-order-splitter: slice an order so it does not move the market.

An order large against recent volume does not get filled at the price that was
seen -- it *becomes* the price. The slippage is not bad luck, it is the order's
own footprint, and it is paid on the whole size.

So a large order is split into slices, each capped at a fraction of what the
symbol actually trades in the slice interval, and spread over time. The cap is
against **measured recent volume**, not a fixed size, because the same order is
invisible in BTCUSDT and enormous in a thin altcoin.

Two shapes of harm this avoids, and it can only trade one against the other:

- **Slicing too coarsely** pays market impact on every slice.
- **Slicing too finely** stretches the order over so long that the reason for
  placing it has passed -- and an order still working when its signal has died is
  a position taken for a reason that no longer exists.

The schedule therefore has a hard horizon: if the participation cap cannot fill
the order inside it, the splitter says so rather than quietly producing a
schedule that runs past the signal's life.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "participation-capped-order-splitter"

PART_DECLARATION = PartDeclaration(
    part_id="participation-capped-order-splitter",
    consumes=("bounded-order", "volatility-forecast", "order-book-snapshot"),
    produces=("execution-schedule", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

SINGLE_SLICE = "single-slice"
SPLIT = "split"
REFUSED_NO_VOLUME = "refused-no-volume-measurement"
REFUSED_HORIZON_EXCEEDED = "refused-cannot-fill-within-the-horizon"


@dataclass(frozen=True)
class ExecutionSlice:
    """One slice of an order, and when it should go."""

    sequence: int
    quantity: float
    at_second: float
    is_final: bool


@dataclass(frozen=True)
class ExecutionSchedule:
    """How an order should be worked, or why it cannot be."""

    venue_id: str
    symbol: str
    side: str
    total_quantity: float
    slices: tuple[ExecutionSlice, ...]
    outcome: str
    participation_cap: float
    measured_volume_per_interval: float | None
    horizon_seconds: float
    reason: str
    scheduled_at_ns: int

    @property
    def is_workable(self) -> bool:
        return self.outcome in (SINGLE_SLICE, SPLIT) and bool(self.slices)


@dataclass
class SplitterStanding:
    schedules: int = 0
    single_slice: int = 0
    split: int = 0
    refused_no_volume: int = 0
    refused_horizon: int = 0
    largest_slice_count: int = 0
    symbols_measured: int = 0


class ParticipationCappedOrderSplitter:
    """Splits an order into slices capped against the volume the symbol actually trades."""

    def __init__(
        self,
        participation_cap: float,
        slice_interval_seconds: float,
        maximum_horizon_seconds: float,
        volatility_urgency_factor: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < participation_cap <= 1.0:
            raise ValueError("the participation cap must be a fraction of traded volume in (0, 1]")
        if slice_interval_seconds <= 0:
            raise ValueError("slices need an interval between them")
        self._cap = participation_cap
        self._interval = slice_interval_seconds
        self._horizon = maximum_horizon_seconds
        self._urgency = volatility_urgency_factor
        self._now_ns = now_ns
        self._volume_per_interval: dict[tuple[str, str], float] = {}
        self.standing = SplitterStanding()

    def observe_traded_volume(
        self, venue_id: str, symbol: str, quantity_traded: float, over_seconds: float
    ) -> None:
        """What this symbol actually trades, normalised to one slice interval."""
        if over_seconds <= 0:
            return
        per_interval = quantity_traded * (self._interval / over_seconds)
        self._volume_per_interval[(venue_id, symbol)] = per_interval
        self.standing.symbols_measured = len(self._volume_per_interval)

    def split(
        self,
        venue_id: str,
        symbol: str,
        side: str,
        quantity: float,
        volatility_forecast: float | None = None,
    ) -> ExecutionSchedule:
        self.standing.schedules += 1
        key = (venue_id, symbol)
        measured = self._volume_per_interval.get(key)

        if measured is None or measured <= 0:
            # Without a volume measurement there is no way to know whether this
            # order is a drop or a flood. Guessing in either direction is a
            # decision about market impact made with no information.
            self.standing.refused_no_volume += 1
            return self._schedule(
                venue_id, symbol, side, quantity, (), REFUSED_NO_VOLUME, None,
                f"no traded volume has been measured for {symbol}; the participation cap "
                f"cannot be applied to an unknown denominator",
            )

        # Volatility raises urgency: in a fast market the cost of being slow
        # exceeds the cost of impact, so a larger share is taken per slice.
        cap = self._cap
        if volatility_forecast:
            cap = min(1.0, self._cap * (1.0 + volatility_forecast * self._urgency))

        per_slice = measured * cap
        if quantity <= per_slice:
            self.standing.single_slice += 1
            return self._schedule(
                venue_id, symbol, side, quantity,
                (ExecutionSlice(1, quantity, 0.0, True),), SINGLE_SLICE, measured,
                f"{quantity:g} is inside {cap:.0%} of the {measured:g} traded per "
                f"{self._interval:g}s; no impact worth splitting for",
            )

        slice_count = math.ceil(quantity / per_slice)
        span = (slice_count - 1) * self._interval
        if span > self._horizon:
            self.standing.refused_horizon += 1
            return self._schedule(
                venue_id, symbol, side, quantity, (), REFUSED_HORIZON_EXCEEDED, measured,
                f"filling {quantity:g} at {cap:.0%} participation needs {slice_count} slices over "
                f"{span:.0f}s, past the {self._horizon:.0f}s horizon; an order still working "
                f"after its signal has died is a position taken for a reason that has gone",
            )

        slices = []
        remaining = quantity
        for index in range(slice_count):
            size = min(per_slice, remaining)
            remaining -= size
            slices.append(
                ExecutionSlice(
                    sequence=index + 1,
                    quantity=round(size, 12),
                    at_second=index * self._interval,
                    is_final=remaining <= 0,
                )
            )
        self.standing.split += 1
        self.standing.largest_slice_count = max(self.standing.largest_slice_count, len(slices))

        return self._schedule(
            venue_id, symbol, side, quantity, tuple(slices), SPLIT, measured,
            f"{len(slices)} slices of at most {per_slice:g} over {span:.0f}s, "
            f"{cap:.0%} of the {measured:g} traded per {self._interval:g}s",
        )

    def _schedule(
        self, venue_id, symbol, side, quantity, slices, outcome, measured, reason
    ) -> ExecutionSchedule:
        return ExecutionSchedule(
            venue_id=venue_id, symbol=symbol, side=side, total_quantity=quantity,
            slices=slices, outcome=outcome, participation_cap=self._cap,
            measured_volume_per_interval=measured, horizon_seconds=self._horizon,
            reason=reason, scheduled_at_ns=self._now_ns(),
        )


def describe_splitting(splitter: ParticipationCappedOrderSplitter) -> dict:
    return {
        "part_id": PART_ID,
        "schedules": splitter.standing.schedules,
        "single_slice": splitter.standing.single_slice,
        "split": splitter.standing.split,
        "refused_no_volume": splitter.standing.refused_no_volume,
        "refused_horizon_exceeded": splitter.standing.refused_horizon,
        "largest_slice_count": splitter.standing.largest_slice_count,
        "symbols_measured": splitter.standing.symbols_measured,
    }


def run_participation_capped_order_splitter(
    splitter: ParticipationCappedOrderSplitter, control_socket, read_orders, publish_schedules,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        volumes, orders = read_orders()
        for volume in volumes:
            splitter.observe_traded_volume(**volume)
        publish_schedules(tuple(splitter.split(**order) for order in orders))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
