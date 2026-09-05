"""cash-equity-shortlist-ranker: today's top-N ordinary NSE shares, ranked by
momentum, volume, 52-week range, gap, VWAP deviation and volatility-normalised
move -- never a fixed list, and never open interest, which is a derivatives
fact with no cash-equity analogue (docs/proposals/cash-equity-shortlist-
ranker.md).

**Why this is a part of its own, not folded into `broker-symbol-universe-
bridge` or `equity-opportunity-profiler` (T-6).** The bridge's job is
"republish the broker's own instrument master as `symbol-universe`" -- adding a
multi-signal blend to it would be a second job welded onto the first. The
profiler's job is "know each share's own history" -- it has no opinion about
today's price action. Ranking today's opportunity from both, plus what is
trading right now, is its own one job.

**It does not know about F&O exclusion (T-4).** "Ordinary NSE share" is
`EquityWithoutADerivative.admits`, the same predicate
`equity-opportunity-profiler` reuses -- whether a share is *also* an F&O
underlying is `broker-symbol-universe-bridge`'s question, asked of this part's
output where cash-equity's universe is actually published.

The blend is `runtime.cash_equity_shortlist.rank_cash_equity_candidates`, a
pure function shared with `operate/replay_a_captured_session.py` so a replay
cannot rank a different day's shortlist than the live spine would have.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from runtime.brokers.upstox import UPSTOX_BROKER_ID
from runtime.cash_equity_shortlist import (
    CashEquityCandidate, ShortlistWeights, rank_cash_equity_candidates,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "cash-equity-shortlist-ranker"

PART_DECLARATION = PartDeclaration(
    part_id="cash-equity-shortlist-ranker",
    consumes=(
        "broker-instrument-listing", "equity-historical-profile", "liquidity-grade",
        "broker-price-frame", "candle",
    ),
    produces=("cash-equity-shortlist", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="delays",
)

IST_OFFSET = datetime.timedelta(hours=5, minutes=30)


def ist_date_of(time_ns: int) -> datetime.date:
    """The calendar date a UTC nanosecond timestamp falls on in IST.

    Session-day boundaries here have to agree with the exchange's own calendar,
    not UTC's -- a candle printed at 18:31 UTC is already the next day's first
    bar in Mumbai.
    """
    return (datetime.datetime.fromtimestamp(time_ns / 1_000_000_000, tz=datetime.timezone.utc) + IST_OFFSET).date()


@dataclass(frozen=True)
class CashEquityShortlist:
    """Today's ranked top-N cash-equity symbols. A level, republished on an
    interval like every other symbol-universe-shaped type -- a consumer that
    started after the last change has no way to ask for what it missed."""

    venue_id: str
    symbols: tuple[str, ...]
    candidates_considered: int
    ranked_at_ns: int


@dataclass
class RankerStanding:
    candidates_known: int = 0
    profiles_held: int = 0
    liquidity_grades_held: int = 0
    priced_symbols: int = 0
    ranks_computed: int = 0
    last_shortlist_size: int = 0


class CashEquityShortlistRanker:
    """Holds every input signal and turns them into today's shortlist."""

    def __init__(
        self,
        equity_admits,
        shortlist_size: int,
        liquidity_pool_size: int,
        weights: ShortlistWeights,
        now_ns=None,
    ) -> None:
        import time

        self._admits = equity_admits
        self._shortlist_size = shortlist_size
        self._liquidity_pool_size = liquidity_pool_size
        self._weights = weights
        self._now_ns = now_ns or time.time_ns
        self._symbol_by_key: dict[str, str] = {}
        self._profile_by_symbol: dict = {}
        self._liquidity_by_symbol: dict = {}
        self._last_price_by_symbol: dict[str, float] = {}
        self._session_day_by_symbol: dict[str, datetime.date] = {}
        self._session_open_by_symbol: dict[str, float] = {}
        self._cumulative_volume_by_symbol: dict[str, float] = {}
        self._cumulative_quote_volume_by_symbol: dict[str, float] = {}
        self.standing = RankerStanding()

    def observe_listings(self, listings) -> None:
        for listing in listings:
            if not self._admits(listing):
                continue
            self._symbol_by_key[listing.instrument_key] = listing.trading_symbol
        self.standing.candidates_known = len(self._symbol_by_key)

    def observe_profile(self, profile) -> None:
        self._profile_by_symbol[profile.symbol] = profile
        self.standing.profiles_held = len(self._profile_by_symbol)

    def observe_liquidity_grade(self, grade) -> None:
        self._liquidity_by_symbol[grade.symbol] = grade
        self.standing.liquidity_grades_held = len(self._liquidity_by_symbol)

    def observe_price_frame(self, frame) -> None:
        tracked_keys = set(self._symbol_by_key)
        for level in frame.levels:
            symbol = self._symbol_by_key.get(level.instrument_key)
            if symbol is None:
                continue
            self._last_price_by_symbol[symbol] = level.price
        self.standing.priced_symbols = len(self._last_price_by_symbol)

    def observe_candle(self, candle) -> None:
        if candle.symbol not in self._symbol_by_key.values():
            # Not one of today's ordinary-share candidates -- an option or
            # index candle sharing the same wire. T-4: this part does not ask
            # what kind of instrument it is, only whether it is one it tracks.
            return
        day = ist_date_of(candle.open_time_ns)
        if self._session_day_by_symbol.get(candle.symbol) != day:
            self._session_day_by_symbol[candle.symbol] = day
            self._session_open_by_symbol[candle.symbol] = candle.open
            self._cumulative_volume_by_symbol[candle.symbol] = 0.0
            self._cumulative_quote_volume_by_symbol[candle.symbol] = 0.0
        self._cumulative_volume_by_symbol[candle.symbol] += candle.volume
        self._cumulative_quote_volume_by_symbol[candle.symbol] += candle.quote_volume

    def _candidate(self, symbol: str) -> CashEquityCandidate:
        profile = self._profile_by_symbol.get(symbol)
        liquidity = self._liquidity_by_symbol.get(symbol)
        volume_so_far = self._cumulative_volume_by_symbol.get(symbol)
        quote_volume_so_far = self._cumulative_quote_volume_by_symbol.get(symbol)
        vwap = (
            quote_volume_so_far / volume_so_far
            if volume_so_far else None
        )
        return CashEquityCandidate(
            symbol=symbol,
            last_price=self._last_price_by_symbol.get(symbol),
            session_open=self._session_open_by_symbol.get(symbol),
            previous_close=profile.previous_close if profile else None,
            week_52_high=profile.week_52_high if profile else None,
            week_52_low=profile.week_52_low if profile else None,
            average_daily_volume=profile.average_daily_volume if profile else None,
            volume_so_far=volume_so_far,
            average_true_range=profile.average_true_range if profile else None,
            session_vwap=vwap,
            liquidity_spread_fraction=(
                liquidity.spread_fraction if liquidity is not None else None
            ),
        )

    def rank(self) -> CashEquityShortlist | None:
        """Today's shortlist, or `None` while there are no candidates at all --
        an empty universe published as an empty shortlist would read as "ranked
        and nothing qualified" rather than "nothing to rank yet" (Rule 8)."""
        symbols = tuple(self._symbol_by_key.values())
        if not symbols:
            return None
        candidates = [self._candidate(symbol) for symbol in symbols]
        pool_size = min(self._liquidity_pool_size, len(candidates))
        shortlist_size = min(self._shortlist_size, pool_size)
        ranked = rank_cash_equity_candidates(
            candidates, shortlist_size=shortlist_size, weights=self._weights,
            liquidity_pool_size=pool_size,
        )
        self.standing.ranks_computed += 1
        self.standing.last_shortlist_size = len(ranked)
        return CashEquityShortlist(
            venue_id=UPSTOX_BROKER_ID, symbols=ranked,
            candidates_considered=len(candidates), ranked_at_ns=self._now_ns(),
        )


def describe_ranker(ranker: CashEquityShortlistRanker) -> dict:
    return {
        "part_id": PART_ID,
        "candidates_known": ranker.standing.candidates_known,
        "profiles_held": ranker.standing.profiles_held,
        "liquidity_grades_held": ranker.standing.liquidity_grades_held,
        "priced_symbols": ranker.standing.priced_symbols,
        "ranks_computed": ranker.standing.ranks_computed,
        "last_shortlist_size": ranker.standing.last_shortlist_size,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from parts.market_data_feed.broker_symbol_universe_bridge import EquityWithoutADerivative
    from runtime.input_assembly import Batch
    from runtime.level_publishing import LevelPublisher

    listings = Batch(read=context.bus.reader("broker-instrument-listing"))
    profiles = Batch(read=context.bus.reader("equity-historical-profile"))
    liquidity_grades = Batch(read=context.bus.reader("liquidity-grade"))
    price_frames = Batch(read=context.bus.reader("broker-price-frame"))
    candles = Batch(read=context.bus.reader("candle"))
    publish_shortlist = context.bus.publisher_for("cash-equity-shortlist")

    ranker = CashEquityShortlistRanker(
        equity_admits=EquityWithoutADerivative().admits,
        shortlist_size=int(context.number("cash_equity_shortlist_size")),
        liquidity_pool_size=int(context.number("cash_equity_shortlist_liquidity_pool_size")),
        weights=ShortlistWeights(
            momentum_weight=context.number("cash_equity_shortlist_momentum_weight"),
            volume_weight=context.number("cash_equity_shortlist_volume_weight"),
            week_52_weight=context.number("cash_equity_shortlist_week_52_weight"),
            gap_weight=context.number("cash_equity_shortlist_gap_weight"),
            vwap_weight=context.number("cash_equity_shortlist_vwap_weight"),
            atr_weight=context.number("cash_equity_shortlist_atr_weight"),
        ),
    )
    restated = LevelPublisher(
        publish=publish_shortlist,
        refresh_interval_seconds=context.number("cash_equity_shortlist_restatement_interval"),
        # Only `symbols` is the finding. `ranked_at_ns` and
        # `candidates_considered` restate when this part last looked, not what
        # it found -- compared whole, no two ticks would ever be equal and this
        # would republish every tick regardless of the digest, the exact storm
        # runtime/level_publishing.py's own module docstring measured.
        identity_of=lambda items: tuple(item.symbols for item in items),
    )

    def tick() -> None:
        ranker.observe_listings(listings.payloads())
        for profile in profiles.payloads():
            ranker.observe_profile(profile)
        for grade in liquidity_grades.payloads():
            ranker.observe_liquidity_grade(grade)
        for frame in price_frames.payloads():
            ranker.observe_price_frame(frame)
        for candle in candles.payloads():
            ranker.observe_candle(candle)
        shortlist = ranker.rank()
        if shortlist is not None:
            restated.publish_level((shortlist,))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_ranker(ranker),
    )


__all__ = [
    "PART_DECLARATION",
    "PART_ID",
    "CashEquityShortlist",
    "CashEquityShortlistRanker",
    "RankerStanding",
    "describe_ranker",
    "ist_date_of",
    "start_part",
]
