"""The knowledge block: three memory tiers that disagree on purpose.

A single store resolves every contradiction by overwriting, silently, and the
system loses the one signal that says something changed. So these tests are about
what each tier refuses to do: the semantic store will not invent a key, the
episodic store will not edit an episode, the playbook will not be searched, the
pruner will not delete for age alone, and the contradiction detector will not
pick a winner.
"""

import importlib

import pytest

from parts.knowledge.contradiction_detector import (
    ContradictionDetector, FACT_AGAINST_FACT, FACT_AGAINST_RULE, LINK_AGAINST_FACT,
    RULE_AGAINST_RULE,
)
from parts.knowledge.episode_embedder import (
    EMBEDDED, EpisodeEmbedder, NO_FEATURES, TOO_FEW_FEATURES,
)
from parts.knowledge.episodic_trade_store import (
    EpisodicTradeStore, NOTHING_SIMILAR, RECALLED,
)
from parts.knowledge.fact_provenance_tracker import (
    FactProvenanceTracker, INVALIDATED, NO_SOURCE, SOURCE_IS_GONE, TRACKED,
)
from parts.knowledge.forgetting_curve_scheduler import (
    CANNOT_BE_RECHECKED, ForgettingCurveScheduler, HALF_LIFE_SECONDS, NEEDS_RECHECKING,
    NO_PROVENANCE, SCHEDULED,
)
from parts.knowledge.instruction_archive import (
    InstructionArchive, MUTATED, RESURRECTED, RETIRED, SCORED, TRADED, WRITTEN,
)
from parts.knowledge.knowledge_graph_linker import (
    CONTRADICTS, EXPLAINS, KnowledgeGraphLinker, LINKED, LINK_KINDS, MOVES_WITH, PRECEDED,
    TOO_WEAK, UNKNOWN_KIND,
)
from parts.knowledge.knowledge_pruner import (
    KnowledgePruner, KEPT, ONLY_OLD, PRUNED, RESTS_ON_SOMETHING_INVALIDATED,
    STILL_IN_USE, SUPERSEDED_AND_SETTLED, SUPERSESSION_UNSETTLED, USELESS,
)
from parts.knowledge.knowledge_snapshot_versioner import (
    FACTS, KnowledgeSnapshotVersioner, RULES, TAKEN, UNCHANGED,
)
from parts.knowledge.procedural_playbook import (
    APPLIED, NOTHING_MATCHED, ProceduralPlaybook,
)
from parts.knowledge.regime_memory_store import (
    NEW_REGIME, RECOGNISED, RegimeMemoryStore, RegimeSignature, TOO_FEW_OCCURRENCES,
)
from parts.knowledge.semantic_fact_store import (
    CONTRADICTS as FACT_CONTRADICTS, SemanticFactStore, STORED, SUPERSEDED, UNKNOWN_KEY,
)
from parts.knowledge.symbol_profile_store import (
    ABSENT, ESTABLISHED, MEASURED, NEW_LISTING, PUBLISHED, SymbolProfileStore,
)
from runtime.knowledge_types import (
    FACT_KEYS, FROM_MEASUREMENT, FROM_A_VENUE_DOCUMENT, NORMAL_MOVE, TICK_SIZE,
    TYPICAL_SPREAD, TYPICAL_VOLUME, TradeEpisode,
)
from runtime.part_declaration import load_declaration_from_blueprint

BLOCK_PARTS = {
    "semantic-fact-store": "parts.knowledge.semantic_fact_store",
    "episodic-trade-store": "parts.knowledge.episodic_trade_store",
    "procedural-playbook": "parts.knowledge.procedural_playbook",
    "symbol-profile-store": "parts.knowledge.symbol_profile_store",
    "instruction-archive": "parts.knowledge.instruction_archive",
    "knowledge-pruner": "parts.knowledge.knowledge_pruner",
    "knowledge-graph-linker": "parts.knowledge.knowledge_graph_linker",
    "fact-provenance-tracker": "parts.knowledge.fact_provenance_tracker",
    "contradiction-detector": "parts.knowledge.contradiction_detector",
    "episode-embedder": "parts.knowledge.episode_embedder",
    "regime-memory-store": "parts.knowledge.regime_memory_store",
    "knowledge-snapshot-versioner": "parts.knowledge.knowledge_snapshot_versioner",
    "forgetting-curve-scheduler": "parts.knowledge.forgetting_curve_scheduler",
}

VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"
DAY_NS = 86_400_000_000_000


class Clock:
    def __init__(self, now_ns=1_700_000_000_000_000_000):
        self.now_ns = now_ns

    def __call__(self):
        return self.now_ns

    def advance_days(self, days):
        self.now_ns += int(days * DAY_NS)


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_no_knowledge_part_imports_another_part(part_id):
    """T-4: a part names data, never another part."""
    with open(importlib.import_module(BLOCK_PARTS[part_id]).__file__, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(("from parts.", "import parts.")):
                raise AssertionError(f"{part_id} imports another part: {line.strip()}")


# ---- semantic-fact-store ----------------------------------------------------

def a_fact_store(tolerance=0.01, clock=None):
    store = SemanticFactStore(prior_confidence=0.5, contradiction_tolerance=tolerance)
    if clock is not None:
        store._now_ns = clock
    return store


def test_a_key_outside_the_closed_set_is_refused():
    """An open key set lets the same fact exist twice under two spellings."""
    subject = a_fact_store()
    assert subject.upsert(VENUE, SYMBOL, "usual-spread", 0.001, FROM_MEASUREMENT)[1] == UNKNOWN_KEY
    assert subject.upsert(VENUE, SYMBOL, TYPICAL_SPREAD, 0.001, FROM_MEASUREMENT)[1] == STORED


def test_a_superseded_value_is_kept_so_the_history_survives():
    """"When did we start thinking this" is the question after every surprise."""
    subject = a_fact_store()
    subject.upsert(VENUE, SYMBOL, TICK_SIZE, 0.1, FROM_A_VENUE_DOCUMENT)
    subject.upsert(VENUE, SYMBOL, TICK_SIZE, 0.1, FROM_MEASUREMENT)
    assert subject.fact(VENUE, SYMBOL, TICK_SIZE).source == FROM_MEASUREMENT
    assert len(subject.history_of(VENUE, SYMBOL, TICK_SIZE)) == 1


def test_two_sources_disagreeing_is_recorded_rather_than_resolved():
    """Overwriting one with the other silently discards information."""
    subject = a_fact_store(tolerance=0.01)
    subject.upsert(VENUE, SYMBOL, TICK_SIZE, 0.1, FROM_A_VENUE_DOCUMENT)
    _, outcome = subject.upsert(VENUE, SYMBOL, TICK_SIZE, 0.5, FROM_MEASUREMENT)
    assert outcome == FACT_CONTRADICTS
    assert subject.contradictions


def test_a_fact_carries_a_source_and_a_confidence():
    fact, _ = subject_fact = a_fact_store().upsert(
        VENUE, SYMBOL, TICK_SIZE, 0.1, FROM_A_VENUE_DOCUMENT,
        source_reference="https://venue.test/spec", confidence=0.9, observations=10,
    )
    assert fact.can_be_rechecked
    assert fact.confidence.is_fitted


def test_a_fact_with_no_reference_cannot_be_rechecked():
    fact, _ = a_fact_store().upsert(VENUE, SYMBOL, TICK_SIZE, 0.1, FROM_MEASUREMENT)
    assert fact.can_be_rechecked is False


def test_the_store_never_expires_a_fact_itself():
    """That would be it deciding, alone, that something had stopped being true."""
    assert importlib.import_module(
        BLOCK_PARTS["semantic-fact-store"]
    ).describe_semantic_facts(a_fact_store())["expires_facts_itself"] is False


# ---- episodic-trade-store ---------------------------------------------------

def an_episodic_store(maximum_returned=3, minimum_similarity=0.5, maximum_held=1000):
    return EpisodicTradeStore(
        maximum_returned=maximum_returned, minimum_similarity=minimum_similarity,
        maximum_held=maximum_held,
    )


def an_episode(episode_id="e-1", conditions=None, realised=0.02, symbol=SYMBOL):
    return TradeEpisode(
        episode_id=episode_id, venue_id=VENUE, symbol=symbol, detector="d", regime="trending",
        conditions=conditions or {"z_score": -2.0, "book_imbalance": 0.4},
        action="long", outcome="target", realised=realised, opened_at_ns=0,
        closed_at_ns=600_000_000_000, narrative="",
    )


def test_an_episode_cannot_be_edited_or_deleted():
    """There is no method to do it, so no future part can decide one was mistaken."""
    subject = an_episodic_store()
    assert not hasattr(subject, "edit")
    assert not hasattr(subject, "delete")
    assert not hasattr(subject, "revise")


def test_recall_returns_the_losses_too():
    """A store surfacing only winners makes every situation look like an opportunity."""
    subject = an_episodic_store(maximum_returned=10, minimum_similarity=0.5)
    for index in range(5):
        subject.append(an_episode(f"win-{index}", realised=0.02))
        subject.append(an_episode(f"loss-{index}", realised=-0.02))
    recall = subject.recall(VENUE, SYMBOL, {"z_score": -2.0, "book_imbalance": 0.4})
    assert recall.losing_returned > 0
    assert recall.profitable_returned > 0


def test_recall_says_what_it_left_out():
    """A truncated recall that stays quiet is a bias nobody sees."""
    subject = an_episodic_store(maximum_returned=2, minimum_similarity=0.5)
    for index in range(10):
        subject.append(an_episode(f"e-{index}"))
    recall = subject.recall(VENUE, SYMBOL, {"z_score": -2.0, "book_imbalance": 0.4})
    assert recall.was_truncated
    assert "not returned" in recall.reason


def test_nothing_similar_is_a_real_answer():
    subject = an_episodic_store(minimum_similarity=0.9)
    subject.append(an_episode(conditions={"z_score": 5.0}))
    recall = subject.recall(VENUE, SYMBOL, {"z_score": -5.0})
    assert recall.state == NOTHING_SIMILAR
    assert "nothing like this has happened here" in recall.reason


def test_recall_falls_back_to_the_raw_conditions_without_an_embedding():
    """An embedder that went down would otherwise make the system believe nothing happened."""
    subject = an_episodic_store(minimum_similarity=0.5)
    subject.append(an_episode())
    recall = subject.recall(VENUE, SYMBOL, {"z_score": -2.0, "book_imbalance": 0.4})
    assert recall.found_anything
    assert recall.episodes[0].matched_on == "conditions"


def test_recall_is_keyed_on_conditions_not_on_outcome():
    """Keying on outcome answers "what worked", which is survivorship bias."""
    subject = an_episodic_store(maximum_returned=10, minimum_similarity=0.9)
    subject.append(an_episode("similar-loss", conditions={"z_score": -2.0}, realised=-0.05))
    subject.append(an_episode("different-win", conditions={"z_score": 9.0}, realised=0.05))
    recall = subject.recall(VENUE, SYMBOL, {"z_score": -2.0})
    assert [entry.episode.episode_id for entry in recall.episodes] == ["similar-loss"]


# ---- procedural-playbook ----------------------------------------------------

class Instruction:
    def __init__(self, instruction_id="i-1", measurement="z_score", comparison="below",
                 threshold=-2.0, regime=None):
        self.instruction_id = instruction_id
        self.measurement = measurement
        self.comparison = comparison
        self.threshold = threshold
        self.regime_tag = regime
        self.reason = "it cleared every gate"


def test_the_playbook_has_no_search_method():
    """A rule that can be searched for is a suggestion."""
    subject = ProceduralPlaybook()
    for name in ("search", "find", "lookup", "similar_to", "matching"):
        assert not hasattr(subject, name)


def test_a_rule_fires_when_its_conditions_hold():
    subject = ProceduralPlaybook()
    subject.write_from_instruction(Instruction(), then="go long")
    application = subject.apply({"z_score": -3.0})
    assert application.state == APPLIED
    assert application.governing_rule.then == "go long"


def test_nothing_matching_is_the_procedure():
    application = ProceduralPlaybook().apply({"z_score": 0.0})
    assert application.state == NOTHING_MATCHED
    assert "nothing is chosen when nothing applies" in application.reason


def test_a_retired_instructions_rule_stops_immediately():
    """A rule outliving its instruction is a procedure whose justification is discarded."""
    subject = ProceduralPlaybook()
    subject.write_from_instruction(Instruction(), then="go long")
    subject.deactivate_for_instruction("i-1")
    assert subject.apply({"z_score": -3.0}).state == NOTHING_MATCHED


def test_the_more_specific_rule_applies_first():
    subject = ProceduralPlaybook()
    subject.write_from_instruction(Instruction("general", regime=None), then="general")
    subject.write_from_instruction(Instruction("specific", regime="trending"), then="specific")
    application = subject.apply({"z_score": -3.0}, regime="trending")
    assert application.governing_rule.then == "specific"


def test_a_rule_that_has_never_fired_is_one_the_system_has_rather_than_follows():
    subject = ProceduralPlaybook()
    subject.write_from_instruction(Instruction(), then="go long")
    assert len(subject.rules_never_fired()) == 1
    subject.apply({"z_score": -3.0})
    assert subject.rules_never_fired() == ()


# ---- symbol-profile-store ---------------------------------------------------

def a_profile_store(minimum=5, new_listing_days=7.0, clock=None):
    store = SymbolProfileStore(
        window=200, minimum_observations=minimum,
        new_listing_seconds=new_listing_days * 86400,
    )
    if clock is not None:
        store._now_ns = clock
    return store


def feed_profile(store, count=20, tick=None):
    for index in range(count):
        store.observe_market(
            VENUE, SYMBOL, spread_fraction=0.0002, quote_volume=1_000_000.0,
            move_fraction=0.01, observed_tick=tick,
        )


def test_a_measured_fact_beats_a_published_one():
    """What the venue says it does, against what the fills say it does."""
    subject = a_profile_store(minimum=3)
    subject.observe_published_fact(VENUE, SYMBOL, TICK_SIZE, 0.1)
    feed_profile(subject, tick=0.5)
    profile = subject.build(VENUE, SYMBOL)
    assert profile.field(TICK_SIZE).value == 0.5
    assert profile.field(TICK_SIZE).source == MEASURED


def test_an_unmeasured_field_is_absent_not_zero():
    """A typical spread of zero makes every symbol look free to trade."""
    profile = a_profile_store().build(VENUE, "NEVERSEENUSDT")
    assert profile.field(TYPICAL_SPREAD).value is None
    assert profile.field(TYPICAL_SPREAD).source == ABSENT


def test_every_field_says_whether_it_is_measured_or_assumed():
    subject = a_profile_store(minimum=3)
    subject.observe_published_fact(VENUE, SYMBOL, TICK_SIZE, 0.1)
    feed_profile(subject)
    profile = subject.build(VENUE, SYMBOL)
    assert profile.field(TICK_SIZE).source == PUBLISHED
    assert profile.field(TYPICAL_SPREAD).source == MEASURED


def test_a_new_listing_is_a_state_rather_than_something_to_infer():
    clock = Clock()
    subject = a_profile_store(minimum=3, new_listing_days=7.0, clock=clock)
    feed_profile(subject)
    assert subject.build(VENUE, SYMBOL).state == NEW_LISTING
    clock.advance_days(10)
    assert subject.build(VENUE, SYMBOL).state == ESTABLISHED


# ---- instruction-archive ----------------------------------------------------

class ArchivableInstruction:
    def __init__(self, instruction_id="i-1", family="f-1", trials=3, threshold=-2.0):
        self.instruction_id = instruction_id
        self.hypothesis_id = f"{family}:h"
        self.family = family
        self.measurement = "z_score"
        self.comparison = "below"
        self.threshold = threshold
        self.regime_tag = "trending"
        self.written_at_ns = 1_000
        self.trials_in_family = trials
        self.reason = "written"


def test_the_archive_never_removes_anything():
    """Removing makes the same idea look novel next month."""
    subject = InstructionArchive()
    assert not hasattr(subject, "remove")
    assert not hasattr(subject, "delete")
    subject.archive(ArchivableInstruction())
    subject.record_retirement("i-1", because="its criterion fired")
    assert subject.history_of("i-1") is not None


def test_the_whole_life_is_recorded_not_just_the_ending():
    subject = InstructionArchive()
    subject.archive(ArchivableInstruction())
    subject.record_trade("i-1", 0.01)
    subject.record_score("i-1", 0.6, 0.005)
    subject.record_retirement("i-1", "decayed")
    history = subject.history_of("i-1")
    assert {event.event for event in history.events} == {WRITTEN, TRADED, SCORED, RETIRED}
    assert history.seconds_alive is not None


def test_a_family_keeps_its_trial_count_after_every_member_retires():
    """The next member still needs the corrected bar."""
    subject = InstructionArchive()
    subject.archive(ArchivableInstruction("i-1", trials=100))
    subject.record_retirement("i-1", "decayed")
    assert subject.trials_in_family("f-1") == 100


def test_the_archive_answers_whether_this_has_been_tried():
    subject = InstructionArchive()
    subject.archive(ArchivableInstruction(threshold=-2.0))
    tried, which = subject.has_been_tried("z_score", "below", -2.05, tolerance=0.1)
    assert tried is True
    assert which == "i-1"


def test_a_resurrected_instruction_is_live_again():
    subject = InstructionArchive()
    subject.archive(ArchivableInstruction())
    subject.record_retirement("i-1", "its regime broke")
    subject.record_resurrection("i-1")
    assert subject.history_of("i-1").is_retired is False


# ---- fact-provenance-tracker ------------------------------------------------

def a_provenance_tracker(clock=None):
    tracker = FactProvenanceTracker()
    if clock is not None:
        tracker._now_ns = clock
    return tracker


def test_a_fact_with_no_source_is_recorded_as_having_none():
    """Rather than being attributed to whoever last touched it."""
    provenance = a_provenance_tracker().record("f-1", source=None)
    assert provenance.state == NO_SOURCE
    assert provenance.can_be_rechecked is False


def test_a_derived_fact_names_its_weakest_ancestor():
    """A derived fact is only as good as that."""
    subject = a_provenance_tracker()
    subject.record("root", source="somewhere", source_reference=None)
    subject.record("middle", source="inference", derived_from=("root",))
    provenance = subject.record("leaf", source="inference", derived_from=("middle",))
    assert provenance.weakest_ancestor in ("root", "middle")
    assert provenance.is_derived


def test_invalidating_a_source_invalidates_everything_derived_from_it():
    """This is how one bad source stops quietly poisoning everything below it."""
    subject = a_provenance_tracker()
    subject.record("root", source="a paper", source_reference="https://x.test")
    subject.record("middle", source="inference", derived_from=("root",))
    subject.record("leaf", source="inference", derived_from=("middle",))
    affected = subject.invalidate("root")
    assert set(affected) == {"root", "middle", "leaf"}


def test_a_source_that_has_gone_is_a_state_not_a_wrong_fact():
    subject = a_provenance_tracker()
    subject.record("f-1", source="a page", source_reference="https://gone.test")
    affected = subject.source_is_gone("https://gone.test")
    assert affected[0].state == SOURCE_IS_GONE
    assert "different from it being wrong" in affected[0].reason


def test_establishment_and_confirmation_are_different():
    clock = Clock()
    subject = a_provenance_tracker(clock=clock)
    subject.record("f-1", source="a page", source_reference="https://x.test")
    clock.advance_days(300)
    confirmed = subject.confirm("f-1")
    assert confirmed.age_seconds(clock()) > confirmed.seconds_since_confirmed(clock())


# ---- forgetting-curve-scheduler ---------------------------------------------

def a_forgetting_scheduler(recheck_below=0.5, clock=None):
    scheduler = ForgettingCurveScheduler(
        recheck_below=recheck_below, default_half_life_seconds=30 * 86400.0
    )
    if clock is not None:
        scheduler._now_ns = clock
    return scheduler


class Provenance:
    def __init__(self, established_at_ns, last_confirmed_at_ns=None, reachable=True, weakest=None):
        self.fact_key = "f-1"
        self.established_at_ns = established_at_ns
        self.last_confirmed_at_ns = last_confirmed_at_ns
        self.source_still_reachable = reachable
        self.weakest_ancestor = weakest


def test_confidence_decays_on_a_half_life_rather_than_a_cliff():
    """A cliff makes every decision lurch on the day a fact crosses it."""
    clock = Clock()
    subject = a_forgetting_scheduler(clock=clock)
    subject.observe_fact("f-1", TYPICAL_SPREAD, 1.0)
    subject.observe_provenance(Provenance(established_at_ns=clock()))
    fresh = subject.confidence_now("f-1").confidence
    clock.advance_days(3)
    half_life_later = subject.confidence_now("f-1").confidence
    assert half_life_later == pytest.approx(fresh * 0.5, rel=0.05)


def test_different_kinds_of_fact_decay_at_different_rates():
    """One rate either forgets the stable facts or trusts the volatile ones too long."""
    subject = a_forgetting_scheduler()
    assert subject.half_life_for(TICK_SIZE) > subject.half_life_for(TYPICAL_SPREAD)
    assert HALF_LIFE_SECONDS[TICK_SIZE] > HALF_LIFE_SECONDS[TYPICAL_VOLUME]


def test_confirmation_resets_the_decay():
    """Which is what makes rechecking worth doing."""
    clock = Clock()
    subject = a_forgetting_scheduler(clock=clock)
    subject.observe_fact("f-1", TYPICAL_SPREAD, 1.0)
    subject.observe_provenance(Provenance(established_at_ns=clock() - 10 * DAY_NS))
    stale = subject.confidence_now("f-1").confidence
    subject.observe_provenance(
        Provenance(established_at_ns=clock() - 10 * DAY_NS, last_confirmed_at_ns=clock())
    )
    assert subject.confidence_now("f-1").confidence > stale


def test_a_fact_whose_source_is_gone_cannot_be_rechecked():
    clock = Clock()
    subject = a_forgetting_scheduler(recheck_below=0.9, clock=clock)
    subject.observe_fact("f-1", TYPICAL_SPREAD, 1.0)
    subject.observe_provenance(
        Provenance(established_at_ns=clock() - 30 * DAY_NS, reachable=False)
    )
    assert subject.confidence_now("f-1").state == CANNOT_BE_RECHECKED


def test_a_fact_with_no_provenance_has_no_confidence():
    subject = a_forgetting_scheduler()
    subject.observe_fact("f-1", TYPICAL_SPREAD, 1.0)
    assert subject.confidence_now("f-1").state == NO_PROVENANCE


def test_the_scheduler_never_deletes():
    """An old fact that is still true is still true."""
    assert importlib.import_module(
        BLOCK_PARTS["forgetting-curve-scheduler"]
    ).describe_forgetting_curve(a_forgetting_scheduler())["deletes_facts"] is False


# ---- knowledge-pruner -------------------------------------------------------

def a_pruner(confidence_floor=0.2, idle_days=30.0, clock=None):
    pruner = KnowledgePruner(
        confidence_floor=confidence_floor, idle_seconds_before_useless=idle_days * 86400
    )
    if clock is not None:
        pruner._now_ns = clock
    return pruner


def test_age_alone_does_not_earn_removal():
    """A tick size established two years ago and never rechecked is still correct."""
    clock = Clock()
    subject = a_pruner(clock=clock)
    subject.observe_established("f-1", clock() - 700 * DAY_NS)
    subject.observe_confidence("f-1", 0.9)
    judgement = subject.judge("f-1")
    assert judgement.state == KEPT
    assert judgement.because == ONLY_OLD


def test_nothing_is_pruned_while_something_still_reads_it():
    """Removing it makes a live part fail for a reason nobody will connect to this."""
    clock = Clock()
    subject = a_pruner(clock=clock)
    subject.observe_confidence("f-1", 0.0)
    subject.observe_contradiction("f-1", "a better source")
    subject.observe_read("f-1")
    judgement = subject.judge("f-1")
    assert judgement.state == KEPT
    assert judgement.because == STILL_IN_USE


def test_uselessness_needs_all_three_conditions():
    clock = Clock()
    subject = a_pruner(confidence_floor=0.5, clock=clock)
    subject.observe_confidence("f-1", 0.1)
    subject.observe_contradiction("f-1", "a better source")
    judgement = subject.judge("f-1")
    assert judgement.state == PRUNED
    assert judgement.because == USELESS
    assert "any one alone is not evidence" in judgement.reason


def test_a_contested_supersession_keeps_the_old_value():
    """The contest may resolve the other way."""
    subject = a_pruner()
    subject.observe_supersession("f-1", "f-2", is_settled=False)
    assert subject.judge("f-1").because == SUPERSESSION_UNSETTLED
    subject.observe_supersession("f-1", "f-2", is_settled=True)
    assert subject.judge("f-1").because == SUPERSEDED_AND_SETTLED


def test_a_fact_resting_on_something_invalidated_goes_immediately():
    subject = a_pruner()
    subject.observe_invalidated_ancestor("f-1")
    judgement = subject.judge("f-1")
    assert judgement.state == PRUNED
    assert judgement.because == RESTS_ON_SOMETHING_INVALIDATED


def test_every_removal_is_recorded():
    """A deletion leaving no trace makes the history unreconstructable."""
    subject = a_pruner()
    subject.observe_invalidated_ancestor("f-1")
    subject.prune(["f-1"])
    assert len(subject.removals) == 1
    assert subject.removals[0]["knowledge_key"] == "f-1"


# ---- knowledge-graph-linker -------------------------------------------------

def a_linker(minimum_strength=0.3, half_life_days=30.0, clock=None):
    linker = KnowledgeGraphLinker(
        minimum_strength=minimum_strength, half_life_seconds=half_life_days * 86400
    )
    if clock is not None:
        linker._now_ns = clock
    return linker


def test_links_are_typed_because_relations_mean_different_things():
    subject = a_linker()
    for kind in LINK_KINDS:
        assert subject.link("a", "b", kind, 0.9, "a source")[1] == LINKED
    assert subject.link("a", "b", "vaguely-related", 0.9, "a source")[1] == UNKNOWN_KIND


def test_a_weak_relation_is_not_recorded():
    """Without a floor everything links to everything and the graph says nothing."""
    assert a_linker(minimum_strength=0.5).link("a", "b", MOVES_WITH, 0.1, "measurement")[1] == TOO_WEAK


def test_only_explains_claims_a_mechanism():
    """A graph recording sequence as causal lets the system believe it found one."""
    subject = a_linker()
    explains, _ = subject.link("a-skill", "an-instruction", EXPLAINS, 0.9, "a source")
    preceded, _ = subject.link("episode-1", "episode-2", PRECEDED, 0.9, "the tape")
    assert explains.is_causal_claim is True
    assert preceded.is_causal_claim is False
    assert "rather than causation" in preceded.reason


def test_a_link_decays_and_is_dropped():
    """A graph that never forgets accumulates every relation that has briefly been true."""
    clock = Clock()
    subject = a_linker(minimum_strength=0.3, half_life_days=10.0, clock=clock)
    subject.link("a", "b", MOVES_WITH, 0.9, "measurement")
    assert subject.links_from("a")
    clock.advance_days(100)
    assert subject.prune_decayed()
    assert subject.links_from("a") == ()


def test_a_measured_correlation_becomes_a_moves_with_link():
    subject = a_linker(minimum_strength=0.3)
    for _ in range(10):
        subject.observe_correlation("AUSDT", "BUSDT", 0.9)
    assert subject.related("AUSDT", "BUSDT", MOVES_WITH) is not None


def test_a_symmetric_relation_is_stored_once():
    subject = a_linker()
    subject.link("a", "b", MOVES_WITH, 0.9, "measurement")
    subject.link("b", "a", MOVES_WITH, 0.9, "measurement")
    assert subject.link_count == 1


# ---- contradiction-detector -------------------------------------------------

class DetectableFact:
    def __init__(self, value, source, confidence=0.8, venue=VENUE, symbol=SYMBOL, key=TICK_SIZE):
        self.venue_id, self.symbol, self.key = venue, symbol, key
        self.value = value
        self.source = source

        class C:
            pass

        self.confidence = C()
        self.confidence.value = confidence


class DetectableRule:
    def __init__(self, rule_id, then, instruction_id=None, active=True):
        self.rule_id = rule_id
        self.when = "a condition"
        self.then = then
        self.instruction_id = instruction_id
        self.is_active = active


def a_contradiction_detector(tolerance=0.01):
    return ContradictionDetector(value_tolerance=tolerance)


def test_two_sources_disagreeing_about_a_fact_is_a_contradiction():
    subject = a_contradiction_detector()
    subject.observe_fact(DetectableFact(0.1, "the venue"))
    subject.observe_fact(DetectableFact(0.5, "measurement"))
    found = subject.check()
    assert any(entry.kind == FACT_AGAINST_FACT for entry in found)


def test_confidence_is_reported_and_never_used_to_pick_a_winner():
    """A high-confidence stale fact beats a low-confidence fresh one, which is backwards."""
    subject = a_contradiction_detector()
    subject.observe_fact(DetectableFact(0.1, "stale", confidence=0.99))
    subject.observe_fact(DetectableFact(0.5, "fresh", confidence=0.10))
    contradiction = subject.check()[0]
    assert contradiction.left_confidence != contradiction.right_confidence
    assert "exactly backwards" in contradiction.reason


def test_a_rule_firing_on_something_that_never_happens_is_a_contradiction():
    subject = a_contradiction_detector()
    subject.observe_rule(
        DetectableRule("r-1", "go long"),
        {"measurement": "z_score", "comparison": "above", "threshold": 99.0,
         "observed_range": (-3.0, 3.0)},
    )
    found = subject.check()
    assert any(entry.kind == FACT_AGAINST_RULE for entry in found)
    assert any("dead and nobody has noticed" in entry.reason for entry in found)


def test_two_rules_saying_opposite_things_is_urgent():
    """Whichever ordering applies becomes the behaviour, and nobody chose it."""
    subject = a_contradiction_detector()
    condition = {"measurement": "z_score", "comparison": "above", "threshold": 1.0}
    subject.observe_rule(DetectableRule("r-1", "go long"), condition)
    subject.observe_rule(DetectableRule("r-2", "go short"), condition)
    found = subject.check()
    assert any(entry.kind == RULE_AGAINST_RULE for entry in found)
    assert subject.standing.urgent_rule_conflicts == 1


def test_a_link_the_measurements_contradict_is_found():
    subject = a_contradiction_detector(tolerance=0.5)
    subject.observe_link("AUSDT", "BUSDT", MOVES_WITH, 0.9)
    found = subject.check({("AUSDT", "BUSDT"): 0.01})
    assert any(entry.kind == LINK_AGAINST_FACT for entry in found)


def test_the_detector_resolves_nothing():
    assert importlib.import_module(
        BLOCK_PARTS["contradiction-detector"]
    ).describe_contradictions(a_contradiction_detector())["resolves_anything"] is False


# ---- episode-embedder -------------------------------------------------------

def an_embedder(dimensions=("z_score", "book_imbalance", "funding"), minimum_features=2):
    return EpisodeEmbedder(
        dimensions=dimensions, half_life_observations=500, minimum_observations=5,
        minimum_features=minimum_features, default_reliability=1.0,
    )


def teach_embedder(embedder, count=30):
    for index in range(count):
        embedder.observe_conditions(
            {"z_score": 1.0 if index % 2 else -1.0,
             "book_imbalance": 0.5 if index % 2 else -0.5,
             "funding": 0.001 if index % 2 else -0.001}
        )


def test_a_missing_feature_is_marked_not_zeroed():
    """A situation where funding was unavailable would be recorded as exactly average."""
    subject = an_embedder()
    teach_embedder(subject)
    embedding = subject.embed("e-1", {"z_score": 2.0, "book_imbalance": 0.3})
    assert "funding" in embedding.features_missing
    assert embedding.is_complete is False


def test_a_feature_that_predicts_nothing_does_not_push_situations_apart():
    """Unweighted, the noisiest features dominate precisely because they vary most."""
    subject = an_embedder()
    teach_embedder(subject)
    subject.observe_reliability("z_score", 1.0)
    subject.observe_reliability("book_imbalance", 0.0)
    subject.observe_reliability("funding", 1.0)
    left = subject.embed("e-1", {"z_score": 1.0, "book_imbalance": 5.0, "funding": 0.001})
    right = subject.embed("e-2", {"z_score": 1.0, "book_imbalance": -5.0, "funding": 0.001})
    assert subject.distance(left, right) == pytest.approx(0.0)


def test_the_embedding_is_deterministic():
    """So a surprising recall can be investigated rather than re-rolled."""
    subject = an_embedder()
    teach_embedder(subject)
    conditions = {"z_score": 2.0, "book_imbalance": 0.3, "funding": 0.002}
    assert subject.embed("e-1", conditions).vector == subject.embed("e-1", conditions).vector


def test_too_few_standardisable_features_still_leaves_the_episode_recallable():
    subject = an_embedder(minimum_features=3)
    embedding = subject.embed("e-1", {"z_score": 2.0})
    assert embedding.state == TOO_FEW_FEATURES
    assert "still recallable" in embedding.reason


def test_two_vectors_over_different_features_are_not_compared():
    """Comparing them produces a number that means nothing."""
    subject = an_embedder(minimum_features=1)
    teach_embedder(subject)
    left = subject.embed("e-1", {"z_score": 1.0})
    right = subject.embed("e-2", {"book_imbalance": 1.0})
    assert subject.distance(left, right) is None


# ---- regime-memory-store ----------------------------------------------------

def a_regime_store(minimum_occurrences=2, tolerance=0.5, minimum_trades=5):
    return RegimeMemoryStore(
        minimum_occurrences=minimum_occurrences, signature_tolerance=tolerance,
        minimum_instruction_trades=minimum_trades, prior_hit_rate=0.5, prior_weight=4.0,
        half_life_observations=500,
    )


def a_signature(volatility=0.02, correlation=0.5, move=0.01, hurst=0.6):
    return RegimeSignature(
        volatility=volatility, correlation=correlation, typical_move=move, hurst=hurst
    )


def test_a_regime_seen_once_is_a_story_rather_than_a_memory():
    subject = a_regime_store(minimum_occurrences=3)
    subject.record_occurrence("trending", a_signature(), 10 * 86400, "volatility stepped")
    assert subject.recognise(a_signature()).state == TOO_FEW_OCCURRENCES


def test_a_recurrence_is_recognised_on_the_signature_not_the_name():
    """Names are assigned by a classifier that can change."""
    subject = a_regime_store(minimum_occurrences=2, tolerance=0.5)
    for _ in range(3):
        subject.record_occurrence("trending", a_signature(), 10 * 86400, "it broke")
    memory = subject.recognise(a_signature(volatility=0.021))
    assert memory.state == RECOGNISED
    assert memory.regime == "trending"


def test_a_genuinely_different_signature_is_a_new_regime():
    subject = a_regime_store(minimum_occurrences=2, tolerance=0.2)
    for _ in range(3):
        subject.record_occurrence("trending", a_signature(), 10 * 86400, "it broke")
    assert subject.recognise(a_signature(volatility=5.0, hurst=0.1)).state == NEW_REGIME


def test_the_memory_splits_instructions_by_what_they_did():
    """So they can be reweighted on recurrence rather than relearned."""
    subject = a_regime_store(minimum_occurrences=2, minimum_trades=5)
    for _ in range(3):
        subject.record_occurrence("trending", a_signature(), 10 * 86400, "it broke")
    for index in range(20):
        subject.record_instruction_outcome("trending", "worked", True)
        subject.record_instruction_outcome("trending", "did-not", False)
    memory = subject.memory_of("trending")
    assert "worked" in memory.instructions_that_worked
    assert "did-not" in memory.instructions_that_did_not
    assert "Reweighted, not reinstated" in memory.reason


def test_a_wildly_variable_duration_is_trusted_less():
    subject = a_regime_store(minimum_occurrences=2)
    for duration in (1 * 86400, 400 * 86400, 3 * 86400):
        subject.record_occurrence("trending", a_signature(), duration, "it broke")
    assert subject.memory_of("trending").duration_is_predictable is False


# ---- knowledge-snapshot-versioner -------------------------------------------

def a_versioner():
    return KnowledgeSnapshotVersioner()


def test_a_snapshot_is_taken_on_change_not_on_a_timer():
    """A timer either misses the change that mattered or fills the archive."""
    subject = a_versioner()
    subject.observe_contents(FACTS, {"f-1"})
    assert subject.take().state == TAKEN
    assert subject.take().state == UNCHANGED
    subject.observe_contents(FACTS, {"f-1", "f-2"})
    assert subject.take().state == TAKEN


def test_a_snapshot_names_what_changed():
    subject = a_versioner()
    subject.observe_contents(FACTS, {"f-1"})
    subject.take()
    subject.observe_contents(FACTS, {"f-2"})
    snapshot = subject.take()
    assert snapshot.added[FACTS] == ("f-2",)
    assert snapshot.removed[FACTS] == ("f-1",)


def test_snapshots_cannot_be_edited():
    subject = a_versioner()
    assert not hasattr(subject, "edit")
    assert not hasattr(subject, "amend")


def test_a_decision_is_reviewed_against_the_knowledge_it_was_made_with():
    """Reviewing against present knowledge is how a reasonable decision looks obviously wrong."""
    subject = a_versioner()
    subject.observe_contents(FACTS, {"f-1"})
    subject.take()
    version = subject.stamp_decision("d-1")
    subject.observe_contents(FACTS, {"f-1", "f-2"})
    subject.take()
    snapshot, reason = subject.knowledge_behind("d-1")
    assert snapshot.version == version
    assert "f-2" not in snapshot.added.get(FACTS, ())


def test_a_decision_with_no_snapshot_is_unreviewable():
    snapshot, reason = a_versioner().knowledge_behind("never-stamped")
    assert snapshot is None
    assert "cannot be reviewed fairly" in reason


def test_a_snapshot_is_a_manifest_rather_than_a_copy():
    subject = a_versioner()
    subject.observe_contents(FACTS, {f"f-{index}" for index in range(1000)})
    snapshot = subject.take()
    assert snapshot.counts[FACTS] == 1000
    assert len(snapshot.digests[FACTS]) == 16
