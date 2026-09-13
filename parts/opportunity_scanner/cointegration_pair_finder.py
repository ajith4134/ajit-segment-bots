"""cointegration-pair-finder: pairs whose spread has actually held together.

Correlation is not enough and is the classic way to lose money on pairs. Two
symbols can be highly correlated and drift apart forever -- correlation is about
returns moving together, cointegration is about the *spread* staying bounded, and
only the second one implies a trade that comes back.

So a pair qualifies on the spread's own behaviour: it must be stationary enough
that deviations revert, measured by how strongly the spread pulls back toward its
mean rather than by how well the two prices move together.

**A pair is re-tested continually and can lose its status.** Cointegration is a
property of a period, not of a pair -- the relationship that held for six months
breaks when one of the two lists a competitor or changes its supply schedule --
and a finder that never retired a pair would keep trading a spread that had
stopped existing.
"""

from __future__ import annotations

import math
import pathlib
import time
from dataclasses import dataclass, field

from runtime.price_frames import levels_in
from runtime.durable_state import RESTORED
from runtime.pair_sweep import pairs_after
from runtime.part_declaration import PartDeclaration
from runtime.lot_book_checkpoint import book_key_of, book_key_text
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow, correlation, linear_fit

PART_ID = "cointegration-pair-finder"

CHECKPOINT_COMPONENT = "pairs"

PART_DECLARATION = PartDeclaration(
    part_id="cointegration-pair-finder",
    consumes=("symbol-price-frame", "market-regime"),
    produces=("cointegrated-pair", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

COINTEGRATED = "cointegrated"
CORRELATED_ONLY = "correlated-but-drifting"
UNRELATED = "unrelated"
TOO_FEW_OBSERVATIONS = "too-few-observations"


@dataclass(frozen=True)
class CointegratedPair:
    """Two symbols whose spread holds together, and the ratio that defines it."""

    venue_id: str
    left_symbol: str
    right_symbol: str
    state: str
    hedge_ratio: float | None
    spread_mean: float | None
    spread_deviation: float | None
    reversion_strength: float | None
    correlation: float | None
    observations: int
    reason: str
    tested_at_ns: int

    @property
    def is_tradeable(self) -> bool:
        return self.state == COINTEGRATED


@dataclass
class FinderStanding:
    pairs_tested: int = 0
    cointegrated: int = 0
    correlated_only: int = 0
    unrelated: int = 0
    pairs_retired: int = 0
    verdicts_published: int = 0
    verdicts_suppressed: int = 0
    symbols_tracked: int = 0
    prices_observed: int = 0
    strongest_reversion: float = 0.0
    # What survived the last off switch. `restored_symbols` is the name the
    # substrate sets; here it counts price series brought back, and
    # `restored_pairs` counts the verdicts that came with them.
    restored_symbols: int = 0
    restored_pairs: int = 0
    checkpoint_verdict: str = ""


class CointegrationPairFinder:
    """Tests whether a pair's spread reverts, and retires pairs whose spread stops."""

    def __init__(
        self,
        window_length: int,
        minimum_observations: int,
        minimum_correlation: float,
        minimum_reversion_strength: float,
        maximum_gap_seconds: float | None = None,
        gap_patience_multiple: float | None = None,
        gap_warmup_gaps: int | None = None,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < minimum_reversion_strength < 1.0:
            raise ValueError("reversion strength is a fraction of the deviation pulled back per step")
        self._window_length = window_length
        self._minimum = minimum_observations
        self._minimum_correlation = minimum_correlation
        self._minimum_reversion = minimum_reversion_strength
        self._now_ns = now_ns
        # How long a symbol may be silent before its window is judged to have a
        # hole in it rather than a series. None means the caller stated no bound,
        # and this part does not invent one (RL-061).
        self._maximum_gap_seconds = maximum_gap_seconds
        self._gap_patience_multiple = gap_patience_multiple
        self._gap_warmup_gaps = gap_warmup_gaps
        self._prices: dict[tuple[str, str], RollingWindow] = {}
        self._cointegrated: set[tuple[str, str, str]] = set()
        self.standing = FinderStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        """One print, with the venue's own time for it.

        Two series are only cointegrated relative to each other over one stretch of
        market. A hole in either one leaves the pair's histories describing
        different spans, and the relationship measured across them is between two
        things that were never observed together.
        """
        window = self._window_for((venue_id, symbol))
        window.observe(price, at_ns)
        self.standing.symbols_tracked = len(self._prices)
        # Counts observations rather than ticks: what a crash costs is prices,
        # and the checkpoint schedule is spelled in the same unit.
        self.standing.prices_observed += 1

    def _window_for(self, key: tuple[str, str]) -> RollingWindow:
        """This symbol's series, made on first sight with this process's settings.

        One place, so a series restored from a checkpoint is configured exactly as
        one built from a live print -- a restore that made its windows a different
        length would judge a different stretch of market from the tests that follow.
        """
        window = self._prices.get(key)
        if window is None:
            window = RollingWindow(
                length=self._window_length,
                maximum_gap_seconds=self._maximum_gap_seconds,
                gap_patience_multiple=self._gap_patience_multiple,
                gap_warmup_gaps=self._gap_warmup_gaps,
            )
            self._prices[key] = window
        return window

    def test_pair(self, venue_id: str, left_symbol: str, right_symbol: str) -> CointegratedPair:
        self.standing.pairs_tested += 1
        left = self._prices.get((venue_id, left_symbol))
        right = self._prices.get((venue_id, right_symbol))

        if left is None or right is None or min(left.count, right.count) < self._minimum:
            return self._pair(
                venue_id, left_symbol, right_symbol, TOO_FEW_OBSERVATIONS,
                None, None, None, None, None, 0,
                f"{self._minimum} observations of each are needed before a spread can be judged",
            )

        length = min(left.count, right.count)
        left_series = list(left.values)[-length:]
        right_series = list(right.values)[-length:]
        pair_correlation = correlation(left_series, right_series)

        pair_key = (venue_id, left_symbol, right_symbol)

        fit = linear_fit(list(zip(right_series, left_series)))
        if fit is None:
            # No hedge ratio exists, so there is no spread to judge. A pair that
            # was cointegrated is retired here as firmly as one that failed a
            # threshold -- untestable is not a reason to keep trading it.
            self.standing.unrelated += 1
            self._retire(pair_key)
            return self._pair(
                venue_id, left_symbol, right_symbol, UNRELATED,
                None, None, None, pair_correlation, None, length,
                "the second symbol did not move, so no hedge ratio exists and the "
                "spread cannot be judged at all",
            )
        hedge_ratio, _intercept = fit

        spread = [
            left_price - hedge_ratio * right_price
            for left_price, right_price in zip(left_series, right_series)
        ]
        spread_mean = sum(spread) / len(spread)
        variance = sum((value - spread_mean) ** 2 for value in spread) / (len(spread) - 1)
        spread_deviation = math.sqrt(variance)

        # How strongly the spread pulls back: regress each step's change on the
        # previous deviation from the mean. A negative slope means it reverts,
        # and its magnitude is the fraction pulled back per step.
        points = [
            (earlier - spread_mean, later - earlier)
            for earlier, later in zip(spread, spread[1:])
        ]
        reversion_fit = linear_fit(points)
        reversion = -reversion_fit[0] if reversion_fit else None

        if pair_correlation is None or abs(pair_correlation) < self._minimum_correlation:
            self.standing.unrelated += 1
            self._retire(pair_key)
            return self._pair(
                venue_id, left_symbol, right_symbol, UNRELATED, hedge_ratio, spread_mean,
                spread_deviation, pair_correlation, reversion, length,
                f"correlation {pair_correlation if pair_correlation is not None else float('nan'):.2f} "
                f"is below the {self._minimum_correlation:.2f} needed even to look at the spread",
            )

        if reversion is None or reversion < self._minimum_reversion:
            # Correlated and drifting: the classic way to lose money on pairs.
            self.standing.correlated_only += 1
            self._retire(pair_key)
            return self._pair(
                venue_id, left_symbol, right_symbol, CORRELATED_ONLY, hedge_ratio, spread_mean,
                spread_deviation, pair_correlation, reversion, length,
                f"correlation is {pair_correlation:.2f} but the spread pulls back only "
                f"{(reversion or 0):.3f} per step; correlated symbols can drift apart forever",
            )

        self.standing.cointegrated += 1
        self.standing.strongest_reversion = max(self.standing.strongest_reversion, reversion)
        self._cointegrated.add(pair_key)
        return self._pair(
            venue_id, left_symbol, right_symbol, COINTEGRATED, hedge_ratio, spread_mean,
            spread_deviation, pair_correlation, reversion, length,
            f"the spread pulls back {reversion:.3f} of its deviation per step at a hedge ratio "
            f"of {hedge_ratio:.4f}, over {length} observations",
        )

    def test_pair_for_publication(
        self, venue_id: str, left_symbol: str, right_symbol: str
    ) -> CointegratedPair | None:
        """Test the pair, and return it only when the verdict is worth saying.

        A pair that is not cointegrated and was not cointegrated last time it was
        tested is not news: saying so again tells the reader something it already
        believes, and the reader pays for every one of them.

        Measured live on 2026-08-24 at 50 symbols per venue, before this existed:
        this part published 506 verdicts a second, of which 299 were `unrelated`
        and 92 `correlated-but-drifting` -- 77% of the traffic was pairs that
        cannot be traded, against 2,485 pairs that actually were cointegrated.
        Downstream, spread-reversion-detector held every one of them and re-tested
        it on every tick, which is where 11,013 of its 13,557 tests a second went.
        At 100 symbols per venue the same shape dropped 663,028 of its inputs.

        Three things are news, and nothing else is:

        * the pair is cointegrated -- the reader trades on the hedge ratio and the
          statistics, and they are refreshed exactly as often as before;
        * the pair has just stopped being cointegrated -- said once, because a
          reader that never heard it would keep trading a spread that has ended;
        * nothing. A pair that was untradeable and still is stays unsaid.

        The retirement is what makes the silence safe. Suppressing a verdict the
        reader needs would be a worse defect than the flood this replaces.
        """
        pair_key = (venue_id, left_symbol, right_symbol)
        was_tradeable = pair_key in self._cointegrated
        pair = self.test_pair(venue_id, left_symbol, right_symbol)
        if pair.is_tradeable or was_tradeable:
            self.standing.verdicts_published += 1
            return pair
        self.standing.verdicts_suppressed += 1
        return None

    def read_checkpoint_state(self) -> dict:
        """The price series and the verdicts, so a restart does not start blind.

        What this saves is not a little time. The windows refill in under a minute,
        but the pair verdicts are earned by rotating through every pair a few at a
        time -- pairs grow as the square of symbols, so at 50 symbols a venue that
        rotation is thousands of pairs long. A cold scanner publishes nothing
        tradeable until it has been round, and nothing downstream of it can act.

        Each window carries its own last observation time, so the gap across the
        restart is measured rather than assumed continuous. Without that the first
        price after an outage would sit beside the last one before it and read as a
        move that happened in an instant -- which is exactly the shape a detector
        fires on.
        """
        return {
            "prices": {
                book_key_text(key): window.as_document()
                for key, window in self._prices.items()
            },
            "cointegrated": [list(pair) for pair in sorted(self._cointegrated)],
        }

    def restore_from_checkpoint(self, state: dict) -> int:
        """Refill the series and the verdicts. Returns how many series came back."""
        for text, document in (state.get("prices") or {}).items():
            window = self._window_for(book_key_of(text))
            window.restore_document(document)
        self._cointegrated = {
            tuple(pair) for pair in (state.get("cointegrated") or ()) if len(pair) == 3
        }
        self.standing.symbols_tracked = len(self._prices)
        self.standing.restored_pairs = len(self._cointegrated)
        return len(self._prices)

    def _retire(self, pair_key) -> None:
        """A pair that stops cointegrating stops being tradeable, immediately."""
        if pair_key in self._cointegrated:
            self._cointegrated.discard(pair_key)
            self.standing.pairs_retired += 1

    @property
    def cointegrated_pairs(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(sorted(self._cointegrated))

    def _pair(
        self, venue_id, left, right, state, hedge_ratio, spread_mean,
        spread_deviation, pair_correlation, reversion, observations, reason
    ) -> CointegratedPair:
        return CointegratedPair(
            venue_id=venue_id, left_symbol=left, right_symbol=right, state=state,
            hedge_ratio=hedge_ratio, spread_mean=spread_mean,
            spread_deviation=spread_deviation, reversion_strength=reversion,
            correlation=pair_correlation, observations=observations,
            reason=reason, tested_at_ns=self._now_ns(),
        )


def describe_pairs(finder: CointegrationPairFinder) -> dict:
    return {
        "part_id": PART_ID,
        "pairs_tested": finder.standing.pairs_tested,
        "cointegrated": finder.standing.cointegrated,
        "correlated_but_drifting": finder.standing.correlated_only,
        "unrelated": finder.standing.unrelated,
        "pairs_retired": finder.standing.pairs_retired,
        "currently_cointegrated": len(finder.cointegrated_pairs),
        # What this part chose to say and what it chose to leave unsaid. Counted
        # because a suppression rate that fell to zero would mean the flood is
        # back, and one that reached 100% would mean nothing is cointegrated at
        # all -- two different failures that look identical from the bus.
        "verdicts_published": finder.standing.verdicts_published,
        "verdicts_suppressed": finder.standing.verdicts_suppressed,
        "symbols_tracked": finder.standing.symbols_tracked,
        "prices_observed": finder.standing.prices_observed,
        "restored_symbols": finder.standing.restored_symbols,
        "restored_pairs": finder.standing.restored_pairs,
        "checkpoint_restored": 1.0 if finder.standing.checkpoint_verdict == RESTORED else 0.0,
        "strongest_reversion": finder.standing.strongest_reversion,
    }


def run_cointegration_pair_finder(
    finder: CointegrationPairFinder, control_socket, read_prices_and_pairs, publish_pairs,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
    write_checkpoint=None,
) -> int:
    def tick() -> None:
        pairs = read_prices_and_pairs(finder)
        news = tuple(
            verdict
            for verdict in (finder.test_pair_for_publication(*pair) for pair in pairs)
            if verdict is not None
        )
        # Publishing an empty tuple would be a message saying nothing, delivered to
        # every reader, on every tick this part finds no news -- which is most of
        # them once the untradeable pairs go unsaid.
        if news:
            publish_pairs(news)
        # After publishing, and on its own schedule: the series and verdicts are
        # written every pair_state_checkpoint_interval observations, not every
        # tick, because a tick is a few milliseconds and an fsync is not.
        if write_checkpoint is not None:
            write_checkpoint(finder.standing.prices_observed)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_pairs(finder),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Pairs grow as the square of symbols -- 30 captured symbols per venue are 435
    pairs -- and each test is a linear fit over the window, so a tick that tested
    every pair would be a tick the governor could not interrupt (T-2). Pairs are
    tested in rotation instead: a bounded number per tick, every pair reached, none
    of them all at once.

    Which symbols exist is learned from the trades that arrive, never from a list.
    A part that read the symbol universe to decide what to pair would be consuming
    a data type it does not declare, and the blueprint is what decides that.
    """
    
    from runtime.input_assembly import Batch

    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    regimes = Batch(read=context.bus.reader("market-regime"))
    publish_pairs = context.bus.publisher_for("cointegrated-pair")

    finder = CointegrationPairFinder(
        window_length=int(context.number("cointegration_window_length")),
        minimum_observations=int(context.number("cointegration_minimum_observations")),
        minimum_correlation=context.number("cointegration_minimum_correlation"),
        minimum_reversion_strength=context.number("cointegration_minimum_reversion_strength"),
        maximum_gap_seconds=context.number("price_series_maximum_gap_seconds"),
            gap_patience_multiple=context.number("price_gap_patience_multiple"),
            gap_warmup_gaps=int(context.number("price_gap_warmup_gaps")),
    )
    # The series and the verdicts survive the off switch. Pairs grow as the square
    # of symbols, so a cold scanner has to rotate through thousands of them before
    # it can say anything tradeable, and everything downstream waits on that.
    from runtime.durable_state import (
        CheckpointSchedule,
        DurableStateStore,
        restore_and_arm_checkpoint,
    )

    pair_store = DurableStateStore(
        pathlib.Path(str(context.setting("position_state_root").value)).expanduser()
    )
    pair_schedule = CheckpointSchedule(int(context.number("pair_state_checkpoint_interval")))
    # The settings that give the stored series their meaning. A window of a
    # different length, or judged against a different gap bound, describes a
    # different stretch of market -- restoring across such a change would be
    # testing one span while reporting another.
    pair_settings = {
        "cointegration_window_length": float(context.number("cointegration_window_length")),
        "price_series_maximum_gap_seconds": float(
            context.number("price_series_maximum_gap_seconds")
        ),
        "price_gap_patience_multiple": float(context.number("price_gap_patience_multiple")),
    }
    write_pair_checkpoint = restore_and_arm_checkpoint(
        pair_store, pair_schedule, PART_ID, CHECKPOINT_COMPONENT, finder, pair_settings
    )

    pairs_per_tick = int(context.number("cointegration_pairs_tested_per_tick"))
    symbols_by_venue: dict[str, set[str]] = {}
    # Where the last tick stopped, as the pair itself. Not an index: the symbol
    # list changes as the feed does, and an index into yesterday's list points at
    # a different pair today.
    resume_after: list[tuple[str, str, str] | None] = [None]

    def read_prices_and_pairs(_finder):
        for trade in levels_in(trades.payloads()):
            finder.observe_price(
                trade.venue_id, trade.symbol, trade.price, trade.observed_at_ns
            )
            symbols_by_venue.setdefault(trade.venue_id, set()).add(trade.symbol)
        # The regime is consumed to keep this part's reading of the market current
        # even when it is drained by nobody else; the pair test itself is
        # regime-independent, and saying so is better than implying otherwise.
        regimes.payloads()

        # Walked lazily, never built. This list was materialised every tick until
        # 2026-09-05, which was affordable at 30 captured symbols a venue and is
        # not at the cash-equity universe: measured on this box, 2,444 symbols
        # make 2,985,346 pairs costing 252 MB and a quarter of a second to build
        # -- every tick, to test a few hundred of them.
        groups = {
            venue_id: sorted(symbols)
            for venue_id, symbols in sorted(symbols_by_venue.items())
        }
        taken = []
        for pair in pairs_after(groups, resume_after[0]):
            if len(taken) >= pairs_per_tick:
                break
            taken.append(pair)
        if taken:
            resume_after[0] = taken[-1]
        return tuple(taken)

    return run_cointegration_pair_finder(
        finder=finder,
        control_socket=context.control_socket,
        read_prices_and_pairs=read_prices_and_pairs,
        publish_pairs=publish_pairs,
        health_interval_seconds=context.health_interval_seconds,
        emit_health=context.emit_health,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        write_checkpoint=write_pair_checkpoint,
    )
