"""size-hint-writer: how large this should be relative to normal, and never a notional.

A hint, not a size. The capital desk owns sizing because it knows the rest of the
book -- what else is open, what is correlated with it, what the account can carry
-- and this part knows none of that. Returning a notional here would be sizing
without knowing what else the system holds, which is the way a book ends up
concentrated with every individual decision defensible.

What raises and lowers the hint:

- **Calibrated conviction, not raw.** A bot's stated 0.8 that has meant 0.5 must
  size like 0.5. This part reads the calibrated numbers directly from all three
  bots rather than the arbiter's blend, so a single bot's miscalibration is
  visible here rather than averaged into the intent.
- **Agreement.** Two bots reaching the same conclusion from different features
  justifies more size than one bot being sure.
- **The bot's own record**, from the scorecard: how a bot's calls at this
  conviction band have actually resolved.

**It is floored and capped, and both bounds are hard.** The floor stops a
conviction the system cannot measure from producing a position too small to teach
it anything; the ceiling stops the most confident decision from being the one
nobody checked. Conviction that would exceed the ceiling is clipped, and the
clipping is reported -- a hint silently at its maximum looks identical to one that
was merely large.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trade_intent import MAJORITY, SOLE_OPINION, UNANIMOUS, SizeHint

PART_ID = "size-hint-writer"

PART_DECLARATION = PartDeclaration(
    part_id="size-hint-writer",
    consumes=(
        "trade-intent", "bot-scorecard", "bull-calibrated-conviction",
        "bear-calibrated-conviction", "tail-calibrated-conviction",
    ),
    produces=("size-hint", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)


@dataclass
class WriterStanding:
    hints_written: int = 0
    clipped_at_ceiling: int = 0
    raised_to_floor: int = 0
    unmeasured_convictions: int = 0
    largest_hint: float = 0.0
    by_agreement: dict = field(default_factory=dict)


class SizeHintWriter:
    """Writes how large an intent should be relative to a normal one, and why."""

    def __init__(
        self,
        floor_multiple: float,
        ceiling_multiple: float,
        conviction_reference: float,
        agreement_multiple: float,
        sole_opinion_multiple: float,
        unmeasured_multiple: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < floor_multiple < ceiling_multiple:
            raise ValueError(
                "the floor must be positive and below the ceiling; a floor of zero would "
                "produce positions too small to teach the system anything"
            )
        if not 0.0 < conviction_reference < 1.0:
            raise ValueError(
                "the reference is the conviction at which a trade is normal-sized and must "
                "be inside (0, 1)"
            )
        if not 0.0 < unmeasured_multiple <= 1.0:
            raise ValueError(
                "an unmeasured conviction sizes smaller than a measured one, never larger"
            )
        self._floor = floor_multiple
        self._ceiling = ceiling_multiple
        self._reference = conviction_reference
        self._agreement_multiple = agreement_multiple
        self._sole_multiple = sole_opinion_multiple
        self._unmeasured_multiple = unmeasured_multiple
        self._now_ns = now_ns
        self._convictions: dict[tuple[str, str, str], object] = {}
        self._band_records: dict[tuple[str, int], tuple] = {}
        self.standing = WriterStanding()

    def observe_calibrated_conviction(self, conviction) -> None:
        """One bot's calibrated number for one symbol, kept by bot and symbol."""
        self._convictions[(conviction.bot, conviction.venue_id, conviction.symbol)] = conviction

    def observe_scorecard(self, bot: str, scorecard) -> None:
        """How this bot's calls at each conviction band have actually resolved."""
        for band, record in scorecard.describe()["by_probability_band"].items():
            low, _, _ = band.partition("-")
            index = int(round(float(low) * 10))
            self._band_records[(bot, index)] = (record["wins"], record["trades"])

    def band_hit_rate(self, bot: str, probability: float) -> float | None:
        record = self._band_records.get((bot, min(9, max(0, int(probability * 10)))))
        if record is None or record[1] == 0:
            return None
        return record[0] / record[1]

    def write(self, intent) -> SizeHint:
        self.standing.hints_written += 1

        convictions = [
            self._convictions.get((bot, intent.venue_id, intent.symbol))
            for bot in intent.contributing_bots
        ]
        present = [conviction for conviction in convictions if conviction is not None]

        # Calibrated, not raw: a bot's stated 0.8 that has meant 0.5 must size
        # like 0.5, and reading the bots directly keeps one bot's miscalibration
        # visible rather than averaged into the intent.
        if present:
            probability = sum(c.probability for c in present) / len(present)
            measured = all(c.is_measured for c in present)
            source = f"{len(present)} bot(s)' own calibrated numbers"
        else:
            probability = intent.conviction.value
            measured = intent.conviction.is_fitted
            source = "the intent's conviction, since no bot's calibrated number was available"

        multiple = probability / self._reference

        if intent.agreement == UNANIMOUS:
            multiple *= self._agreement_multiple
        elif intent.agreement == SOLE_OPINION:
            multiple *= self._sole_multiple

        if not measured:
            self.standing.unmeasured_convictions += 1
            multiple *= self._unmeasured_multiple

        band_notes = []
        for conviction in present:
            observed = self.band_hit_rate(conviction.bot, conviction.probability)
            if observed is not None:
                band_notes.append(
                    f"{conviction.bot}'s calls near {conviction.probability:.0%} have resolved "
                    f"{observed:.0%} of the time"
                )

        clipped = multiple > self._ceiling
        raised = multiple < self._floor
        if clipped:
            self.standing.clipped_at_ceiling += 1
        if raised:
            self.standing.raised_to_floor += 1
        multiple = min(self._ceiling, max(self._floor, multiple))

        self.standing.largest_hint = max(self.standing.largest_hint, multiple)
        self.standing.by_agreement[intent.agreement] = (
            self.standing.by_agreement.get(intent.agreement, 0) + 1
        )

        return SizeHint(
            venue_id=intent.venue_id,
            symbol=intent.symbol,
            multiple_of_normal=multiple,
            conviction=Estimate(
                value=probability,
                is_fitted=measured,
                observations=sum(c.calibrated.observations for c in present),
                prior=self._reference,
                was_clamped=clipped or raised,
                bound_low=self._floor,
                bound_high=self._ceiling,
                reason=source,
            ),
            agreement=intent.agreement,
            floor=self._floor,
            ceiling=self._ceiling,
            reason=(
                f"{multiple:.2f}x a normal trade: {probability:.1%} conviction from {source} "
                f"against a {self._reference:.0%} reference, {intent.agreement}"
                + (f"; {'; '.join(band_notes)}" if band_notes else "")
                + (
                    "; the conviction is not yet a measured frequency, so it sizes smaller"
                    if not measured
                    else ""
                )
                + (
                    f"; clipped at the {self._ceiling:.2f}x ceiling, so the most confident "
                    f"decision is not the one nobody checked"
                    if clipped
                    else ""
                )
                + (
                    f"; raised to the {self._floor:.2f}x floor, below which a position teaches "
                    f"the system nothing"
                    if raised
                    else ""
                )
            ),
            hinted_at_ns=self._now_ns(),
        )


def describe_size_hints(writer: SizeHintWriter) -> dict:
    return {
        "part_id": PART_ID,
        "hints_written": writer.standing.hints_written,
        "clipped_at_the_ceiling": writer.standing.clipped_at_ceiling,
        "raised_to_the_floor": writer.standing.raised_to_floor,
        "sized_down_for_unmeasured_conviction": writer.standing.unmeasured_convictions,
        "largest_hint": writer.standing.largest_hint,
        "by_agreement": dict(sorted(writer.standing.by_agreement.items())),
        "calibrated_convictions_held": len(writer._convictions),
    }


def run_size_hint_writer(
    writer: SizeHintWriter, control_socket, read_intents_and_convictions, publish_hints,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        intents = read_intents_and_convictions(writer)
        publish_hints(tuple(writer.write(intent) for intent in intents))

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

    intents = Batch(read=context.bus.reader("trade-intent"))
    scorecards = Batch(read=context.bus.reader("bot-scorecard"))
    convictions = [
        Batch(read=context.bus.reader(kind))
        for kind in ("bull-calibrated-conviction", "bear-calibrated-conviction", "tail-calibrated-conviction")
    ]
    publish_hints = context.bus.publisher_for("size-hint")
    writer = SizeHintWriter(
        floor_multiple=context.number("size_hint_floor_multiple"),
        ceiling_multiple=context.number("size_hint_ceiling_multiple"),
        conviction_reference=context.number("size_hint_conviction_reference"),
        agreement_multiple=context.number("size_hint_agreement_multiple"),
        sole_opinion_multiple=context.number("size_hint_sole_opinion_multiple"),
        unmeasured_multiple=context.number("size_hint_unmeasured_multiple"),
    )

    def read_intents_and_convictions(_writer):
        for scorecard in scorecards.payloads():
            writer.observe_scorecard(scorecard.bot, scorecard)
        for source in convictions:
            for conviction in source.payloads():
                writer.observe_calibrated_conviction(conviction)
        return tuple(intent for intent in intents.payloads() if intent.is_actionable)

    def publish(hints) -> None:
        if hints:
            publish_hints(hints)

    return run_size_hint_writer(
        writer=writer,
        control_socket=context.control_socket,
        read_intents_and_convictions=read_intents_and_convictions,
        publish_hints=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
