"""equity-opportunity-profiler: each ordinary NSE share's own 52-week range,
average volume, average true range and prior close -- the history the day's
cash-equity shortlist is ranked against (`cash-equity-shortlist-ranker`,
docs/proposals/equity-opportunity-profiler.md).

The operator asked (2026-09-05) for the top 50 cash-equity names actually worth
scanning today, picked by momentum, volume, 52-week high/low and more -- never a
fixed list. Momentum and today's volume are already on the wire
(`broker-price-frame`, `candle`); a 52-week range and an average day are not,
because nothing this project runs asks Upstox for more than one day of history
at a time (`broker-history-reader`, which exists for a different job -- filling
the hours the market is shut, RL-071, and deliberately never reaches back a
year). This part is the one that does.

**It knows nothing about segments or F&O exclusion (T-4).** "Ordinary NSE
share" is a fact about one instrument's own listing -- `instrument_type=EQ`,
`security_type=NORMAL`, exactly `EquityWithoutADerivative.admits` in
`broker_symbol_universe_bridge.py`, reused rather than restated (T-6). Whether a
share is *also* an F&O underlying, and therefore not cash-equity-intraday's to
trade, is `broker-symbol-universe-bridge`'s question, asked downstream of this
part's output.

**Rotates like `broker-history-reader`'s own sweep, one window key at a time.**
A window's key includes today's date, so a symbol profiled today is not
re-fetched until tomorrow -- the same trick that reader uses, and the reason
neither part needs a separate "how stale is this" clock of its own.

**Circuit limits are named on the payload and always `None` here.** Neither the
instrument master nor any live feed this project reads carries them (checked
2026-09-05); fetching them is real, additional work, deliberately deferred
(docs/proposals/equity-opportunity-profiler.md, "What this does NOT fix") so the
shape published here does not have to change again once that fetch exists.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from runtime.brokers.upstox import UPSTOX_BROKER_ID
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "equity-opportunity-profiler"

PART_DECLARATION = PartDeclaration(
    part_id="equity-opportunity-profiler",
    consumes=("broker-instrument-listing", "broker-token-standing"),
    produces=("equity-historical-profile", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

DAY_UNIT = "days"
DAY_INTERVAL = 1


@dataclass(frozen=True)
class EquityHistoricalProfile:
    """One ordinary share's own history, as of one fetch.

    `upper_circuit_limit`/`lower_circuit_limit` are always `None` in this build
    -- named so the shortlist ranker's shape does not change when the deferred
    fetch for them lands, not because they were measured and found absent.
    """

    venue_id: str
    symbol: str
    week_52_high: float
    week_52_low: float
    average_daily_volume: float
    average_true_range: float
    previous_close: float
    upper_circuit_limit: float | None
    lower_circuit_limit: float | None
    bars_used: int
    measured_at_ns: int
    source: str


@dataclass(frozen=True)
class ProfileRequest:
    """One symbol's window, as this part asks Upstox for it."""

    instrument_key: str
    symbol: str
    from_date: str
    to_date: str

    @property
    def window_key(self) -> tuple:
        return (self.instrument_key, self.from_date, self.to_date)


@dataclass
class ProfilerStanding:
    candidates_known: int = 0
    requests_planned: int = 0
    requests_refused: int = 0
    profiles_published: int = 0
    skipped_too_few_bars: int = 0


def average_true_range(bars, window_days: int) -> float | None:
    """Wilder's true range, averaged over the trailing `window_days` bars.

    `None` when fewer than two bars exist -- a true range needs a prior close,
    and a window of one bar has none to compare against.
    """
    if len(bars) < 2:
        return None
    true_ranges = []
    for previous, current in zip(bars, bars[1:]):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    trailing = true_ranges[-window_days:] if window_days > 0 else true_ranges
    if not trailing:
        return None
    return sum(trailing) / len(trailing)


class EquityOpportunityProfiler:
    """Decides which ordinary shares still need today's window, and turns an
    answered one into a profile."""

    def __init__(
        self,
        equity_admits,
        days_back: int,
        atr_window_days: int,
        most_symbols_per_sweep: int,
        now_ns=None,
    ) -> None:
        import time

        if days_back < 2:
            raise ValueError(
                f"days_back is {days_back!r}; a 52-week range needs meaningfully more "
                f"than one day of history"
            )
        if most_symbols_per_sweep < 1:
            raise ValueError(
                "a profiler that asks about no symbol publishes nothing while looking "
                f"like a working one; got {most_symbols_per_sweep!r}"
            )
        self._admits = equity_admits
        self._days_back = days_back
        self._atr_window_days = atr_window_days
        self._most_symbols_per_sweep = most_symbols_per_sweep
        self._now_ns = now_ns or time.time_ns
        self._candidate_by_key: dict[str, str] = {}
        self._windows_read: set[tuple] = set()
        self.standing = ProfilerStanding()

    def observe_listings(self, listings) -> None:
        for listing in listings:
            if not self._admits(listing):
                continue
            self._candidate_by_key[listing.instrument_key] = listing.trading_symbol
        self.standing.candidates_known = len(self._candidate_by_key)

    def requests_due(self, today: datetime.date) -> tuple[ProfileRequest, ...]:
        """The windows worth asking for right now.

        Sorted by instrument key rather than by anything measured, because
        nothing about opportunity is known yet for a symbol that has never been
        profiled -- that is exactly what this sweep exists to learn. Once every
        candidate has a window for today, the sweep plans nothing further until
        tomorrow's date changes every window key at once.
        """
        requests = []
        to_date = today.isoformat()
        from_date = (today - datetime.timedelta(days=self._days_back)).isoformat()
        for key in sorted(self._candidate_by_key):
            if len(requests) >= self._most_symbols_per_sweep:
                break
            request = ProfileRequest(
                instrument_key=key, symbol=self._candidate_by_key[key],
                from_date=from_date, to_date=to_date,
            )
            if request.window_key in self._windows_read:
                continue
            requests.append(request)
        self.standing.requests_planned += len(requests)
        return tuple(requests)

    def observe_window(self, request: ProfileRequest, response) -> EquityHistoricalProfile | None:
        """One answered window, turned into a profile -- or None, named why."""
        from runtime.brokers.upstox import UpstoxAdapter

        try:
            bars = UpstoxAdapter().read_historical_candles(
                request.instrument_key, DAY_UNIT, DAY_INTERVAL, response
            )
        except ValueError:
            self.standing.requests_refused += 1
            return None

        self._windows_read.add(request.window_key)
        if len(bars) < 2:
            self.standing.skipped_too_few_bars += 1
            return None

        atr = average_true_range(bars, self._atr_window_days)
        if atr is None:
            self.standing.skipped_too_few_bars += 1
            return None

        profile = EquityHistoricalProfile(
            venue_id=UPSTOX_BROKER_ID,
            symbol=request.symbol,
            week_52_high=max(bar.high for bar in bars),
            week_52_low=min(bar.low for bar in bars),
            average_daily_volume=sum(bar.volume for bar in bars) / len(bars),
            average_true_range=atr,
            previous_close=bars[-1].close,
            upper_circuit_limit=None,
            lower_circuit_limit=None,
            bars_used=len(bars),
            measured_at_ns=self._now_ns(),
            source=f"upstox v3 historical-candle, {len(bars)} daily bars to {request.to_date}",
        )
        self.standing.profiles_published += 1
        return profile


def describe_profiler(profiler: EquityOpportunityProfiler) -> dict:
    return {
        "part_id": PART_ID,
        "candidates_known": profiler.standing.candidates_known,
        "requests_planned": profiler.standing.requests_planned,
        "requests_refused": profiler.standing.requests_refused,
        "profiles_published": profiler.standing.profiles_published,
        "skipped_too_few_bars": profiler.standing.skipped_too_few_bars,
        "windows_already_read": len(profiler._windows_read),
        "candidates_awaiting_a_window": max(
            0, len(profiler._candidate_by_key) - len(profiler._windows_read)
        ),
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    import time

    from parts.market_data_feed.broker_symbol_universe_bridge import EquityWithoutADerivative
    from runtime.brokers.upstox import UpstoxAdapter
    from runtime.input_assembly import Batch, LatestByKey

    listings = Batch(read=context.bus.reader("broker-instrument-listing"))
    tokens = LatestByKey(
        read=context.bus.reader("broker-token-standing"),
        key_of=lambda standing: standing.broker_id,
        maximum_age_seconds=context.number("broker_token_standing_maximum_age"),
    )
    publish_profiles = context.bus.publisher_for("equity-historical-profile")

    profiler = EquityOpportunityProfiler(
        equity_admits=EquityWithoutADerivative().admits,
        days_back=int(context.number("equity_profile_days_back")),
        atr_window_days=int(context.number("equity_profile_atr_window_days")),
        most_symbols_per_sweep=int(context.number("equity_profile_symbols_per_sweep")),
    )
    adapter = UpstoxAdapter()
    fetch_interval = context.number("equity_profile_fetch_interval_seconds")
    last_fetched_at: list[float | None] = [None]

    def tick() -> None:
        profiler.observe_listings(listings.payloads())

        now = time.monotonic()
        if last_fetched_at[0] is not None and now - last_fetched_at[0] < fetch_interval:
            return
        held = tokens.values()
        if not held:
            return
        requests = profiler.requests_due(today=datetime.date.today())
        if not requests:
            return
        last_fetched_at[0] = now
        # One window per due tick, the same reasoning broker-history-reader's own
        # sweep uses: the broker's rate limit is real, and a burst of requests
        # this tick buys nothing the next due tick would not.
        request = requests[0]
        url = adapter.historical_candle_url(
            request.instrument_key, DAY_UNIT, DAY_INTERVAL,
            from_date=request.from_date, to_date=request.to_date,
        )
        profile = profiler.observe_window(request, _fetch_json(url, held[0].access_token))
        if profile is not None:
            publish_profiles((profile,))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_profiler(profiler),
    )


def _fetch_json(url: str, access_token: str):
    """One GET, with the same Chrome impersonation `broker-history-reader`
    already needs -- Upstox's Cloudflare front answers stdlib urllib with a
    bot-fingerprint block (`Error 1010`), not an auth failure."""
    from curl_cffi import requests as curl_requests

    response = curl_requests.get(
        url,
        headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
        impersonate="chrome",
        timeout=30,
    )
    if response.status_code != 200:
        return {"status": "error", "http_status": response.status_code}
    return response.json()


__all__ = [
    "PART_DECLARATION",
    "PART_ID",
    "EquityHistoricalProfile",
    "EquityOpportunityProfiler",
    "ProfileRequest",
    "ProfilerStanding",
    "average_true_range",
    "describe_profiler",
    "start_part",
]
