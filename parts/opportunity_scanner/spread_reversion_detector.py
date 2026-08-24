"""spread-reversion-detector: a cointegrated spread stretched far enough to snap back.

The trade that follows from `cointegration-pair-finder`. The pair finder says the
spread reverts; this says it has stretched far enough now to be worth acting on.

Two positions, not one. A spread trade is long one symbol and short the other at
the hedge ratio, and that is the point: the market direction cancels and what is
left is the relationship. A detector that fired on one leg would be taking a
directional bet it never intended.

**It refuses a pair whose cointegration has lapsed.** The pair finder retires
pairs whose spread stops reverting, and a stretched spread on a retired pair is
not an opportunity -- it is two symbols that have parted company, and the trade
that looks best is the one that has already gone wrong.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.price_frames import levels_in
from runtime.quote_frames import quote_levels_in
from runtime.reference_price import ReferencePriceChooser
from runtime.price_staleness import ObservedPrice, PriceStalenessEstimator, price_staleness_from
from runtime.market_signal import LONG, REVERSION, SHORT, SignalCalibrator, make_candidate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "spread-reversion-detector"

PART_DECLARATION = PartDeclaration(
    part_id="spread-reversion-detector",
    consumes=("cointegrated-pair", "symbol-price-frame", "symbol-quote-frame"),
    produces=("entry-candidate", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FIRED = "fired"
NOT_STRETCHED = "spread-not-stretched-far-enough"
STILL_STRETCHED = "already-fired-and-the-spread-has-not-come-back"
PAIR_NOT_COINTEGRATED = "pair-is-not-currently-cointegrated"
NO_PRICES = "no-current-prices-for-both-legs"
A_LEG_IS_STALE = "one-leg's-last-price-is-too-old-to-price-the-spread-with"


@dataclass(frozen=True)
class SpreadCandidatePair:
    """Both legs of one spread trade, and which way each goes."""

    long_symbol: str
    short_symbol: str
    hedge_ratio: float
    spread_z: float


@dataclass
class SpreadStanding:
    tests: int = 0
    candidates: int = 0
    not_stretched: int = 0
    still_stretched: int = 0
    not_cointegrated: int = 0
    no_prices: int = 0
    stale_leg: int = 0
    outcomes_learned: int = 0
    widest_z: float = 0.0
    untradeable_pairs_held: int = 0
    leg_priced_from_a_quote: int = 0
    leg_quote_too_wide: int = 0


class SpreadReversionDetector:
    """Fires when a live cointegrated spread is unusually far from its own mean."""

    def __init__(
        self,
        z_threshold: float,
        rearm_z: float,
        window_length: int,
        minimum_observations: int,
        horizon_seconds: float,
        calibrator: SignalCalibrator,
        price_staleness: PriceStalenessEstimator | None = None,
        reference_price=None,
        maximum_gap_seconds: float | None = None,
        gap_patience_multiple: float | None = None,
        now_ns=time.time_ns,
    ) -> None:
        if z_threshold <= 0:
            raise ValueError("a threshold of zero fires on every observation")
        if not 0 <= rearm_z < z_threshold:
            raise ValueError(
                f"the re-arm level must sit inside the firing threshold -- equal or above it, "
                f"every wiggle across the threshold is a fresh candidate, which is the flapping "
                f"this level exists to stop. Got rearm_z={rearm_z!r} against "
                f"z_threshold={z_threshold!r}"
            )
        self._z_threshold = z_threshold
        self._rearm_z = rearm_z
        self._window_length = window_length
        self._minimum = minimum_observations
        self._horizon = horizon_seconds
        self._calibrator = calibrator
        # A spread is two prices subtracted, and they arrive separately. If either
        # leg is old the difference is not a spread that ever existed -- and a
        # stale leg produces exactly the shape this detector exists to fire on,
        # because a price that stopped moving while the other leg ran looks like
        # the widest stretch it has ever seen. Both bounds are the caller's to
        # state; this part invents neither (RL-061).
        self._price_staleness = price_staleness
        self._reference_price = reference_price
        self._maximum_gap_seconds = maximum_gap_seconds
        self._gap_patience_multiple = gap_patience_multiple
        self._now_ns = now_ns
        self._prices: dict[tuple[str, str], float] = {}
        self._spreads: dict[tuple[str, str, str], RollingWindow] = {}
        # Which pairs are currently past the threshold. A candidate is the
        # crossing into stretched, not the state of being stretched: a pair
        # that stays wide is one opportunity, and announcing it on every level
        # update published 620,000 candidates in thirty-five minutes at 100
        # symbols per venue (2026-08-24) -- the labeller refused 645,000 of
        # them and every part of the trading half chewed the duplicates. The
        # pair re-arms only when its spread has come back inside rearm_z.
        self._stretched: set[tuple[str, str, str]] = set()
        self.standing = SpreadStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        """One leg's print, with the venue's own time for it."""
        self._prices[(venue_id, symbol)] = ObservedPrice(price=price, observed_at_ns=at_ns)
        if self._reference_price is not None:
            self._reference_price.observe_trade(venue_id, symbol, price, at_ns)

    def observe_quote(
        self, venue_id: str, symbol: str, bid_price: float, ask_price: float, at_ns: int
    ) -> None:
        """One leg's resting market, which exists whether or not the symbol traded.

        This is the fact that was missing. Measured 2026-08-24, 1,438,376 of this
        part's 4,199,062 tests were refused because a leg's last *trade* was too
        old -- 34% of its work -- while the symbol was being quoted the whole time.
        """
        if self._reference_price is None:
            return
        self._reference_price.observe_quote(venue_id, symbol, bid_price, ask_price, at_ns)

    def _leg_price(self, venue_id: str, symbol: str, at_ns: int):
        """What this leg is worth now: the trade if fresh, else the quote. None if neither.

        Counting happens here rather than in the chooser's own totals because a
        spread has two legs and a refusal is about the pair -- the chooser counts
        legs, this counts tests, and conflating them would double every number.
        """
        from runtime.reference_price import QUOTE_TOO_WIDE, ChosenPrice

        chosen = self._reference_price.price_for(venue_id, symbol, at_ns)
        if isinstance(chosen, ChosenPrice):
            if chosen.came_from_a_quote:
                self.standing.leg_priced_from_a_quote += 1
            return chosen
        if chosen.reason == QUOTE_TOO_WIDE:
            self.standing.leg_quote_too_wide += 1
        return None

    def tradeable_pairs_among(self, pairs) -> tuple:
        """The pairs worth testing, and a count of the ones being held and skipped.

        A retired pair stays in the level store upstream -- that store is what told
        this part it retired -- but testing it again every tick is work whose answer
        is already known, and `detect` would refuse it immediately.

        Measured live on 2026-08-24 at 50 symbols per venue: that refusal was
        11,013 of this part's 13,557 tests a second, and it grows as the square of
        the universe. At 100 symbols per venue the same shape left this part
        dropping 663,028 of its inputs.

        A method rather than a filter written where the inbox is read, because an
        untested filter standing between this part and most of its work is exactly
        the thing that should not be taken on trust.
        """
        held = tuple(pairs)
        tradeable = tuple(pair for pair in held if pair.is_tradeable)
        self.standing.untradeable_pairs_held = len(held) - len(tradeable)
        return tradeable

    def observe_outcome(self, regime: str, reverted: bool) -> None:
        self._calibrator.observe_outcome(PART_ID, regime, reverted)
        self.standing.outcomes_learned += 1

    def detect(self, pair, regime_name: str = "any") -> tuple[object | None, str]:
        """One pair's live spread against its own history."""
        self.standing.tests += 1

        if not pair.is_tradeable:
            # A stretched spread on a retired pair is two symbols that have
            # parted company, and it looks exactly like the best opportunity.
            self.standing.not_cointegrated += 1
            return None, PAIR_NOT_COINTEGRATED

        if self._reference_price is not None:
            # Each leg is worth its last trade while that is fresh, and its resting
            # mid once it is not. A symbol nobody has traded for a minute still has
            # a market, and refusing the pair over that was 34% of this part's work
            # on 2026-08-24 -- 1,438,376 tests of 4,199,062, thrown away over
            # symbols that were being quoted the whole time.
            at = self._now_ns()
            left = self._leg_price(pair.venue_id, pair.left_symbol, at)
            right = self._leg_price(pair.venue_id, pair.right_symbol, at)
            if left is None or right is None:
                # Both refusals land here: no price of any kind, and a price too
                # old on both sides. The chooser has counted which, and this counts
                # the test that could not be run.
                self.standing.stale_leg += 1
                return None, A_LEG_IS_STALE
        else:
            left = self._prices.get((pair.venue_id, pair.left_symbol))
            right = self._prices.get((pair.venue_id, pair.right_symbol))
            if left is None or right is None:
                self.standing.no_prices += 1
                return None, NO_PRICES

            if self._price_staleness is not None:
                at = self._now_ns()
                for symbol, observed in (
                    (pair.left_symbol, left), (pair.right_symbol, right),
                ):
                    bound = self._price_staleness.believable_age_seconds(pair.venue_id, symbol)
                    if observed.age_seconds(at) > bound.value:
                        self.standing.stale_leg += 1
                        return None, A_LEG_IS_STALE

        spread = left.price - pair.hedge_ratio * right.price
        # The spread is as recent as its older leg, not as its newer one: a
        # difference is only as current as the least current thing in it.
        spread_at_ns = min(left.observed_at_ns, right.observed_at_ns)
        key = (pair.venue_id, pair.left_symbol, pair.right_symbol)
        window = self._spreads.get(key)
        if window is None:
            window = RollingWindow(
                length=self._window_length,
                maximum_gap_seconds=self._maximum_gap_seconds,
                gap_patience_multiple=self._gap_patience_multiple,
            )
            self._spreads[key] = window
        window.observe(spread, spread_at_ns)

        z = window.z_score(spread, self._minimum)
        if z is None:
            # Fall back to the pair finder's own statistics while this detector's
            # own window fills, rather than refusing a pair already proven.
            if pair.spread_deviation and pair.spread_deviation > 0 and pair.spread_mean is not None:
                z = (spread - pair.spread_mean) / pair.spread_deviation
            else:
                self.standing.no_prices += 1
                return None, NO_PRICES

        if abs(z) < self._z_threshold:
            self.standing.not_stretched += 1
            # Re-arm only once the reversion the candidate predicted has
            # substantially happened. A spread that dips just under the
            # threshold and stretches again is the same episode -- measured
            # live at 100 symbols per venue, threshold-wiggling re-fired
            # 25,880 candidates in twenty-five minutes with plain crossing.
            if abs(z) <= self._rearm_z:
                self._stretched.discard(key)
            return None, NOT_STRETCHED

        self.standing.widest_z = max(self.standing.widest_z, abs(z))
        if key in self._stretched:
            self.standing.still_stretched += 1
            return None, STILL_STRETCHED
        self._stretched.add(key)
        self.standing.candidates += 1
        # A high spread means the left leg is rich against the right: sell the
        # left, buy the right. The candidate names the left leg's direction and
        # carries both legs in its evidence.
        direction = SHORT if z > 0 else LONG
        legs = SpreadCandidatePair(
            long_symbol=pair.right_symbol if z > 0 else pair.left_symbol,
            short_symbol=pair.left_symbol if z > 0 else pair.right_symbol,
            hedge_ratio=pair.hedge_ratio,
            spread_z=z,
        )
        confidence = self._calibrator.confidence(PART_ID, regime_name)

        return (
            make_candidate(
                detector=PART_ID,
                venue_id=pair.venue_id,
                symbol=pair.left_symbol,
                direction=direction,
                expectation=REVERSION,
                signal_strength=abs(z),
                confidence=confidence,
                horizon_seconds=self._horizon,
                evidence={
                    "long_symbol": legs.long_symbol,
                    "short_symbol": legs.short_symbol,
                    "hedge_ratio": pair.hedge_ratio,
                    "spread": spread,
                    "spread_z": z,
                    "reversion_strength": pair.reversion_strength,
                    "both_legs_required": True,
                },
                reason=(
                    f"the {pair.left_symbol}/{pair.right_symbol} spread is {abs(z):.2f} standard "
                    f"deviations {'wide' if z > 0 else 'narrow'} at a hedge ratio of "
                    f"{pair.hedge_ratio:.4f}; long {legs.long_symbol} and short "
                    f"{legs.short_symbol}, which cancels market direction. Reverted "
                    f"{confidence.value:.0%} of the time "
                    f"({'measured' if confidence.is_fitted else 'the prior'})"
                ),
                now_ns=self._now_ns,
            ),
            FIRED,
        )


def describe_spreads(detector: SpreadReversionDetector) -> dict:
    return {
        "part_id": PART_ID,
        "tests": detector.standing.tests,
        "candidates": detector.standing.candidates,
        "not_stretched": detector.standing.not_stretched,
        "still_stretched": detector.standing.still_stretched,
        "pair_not_cointegrated": detector.standing.not_cointegrated,
        "no_prices": detector.standing.no_prices,
        "stale_leg": detector.standing.stale_leg,
        "outcomes_learned": detector.standing.outcomes_learned,
        "widest_z": detector.standing.widest_z,
        # A gauge, not a counter: how many pairs this part is holding that it
        # cannot trade. They arrive as retirements and are never tested again, so
        # they cost memory rather than a test per tick -- which is what they cost
        # before 2026-08-24, at 11,013 wasted tests a second.
        "untradeable_pairs_held": detector.standing.untradeable_pairs_held,
        # Whether the quote feed is doing the job it was added for. Refusals
        # falling while this stayed zero would mean they fell for some other
        # reason, and a wide quote refused is a market too thin to stand in for a
        # trade rather than a feed that failed.
        "leg_priced_from_a_quote": detector.standing.leg_priced_from_a_quote,
        "leg_quote_too_wide": detector.standing.leg_quote_too_wide,
        # Why a leg could not be priced, from the chooser's own counters. Without
        # these, a refusal rate that stays high is a fact with no diagnosis: a leg
        # that has no quote at all and one whose quote is itself too old are
        # different failures with different fixes, and they were indistinguishable
        # from outside on 2026-08-24.
        **(
            {}
            if detector._reference_price is None
            else {
                f"leg_{name}": value
                for name, value in detector._reference_price.describe().items()
            }
        ),
    }


def run_spread_reversion_detector(
    detector: SpreadReversionDetector, control_socket, read_pairs, publish_candidates,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        pairs = read_pairs(detector)
        candidates = []
        for pair in pairs:
            candidate, _ = detector.detect(pair)
            if candidate is not None:
                candidates.append(candidate)
        publish_candidates(tuple(candidates))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_spreads(detector),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A cointegrated pair is a level -- these two symbols hold together, until the
    finder says they no longer do -- so the latest verdict per pair is kept across
    ticks. A pair the finder retires arrives as a fresh verdict with a state that is
    no longer tradeable, and `detect` refuses it: a stretched spread on a retired
    pair is two symbols that have parted company, and it looks exactly like the best
    opportunity there has ever been.
    """
    from runtime.input_assembly import Batch, LatestByKey

    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    quotes = Batch(read=context.bus.reader("symbol-quote-frame"))
    pairs = LatestByKey(
        read=context.bus.reader("cointegrated-pair"),
        key_of=lambda pair: (pair.venue_id, pair.left_symbol, pair.right_symbol),
    )
    publish_candidates = context.bus.publisher_for("entry-candidate")

    price_staleness = price_staleness_from(context)
    # A quote may stand in for a trade only while its own spread is immaterial, and
    # what counts as material here is what counts as material everywhere in this
    # system: the round trip cost. One number, applied twice, rather than a second
    # threshold that could disagree with the first (RL-061).
    reference_price = ReferencePriceChooser(
        staleness=price_staleness,
        materiality_fraction=2 * context.number("taker_fee_rate"),
    )
    detector = SpreadReversionDetector(
        z_threshold=context.number("spread_reversion_z_threshold"),
        rearm_z=context.number("spread_reversion_rearm_z"),
        window_length=int(context.number("spread_reversion_window_length")),
        minimum_observations=int(context.number("spread_reversion_minimum_observations")),
        horizon_seconds=context.number("spread_reversion_horizon"),
        calibrator=SignalCalibrator(
            prior_hit_rate=context.number("signal_prior_hit_rate"),
            prior_weight=context.number("signal_prior_weight"),
            half_life_observations=context.number("signal_half_life_observations"),
            minimum_observations=int(context.number("signal_minimum_observations")),
        ),
        price_staleness=price_staleness,
        reference_price=reference_price,
        maximum_gap_seconds=context.number("price_series_maximum_gap_seconds"),
            gap_patience_multiple=context.number("price_gap_patience_multiple"),
    )

    def read_pairs(_detector):
        for trade in levels_in(trades.payloads()):
            detector.observe_price(
                trade.venue_id, trade.symbol, trade.price, trade.observed_at_ns
            )
            price_staleness.observe_price(
                trade.venue_id, trade.symbol, trade.price, trade.observed_at_ns
            )
        # Only pairs that can actually be traded. A retired pair stays in the
        # level store -- that is what tells this part it retired -- but testing it
        # again on every tick is work whose answer is known: `detect` would refuse
        # it immediately. Measured 2026-08-24, that refusal was 11,013 of this
        # part's 13,557 tests a second, and it grows as the square of the universe.
        #
        # `detect` keeps its own guard. A pair reaching it untradeable would be a
        # defect in this filter, and a stretched spread on a pair that has parted
        # company looks exactly like the best opportunity there has ever been.
        for quote in quote_levels_in(quotes.payloads()):
            # The venue's own stamp, and for a quote merged out of one-sided deltas
            # that is its stalest side's. Both sides travel so the chooser can see
            # how wide the market is before believing its mid.
            detector.observe_quote(
                quote.venue_id, quote.symbol,
                quote.bid_price, quote.ask_price, quote.observed_at_ns,
            )
        return detector.tradeable_pairs_among(pairs.values())

    return run_spread_reversion_detector(
        detector=detector,
        control_socket=context.control_socket,
        read_pairs=read_pairs,
        publish_candidates=publish_candidates,
        health_interval_seconds=context.health_interval_seconds,
        emit_health=context.emit_health,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
    )
