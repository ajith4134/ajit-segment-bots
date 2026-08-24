"""fill-volume-capper: you cannot trade more than traded.

The most flattering assumption available in a backtest is that the intended size
fills regardless of what actually printed. In a liquid major it is nearly harmless;
in a thin symbol it is the entire result. A strategy showing 400% a year on an
altcoin has usually been filling ten times the volume that existed at prices nobody
could have got.

The cap is a participation limit: a fraction of the volume that traded in the bar.
It is a fraction rather than the whole because being the entire volume of a bar is
not a fill, it is a market event -- everyone else's orders are also in that volume,
and taking all of it means moving through every level in the book.

Three consequences that a naive backtest gets wrong in the same direction:

- **A capped fill is a partial position, not a skipped trade.** The strategy would
  have got some of it, and pretending otherwise understates rather than overstates.
  Both errors are wrong; only one of them is flattering.
- **The cap compounds across a sequence.** Scaling into a position over five bars is
  capped bar by bar, and the total is what could be accumulated rather than what was
  wanted.
- **Zero volume means no fill at all.** A bar with no trades is a bar where nothing
  could be bought at any price, and a backtest that fills at its close is trading
  against nobody.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.backtest_types import FillableSize
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "fill-volume-capper"

PART_DECLARATION = PartDeclaration(
    part_id="fill-volume-capper",
    consumes=("historical-window",),
    produces=("fillable-size", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FILLED = "the-whole-intended-size-was-available"
CAPPED = "only-part-of-the-intended-size-could-have-traded"
NO_VOLUME = "nothing-traded-in-this-bar"
NO_BAR = "no-bar-covers-this-moment"


@dataclass(frozen=True)
class CapOutcome:
    venue_id: str
    symbol: str
    state: str
    size: FillableSize | None
    reason: str
    capped_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.size is not None


@dataclass
class CapperStanding:
    requests: int = 0
    filled_in_full: int = 0
    capped: int = 0
    refused_no_volume: int = 0
    refused_no_bar: int = 0
    total_intended: float = 0.0
    total_fillable: float = 0.0
    largest_cap_fraction: float = 0.0


class FillVolumeCapper:
    """Limits a simulated fill to a fraction of the volume that actually traded."""

    def __init__(self, participation_cap: float, now_ns=time.time_ns) -> None:
        if not 0.0 < participation_cap <= 1.0:
            raise ValueError(
                "the cap is a fraction of the bar's volume; taking all of it is not a "
                "fill, it is a market event"
            )
        if participation_cap == 1.0:
            raise ValueError(
                "being the entire volume of a bar means moving through every level in "
                "the book, which no cost model here can price"
            )
        self._participation_cap = participation_cap
        self._now_ns = now_ns
        self._volumes: dict[tuple, float] = {}
        self.standing = CapperStanding()

    def observe_bar(self, venue_id: str, symbol: str, at_ns: int, volume: float) -> None:
        self._volumes[(venue_id, symbol, at_ns)] = volume

    def cap(self, venue_id: str, symbol: str, at_ns: int, intended: float) -> CapOutcome:
        self.standing.requests += 1
        key = (venue_id, symbol, at_ns)
        if key not in self._volumes:
            self.standing.refused_no_bar += 1
            return self._outcome(
                venue_id, symbol, NO_BAR, None,
                "no bar covers this moment, so there is no volume to fill against",
            )

        volume = self._volumes[key]
        if volume <= 0:
            self.standing.refused_no_volume += 1
            return self._outcome(
                venue_id, symbol, NO_VOLUME,
                FillableSize(
                    venue_id=venue_id, symbol=symbol, at_ns=at_ns, intended=intended,
                    fillable=0.0, volume_in_bar=0.0,
                    participation_cap=self._participation_cap,
                    reason=(
                        "nothing traded in this bar. A backtest filling at its close is "
                        "trading against nobody"
                    ),
                ),
                "nothing traded",
            )

        available = volume * self._participation_cap
        fillable = min(intended, available)
        self.standing.total_intended += intended
        self.standing.total_fillable += fillable

        if fillable < intended:
            self.standing.capped += 1
            fraction = 1.0 - fillable / intended if intended > 0 else 0.0
            self.standing.largest_cap_fraction = max(
                self.standing.largest_cap_fraction, fraction
            )
            state, reason = CAPPED, (
                f"{fillable:.6f} of {intended:.6f} could have traded at "
                f"{self._participation_cap:.0%} of the bar's {volume:.6f} volume. The rest "
                f"is a partial position rather than a skipped trade -- everyone else's "
                f"orders are in that volume too"
            )
        else:
            self.standing.filled_in_full += 1
            state, reason = FILLED, (
                f"{intended:.6f} is within {self._participation_cap:.0%} of the bar's "
                f"{volume:.6f} volume"
            )

        return self._outcome(
            venue_id, symbol, state,
            FillableSize(
                venue_id=venue_id, symbol=symbol, at_ns=at_ns, intended=intended,
                fillable=fillable, volume_in_bar=volume,
                participation_cap=self._participation_cap, reason=reason,
            ),
            reason,
        )

    def cap_a_sequence(self, venue_id: str, symbol: str, requests) -> tuple:
        """Scaling in is capped bar by bar; the total is what could be accumulated."""
        results = []
        remaining = None
        for at_ns, intended in requests:
            wanted = intended if remaining is None else remaining
            outcome = self.cap(venue_id, symbol, at_ns, wanted)
            results.append(outcome)
            if outcome.size is not None:
                remaining = max(wanted - outcome.size.fillable, 0.0)
            else:
                remaining = wanted
        return tuple(results)

    def _outcome(self, venue_id, symbol, state, size, reason) -> CapOutcome:
        return CapOutcome(
            venue_id=venue_id, symbol=symbol, state=state, size=size, reason=reason,
            capped_at_ns=self._now_ns(),
        )


def describe_capping(capper: FillVolumeCapper) -> dict:
    return {
        "part_id": PART_ID,
        "requests": capper.standing.requests,
        "filled_in_full": capper.standing.filled_in_full,
        "capped": capper.standing.capped,
        "refused_no_volume": capper.standing.refused_no_volume,
        "refused_no_bar": capper.standing.refused_no_bar,
        "total_intended": capper.standing.total_intended,
        "total_fillable": capper.standing.total_fillable,
        "largest_cap_fraction": capper.standing.largest_cap_fraction,
        "participation_cap": capper._participation_cap,
        "fills_the_intended_size_regardless_of_volume": False,
    }


def run_fill_volume_capper(
    capper: FillVolumeCapper, control_socket, read_bars, read_requests, publish_sizes,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for venue_id, symbol, at_ns, volume in read_bars():
            capper.observe_bar(venue_id, symbol, at_ns, volume)
        for venue_id, symbol, at_ns, intended in read_requests():
            outcome = capper.cap(venue_id, symbol, at_ns, intended)
            if outcome.is_usable:
                publish_sizes(outcome.size)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_capping(capper),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Every bar of a window is volume the capper knows; a fillable size is
    published per bar at the replay quantity, so the replayer has a cap for
    each bar it may fill in.
    """
    from runtime.input_assembly import Batch

    windows = Batch(read=context.bus.reader("historical-window"))
    publish_sizes = context.bus.publisher_for("fillable-size")
    capper = FillVolumeCapper(participation_cap=context.number("order_participation_cap"))
    quantity = context.number("replay_quantity")
    pending: list = []

    def read_bars():
        bars = []
        for window in windows.payloads():
            for bar in window.bars:
                bars.append((window.venue_id, window.symbol, bar.at_ns, bar.volume))
                pending.append((window.venue_id, window.symbol, bar.at_ns, quantity))
        return tuple(bars)

    def read_requests():
        requests = tuple(pending)
        pending.clear()
        return requests

    def publish(item) -> None:
        if item is not None:
            publish_sizes((item,))

    return run_fill_volume_capper(
        capper=capper,
        control_socket=context.control_socket,
        read_bars=read_bars,
        read_requests=read_requests,
        publish_sizes=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
