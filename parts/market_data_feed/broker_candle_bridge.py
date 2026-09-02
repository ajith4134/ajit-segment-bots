"""broker-candle-bridge: republishes a broker's own OHLC bars as candle --
the crypto-era type kline-window-builder reads, so the real conviction
models (kronos-forecaster, bull/bear-conviction-model) train on real Indian
bars the same way they trained on Binance/Bybit klines.

Upstox states neither a per-bar turnover figure nor a trade count, and
carries no closed/live flag on any OHLC entry at all (verified against the
committed .proto -- runtime/brokers/upstox.py's own `_read_ohlc` comment).
Both crypto venues state all three directly (Binance's `q`/`n`/`k.x`,
Bybit's `confirm`); Upstox genuinely does not, so this bridge does not
guess two of the three and infers the third from the one fact Upstox does
give -- how much wall-clock time has actually passed:

- **quote_volume**: Upstox has no per-bar turnover field, so close * volume
  is a stated approximation (RL-061) -- the actual sum of price*quantity a
  turnover-reporting venue would give is not recoverable from an OHLC bar
  alone, and this bridge never claims otherwise.
- **trades**: None, not a fabricated 0 -- the same shape
  `NormalisedCandle.trades` already carries for Bybit, whose kline stream
  also has no count (see that field's own docstring). A 0 would read as "a
  minute in which nothing traded," which is not what "the venue didn't say"
  means.
- **is_closed**: Upstox restates the forming bar on every tick, the same
  behaviour as Binance and Bybit, but sends no flag saying which update is
  final. Binance/Bybit's flag is carried from the venue; here it is the one
  thing this bridge computes rather than reads -- a bar is closed once wall
  time has passed its own interval past its open, which is the only fact
  available when the venue itself does not say. `interval` codes are
  Upstox's own documented ones (upstox.com/developer/api-documentation/v3/
  get-market-data-feed, fetched 2026-09-02): "I1" for 1minute, "1d" for
  daily -- generalised to `I<n>` = n minutes, `<n>d` = n days, matching the
  doc's own phrasing ("1d for daily, I1 for 1minute"). An interval outside
  that pattern is refused, never guessed.
"""

from __future__ import annotations

import re

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.venues.venue_adapter import NormalisedCandle

PART_ID = "broker-candle-bridge"
UPSTOX_VENUE_ID = "upstox"

PART_DECLARATION = PartDeclaration(
    part_id="broker-candle-bridge",
    consumes=("broker-instrument-listing", "broker-candle"),
    produces=("candle", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="delays",
)

MINUTE_INTERVAL = re.compile(r"^I(\d+)$")
DAY_INTERVAL = re.compile(r"^(\d+)d$")
NANOSECONDS_PER_SECOND = 1_000_000_000
MILLISECONDS_TO_NANOSECONDS = 1_000_000
SECONDS_PER_MINUTE = 60
SECONDS_PER_DAY = 86_400


def interval_duration_ns(interval: str) -> int | None:
    """Upstox's own two documented interval shapes, generalised from their
    stated examples -- never a guessed enum member. None for anything else."""
    minute_match = MINUTE_INTERVAL.match(interval)
    if minute_match:
        return int(minute_match.group(1)) * SECONDS_PER_MINUTE * NANOSECONDS_PER_SECOND
    day_match = DAY_INTERVAL.match(interval)
    if day_match:
        return int(day_match.group(1)) * SECONDS_PER_DAY * NANOSECONDS_PER_SECOND
    return None


class BrokerCandleBridge:
    """Resolves an instrument's own trading_symbol and republishes its OHLC bar."""

    def __init__(self) -> None:
        self._trading_symbol_by_key: dict[str, str] = {}

    def observe_listing(self, listing) -> None:
        self._trading_symbol_by_key[listing.instrument_key] = listing.trading_symbol

    def candle_for(self, bar, now_ns: int) -> NormalisedCandle | None:
        """One OHLC bar, as candle -- or None if unresolved or the interval
        code is not one of Upstox's documented shapes."""
        symbol = self._trading_symbol_by_key.get(bar.instrument_key)
        if symbol is None:
            return None
        duration_ns = interval_duration_ns(bar.interval)
        if duration_ns is None:
            return None
        open_time_ns = bar.bar_time_ms * MILLISECONDS_TO_NANOSECONDS
        close_time_ns = open_time_ns + duration_ns
        return NormalisedCandle(
            venue_id=UPSTOX_VENUE_ID, symbol=symbol, interval=bar.interval,
            open_time_ns=open_time_ns, close_time_ns=close_time_ns,
            open=bar.open, high=bar.high, low=bar.low, close=bar.close,
            volume=bar.volume, quote_volume=bar.close * bar.volume, trades=None,
            is_closed=now_ns >= close_time_ns,
            # Upstox's OHLC entry carries one timestamp only (bar_time_ms) --
            # unlike Binance, which separates the bar's own open time from
            # the event's own arrival time (`t` vs `E`), Upstox gives nothing
            # to distinguish the two, so both reuse the same value.
            venue_time_ns=open_time_ns,
        )


def describe_bridge(bridge: BrokerCandleBridge) -> dict:
    return {
        "part_id": PART_ID,
        "instruments_resolved": len(bridge._trading_symbol_by_key),
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    import time

    from runtime.input_assembly import Batch

    listings = Batch(read=context.bus.reader("broker-instrument-listing"))
    bars = Batch(read=context.bus.reader("broker-candle"))
    publish_candles = context.bus.publisher_for("candle")
    bridge = BrokerCandleBridge()

    def tick() -> None:
        for listing in listings.payloads():
            bridge.observe_listing(listing)
        now_ns = time.time_ns()
        candles = tuple(
            candle
            for bar in bars.payloads()
            if (candle := bridge.candle_for(bar, now_ns)) is not None
        )
        if candles:
            publish_candles(candles)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_bridge(bridge),
    )


__all__ = [
    "BrokerCandleBridge",
    "PART_DECLARATION",
    "PART_ID",
    "UPSTOX_VENUE_ID",
    "describe_bridge",
    "interval_duration_ns",
    "start_part",
]
