"""tail-copy-selector: joining a tracked trader's position, and the latency that decides it.

Copying is the tailgater's third source, and it is the one where being late is
not a degradation but a reversal of the edge. A trader whose entries are worth
copying is producing moves; by the time their position is visible and ours is
filled, some fraction of that move has happened. Above a certain fraction we are
not copying them -- we are providing their exit.

So this part does not ask "is this trader good". It asks two questions the
copy-score alone cannot answer:

1. **How much of their move is left by the time we could be in?** Measured from
   the observed copy latency and the symbol's speed, not assumed.
2. **Are we the last one in?** A position many followers have already copied has
   had its move consumed by the copying itself. The crowding reading exists for
   this, and the copy selector refuses rather than scoring low, because a small
   position in a crowded copy is exposed to the same exit as a large one.

**A trader's score does not travel between symbols.** Someone excellent in
majors and reckless in new listings has two records, and a single score over both
would let the second inherit the first's authority.

**It never copies a position it cannot exit independently.** Following someone in
is a decision; following them out is not available, because their exit is not
observable until it has happened.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import FROM_A_TRACKED_TRADER, LONG, SHORT, FollowCandidate
from runtime.learned_estimator import Estimate, QuantileEstimator, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "tail-copy-selector"
BOT = "profit-tailgating-bot"

PART_DECLARATION = PartDeclaration(
    part_id="tail-copy-selector",
    consumes=("external-position", "copy-score", "copy-latency"),
    produces=("follow-candidate", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

SELECTED = "selected"
TRADER_NOT_SCORED_HERE = "this-trader-has-no-record-in-this-symbol"
SCORE_TOO_LOW = "copy-score-below-floor"
TOO_LATE = "the-move-would-be-over-before-we-were-filled"
TOO_CROWDED = "too-many-followers-are-already-in-this"
NO_LATENCY_RECORD = "no-measured-latency-for-this-trader"
CANNOT_EXIT_INDEPENDENTLY = "no-exit-of-our-own-for-this-symbol"


@dataclass(frozen=True)
class ExternalPosition:
    """A tracked trader's position, as observed rather than as reported."""

    trader_id: str
    venue_id: str
    symbol: str
    direction: str
    notional: float
    opened_at_ns: int
    followers_observed: int
    is_exitable_by_us: bool


@dataclass
class TraderRecord:
    """What copying this trader in this symbol has actually produced.

    Per symbol on purpose: someone excellent in majors and reckless in new
    listings has two records, and one score over both lets the second borrow the
    first's authority.
    """

    hit_rate: RateEstimator
    latency_seconds: QuantileEstimator
    copies: int = 0


@dataclass
class SelectorStanding:
    positions_seen: int = 0
    selected: int = 0
    by_rejection: dict = field(default_factory=dict)
    traders_tracked: int = 0
    slowest_latency_accepted: float = 0.0


class TailCopySelector:
    """Selects tracked positions worth joining, given how late we would be."""

    def __init__(
        self,
        minimum_copy_score: float,
        maximum_fraction_of_move_lost_to_latency: float,
        maximum_followers: int,
        latency_quantile: float,
        latency_window: int,
        prior_latency_seconds: float,
        prior_copy_hit_rate: float,
        prior_weight: float,
        half_life_observations: float,
        minimum_observations: int,
        default_setup_weight: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < maximum_fraction_of_move_lost_to_latency < 1.0:
            raise ValueError(
                "past some fraction of the move we are not copying the trader, we are "
                "providing their exit; that fraction must be inside (0, 1)"
            )
        if maximum_followers < 1:
            raise ValueError("a copy nobody else may take is not a bound worth stating")
        self._minimum_score = minimum_copy_score
        self._maximum_lost = maximum_fraction_of_move_lost_to_latency
        self._maximum_followers = maximum_followers
        self._latency_quantile = latency_quantile
        self._latency_window = latency_window
        self._prior_latency = prior_latency_seconds
        self._prior_hit_rate = prior_copy_hit_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._minimum = minimum_observations
        self._default_weight = default_setup_weight
        self._now_ns = now_ns
        self._records: dict[tuple[str, str, str], TraderRecord] = {}
        self._move_speed: dict[tuple[str, str], float] = {}
        self.standing = SelectorStanding()

    def observe_copy_outcome(self, trader_id: str, venue_id: str, symbol: str, was_profitable: bool) -> None:
        record = self._record_for(trader_id, venue_id, symbol)
        record.hit_rate.observe(was_profitable)
        record.copies += 1

    def observe_copy_latency(self, trader_id: str, venue_id: str, symbol: str, seconds: float) -> None:
        """How long it actually took from their fill being visible to ours landing."""
        if seconds < 0:
            raise ValueError("latency cannot be negative")
        self._record_for(trader_id, venue_id, symbol).latency_seconds.observe(seconds)

    def observe_move_speed(self, venue_id: str, symbol: str, fraction_per_second: float) -> None:
        """How fast this symbol's moves run, so latency can be priced in move terms."""
        self._move_speed[(venue_id, symbol)] = abs(fraction_per_second)

    def copy_score(self, trader_id: str, venue_id: str, symbol: str) -> Estimate:
        return self._record_for(trader_id, venue_id, symbol).hit_rate.estimate(self._minimum)

    def expected_latency(self, trader_id: str, venue_id: str, symbol: str) -> Estimate:
        return self._record_for(trader_id, venue_id, symbol).latency_seconds.estimate(
            self._latency_quantile, minimum_observations=self._minimum
        )

    def select(self, position: ExternalPosition, expected_move_fraction: float) -> tuple[FollowCandidate | None, str]:
        self.standing.positions_seen += 1
        key = (position.trader_id, position.venue_id, position.symbol)

        if not position.is_exitable_by_us:
            # Following someone in is a decision; following them out is not
            # available, because their exit is not observable until it has
            # happened.
            return None, self._reject(CANNOT_EXIT_INDEPENDENTLY)

        record = self._records.get(key)
        if record is None or record.copies == 0:
            return None, self._reject(TRADER_NOT_SCORED_HERE)

        score = self.copy_score(*key)
        if score.value < self._minimum_score:
            return None, self._reject(SCORE_TOO_LOW)

        latency = self.expected_latency(*key)
        speed = self._move_speed.get((position.venue_id, position.symbol))
        if speed is None:
            return None, self._reject(NO_LATENCY_RECORD)

        lost = latency.value * speed
        lost_fraction = lost / expected_move_fraction if expected_move_fraction > 0 else 1.0
        if lost_fraction > self._maximum_lost:
            return None, self._reject(TOO_LATE)

        if position.followers_observed > self._maximum_followers:
            # Refused rather than scored low: a small position in a crowded copy
            # is exposed to the same exit as a large one.
            return None, self._reject(TOO_CROWDED)

        self.standing.selected += 1
        self.standing.traders_tracked = len({trader for trader, _, _ in self._records})
        self.standing.slowest_latency_accepted = max(
            self.standing.slowest_latency_accepted, latency.value
        )

        return (
            FollowCandidate(
                bot=BOT,
                source=FROM_A_TRACKED_TRADER,
                venue_id=position.venue_id,
                symbol=position.symbol,
                direction=position.direction,
                move_so_far=lost,
                move_normal=expected_move_fraction,
                observations_in_move=record.copies,
                entry_cost_fraction=None,
                setup_weight=self._default_weight,
                detector=f"copy:{position.trader_id}",
                evidence={
                    "trader_id": position.trader_id,
                    "copy_score": score.value,
                    "copy_score_is_measured": score.is_fitted,
                    "expected_latency_seconds": latency.value,
                    "fraction_of_move_lost_to_latency": lost_fraction,
                    "followers_observed": position.followers_observed,
                },
                reason=(
                    f"{position.trader_id} is {score.value:.0%} right in {position.symbol} over "
                    f"{record.copies} copies "
                    f"({'measured' if score.is_fitted else 'still pulled toward the prior'}); "
                    f"at {latency.value:.1f}s of observed latency and this symbol's speed we "
                    f"would give up {lost_fraction:.0%} of the move, inside the "
                    f"{self._maximum_lost:.0%} past which we would be providing their exit "
                    f"rather than copying their entry; {position.followers_observed} follower(s) "
                    f"already in, under the {self._maximum_followers} that makes a copy its own "
                    f"crowd"
                ),
                qualified_at_ns=self._now_ns(),
            ),
            SELECTED,
        )

    def _record_for(self, trader_id: str, venue_id: str, symbol: str) -> TraderRecord:
        key = (trader_id, venue_id, symbol)
        record = self._records.get(key)
        if record is None:
            record = TraderRecord(
                hit_rate=RateEstimator(
                    prior=self._prior_hit_rate, prior_weight=self._prior_weight,
                    half_life_observations=self._half_life,
                ),
                latency_seconds=QuantileEstimator(
                    window=self._latency_window, prior=self._prior_latency
                ),
            )
            self._records[key] = record
        return record

    def _reject(self, reason: str) -> str:
        self.standing.by_rejection[reason] = self.standing.by_rejection.get(reason, 0) + 1
        return reason


def describe_copy_selection(selector: TailCopySelector) -> dict:
    return {
        "part_id": PART_ID,
        "positions_seen": selector.standing.positions_seen,
        "selected": selector.standing.selected,
        "rejected_by_reason": dict(selector.standing.by_rejection),
        "trader_symbol_records": len(selector._records),
        "slowest_latency_accepted_seconds": selector.standing.slowest_latency_accepted,
    }


def run_tail_copy_selector(
    selector: TailCopySelector, control_socket, read_external_positions,
    publish_follow_candidates, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        selected = []
        for position, expected_move in read_external_positions(selector):
            candidate, _ = selector.select(position, expected_move)
            if candidate is not None:
                selected.append(candidate)
        publish_follow_candidates(tuple(selected))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_copy_selection(selector),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    An external position from the on-chain reader is turned into this part's
    own shape; no on-chain reader runs in phase 1, so none arrives.
    """
    from runtime.input_assembly import Batch

    externals = Batch(read=context.bus.reader("external-position"))
    scores = Batch(read=context.bus.reader("copy-score"))
    latencies = Batch(read=context.bus.reader("copy-latency"))
    publish_follow_candidates = context.bus.publisher_for("follow-candidate")
    selector = TailCopySelector(
        minimum_copy_score=context.number("tail_copy_minimum_score"),
        maximum_fraction_of_move_lost_to_latency=context.number("tail_copy_maximum_fraction_lost_to_latency"),
        maximum_followers=int(context.number("tail_copy_maximum_followers")),
        latency_quantile=context.number("tail_copy_latency_quantile"),
        latency_window=int(context.number("learning_window")),
        prior_latency_seconds=context.number("tail_copy_prior_latency"),
        prior_copy_hit_rate=context.number("learning_prior_hit_rate"),
        prior_weight=context.number("learning_prior_weight"),
        half_life_observations=context.number("learning_half_life_observations"),
        minimum_observations=int(context.number("learning_minimum_observations")),
        default_setup_weight=context.number("tail_default_setup_weight"),
    )

    def read_external_positions(_selector):
        for score in scores.payloads():
            if score.their_return is not None and score.symbol:
                selector.observe_copy_outcome(score.trader_id, "", score.symbol, score.copyable_return is not None and score.copyable_return > 0)
        for latency in latencies.payloads():
            selector.observe_move_speed(latency.venue_id, latency.symbol, latency.adverse_move_fraction / max(latency.detection_delay_seconds, 1e-9))
        jobs = []
        for external in externals.payloads():
            if external.notional is None or external.opened_at_ns is None:
                continue
            position = ExternalPosition(
                trader_id=external.trader_id, venue_id=external.venue_id, symbol=external.symbol,
                direction=external.side, notional=external.notional, opened_at_ns=external.opened_at_ns,
                followers_observed=0, is_exitable_by_us=external.is_full_book,
            )
            jobs.append((position, context.number("tail_prior_normal_move_fraction")))
        return tuple(jobs)

    def publish(items) -> None:
        if items:
            publish_follow_candidates(items)

    return run_tail_copy_selector(
        selector=selector,
        control_socket=context.control_socket,
        read_external_positions=read_external_positions,
        publish_follow_candidates=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
