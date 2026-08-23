"""historical-bar-store: recorded market data, with its holes visible.

Every backtest in this system reads from here, so the store's failure modes become
everyone's. There is exactly one that matters and it is silent: a gap in the data
that gets filled in.

A missing hour is usually a missing hour of *something* -- an outage, a halt, a
listing gap, a crash that took the feed down. Interpolating across it produces a
smooth price series through a period where the market was doing the most interesting
thing it ever did, and a backtest over that series will show a strategy trading
calmly through a crash it never saw. So this store never interpolates. It reports the
gap, refuses to claim the window is backtestable, and leaves the decision about what
to do to whoever asked.

Three further properties:

- **Bars are built from the recorded tape, not fetched from an endpoint.** A venue's
  historical endpoint returns its current view of the past, which quietly differs
  from what was actually broadcast at the time -- corrections, late trades,
  re-labelled candles. The tape is what arrived.
- **The interval is exact.** A store that accepts approximately-one-minute bars
  produces a series where the spacing varies and every time-based feature is
  slightly wrong in a way nothing surfaces.
- **A window states its own completeness.** Reading it is how a consumer knows
  whether the number it computes means anything.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.backtest_types import HistoricalWindow
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "historical-bar-store"

PART_DECLARATION = PartDeclaration(
    part_id="historical-bar-store",
    consumes=("market-data",),
    produces=("historical-window", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

BUILT = "built"
HAS_GAPS = "the-window-has-holes-and-they-are-not-filled-in"
NOTHING_RECORDED = "no-bar-has-been-recorded-for-this-range"
BAD_INTERVAL = "a-bar-does-not-sit-on-the-declared-interval"


@dataclass(frozen=True)
class Bar:
    """One interval of the tape, as it arrived."""

    at_ns: int
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float
    trades: int

    @property
    def is_coherent(self) -> bool:
        return (
            self.low_price <= min(self.open_price, self.close_price)
            and self.high_price >= max(self.open_price, self.close_price)
        )


@dataclass(frozen=True)
class WindowOutcome:
    venue_id: str
    symbol: str
    state: str
    window: HistoricalWindow | None
    reason: str
    built_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.window is not None


@dataclass
class StoreStanding:
    bars_stored: int = 0
    windows_built: int = 0
    windows_with_gaps: int = 0
    bars_rejected_off_interval: int = 0
    bars_rejected_incoherent: int = 0
    duplicate_bars: int = 0
    interpolations_performed: int = 0
    largest_gap_bars: int = 0


class HistoricalBarStore:
    """Stores bars from the recorded tape and hands out windows that name their holes."""

    def __init__(self, interval_seconds: float, now_ns=time.time_ns) -> None:
        if interval_seconds <= 0:
            raise ValueError(
                "a store that accepts approximately-spaced bars makes every time-based "
                "feature slightly wrong in a way nothing surfaces"
            )
        self._interval_seconds = interval_seconds
        self._interval_ns = int(interval_seconds * 1e9)
        self._now_ns = now_ns
        self._bars: dict[tuple, dict] = {}
        self._sequence = 0
        self.standing = StoreStanding()

    def store(self, venue_id: str, symbol: str, bar: Bar) -> bool:
        """Returns whether the bar was admitted. Off-interval bars are refused."""
        if bar.at_ns % self._interval_ns != 0:
            self.standing.bars_rejected_off_interval += 1
            return False
        if not bar.is_coherent:
            self.standing.bars_rejected_incoherent += 1
            return False
        key = (venue_id, symbol)
        bars = self._bars.setdefault(key, {})
        if bar.at_ns in bars:
            self.standing.duplicate_bars += 1
            return False
        bars[bar.at_ns] = bar
        self.standing.bars_stored += 1
        return True

    def window(self, venue_id: str, symbol: str, from_ns: int, to_ns: int) -> WindowOutcome:
        bars = self._bars.get((venue_id, symbol), {})
        wanted = [
            stamp
            for stamp in range(
                from_ns - from_ns % self._interval_ns, to_ns + 1, self._interval_ns
            )
        ]
        present = [bars[stamp] for stamp in wanted if stamp in bars]

        if not present:
            return self._outcome(
                venue_id, symbol, NOTHING_RECORDED, None,
                "no bar has been recorded for this range. That is a fact about the tape, "
                "and it is not the same as the market being flat",
            )

        missing = [stamp for stamp in wanted if stamp not in bars]
        gaps = self._gap_spans(missing)
        if gaps:
            self.standing.windows_with_gaps += 1
            self.standing.largest_gap_bars = max(
                self.standing.largest_gap_bars,
                max(len(span) for span in gaps),
            )

        self._sequence += 1
        window = HistoricalWindow(
            window_id=f"window-{self._sequence}",
            venue_id=venue_id,
            symbol=symbol,
            interval_seconds=self._interval_seconds,
            bars=tuple(present),
            first_at_ns=present[0].at_ns,
            last_at_ns=present[-1].at_ns,
            expected_bars=len(wanted),
            missing_bars=len(missing),
            gap_spans=tuple((span[0], span[-1]) for span in gaps),
            was_interpolated=False,
            built_at_ns=self._now_ns(),
        )
        self.standing.windows_built += 1

        if missing:
            return self._outcome(
                venue_id, symbol, HAS_GAPS, window,
                f"{len(missing)} of {len(wanted)} bar(s) are missing across "
                f"{len(gaps)} gap(s). Nothing is interpolated: a smooth series through a "
                f"period the feed missed produces a backtest that traded calmly through a "
                f"crash it never saw",
            )

        return self._outcome(
            venue_id, symbol, BUILT, window,
            f"{len(present)} bar(s) at {self._interval_seconds:.0f}s, complete and "
            f"unmodified from the tape",
        )

    def _gap_spans(self, missing) -> list:
        spans: list = []
        for stamp in sorted(missing):
            if spans and stamp - spans[-1][-1] == self._interval_ns:
                spans[-1].append(stamp)
            else:
                spans.append([stamp])
        return spans

    def _outcome(self, venue_id, symbol, state, window, reason) -> WindowOutcome:
        return WindowOutcome(
            venue_id=venue_id, symbol=symbol, state=state, window=window, reason=reason,
            built_at_ns=self._now_ns(),
        )


def describe_bar_store(store: HistoricalBarStore) -> dict:
    return {
        "part_id": PART_ID,
        "bars_stored": store.standing.bars_stored,
        "windows_built": store.standing.windows_built,
        "windows_with_gaps": store.standing.windows_with_gaps,
        "bars_rejected_off_interval": store.standing.bars_rejected_off_interval,
        "bars_rejected_incoherent": store.standing.bars_rejected_incoherent,
        "duplicate_bars": store.standing.duplicate_bars,
        "largest_gap_in_bars": store.standing.largest_gap_bars,
        "interval_seconds": store._interval_seconds,
        "interpolates_gaps": False,
        "interpolations_performed": store.standing.interpolations_performed,
        "fetches_history_from_a_venue_endpoint": False,
    }


def run_historical_bar_store(
    store: HistoricalBarStore, control_socket, read_bars, read_requests,
    publish_windows, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for venue_id, symbol, bar in read_bars():
            store.store(venue_id, symbol, bar)
        for venue_id, symbol, from_ns, to_ns in read_requests():
            outcome = store.window(venue_id, symbol, from_ns, to_ns)
            if outcome.is_usable:
                publish_windows(outcome.window)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
