"""brain-self-reflector: whether the reasoning was sound, which is not whether it won.

The one part that judges the brain rather than the market, and its whole value is
in keeping two questions apart that every trading system conflates:

- **Was the outcome good?** The trade made money or it did not.
- **Was the reasoning sound?** Given what was known at the time, was this the
  decision to make?

A system that treats those as one learns to repeat lucky mistakes and to abandon
sound processes after unlucky losses -- and it does so invisibly, because the
profit and loss looks like feedback. So this part reports both and names the two
dangerous cases explicitly: a **lucky win** (good outcome, unsound reasoning) is
the most expensive event a learning system can have, because it reinforces
exactly what should be corrected.

Soundness is judged against what the premortem and the counter-argument said
**before** the trade:

- A trade that failed the way its own premortem predicted was reasoned soundly
  and lost anyway. That is the system working.
- A trade that failed for a reason the devil's advocate raised and that was
  overruled was not reasoned soundly, whatever the conviction said.
- A trade that failed in a way nothing anticipated is the one that produces a
  lesson, and the lesson is what this part exists to write.

**A lesson is marked as applying beyond this trade or not.** "ADAUSDT gapped on a
listing announcement" is a fact; "announcements move thin symbols more than the
book implies" is a lesson, and only the second should change anything.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.claim_verification import make_request, verify_against_facts, written_without_a_model
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trade_intent import ReflectionNote

PART_ID = "brain-self-reflector"

PART_DECLARATION = PartDeclaration(
    part_id="brain-self-reflector",
    consumes=(
        "decision-rationale", "trade-episode", "validated-llm-output", "knowledge-snapshot",
        "premortem-note", "counter-argument", "trade-narrative",
    ),
    produces=("reflection-note", "llm-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

PURPOSE = "judge-the-reasoning-behind-a-closed-trade"

INSTRUCTION = (
    "This trade has closed. Judge whether the decision was right given what was known "
    "when it was made -- not whether it made money. Use only the facts given; every number "
    "you write must be one of them. State one lesson, and say whether it applies beyond "
    "this trade."
)

FAILED_AS_PREDICTED = "it-failed-the-way-its-own-premortem-said-it-would"
FAILED_AS_ARGUED = "it-failed-for-the-reason-the-counter-argument-raised"
FAILED_UNANTICIPATED = "it-failed-in-a-way-nothing-anticipated"
WON_AS_REASONED = "it-worked-and-the-reasoning-held"
WON_DESPITE_THE_REASONING = "it-worked-and-the-reasoning-did-not-hold"


@dataclass(frozen=True)
class TradeEpisode:
    """A closed trade, with what actually ended it."""

    venue_id: str
    symbol: str
    was_profitable: bool
    realised_fraction: float
    how_it_ended: str
    seconds_held: float
    entry_conviction: float
    conviction_was_measured: bool


@dataclass
class ReflectorStanding:
    reflections_written: int = 0
    lucky_wins: int = 0
    unlucky_losses: int = 0
    sound_and_won: int = 0
    unsound_and_lost: int = 0
    lessons_beyond_this_trade: int = 0
    model_sentences_removed: int = 0
    by_verdict: dict = field(default_factory=dict)


class BrainSelfReflector:
    """Judges the brain's reasoning separately from the trade's outcome."""

    def __init__(
        self,
        relative_tolerance: float,
        maximum_sentences: int,
        now_ns=time.time_ns,
    ) -> None:
        self._tolerance = relative_tolerance
        self._maximum_sentences = maximum_sentences
        self._now_ns = now_ns
        self._premortems: dict[tuple[str, str], object] = {}
        self._counter_arguments: dict[tuple[str, str], object] = {}
        self._rationales: dict[tuple[str, str], object] = {}
        self._knowledge: dict[tuple[str, str], dict] = {}
        self.standing = ReflectorStanding()

    def observe_premortem(self, note) -> None:
        self._premortems[(note.venue_id, note.symbol)] = note

    def observe_counter_argument(self, argument) -> None:
        self._counter_arguments[(argument.venue_id, argument.symbol)] = argument

    def observe_rationale(self, rationale) -> None:
        self._rationales[(rationale.venue_id, rationale.symbol)] = rationale

    def observe_knowledge_snapshot(self, venue_id: str, symbol: str, snapshot: dict) -> None:
        self._knowledge[(venue_id, symbol)] = dict(snapshot)

    def facts_for(self, episode: TradeEpisode) -> dict:
        facts = {
            "realised_fraction": round(episode.realised_fraction, 6),
            "entry_conviction": round(episode.entry_conviction, 4),
            "seconds_held": episode.seconds_held,
        }
        facts.update(self._knowledge.get((episode.venue_id, episode.symbol), {}))
        return facts

    def request(self, episode: TradeEpisode):
        return make_request(
            purpose=PURPOSE,
            venue_id=episode.venue_id,
            symbol=episode.symbol,
            instruction=INSTRUCTION,
            facts=self.facts_for(episode),
            maximum_sentences=self._maximum_sentences,
            now_ns=self._now_ns,
        )

    def judge_the_reasoning(self, episode: TradeEpisode) -> tuple[bool | None, str]:
        """Was this the decision to make, given what was known at the time?"""
        key = (episode.venue_id, episode.symbol)
        premortem = self._premortems.get(key)
        argument = self._counter_arguments.get(key)

        if episode.was_profitable:
            # A win is not evidence the reasoning held. An overruled objection
            # that would have been right makes it a lucky win, which is the most
            # expensive thing a learning system can record as a success.
            if argument is not None and argument.would_reverse_the_decision:
                return False, WON_DESPITE_THE_REASONING
            if not episode.conviction_was_measured:
                return False, WON_DESPITE_THE_REASONING
            return True, WON_AS_REASONED

        if argument is not None and argument.strongest_objection and self._ended_as_argued(
            episode, argument
        ):
            return False, FAILED_AS_ARGUED

        if premortem is not None and self._ended_as_predicted(episode, premortem):
            # Sound reasoning that lost anyway: the system worked and the trade
            # did not, which are different facts.
            return True, FAILED_AS_PREDICTED

        return None, FAILED_UNANTICIPATED

    def _ended_as_predicted(self, episode: TradeEpisode, premortem) -> bool:
        return any(
            episode.how_it_ended.lower() in mode.lower()
            or any(word in mode.lower() for word in episode.how_it_ended.lower().split("-"))
            for mode in premortem.failure_modes
        )

    def _ended_as_argued(self, episode: TradeEpisode, argument) -> bool:
        objection = (argument.strongest_objection or "").lower()
        return any(word in objection for word in episode.how_it_ended.lower().split("-"))

    def reflect(self, episode: TradeEpisode, model_output: str | None = None) -> ReflectionNote:
        self.standing.reflections_written += 1
        facts = self.facts_for(episode)
        sound, verdict = self.judge_the_reasoning(episode)
        self.standing.by_verdict[verdict] = self.standing.by_verdict.get(verdict, 0) + 1

        if model_output is None:
            verified = written_without_a_model(
                f"the trade ended {episode.how_it_ended} and the reasoning is judged {verdict}",
                facts,
            )
        else:
            verified = verify_against_facts(
                model_output, facts, self._tolerance, require_a_citation=False
            )
            self.standing.model_sentences_removed += len(verified.removed_sentences)

        # A lesson generalises only when it is about a mechanism rather than an
        # instance. "ADAUSDT gapped" is a fact; "announcements move thin symbols
        # more than the book implies" is a lesson, and only the second should
        # change anything.
        applies_beyond = verdict in (FAILED_UNANTICIPATED, WON_DESPITE_THE_REASONING)
        if applies_beyond:
            self.standing.lessons_beyond_this_trade += 1

        note = ReflectionNote(
            venue_id=episode.venue_id,
            symbol=episode.symbol,
            reasoning_was_sound=sound,
            outcome_was_good=episode.was_profitable,
            lesson=verified.text or verdict,
            applies_beyond_this_trade=applies_beyond,
            citations=dict(verified.citations),
            was_written_by_a_model=verified.was_written_by_a_model,
            reason=(
                f"outcome {'good' if episode.was_profitable else 'bad'}, reasoning "
                f"{'sound' if sound else ('unsound' if sound is False else 'unjudgeable')}: "
                f"{verdict}"
            ),
            reflected_at_ns=self._now_ns(),
        )

        if note.is_a_lucky_win:
            self.standing.lucky_wins += 1
        elif note.is_an_unlucky_loss:
            self.standing.unlucky_losses += 1
        elif sound and episode.was_profitable:
            self.standing.sound_and_won += 1
        elif sound is False and not episode.was_profitable:
            self.standing.unsound_and_lost += 1

        return note


def describe_reflection(reflector: BrainSelfReflector) -> dict:
    return {
        "part_id": PART_ID,
        "reflections_written": reflector.standing.reflections_written,
        "lucky_wins": reflector.standing.lucky_wins,
        "unlucky_losses": reflector.standing.unlucky_losses,
        "sound_reasoning_that_won": reflector.standing.sound_and_won,
        "unsound_reasoning_that_lost": reflector.standing.unsound_and_lost,
        "lessons_that_apply_beyond_one_trade": reflector.standing.lessons_beyond_this_trade,
        "model_sentences_removed": reflector.standing.model_sentences_removed,
        "by_verdict": dict(sorted(reflector.standing.by_verdict.items())),
    }


def run_brain_self_reflector(
    reflector: BrainSelfReflector, control_socket, read_episodes_and_output,
    publish_notes, publish_requests, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        notes = []
        requests = []
        for episode, model_output in read_episodes_and_output(reflector):
            if model_output is None:
                requests.append(reflector.request(episode))
            notes.append(reflector.reflect(episode, model_output))
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
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A closed trade arrives as the encoder's episode, which carries what the
    trade was and how it ended; the reflector's own episode shape is built
    from it. The premortem, counter-argument and rationale written before
    the trade are what the reflection judges the reasoning against.
    """
    from runtime.input_assembly import Batch

    episodes = Batch(read=context.bus.reader("trade-episode"))
    outputs = Batch(read=context.bus.reader("validated-llm-output"))
    rationales = Batch(read=context.bus.reader("decision-rationale"))
    snapshots = Batch(read=context.bus.reader("knowledge-snapshot"))
    premortems = Batch(read=context.bus.reader("premortem-note"))
    arguments = Batch(read=context.bus.reader("counter-argument"))
    narratives = Batch(read=context.bus.reader("trade-narrative"))
    publish_notes = context.bus.publisher_for("reflection-note")
    publish_requests = context.bus.publisher_for("llm-request")
    reflector = BrainSelfReflector(
        relative_tolerance=context.number("llm_claim_relative_tolerance"),
        maximum_sentences=int(context.number("llm_maximum_sentences")),
    )
    pending: dict[tuple[str, str], object] = {}

    def as_reflectable(encoded) -> TradeEpisode:
        held = max(0.0, (encoded.closed_at_ns - encoded.opened_at_ns) / 1e9)
        conditions = encoded.conditions if isinstance(encoded.conditions, dict) else {}
        entry_conviction = float(conditions.get("conviction", 0.0) or 0.0)
        return TradeEpisode(
            venue_id=encoded.venue_id, symbol=encoded.symbol,
            was_profitable=encoded.realised > 0,
            realised_fraction=float(conditions.get("realised_fraction", encoded.realised) or 0.0),
            how_it_ended=str(encoded.outcome), seconds_held=held,
            entry_conviction=entry_conviction,
            conviction_was_measured=bool(conditions.get("conviction_was_measured", False)),
        )

    def read_episodes_and_output(_reflector):
        for note in premortems.payloads():
            reflector.observe_premortem(note)
        for argument in arguments.payloads():
            reflector.observe_counter_argument(argument)
        for rationale in rationales.payloads():
            reflector.observe_rationale(rationale)
        snapshots.payloads()
        narratives.payloads()
        judgements = []
        for output in outputs.payloads():
            if output.purpose != PURPOSE:
                continue
            value = output.value if isinstance(output.value, dict) else {}
            key = (str(value.get("venue_id", "")), str(value.get("symbol", "")))
            if key in pending:
                judgements.append((pending.pop(key), output.text))
        for encoded in episodes.payloads():
            episode = as_reflectable(encoded)
            pending[(episode.venue_id, episode.symbol)] = episode
            judgements.append((episode, None))
        return tuple(judgements)

    def publish_some(publish):
        return lambda items: publish(items) if items else None

    return run_brain_self_reflector(
        reflector=reflector,
        control_socket=context.control_socket,
        read_episodes_and_output=read_episodes_and_output,
        publish_notes=publish_some(publish_notes),
        publish_requests=publish_some(publish_requests),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
