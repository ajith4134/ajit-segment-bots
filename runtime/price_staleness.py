"""How old a symbol's last price may be before a position cannot be sized against it.

Every part that decides something about a symbol holds that symbol's last price as
a level, and a level stops updating the moment the symbol stops printing. Nothing
in that is visible from inside the part: it ticks, it has a number, and the number
is whatever last arrived. On the live run of 2026-08-23 that was how an ENAUSDT
order came to be priced at 0.17019, the market of fifty-six minutes earlier.

**The bound is per symbol, and it is learned.** One global number cannot be right:
measured on this system's own tape for 2026-08-23, a price stays within one round
trip's cost for about fifteen seconds on BTCUSDT and for about one on ETHUSDT,
because what makes a price wrong is not how long ago it printed but how far the
symbol moves while it sits there. A part that judges carries a learned component
(RL-060), and this is the component.

The estimate itself:

    a price of age A is off by roughly  m x sqrt(A)

where `m` is the symbol's own 95th-percentile absolute move over one second, kept
over a bounded window. Setting that equal to what a round trip costs -- the
threshold at which the system already calls a number material, rather than a new
one invented here -- gives

    believable age  =  (round trip cost / m) ^ 2

The square-root scaling is the random walk's, and it is the conservative
direction: measured against the tape it produced 8.6 s for BTCUSDT where the
distribution itself tolerated 15 s, so the bound errs toward refusing a price
rather than toward believing one. The measurement, its method and its numbers are
in `measurements/2026-08-24-reference-price-staleness/`.

Nothing here is a decision code path's literal (RL-061): every number arrives from
a named setting whose provenance is written beside it, and the prior for a symbol
that has not yet been measured is itself a measured number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from runtime.learned_estimator import Estimate, QuantileEstimator


@dataclass(frozen=True)
class ObservedPrice:
    """What a symbol last traded at, and when -- never one without the other."""

    price: float
    observed_at_ns: int

    def age_seconds(self, now_ns: int) -> float:
        return (now_ns - self.observed_at_ns) / 1e9


class PriceStalenessEstimator:
    """Per symbol: how far it moves in a second, and therefore how old a price may be.

    It holds no prices for anyone else to read. The part that needs a price keeps
    its own; this answers only the question that part cannot answer for itself,
    which is how long its copy stays worth acting on.
    """

    def __init__(
        self,
        materiality_fraction: float,
        anchor_seconds: float,
        quantile: float,
        window: int,
        observations_needed: int,
        prior_one_second_move: float,
        minimum_age_seconds: float,
        maximum_age_seconds: float,
    ) -> None:
        if not materiality_fraction > 0:
            raise ValueError(
                "the materiality fraction is what a round trip costs, as a fraction of "
                f"notional, and must be positive; got {materiality_fraction!r}"
            )
        if not anchor_seconds > 0:
            raise ValueError(f"anchor_seconds must be positive; got {anchor_seconds!r}")
        if not prior_one_second_move > 0:
            raise ValueError(
                "the prior is a measured one-second move and must be positive: a prior of "
                f"zero would say an unmeasured symbol never moves; got {prior_one_second_move!r}"
            )
        if not 0 < minimum_age_seconds <= maximum_age_seconds:
            raise ValueError(
                "the age bounds must satisfy 0 < minimum <= maximum; got "
                f"{minimum_age_seconds!r} and {maximum_age_seconds!r}"
            )
        self._materiality = materiality_fraction
        self._anchor_seconds = anchor_seconds
        self._quantile = quantile
        self._window = window
        self._observations_needed = observations_needed
        self._prior = prior_one_second_move
        self._minimum_age = minimum_age_seconds
        self._maximum_age = maximum_age_seconds
        self._moves: dict[tuple[str, str], QuantileEstimator] = {}
        self._anchors: dict[tuple[str, str], ObservedPrice] = {}

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        """One print, turned into a move only once it is far enough from the last anchor.

        Consecutive prints of the same symbol arrive milliseconds apart and their
        difference is the tick, not the symbol's volatility. So a move is measured
        against an anchor at least `anchor_seconds` old, and scaled to one second
        by the square root of however long the gap actually was -- which keeps the
        estimate the same quantity whether a symbol prints ten times a second or
        once a minute.
        """
        if price <= 0:
            return
        key = (venue_id, symbol)
        anchor = self._anchors.get(key)
        if anchor is None:
            self._anchors[key] = ObservedPrice(price=price, observed_at_ns=at_ns)
            return
        gap_seconds = (at_ns - anchor.observed_at_ns) / 1e9
        if gap_seconds < self._anchor_seconds:
            return
        move = abs(price / anchor.price - 1.0) / math.sqrt(gap_seconds)
        estimator = self._moves.get(key)
        if estimator is None:
            estimator = QuantileEstimator(window=self._window, prior=self._prior)
            self._moves[key] = estimator
        estimator.observe(move)
        self._anchors[key] = ObservedPrice(price=price, observed_at_ns=at_ns)

    def one_second_move(self, venue_id: str, symbol: str) -> Estimate:
        """How far this symbol moves in a second, at the quantile that matters."""
        estimator = self._moves.get((venue_id, symbol))
        if estimator is None:
            estimator = QuantileEstimator(window=self._window, prior=self._prior)
        return estimator.estimate(
            quantile=self._quantile, minimum_observations=self._observations_needed
        )

    def believable_age_seconds(self, venue_id: str, symbol: str) -> Estimate:
        """How old this symbol's price may be and still be worth sizing against.

        Clamped at both ends. The floor exists because below the anchor spacing the
        estimate is extrapolating inside its own resolution and cannot tell one
        sub-second bound from another; the ceiling because past it every symbol
        measured moves further than the stop distance the sizer works with, so a
        price that old cannot place a stop at all.
        """
        move = self.one_second_move(venue_id, symbol)
        if move.value <= 0.0:
            # **Every measured move was zero: the symbol printed the same price
            # over and over.** A price that is not moving cannot drift past what a
            # round trip costs, so the longest believable age is the honest answer
            # and the ceiling still bounds it.
            #
            # This divided by zero and crash-looped `instrument-selector` on
            # 2026-09-07, on the *health read* rather than in a decision, so the
            # part died once a second while every counter it published looked
            # ordinary. It became reachable when the prior and the materiality
            # were re-derived for Indian options that day: a quiet strike prints
            # the same premium hundreds of times, and its p95 one-second move is
            # exactly 0.0. Nothing had ever been that still on a crypto perpetual.
            return Estimate(
                value=self._maximum_age, is_fitted=move.is_fitted,
                observations=move.observations, prior=(self._materiality / self._prior) ** 2,
                was_clamped=True, bound_low=self._minimum_age, bound_high=self._maximum_age,
                reason=(
                    f"every one of {move.observations} measured moves was zero -- this symbol "
                    f"printed the same price throughout, so nothing can make its price stale "
                    f"except the {self._maximum_age:g}s ceiling"
                ),
            )
        age = (self._materiality / move.value) ** 2
        clamped = min(max(age, self._minimum_age), self._maximum_age)
        reason = (
            f"a {self._quantile:.0%} one-second move of {move.value:.4%} reaches the "
            f"{self._materiality:.4%} a round trip costs after {age:.2f}s ({move.reason})"
        )
        if clamped != age:
            reason += (
                f"; clamped to {clamped:.2f}s, inside "
                f"[{self._minimum_age:g}s, {self._maximum_age:g}s]"
            )
        return Estimate(
            value=clamped,
            is_fitted=move.is_fitted,
            observations=move.observations,
            prior=(self._materiality / self._prior) ** 2,
            was_clamped=clamped != age,
            bound_low=self._minimum_age,
            bound_high=self._maximum_age,
            reason=reason,
        )

    @property
    def symbols_measured(self) -> int:
        return len(self._moves)

    def describe(self) -> dict:
        """What this estimator currently believes, for a part's own standing."""
        return {
            "symbols_measured": self.symbols_measured,
            "materiality_fraction": self._materiality,
            "believable_age_seconds": {
                f"{venue_id}|{symbol}": self.believable_age_seconds(venue_id, symbol).value
                for venue_id, symbol in sorted(self._moves)
            },
        }


def price_staleness_from(context, materiality_fraction: float | None = None) -> PriceStalenessEstimator:
    """The estimator a part builds from its settings, assembled in one place.

    Every part that judges a price needs the same seven numbers, and seven
    settings read separately in each of them is seven chances for two parts to
    disagree about how old a price may be. What makes a stale price material is
    the same threshold that makes a cost material, so the materiality defaults to
    what a round trip on the traded instrument costs (RL-061).

    **That default is `reference_price_materiality_fraction`, and it stopped being
    `2 * taker_fee_rate` on 2026-09-07.** `taker_fee_rate` is Bybit's published
    perpetual rate; this project trades NSE options, whose round trip is Upstox's
    six-line charge stack *plus two crossings of a spread that is wider than every
    charge put together*. Measured on this project's own tape that day: charges
    0.2341% round trip, half touch spread 0.3175% at p50 over 23,606 book
    snapshots, so 0.8532% all in against the 0.1100% the crypto fee implied. The
    prior one-second move was wrong the other way by 2.6x, and the two errors
    cancelled into a bound of 1.5s that looked ordinary -- while the median option
    contract prints every 9.25s, so the newest price in existence was refused for
    82% of the session and every reader downstream fell back to the underlying's
    price. Both numbers now come from
    `measurements/2026-09-07-indian-price-staleness/`.

    **`materiality_fraction` exists because "material" is not one question.** The
    default asks how old a price may be before acting on it costs more than the
    round trip does -- which is the right question for a part about to size or
    place an order, and the wrong one for a part that is only recording where
    price stood when a claim was made. A labeller's price is contaminated when it
    has drifted far enough to distort the barrier the claim will be judged
    against, and that barrier is wider than a fee: measured on the live spine on
    2026-08-26, `signal-outcome-labeller` refused 1,053 of 1,397 claims for a
    stale price against a 1.5-second bound derived from a 0.11% round trip, while
    the move it actually judges is 0.2%. Because the bound goes as the square of
    the materiality, that fee-derived bound is more than three times tighter than
    the labeller's own question warrants, and it was discarding most of the
    training signal the system has.

    A caller passing this states what "material" means for the judgement it is
    about to make. It is still derived from a measured quantity -- never a number
    chosen to admit more claims.
    """
    return PriceStalenessEstimator(
        materiality_fraction=(
            context.number("reference_price_materiality_fraction")
            if materiality_fraction is None
            else materiality_fraction
        ),
        anchor_seconds=context.number("reference_price_move_anchor_seconds"),
        quantile=context.number("reference_price_move_quantile"),
        window=int(context.number("reference_price_move_window")),
        observations_needed=int(context.number("reference_price_move_observations_needed")),
        prior_one_second_move=context.number("reference_price_prior_one_second_move"),
        minimum_age_seconds=context.number("reference_price_minimum_age_seconds"),
        maximum_age_seconds=context.number("reference_price_maximum_age_seconds"),
    )
