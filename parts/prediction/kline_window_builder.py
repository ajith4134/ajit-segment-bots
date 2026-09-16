"""kline-window-builder: candles shaped for whatever model reads them, with holes named.

Separate from the forecaster because the window length is a property of the
model, not of the market (T-6). Kronos-base reads 512 candles and Kronos-mini
reads 2048; swapping one for the other must be a change to one part, and it is
only that if the shaping lives outside the model.

**A gap is named, never closed.** A feed that dropped three minutes leaves a
window whose candles are not consecutive, and a model handed those as though they
were learns that time moves at whatever rate the feed happened to deliver. So the
builder detects missing intervals from the timestamps themselves rather than
trusting the count, and reports each one.

**An open candle is excluded by default.** The last bar of a live stream is
incomplete, and a model trained on closed candles reading a partial one is being
shown a bar that will change after it has been forecast from. That is the single
easiest way to produce a backtest that cannot be reproduced live.

**Aggregation is arithmetic, not resampling.** Building a 5-minute window from
1-minute candles takes the first open, the last close, the extremes and the sums
-- and refuses a group that is missing a minute, because a 5-minute bar built
from four minutes is not a shorter bar, it is a wrong one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.forecast_types import Candle, KlineWindow
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "kline-window-builder"
# The exchange's own day boundary, for deciding which bars sit before a
# corporate action's ex-date. This box runs on UTC, and a UTC midnight would put
# every bar between 00:00 and 05:30 IST on the wrong side of the adjustment.
EXCHANGE_TIMEZONE = "Asia/Kolkata"

PART_DECLARATION = PartDeclaration(
    part_id="kline-window-builder",
    consumes=("candle", "corporate-action"),
    produces=("kline-window", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

INTERVAL_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


@dataclass(frozen=True)
class Gap:
    """A run of intervals the feed never delivered."""

    after_open_time_ns: int
    missing_intervals: int

    def __str__(self) -> str:
        return f"{self.missing_intervals} interval(s) missing after {self.after_open_time_ns}"


@dataclass
class BuilderStanding:
    candles_observed: int = 0
    windows_built: int = 0
    complete_windows: int = 0
    open_candles_excluded: int = 0
    gaps_found: int = 0
    # A window that is simply not full yet. Held apart from a window with a
    # hole in it: one needs time and the other needs the feed looked at, and a
    # board that called the first the second would send someone hunting a
    # missing candle that never existed.
    windows_still_filling: int = 0
    aggregations_refused: int = 0
    symbols_tracked: int = 0
    # Corporate actions this builder has rescaled its own history for. A count
    # that stays at zero through an Indian ex-date is a series with a synthetic
    # gap in it, not a quiet calendar.
    corporate_actions_applied: int = 0
    # Candles taken from the tape to fill a window that started empty, and the
    # symbols whose history was refused for having a hole in it. Counted apart
    # from candles_observed because one arrived live and the other was recorded
    # earlier, and a window is a different claim depending on which it holds.
    candles_seeded_from_history: int = 0
    symbols_seeded: int = 0
    seeds_refused_for_a_gap: int = 0
    by_interval: dict = field(default_factory=dict)


def _ex_date_boundary_ns(ex_date) -> int:
    """Midnight IST on the ex-date, as epoch nanoseconds.

    The exchange's own day boundary, not the machine's: this box runs on UTC,
    and a UTC midnight would put every bar from 05:30 IST on the wrong side.
    """
    import datetime
    import zoneinfo

    midnight = datetime.datetime.combine(
        ex_date, datetime.time(0, 0), tzinfo=zoneinfo.ZoneInfo(EXCHANGE_TIMEZONE)
    )
    return int(midnight.timestamp() * 1_000_000_000)


def _rescaled(candle: Candle, action) -> Candle:
    """One pre-ex bar, restated in post-ex terms. quote_volume is untouched."""
    return Candle(
        open_time_ns=candle.open_time_ns,
        open=candle.open * action.price_factor,
        high=candle.high * action.price_factor,
        low=candle.low * action.price_factor,
        close=candle.close * action.price_factor,
        volume=candle.volume * action.quantity_factor,
        quote_volume=candle.quote_volume,
        trades=candle.trades,
        is_closed=candle.is_closed,
    )


class KlineWindowBuilder:
    """Keeps a bounded run of candles per symbol and shapes windows on request."""

    def __init__(
        self,
        interval: str,
        maximum_window: int,
        include_open_candle: bool = False,
        now_ns=time.time_ns,
    ) -> None:
        if interval not in INTERVAL_SECONDS:
            raise ValueError(
                f"{interval!r} is not an interval this builder can space-check; add it to "
                f"INTERVAL_SECONDS with its length in seconds rather than guessing"
            )
        if maximum_window < 2:
            raise ValueError("a window of one candle has no sequence to model")
        self._interval = interval
        self._interval_ns = INTERVAL_SECONDS[interval] * 1_000_000_000
        self._maximum = maximum_window
        self._include_open = include_open_candle
        self._now_ns = now_ns
        self._candles: dict[tuple[str, str], list] = {}
        # Actions already applied, by (venue, symbol, ex-date, the wording it
        # was read from). Reports are republished on every poll of NSE's file,
        # and applying a 1:1 bonus twice quarters the history -- a worse answer
        # than never applying it, because it looks adjusted.
        self._actions_applied: set[tuple[str, str, object, str]] = set()
        self.standing = BuilderStanding()

    @property
    def interval(self) -> str:
        return self._interval

    @property
    def interval_ns(self) -> int:
        """The interval in nanoseconds, for a caller reading history off the tape."""
        return self._interval_ns

    def candles_for(self, venue_id: str, symbol: str) -> tuple:
        """This symbol's stored run, oldest first."""
        return tuple(self._candles.get((venue_id, symbol), ()))

    def symbols_tracked_keys(self) -> tuple:
        """Every (venue, symbol) this builder holds candles for.

        A corporate action names a symbol and no venue -- NSE publishes one, and
        which venue's feed carried the bars is this part's own bookkeeping.
        """
        return tuple(self._candles)

    def observe_corporate_action(self, venue_id: str, action) -> None:
        """Rescale this symbol's stored candles from before the action's ex-date.

        A 1:1 bonus halves the price. Unadjusted, the ex-date open sits beside
        the previous close at half the value: a -50% bar, the largest move this
        window has ever carried, and every detector downstream fires on an event
        that did not happen.

        Prices are multiplied by `price_factor` and quantities by
        `quantity_factor`. Turnover is left exactly as it was, because the same
        rupees changed hands either side of a bonus -- price falls by the same
        factor the share count rises by, and rescaling it would invent money.

        The boundary is midnight IST on the ex-date: a bar opened before it is
        pre-ex, one opened on or after it is already adjusted by the exchange.
        Applied once per (venue, symbol, ex-date, wording).
        """
        if action.price_factor == 1.0 and action.quantity_factor == 1.0:
            return
        seen = (venue_id, action.symbol, action.ex_date, action.stated_from)
        if seen in self._actions_applied:
            return
        candles = self._candles.get((venue_id, action.symbol))
        if candles is None:
            self._actions_applied.add(seen)
            return
        boundary_ns = _ex_date_boundary_ns(action.ex_date)
        self._candles[(venue_id, action.symbol)] = [
            _rescaled(candle, action) if candle.open_time_ns < boundary_ns else candle
            for candle in candles
        ]
        self._actions_applied.add(seen)
        self.standing.corporate_actions_applied += 1

    def observe_candle(self, venue_id: str, symbol: str, candle: Candle) -> None:
        """One candle. A repeat of the same open time replaces it -- streams revise."""
        self.standing.candles_observed += 1
        key = (venue_id, symbol)
        candles = self._candles.setdefault(key, [])
        if candles and candles[-1].open_time_ns == candle.open_time_ns:
            candles[-1] = candle
        else:
            candles.append(candle)
        del candles[: max(0, len(candles) - self._maximum)]
        self.standing.symbols_tracked = len(self._candles)

    def seed_history(self, venue_id: str, symbol: str, candles) -> int:
        """Put recorded candles in front of what has arrived live, or nothing.

        Prepended and never appended: the stream is the newest and the most
        authoritative, and history is only ever what came before it. A seed is
        kept only while it runs contiguously into the earliest live candle --
        one hole and the whole seed is refused, because a window with a hole in
        it is a different series and the model cannot tell.

        Seeding once per symbol is the caller's business; this counts what it
        was given either way.
        """
        key = (venue_id, symbol)
        live = self._candles.get(key, [])
        if not candles:
            return 0
        usable = [candle for candle in candles if candle.is_closed]
        if live:
            earliest = live[0].open_time_ns
            usable = [candle for candle in usable if candle.open_time_ns < earliest]
            if usable and earliest - usable[-1].open_time_ns != self._interval_ns:
                self.standing.seeds_refused_for_a_gap += 1
                return 0
        if not usable:
            return 0
        self._candles[key] = usable + live
        del self._candles[key][: max(0, len(self._candles[key]) - self._maximum)]
        self.standing.candles_seeded_from_history += len(usable)
        self.standing.symbols_seeded += 1
        self.standing.symbols_tracked = len(self._candles)
        return len(usable)

    def build(self, venue_id: str, symbol: str, length: int) -> KlineWindow:
        """The last `length` candles, with any missing intervals named."""
        if length > self._maximum:
            raise ValueError(
                f"this builder keeps {self._maximum} candles and was asked for {length}; "
                f"returning a shorter window silently would hand the model a series it "
                f"believes is {length} long"
            )
        self.standing.windows_built += 1
        self.standing.by_interval[self._interval] = (
            self.standing.by_interval.get(self._interval, 0) + 1
        )

        candles = list(self._candles.get((venue_id, symbol), []))
        if not self._include_open:
            closed = [candle for candle in candles if candle.is_closed]
            self.standing.open_candles_excluded += len(candles) - len(closed)
            candles = closed

        candles = candles[-length:]
        gaps = self._gaps_in(candles)
        self.standing.gaps_found += len(gaps)

        window = KlineWindow(
            venue_id=venue_id,
            symbol=symbol,
            interval=self._interval,
            candles=tuple(candles),
            length_requested=length,
            gaps=tuple(str(gap) for gap in gaps),
            built_at_ns=self._now_ns(),
        )
        if window.is_complete:
            self.standing.complete_windows += 1
        elif not gaps:
            # Short, not holed. 64 one-minute candles is 64 minutes of running,
            # and for the first hour after a start every window is legitimately
            # incomplete with nothing wrong anywhere.
            self.standing.windows_still_filling += 1
        return window

    def aggregate(self, venue_id: str, symbol: str, group_size: int, length: int) -> KlineWindow:
        """Build a longer interval from the one this builder holds.

        A group missing a candle is refused rather than built from what arrived:
        a 5-minute bar assembled from four minutes is not a shorter bar, it is a
        wrong one, and nothing downstream could tell.
        """
        if group_size < 2:
            raise ValueError("aggregating by one is not aggregation")
        source = self.build(venue_id, symbol, min(self._maximum, group_size * length))
        aggregated = []
        candles = list(source.candles)

        for start in range(0, len(candles) - group_size + 1, group_size):
            group = candles[start : start + group_size]
            expected = group[0].open_time_ns + self._interval_ns * (group_size - 1)
            if group[-1].open_time_ns != expected:
                self.standing.aggregations_refused += 1
                continue
            aggregated.append(
                Candle(
                    open_time_ns=group[0].open_time_ns,
                    open=group[0].open,
                    high=max(candle.high for candle in group),
                    low=min(candle.low for candle in group),
                    close=group[-1].close,
                    volume=sum(candle.volume for candle in group),
                    quote_volume=sum(candle.quote_volume for candle in group),
                    trades=sum(candle.trades for candle in group),
                    is_closed=all(candle.is_closed for candle in group),
                )
            )

        aggregated = aggregated[-length:]
        return KlineWindow(
            venue_id=venue_id,
            symbol=symbol,
            interval=f"{group_size}x{self._interval}",
            candles=tuple(aggregated),
            length_requested=length,
            gaps=source.gaps,
            built_at_ns=self._now_ns(),
        )

    def _gaps_in(self, candles) -> list:
        """Missing intervals, found from the timestamps rather than from the count."""
        gaps = []
        for earlier, later in zip(candles, candles[1:]):
            spacing = later.open_time_ns - earlier.open_time_ns
            if spacing > self._interval_ns:
                missing = spacing // self._interval_ns - 1
                if missing > 0:
                    gaps.append(Gap(earlier.open_time_ns, int(missing)))
        return gaps

    def release(self, venue_id: str, symbol: str) -> None:
        """Drop a symbol's candles. T-3: an off part releases its memory."""
        self._candles.pop((venue_id, symbol), None)
        self.standing.symbols_tracked = len(self._candles)


def describe_window_building(builder: KlineWindowBuilder) -> dict:
    return {
        "part_id": PART_ID,
        "interval": builder.interval,
        "candles_observed": builder.standing.candles_observed,
        "windows_built": builder.standing.windows_built,
        "complete_windows": builder.standing.complete_windows,
        # Only windows that actually have a hole. This used to be every window
        # that was not complete, so on 2026-08-25 it read 300 windows with a gap
        # while gaps_found was 0 -- the windows were six minutes into needing
        # sixty-four, and nothing was missing at all.
        "windows_with_a_gap": (
            builder.standing.windows_built
            - builder.standing.complete_windows
            - builder.standing.windows_still_filling
        ),
        "windows_still_filling": builder.standing.windows_still_filling,
        "candles_seeded_from_history": builder.standing.candles_seeded_from_history,
        "symbols_seeded": builder.standing.symbols_seeded,
        "seeds_refused_for_a_gap": builder.standing.seeds_refused_for_a_gap,
        "open_candles_excluded": builder.standing.open_candles_excluded,
        "gaps_found": builder.standing.gaps_found,
        "aggregations_refused_for_a_missing_candle": builder.standing.aggregations_refused,
        "symbols_tracked": builder.standing.symbols_tracked,
    }


def run_kline_window_builder(
    builder: KlineWindowBuilder, control_socket, read_candles, publish_windows,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        requests = read_candles(builder)
        publish_windows(
            tuple(builder.build(venue_id, symbol, length) for venue_id, symbol, length in requests)
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_window_building(builder),
    )


def build_kline_window_builder_from_settings(settings, now_ns=time.time_ns) -> KlineWindowBuilder:
    """The window builder as the live part builds it, from the settings the part reads.

    Shared with the detector-edge measurement (operate/detectors_for_measurement.py) so an
    offline run builds exactly what the spine builds: a second copy of these arguments
    would drift the first time a setting is renamed.
    """
    return KlineWindowBuilder(
        interval=str(settings.setting("candle_interval").value),
        maximum_window=int(settings.number("kline_maximum_window")),
        include_open_candle=bool(settings.setting("kline_include_open_candle").value),
        now_ns=now_ns,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Candles arrive on `candle` from the candle reader with the venue's
    closed flag; a window is rebuilt and published for a symbol when a
    closed candle lands on it.

    **The first candle for a symbol brings its history with it.** This part
    starts every process holding nothing, so without a seed the first full window
    is a full window's worth of wall-clock minutes away -- 64 of them on
    2026-08-26, during which kronos-forecaster refused every window it was asked
    about. The history is read from the tape rather than from a venue endpoint,
    which is `historical-bar-store`'s rule and its reason: an endpoint returns the
    venue's current view of the past, and the tape is what arrived. It is read
    once per symbol, on that symbol's first live candle, so the cost is spread
    across the first minute instead of landing at start.
    """
    import pathlib

    from runtime.candle_history import (
        closed_broker_candles_on_the_tape, closed_candles_on_the_tape,
    )
    from runtime.forecast_types import Candle
    from runtime.input_assembly import Batch
    from runtime.venues.adapter_registry import load_venue_adapter
    from runtime.venues.venue_adapter import NormalisedCandle

    # A data value carried on the wire, not a reference to another part
    # (T-4): the broker path's tape is shaped differently from a crypto
    # venue's own (runtime/candle_history.py's closed_broker_candles_on_the_
    # tape docstring), so this is the one venue id this part branches on
    # rather than resolving through the crypto-only venue-adapter registry.
    UPSTOX_VENUE_ID = "upstox"

    updates = Batch(read=context.bus.reader("candle"))
    # An event, not a level: an action happens once and is applied once. The
    # builder itself refuses a repeat, because NSE's file republishes the same
    # action on every poll.
    actions = Batch(read=context.bus.reader("corporate-action"))
    publish_windows = context.bus.publisher_for("kline-window")
    builder = build_kline_window_builder_from_settings(context)
    length = int(context.number("kline_window_length"))
    tape_root = pathlib.Path(str(context.setting("tape_root").value)).expanduser()
    seeded: set[tuple[str, str]] = set()
    adapters: dict[str, object] = {}

    def seed_once(venue_id: str, symbol: str) -> None:
        """History for a symbol the first time it says anything, and never again."""
        key = (venue_id, symbol)
        if key in seeded:
            return
        seeded.add(key)
        if venue_id == UPSTOX_VENUE_ID:
            history = closed_broker_candles_on_the_tape(
                tape_root=tape_root,
                venue_id=venue_id,
                symbol=symbol,
                wanted_interval=str(context.setting("upstox_candle_interval").value),
                interval_ns=builder.interval_ns,
                wanted=int(context.number("kline_maximum_window")),
            )
            builder.seed_history(venue_id, symbol, history)
            return
        adapter = adapters.get(venue_id)
        if adapter is None:
            adapter = load_venue_adapter(venue_id)
            adapters[venue_id] = adapter
        history = closed_candles_on_the_tape(
            tape_root=tape_root,
            venue_id=venue_id,
            symbol=symbol,
            interval_ns=builder.interval_ns,
            wanted=int(context.number("kline_maximum_window")),
            read_candles=adapter.read_candles,
        )
        builder.seed_history(
            venue_id,
            symbol,
            tuple(
                Candle(
                    open_time_ns=candle.open_time_ns, open=candle.open, high=candle.high,
                    low=candle.low, close=candle.close, volume=candle.volume,
                    quote_volume=candle.quote_volume, trades=candle.trades or 0,
                    is_closed=True,
                )
                for candle in history
            ),
        )

    def read_candles(_builder):
        touched = set()
        # Before the candles, so a bar arriving in the same tick as the action
        # is not rescaled by an adjustment the exchange already made to it.
        for action in actions.payloads():
            for venue_id, symbol in list(builder.symbols_tracked_keys()):
                if symbol == action.symbol:
                    builder.observe_corporate_action(venue_id, action)
        for update in updates.payloads():
            if not isinstance(update, NormalisedCandle):
                continue
            seed_once(update.venue_id, update.symbol)
            builder.observe_candle(
                update.venue_id, update.symbol,
                Candle(
                    open_time_ns=update.open_time_ns, open=update.open, high=update.high, low=update.low,
                    close=update.close, volume=update.volume, quote_volume=update.quote_volume,
                    trades=update.trades or 0, is_closed=update.is_closed,
                ),
            )
            if update.is_closed:
                touched.add((update.venue_id, update.symbol))
        return tuple((venue_id, symbol, length) for venue_id, symbol in sorted(touched))

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_windows(kept)

    return run_kline_window_builder(
        builder=builder,
        control_socket=context.control_socket,
        read_candles=read_candles,
        publish_windows=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
