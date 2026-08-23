"""knowledge-graph-linker: what relates to what, and how strongly.

Facts, episodes, instructions and skills all describe the same market from
different angles, and nothing connects them. Without links, the system can know
that BTCUSDT and ETHUSDT move together, that a skill covers funding regimes, and
that an instruction fires on funding -- and never notice that the three are about
the same thing.

The links are typed, because different relations mean different things and
merging them produces a graph that says everything is related to everything:

- **Moves-with**, from measured correlation, decayed. The only link that is
  purely quantitative and the only one that can be wrong in a way measurement
  catches.
- **Explains**, from a skill or a fact to an instruction. A link that says
  "this is why that works", which is what makes a refutation of the explanation
  a reason to recheck the instruction.
- **Preceded**, from one episode to another. Sequence, not causation, and the
  type says so -- a graph that recorded them as causal would let the system
  believe it had found a mechanism.
- **Contradicts**, which is the most useful link and the one a naive graph omits
  because it looks like an error.

**Every link carries strength and a source.** A link nobody rechecks becomes a
belief, and one whose strength is unstated is believed absolutely.

**Links decay.** A relation measured in one regime stops holding, and a graph
that never forgets accumulates every relation that has ever briefly been true.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.online_learner import RunningMoments
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "knowledge-graph-linker"

PART_DECLARATION = PartDeclaration(
    part_id="knowledge-graph-linker",
    consumes=("semantic-fact", "recalled-episode", "instruction-history", "available-skill"),
    produces=("knowledge-link", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

MOVES_WITH = "moves-with"
EXPLAINS = "explains"
PRECEDED = "preceded"
CONTRADICTS = "contradicts"
IS_ABOUT = "is-about"

LINK_KINDS = (MOVES_WITH, EXPLAINS, PRECEDED, CONTRADICTS, IS_ABOUT)

LINKED = "linked"
TOO_WEAK = "the-relation-is-too-weak-to-record"
UNKNOWN_KIND = "not-a-relation-this-graph-records"
DECAYED_AWAY = "the-relation-has-decayed-past-being-worth-keeping"


@dataclass(frozen=True)
class KnowledgeLink:
    """One typed relation between two things, with its strength and its source."""

    left: str
    right: str
    kind: str
    strength: float
    source: str
    observations: int
    established_at_ns: int
    last_seen_at_ns: int
    reason: str

    @property
    def is_causal_claim(self) -> bool:
        """Only EXPLAINS claims a mechanism. PRECEDED is sequence and says so."""
        return self.kind == EXPLAINS

    def strength_now(self, now_ns: int, half_life_seconds: float) -> float:
        """A relation measured in one regime stops holding; the graph forgets it."""
        elapsed = max(0.0, (now_ns - self.last_seen_at_ns) / 1e9)
        return self.strength * 0.5 ** (elapsed / half_life_seconds)


@dataclass
class LinkerStanding:
    links_offered: int = 0
    links_recorded: int = 0
    refused_too_weak: int = 0
    refused_unknown_kind: int = 0
    decayed_away: int = 0
    by_kind: dict = field(default_factory=dict)
    strongest_link: float | None = None


class KnowledgeGraphLinker:
    """Records typed, sourced, decaying relations between everything the system knows."""

    def __init__(
        self,
        minimum_strength: float,
        half_life_seconds: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < minimum_strength <= 1.0:
            raise ValueError(
                "without a strength floor everything links to everything and the graph says "
                "nothing"
            )
        if half_life_seconds <= 0:
            raise ValueError(
                "a graph that never forgets accumulates every relation that has ever briefly "
                "been true"
            )
        self._minimum_strength = minimum_strength
        self._half_life = half_life_seconds
        self._now_ns = now_ns
        self._links: dict[tuple[str, str, str], KnowledgeLink] = {}
        self._correlations: dict[tuple[str, str], RunningMoments] = {}
        self.standing = LinkerStanding()

    def link(
        self, left: str, right: str, kind: str, strength: float, source: str, observations: int = 1
    ) -> tuple[KnowledgeLink | None, str]:
        """One relation. Typed, because merging kinds produces a graph that says everything."""
        self.standing.links_offered += 1

        if kind not in LINK_KINDS:
            self.standing.refused_unknown_kind += 1
            return None, UNKNOWN_KIND

        if abs(strength) < self._minimum_strength:
            self.standing.refused_too_weak += 1
            return None, TOO_WEAK

        key = self._key(left, right, kind)
        existing = self._links.get(key)
        now = self._now_ns()

        record = KnowledgeLink(
            left=left,
            right=right,
            kind=kind,
            strength=strength,
            source=source,
            observations=(existing.observations if existing else 0) + observations,
            established_at_ns=existing.established_at_ns if existing else now,
            last_seen_at_ns=now,
            reason=(
                f"{left} {kind} {right} at {strength:.2f}, from {source} over "
                f"{(existing.observations if existing else 0) + observations} observation(s)"
                + (
                    ". This is a claim about a mechanism, which makes a refutation of the "
                    "explanation a reason to recheck what it explains"
                    if kind == EXPLAINS
                    else ""
                )
                + (
                    ". Sequence rather than causation -- the type says so, because a graph "
                    "recording these as causal would let the system believe it had found a "
                    "mechanism"
                    if kind == PRECEDED
                    else ""
                )
            ),
        )

        self._links[key] = record
        self.standing.links_recorded += 1
        self.standing.by_kind[kind] = self.standing.by_kind.get(kind, 0) + 1
        if self.standing.strongest_link is None or abs(strength) > self.standing.strongest_link:
            self.standing.strongest_link = abs(strength)
        return record, LINKED

    def observe_correlation(self, left: str, right: str, correlation: float) -> tuple:
        """A measured correlation, which is the one link measurement can catch being wrong."""
        key = tuple(sorted((left, right)))
        moments = self._correlations.get(key)
        if moments is None:
            moments = RunningMoments(half_life_observations=200.0)
            self._correlations[key] = moments
        moments.observe(correlation)
        return self.link(
            key[0], key[1], MOVES_WITH, abs(moments.mean), "measurement", observations=1
        )

    def links_from(self, node: str, kind: str | None = None) -> tuple:
        """Every live relation from one thing, with decay applied."""
        now = self._now_ns()
        found = []
        for (left, right, link_kind), record in sorted(self._links.items()):
            if node not in (left, right):
                continue
            if kind is not None and link_kind != kind:
                continue
            strength = record.strength_now(now, self._half_life)
            if strength < self._minimum_strength:
                self.standing.decayed_away += 1
                continue
            found.append(record)
        return tuple(found)

    def related(self, left: str, right: str, kind: str | None = None) -> KnowledgeLink | None:
        if kind is not None:
            return self._links.get(self._key(left, right, kind))
        for link_kind in LINK_KINDS:
            record = self._links.get(self._key(left, right, link_kind))
            if record is not None:
                return record
        return None

    def prune_decayed(self) -> tuple:
        """Drop relations that have decayed past being worth keeping."""
        now = self._now_ns()
        dropped = []
        for key, record in list(self._links.items()):
            if record.strength_now(now, self._half_life) < self._minimum_strength:
                del self._links[key]
                dropped.append(record)
                self.standing.decayed_away += 1
        return tuple(dropped)

    def _key(self, left: str, right: str, kind: str) -> tuple:
        # Symmetric relations are stored once; directed ones keep their direction.
        if kind in (MOVES_WITH, CONTRADICTS):
            left, right = sorted((left, right))
        return (left, right, kind)

    @property
    def link_count(self) -> int:
        return len(self._links)


def describe_links(linker: KnowledgeGraphLinker) -> dict:
    return {
        "part_id": PART_ID,
        "links_offered": linker.standing.links_offered,
        "links_recorded": linker.standing.links_recorded,
        "links_held": linker.link_count,
        "refused_too_weak": linker.standing.refused_too_weak,
        "refused_unknown_kind": linker.standing.refused_unknown_kind,
        "decayed_away": linker.standing.decayed_away,
        "by_kind": dict(sorted(linker.standing.by_kind.items())),
        "strongest_link": linker.standing.strongest_link,
        "link_kinds": list(LINK_KINDS),
    }


def run_knowledge_graph_linker(
    linker: KnowledgeGraphLinker, control_socket, read_knowledge, publish_links,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        nodes = read_knowledge(linker)
        linker.prune_decayed()
        links = []
        for node in nodes:
            links.extend(linker.links_from(node))
        publish_links(tuple(links))

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

    Every arrival is offered as an is-about link, the one relation that
    claims no cause: a fact is about its symbol, a recalled episode's
    detector is about the regime it was recalled in, an instruction is
    about its regime tag, a skill is about each of its sections. Strength
    is what the source measured -- a fact's confidence, a recall's
    similarity, a history's hit rate, a skill's backtest score. The links
    from every node touched go out.
    """
    from runtime.input_assembly import Batch

    facts = Batch(read=context.bus.reader("semantic-fact"))
    recalls = Batch(read=context.bus.reader("recalled-episode"))
    histories = Batch(read=context.bus.reader("instruction-history"))
    skills = Batch(read=context.bus.reader("available-skill"))
    publish_links = context.bus.publisher_for("knowledge-link")
    linker = KnowledgeGraphLinker(
        minimum_strength=context.number("knowledge_link_minimum_strength"),
        half_life_seconds=context.number("knowledge_link_half_life_seconds"),
    )

    def read_knowledge(_linker):
        touched: set[str] = set()
        for fact in facts.payloads():
            node = f"{fact.venue_id}:{fact.symbol}"
            linker.link(node, fact.key, IS_ABOUT, float(fact.confidence.value), fact.source, max(1, int(fact.confidence.observations)))
            touched.add(node)
        for recall in recalls.payloads():
            for recalled in recall.episodes:
                episode = recalled.episode
                linker.link(episode.detector, episode.regime, IS_ABOUT, float(recalled.similarity), "recalled-episode")
                touched.add(episode.detector)
        for history in histories.payloads():
            if history.regime_tag and history.trades > 0:
                strength = max(0.0, min(1.0, (history.realised > 0) * 1.0))
                linker.link(history.instruction_id, str(history.regime_tag), IS_ABOUT, strength, "instruction-history", max(1, history.trades))
                touched.add(history.instruction_id)
        for skill in skills.payloads():
            if skill.backtest_score is None:
                continue
            for section in skill.sections:
                linker.link(skill.skill_id, str(section), IS_ABOUT, float(skill.backtest_score), "available-skill")
            touched.add(skill.skill_id)
        return tuple(sorted(touched))

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_links(kept)

    return run_knowledge_graph_linker(
        linker=linker,
        control_socket=context.control_socket,
        read_knowledge=read_knowledge,
        publish_links=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
