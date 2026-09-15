"""winner-pattern-miner: what winners had that losers did not.

The half that gets skipped is the second one. Mining what winning trades have in
common produces "they were long", "they were in BTCUSDT", "they happened on a
weekday" -- all true, all present in the losers too, all useless. A pattern is only a
pattern if it separates.

So every candidate is scored as a lift: how much more often it appears in winners
than in losers. A condition present in 80% of winners and 80% of losers has a lift of
one and is discarded, however impressive the first number sounds alone.

Three further guards, because mining is where overfitting lives:

- **Conditions are declared, not searched.** The miner tests a fixed vocabulary of
  conditions the system already measures. Searching a space of derived features
  until something separates is guaranteed to succeed on any data set and means
  nothing.
- **Significance is required, not just lift.** Three winners out of four is a lift
  worth nothing at that sample size, and the part reports how many trades each side
  of the comparison had.
- **Clustered trades count once.** Ten correlated winners sharing a condition are
  one observation of it, and treating them as ten is how a pattern becomes
  "significant" without any new evidence.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.trade_decoding_types import WinnerPattern
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "winner-pattern-miner"

PART_DECLARATION = PartDeclaration(
    part_id="winner-pattern-miner",
    consumes=("trade-episode", "sequence-pattern", "outcome-significance"),
    produces=("winner-pattern", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

FOUND = "it-separates-winners-from-losers"
DOES_NOT_SEPARATE = "it-is-just-as-common-among-losers"
TOO_FEW_TRADES = "too-few-trades-on-one-side-of-the-comparison"
NOT_DECLARED = "this-condition-is-not-one-the-system-measures"


@dataclass(frozen=True)
class MiningOutcome:
    condition_name: str
    state: str
    pattern: WinnerPattern | None
    reason: str
    mined_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == FOUND and self.pattern is not None


@dataclass
class MinerStanding:
    conditions_tested: int = 0
    patterns_found: int = 0
    conditions_that_did_not_separate: int = 0
    refused_thin: int = 0
    refused_undeclared: int = 0
    winners_recorded: int = 0
    losers_recorded: int = 0
    clustered_trades_counted_once: int = 0


class WinnerPatternMiner:
    """Scores declared conditions by how much more often winners carry them."""

    def __init__(
        self,
        declared_conditions,
        minimum_each_side: int,
        minimum_lift: float,
        now_ns=time.time_ns,
    ) -> None:
        if not declared_conditions:
            raise ValueError(
                "searching a space of derived features until something separates is "
                "guaranteed to succeed on any data set and means nothing"
            )
        if minimum_each_side < 2:
            raise ValueError(
                "three winners out of four is a lift worth nothing at that sample size"
            )
        if minimum_lift <= 1.0:
            raise ValueError(
                "a lift of one means the condition is just as common among losers"
            )
        self._declared = set(declared_conditions)
        self._minimum_each_side = minimum_each_side
        self._minimum_lift = minimum_lift
        self._now_ns = now_ns
        self._winners: list = []
        self._losers: list = []
        self._cluster_of: dict[str, str] = {}
        self._sequence = 0
        self.standing = MinerStanding()

    def observe_cluster(self, trade_id: str, cluster_id: str) -> None:
        """Ten correlated winners sharing a condition are one observation of it."""
        self._cluster_of[trade_id] = cluster_id

    def observe_trade(self, trade_id: str, conditions: dict, was_a_winner: bool) -> None:
        undeclared = set(conditions) - self._declared
        if undeclared:
            raise ValueError(
                f"{sorted(undeclared)} are not conditions this system measures. A miner "
                f"given free rein over derived features finds patterns in noise"
            )
        entry = (trade_id, dict(conditions))
        if was_a_winner:
            self._winners.append(entry)
            self.standing.winners_recorded += 1
        else:
            self._losers.append(entry)
            self.standing.losers_recorded += 1

    def _distinct(self, trades, condition_name, value) -> int:
        """Counts clusters rather than trades, so one bet counts once."""
        seen = set()
        for trade_id, conditions in trades:
            if conditions.get(condition_name) == value:
                seen.add(self._cluster_of.get(trade_id, trade_id))
        return len(seen)

    def _population(self, trades) -> int:
        distinct = len(
            {self._cluster_of.get(trade_id, trade_id) for trade_id, _ in trades}
        )
        self.standing.clustered_trades_counted_once += len(trades) - distinct
        return distinct

    def mine(self, condition_name: str, value) -> MiningOutcome:
        self.standing.conditions_tested += 1
        if condition_name not in self._declared:
            self.standing.refused_undeclared += 1
            return self._outcome(
                condition_name, NOT_DECLARED, None,
                f"{condition_name} is not one of the conditions this system measures",
            )

        winners_total = self._population(self._winners)
        losers_total = self._population(self._losers)
        if winners_total < self._minimum_each_side or losers_total < self._minimum_each_side:
            self.standing.refused_thin += 1
            return self._outcome(
                condition_name, TOO_FEW_TRADES, None,
                f"{winners_total} winner(s) and {losers_total} loser(s) after clustering, "
                f"below the {self._minimum_each_side} needed on each side. A comparison "
                f"needs both halves",
            )

        winners_matching = self._distinct(self._winners, condition_name, value)
        losers_matching = self._distinct(self._losers, condition_name, value)

        winner_rate = winners_matching / winners_total
        loser_rate = losers_matching / losers_total
        # Lift: how much more often winners carry it. One means it is everywhere.
        lift = winner_rate / loser_rate if loser_rate > 0 else (
            float("inf") if winner_rate > 0 else 0.0
        )

        is_significant = (
            winners_matching >= self._minimum_each_side and lift >= self._minimum_lift
        )

        pattern = WinnerPattern(
            pattern_id=f"pattern-{condition_name}-{value}",
            conditions={condition_name: value},
            winners_matching=winners_matching,
            losers_matching=losers_matching,
            winners_total=winners_total,
            losers_total=losers_total,
            lift=lift,
            is_significant=is_significant,
            reason=(
                f"{winner_rate:.0%} of winners carry it against {loser_rate:.0%} of "
                f"losers, lift {lift:.2f}"
                + (
                    ". It separates"
                    if is_significant
                    else ". It is just as common among losers, so it describes the "
                         "population rather than the winners"
                )
            ),
            mined_at_ns=self._now_ns(),
        )

        if is_significant:
            self.standing.patterns_found += 1
            return self._outcome(condition_name, FOUND, pattern, pattern.reason)

        self.standing.conditions_that_did_not_separate += 1
        return self._outcome(condition_name, DOES_NOT_SEPARATE, pattern, pattern.reason)

    def _outcome(self, condition_name, state, pattern, reason) -> MiningOutcome:
        return MiningOutcome(
            condition_name=condition_name, state=state, pattern=pattern, reason=reason,
            mined_at_ns=self._now_ns(),
        )


def describe_pattern_mining(miner: WinnerPatternMiner) -> dict:
    return {
        "part_id": PART_ID,
        "conditions_tested": miner.standing.conditions_tested,
        "patterns_found": miner.standing.patterns_found,
        "conditions_that_did_not_separate": (
            miner.standing.conditions_that_did_not_separate
        ),
        "refused_thin_samples": miner.standing.refused_thin,
        "refused_undeclared_conditions": miner.standing.refused_undeclared,
        "winners_recorded": miner.standing.winners_recorded,
        "losers_recorded": miner.standing.losers_recorded,
        "clustered_trades_counted_once": miner.standing.clustered_trades_counted_once,
        "declared_conditions": sorted(miner._declared),
        "searches_for_conditions": False,
        "reports_winner_frequency_alone": False,
    }


def run_winner_pattern_miner(
    miner: WinnerPatternMiner, control_socket, read_conditions, publish_patterns,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for condition_name, value in read_conditions():
            outcome = miner.mine(condition_name, value)
            if outcome.is_usable:
                publish_patterns(outcome.pattern)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_pattern_mining(miner),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Each episode is a trade with its declared conditions; once per health
    interval every condition value seen is mined for lift.
    """
    import datetime
    import time as _time

    from runtime.input_assembly import Batch

    episodes = Batch(read=context.bus.reader("trade-episode"))
    patterns = Batch(read=context.bus.reader("sequence-pattern"))
    significances = Batch(read=context.bus.reader("outcome-significance"))
    publish_patterns = context.bus.publisher_for("winner-pattern")
    declared = tuple(str(name) for name in context.setting("winner_pattern_conditions").value)
    miner = WinnerPatternMiner(
        declared_conditions=declared,
        minimum_each_side=int(context.number("winner_pattern_minimum_each_side")),
        minimum_lift=context.number("winner_pattern_minimum_lift"),
    )
    values_seen: set[tuple[str, str]] = set()
    last_mine = [float("-inf")]

    def read_conditions():
        patterns.payloads()
        significances.payloads()
        for episode in episodes.payloads():
            trade_id = episode.trade_id
            opened = datetime.datetime.fromtimestamp(episode.opened_at_ns / 1e9, datetime.UTC)
            session = "asia" if opened.hour < 8 else "europe" if opened.hour < 16 else "america"
            conditions = {"regime": episode.regime, "session": session, "detector": episode.detector}
            kept = {name: value for name, value in conditions.items() if name in declared}
            miner.observe_trade(trade_id, kept, episode.realised > 0)
            for name, value in kept.items():
                values_seen.add((name, str(value)))
        now = _time.monotonic()
        if now - last_mine[0] < context.health_interval_seconds:
            return ()
        last_mine[0] = now
        return tuple(sorted(values_seen))

    return run_winner_pattern_miner(
        miner=miner,
        control_socket=context.control_socket,
        read_conditions=read_conditions,
        publish_patterns=lambda pattern: publish_patterns((pattern,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
