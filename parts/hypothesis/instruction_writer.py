"""instruction-writer: the one part that turns everything learned into something that acts.

Every other part in this block and the two beside it produces evidence. This is
where evidence becomes an instruction the scanner will watch for on every symbol,
which makes it the narrowest and most consequential gate in the system: whatever
gets through here is what the system actually does.

It consumes twenty data types, and the point of that is that **an instruction is
written only when every one of them agrees**. The conditions are not weighed
against each other -- each covers a distinct way a wrong instruction gets written:

- **A falsification criterion** exists, so it can be retired.
- **A required sample size** exists and is reachable, so it can be answered.
- **It clears the bar its own search implies**, so the best of two hundred
  variations is not mistaken for a finding.
- **It is novel**, so the system is not testing the same idea in three wordings
  and reading three confirmations.
- **It has a regime tag**, so it fires where its evidence came from and nowhere
  else.
- **Its edge outlives its own sample size**, because confirming an edge that has
  already decayed is spending the trades to learn nothing.

**This is the gate INTO paper testing, not the gate out of it (2026-08-26).**
Refutation used to be a condition here, and it made the part unsatisfiable by
construction: the only producer of `refutation-verdict` is
`causal-refutation-battery`, which consumes `instruction-scorecard` -- the record
of an instruction that has **already traded**. A hypothesis that has never traded
could not carry a verdict, so every first-time hypothesis was refused with
`nothing-tried-to-break-it`, no instruction was ever written, nothing ever traded,
and no verdict could ever exist. Zero instructions was a stable fixed point of the
whole pipeline, and it held: measured on the live spine, 479,323 requests and
479,323 refusals. `instruction-promotion-gate` already consumes
`refutation-verdict`, `required-sample-size` and `falsification-criterion`, and
the battery runs on the paper record an instruction produces -- so refutation is a
**promotion** condition, where the evidence for it can exist, and this part is the
gate that lets a claim start earning that evidence. `docs/proposals/instruction-
writer-refutation-before-first-trade.md`, option 1.

**Novelty, the required sample size and the regime tag are read from the parts
that measure them**, not relayed through `hypothesis-ranker`. The ranker's job is
ordering a queue, and it cannot order a hypothesis whose expected edge is unknown
-- which every never-traded hypothesis's is. Reading it as a courier made three of
this part's conditions fail on the ranker's inability to rank rather than on
anything about the hypothesis: `hypotheses_ranked` 0 against `not_enough_inputs`
602,112, while it published 602,112 priorities carrying None in every field this
part read from them.

**The instruction carries its retirement condition with it.** An instruction
without one outlives the market it was learned in, and nothing else is positioned
to notice.

**A refusal names every failing condition, not the first.** "It failed" sends the
next attempt to fix the wrong thing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learning_types import OpportunityInstruction
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "instruction-writer"

PART_DECLARATION = PartDeclaration(
    part_id="instruction-writer",
    consumes=(
        "decoded-trade-instruction", "expectancy-breakdown", "recalled-episode",
        "semantic-fact", "loaded-skill-section", "strategy-gap", "feature-reliability",
        "regime-break-alert", "novel-idea", "cross-segment-lesson", "inverted-hypothesis",
        "hypothesis-priority", "winner-pattern", "reflection-note", "candidate-formula",
        "mutated-hypothesis", "falsification-criterion", "knowledge-link", "regime-memory",
        "horizon-profile", "novelty-score", "required-sample-size", "hypothesis-regime-tag",
    ),
    produces=("opportunity-instruction", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

WRITTEN = "written"

NO_FALSIFICATION_CRITERION = "nothing-could-retire-it"
NO_SAMPLE_SIZE = "nobody-has-said-how-many-trades-would-answer-it"
SAMPLE_UNREACHABLE = "more-trades-than-this-system-can-produce"
DOES_NOT_CLEAR_ITS_TRIALS = "the-best-of-many-variations-is-not-a-finding"
NOT_NOVEL = "the-same-idea-in-different-words-is-not-three-confirmations"
NO_REGIME_TAG = "nothing-says-which-market-this-is-a-claim-about"
# Kept as a name rather than deleted: an instruction whose tag says no regime has
# enough measured evidence yet is refused for a *different* reason from one that
# was never tagged at all, and a refusal that cannot tell them apart sends the
# next attempt to fix the wrong thing.
REGIME_EVIDENCE_TOO_THIN = "no-regime-has-enough-measured-evidence-to-say-where-this-applies"
# A tag naming several regimes is a claim about each of them separately, and one
# instruction carries one regime. Refused by name and counted rather than
# collapsed to the first regime in the tuple, which would silently narrow the
# claim, or to None, which would widen it. The right answer is one instruction per
# regime and that is a change with its own instruction_id shape; until it is made,
# this is a number on the board rather than a wrong tag on a live instruction.
SEVERAL_REGIMES_NEEDS_AN_INSTRUCTION_EACH = "it-applies-in-several-regimes-and-an-instruction-carries-one"
# Tested somewhere and found not to work there is a **stronger** fact than never
# having been measured, and refusing both under one name would report a hypothesis
# the system has actually refuted as one it simply has not got round to. The
# tagger states this case as a tag naming no regime it may fire in.
TESTED_AND_WORKS_NOWHERE = "it-was-tested-and-did-not-work-in-any-regime-it-was-tested-in"

# What a regime-independent hypothesis's instruction is tagged with. Named rather
# than None: None means nobody said, and this is the opposite -- it was tested in
# every regime that had evidence and worked in all of them.
EVERY_TESTED_REGIME = "every-regime-it-has-been-tested-in"

# The tagger's own verdict names. Strings rather than an import: a part names
# data, never another part (T-4).
TAG_STATE_UNTAGGED = "too-little-evidence-in-any-regime-to-say"
TAG_STATE_REGIME_INDEPENDENT = "works-in-every-regime-it-has-been-tested-in"


def regime_an_instruction_may_carry(tag) -> tuple[str | None, str | None]:
    """The one regime this tag supports, or the condition that says why there is none.

    Returns `(regime, failing_condition)` with exactly one of the two set. A
    hypothesis that names its own regime -- a mutation or an inversion carries the
    regime of the parent it came from -- arrives here as a plain string and is
    taken at its word; a tagger's verdict is read for what its evidence supports.
    """
    if tag is None:
        return None, NO_REGIME_TAG
    if isinstance(tag, str):
        return (tag, None) if tag else (None, NO_REGIME_TAG)

    state = getattr(tag, "state", None)
    if state == TAG_STATE_UNTAGGED:
        return None, REGIME_EVIDENCE_TOO_THIN
    if state == TAG_STATE_REGIME_INDEPENDENT:
        return EVERY_TESTED_REGIME, None

    fires_in = tuple(getattr(tag, "regimes_it_may_fire_in", ()) or ())
    if len(fires_in) == 1:
        return str(fires_in[0]), None
    if not fires_in:
        # A tag that is not UNTAGGED but names no regime it may fire in is the
        # tagger saying it measured this hypothesis and it worked nowhere.
        return None, TESTED_AND_WORKS_NOWHERE
    return None, SEVERAL_REGIMES_NEEDS_AN_INSTRUCTION_EACH


def _tag_says_where_it_applies(tag) -> bool:
    return regime_an_instruction_may_carry(tag)[0] is not None
EDGE_DIES_BEFORE_IT_IS_CONFIRMED = "its-edge-decays-before-its-own-sample-size-is-reached"
NO_MEASUREMENT = "the-scanner-cannot-watch-for-this"


@dataclass
class WriterStanding:
    requests: int = 0
    written: int = 0
    refused: int = 0
    by_failing_condition: dict = field(default_factory=dict)
    by_source: dict = field(default_factory=dict)
    instructions_live: int = 0


class InstructionWriter:
    """Writes an instruction only when every condition agrees, and says which failed."""

    def __init__(
        self,
        minimum_novelty: float,
        maximum_reachable_trades: int,
        known_measurements: tuple,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < minimum_novelty <= 1.0:
            raise ValueError("novelty is a fraction and its bar must be inside (0, 1]")
        if not known_measurements:
            raise ValueError(
                "an instruction over something nothing measures can never fire, so the set of "
                "measurements the scanner watches has to be stated"
            )
        self._minimum_novelty = minimum_novelty
        self._maximum_trades = maximum_reachable_trades
        self._known_measurements = tuple(known_measurements)
        self._now_ns = now_ns
        self._criteria: dict[str, object] = {}
        self._samples: dict[str, int] = {}
        self._trial_clears: dict[str, bool] = {}
        self._novelty: dict[str, float] = {}
        self._tags: dict[str, object] = {}
        self._half_lives: dict[str, float] = {}
        self._live: dict[str, OpportunityInstruction] = {}
        self.standing = WriterStanding()

    def observe_falsification_criterion(self, hypothesis_id: str, criterion) -> None:
        self._criteria[hypothesis_id] = criterion

    def observe_required_sample(self, hypothesis_id: str, trades: int) -> None:
        self._samples[hypothesis_id] = trades

    def observe_trial_verdict(self, hypothesis_id: str, clears_its_bar: bool) -> None:
        self._trial_clears[hypothesis_id] = clears_its_bar

    def observe_novelty(self, hypothesis_id: str, novelty: float) -> None:
        self._novelty[hypothesis_id] = novelty

    def observe_regime_tag(self, hypothesis_id: str, tag) -> None:
        """Where this hypothesis applies, as a tagger's verdict or a plain regime name.

        A mutation or an inversion inherits its parent's regime and states it as a
        string; hypothesis-regime-tagger publishes a verdict object carrying the
        evidence behind it. Both are accepted, and
        `regime_an_instruction_may_carry` is the single place that reads either.

        **A verdict is never replaced by an absence.** A hypothesis re-arriving on
        its own input carries `regime_tag=None` when it never named one, and
        letting that overwrite a tag the tagger measured would make the writer
        forget, every tick, what it had just been told.
        """
        if tag is None and self._tags.get(hypothesis_id) is not None:
            return
        self._tags[hypothesis_id] = tag

    def observe_edge_half_life(self, hypothesis_id: str, half_life_trades: float) -> None:
        self._half_lives[hypothesis_id] = half_life_trades

    def failing_conditions(self, hypothesis) -> tuple:
        """Every condition that fails, not the first -- so the next attempt fixes the right one."""
        hypothesis_id = hypothesis.hypothesis_id
        failing = []

        if hypothesis.context.get("measurement") not in self._known_measurements:
            failing.append(NO_MEASUREMENT)

        if hypothesis_id not in self._criteria:
            failing.append(NO_FALSIFICATION_CRITERION)

        trades = self._samples.get(hypothesis_id)
        if trades is None:
            failing.append(NO_SAMPLE_SIZE)
        elif trades > self._maximum_trades:
            failing.append(SAMPLE_UNREACHABLE)

        if not self._trial_clears.get(hypothesis_id, False):
            failing.append(DOES_NOT_CLEAR_ITS_TRIALS)

        if self._novelty.get(hypothesis_id, 0.0) < self._minimum_novelty:
            failing.append(NOT_NOVEL)

        # Refutation is deliberately absent -- see the module docstring. It is a
        # promotion condition, judged by instruction-promotion-gate on the paper
        # record this instruction is about to start producing.

        _, tag_refusal = regime_an_instruction_may_carry(self._tags.get(hypothesis_id))
        if tag_refusal is not None:
            failing.append(tag_refusal)

        half_life = self._half_lives.get(hypothesis_id)
        if half_life is not None and trades is not None and half_life < trades:
            # Confirming an edge that decays before its own sample size is
            # reached spends the trades to learn nothing.
            failing.append(EDGE_DIES_BEFORE_IT_IS_CONFIRMED)

        return tuple(failing)

    def write(self, hypothesis) -> tuple[OpportunityInstruction | None, tuple]:
        """One hypothesis, written only when every condition agrees."""
        self.standing.requests += 1
        self.standing.by_source[hypothesis.source] = (
            self.standing.by_source.get(hypothesis.source, 0) + 1
        )

        failing = self.failing_conditions(hypothesis)
        if failing:
            self.standing.refused += 1
            for condition in failing:
                self.standing.by_failing_condition[condition] = (
                    self.standing.by_failing_condition.get(condition, 0) + 1
                )
            return None, failing

        hypothesis_id = hypothesis.hypothesis_id
        criterion = self._criteria[hypothesis_id]
        trades = self._samples[hypothesis_id]
        regime, _ = regime_an_instruction_may_carry(self._tags[hypothesis_id])
        context = hypothesis.context

        instruction = OpportunityInstruction(
            instruction_id=f"instruction:{hypothesis_id}",
            hypothesis_id=hypothesis_id,
            measurement=context["measurement"],
            comparison=context.get("comparison", "above"),
            threshold=context.get("threshold", 0.0),
            direction=context.get("direction", "long"),
            expectation=context.get("expectation", "continuation"),
            horizon_seconds=context.get("horizon_seconds", 0.0),
            regime_tag=regime,
            # Carried with it: an instruction without a retirement condition
            # outlives the market it was learned in, and nothing else is
            # positioned to notice.
            retire_when=(
                f"{criterion.measure} {criterion.comparison} {criterion.threshold:.4g} over "
                f"{criterion.required_trades:,} trade(s), or the {regime} regime breaks, or "
                f"its edge half-life runs out"
            ),
            required_sample_size=trades,
            trials_in_family=hypothesis.trials_in_family,
            evidence={
                "novelty": self._novelty.get(hypothesis_id),
                # Deliberately absent: an instruction written here has not been
                # refuted yet, and recording a field for it would suggest the
                # question had been asked. instruction-promotion-gate asks it,
                # against the paper record this instruction is about to produce.
                "edge_half_life_trades": self._half_lives.get(hypothesis_id),
                "source": hypothesis.source,
                **hypothesis.evidence,
            },
            reason=(
                f"{hypothesis.statement}. Every condition agreed: it can be refuted "
                f"({criterion.measure} {criterion.comparison} {criterion.threshold:.4g}), "
                f"{trades:,} trade(s) would answer it, it clears the bar its "
                f"{hypothesis.trials_in_family}-trial search implies, it is novel at "
                f"{self._novelty.get(hypothesis_id, 0.0):.2f}, and it is tagged for "
                f"{regime} so it fires where its evidence came from and nowhere else. It "
                f"has not been refuted yet and is not claimed to have been -- that is "
                f"what the paper record it now starts producing is for"
            ),
            written_at_ns=self._now_ns(),
        )

        self._live[instruction.instruction_id] = instruction
        self.standing.written += 1
        self.standing.instructions_live = len(self._live)
        return instruction, ()

    def retire(self, instruction_id: str) -> None:
        self._live.pop(instruction_id, None)
        self.standing.instructions_live = len(self._live)

    @property
    def live_instructions(self) -> tuple:
        return tuple(self._live.values())


def describe_instruction_writing(writer: InstructionWriter) -> dict:
    return {
        "part_id": PART_ID,
        "requests": writer.standing.requests,
        "written": writer.standing.written,
        "refused": writer.standing.refused,
        "by_failing_condition": dict(sorted(writer.standing.by_failing_condition.items())),
        "by_source": dict(sorted(writer.standing.by_source.items())),
        "instructions_live": writer.standing.instructions_live,
        "known_measurements": list(writer._known_measurements),
    }


def run_instruction_writer(
    writer: InstructionWriter, control_socket, read_hypotheses, publish_instructions,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        instructions = []
        for hypothesis in read_hypotheses(writer):
            instruction, _ = writer.write(hypothesis)
            if instruction is not None:
                instructions.append(instruction)
        publish_instructions(tuple(instructions))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_instruction_writing(writer),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Hypotheses arrive on four of the twenty inputs -- mutated, inverted, a
    novel idea, a mined formula -- and are kept until every condition agrees
    or the system moves on. The conditions are refreshed from the others: the
    falsifier's criterion is the retirement condition; a mined formula's
    held-out excess is whether it cleared the bar its search implies; the
    priority carries the edge half-life. A regime-break alert retires every
    live instruction tagged for the regime that broke.

    Novelty, the trades required and the regime tag are read from the three
    parts that measure them -- hypothesis-deduplicator, power-estimator and
    hypothesis-regime-tagger -- rather than from hypothesis-priority, which
    carried None in all three for every hypothesis that had never traded.
    The priority is still read, for the edge half-life it does carry.
    """
    from runtime.input_assembly import Batch
    from runtime.learning_types import Hypothesis
    from runtime.sweep_measurements import KNOWN_MEASUREMENTS

    hypothesis_sources = {
        name: Batch(read=context.bus.reader(name))
        for name in ("mutated-hypothesis", "inverted-hypothesis", "novel-idea", "candidate-formula")
    }
    priorities = Batch(read=context.bus.reader("hypothesis-priority"))
    criteria = Batch(read=context.bus.reader("falsification-criterion"))
    alerts = Batch(read=context.bus.reader("regime-break-alert"))
    novelties = Batch(read=context.bus.reader("novelty-score"))
    samples = Batch(read=context.bus.reader("required-sample-size"))
    regime_tags = Batch(read=context.bus.reader("hypothesis-regime-tag"))
    evidence_only = tuple(
        Batch(read=context.bus.reader(name))
        for name in (
            "decoded-trade-instruction", "expectancy-breakdown", "recalled-episode", "semantic-fact",
            "loaded-skill-section", "strategy-gap", "feature-reliability", "cross-segment-lesson",
            "winner-pattern", "reflection-note", "knowledge-link", "regime-memory", "horizon-profile",
        )
    )
    publish_instructions = context.bus.publisher_for("opportunity-instruction")
    writer = InstructionWriter(
        minimum_novelty=context.number("hypothesis_minimum_novelty"),
        maximum_reachable_trades=int(context.number("hypothesis_maximum_reachable_trades")),
        known_measurements=KNOWN_MEASUREMENTS,
    )
    pending: dict[str, Hypothesis] = {}
    changed: set[str] = set()

    def as_hypothesis(item) -> Hypothesis | None:
        if isinstance(item, Hypothesis):
            return item
        idea_id = getattr(item, "idea_id", None)
        if idea_id is not None:
            context_of = dict(item.context) if isinstance(item.context, dict) else {}
            return Hypothesis(
                hypothesis_id=idea_id, statement=item.statement,
                what_would_refute_it=item.what_would_refute_it, family=item.source,
                trials_in_family=int(item.trials_in_this_family), source=item.source,
                context=context_of, required_sample_size=None,
                regime_tag=context_of.get("regime"), novelty=None,
                evidence=dict(item.evidence), proposed_at_ns=item.proposed_at_ns,
            )
        formula_id = getattr(item, "formula_id", None)
        if formula_id is not None:
            terms = tuple(item.terms)
            # One term is one watch condition; a conjunction is not something the
            # scanner can watch, and the writer refuses it as NO_MEASUREMENT.
            context_of = (
                {"measurement": terms[0].measurement, "comparison": terms[0].comparison, "threshold": terms[0].threshold}
                if len(terms) == 1 else {"measurement": None, "terms": tuple(str(term) for term in terms)}
            )
            excess = item.held_out_excess
            writer.observe_trial_verdict(formula_id, bool(item.is_usable and excess is not None and excess > 0))
            return Hypothesis(
                hypothesis_id=formula_id, statement=str(item),
                what_would_refute_it=f"a held-out hit rate at or below {item.base_rate:.1%}",
                family=item.family, trials_in_family=int(item.trials_in_family), source="candidate-formula",
                # No regime of its own: a mined formula's regime is what
                # hypothesis-regime-tagger's evidence says, and it arrives on
                # hypothesis-regime-tag rather than being asserted here.
                context=context_of, required_sample_size=None, regime_tag=None, novelty=None,
                evidence={"fitted_hit_rate": item.fitted_hit_rate, "held_out_hit_rate": item.held_out_hit_rate,
                          "held_out_trades": item.held_out_trades, "base_rate": item.base_rate,
                          "complexity_penalty": item.complexity_penalty},
                proposed_at_ns=item.mined_at_ns,
            )
        return None

    def read_hypotheses(_writer):
        for source in evidence_only:
            source.payloads()
        for source in hypothesis_sources.values():
            for item in source.payloads():
                hypothesis = as_hypothesis(item)
                if hypothesis is None:
                    continue
                pending[hypothesis.hypothesis_id] = hypothesis
                writer.observe_regime_tag(hypothesis.hypothesis_id, hypothesis.regime_tag)
                changed.add(hypothesis.hypothesis_id)
        for priority in priorities.payloads():
            # The half-life is the one field a priority carries that no other
            # part publishes to this one. Novelty and the trades required are
            # read from their own producers below, because a priority carries
            # them only when the ranker could rank -- and it cannot rank a
            # hypothesis that has never traded, which is every new one.
            if priority.half_life_trades is not None:
                writer.observe_edge_half_life(priority.hypothesis_id, float(priority.half_life_trades))
                changed.add(priority.hypothesis_id)
        for novelty in novelties.payloads():
            writer.observe_novelty(novelty.hypothesis_id, float(novelty.novelty))
            changed.add(novelty.hypothesis_id)
        for sample in samples.payloads():
            if sample.trades_required is not None:
                writer.observe_required_sample(sample.hypothesis_id, int(sample.trades_required))
                changed.add(sample.hypothesis_id)
        for tag in regime_tags.payloads():
            writer.observe_regime_tag(tag.hypothesis_id, tag)
            changed.add(tag.hypothesis_id)
        for criterion in criteria.payloads():
            if criterion.required_trades is not None:
                writer.observe_falsification_criterion(criterion.hypothesis_id, criterion)
                changed.add(criterion.hypothesis_id)
        for alert in alerts.payloads():
            if alert.has_broken:
                for instruction in writer.live_instructions:
                    if instruction.regime_tag == alert.regime:
                        writer.retire(instruction.instruction_id)
        due = tuple(pending[identity] for identity in sorted(changed) if identity in pending)
        changed.clear()
        return due

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        for instruction in kept:
            pending.pop(instruction.hypothesis_id, None)
        if kept:
            publish_instructions(kept)

    return run_instruction_writer(
        writer=writer,
        control_socket=context.control_socket,
        read_hypotheses=read_hypotheses,
        publish_instructions=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
