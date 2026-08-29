"""sequence-pattern-miner: what is true about trades in order.

Per-trade analysis destroys order, and order carries the failures that hurt most:
losses that arrive together rather than spread out, size that grows after a win,
quality that falls as a session goes on, a strategy that only works when it has just
worked. None of these are visible in an aggregate, and all of them are visible in a
sequence.

The patterns here are declared shapes rather than a search, because a search over
sequences finds structure in random walks reliably enough to be dangerous:

- **Streaks** -- are losses clustered more than chance would produce? Measured
  against the runs expected from the same win rate in random order, not against an
  intuition about what looks streaky.
- **Size drift** -- does position size grow after wins or shrink after losses? This
  is the mechanical form of the behaviour that turns a positive edge negative.
- **Time-of-session decay** -- does trade quality fall later in a session? A real and
  common effect, and one that a per-trade view cannot see at all.
- **Conditioning on the previous outcome** -- is the next trade better after a win
  than after a loss? If it is, the strategy is not what it appears to be.

Every finding is tested against a shuffled version of the same trades, which is the
only honest baseline: a pattern that survives shuffling is not a pattern about order.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from runtime.trade_decoding_types import (
    SequencePattern, SEQUENCE_KINDS, STREAKS, SIZE_DRIFT, SESSION_DECAY,
    OUTCOME_CONDITIONING,
)
from runtime.level_publishing import LevelPublisherByKey
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "sequence-pattern-miner"

PART_DECLARATION = PartDeclaration(
    part_id="sequence-pattern-miner",
    consumes=("trade-episode", "closed-trade"),
    produces=("sequence-pattern", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

FOUND = "found"
NOT_PRESENT = "not-present-in-this-sequence"
TOO_FEW_TRADES = "too-few-trades-for-order-to-mean-anything"
SURVIVES_SHUFFLING = "the-same-effect-appears-when-the-order-is-destroyed"


@dataclass(frozen=True)
class SequenceOutcome:
    kind: str
    state: str
    pattern: SequencePattern | None
    effect: float | None
    shuffled_effect: float | None
    reason: str
    found_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == FOUND and self.pattern is not None


@dataclass
class SequenceMinerStanding:
    trades_recorded: int = 0
    patterns_tested: int = 0
    patterns_found: int = 0
    not_present: int = 0
    refused_thin: int = 0
    rejected_by_shuffling: int = 0


class SequencePatternMiner:
    """Tests declared order-dependent shapes against a shuffled baseline."""

    def __init__(
        self,
        minimum_trades: int,
        effect_threshold: float,
        shuffle_margin: float,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_trades < 4:
            raise ValueError(
                "order needs a sequence: with three trades every arrangement looks like "
                "a pattern"
            )
        if effect_threshold <= 0 or shuffle_margin <= 0:
            raise ValueError(
                "a pattern must exceed both a size threshold and its own shuffled "
                "baseline, or a random walk qualifies"
            )
        self._minimum_trades = minimum_trades
        self._effect_threshold = effect_threshold
        self._shuffle_margin = shuffle_margin
        self._now_ns = now_ns
        self._trades: list = []
        self._sequence = 0
        self.standing = SequenceMinerStanding()

    def observe_trade(
        self, trade_id: str, realised: float, notional: float, opened_at_ns: int,
        seconds_into_session: float,
    ) -> None:
        self._trades.append(
            {
                "trade_id": trade_id, "realised": realised, "notional": notional,
                "opened_at_ns": opened_at_ns,
                "seconds_into_session": seconds_into_session,
            }
        )
        self.standing.trades_recorded += 1

    def _ordered(self) -> list:
        return sorted(self._trades, key=lambda trade: trade["opened_at_ns"])

    def _shuffled(self, trades) -> list:
        """A deterministic reordering: no randomness is available in a part (RL-063
        keeps tests on real data, and a seeded shuffle would be a hidden constant).
        Reversing and interleaving destroys adjacency without inventing anything."""
        first_half = trades[::2]
        second_half = trades[1::2][::-1]
        return second_half + first_half

    def _longest_run_of_losses(self, trades) -> int:
        longest = current = 0
        for trade in trades:
            if trade["realised"] < 0:
                current += 1
                longest = max(longest, current)
            else:
                current = 0
        return longest

    def _size_drift(self, trades) -> float:
        after_win = [
            trades[index]["notional"]
            for index in range(1, len(trades))
            if trades[index - 1]["realised"] > 0
        ]
        after_loss = [
            trades[index]["notional"]
            for index in range(1, len(trades))
            if trades[index - 1]["realised"] < 0
        ]
        if not after_win or not after_loss:
            return 0.0
        mean_after_win = statistics.mean(after_win)
        mean_after_loss = statistics.mean(after_loss)
        base = (mean_after_win + mean_after_loss) / 2.0
        return (mean_after_win - mean_after_loss) / base if base else 0.0

    def _session_decay(self, trades) -> float:
        halfway = statistics.median(trade["seconds_into_session"] for trade in trades)
        early = [
            trade["realised"] for trade in trades
            if trade["seconds_into_session"] <= halfway
        ]
        late = [
            trade["realised"] for trade in trades
            if trade["seconds_into_session"] > halfway
        ]
        if not early or not late:
            return 0.0
        return statistics.mean(early) - statistics.mean(late)

    def _outcome_conditioning(self, trades) -> float:
        after_win = [
            trades[index]["realised"]
            for index in range(1, len(trades))
            if trades[index - 1]["realised"] > 0
        ]
        after_loss = [
            trades[index]["realised"]
            for index in range(1, len(trades))
            if trades[index - 1]["realised"] < 0
        ]
        if not after_win or not after_loss:
            return 0.0
        return statistics.mean(after_win) - statistics.mean(after_loss)

    def _measure(self, kind, trades) -> float:
        if kind == STREAKS:
            return float(self._longest_run_of_losses(trades))
        if kind == SIZE_DRIFT:
            return self._size_drift(trades)
        if kind == SESSION_DECAY:
            return self._session_decay(trades)
        return self._outcome_conditioning(trades)

    def mine(self, kind: str) -> SequenceOutcome:
        if kind not in SEQUENCE_KINDS:
            raise ValueError(
                f"{kind!r} is not a declared shape. A search over sequences finds "
                f"structure in random walks reliably enough to be dangerous"
            )
        self.standing.patterns_tested += 1
        trades = self._ordered()

        if len(trades) < self._minimum_trades:
            self.standing.refused_thin += 1
            return self._outcome(
                kind, TOO_FEW_TRADES, None, None, None,
                f"{len(trades)} trade(s), below the {self._minimum_trades} needed for "
                f"order to mean anything",
            )

        effect = self._measure(kind, trades)
        shuffled = self._measure(kind, self._shuffled(trades))

        if abs(effect) < self._effect_threshold:
            self.standing.not_present += 1
            return self._outcome(
                kind, NOT_PRESENT, None, effect, shuffled,
                f"{kind} measures {effect:+.3f}, inside the "
                f"{self._effect_threshold:.3f} threshold",
            )

        # The only honest baseline: a pattern that survives shuffling is not about order.
        if abs(effect) - abs(shuffled) < self._shuffle_margin:
            self.standing.rejected_by_shuffling += 1
            return self._outcome(
                kind, SURVIVES_SHUFFLING, None, effect, shuffled,
                f"{kind} measures {effect:+.3f} in order and {shuffled:+.3f} with the "
                f"order destroyed. The same effect appears without the sequence, so it is "
                f"not a pattern about order",
            )

        self._sequence += 1
        self.standing.patterns_found += 1
        return self._outcome(
            kind, FOUND,
            SequencePattern(
                pattern_id=f"sequence-{self._sequence}",
                description={
                    STREAKS: "losing trades arrive together rather than spread out",
                    SIZE_DRIFT: "position size changes depending on the previous outcome",
                    SESSION_DECAY: "trade quality falls later in the session",
                    OUTCOME_CONDITIONING: (
                        "the next trade's result depends on the last one's"
                    ),
                }[kind],
                kind=kind,
                trades_examined=len(trades),
                occurrences=1,
                effect=effect,
                is_significant=True,
                reason=(
                    f"{effect:+.3f} in order against {shuffled:+.3f} shuffled, over "
                    f"{len(trades)} trade(s). None of this is visible in an aggregate"
                ),
                found_at_ns=self._now_ns(),
            ),
            effect, shuffled,
            f"{kind}: {effect:+.3f} against {shuffled:+.3f} shuffled",
        )

    def _outcome(self, kind, state, pattern, effect, shuffled, reason) -> SequenceOutcome:
        return SequenceOutcome(
            kind=kind, state=state, pattern=pattern, effect=effect,
            shuffled_effect=shuffled, reason=reason, found_at_ns=self._now_ns(),
        )


def describe_sequence_mining(miner: SequencePatternMiner) -> dict:
    return {
        "part_id": PART_ID,
        "trades_recorded": miner.standing.trades_recorded,
        "patterns_tested": miner.standing.patterns_tested,
        "patterns_found": miner.standing.patterns_found,
        "not_present": miner.standing.not_present,
        "refused_thin_samples": miner.standing.refused_thin,
        "rejected_because_they_survived_shuffling": (
            miner.standing.rejected_by_shuffling
        ),
        "pattern_kinds": list(SEQUENCE_KINDS),
        "searches_for_patterns": False,
        "tests_against_a_shuffled_baseline": True,
    }


def _sequence_pattern_identity(items):
    return tuple(
        (p.kind, p.description, p.trades_examined, p.occurrences, p.effect,
         p.is_significant, p.reason)
        for p in items
    )


def mine_and_publish(miner: SequencePatternMiner, publish_patterns) -> None:
    """One pass over every kind, publishing each usable finding.

    `publish_patterns` takes (kind, pattern) -- separated from tick() so it is
    directly testable without running the whole part's event loop.
    """
    for kind in SEQUENCE_KINDS:
        outcome = miner.mine(kind)
        if outcome.is_usable:
            publish_patterns(kind, outcome.pattern)


def run_sequence_pattern_miner(
    miner: SequencePatternMiner, control_socket, read_trades, publish_patterns,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for job in read_trades():
            miner.observe_trade(**job)
        mine_and_publish(miner, publish_patterns)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_sequence_mining(miner),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    import datetime

    from runtime.input_assembly import Batch
    from runtime.trade_identity import closed_trade_id

    episodes = Batch(read=context.bus.reader("trade-episode"))
    closed = Batch(read=context.bus.reader("closed-trade"))
    raw_publish_patterns = context.bus.publisher_for("sequence-pattern")
    level_publisher = LevelPublisherByKey(
        publish=raw_publish_patterns,
        refresh_interval_seconds=context.number("sequence_pattern_republish_interval_seconds"),
        identity_of=_sequence_pattern_identity,
    )
    miner = SequencePatternMiner(
        minimum_trades=int(context.number("decoding_minimum_trades")),
        effect_threshold=context.number("sequence_effect_threshold"),
        shuffle_margin=context.number("sequence_shuffle_margin"),
    )

    def read_trades():
        episodes.payloads()
        jobs = []
        for trade in closed.payloads():
            opened = datetime.datetime.fromtimestamp(trade.opened_at_ns / 1e9, datetime.UTC)
            jobs.append({
                "trade_id": closed_trade_id(trade), "realised": trade.realised_pnl,
                "notional": trade.quantity * trade.entry_price, "opened_at_ns": trade.opened_at_ns,
                "seconds_into_session": float(opened.hour * 3600 + opened.minute * 60 + opened.second),
            })
        return tuple(jobs)

    return run_sequence_pattern_miner(
        miner=miner,
        control_socket=context.control_socket,
        read_trades=read_trades,
        publish_patterns=lambda kind, pattern: level_publisher.publish_level(kind, (pattern,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
