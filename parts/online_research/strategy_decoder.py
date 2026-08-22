"""strategy-decoder: what somebody's positions imply about how they decide.

A sequence of positions is a behaviour, and a behaviour can be described without
being understood. That is the trap this part is built around: a language model
given a list of trades will always produce a fluent strategy description, and the
description will be plausible whether or not the trader has a strategy at all.

So the decoder works in two separated steps, and the model is only allowed into the
second one:

1. **The regularities are measured here, in code.** Holding time, direction bias,
   entry timing relative to a move, symbol concentration, whether size scales with
   anything, whether losses are cut or held. These are arithmetic over observed
   positions and they are either present in the data or not.
2. **The model is asked to phrase the measured regularities, and nothing else.**
   Its output goes through the same fact-checking every other generated text in
   this system goes through: sentences whose numbers do not trace to a measurement
   are removed before anything is published.

**Randomness must be a possible answer.** A trader with no detectable regularity is
a real and common finding, and a decoder that cannot return it will describe noise
as a strategy every time. `NO_PATTERN` is therefore a first-class outcome, and the
part counts how often it is reached -- a decoder that never returns it is broken,
not insightful.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from runtime.claim_verification import make_request, verify_against_facts
from runtime.external_research_types import ResearchFinding
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "strategy-decoder"

PART_DECLARATION = PartDeclaration(
    part_id="strategy-decoder",
    consumes=("external-position", "validated-llm-output"),
    produces=("research-finding", "part-health", "llm-request"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

DECODED = "decoded"
NO_PATTERN = "no-regularity-is-detectable"
TOO_FEW_POSITIONS = "too-few-positions-to-measure-a-regularity"
AWAITING_PHRASING = "the-regularities-are-measured-and-await-phrasing"

# The regularities this part can actually measure. Anything not on this list is
# not something the decoder may claim, however well it would read.
DIRECTION_BIAS = "direction-bias"
HOLDING_TIME = "holding-time"
SYMBOL_CONCENTRATION = "symbol-concentration"
SIZE_DISCIPLINE = "size-discipline"
ENTRY_AFTER_A_MOVE = "entry-after-a-move"
LOSS_HANDLING = "loss-handling"

MEASURABLE_REGULARITIES = (
    DIRECTION_BIAS, HOLDING_TIME, SYMBOL_CONCENTRATION, SIZE_DISCIPLINE,
    ENTRY_AFTER_A_MOVE, LOSS_HANDLING,
)


@dataclass(frozen=True)
class Regularity:
    """One measured property of a trader's behaviour, with its strength."""

    kind: str
    statement: str
    value: float
    strength: float
    positions_supporting: int

    @property
    def is_strong_enough_to_report(self) -> bool:
        return self.strength >= 0.5


@dataclass(frozen=True)
class DecodedStrategy:
    trader_id: str
    state: str
    regularities: tuple
    positions_examined: int
    request: object | None
    finding: ResearchFinding | None
    reason: str
    decoded_at_ns: int

    @property
    def found_a_strategy(self) -> bool:
        return self.state == DECODED and bool(self.regularities)


@dataclass
class DecoderStanding:
    traders_decoded: int = 0
    strategies_found: int = 0
    no_pattern_found: int = 0
    too_few_positions: int = 0
    requests_made: int = 0
    sentences_removed_as_unsupported: int = 0


class StrategyDecoder:
    """Measures regularities in code; lets a model phrase only what was measured."""

    def __init__(
        self,
        minimum_positions: int,
        strength_threshold: float,
        relative_tolerance: float,
        maximum_sentences: int,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_positions < 3:
            raise ValueError(
                "a regularity over fewer than three positions is a coincidence with a name"
            )
        if not 0.0 < strength_threshold <= 1.0:
            raise ValueError("the strength threshold is a fraction inside (0, 1]")
        self._minimum_positions = minimum_positions
        self._strength_threshold = strength_threshold
        self._relative_tolerance = relative_tolerance
        self._maximum_sentences = maximum_sentences
        self._now_ns = now_ns
        self._positions: dict[str, list] = {}
        self._outcomes: dict[str, list] = {}
        self.standing = DecoderStanding()

    def observe_position(
        self, position, closed_at_ns: int | None = None,
        realised_return: float | None = None, move_before_entry: float | None = None,
    ) -> None:
        self._positions.setdefault(position.trader_id, []).append(
            (position, closed_at_ns, move_before_entry)
        )
        if realised_return is not None:
            self._outcomes.setdefault(position.trader_id, []).append(
                (realised_return, closed_at_ns, position.opened_at_ns)
            )

    def measure(self, trader_id: str) -> tuple:
        """Every regularity that is actually present, measured in code."""
        entries = self._positions.get(trader_id, [])
        if len(entries) < self._minimum_positions:
            return ()

        found = []
        positions = [position for position, _, _ in entries]
        count = len(positions)

        longs = sum(1 for position in positions if position.side == "long")
        bias = longs / count
        found.append(
            Regularity(
                DIRECTION_BIAS,
                f"{bias:.0%} of positions are long",
                bias,
                abs(bias - 0.5) * 2.0,
                count,
            )
        )

        holds = [
            (closed - position.opened_at_ns) / 1e9
            for position, closed, _ in entries
            if closed is not None and position.opened_at_ns is not None
        ]
        if len(holds) >= self._minimum_positions:
            median_hold = statistics.median(holds)
            spread = statistics.pstdev(holds) if len(holds) > 1 else 0.0
            # A tight holding time is a rule; a scattered one is not a finding.
            consistency = 0.0 if median_hold <= 0 else max(
                0.0, 1.0 - spread / median_hold
            )
            found.append(
                Regularity(
                    HOLDING_TIME,
                    f"positions are held about {median_hold / 60.0:.0f} minutes",
                    median_hold,
                    consistency,
                    len(holds),
                )
            )

        symbols = {}
        for position in positions:
            symbols[position.symbol] = symbols.get(position.symbol, 0) + 1
        concentration = max(symbols.values()) / count
        found.append(
            Regularity(
                SYMBOL_CONCENTRATION,
                f"{concentration:.0%} of positions are in one symbol",
                concentration,
                concentration,
                count,
            )
        )

        notionals = [
            position.notional for position in positions if position.notional is not None
        ]
        if len(notionals) >= self._minimum_positions and statistics.mean(notionals) > 0:
            variation = statistics.pstdev(notionals) / statistics.mean(notionals)
            found.append(
                Regularity(
                    SIZE_DISCIPLINE,
                    f"position size varies by {variation:.0%} around its mean",
                    variation,
                    max(0.0, 1.0 - variation),
                    len(notionals),
                )
            )

        moves = [move for _, _, move in entries if move is not None]
        if len(moves) >= self._minimum_positions:
            with_the_move = sum(1 for move in moves if move > 0) / len(moves)
            found.append(
                Regularity(
                    ENTRY_AFTER_A_MOVE,
                    f"{with_the_move:.0%} of entries follow a move in the same direction",
                    with_the_move,
                    abs(with_the_move - 0.5) * 2.0,
                    len(moves),
                )
            )

        outcomes = self._outcomes.get(trader_id, [])
        losers = [
            (realised, closed, opened)
            for realised, closed, opened in outcomes
            if realised < 0 and closed is not None and opened is not None
        ]
        winners = [
            (realised, closed, opened)
            for realised, closed, opened in outcomes
            if realised > 0 and closed is not None and opened is not None
        ]
        if len(losers) >= 2 and len(winners) >= 2:
            loser_hold = statistics.median((closed - opened) / 1e9 for _, closed, opened in losers)
            winner_hold = statistics.median((closed - opened) / 1e9 for _, closed, opened in winners)
            if winner_hold > 0:
                ratio = loser_hold / winner_hold
                found.append(
                    Regularity(
                        LOSS_HANDLING,
                        f"losing positions are held {ratio:.1f}x as long as winning ones",
                        ratio,
                        min(abs(ratio - 1.0), 1.0),
                        len(losers) + len(winners),
                    )
                )

        return tuple(found)

    def decode(self, trader_id: str, phrased_text: str | None = None) -> DecodedStrategy:
        self.standing.traders_decoded += 1
        entries = self._positions.get(trader_id, [])
        if len(entries) < self._minimum_positions:
            self.standing.too_few_positions += 1
            return self._decoded(
                trader_id, TOO_FEW_POSITIONS, (), len(entries), None, None,
                f"{len(entries)} position(s), below the {self._minimum_positions} needed. "
                f"A regularity measured over fewer is a coincidence with a name",
            )

        measured = self.measure(trader_id)
        strong = tuple(
            regularity
            for regularity in measured
            if regularity.strength >= self._strength_threshold
        )

        if not strong:
            self.standing.no_pattern_found += 1
            return self._decoded(
                trader_id, NO_PATTERN, (), len(entries), None, None,
                f"nothing in {len(entries)} position(s) is regular enough to describe. "
                f"That is a real finding: a decoder that cannot return it describes noise "
                f"as a strategy every time",
            )

        facts = {
            f"{regularity.kind}-value": regularity.value for regularity in strong
        }
        facts["positions-examined"] = float(len(entries))

        if phrased_text is None:
            request = make_request(
                purpose="describe-a-traders-behaviour",
                venue_id=entries[0][0].venue_id,
                symbol=entries[0][0].symbol,
                instruction=(
                    "Describe only these measured regularities in plain sentences. Do not "
                    "name a strategy, do not infer intent, and do not add anything not in "
                    "the facts: "
                    + "; ".join(regularity.statement for regularity in strong)
                ),
                facts=facts,
                maximum_sentences=self._maximum_sentences,
                now_ns=self._now_ns,
            )
            self.standing.requests_made += 1
            return self._decoded(
                trader_id, AWAITING_PHRASING, strong, len(entries), request, None,
                f"{len(strong)} regularity(ies) measured. The model is asked to phrase "
                f"them and nothing else, and its answer is checked before use",
            )

        verified = verify_against_facts(
            phrased_text, facts, relative_tolerance=self._relative_tolerance,
            require_a_citation=True,
        )
        self.standing.sentences_removed_as_unsupported += len(verified.removed_sentences)

        finding = ResearchFinding(
            finding_id=f"strategy:{trader_id}",
            topic="how-a-tracked-trader-decides",
            statement=verified.text or "; ".join(
                regularity.statement for regularity in strong
            ),
            evidence=tuple(regularity.statement for regularity in strong),
            source_references=tuple(
                sorted({position.source_reference for position, _, _ in entries})
            ),
            confidence=min(
                regularity.strength for regularity in strong
            ),
            would_be_refuted_by=(
                "the same regularities measured over their next positions not holding"
            ),
            is_testable_here=True,
            found_at_ns=self._now_ns(),
        )
        self.standing.strategies_found += 1
        return self._decoded(
            trader_id, DECODED, strong, len(entries), None, finding,
            f"{len(strong)} measured regularity(ies)"
            + (
                f", {len(verified.removed_sentences)} phrased sentence(s) removed as "
                f"unsupported"
                if verified.removed_sentences
                else ", every phrased sentence traced to a measurement"
            ),
        )

    def _decoded(
        self, trader_id, state, regularities, examined, request, finding, reason,
    ) -> DecodedStrategy:
        return DecodedStrategy(
            trader_id=trader_id, state=state, regularities=regularities,
            positions_examined=examined, request=request, finding=finding, reason=reason,
            decoded_at_ns=self._now_ns(),
        )


def describe_strategy_decoding(decoder: StrategyDecoder) -> dict:
    return {
        "part_id": PART_ID,
        "traders_decoded": decoder.standing.traders_decoded,
        "strategies_found": decoder.standing.strategies_found,
        "traders_with_no_detectable_pattern": decoder.standing.no_pattern_found,
        "too_few_positions": decoder.standing.too_few_positions,
        "requests_made": decoder.standing.requests_made,
        "sentences_removed_as_unsupported": (
            decoder.standing.sentences_removed_as_unsupported
        ),
        "measurable_regularities": list(MEASURABLE_REGULARITIES),
        "lets_a_model_measure_anything": False,
    }


def run_strategy_decoder(
    decoder: StrategyDecoder, control_socket, read_traders, publish_findings,
    publish_requests, health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for trader_id, phrased in read_traders(decoder):
            decoded = decoder.decode(trader_id, phrased)
            if decoded.request is not None:
                publish_requests(decoded.request)
            if decoded.finding is not None:
                publish_findings(decoded.finding)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
