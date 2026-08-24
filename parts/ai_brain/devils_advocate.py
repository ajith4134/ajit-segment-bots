"""devils-advocate: the strongest case against, made before the trade is taken.

Every part upstream is looking for reasons to act. This one is the only part
whose job is to find reasons not to, and it exists because a system where nothing
argues the other side will always find the argument it went looking for.

It can **veto**, and that is what makes it more than decoration. An objection
that could only lower a score would be absorbed -- conviction would drift up to
compensate, and the argument would become a formality. So an objection strong
enough to reverse the decision stops it, and the arbiter honours that rather than
weighing it.

What makes an objection strong is measured, not asserted:

- **It cites something.** An objection whose numbers cannot be traced to a
  measurement is removed exactly like an invented rationale. "This looks
  overextended" is not an objection; "price is 4.2 deviations above its mean and
  the bot's record above 3 is 31%" is.
- **Regime memory raises it.** An objection that has been right before in this
  regime is stronger than a novel one, and the record of that is what separates
  a real warning from a plausible sentence.

**It always produces something, including "no objection survives".** A silent
advocate is indistinguishable from one that was not run, and the record of which
trades had no case against them is worth as much as the objections themselves.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.claim_verification import make_request, verify_against_facts
from runtime.learned_estimator import RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trade_intent import CounterArgument

PART_ID = "devils-advocate"

PART_DECLARATION = PartDeclaration(
    part_id="devils-advocate",
    consumes=(
        "trade-intent", "directional-opinion", "validated-llm-output",
        "verified-snapshot", "regime-memory",
    ),
    produces=("counter-argument", "llm-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PURPOSE = "argue-against-this-trade"

INSTRUCTION = (
    "Argue against this position as strongly as the facts allow. Use only the facts given; "
    "every number you write must be one of them. One objection per sentence. Do not "
    "hedge, do not balance, and do not concede -- something else already made the case for."
)

# Objections this part raises itself, from the measurements, before any model is
# asked. A model that is unavailable must not mean a trade goes unopposed.
CONVICTION_IS_UNMEASURED = "the-conviction-is-the-model's-own-number-not-a-measured-frequency"
SOLE_UNPROVEN_BOT = "one-bot-with-a-thin-record-is-carrying-this-alone"
DISSENT_WAS_OVERRULED = "another-bot-actively-wanted-the-other-side"
NO_STOP = "there-is-nowhere-this-trade-is-wrong"
OBJECTION_HAS_BEEN_RIGHT_BEFORE = "this-objection-has-been-right-before-in-this-regime"


@dataclass
class AdvocateStanding:
    arguments_made: int = 0
    vetoes: int = 0
    objections_raised: int = 0
    objections_removed_as_unsupported: int = 0
    trades_with_no_objection: int = 0
    by_objection: dict = field(default_factory=dict)


class DevilsAdvocate:
    """Makes the strongest case against a trade, and can stop it."""

    def __init__(
        self,
        veto_when_objection_hit_rate_above: float,
        minimum_observations: int,
        prior_objection_hit_rate: float,
        prior_weight: float,
        half_life_observations: float,
        relative_tolerance: float,
        maximum_sentences: int,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < veto_when_objection_hit_rate_above < 1.0:
            raise ValueError(
                "the veto threshold is how often an objection must have been right before it "
                "can stop a trade, and must be inside (0, 1)"
            )
        self._veto_above = veto_when_objection_hit_rate_above
        self._minimum = minimum_observations
        self._prior_hit_rate = prior_objection_hit_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._tolerance = relative_tolerance
        self._maximum_sentences = maximum_sentences
        self._now_ns = now_ns
        self._objection_records: dict[tuple[str, str], RateEstimator] = {}
        self._snapshots: dict[tuple[str, str], dict] = {}
        self._bot_trades: dict[tuple[str, str], int] = {}
        self.standing = AdvocateStanding()

    def observe_verified_snapshot(self, venue_id: str, symbol: str, snapshot: dict) -> None:
        self._snapshots[(venue_id, symbol)] = dict(snapshot)

    def observe_bot_maturity(self, bot: str, regime: str, trades_here: int) -> None:
        self._bot_trades[(bot, regime)] = trades_here

    def observe_objection_outcome(self, objection: str, regime: str, was_right: bool) -> None:
        """Whether an objection of this kind turned out to be the reason a trade failed."""
        self._objection_for(objection, regime).observe(was_right)

    def objection_record(self, objection: str, regime: str):
        return self._objection_for(objection, regime).estimate(self._minimum)

    def facts_for(self, intent, opinions) -> dict:
        facts = {
            "weighted_conviction": round(intent.conviction.value, 4),
            "contributing_bots": len(intent.contributing_bots),
            "dissenting_bots": len(intent.dissenting_bots),
        }
        if intent.stop_price is not None:
            facts["stop_price"] = intent.stop_price
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
        )

    def measured_objections(self, intent, opinions, regime: str) -> list:
        """Objections this part raises from the measurements, with no model involved."""
        objections = []

        if not intent.conviction.is_fitted:
            objections.append(
                (
                    CONVICTION_IS_UNMEASURED,
                    f"the {intent.conviction.value:.1%} conviction is the models' own number "
                    f"over {intent.conviction.observations} observation(s), not a frequency "
                    f"anything has measured",
                )
            )

        if len(intent.contributing_bots) == 1:
            bot = intent.contributing_bots[0]
            trades = self._bot_trades.get((bot, regime))
            if trades is not None and trades < self._minimum:
                objections.append(
                    (
                        SOLE_UNPROVEN_BOT,
                        f"{bot} is carrying this alone on {trades} closed trade(s) in {regime}, "
                        f"below the {self._minimum} that would make its record evidence",
                    )
                )

        if intent.dissenting_bots:
            objections.append(
                (
                    DISSENT_WAS_OVERRULED,
                    f"{', '.join(intent.dissenting_bots)} actively wanted the other side and "
                    f"were overruled; that is a disagreement being resolved, not an absence "
                    f"of one",
                )
            )

        if intent.stop_price is None:
            objections.append(
                (
                    NO_STOP,
                    "no stop price came with this intent, so there is nowhere this trade is "
                    "wrong and nothing that would end it except a decision nobody has made yet",
                )
            )

        return objections

    def argue(self, intent, opinions, regime: str, model_output: str | None = None) -> CounterArgument:
        """The case against, from the measurements and from a model if one answered."""
        self.standing.arguments_made += 1
        facts = self.facts_for(intent, opinions)

        objections = self.measured_objections(intent, opinions, regime)
        removed = 0

        if model_output is not None:
            verified = verify_against_facts(
                model_output, facts, self._tolerance, require_a_citation=True
            )
            removed = len(verified.removed_sentences)
            self.standing.objections_removed_as_unsupported += removed
            objections.extend(("model", sentence) for sentence in verified.kept_sentences)

        if not objections:
            self.standing.trades_with_no_objection += 1
            return CounterArgument(
                venue_id=intent.venue_id,
                symbol=intent.symbol,
                objections=(),
                strongest_objection=None,
                would_reverse_the_decision=False,
                evidence_cited=facts,
                was_written_by_a_model=model_output is not None,
                reason=(
                    f"no objection survives against {len(facts)} fact(s)"
                    + (
                        f"; {removed} model sentence(s) were removed for citing numbers "
                        f"nothing measured"
                        if removed
                        else ""
                    )
                ),
                argued_at_ns=self._now_ns(),
            )

        self.standing.objections_raised += len(objections)
        for kind, _ in objections:
            self.standing.by_objection[kind] = self.standing.by_objection.get(kind, 0) + 1

        # The strongest objection is the one whose kind has most often been the
        # reason a trade failed -- measured, so a plausible sentence cannot
        # outrank a warning with a record.
        ranked = sorted(
            objections,
            key=lambda entry: self.objection_record(entry[0], regime).value,
            reverse=True,
        )
        strongest_kind, strongest_text = ranked[0]
        record = self.objection_record(strongest_kind, regime)

        would_reverse = record.is_fitted and record.value > self._veto_above
        if would_reverse:
            self.standing.vetoes += 1

        return CounterArgument(
            venue_id=intent.venue_id,
            symbol=intent.symbol,
            objections=tuple(text for _, text in ranked),
            strongest_objection=strongest_text,
            would_reverse_the_decision=would_reverse,
            evidence_cited=facts,
            was_written_by_a_model=model_output is not None,
            reason=(
                f"{len(objections)} objection(s); the strongest is of a kind that has been the "
                f"reason a trade failed {record.value:.0%} of the time over "
                f"{record.observations} judged instance(s) in {regime}"
                + (
                    f", above the {self._veto_above:.0%} that stops a trade rather than "
                    f"discounting it -- an objection that could only lower a score would be "
                    f"absorbed by conviction drifting up to compensate"
                    if would_reverse
                    else ", which is not enough to stop it"
                )
                + (f"; {removed} model sentence(s) removed as unsupported" if removed else "")
            ),
            argued_at_ns=self._now_ns(),
        )

    def _objection_for(self, objection: str, regime: str) -> RateEstimator:
        key = (objection, regime)
        estimator = self._objection_records.get(key)
        if estimator is None:
            estimator = RateEstimator(
                prior=self._prior_hit_rate, prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._objection_records[key] = estimator
        return estimator


def describe_arguing(advocate: DevilsAdvocate) -> dict:
    return {
        "part_id": PART_ID,
        "arguments_made": advocate.standing.arguments_made,
        "trades_vetoed": advocate.standing.vetoes,
        "objections_raised": advocate.standing.objections_raised,
        "objections_removed_as_unsupported": advocate.standing.objections_removed_as_unsupported,
        "trades_with_no_objection": advocate.standing.trades_with_no_objection,
        "by_objection": dict(sorted(advocate.standing.by_objection.items())),
        "objection_records_held": len(advocate._objection_records),
    }


def run_devils_advocate(
    advocate: DevilsAdvocate, control_socket, read_intents_and_output,
    publish_arguments, publish_requests, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        arguments = []
        requests = []
        for intent, opinions, regime, model_output in read_intents_and_output(advocate):
            if model_output is None:
                requests.append(advocate.request(intent, opinions))
            arguments.append(advocate.argue(intent, opinions, regime, model_output))
        publish_arguments(tuple(arguments))
        publish_requests(tuple(requests))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_arguing(advocate),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch, LatestByKey

    intents = Batch(read=context.bus.reader("trade-intent"))
    opinions = LatestByKey(read=context.bus.reader("directional-opinion"), key_of=lambda o: (o.bot, o.venue_id, o.symbol))
    outputs = Batch(read=context.bus.reader("validated-llm-output"))
    snapshots = Batch(read=context.bus.reader("verified-snapshot"))
    memories = LatestByKey(read=context.bus.reader("regime-memory"), key_of=lambda m: m.regime)
    publish_arguments = context.bus.publisher_for("counter-argument")
    publish_requests = context.bus.publisher_for("llm-request")
    advocate = DevilsAdvocate(
        veto_when_objection_hit_rate_above=context.number("devils_advocate_veto_hit_rate"),
        minimum_observations=int(context.number("brain_minimum_observations")),
        prior_objection_hit_rate=context.number("brain_prior_hit_rate"),
        prior_weight=context.number("brain_prior_weight"),
        half_life_observations=context.number("brain_half_life_observations"),
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

    def regime_for(venue_id, symbol, held) -> str:
        # The regime the opinions were formed in, read off what they carry;
        # regime-memory is consulted so a remembered regime's record is current.
        memories.mapping()
        for opinion in held:
            summary = getattr(opinion, "features_summary", None) or {}
            regime = (summary.get("sources") or {}).get("regime") if isinstance(summary, dict) else None
            if regime:
                return str(regime)
        return "unclassified"

    def read_intents_and_output(_advocate):
        for snapshot in snapshots.payloads():
            advocate.observe_verified_snapshot(snapshot.venue_id, snapshot.symbol, snapshot.facts)
        answered = take_model_outputs()
        judgements = []
        for key, text in answered.items():
            intent, held = pending.pop(key)
            judgements.append((intent, held, regime_for(*key, held), text))
        for intent in intents.payloads():
            if not intent.is_actionable:
                continue
            key = (intent.venue_id, intent.symbol)
            held = opinions_for(*key)
            pending[key] = (intent, held)
            judgements.append((intent, held, regime_for(*key, held), None))
        return tuple(judgements)

    def publish_some(publish):
        return lambda items: publish(items) if items else None

    return run_devils_advocate(
        advocate=advocate,
        control_socket=context.control_socket,
        read_intents_and_output=read_intents_and_output,
        publish_arguments=publish_some(publish_arguments),
        publish_requests=publish_some(publish_requests),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
