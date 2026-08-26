"""The autonomous block: a system that keeps itself running and changes itself.

One asymmetry governs everything here: expanding what the system may do requires
evidence, and contracting it requires none. These tests are about the places that
asymmetry is enforced -- the override nothing inside can clear, the halt that fails
towards stopping, the envelope that closes on one reason and opens on all of them,
and the gate that admits nothing the system wrote until every check passes.
"""

import importlib

import pytest

from parts.autonomous.autonomy_boundary import (
    AutonomyBoundary, A_FAULT_IS_UNRESOLVED, A_HUMAN_SAID_SO, COMPETENCE_FELL, HELD,
    NARROWED, WIDENED,
)
from parts.autonomous.autonomy_policy_engine import (
    ABOVE_THE_ENVELOPE_CEILING, ABOVE_WHAT_THIS_SUBJECT_HAS_EARNED, ADMIT_A_PART,
    ALLOWED, AutonomyPolicyEngine, CLOSE_A_POSITION, LEVEL_FORBIDS_IT, OPEN_A_POSITION,
    SUBJECT_IS_UNMEASURED, UNKNOWN_ACTION,
)
from parts.autonomous.capability_gap_finder import (
    ALREADY_KNOWN as GAP_ALREADY_KNOWN, CapabilityGapFinder, FOUND as GAP_FOUND,
    NEEDS_A_VENUE_ACCOUNT, NOT_NAMEABLE, NOT_REACHABLE,
)
from parts.autonomous.conservation_planner import (
    ConservationPlanner, CUT_ORDER, DISCRETIONARY_RESEARCH, NEW_POSITION_ENTRY,
    NOTHING_TO_CUT, PLANNED as CONSERVATION_PLANNED, POSITION_MANAGEMENT,
    REFUSED_PROTECTS_CAPITAL,
)
from parts.autonomous.failing_part_detector import (
    CRASHED, FailingPartDetector, GETTING_SLOWER, HEALTHY, NOT_ENOUGH_HISTORY,
    STOPPED_PRODUCING, STUCK_ON_ONE_ANSWER, SUSPICIOUSLY_PERFECT,
)
from parts.autonomous.folded_circuit_view import (
    FOLDED, FoldedCircuitView, NEVER_STARTED, NOT_MEASURED,
    RUNNING,
)
from parts.autonomous.human_override_reader import (
    HumanOverrideReader, MALFORMED, NONE_PRESENT, READ, STOP_EVERYTHING, STOP_TRADING,
    UNREADABLE,
)
from parts.autonomous.no_progress_detector import (
    NOTHING_EXPECTED, NoProgressDetector, PROGRESSING, SLOWING, STALLED_AT_A_STAGE,
)
from parts.autonomous.part_admission_gate import (
    ADMITTED, A_HUMAN_SAID_NO, CONTRACT_BROKEN, DUPLICATES_AN_EXISTING_PART,
    ENVELOPE_FORBIDS_IT, NOT_ON_REAL_DATA, PartAdmissionGate, TESTS_FAILED,
)
from parts.autonomous.part_author import (
    NAMES_ANOTHER_PART, NO_GAP, NO_TESTS, PRODUCES_NOTHING, PROPOSED, PartAuthor,
    UNDECLARED_DATA,
)
from parts.autonomous.part_replacement_planner import (
    NO_REPLACEMENT, PartReplacementPlanner, PLANNED as REPLACEMENT_PLANNED,
    START_THEN_STOP, STOP_THEN_START,
)
from parts.autonomous.self_modification_journal import (
    APPLIED, NOT_AUTHORISED, NOT_RECORDED_FIRST, RECORDED, SelfModificationJournal,
)
from parts.autonomous.survival_tier_monitor import (
    MEASURED, MONEY, NOT_MEASURED as TIER_NOT_MEASURED, QUOTA, SurvivalTierMonitor,
)
from parts.autonomous.trading_halt_decider import (
    EXPOSURE_UNKNOWN, HALTED, HUMAN_OVERRIDE, MARKET_ANOMALY, TRADING,
    TradingHaltDecider, VENUE_UNREACHABLE_WITH_EXPOSURE,
)
from parts.autonomous.unattended_run_warden import (
    BACKING_OFF, GIVE_UP_AND_REPLACE, HOLDS_CAPITAL_STATE, RESTART, SYSTEM_WIDE_CAUSE,
    UnattendedRunWarden, UNRECOGNISED_FAULT,
)
from parts.autonomous.upstream_improvement_watch import (
    AFFECTS_NOTHING, BREAKING, IMPROVEMENT, NOTICED, SILENT_BEHAVIOUR_CHANGE,
    UpstreamImprovementWatch,
)
from parts.autonomous.venue_outage_rider import (
    CONNECTION_LOST, FLOWING, QUIET_MARKET, UNREACHABLE_WITH_EXPOSURE, VENUE_DOWN,
    VenueOutageRider,
)
from runtime.autonomy_types import (
    ACT_WITHIN_LIMITS, COMFORTABLE, CRITICAL, FRUGAL, MODIFY_ITSELF, OBSERVE_ONLY,
    PROPOSE_ONLY, PartFault, SHUTDOWN,
)
from runtime.part_declaration import load_declaration_from_blueprint

BLOCK_PARTS = {
    "unattended-run-warden": "parts.autonomous.unattended_run_warden",
    "venue-outage-rider": "parts.autonomous.venue_outage_rider",
    "capability-gap-finder": "parts.autonomous.capability_gap_finder",
    "part-author": "parts.autonomous.part_author",
    "part-admission-gate": "parts.autonomous.part_admission_gate",
    "autonomy-boundary": "parts.autonomous.autonomy_boundary",
    "trading-halt-decider": "parts.autonomous.trading_halt_decider",
    "failing-part-detector": "parts.autonomous.failing_part_detector",
    "part-replacement-planner": "parts.autonomous.part_replacement_planner",
    "survival-tier-monitor": "parts.autonomous.survival_tier_monitor",
    "conservation-planner": "parts.autonomous.conservation_planner",
    "autonomy-policy-engine": "parts.autonomous.autonomy_policy_engine",
    "self-modification-journal": "parts.autonomous.self_modification_journal",
    "no-progress-detector": "parts.autonomous.no_progress_detector",
    "upstream-improvement-watch": "parts.autonomous.upstream_improvement_watch",
    "folded-circuit-view": "parts.autonomous.folded_circuit_view",
    "human-override-reader": "parts.autonomous.human_override_reader",
}

SECOND_NS = 1_000_000_000


class Clock:
    def __init__(self, now_ns=1_700_000_000_000_000_000):
        self.now_ns = now_ns

    def __call__(self):
        return self.now_ns


class TickingClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def a_fault(part_id="some-part", kind="crashed", detail="it crashed", severity="fatal"):
    return PartFault(
        part_id=part_id, kind=kind, detail=detail, first_seen_at_ns=0, observations=10,
        is_silent=False, severity=severity, reason="", detected_at_ns=0,
    )


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_no_part_in_this_block_imports_another_part(part_id):
    with open(importlib.import_module(BLOCK_PARTS[part_id]).__file__, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(("from parts.", "import parts.")):
                raise AssertionError(f"{part_id} imports another part: {line.strip()}")


# ---- human-override-reader --------------------------------------------------

def an_override_reader():
    return HumanOverrideReader(now_ns=Clock())


def test_an_unreadable_source_is_treated_as_an_active_stop():
    """If the door cannot be checked, the system does not assume nobody is at it."""
    subject = an_override_reader()
    reading = subject.read()
    assert reading.state == UNREADABLE
    assert reading.override.instruction == STOP_EVERYTHING
    assert reading.stops_something


def test_nothing_in_the_system_can_revoke_an_override():
    subject = an_override_reader()
    assert not hasattr(subject, "revoke")
    assert not hasattr(subject, "clear")
    described = importlib.import_module(
        BLOCK_PARTS["human-override-reader"]
    ).describe_override_reading(subject)
    assert described["can_revoke_an_override"] is False
    assert described["reads_system_state"] is False


def test_an_instruction_it_cannot_parse_becomes_a_stop():
    subject = an_override_reader()
    subject.install_source(lambda: {"instruction": "go-faster"})
    reading = subject.read()
    assert reading.state == MALFORMED
    assert reading.override.instruction == STOP_EVERYTHING


def test_an_override_is_read_and_carried():
    clock = Clock()
    subject = HumanOverrideReader(now_ns=clock)
    subject.install_source(
        lambda: {"instruction": STOP_TRADING, "scope": "everything",
                 "issued_at_ns": clock.now_ns}
    )
    reading = subject.read()
    assert reading.state == READ
    assert reading.override.is_active
    assert "nothing in this system can clear it" in reading.reason


def test_no_override_present_is_its_own_answer():
    subject = an_override_reader()
    subject.install_source(lambda: None)
    reading = subject.read()
    assert reading.state == NONE_PRESENT
    assert reading.override is None


def test_the_reader_consumes_nothing_by_declaration():
    assert load_declaration_from_blueprint("human-override-reader").consumes == ()


# ---- failing-part-detector --------------------------------------------------

def a_detector(window=10, minimum=4, stuck=3, slowdown=3.0, perfect=20):
    return FailingPartDetector(
        window=window, minimum_ticks=minimum, stuck_answer_ticks=stuck,
        slowdown_ratio=slowdown, perfect_run_ticks=perfect, now_ns=Clock(),
    )


def _tick(detector, part_id="p-1", count=5, produced=1, errors=1, digest=None,
          seconds=0.1):
    for index in range(count):
        detector.observe_health(
            part_id, tick_seconds=seconds, produced=produced, errors=errors,
            output_digest=digest if digest is not None else f"d-{index}",
        )


def test_a_part_alive_and_producing_nothing_is_a_silent_fault():
    subject = a_detector()
    _tick(subject, produced=0)
    outcome = subject.check("p-1")
    assert outcome.state == STOPPED_PRODUCING
    assert outcome.fault.is_silent
    assert subject.standing.silent_faults_found == 1


def test_a_part_returning_the_same_answer_is_stuck():
    subject = a_detector(stuck=3)
    _tick(subject, digest="always-the-same")
    assert subject.check("p-1").state == STUCK_ON_ONE_ANSWER


def test_a_part_getting_slower_against_its_own_baseline_is_flagged():
    subject = a_detector(minimum=4, slowdown=2.0)
    for index in range(4):
        subject.observe_health("p-1", 0.1, 1, 1, f"d-{index}")
    for index in range(4):
        subject.observe_health("p-1", 1.0, 1, 1, f"e-{index}")
    assert subject.check("p-1").state == GETTING_SLOWER


def test_a_part_with_no_errors_at_all_is_suspect():
    """Zero errors in a system that talks to venues means they are being swallowed."""
    subject = a_detector(perfect=10, slowdown=100.0)
    _tick(subject, count=12, errors=0)
    assert subject.check("p-1").state == SUSPICIOUSLY_PERFECT


def test_a_crash_is_fatal_and_a_restart_is_the_treatment():
    subject = a_detector()
    subject.observe_crash("p-1", "traceback")
    outcome = subject.check("p-1")
    assert outcome.state == CRASHED
    assert outcome.fault.needs_restarting


def test_a_part_is_judged_against_its_own_history():
    subject = a_detector(minimum=20)
    _tick(subject, count=3)
    assert subject.check("p-1").state == NOT_ENOUGH_HISTORY
    assert importlib.import_module(
        BLOCK_PARTS["failing-part-detector"]
    ).describe_detection(subject)["uses_one_global_threshold"] is False


def test_a_working_part_is_healthy():
    subject = a_detector(slowdown=100.0, perfect=1000)
    _tick(subject, count=6)
    assert subject.check("p-1").state == HEALTHY


# ---- no-progress-detector ---------------------------------------------------

def a_progress_detector(minimum_inputs=3, slowdown=5.0):
    return NoProgressDetector(
        stages=("candidates", "opinions", "intents", "orders"), window=5,
        minimum_inputs=minimum_inputs, slowdown_ratio=slowdown, now_ns=Clock(),
    )


def test_quiet_is_not_stalled():
    subject = a_progress_detector(minimum_inputs=10)
    subject.observe("candidates", 1)
    outcome = subject.check()
    assert outcome.state == NOTHING_EXPECTED
    assert "Quiet is not stalled" in outcome.reason


def test_a_stall_is_located_between_two_named_stages():
    subject = a_progress_detector(minimum_inputs=3)
    subject.observe("candidates", 10)
    subject.observe("opinions", 10)
    outcome = subject.check()
    assert outcome.state == STALLED_AT_A_STAGE
    assert outcome.stalled_stage == "intents"
    assert "rejecting everything and reporting success" in outcome.reason


def test_a_gradual_fall_is_the_same_failure_arriving_slowly():
    subject = a_progress_detector(minimum_inputs=1, slowdown=2.0)
    for _ in range(5):
        for stage in ("candidates", "opinions", "intents", "orders"):
            subject.observe(stage, 100)
        subject.roll_period()
    for stage in ("candidates", "opinions", "intents"):
        subject.observe(stage, 100)
    subject.observe("orders", 1)
    assert subject.check().state == SLOWING


def test_a_working_pipeline_is_progressing():
    subject = a_progress_detector(minimum_inputs=1)
    for stage in ("candidates", "opinions", "intents", "orders"):
        subject.observe(stage, 5)
    assert subject.check().state == PROGRESSING


def test_an_unnamed_stage_is_refused():
    subject = a_progress_detector()
    with pytest.raises(ValueError):
        subject.observe("somewhere-in-the-middle", 1)


# ---- unattended-run-warden --------------------------------------------------

def a_warden(maximum=3, window=60.0, ceiling=3, monotonic=None):
    return UnattendedRunWarden(
        maximum_restarts=maximum, within_seconds=window, initial_backoff_seconds=1.0,
        backoff_multiplier=2.0, system_wide_ceiling=ceiling,
        monotonic=monotonic or TickingClock(), now_ns=Clock(),
    )


def test_the_second_restart_is_a_fix_and_the_fifth_is_a_loop():
    clock = TickingClock()
    subject = a_warden(maximum=2, monotonic=clock)
    assert subject.decide(a_fault()).state == RESTART
    clock.now = 10.0
    assert subject.decide(a_fault()).state == RESTART
    clock.now = 20.0
    outcome = subject.decide(a_fault())
    assert outcome.state == GIVE_UP_AND_REPLACE
    assert outcome.escalate


def test_the_backoff_grows():
    subject = a_warden()
    assert subject.backoff_for(3) > subject.backoff_for(1)


def test_a_part_holding_capital_state_is_never_restarted():
    subject = a_warden()
    subject.declare_holds_capital_state("position-keeper")
    outcome = subject.decide(a_fault("position-keeper"))
    assert outcome.state == HOLDS_CAPITAL_STATE
    assert outcome.escalate


def test_many_parts_failing_at_once_is_one_cause():
    clock = TickingClock()
    subject = a_warden(maximum=5, ceiling=2, monotonic=clock)
    subject.decide(a_fault("a"))
    subject.decide(a_fault("b"))
    outcome = subject.decide(a_fault("c"))
    assert outcome.state == SYSTEM_WIDE_CAUSE
    assert "makes the cause harder to see" in outcome.reason


def test_restarting_is_not_a_general_purpose_remedy():
    subject = a_warden()
    outcome = subject.decide(a_fault(kind="producing-the-same-answer-every-time"))
    assert outcome.state == UNRECOGNISED_FAULT
    assert outcome.escalate


def test_a_restart_inside_its_backoff_waits():
    clock = TickingClock()
    subject = a_warden(monotonic=clock)
    subject.decide(a_fault())
    outcome = subject.decide(a_fault())
    assert outcome.state == BACKING_OFF
    assert outcome.request.is_backing_off


# ---- venue-outage-rider -----------------------------------------------------

def a_rider(silence=30.0, fraction=0.8, failures=3, clock=None):
    return VenueOutageRider(
        silence_seconds=silence, venue_wide_fraction=fraction,
        failures_before_venue_down=failures, now_ns=clock or Clock(),
    )


def _messages(rider, clock, symbols, ago_seconds=0.0):
    for symbol in symbols:
        rider.observe_message(
            "binance-usdm", symbol, clock.now_ns - int(ago_seconds * 1e9)
        )


def test_one_quiet_symbol_is_that_symbol_not_the_venue():
    clock = Clock()
    subject = a_rider(silence=10.0, clock=clock)
    _messages(subject, clock, ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    subject.observe_message("binance-usdm", "TINYUSDT", clock.now_ns - 60 * SECOND_NS)
    outcome = subject.measure("binance-usdm")
    assert outcome.state == QUIET_MARKET
    assert "reconnecting would achieve nothing" in outcome.reason


def test_everything_silent_with_working_requests_means_this_system_is_deaf():
    clock = Clock()
    subject = a_rider(silence=10.0, clock=clock)
    _messages(subject, clock, ["BTCUSDT", "ETHUSDT"], ago_seconds=60.0)
    outcome = subject.measure("binance-usdm")
    assert outcome.state == CONNECTION_LOST


def test_everything_silent_with_failed_requests_means_the_venue_is_down():
    clock = Clock()
    subject = a_rider(silence=10.0, failures=2, clock=clock)
    _messages(subject, clock, ["BTCUSDT", "ETHUSDT"], ago_seconds=60.0)
    subject.observe_request_failure("binance-usdm")
    subject.observe_request_failure("binance-usdm")
    assert subject.measure("binance-usdm").state == VENUE_DOWN


def test_unreachable_with_exposure_is_its_own_state():
    clock = Clock()
    subject = a_rider(silence=10.0, clock=clock)
    _messages(subject, clock, ["BTCUSDT"], ago_seconds=60.0)
    subject.observe_exposure("binance-usdm", True)
    outcome = subject.measure("binance-usdm")
    assert outcome.state == UNREACHABLE_WITH_EXPOSURE
    assert outcome.needs_a_human
    assert outcome.outage.is_dangerous


def test_flowing_data_is_flowing():
    clock = Clock()
    subject = a_rider(silence=60.0, clock=clock)
    _messages(subject, clock, ["BTCUSDT", "ETHUSDT"])
    assert subject.measure("binance-usdm").state == FLOWING


def test_the_rider_reconnects_nothing():
    assert importlib.import_module(
        BLOCK_PARTS["venue-outage-rider"]
    ).describe_outage_riding(a_rider())["reconnects"] is False


# ---- survival-tier-monitor --------------------------------------------------

THRESHOLDS = {FRUGAL: 0.5, CRITICAL: 0.2, SHUTDOWN: 0.05}


def a_tier_monitor(margin=0.1, readings=2):
    return SurvivalTierMonitor(
        thresholds=THRESHOLDS, improvement_margin=margin,
        readings_before_improving=readings, burn_window=5, now_ns=Clock(),
    )


def test_unmeasured_is_the_most_restrictive_tier():
    """Assuming runway that has not been measured is how a system stops abruptly."""
    outcome = a_tier_monitor().measure()
    assert outcome.state == TIER_NOT_MEASURED
    assert outcome.tier.tier == SHUTDOWN


def test_the_binding_resource_decides_rather_than_an_average():
    subject = a_tier_monitor()
    subject.observe(QUOTA, 0.9)
    subject.observe(MONEY, 0.1)
    outcome = subject.measure()
    assert outcome.tier.binding_resource == MONEY
    assert outcome.tier.tier == CRITICAL


def test_falling_is_immediate_and_rising_is_slow():
    subject = a_tier_monitor(margin=0.1, readings=3)
    subject.observe(QUOTA, 0.9)
    subject.measure()
    subject.observe(QUOTA, 0.1)
    fell = subject.measure()
    assert fell.tier.tier == CRITICAL
    subject.observe(QUOTA, 0.9)
    held = subject.measure()
    assert held.held_by_hysteresis
    assert held.tier.tier == CRITICAL


def test_runway_is_measured_in_time_not_fraction():
    subject = a_tier_monitor()
    for fraction in (1.0, 0.9, 0.8, 0.7):
        subject.observe(QUOTA, fraction)
    outcome = subject.measure(seconds_per_reading=60.0)
    assert outcome.tier.seconds_to_exhaustion is not None
    assert outcome.tier.seconds_to_exhaustion > 0


def test_the_monitor_averages_nothing():
    assert importlib.import_module(
        BLOCK_PARTS["survival-tier-monitor"]
    ).describe_survival(a_tier_monitor())["averages_its_resources"] is False


# ---- conservation-planner ---------------------------------------------------

def a_conservation_planner():
    planner = ConservationPlanner(
        protected_parts=("risk-gate", "exposure-view", "human-override-reader"),
        now_ns=Clock(),
    )
    planner.declare_part("web-researcher", DISCRETIONARY_RESEARCH)
    planner.declare_part("model-trainer", "model-training")
    planner.declare_part("narrative-writer", "narrative-and-explanation")
    planner.declare_part("scanner", "opportunity-scanning")
    planner.declare_part("entry-placer", NEW_POSITION_ENTRY)
    planner.declare_part("position-watcher", POSITION_MANAGEMENT)
    for part_id, value, cost in (
        ("web-researcher", 0.1, 100.0), ("model-trainer", 1.0, 50.0),
        ("narrative-writer", 0.5, 20.0), ("scanner", 5.0, 10.0),
        ("entry-placer", 10.0, 5.0), ("position-watcher", 20.0, 5.0),
    ):
        planner.observe_value(part_id, value, cost)
    return planner


def test_nothing_that_protects_capital_can_be_stopped():
    subject = a_conservation_planner()
    subject.declare_part("risk-gate", DISCRETIONARY_RESEARCH)
    outcome = subject.plan(SHUTDOWN)
    assert outcome.state == REFUSED_PROTECTS_CAPITAL
    assert "risk-gate" in outcome.refused_parts


def test_discretionary_work_is_cut_first():
    subject = a_conservation_planner()
    outcome = subject.plan(FRUGAL)
    assert outcome.state == CONSERVATION_PLANNED
    assert "web-researcher" in outcome.plan.parts_to_stop
    assert "position-watcher" not in outcome.plan.parts_to_stop


def test_trading_stops_before_the_machinery_that_watches_trades():
    subject = a_conservation_planner()
    assert CUT_ORDER.index(NEW_POSITION_ENTRY) < CUT_ORDER.index(POSITION_MANAGEMENT)
    outcome = subject.plan(SHUTDOWN)
    assert "entry-placer" in outcome.plan.parts_to_stop


def test_deeper_cuts_never_resurrect_a_stopped_part():
    subject = a_conservation_planner()
    assert subject.plans_are_nested()


def test_a_comfortable_tier_cuts_nothing():
    assert a_conservation_planner().plan(COMFORTABLE).state == NOTHING_TO_CUT


def test_a_planner_with_no_protected_parts_is_refused():
    with pytest.raises(ValueError):
        ConservationPlanner(protected_parts=())


# ---- folded-circuit-view ----------------------------------------------------

def a_view():
    return FoldedCircuitView(now_ns=Clock())


def test_an_unmeasured_part_is_never_rendered_healthy():
    subject = a_view()
    subject.declare_part("p-1", "block-a", produces=("d-1",))
    outcome = subject.fold()
    assert outcome.state == FOLDED
    assert outcome.circuit_map.blocks["block-a"]["not_measured"] == 1
    assert outcome.circuit_map.parts_running == 0


def test_a_block_with_nothing_running_is_reported_dark():
    subject = a_view()
    for index in range(3):
        subject.declare_part(f"p-{index}", "block-a")
        subject.observe_state(f"p-{index}", NEVER_STARTED)
    outcome = subject.fold()
    assert outcome.circuit_map.blocks_entirely_dark == ("block-a",)
    assert "a capability that does not exist" in outcome.reason


def test_never_started_and_faulted_are_different_states():
    subject = a_view()
    subject.declare_part("p-1", "block-a")
    subject.declare_part("p-2", "block-a")
    subject.observe_state("p-1", NEVER_STARTED)
    subject.observe_state("p-2", "faulted")
    blocks = subject.fold().circuit_map.blocks["block-a"]
    assert blocks["never_started"] == 1
    assert blocks["faulted"] == 1


def test_edges_between_blocks_are_counted_not_drawn():
    subject = a_view()
    subject.declare_part("writer", "block-a", produces=("d-1", "d-2"))
    subject.declare_part("reader", "block-b", consumes=("d-1", "d-2"))
    edges = subject.fold().edges_between_blocks
    assert edges[("block-a", "block-b")] == 2


def test_a_state_it_cannot_render_is_refused():
    subject = a_view()
    subject.declare_part("p-1", "block-a")
    with pytest.raises(ValueError):
        subject.observe_state("p-1", "probably-fine")


# ---- autonomy-boundary ------------------------------------------------------

COMPETENCE_BARS = {
    OBSERVE_ONLY: 0.0, PROPOSE_ONLY: 0.3, ACT_WITHIN_LIMITS: 0.6, MODIFY_ITSELF: 0.9,
}
NOTIONAL_CEILINGS = {
    OBSERVE_ONLY: 0.0, PROPOSE_ONLY: 0.0, ACT_WITHIN_LIMITS: 10_000.0,
    MODIFY_ITSELF: 50_000.0,
}


def a_boundary(readings=2):
    boundary = AutonomyBoundary(
        competence_for_level=COMPETENCE_BARS, clean_modifications_required=1,
        readings_before_widening=readings,
        maximum_notional_for_level=NOTIONAL_CEILINGS, now_ns=Clock(),
    )
    boundary.observe_competence(1.0)
    boundary.observe_survival_tier(COMFORTABLE)
    boundary.observe_modification(broke_something=False)
    boundary.observe_unresolved_faults(0)
    boundary.observe_unreachable_exposure(False)
    boundary.observe_override(False)
    return boundary


def test_widening_takes_sustained_evidence():
    subject = a_boundary(readings=3)
    assert subject.issue().state == HELD
    assert subject.issue().state == HELD
    assert subject.issue().state == WIDENED


def test_narrowing_needs_one_reason():
    subject = a_boundary(readings=2)
    for _ in range(6):
        subject.issue()
    subject.observe_unresolved_faults(1)
    outcome = subject.issue()
    assert outcome.state in (NARROWED, HELD)
    assert A_FAULT_IS_UNRESOLVED in outcome.narrowing_reasons


def test_a_human_override_closes_the_envelope():
    subject = a_boundary(readings=2)
    for _ in range(6):
        subject.issue()
    subject.observe_override(True)
    outcome = subject.issue()
    assert A_HUMAN_SAID_SO in outcome.narrowing_reasons
    assert outcome.envelope.level == OBSERVE_ONLY


def test_falling_competence_narrows_the_envelope():
    subject = a_boundary(readings=2)
    for _ in range(6):
        subject.issue()
    subject.observe_competence(0.1)
    outcome = subject.issue()
    assert COMPETENCE_FELL in outcome.narrowing_reasons


def a_boundary_with_nothing_demonstrated():
    """What every start looks like: no closed trade, so no competence."""
    boundary = AutonomyBoundary(
        competence_for_level=COMPETENCE_BARS, clean_modifications_required=1,
        readings_before_widening=2,
        maximum_notional_for_level=NOTIONAL_CEILINGS, now_ns=Clock(),
    )
    boundary.observe_survival_tier(COMFORTABLE)
    return boundary


def test_on_paper_the_envelope_may_trade_before_it_has_demonstrated_anything():
    """Competence is measured from closed trades and closed trades need trading.
    Measured 2026-08-26: 49,908 envelopes issued, 0 widenings, 444,545 zero risk
    limits and 33 order intents refused -- a loop with no entry point."""
    subject = a_boundary_with_nothing_demonstrated()
    subject.observe_money_mode("paper")
    envelope = subject.issue().envelope
    assert envelope.level == ACT_WITHIN_LIMITS
    assert envelope.may_trade is True
    assert subject.earned_level == OBSERVE_ONLY


def test_the_paper_floor_grants_trading_and_nothing_else():
    subject = a_boundary_with_nothing_demonstrated()
    subject.observe_money_mode("paper")
    envelope = subject.issue().envelope
    assert envelope.may_change_settings is False
    assert envelope.may_admit_parts is False


def test_live_money_has_no_floor():
    subject = a_boundary_with_nothing_demonstrated()
    subject.observe_money_mode("live")
    envelope = subject.issue().envelope
    assert envelope.level == OBSERVE_ONLY and envelope.may_trade is False


def test_a_money_mode_that_has_not_been_read_is_not_paper():
    """Missing is not empty: a floor granted because the mode could not be read
    would treat "we do not know" as "no money is at risk"."""
    subject = a_boundary_with_nothing_demonstrated()
    envelope = subject.issue().envelope
    assert envelope.level == OBSERVE_ONLY and envelope.may_trade is False


def test_a_human_override_goes_through_the_paper_floor():
    subject = a_boundary_with_nothing_demonstrated()
    subject.observe_money_mode("paper")
    subject.issue()
    subject.observe_override(True)
    envelope = subject.issue().envelope
    assert envelope.level == OBSERVE_ONLY and envelope.may_trade is False


def test_the_envelope_is_not_a_binary_switch():
    described = importlib.import_module(
        BLOCK_PARTS["autonomy-boundary"]
    ).describe_boundary(a_boundary())
    assert described["is_a_binary_switch"] is False
    assert len(described["levels"]) == 4


# ---- autonomy-policy-engine -------------------------------------------------

class Envelope:
    def __init__(self, level=ACT_WITHIN_LIMITS, maximum_notional=10_000.0):
        self.level = level
        self.maximum_notional = maximum_notional


def a_policy_engine(earned=1000.0, minimum=5):
    engine = AutonomyPolicyEngine(
        earned_size_per_competence=earned, minimum_observations=minimum, now_ns=Clock(),
    )
    engine.observe_envelope(Envelope())
    return engine


def test_competence_is_per_subject():
    """Competence at BTCUSDT says nothing about a three-day-old listing."""
    subject = a_policy_engine()
    subject.observe_competence("BTCUSDT", 0.9, 100)
    outcome = subject.decide("NEWUSDT", OPEN_A_POSITION, size=100.0)
    assert outcome.state == SUBJECT_IS_UNMEASURED


def test_size_is_checked_against_what_the_subject_has_earned():
    subject = a_policy_engine(earned=1000.0)
    subject.observe_competence("BTCUSDT", 0.5, 100)
    # 0.5 competence earns 500, so 400 is inside it and 2,000 is not.
    outcome = subject.decide("BTCUSDT", OPEN_A_POSITION, size=400.0)
    assert outcome.state == ALLOWED
    bigger = subject.decide("BTCUSDT", OPEN_A_POSITION, size=2_000.0)
    assert bigger.state == ABOVE_WHAT_THIS_SUBJECT_HAS_EARNED


def test_the_envelope_ceiling_is_a_hard_limit():
    subject = a_policy_engine(earned=1_000_000.0)
    subject.observe_competence("BTCUSDT", 1.0, 100)
    outcome = subject.decide("BTCUSDT", OPEN_A_POSITION, size=50_000.0)
    assert outcome.state == ABOVE_THE_ENVELOPE_CEILING


def test_an_action_needing_a_wider_level_is_refused():
    subject = a_policy_engine()
    assert subject.decide("part-x", ADMIT_A_PART).state == LEVEL_FORBIDS_IT


def test_closing_a_position_needs_less_than_opening_one():
    subject = a_policy_engine()
    subject.observe_envelope(Envelope(level=PROPOSE_ONLY))
    assert subject.decide("BTCUSDT", CLOSE_A_POSITION).state == ALLOWED
    assert subject.decide("BTCUSDT", OPEN_A_POSITION, 1.0).state == LEVEL_FORBIDS_IT


def test_an_unknown_action_is_refused_rather_than_allowed_by_omission():
    assert a_policy_engine().decide("BTCUSDT", "do-something-clever").state == UNKNOWN_ACTION


# ---- trading-halt-decider ---------------------------------------------------

def a_halt_decider(healthy=True):
    decider = TradingHaltDecider(now_ns=Clock())
    if healthy:
        decider.observe_override(False)
        decider.observe_envelope(may_trade=True)
        decider.observe_exposure_view(is_known=True)
        decider.observe_survival_tier(COMFORTABLE)
    return decider


def test_a_missing_input_halts():
    """Trading without knowing what is open is the state that ends accounts."""
    subject = a_halt_decider(healthy=False)
    subject.observe_override(False)
    subject.observe_envelope(may_trade=True)
    subject.observe_survival_tier(COMFORTABLE)
    outcome = subject.decide()
    assert outcome.state == HALTED
    assert EXPOSURE_UNKNOWN in outcome.causes


def test_a_halt_still_permits_closing():
    subject = a_halt_decider()
    subject.observe_outage("binance-usdm", is_dangerous=True)
    outcome = subject.decide()
    assert VENUE_UNREACHABLE_WITH_EXPOSURE in outcome.causes
    assert outcome.halt.may_close_positions
    assert outcome.blocks_closing is False


def test_a_human_override_blocks_closing_too():
    subject = a_halt_decider()
    subject.observe_override(True)
    outcome = subject.decide()
    assert HUMAN_OVERRIDE in outcome.causes
    assert outcome.blocks_closing


def test_a_symbol_level_cause_narrows_the_scope():
    subject = a_halt_decider()
    subject.observe_anomaly("TINYUSDT", True)
    outcome = subject.decide()
    assert outcome.halt.scope == "TINYUSDT"
    assert MARKET_ANOMALY in outcome.causes


def test_every_halt_names_what_would_clear_it():
    subject = a_halt_decider()
    subject.observe_regime_break("BTCUSDT", True)
    assert subject.decide().halt.cleared_by


def test_a_healthy_system_trades():
    assert a_halt_decider().decide().state == TRADING


def test_halting_needs_no_consensus():
    assert importlib.import_module(
        BLOCK_PARTS["trading-halt-decider"]
    ).describe_halting(a_halt_decider())["requires_consensus_to_halt"] is False


# ---- capability-gap-finder --------------------------------------------------

def a_gap_finder():
    finder = CapabilityGapFinder(minimum_evidence=1, now_ns=Clock())
    finder.declare_recorded_data({"kline", "aggtrade"})
    return finder


def test_a_dark_block_is_the_strongest_kind_of_gap():
    outcome = a_gap_finder().from_a_dark_block("options", parts_in_block=12)
    assert outcome.state == GAP_FOUND
    assert outcome.gap.is_actionable


def test_a_bad_result_without_a_condition_names_no_capability():
    outcome = a_gap_finder().from_a_losing_condition("bear-bot", "", 100, 0.3)
    assert outcome.state == NOT_NAMEABLE
    assert "names no capability" in outcome.reason


def test_an_unreachable_gap_is_recorded_with_its_blocker():
    subject = a_gap_finder()
    subject.declare_blocker("the options block has nothing running", NEEDS_A_VENUE_ACCOUNT)
    outcome = subject.from_a_dark_block("options", 12)
    assert outcome.state == NOT_REACHABLE
    assert outcome.gap.blocked_by == NEEDS_A_VENUE_ACCOUNT
    assert "already written down" in outcome.reason


def test_a_finding_needing_unrecorded_data_is_a_data_gap():
    outcome = a_gap_finder().from_an_untestable_finding("f-1", ("order-book-l3",))
    assert outcome.state == GAP_FOUND
    assert "order-book-l3" in outcome.gap.description


def test_a_gap_is_recorded_once():
    subject = a_gap_finder()
    subject.from_a_dark_block("options", 12)
    assert subject.from_a_dark_block("options", 12).state == GAP_ALREADY_KNOWN


# ---- part-author ------------------------------------------------------------

class Gap:
    def __init__(self, gap_id="gap-1", new_part=True):
        self.gap_id = gap_id
        self.description = "nothing records order-book-l3"
        self.evidence = ("f-1 cannot be tested without it",)
        self.is_actionable = True
        self.would_be_a_new_part = new_part


def an_author():
    author = PartAuthor(now_ns=Clock())
    author.observe_gap(Gap())
    author.observe_existing_part("existing-part", produces=("d-1",))
    return author


def _propose(author, **overrides):
    job = {
        "gap_id": "gap-1", "part_id": "new-part", "consumes": ("d-1",),
        "produces": ("d-2", "part-health"), "resource_class": "compute-bound",
        "rate_risk": "latency-only", "skipped_tick_effect": "delays",
        "source": "def run(): ...", "tests": "def test_it(): ...",
    }
    job.update(overrides)
    return author.propose(**job)


def test_a_part_with_no_gap_behind_it_is_refused():
    subject = an_author()
    assert _propose(subject, gap_id="gap-nobody-recorded").state == NO_GAP


def test_a_part_producing_nothing_is_refused():
    assert _propose(an_author(), produces=()).state == PRODUCES_NOTHING


def test_a_part_naming_another_part_breaks_t4():
    outcome = _propose(an_author(), consumes=("existing-part",))
    assert outcome.state in (NAMES_ANOTHER_PART, UNDECLARED_DATA)
    assert NAMES_ANOTHER_PART in outcome.violations


def test_a_part_consuming_a_data_type_nothing_produces_is_refused():
    outcome = _propose(an_author(), consumes=("d-nobody-writes",))
    assert outcome.state == UNDECLARED_DATA
    assert "look healthy forever" in outcome.reason


def test_a_part_without_tests_could_never_be_admitted():
    assert _propose(an_author(), tests="   ").state == NO_TESTS


def test_a_valid_proposal_is_a_proposal_not_a_part():
    outcome = _propose(an_author())
    assert outcome.state == PROPOSED
    assert "goes through exactly the gates a human-written part would" in outcome.reason


def test_the_author_admits_nothing_itself():
    assert importlib.import_module(
        BLOCK_PARTS["part-author"]
    ).describe_authoring(an_author())["admits_its_own_parts"] is False


# ---- part-admission-gate ----------------------------------------------------

class Proposal:
    def __init__(self, part_id="new-part", consumes=("d-1",), produces=("d-2",)):
        self.proposal_id = f"proposal-{part_id}"
        self.part_id = part_id
        self.consumes = consumes
        self.produces = produces


def an_admission_gate(level=MODIFY_ITSELF, contract=(True, ""), tests=(5, 0, True)):
    gate = PartAdmissionGate(minimum_tests=1, now_ns=Clock())
    gate.observe_envelope(level)
    gate.observe_override(False)
    gate.install_contract_checker(lambda proposal: contract)
    gate.install_test_runner(lambda proposal: tests)
    return gate


def test_a_proposal_passing_everything_is_admitted():
    outcome = an_admission_gate().admit(Proposal())
    assert outcome.state == ADMITTED
    assert len(outcome.admitted.checks_passed) >= 5


def test_a_broken_contract_stops_admission():
    gate = an_admission_gate(contract=(False, "d-2 has no consumer"))
    assert gate.admit(Proposal()).state == CONTRACT_BROKEN


def test_failing_tests_stop_admission():
    gate = an_admission_gate(tests=(3, 2, True))
    assert gate.admit(Proposal()).state == TESTS_FAILED


def test_tests_on_invented_fixtures_stop_admission():
    gate = an_admission_gate(tests=(5, 0, False))
    outcome = gate.admit(Proposal())
    assert outcome.state == NOT_ON_REAL_DATA
    assert "the author's idea of the market" in outcome.reason


def test_a_duplicate_part_is_an_ambiguity_not_redundancy():
    gate = an_admission_gate()
    gate.observe_existing_part("old-part", consumes=("d-1",), produces=("d-2",))
    outcome = gate.admit(Proposal())
    assert outcome.state == DUPLICATES_AN_EXISTING_PART


def test_admission_requires_the_widest_envelope():
    gate = an_admission_gate(level=ACT_WITHIN_LIMITS)
    assert gate.admit(Proposal()).state == ENVELOPE_FORBIDS_IT


def test_a_human_override_blocks_admission_outright():
    gate = an_admission_gate()
    gate.observe_override(True)
    assert gate.admit(Proposal()).state == A_HUMAN_SAID_NO


def test_no_check_is_traded_against_another():
    assert importlib.import_module(
        BLOCK_PARTS["part-admission-gate"]
    ).describe_admission(an_admission_gate())[
        "trades_one_check_against_another"
    ] is False


# ---- self-modification-journal ----------------------------------------------

def a_journal():
    journal = SelfModificationJournal(now_ns=Clock())
    journal.observe_authorisation("policy-1", MODIFY_ITSELF)
    return journal


def test_a_record_is_written_before_the_change_takes_effect():
    subject = a_journal()
    outcome = subject.record("p-1", "swap", before="v1", after="v2", authorised_by="policy-1")
    assert outcome.state == RECORDED
    assert outcome.record.took_effect_at_ns is None
    assert "leaves no trace of itself" in outcome.reason


def test_a_change_nobody_authorised_is_refused():
    subject = SelfModificationJournal(now_ns=Clock())
    assert subject.record("p-1", "swap", "v1", "v2", "nobody").state == NOT_AUTHORISED


def test_applying_without_a_record_is_reported():
    subject = a_journal()
    assert subject.mark_applied("modification-999").state == NOT_RECORDED_FIRST


def test_the_journal_is_append_only():
    subject = a_journal()
    subject.record("p-1", "swap", "v1", "v2", "policy-1")
    subject.mark_applied("modification-1")
    assert len(subject.entries()) == 2
    assert not hasattr(subject, "edit")
    described = importlib.import_module(
        BLOCK_PARTS["self-modification-journal"]
    ).describe_journal(subject)
    assert described["can_edit_an_entry"] is False
    assert described["can_delete_an_entry"] is False


def test_a_rollback_target_needs_a_recorded_before_state():
    subject = a_journal()
    subject.record("p-1", "swap", None, "v2", "policy-1")
    subject.mark_applied("modification-1")
    outcome = subject.rollback_target("p-1")
    assert outcome.state.startswith("no-before-state")


def test_a_reversible_change_offers_its_before_state():
    subject = a_journal()
    subject.record("p-1", "swap", "v1", "v2", "policy-1")
    subject.mark_applied("modification-1")
    outcome = subject.rollback_target("p-1")
    assert outcome.record.before == "v1"


# ---- part-replacement-planner -----------------------------------------------

class Admitted:
    def __init__(self, part_id="p-1", proposal_id="proposal-p-1"):
        self.part_id = part_id
        self.proposal_id = proposal_id


def a_replacement_planner(skipped="delays", holds=False, handover=True):
    planner = PartReplacementPlanner(now_ns=Clock())
    planner.declare_part(
        "p-1", skipped_tick_effect=skipped, holds_capital_state=holds,
        can_hand_over_state=handover, current_source="v1",
    )
    planner.observe_admitted(Admitted())
    return planner


def test_a_corrupting_part_is_replaced_without_a_gap():
    subject = a_replacement_planner(skipped="corrupts")
    outcome = subject.plan(a_fault("p-1"))
    assert outcome.state == REPLACEMENT_PLANNED
    assert outcome.order == START_THEN_STOP
    assert "only correct order" in outcome.plan.reason


def test_a_merely_delaying_part_is_swapped_in_place():
    subject = a_replacement_planner(skipped="delays")
    assert subject.plan(a_fault("p-1")).order == STOP_THEN_START


def test_a_part_whose_state_cannot_be_handed_over_needs_a_flat_book():
    subject = a_replacement_planner(holds=True, handover=False)
    outcome = subject.plan(a_fault("p-1"))
    assert outcome.plan.requires_a_pause
    assert any("flat" in step for step in outcome.plan.steps)


def test_every_plan_writes_its_rollback_first():
    subject = a_replacement_planner()
    outcome = subject.plan(a_fault("p-1"))
    assert outcome.plan.is_reversible
    assert "rollback" in outcome.plan.steps[0]


def test_a_fault_with_no_replacement_is_not_a_plan():
    subject = PartReplacementPlanner(now_ns=Clock())
    subject.declare_part("p-1", "delays", False, True, "v1")
    assert subject.plan(a_fault("p-1")).state == NO_REPLACEMENT


# ---- upstream-improvement-watch ---------------------------------------------

def an_upstream_watch():
    watch = UpstreamImprovementWatch(now_ns=Clock())
    watch.declare_dependency("kline-reader", "binance-kline-schema")
    watch.declare_dependency("kline-window-builder", "binance-kline-schema")
    return watch


def test_a_change_is_routed_to_the_parts_that_declare_the_dependency():
    subject = an_upstream_watch()
    outcome = subject.notice(
        "binance-kline-schema", BREAKING, "the volume field was renamed", "notice://1"
    )
    assert outcome.state == NOTICED
    assert set(outcome.change.affects_parts) == {"kline-reader", "kline-window-builder"}


def test_a_silent_behaviour_change_is_the_dangerous_one():
    subject = an_upstream_watch()
    outcome = subject.notice(
        "binance-kline-schema", SILENT_BEHAVIOUR_CHANGE, "rounding changed", "notice://2"
    )
    assert outcome.change.is_breaking
    assert "attributed to the market for weeks" in outcome.reason


def test_improvements_are_watched_for_too():
    subject = an_upstream_watch()
    outcome = subject.notice(
        "binance-kline-schema", IMPROVEMENT, "a batch endpoint exists now", "notice://3"
    )
    assert outcome.state == NOTICED
    assert subject.standing.improvements_noticed == 1
    assert importlib.import_module(
        BLOCK_PARTS["upstream-improvement-watch"]
    ).describe_upstream_watch(subject)["watches_only_for_breakage"] is False


def test_a_notice_nobody_depends_on_is_not_routed():
    subject = an_upstream_watch()
    outcome = subject.notice("something-else", BREAKING, "changed", "notice://4")
    assert outcome.state == AFFECTS_NOTHING


def test_the_same_notice_is_recorded_once():
    subject = an_upstream_watch()
    subject.notice("binance-kline-schema", BREAKING, "renamed", "notice://1")
    outcome = subject.notice("binance-kline-schema", BREAKING, "renamed", "notice://1")
    assert outcome.state.startswith("already")


# ---- an unmeasured runway is not an emergency (2026-08-25) -------------------

def test_an_unmeasured_survival_tier_does_not_halt_trading():
    """SHUTDOWN because nothing was measured is not SHUTDOWN because it ran out.

    survival-tier-monitor reports the most restrictive tier when nothing has been
    measured, which is right: assuming runway nobody counted is how a system finds
    out it is out of money by stopping. But the runway it measures is a **provider
    budget**, and this box has no provider configured at all -- so reading that as
    NO_RUNWAY would have halted paper trading permanently the moment the
    autonomous block started, which is RL-005 inverted.
    """
    from runtime.autonomy_types import SHUTDOWN

    decider = TradingHaltDecider(now_ns=Clock())
    decider.observe_exposure_view(is_known=True)
    decider.observe_survival_tier(SHUTDOWN, is_measured=False)
    decision = decider.decide()
    assert not decision.halt.is_halted
    assert decider.standing.times_an_unmeasured_tier_was_not_a_halt == 1


def test_a_measured_shutdown_tier_still_halts_trading():
    from runtime.autonomy_types import SHUTDOWN

    decider = TradingHaltDecider(now_ns=Clock())
    decider.observe_exposure_view(is_known=True)
    decider.observe_survival_tier(SHUTDOWN, is_measured=True)
    decision = decider.decide()
    assert decision.halt.is_halted
    assert "not-enough-resource" in " ".join(decision.halt.causes)
