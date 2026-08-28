"""exploration-pair-decoder: what a paired trade actually settled (RL-005).

An exploration pair opens both directions on the same instrument at the same moment
to answer a question the system could not answer any other way -- whether a setup has
directional edge at all. Its verdict is about the question, not about the money: **a
pair that lost money and settled the question did its job**, and scoring pairs on PnL
turns an experiment into a bet.

What makes a pair conclusive, and what quietly makes it worthless:

- **The two legs must have been simultaneous and symmetric.** Opened seconds apart,
  or at different sizes, and the difference between them measures the delay or the
  size rather than the direction.
- **The difference must exceed the round-trip cost of both legs.** Both legs pay
  fees, so a pair whose legs differ by less than that has established nothing --
  and this is the most common outcome, which is itself worth knowing.
- **A pair that closed for different reasons is not a comparison.** One leg stopped
  out and one leg held to target measures the stop placement, not the direction.
- **An inconclusive pair is a real result.** It says the setup has no detectable
  directional edge here, which is exactly the finding that should stop the system
  trading it -- and a decoder that always produces a direction manufactures one.

The verdict feeds the opportunity system as evidence about the setup, never as a
signal to trade the winning side. The pair already happened; the direction it found
still has to earn its way through the ordinary path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.trade_decoding_types import (
    CLOSED_DIFFERENTLY,
    LONG_SIDE_WON,
    NOT_SYMMETRIC,
    NO_DIRECTIONAL_EDGE,
    PairVerdict,
    SHORT_SIDE_WON,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "exploration-pair-decoder"

PART_DECLARATION = PartDeclaration(
    part_id="exploration-pair-decoder",
    consumes=("closed-trade", "trade-episode", "directional-opinion"),
    produces=("pair-verdict", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

# The five verdicts come from `runtime.trade_decoding_types`, where a consumer can
# read them without importing this part (T-4). INCOMPLETE stays here: it is a state
# of this decoder's own bookkeeping, never a verdict, and no verdict is published
# carrying it.
INCOMPLETE = "one-leg-is-still-open"

# A pair is two directions on ONE instrument. Two symbols is not a pair, and the
# difference between them would measure the symbols rather than the direction.
DIFFERENT_INSTRUMENTS = "the-legs-were-not-the-same-instrument"


@dataclass(frozen=True)
class PairOutcome:
    pair_id: str
    state: str
    verdict: PairVerdict | None
    reason: str
    decided_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.verdict is not None


@dataclass
class DecoderStanding:
    pairs_decoded: int = 0
    conclusive: int = 0
    no_directional_edge: int = 0
    refused_not_symmetric: int = 0
    refused_closed_differently: int = 0
    refused_different_instruments: int = 0
    incomplete: int = 0
    pairs_that_lost_money_and_settled_the_question: int = 0


class ExplorationPairDecoder:
    """Decides what a paired trade established about direction, not about money."""

    def __init__(
        self,
        maximum_opening_gap_seconds: float,
        size_tolerance: float,
        round_trip_cost_fraction: float,
        now_ns=time.time_ns,
    ) -> None:
        if maximum_opening_gap_seconds < 0:
            raise ValueError("legs opened apart measure the delay, not the direction")
        if not 0.0 <= size_tolerance < 1.0:
            raise ValueError(
                "legs of different sizes measure the size, so the tolerance is a small "
                "fraction"
            )
        if round_trip_cost_fraction < 0:
            raise ValueError(
                "both legs pay fees, so the difference must clear the cost of finding out"
            )
        self._maximum_gap = maximum_opening_gap_seconds
        self._size_tolerance = size_tolerance
        self._cost_fraction = round_trip_cost_fraction
        self._now_ns = now_ns
        self._legs: dict[str, dict] = {}
        self._questions: dict[str, str] = {}
        self.standing = DecoderStanding()

    def observe_pair_question(self, pair_id: str, question: str) -> None:
        self._questions[pair_id] = question

    def observe_leg(
        self, pair_id: str, side: str, closed_trade=None, exit_reason: str | None = None,
        opened_at_ns: int | None = None, notional: float | None = None,
    ) -> None:
        self._legs.setdefault(pair_id, {})[side] = {
            "trade": closed_trade, "exit_reason": exit_reason,
            "opened_at_ns": opened_at_ns, "notional": notional,
        }

    def decode(self, pair_id: str) -> PairOutcome:
        self.standing.pairs_decoded += 1
        legs = self._legs.get(pair_id, {})
        question = self._questions.get(pair_id, "does this setup have directional edge")

        if set(legs) != {"long", "short"} or any(
            leg["trade"] is None for leg in legs.values()
        ):
            self.standing.incomplete += 1
            return self._outcome(
                pair_id, INCOMPLETE, None,
                "one leg is still open, so the comparison cannot be made yet",
            )

        long_leg, short_leg = legs["long"], legs["short"]

        instrument = self._instrument_of(long_leg, short_leg)
        if instrument is None:
            self.standing.refused_different_instruments += 1
            return self._outcome(
                pair_id, DIFFERENT_INSTRUMENTS, None,
                f"the legs closed on "
                f"{long_leg['trade'].venue_id}:{long_leg['trade'].symbol} and "
                f"{short_leg['trade'].venue_id}:{short_leg['trade'].symbol}. A pair is "
                f"two directions on one instrument; across two, the difference measures "
                f"the instruments rather than the direction",
            )
        venue_id, symbol = instrument

        gap = abs(
            (long_leg["opened_at_ns"] or 0) - (short_leg["opened_at_ns"] or 0)
        ) / 1e9
        sizes = [long_leg["notional"], short_leg["notional"]]
        size_mismatch = (
            None
            if any(size is None for size in sizes) or max(sizes) == 0
            else abs(sizes[0] - sizes[1]) / max(sizes)
        )

        if gap > self._maximum_gap or (
            size_mismatch is not None and size_mismatch > self._size_tolerance
        ):
            self.standing.refused_not_symmetric += 1
            return self._outcome(
                pair_id, NOT_SYMMETRIC,
                self._verdict(
                    venue_id, symbol, pair_id, question, NOT_SYMMETRIC, None, None, None, False,
                    f"the legs opened {gap:.1f}s apart"
                    + (
                        f" and differ in size by {size_mismatch:.0%}"
                        if size_mismatch is not None
                        else ""
                    )
                    + ". The difference between them measures the delay or the size "
                      "rather than the direction",
                ),
                "legs not comparable",
            )

        if long_leg["exit_reason"] != short_leg["exit_reason"]:
            self.standing.refused_closed_differently += 1
            return self._outcome(
                pair_id, CLOSED_DIFFERENTLY,
                self._verdict(
                    venue_id, symbol, pair_id, question, CLOSED_DIFFERENTLY,
                    long_leg["trade"].realised_pnl, short_leg["trade"].realised_pnl,
                    None, False,
                    f"one leg closed on {long_leg['exit_reason']} and the other on "
                    f"{short_leg['exit_reason']}. That measures the exit rule, not the "
                    f"direction",
                ),
                "legs closed differently",
            )

        long_realised = long_leg["trade"].realised_pnl
        short_realised = short_leg["trade"].realised_pnl
        difference = long_realised - short_realised

        notional = long_leg["notional"] or abs(
            long_leg["trade"].entry_price * long_leg["trade"].quantity
        )
        # Both legs pay to find out, so the answer has to clear both.
        cost_of_finding_out = 2.0 * self._cost_fraction * notional

        if abs(difference) <= cost_of_finding_out:
            self.standing.no_directional_edge += 1
            verdict = self._verdict(
                venue_id, symbol, pair_id, question, NO_DIRECTIONAL_EDGE, long_realised, short_realised,
                difference, True,
                f"the legs differ by {difference:+.4f} against {cost_of_finding_out:.4f} "
                f"of cost to find out. Neither side had detectable edge, which is exactly "
                f"the finding that should stop this setup being traded -- and a decoder "
                f"that always produces a direction manufactures one",
            )
        else:
            self.standing.conclusive += 1
            winner = LONG_SIDE_WON if difference > 0 else SHORT_SIDE_WON
            verdict = self._verdict(
                venue_id, symbol, pair_id, question, winner, long_realised, short_realised, difference,
                True,
                f"the {'long' if difference > 0 else 'short'} side is ahead by "
                f"{abs(difference):.4f}, clear of {cost_of_finding_out:.4f} in costs. This "
                f"is evidence about the setup, not a signal: the direction still has to "
                f"earn its way through the ordinary path",
            )

        if long_realised + short_realised < 0 and verdict.is_conclusive:
            self.standing.pairs_that_lost_money_and_settled_the_question += 1

        return self._outcome(pair_id, verdict.verdict, verdict, verdict.reason)

    def _instrument_of(self, long_leg, short_leg) -> tuple[str, str] | None:
        """The one instrument both legs were run on, or None where they differ."""
        long_key = (long_leg["trade"].venue_id, long_leg["trade"].symbol)
        short_key = (short_leg["trade"].venue_id, short_leg["trade"].symbol)
        return long_key if long_key == short_key else None

    def _verdict(
        self, venue_id, symbol, pair_id, question, verdict, long_realised,
        short_realised, difference, is_conclusive, reason,
    ) -> PairVerdict:
        return PairVerdict(
            pair_id=pair_id, question=question, verdict=verdict,
            long_realised=long_realised, short_realised=short_realised,
            difference=difference, is_conclusive=is_conclusive, reason=reason,
            decided_at_ns=self._now_ns(), venue_id=venue_id, symbol=symbol,
        )

    def _outcome(self, pair_id, state, verdict, reason) -> PairOutcome:
        return PairOutcome(
            pair_id=pair_id, state=state, verdict=verdict, reason=reason,
            decided_at_ns=self._now_ns(),
        )


def describe_pair_decoding(decoder: ExplorationPairDecoder) -> dict:
    return {
        "part_id": PART_ID,
        "pairs_decoded": decoder.standing.pairs_decoded,
        "conclusive": decoder.standing.conclusive,
        "no_directional_edge": decoder.standing.no_directional_edge,
        "refused_not_symmetric": decoder.standing.refused_not_symmetric,
        "refused_different_instruments": decoder.standing.refused_different_instruments,
        "refused_closed_differently": decoder.standing.refused_closed_differently,
        "incomplete": decoder.standing.incomplete,
        "pairs_that_lost_money_and_settled_the_question": (
            decoder.standing.pairs_that_lost_money_and_settled_the_question
        ),
        "scores_pairs_on_pnl": False,
        "always_produces_a_direction": False,
    }


def run_exploration_pair_decoder(
    decoder: ExplorationPairDecoder, control_socket, read_pairs, publish_verdicts,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for pair_id in read_pairs(decoder):
            outcome = decoder.decode(pair_id)
            if outcome.is_usable:
                publish_verdicts(outcome.verdict)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_pair_decoding(decoder),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A pair is two opinions on one symbol from two bots, one long one short,
    that the brain opened together; each closed trade on that symbol is a
    leg, and the pair is decoded when both legs have closed.
    """
    from runtime.input_assembly import Batch
    from runtime.trade_identity import closed_trade_id

    closed = Batch(read=context.bus.reader("closed-trade"))
    episodes = Batch(read=context.bus.reader("trade-episode"))
    opinions = Batch(read=context.bus.reader("directional-opinion"))
    publish_verdicts = context.bus.publisher_for("pair-verdict")
    decoder = ExplorationPairDecoder(
        maximum_opening_gap_seconds=context.number("pair_maximum_opening_gap"),
        size_tolerance=context.number("pair_size_tolerance"),
        round_trip_cost_fraction=2.0 * context.number("taker_fee_rate"),
    )
    sides_seen: dict[tuple[str, str], set[str]] = {}

    def read_pairs(_decoder):
        for opinion in opinions.payloads():
            if opinion.is_a_call_to_act:
                sides_seen.setdefault((opinion.venue_id, opinion.symbol), set()).add(opinion.side)
        episodes.payloads()
        touched = set()
        for trade in closed.payloads():
            key = (trade.venue_id, trade.symbol)
            if len(sides_seen.get(key, ())) < 2:
                continue  # one side only: not a pair
            pair_id = f"{key[0]}:{key[1]}"
            decoder.observe_pair_question(pair_id, "which of two bots was right on this symbol")
            decoder.observe_leg(
                pair_id, trade.direction, trade, exit_reason=None,
                opened_at_ns=trade.opened_at_ns, notional=trade.quantity * trade.entry_price,
            )
            touched.add(pair_id)
        return tuple(sorted(touched))

    return run_exploration_pair_decoder(
        decoder=decoder,
        control_socket=context.control_socket,
        read_pairs=read_pairs,
        publish_verdicts=lambda verdict: publish_verdicts((verdict,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
