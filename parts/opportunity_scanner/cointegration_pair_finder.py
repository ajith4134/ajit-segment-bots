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
import time
from dataclasses import dataclass, field

from runtime.price_frames import levels_in
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow, correlation, linear_fit

PART_ID = "cointegration-pair-finder"

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
    symbols_tracked: int = 0
    strongest_reversion: float = 0.0


class CointegrationPairFinder:
    """Tests whether a pair's spread reverts, and retires pairs whose spread stops."""

    def __init__(
        self,
        window_length: int,
        minimum_observations: int,
        minimum_correlation: float,
        minimum_reversion_strength: float,
        maximum_gap_seconds: float | None = None,
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
        key = (venue_id, symbol)
        window = self._prices.get(key)
        if window is None:
            window = RollingWindow(
                length=self._window_length, maximum_gap_seconds=self._maximum_gap_seconds
            )
            self._prices[key] = window
        window.observe(price, at_ns)
        self.standing.symbols_tracked = len(self._prices)

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
        "symbols_tracked": finder.standing.symbols_tracked,
        "strongest_reversion": finder.standing.strongest_reversion,
    }


def run_cointegration_pair_finder(
    finder: CointegrationPairFinder, control_socket, read_prices_and_pairs, publish_pairs,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        pairs = read_prices_and_pairs(finder)
        publish_pairs(tuple(finder.test_pair(*pair) for pair in pairs))

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
    import itertools

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
    )
    pairs_per_tick = int(context.number("cointegration_pairs_tested_per_tick"))
    symbols_by_venue: dict[str, set[str]] = {}
    rotation: list[tuple[str, str, str]] = []
    rotation_position = [0]

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

        every_pair = [
            (venue_id, left, right)
            for venue_id, symbols in sorted(symbols_by_venue.items())
            for left, right in itertools.combinations(sorted(symbols), 2)
        ]
        if every_pair != rotation:
            rotation[:] = every_pair
            rotation_position[0] = min(rotation_position[0], len(rotation))
        if not rotation:
            return ()
        start = rotation_position[0] % len(rotation)
        taken = rotation[start : start + pairs_per_tick]
        if len(taken) < pairs_per_tick:
            taken += rotation[: pairs_per_tick - len(taken)]
        rotation_position[0] = (start + len(taken)) % len(rotation)
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
    )
