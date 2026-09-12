"""premortem-writer: assume this trade has already failed. What happened?

Written **before entry**, and that is the whole method. After a loss the reasons
are obvious and wrong -- whatever happened last gets blamed -- and after a win
nobody writes one at all. The only moment a failure can be described honestly is
while it is still hypothetical and the position is not yet arguing for itself.

Unlike the devil's advocate, this part does not try to stop the trade. It
produces the list of ways the trade dies and, for each, **what would show it
early**. That second half is what makes a premortem operational rather than
literary: "funding could invert" is a note, and "funding above 0.03% at the next
settlement would show it" is something a part can watch for.

**Failure modes without numbers are kept**, unlike a rationale's sentences. "The
venue could halt withdrawals" is a real way to lose money and has no measurement
attached; deleting it would make the premortem describe only the risks that
happen to be quantified, which are not the ones that end accounts.

**The failure modes this part raises itself never depend on a model**, because
the trades taken when the model is unreachable are exactly the ones nobody
reviewed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.claim_verification import make_request, verify_against_facts
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trade_intent import PremortemNote

PART_ID = "premortem-writer"

PART_DECLARATION = PartDeclaration(
    part_id="premortem-writer",
    consumes=("trade-intent", "directional-opinion", "validated-llm-output", "verified-snapshot"),
    produces=("premortem-note", "llm-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PURPOSE = "describe-how-this-trade-fails"

INSTRUCTION = (
    "This position has lost money. Describe what happened, using only the facts given; "
    "every number you write must be one of them. One way it failed per sentence, most "
    "likely first. Do not say it might work out."
)

STOP_IS_HIT = "price-reaches-the-stop"
THESIS_DECAYS = "the-setup-stops-being-true-without-price-moving"
HORIZON_PASSES = "nothing-happens-before-the-horizon-runs-out"
REGIME_CHANGES = "the-regime-the-models-were-fitted-on-ends"
LIQUIDITY_LEAVES = "the-book-that-sized-this-is-not-there-when-it-is-exited"
CONVICTION_WAS_NOT_MEASURED = "the-conviction-was-never-a-measured-frequency"


@dataclass
class WriterStanding:
    notes_written: int = 0
    written_by_a_model: int = 0
    failure_modes_recorded: int = 0
    model_sentences_removed: int = 0
    notes_with_no_early_warning: int = 0
    by_failure_mode: dict = field(default_factory=dict)


class PremortemWriter:
    """Lists how a trade dies, and what would show each one early."""

    def __init__(
        self,
        relative_tolerance: float,
        maximum_sentences: int,
        now_ns=time.time_ns,
    ) -> None:
        self._tolerance = relative_tolerance
        self._maximum_sentences = maximum_sentences
        self._now_ns = now_ns
        self._snapshots: dict[tuple[str, str], dict] = {}
        self.standing = WriterStanding()

    def observe_verified_snapshot(self, venue_id: str, symbol: str, snapshot: dict) -> None:
        self._snapshots[(venue_id, symbol)] = dict(snapshot)

    def facts_for(self, intent, opinions) -> dict:
        facts = {
            "weighted_conviction": round(intent.conviction.value, 4),
            "contributing_bots": len(intent.contributing_bots),
        }
        if intent.stop_price is not None:
            facts["stop_price"] = intent.stop_price
        if intent.horizon_seconds:
            facts["horizon_seconds"] = intent.horizon_seconds
        for opinion in opinions:
            facts[f"{opinion.bot}_conviction"] = round(opinion.conviction.value, 4)
        facts.update(self._snapshots.get((intent.venue_id, intent.symbol), {}))
        return facts

    def request(self, intent, opinions):
        return make_request(
            purpose=PURPOSE,
            venue_id=intent.venue_id,
            symbol=intent.symbol,
            instruction=INSTRUCTION,
            facts=self.facts_for(intent, opinions),
            maximum_sentences=self._maximum_sentences,
            now_ns=self._now_ns,
            asked_by=PART_ID,
        )

    def measured_failure_modes(self, intent, opinions) -> list:
        """The ways this trade dies that the measurements already imply.

        Raised with no model involved, because the trades taken while the model
        is unreachable are exactly the ones nobody reviewed.
        """
        modes = []

        if intent.stop_price is not None:
            modes.append(
                (
                    STOP_IS_HIT,
                    f"price reaches {intent.stop_price:.8g} and the position closes at a loss",
                    f"price within a fraction of {intent.stop_price:.8g} with the setup's "
                    f"features no longer supporting it",
                )
            )

        modes.append(
            (
                THESIS_DECAYS,
                "the setup stops being true without price moving much: the features that made "
                "this a trade revert while the position sits in it",
                "the entry features crossing back through zero, which the invalidation "
                "watchers already measure",
            )
        )

        if intent.horizon_seconds:
            modes.append(
                (
                    HORIZON_PASSES,
                    f"nothing happens for {intent.horizon_seconds:.0f}s and the position is "
                    f"closed having paid costs for a move that never arrived",
                    f"less than half the expected move by half the {intent.horizon_seconds:.0f}s "
                    f"horizon",
                )
            )

        modes.append(
            (
                REGIME_CHANGES,
                "the regime these models were fitted on ends while the position is open, so "
                "every conviction behind it describes a market that no longer exists",
                "a regime-break alert, or the regime classifier's Hurst estimate crossing its "
                "threshold",
            )
        )

        modes.append(
            (
                LIQUIDITY_LEAVES,
                "the book that made this size reasonable is not there when the position is "
                "exited, so the loss is larger than the stop implied",
                "the liquidity grade for this symbol falling while the position is open",
            )
        )

        if not intent.conviction.is_fitted:
            modes.append(
                (
                    CONVICTION_WAS_NOT_MEASURED,
                    f"the {intent.conviction.value:.1%} conviction was the models' own number "
                    f"and the true rate is lower, so this trade was one of the ones that made "
                    f"the calibration",
                    "the calibrator's correction for this band once it has enough outcomes",
                )
            )

        return modes

    def write(self, intent, opinions, model_output: str | None = None) -> PremortemNote:
        self.standing.notes_written += 1
        facts = self.facts_for(intent, opinions)

        modes = self.measured_failure_modes(intent, opinions)
        removed = 0

        if model_output is not None:
            self.standing.written_by_a_model += 1
            # A failure mode with no number is kept: "the venue could halt
            # withdrawals" is a real way to lose money, and deleting it would
            # make the premortem describe only the quantified risks, which are
            # not the ones that end accounts.
            verified = verify_against_facts(
                model_output, facts, self._tolerance, require_a_citation=False
            )
            removed = len(verified.removed_sentences)
            self.standing.model_sentences_removed += removed
            modes.extend(("model", sentence, "") for sentence in verified.kept_sentences)

        self.standing.failure_modes_recorded += len(modes)
        for kind, _, _ in modes:
            self.standing.by_failure_mode[kind] = self.standing.by_failure_mode.get(kind, 0) + 1

        early_warnings = tuple(warning for _, _, warning in modes if warning)
        if not early_warnings:
            self.standing.notes_with_no_early_warning += 1

        return PremortemNote(
            venue_id=intent.venue_id,
            symbol=intent.symbol,
            failure_modes=tuple(text for _, text, _ in modes),
            most_likely_failure=modes[0][1] if modes else None,
            what_would_show_it_early=early_warnings,
            was_written_by_a_model=model_output is not None,
            reason=(
                f"{len(modes)} way(s) this trade fails, {len(early_warnings)} of them with "
                f"something that would show it early"
                + (
                    f"; {removed} model sentence(s) removed for citing numbers nothing measured"
                    if removed
                    else ""
                )
            ),
            written_at_ns=self._now_ns(),
        )


def describe_premortems(writer: PremortemWriter) -> dict:
    return {
        "part_id": PART_ID,
        "notes_written": writer.standing.notes_written,
        "written_by_a_model": writer.standing.written_by_a_model,
        "failure_modes_recorded": writer.standing.failure_modes_recorded,
        "model_sentences_removed": writer.standing.model_sentences_removed,
        "notes_with_no_early_warning": writer.standing.notes_with_no_early_warning,
        "by_failure_mode": dict(sorted(writer.standing.by_failure_mode.items())),
    }


def run_premortem_writer(
    writer: PremortemWriter, control_socket, read_intents_and_output,
    publish_notes, publish_requests, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        notes = []
        requests = []
        for intent, opinions, model_output in read_intents_and_output(writer):
            if model_output is None:
                requests.append(writer.request(intent, opinions))
            notes.append(writer.write(intent, opinions, model_output))
        publish_notes(tuple(notes))
        publish_requests(tuple(requests))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_premortems(writer),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch, LatestByKey

    intents = Batch(read=context.bus.reader("trade-intent"))
    opinions = LatestByKey(read=context.bus.reader("directional-opinion"), key_of=lambda o: (o.bot, o.venue_id, o.symbol))
    outputs = Batch(read=context.bus.reader("validated-llm-output"))
    snapshots = Batch(read=context.bus.reader("verified-snapshot"))
    publish_notes = context.bus.publisher_for("premortem-note")
    publish_requests = context.bus.publisher_for("llm-request")
    writer = PremortemWriter(
        relative_tolerance=context.number("llm_claim_relative_tolerance"),
        maximum_sentences=int(context.number("llm_maximum_sentences")),
    )
    # Intents judged without a model answer are remembered by symbol until a
    # validated output for this part's purpose names the same symbol in its
    # value; then the judgement is made again with the model's text. No model
    # is configured in phase 1, so today every judgement is the measured one.
    pending: dict[tuple[str, str], tuple] = {}

    def take_model_outputs():
        answered = {}
        for output in outputs.payloads():
            if output.purpose != PURPOSE:
                continue
            value = output.value if isinstance(output.value, dict) else {}
            key = (str(value.get("venue_id", "")), str(value.get("symbol", "")))
            if key in pending:
                answered[key] = output.text
        return answered

    def opinions_for(venue_id, symbol):
        return [o for (bot, v, s), o in opinions.mapping().items() if (v, s) == (venue_id, symbol)]

    def read_intents_and_output(_writer):
        for snapshot in snapshots.payloads():
            writer.observe_verified_snapshot(snapshot.venue_id, snapshot.symbol, snapshot.facts)
        answered = take_model_outputs()
        judgements = []
        for key, text in answered.items():
            intent, held = pending.pop(key)
            judgements.append((intent, held, text))
        for intent in intents.payloads():
            if not intent.is_actionable:
                continue
            key = (intent.venue_id, intent.symbol)
            held = opinions_for(*key)
            pending[key] = (intent, held)
            judgements.append((intent, held, None))
        return tuple(judgements)

    def publish_some(publish):
        return lambda items: publish(items) if items else None

    return run_premortem_writer(
        writer=writer,
        control_socket=context.control_socket,
        read_intents_and_output=read_intents_and_output,
        publish_notes=publish_some(publish_notes),
        publish_requests=publish_some(publish_requests),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
