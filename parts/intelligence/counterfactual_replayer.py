"""counterfactual-replayer: what the other choice would have produced.

Every decision this system makes has alternatives that are never observed: the
opinion that was overruled, the trade that was not taken, standing aside. Without
them, the record shows only what happened, and a system learning from that
learns that whatever it did was the option available.

This replays the alternatives against the market that actually followed:

- **The overruled opinion.** The bear bot wanted short and was overruled; what
  would that have made? The arbiter's conflict rulings are only checkable
  against this.
- **Standing aside.** The alternative to every trade, and the one with the
  highest bar to beat: a trade must beat zero after costs, and a system that
  never computes zero cannot tell a small edge from none.
- **The same trade at a different size or moment.** The entry timer and the size
  hint both make claims that only a counterfactual can test.

**Costs are charged to the counterfactual too.** A replay that ignores the spread
makes every untaken trade look better than the taken one, and the system would
learn to blame execution for what was never an edge.

**A counterfactual is bounded by what was observable.** It replays against the
tape, and where the tape has a gap it says so rather than interpolating -- a
counterfactual that fills its own gaps is a simulation of a market that suited
it.

**It never touches a live decision.** This runs after the fact, on closed
episodes, and its output is evidence rather than instruction.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.price_frames import levels_in
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "counterfactual-replayer"

PART_DECLARATION = PartDeclaration(
    part_id="counterfactual-replayer",
    consumes=("directional-opinion", "trade-intent", "symbol-price-frame", "trade-episode"),
    produces=("counterfactual-outcome", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

REPLAYED = "replayed"
NO_TAPE = "the-tape-does-not-cover-the-period-this-decision-spanned"
TAPE_HAS_A_GAP = "the-tape-has-a-gap-inside-the-period"
NOTHING_TO_COMPARE = "no-alternative-was-available-to-replay"

STOOD_ASIDE = "standing-aside"
THE_OVERRULED_OPINION = "the-opinion-that-was-overruled"
A_DIFFERENT_SIZE = "the-same-trade-at-a-different-size"
A_DIFFERENT_MOMENT = "the-same-trade-entered-later"

LONG = "long"
SHORT = "short"


@dataclass(frozen=True)
class CounterfactualOutcome:
    """What an alternative would have produced, over the same period, after costs."""

    venue_id: str
    symbol: str
    alternative: str
    state: str
    realised_fraction: float | None
    actual_fraction: float
    difference: float | None
    costs_charged: float
    period_seconds: float
    tape_points: int
    reason: str
    replayed_at_ns: int

    @property
    def the_alternative_was_better(self) -> bool:
        return self.difference is not None and self.difference < 0

    @property
    def is_usable(self) -> bool:
        return self.state == REPLAYED


@dataclass
class ReplayerStanding:
    replays: int = 0
    replayed: int = 0
    refused_no_tape: int = 0
    refused_tape_gap: int = 0
    alternatives_that_were_better: int = 0
    by_alternative: dict = field(default_factory=dict)
    largest_difference_seen: float | None = None


class CounterfactualReplayer:
    """Replays the alternatives against the tape that actually followed."""

    def __init__(
        self,
        round_trip_cost_fraction: float,
        maximum_gap_seconds: float,
        minimum_tape_points: int,
        now_ns=time.time_ns,
    ) -> None:
        if round_trip_cost_fraction < 0:
            raise ValueError("costs cannot be negative")
        if maximum_gap_seconds <= 0:
            raise ValueError(
                "a replay that fills its own gaps is a simulation of a market that suited it"
            )
        self._cost = round_trip_cost_fraction
        self._maximum_gap_ns = int(maximum_gap_seconds * 1e9)
        self._minimum_points = minimum_tape_points
        self._now_ns = now_ns
        self._tape: dict[tuple[str, str], list] = {}
        self.standing = ReplayerStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        """One tape point. Kept in arrival order so a replay reads what happened."""
        self._tape.setdefault((venue_id, symbol), []).append((at_ns, price))

    def points_between(self, venue_id: str, symbol: str, start_ns: int, end_ns: int) -> tuple:
        return tuple(
            (at_ns, price)
            for at_ns, price in self._tape.get((venue_id, symbol), ())
            if start_ns <= at_ns <= end_ns
        )

    def replay(
        self, episode, alternative: str, side: str | None,
        start_ns: int, end_ns: int, size_multiple: float = 1.0,
    ) -> CounterfactualOutcome:
        """One alternative, over the period the real decision spanned."""
        self.standing.replays += 1
        points = self.points_between(episode.venue_id, episode.symbol, start_ns, end_ns)
        period = (end_ns - start_ns) / 1e9

        if len(points) < self._minimum_points:
            self.standing.refused_no_tape += 1
            return self._outcome(
                episode, alternative, NO_TAPE, None, 0.0, period, len(points),
                f"{len(points)} tape point(s) of the {self._minimum_points} needed over "
                f"{period:.0f}s; a counterfactual over a period the tape does not cover is a "
                f"guess about what would have happened",
            )

        gap = self._largest_gap(points)
        if gap > self._maximum_gap_ns:
            self.standing.refused_tape_gap += 1
            return self._outcome(
                episode, alternative, TAPE_HAS_A_GAP, None, 0.0, period, len(points),
                f"the tape has a {gap / 1e9:.0f}s gap inside this period, past the "
                f"{self._maximum_gap_ns / 1e9:.0f}s this replayer will bridge. Interpolating "
                f"across it would simulate a market that suited the alternative",
            )

        if alternative == STOOD_ASIDE:
            # The alternative to every trade, and the one with the highest bar:
            # zero after costs. A system that never computes zero cannot tell a
            # small edge from none.
            realised = 0.0
        elif side is None:
            self.standing.refused_no_tape += 1
            return self._outcome(
                episode, alternative, NOTHING_TO_COMPARE, None, 0.0, period, len(points),
                "no side was given, so there is no position to replay",
            )
        else:
            entry = points[0][1]
            exit_price = points[-1][1]
            if entry <= 0:
                return self._outcome(
                    episode, alternative, NO_TAPE, None, 0.0, period, len(points),
                    "the tape's first price is not usable as an entry",
                )
            move = (exit_price - entry) / entry
            realised = (move if side == LONG else -move) * size_multiple
            # Charged to the counterfactual too: a replay that ignores the spread
            # makes every untaken trade look better, and the system learns to
            # blame execution for what was never an edge.
            realised -= self._cost * abs(size_multiple)

        difference = episode.realised_fraction - realised
        if (
            self.standing.largest_difference_seen is None
            or abs(difference) > abs(self.standing.largest_difference_seen)
        ):
            self.standing.largest_difference_seen = difference
        if difference < 0:
            self.standing.alternatives_that_were_better += 1

        self.standing.replayed += 1
        self.standing.by_alternative[alternative] = (
            self.standing.by_alternative.get(alternative, 0) + 1
        )

        return self._outcome(
            episode, alternative, REPLAYED, realised, difference, period, len(points),
            f"{alternative} would have made {realised:+.3%} over {period:.0f}s against the "
            f"{episode.realised_fraction:+.3%} actually made -- "
            + (
                f"the alternative was better by {-difference:.3%}"
                if difference < 0
                else f"the decision beat it by {difference:.3%}"
            )
            + (
                f", with {self._cost:.3%} of round-trip cost charged to the alternative"
                if alternative != STOOD_ASIDE
                else ", and standing aside costs nothing, which is the bar every trade must beat"
            ),
        )

    def replay_all(self, episode, alternatives) -> tuple:
        return tuple(
            self.replay(episode, alternative, side, start_ns, end_ns, size)
            for alternative, side, start_ns, end_ns, size in alternatives
        )

    def _largest_gap(self, points) -> int:
        return max(
            (later[0] - earlier[0] for earlier, later in zip(points, points[1:])), default=0
        )

    def release(self, venue_id: str, symbol: str) -> None:
        """T-3: the tape a replayer holds is the largest thing in it."""
        self._tape.pop((venue_id, symbol), None)

    def _outcome(
        self, episode, alternative, state, realised, difference, period, points, reason
    ) -> CounterfactualOutcome:
        return CounterfactualOutcome(
            venue_id=episode.venue_id,
            symbol=episode.symbol,
            alternative=alternative,
            state=state,
            realised_fraction=realised,
            actual_fraction=episode.realised_fraction,
            difference=difference if realised is not None else None,
            costs_charged=self._cost if alternative != STOOD_ASIDE else 0.0,
            period_seconds=period,
            tape_points=points,
            reason=reason,
            replayed_at_ns=self._now_ns(),
        )


def describe_replaying(replayer: CounterfactualReplayer) -> dict:
    return {
        "part_id": PART_ID,
        "replays": replayer.standing.replays,
        "replayed": replayer.standing.replayed,
        "refused_tape_does_not_cover_it": replayer.standing.refused_no_tape,
        "refused_for_a_gap_in_the_tape": replayer.standing.refused_tape_gap,
        "alternatives_that_were_better": replayer.standing.alternatives_that_were_better,
        "by_alternative": dict(sorted(replayer.standing.by_alternative.items())),
        "largest_difference_seen": replayer.standing.largest_difference_seen,
        "round_trip_cost_charged": replayer._cost,
        "touches_live_decisions": False,
    }


def run_counterfactual_replayer(
    replayer: CounterfactualReplayer, control_socket, read_episodes, publish_outcomes,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        outcomes = []
        for episode, alternatives in read_episodes(replayer):
            outcomes.extend(replayer.replay_all(episode, alternatives))
        publish_outcomes(tuple(outcomes))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_replaying(replayer),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Every print is kept, because a replay is read off the tape between the
    moments a decision spanned. For each closed episode two alternatives are
    replayed: standing aside over the same period, and the opinion that was
    overruled -- the latest opinion on that venue and symbol whose side
    differed from the action taken. Intents are read for the horizon the
    decision claimed.
    """
    from dataclasses import dataclass

    from runtime.input_assembly import Batch, LatestByKey

    @dataclass(frozen=True)
    class ReplayableEpisode:
        venue_id: str
        symbol: str
        realised_fraction: float
        side: str | None

    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    opinions = LatestByKey(read=context.bus.reader("directional-opinion"), key_of=lambda o: (o.bot, o.venue_id, o.symbol))
    intents = LatestByKey(read=context.bus.reader("trade-intent"), key_of=lambda i: (i.venue_id, i.symbol))
    episodes = Batch(read=context.bus.reader("trade-episode"))
    publish_outcomes = context.bus.publisher_for("counterfactual-outcome")
    replayer = CounterfactualReplayer(
        round_trip_cost_fraction=context.number("regret_cost_fraction"),
        maximum_gap_seconds=context.number("counterfactual_maximum_gap_seconds"),
        minimum_tape_points=int(context.number("counterfactual_minimum_tape_points")),
    )

    def side_of(action: str) -> str | None:
        lowered = str(action).lower()
        if LONG in lowered or "buy" in lowered:
            return LONG
        if SHORT in lowered or "sell" in lowered:
            return SHORT
        return None

    def read_episodes(_replayer):
        for trade in levels_in(trades.payloads()):
                replayer.observe_price(trade.venue_id, trade.symbol, trade.price, trade.observed_at_ns)
        intents.mapping()
        held = opinions.mapping()
        jobs = []
        for episode in episodes.payloads():
            conditions = episode.conditions if isinstance(episode.conditions, dict) else {}
            taken = side_of(episode.action)
            replayable = ReplayableEpisode(
                venue_id=episode.venue_id, symbol=episode.symbol,
                realised_fraction=float(conditions.get("realised_fraction", episode.realised) or 0.0),
                side=taken,
            )
            alternatives = [(STOOD_ASIDE, None, int(episode.opened_at_ns), int(episode.closed_at_ns), 1.0)]
            overruled = [
                o for (bot, venue_id, symbol), o in held.items()
                if (venue_id, symbol) == (episode.venue_id, episode.symbol) and o.side in (LONG, SHORT) and o.side != taken
            ]
            if overruled:
                alternatives.append((THE_OVERRULED_OPINION, overruled[0].side, int(episode.opened_at_ns), int(episode.closed_at_ns), 1.0))
            jobs.append((replayable, tuple(alternatives)))
        return tuple(jobs)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_outcomes(kept)

    return run_counterfactual_replayer(
        replayer=replayer,
        control_socket=context.control_socket,
        read_episodes=read_episodes,
        publish_outcomes=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
