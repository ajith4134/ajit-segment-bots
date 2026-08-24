"""tail-mover-qualifier: which moves are worth joining, out of everything already moving.

The tailgater's whole premise is that a move already under way is easier to be
right about than one that has not started. Its whole risk is that by the time a
move is visible enough to join, the part of it worth having is gone.

So this part's job is not to find moves -- the scanner does that -- but to decide
which of them are still **joinable**, and the tests it applies are the ones that
separate the two:

- **The move must be real, not a print.** A single trade three deviations from
  the mean is a print; a move is a sequence. So a candidate qualifies on
  *sustained* displacement over several observations, never on one.
- **The move must have run, but not finished.** A move that has barely started
  might still reverse, and one that has gone further than this symbol's moves
  normally go is a move to be on the other side of. Both bounds come from the
  symbol's own recorded moves, never from a fixed percentage (RL-061).
- **It must be joinable at a price that leaves something.** A move whose spread
  and slippage cost more than what is left of it is not an opportunity.

**It never proposes a reversal.** The tailgater follows; a candidate whose
direction opposes the move under way is not a tailgating setup at all, and
letting one through here would turn this bot into a third directional opinion
with none of a directional bot's checks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import FROM_A_SCANNER_MOVE, LONG, FollowCandidate
from runtime.learned_estimator import QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "tail-mover-qualifier"
BOT = "profit-tailgating-bot"

PART_DECLARATION = PartDeclaration(
    part_id="tail-mover-qualifier",
    consumes=("entry-candidate", "market-data", "symbol-profile", "tail-setup-weight"),
    produces=("follow-candidate", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

QUALIFIED = "qualified"
NOT_A_CONTINUATION = "candidate-opposes-the-move-under-way"
NOT_SUSTAINED = "one-print-is-not-a-move"
BARELY_STARTED = "move-has-not-run-far-enough-to-be-established"
ALREADY_FINISHED = "move-has-gone-further-than-this-symbol-normally-goes"
COST_EXCEEDS_WHAT_IS_LEFT = "spread-costs-more-than-the-move-has-left"
SETUP_DISCOUNTED = "setup-weight-below-floor"


@dataclass
class QualifierStanding:
    candidates_seen: int = 0
    qualified: int = 0
    by_rejection: dict = field(default_factory=dict)
    by_source: dict = field(default_factory=dict)
    largest_move_joined: float = 0.0


class TailMoverQualifier:
    """Decides which moves already under way are still worth joining."""

    def __init__(
        self,
        window_length: int,
        minimum_observations_in_move: int,
        minimum_fraction_of_normal_move: float,
        maximum_fraction_of_normal_move: float,
        move_quantile: float,
        move_window: int,
        prior_normal_move_fraction: float,
        minimum_remaining_over_cost: float,
        default_setup_weight: float,
        minimum_setup_weight: float,
        maximum_gap_seconds: float | None = None,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < minimum_fraction_of_normal_move < maximum_fraction_of_normal_move:
            raise ValueError(
                "the joinable band is between having started and having finished; the floor "
                "must sit below the ceiling and both must be positive"
            )
        if minimum_observations_in_move < 2:
            raise ValueError("a move is a sequence; one observation cannot establish one")
        self._window_length = window_length
        self._minimum_observations = minimum_observations_in_move
        self._minimum_fraction = minimum_fraction_of_normal_move
        self._maximum_fraction = maximum_fraction_of_normal_move
        self._move_quantile = move_quantile
        self._move_window = move_window
        self._prior_normal_move = prior_normal_move_fraction
        self._minimum_remaining_over_cost = minimum_remaining_over_cost
        self._default_weight = default_setup_weight
        self._minimum_weight = minimum_setup_weight
        self._now_ns = now_ns
        # How long a symbol may be silent before its window is judged to have a
        # hole in it rather than a series. None means the caller stated no bound,
        # and this part does not invent one (RL-061).
        self._maximum_gap_seconds = maximum_gap_seconds
        self._prices: dict[tuple[str, str], RollingWindow] = {}
        self._normal_moves: dict[tuple[str, str], QuantileEstimator] = {}
        self._costs: dict[tuple[str, str], float] = {}
        self._weights: dict[str, float] = {}
        self.standing = QualifierStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        key = (venue_id, symbol)
        window = self._prices.get(key)
        if window is None:
            window = RollingWindow(
                length=self._window_length,
                maximum_gap_seconds=self._maximum_gap_seconds,
            )
            self._prices[key] = window
        window.observe(price, at_ns)

    def observe_completed_move(self, venue_id: str, symbol: str, move_fraction: float) -> None:
        """How far one finished move in this symbol actually went.

        This is what "normally" means here. A fixed percentage would call the
        same 2% move enormous in BTCUSDT and trivial in a new listing.
        """
        self._normal_move_for(venue_id, symbol).observe(abs(move_fraction))

    def observe_symbol_profile(self, venue_id: str, symbol: str, round_trip_cost_fraction: float) -> None:
        self._costs[(venue_id, symbol)] = round_trip_cost_fraction

    def observe_setup_weight(self, detector: str, weight: float) -> None:
        if weight < 0.0:
            raise ValueError("a negative weight would invert the detector rather than mute it")
        self._weights[detector] = weight

    def qualify(self, candidate) -> tuple[FollowCandidate | None, str]:
        self.standing.candidates_seen += 1
        key = (candidate.venue_id, candidate.symbol)

        weight = self._weights.get(candidate.detector, self._default_weight)
        if weight < self._minimum_weight:
            return None, self._reject(SETUP_DISCOUNTED)

        window = self._prices.get(key)
        if window is None or window.count < self._minimum_observations:
            return None, self._reject(NOT_SUSTAINED)

        move, observations = self._sustained_move(window, candidate.direction)
        if move is None:
            return None, self._reject(NOT_SUSTAINED)

        # A candidate pointing against the move under way is not a tailgating
        # setup. Letting one through would make this a third directional bot.
        if move <= 0:
            return None, self._reject(NOT_A_CONTINUATION)

        normal = self._normal_move_for(*key).estimate(
            self._move_quantile, minimum_observations=self._minimum_observations
        )
        done = move / normal.value if normal.value > 0 else 0.0

        if done < self._minimum_fraction:
            return None, self._reject(BARELY_STARTED)
        if done > self._maximum_fraction:
            return None, self._reject(ALREADY_FINISHED)

        cost = self._costs.get(key)
        remaining = max(0.0, normal.value - move)
        if cost is not None and cost > 0 and remaining < cost * self._minimum_remaining_over_cost:
            return None, self._reject(COST_EXCEEDS_WHAT_IS_LEFT)

        self.standing.qualified += 1
        self.standing.by_source[FROM_A_SCANNER_MOVE] = (
            self.standing.by_source.get(FROM_A_SCANNER_MOVE, 0) + 1
        )
        self.standing.largest_move_joined = max(self.standing.largest_move_joined, move)

        return (
            FollowCandidate(
                bot=BOT,
                source=FROM_A_SCANNER_MOVE,
                venue_id=candidate.venue_id,
                symbol=candidate.symbol,
                direction=candidate.direction,
                move_so_far=move,
                move_normal=normal.value,
                observations_in_move=observations,
                entry_cost_fraction=cost,
                setup_weight=weight,
                detector=candidate.detector,
                evidence=dict(candidate.evidence),
                reason=(
                    f"{candidate.symbol} has moved {move:.2%} over {observations} observations "
                    f"in the direction {candidate.detector} called, which is {done:.0%} of the "
                    f"{normal.value:.2%} a move in this symbol normally covers "
                    f"({'measured' if normal.is_fitted else 'the setting, not yet measured'}); "
                    f"inside the {self._minimum_fraction:.0%}-{self._maximum_fraction:.0%} band "
                    f"where a move has established itself and not yet finished"
                    + (
                        f", leaving {remaining:.2%} against a {cost:.2%} round trip"
                        if cost is not None
                        else ""
                    )
                ),
                qualified_at_ns=self._now_ns(),
            ),
            QUALIFIED,
        )

    def _sustained_move(self, window: RollingWindow, direction: str) -> tuple[float | None, int]:
        """How far price has moved the candidate's way, over the run still going.

        Two conditions, and the second is the one that matters. The run is
        everything since price was last at this level, which is what "the move so
        far" means. But a run is only *sustained* if enough of its steps actually
        made progress -- a stretch of flat ticks ending in one large print sits
        entirely below the latest price and would otherwise read as a long move,
        when it is a print with a quiet period in front of it.

        Real moves are not monotone, so intervening flat or small adverse steps
        are allowed inside the run; they simply do not count toward the sequence
        that establishes it.
        """
        series = list(window.values)
        if len(series) < self._minimum_observations:
            return None, 0

        sign = 1.0 if direction == LONG else -1.0
        latest = series[-1]
        start_index = len(series) - 1
        for index in range(len(series) - 2, -1, -1):
            if sign * (latest - series[index]) <= 0:
                break
            start_index = index

        observations = len(series) - start_index
        progressing_steps = sum(
            1
            for earlier, later in zip(series[start_index:], series[start_index + 1 :])
            if sign * (later - earlier) > 0
        )
        if progressing_steps < self._minimum_observations:
            return None, observations

        start = series[start_index]
        if start <= 0:
            return None, observations
        return sign * (latest - start) / start, observations

    def _normal_move_for(self, venue_id: str, symbol: str) -> QuantileEstimator:
        key = (venue_id, symbol)
        estimator = self._normal_moves.get(key)
        if estimator is None:
            estimator = QuantileEstimator(
                window=self._move_window, prior=self._prior_normal_move
            )
            self._normal_moves[key] = estimator
        return estimator

    def _reject(self, reason: str) -> str:
        self.standing.by_rejection[reason] = self.standing.by_rejection.get(reason, 0) + 1
        return reason


def describe_qualifying(qualifier: TailMoverQualifier) -> dict:
    return {
        "part_id": PART_ID,
        "candidates_seen": qualifier.standing.candidates_seen,
        "qualified": qualifier.standing.qualified,
        "rejected_by_reason": dict(qualifier.standing.by_rejection),
        "qualified_by_source": dict(qualifier.standing.by_source),
        "largest_move_joined": qualifier.standing.largest_move_joined,
        "symbols_with_a_recorded_normal_move": len(qualifier._normal_moves),
    }


def run_tail_mover_qualifier(
    qualifier: TailMoverQualifier, control_socket, read_candidates_and_market,
    publish_follow_candidates, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        qualified = []
        for candidate in read_candidates_and_market(qualifier):
            follow, _ = qualifier.qualify(candidate)
            if follow is not None:
                qualified.append(follow)
        publish_follow_candidates(tuple(qualified))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    candidates = Batch(read=context.bus.reader("entry-candidate"))
    trades = Batch(read=context.bus.reader("market-data"))
    profiles = Batch(read=context.bus.reader("symbol-profile"))
    weights = Batch(read=context.bus.reader("tail-setup-weight"))
    publish_follow_candidates = context.bus.publisher_for("follow-candidate")
    qualifier = TailMoverQualifier(
        window_length=int(context.number("tail_window_length")),
        minimum_observations_in_move=int(context.number("tail_minimum_observations_in_move")),
        minimum_fraction_of_normal_move=context.number("tail_minimum_fraction_of_normal_move"),
        maximum_fraction_of_normal_move=context.number("tail_maximum_fraction_of_normal_move"),
        move_quantile=context.number("tail_move_quantile"),
        move_window=int(context.number("tail_move_window")),
        prior_normal_move_fraction=context.number("tail_prior_normal_move_fraction"),
        minimum_remaining_over_cost=context.number("tail_minimum_remaining_over_cost"),
        default_setup_weight=context.number("tail_default_setup_weight"),
        minimum_setup_weight=context.number("tail_minimum_setup_weight"),
    )
    round_trip = 2.0 * context.number("taker_fee_rate")

    def read_candidates_and_market(_qualifier):
        for trade in trades.payloads():
            qualifier.observe_price(
                trade.venue_id, trade.symbol, trade.price, trade.venue_time_ns
            )
        for profile in profiles.payloads():
            qualifier.observe_symbol_profile(profile.venue_id, profile.symbol, round_trip)
        for weight in weights.payloads():
            qualifier.observe_setup_weight(weight.detector, weight.weight)
        return tuple(candidates.payloads())

    def publish(items) -> None:
        if items:
            publish_follow_candidates(items)

    return run_tail_mover_qualifier(
        qualifier=qualifier,
        control_socket=context.control_socket,
        read_candidates_and_market=read_candidates_and_market,
        publish_follow_candidates=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
