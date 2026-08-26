"""The resource governor: fourteen parts, and the refusals that matter most."""

import dataclasses
import importlib

import pytest

from parts.resource_governor.accelerator_scheduler import (
    GRANTED, NO_ACCELERATOR, QUEUED, AcceleratorScheduler,
)
from parts.resource_governor.duty_cycle_planner import DutyCyclePlanner
from parts.resource_governor.gate_actuator import FAILED, FLIPPED, GateActuator
from parts.resource_governor.hardware_scanner import HardwareScanner
from parts.resource_governor.hog_detector import CPU, MEMORY, HogDetector
from parts.resource_governor.io_pressure_meter import IoPressureMeter
from parts.resource_governor.memory_pressure_forecaster import MemoryPressureForecaster
from parts.resource_governor.off_state_verifier import (
    STILL_HOLDING, UNVERIFIABLE, OffStateVerifier,
)
from parts.resource_governor.part_appetite_meter import PartAppetiteMeter, PartResourceUsage
from parts.resource_governor.part_priority_reader import UNSTATED_PRIORITY, PartPriorityReader
from parts.resource_governor.part_restart_budgeter import (
    EXHAUSTED, GRANTED as RESTART_GRANTED, PartRestartBudgeter,
)
from parts.resource_governor.resource_reservation_ledger import (
    HONOURED, OVERCOMMITTED, ResourceReservationLedger,
)
from parts.resource_governor.switch_oscillation_damper import SwitchOscillationDamper
from parts.resource_governor.switching_planner import (
    TURN_OFF, TURN_ON, GovernorInputs, SwitchingPlanner,
)
from runtime.hardware_facts import measure_hardware_facts
from runtime.part_declaration import load_declaration_from_blueprint

BLOCK_PARTS = {
    "hardware-scanner": "parts.resource_governor.hardware_scanner",
    "part-appetite-meter": "parts.resource_governor.part_appetite_meter",
    "hog-detector": "parts.resource_governor.hog_detector",
    "part-priority-reader": "parts.resource_governor.part_priority_reader",
    "switching-planner": "parts.resource_governor.switching_planner",
    "gate-actuator": "parts.resource_governor.gate_actuator",
    "off-state-verifier": "parts.resource_governor.off_state_verifier",
    "io-pressure-meter": "parts.resource_governor.io_pressure_meter",
    "accelerator-scheduler": "parts.resource_governor.accelerator_scheduler",
    "memory-pressure-forecaster": "parts.resource_governor.memory_pressure_forecaster",
    "part-restart-budgeter": "parts.resource_governor.part_restart_budgeter",
    "duty-cycle-planner": "parts.resource_governor.duty_cycle_planner",
    "switch-oscillation-damper": "parts.resource_governor.switch_oscillation_damper",
    "resource-reservation-ledger": "parts.resource_governor.resource_reservation_ledger",
}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def usage(part_id, cpu=None, memory=None, measured=True):
    return PartResourceUsage(
        part_id=part_id,
        cpu_seconds_per_second=cpu,
        memory_current_bytes=memory,
        memory_peak_bytes=memory,
        measured_at_ns=1,
        unreadable_reason=None if measured else "cgroup unreadable",
    )


@pytest.fixture
def capacity():
    return HardwareScanner().scan()


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


# ---- hardware-scanner --------------------------------------------------------

def test_this_machine_scans_complete(capacity):
    assert capacity.is_complete
    assert capacity.facts.logical_cpus and capacity.facts.total_ram_bytes


def test_an_unmeasurable_fact_is_named_not_defaulted():
    facts = measure_hardware_facts()
    scanner = HardwareScanner(measure=lambda: dataclasses.replace(facts, numa_nodes=None))
    reading = scanner.scan()
    assert not reading.is_complete
    assert reading.unmeasurable == ("numa_nodes",)


# ---- part-appetite-meter -----------------------------------------------------

def test_this_process_own_cgroup_is_measurable(tmp_path):
    """Read from a real cgroup layout; the first sample has no rate to report."""
    (tmp_path / "cpu.stat").write_text("usage_usec 1000000\nuser_usec 500000\n")
    (tmp_path / "memory.current").write_text("52428800\n")
    (tmp_path / "memory.peak").write_text("62914560\n")
    clock = Clock()
    meter = PartAppetiteMeter(monotonic=clock.monotonic)
    first = meter.measure("a-part", tmp_path)
    assert first.is_measured
    assert first.cpu_seconds_per_second is None
    assert first.memory_current_bytes == 52428800

    clock.now += 2.0
    (tmp_path / "cpu.stat").write_text("usage_usec 2000000\nuser_usec 900000\n")
    second = meter.measure("a-part", tmp_path)
    assert second.cpu_seconds_per_second == pytest.approx(0.5)


def test_a_missing_cgroup_is_unreadable_not_zero(tmp_path):
    meter = PartAppetiteMeter()
    result = meter.measure("gone", tmp_path / "nothing-here")
    assert not result.is_measured
    assert result.memory_current_bytes is None
    assert meter.standing.parts_unreadable == 1


# ---- hog-detector ------------------------------------------------------------

def detector():
    return HogDetector(
        hog_multiple_of_fair_share=3.0,
        cpu_contention_fraction=0.8,
        memory_contention_fraction=0.8,
    )


def test_a_greedy_part_on_an_idle_machine_is_not_a_hog(capacity):
    """Using what nobody wanted is not taking anyone's share."""
    reports = detector().detect([usage("greedy", cpu=1.0), usage("small", cpu=0.01)], capacity)
    assert reports == ()


def test_a_greedy_part_under_contention_is_named(capacity):
    # Five parts, because fair share is per running part: with only two, no part
    # can exceed twice its share and a 3x threshold could never fire.
    cores = capacity.facts.logical_cpus
    usages = [usage("greedy", cpu=cores * 0.8)] + [
        usage(f"starved{index}", cpu=cores * 0.01) for index in range(4)
    ]
    reports = detector().detect(usages, capacity)
    assert [r.part_id for r in reports] == ["greedy"]
    assert reports[0].resource == CPU and reports[0].times_fair_share > 3.0


def test_memory_contention_is_detected_separately(capacity):
    total = capacity.facts.total_ram_bytes
    usages = [usage("fat", memory=int(total * 0.78))] + [
        usage(f"thin{index}", memory=int(total * 0.01)) for index in range(4)
    ]
    reports = detector().detect(usages, capacity)
    assert [(r.part_id, r.resource) for r in reports] == [("fat", MEMORY)]


def test_an_unmeasured_part_is_counted_not_judged(capacity):
    hogs = detector()
    hogs.detect([usage("unknown", measured=False)], capacity)
    assert hogs.standing.unmeasured_parts == 1


# ---- part-priority-reader ----------------------------------------------------

def test_an_unlisted_part_takes_the_middle_rank(tmp_path):
    path = tmp_path / "part-priority.toml"
    path.write_text('[priority]\n"venue-trade-stream-reader" = 1\n')
    reader = PartPriorityReader(priority_path=path)
    reader.read()
    assert reader.priority_of("venue-trade-stream-reader").priority == 1
    unlisted = reader.priority_of("some-other-part")
    assert unlisted.priority == UNSTATED_PRIORITY and unlisted.is_stated is False


def test_a_broken_priority_file_keeps_the_last_good_ranking(tmp_path):
    path = tmp_path / "part-priority.toml"
    path.write_text('[priority]\n"a" = 1\n')
    reader = PartPriorityReader(priority_path=path)
    assert reader.read() == {"a": 1}
    path.write_text("[priority\nbroken")
    assert reader.read() == {"a": 1}
    assert "TOMLDecodeError" in reader.standing.last_failure


def test_ranking_puts_the_lowest_number_first(tmp_path):
    path = tmp_path / "part-priority.toml"
    path.write_text('[priority]\n"a" = 9\n"b" = 1\n')
    reader = PartPriorityReader(priority_path=path)
    reader.read()
    assert [p.part_id for p in reader.rank(["a", "b"])] == ["b", "a"]


# ---- io-pressure-meter -------------------------------------------------------

def test_psi_is_read_when_the_kernel_publishes_it(tmp_path):
    pressure = tmp_path / "io"
    pressure.write_text("some avg10=12.50 avg60=1.00 total=1\nfull avg10=5.00 avg60=0.50 total=1\n")
    net = tmp_path / "dev"
    net.write_text("h\nh\n  eth0: 1000 0 0 0 0 0 0 0 2000 0\n")
    clock = Clock()
    meter = IoPressureMeter(pressure, net, monotonic=clock.monotonic)
    first = meter.measure()
    assert first.some_stalled_10s == pytest.approx(0.125)
    assert first.network_receive_bytes_per_second is None

    clock.now += 2.0
    net.write_text("h\nh\n  eth0: 3000 0 0 0 0 0 0 0 6000 0\n")
    second = meter.measure()
    assert second.network_receive_bytes_per_second == pytest.approx(1000.0)


def test_a_kernel_without_psi_is_unmeasured_not_idle(tmp_path):
    result = IoPressureMeter(tmp_path / "absent", tmp_path / "absent").measure()
    assert not result.is_measured
    assert result.some_stalled_10s is None


# ---- memory-pressure-forecaster ---------------------------------------------

def test_a_steady_grower_gives_a_time_to_exhaustion(capacity):
    clock = Clock()
    forecaster = MemoryPressureForecaster(window_samples=8, monotonic=clock.monotonic)
    for step in range(6):
        clock.now += 1.0
        forecaster.observe([usage("leaky", memory=1_000_000 + step * 1_000_000)])
    forecast = forecaster.forecast(capacity)
    assert forecast.growth_bytes_per_second == pytest.approx(1_000_000, rel=0.01)
    assert forecast.seconds_to_exhaustion == pytest.approx(
        capacity.facts.available_ram_bytes / 1_000_000, rel=0.01
    )
    assert forecast.fastest_growing_part == "leaky"


def test_too_few_samples_forecast_nothing(capacity):
    forecaster = MemoryPressureForecaster(window_samples=8)
    forecaster.observe([usage("new", memory=1000)])
    forecast = forecaster.forecast(capacity)
    assert forecast.seconds_to_exhaustion is None
    assert "not enough samples" in forecast.reason


def test_a_shrinking_part_does_not_forecast_exhaustion(capacity):
    clock = Clock()
    forecaster = MemoryPressureForecaster(window_samples=8, monotonic=clock.monotonic)
    for step in range(6):
        clock.now += 1.0
        forecaster.observe([usage("shrinking", memory=10_000_000 - step * 1_000_000)])
    assert forecaster.forecast(capacity).seconds_to_exhaustion is None


# ---- part-restart-budgeter ---------------------------------------------------

def test_restarts_are_granted_until_the_allowance_is_spent():
    clock = Clock()
    budgeter = PartRestartBudgeter(restarts_allowed=3, window_seconds=60.0, monotonic=clock.monotonic)
    verdicts = [budgeter.request_restart("crashy").verdict for _ in range(4)]
    assert verdicts == [RESTART_GRANTED] * 3 + [EXHAUSTED]
    assert budgeter.request_restart("crashy").seconds_until_allowance_returns > 0


def test_the_allowance_returns_as_the_window_slides():
    clock = Clock()
    budgeter = PartRestartBudgeter(restarts_allowed=1, window_seconds=60.0, monotonic=clock.monotonic)
    budgeter.request_restart("crashy")
    assert budgeter.request_restart("crashy").verdict == EXHAUSTED
    clock.now += 61
    assert budgeter.request_restart("crashy").verdict == RESTART_GRANTED


# ---- switch-oscillation-damper ----------------------------------------------

def test_repeated_flips_are_reported_as_flapping():
    clock = Clock()
    damper = SwitchOscillationDamper(
        transitions_before_flap=4, window_seconds=60.0, base_hold_seconds=30.0, monotonic=clock.monotonic
    )
    reports = []
    for state in ("on", "off", "on", "off"):
        clock.now += 1
        reports.append(damper.observe_switch("flappy", state))
    assert reports[:3] == [None, None, None]
    assert reports[3].transitions_in_window == 4
    assert reports[3].hold_for_seconds == 30.0
    assert reports[3].shortest_on_seconds == pytest.approx(1.0)


def test_the_same_state_twice_is_not_a_transition():
    damper = SwitchOscillationDamper(transitions_before_flap=2, window_seconds=60.0, base_hold_seconds=1.0)
    damper.observe_switch("steady", "on")
    assert damper.observe_switch("steady", "on") is None


def test_a_longer_flap_is_held_longer():
    clock = Clock()
    damper = SwitchOscillationDamper(
        transitions_before_flap=2, window_seconds=60.0, base_hold_seconds=10.0, monotonic=clock.monotonic
    )
    holds = []
    for state in ("on", "off", "on", "off"):
        clock.now += 1
        report = damper.observe_switch("flappy", state)
        if report:
            holds.append(report.hold_for_seconds)
    assert holds == sorted(holds) and holds[-1] > holds[0]


# ---- resource-reservation-ledger --------------------------------------------

def test_floors_are_honoured_until_the_machine_runs_out(capacity):
    ledger = ResourceReservationLedger()
    cores = capacity.facts.logical_cpus
    ledger.reserve("critical", cpu_cores=cores * 0.9, memory_bytes=1_000_000, priority=1)
    ledger.reserve("optional", cpu_cores=cores * 0.9, memory_bytes=1_000_000, priority=9)
    settled = {r.part_id: r for r in ledger.settle(capacity)}
    assert settled["critical"].state == HONOURED
    assert settled["optional"].state == OVERCOMMITTED
    assert "optional" in ledger.standing.refused


def test_an_overcommitted_floor_stays_visible(capacity):
    ledger = ResourceReservationLedger()
    ledger.reserve("huge", cpu_cores=10_000, memory_bytes=10**18, priority=1)
    settled = ledger.settle(capacity)
    assert len(settled) == 1 and settled[0].state == OVERCOMMITTED


def test_an_unmeasurable_machine_honours_nothing():
    facts = measure_hardware_facts()
    capacity = HardwareScanner(measure=lambda: dataclasses.replace(facts, logical_cpus=None)).scan()
    ledger = ResourceReservationLedger()
    ledger.reserve("any", 1.0, 1, 1)
    assert ledger.settle(capacity)[0].state == OVERCOMMITTED


# ---- duty-cycle-planner ------------------------------------------------------

def test_every_hour_is_allowed_until_there_is_evidence():
    planner = DutyCyclePlanner(quiet_hours_wanted=4, minimum_days_observed=2)
    cycle = planner.plan("retrainer")
    assert len(cycle.allowed_hours) == 24
    assert "until there is evidence" in cycle.reason


def test_the_quietest_hours_are_chosen_once_a_full_day_is_seen():
    planner = DutyCyclePlanner(quiet_hours_wanted=3, minimum_days_observed=2)
    for day in range(2):
        for hour in range(24):
            planner.observe_market_activity(hour, day, messages=100 if hour not in (2, 3, 4) else 1)
    cycle = planner.plan("retrainer")
    assert set(cycle.allowed_hours) == {2, 3, 4}
    assert cycle.quietest_hour in (2, 3, 4)


# ---- accelerator-scheduler ---------------------------------------------------

def test_a_machine_with_no_accelerator_says_so():
    scheduler = AcceleratorScheduler(slot_seconds=60.0, discover=lambda: ())
    slot = scheduler.request_slot("trainer", priority=1)
    assert slot.state == NO_ACCELERATOR and slot.device is None


def test_a_device_is_granted_then_queued_then_expires():
    clock = Clock()
    scheduler = AcceleratorScheduler(slot_seconds=60.0, discover=lambda: ("card0",), monotonic=clock.monotonic)
    assert scheduler.request_slot("trainer", 1).state == GRANTED
    assert scheduler.request_slot("other", 2).state == QUEUED
    clock.now += 61
    assert scheduler.request_slot("other", 2).state == GRANTED
    assert scheduler.standing.expired == 1


def test_this_box_reports_what_it_actually_has():
    """Whatever this machine has, the count is measured rather than assumed."""
    scheduler = AcceleratorScheduler(slot_seconds=1.0)
    assert scheduler.standing.devices_found == len(scheduler.standing.devices)


# ---- switching-planner -------------------------------------------------------

def planner(never_switched_off_priority_ceiling=10, plans_between_on_retries=2):
    return SwitchingPlanner(
        memory_exhaustion_warning_seconds=120.0,
        io_stall_fraction=0.5,
        never_switched_off_priority_ceiling=never_switched_off_priority_ceiling,
        plans_between_on_retries=plans_between_on_retries,
    )



def test_a_reservation_never_starts_a_part_the_planner_has_not_seen_run(capacity):
    """A reservation is a floor, not an ask. Reconciling it against a part the
    planner has never seen metered would switch "on" whichever reserved part is
    slowest to its first sweep at every spine boot -- symbol-catalogue-reader,
    fetching two venues' catalogues, on 2026-08-24."""
    from parts.resource_governor.resource_reservation_ledger import ResourceReservation

    subject = planner()
    reservation = ResourceReservation("slow-starter", 1.0, 1, 1, HONOURED, "floor held", 1)
    inputs = GovernorInputs(
        capacity=capacity, usages=(usage("hardware-scanner"),),
        running_parts=("hardware-scanner",), reservations=(reservation,),
    )
    for _ in range(4):
        assert subject.plan(inputs).decisions == ()

    # Once it has been seen running, its later absence is evidence of an off part.
    seen = GovernorInputs(
        capacity=capacity, usages=(usage("hardware-scanner"), usage("slow-starter")),
        running_parts=("hardware-scanner", "slow-starter"), reservations=(reservation,),
    )
    subject.plan(seen)
    subject.plan(inputs)
    vanished = subject.plan(inputs)
    assert [(d.part_id, d.action) for d in vanished.decisions] == [("slow-starter", TURN_ON)]


def test_a_part_the_metering_has_not_missed_yet_is_held_not_started(capacity):
    """Absent from one sweep may mean unmeasured, not off. The first plans after
    every spine start used to switch "on" whichever reserved parts the first
    half-drained sweep had not covered (2026-08-24, twice)."""
    subject = planner()
    inputs = GovernorInputs(capacity=capacity, usages=(usage("hardware-scanner"),), admitted_parts=("late",))
    first = subject.plan(inputs)
    assert first.decisions == () and first.held == ("late",)
    second = subject.plan(inputs)
    assert [(d.part_id, d.action) for d in second.decisions] == [("late", TURN_ON)]


def plan_after_a_full_sweep(subject, inputs):
    """Plan twice with the same inputs: a part absent from the metering is only
    planned on after a whole sweep has missed it (PLANS_ABSENT_BEFORE_START)."""
    subject.plan(inputs)
    return subject.plan(inputs)


def test_no_usage_readings_means_no_plan(capacity):
    """Missing is not empty: no part-resource-usage reading means the metering
    is absent, not that nothing is running."""
    plan = planner().plan(GovernorInputs(capacity=capacity, admitted_parts=("a",)))
    assert not plan.is_plannable
    assert plan.decisions == ()
    assert "part-resource-usage" in plan.unplannable_reason


def test_no_capacity_reading_means_no_plan(capacity):
    plan = planner().plan(GovernorInputs(capacity=None, running_parts=("a",)))
    assert not plan.is_plannable
    assert plan.decisions == ()
    assert "hardware-capacity" in plan.unplannable_reason


def test_an_incomplete_capacity_reading_also_refuses():
    facts = measure_hardware_facts()
    unmeasured = HardwareScanner(measure=lambda: dataclasses.replace(facts, logical_cpus=None)).scan()
    plan = planner().plan(GovernorInputs(capacity=unmeasured, running_parts=("a",)))
    assert not plan.is_plannable and "logical_cpus" in plan.unplannable_reason


def test_a_hog_under_contention_is_switched_off(capacity):
    from parts.resource_governor.hog_detector import HogReport

    report = HogReport("greedy", CPU, 8.0, 1.0, 8.0, True, 1)
    plan = planner().plan(
        GovernorInputs(capacity=capacity, running_parts=("greedy",), usages=(usage("greedy"),), hog_reports=(report,))
    )
    assert [(d.part_id, d.action) for d in plan.decisions] == [("greedy", TURN_OFF)]


def test_a_reserved_part_is_never_switched_off_for_being_a_hog(capacity):
    from parts.resource_governor.hog_detector import HogReport
    from parts.resource_governor.resource_reservation_ledger import ResourceReservation

    reservation = ResourceReservation("greedy", 1.0, 1, 1, HONOURED, "floor held", 1)
    plan = planner().plan(
        GovernorInputs(
            capacity=capacity,
            running_parts=("greedy",),
            hog_reports=(HogReport("greedy", CPU, 8.0, 1.0, 8.0, True, 1),),
            reservations=(reservation,),
        )
    )
    assert all(d.action != TURN_OFF for d in plan.decisions)


def test_a_flapping_part_is_held_rather_than_started(capacity):
    from parts.resource_governor.switch_oscillation_damper import FlapReport

    plan = planner().plan(
        GovernorInputs(
            capacity=capacity,
            usages=(usage("hardware-scanner"),),
            admitted_parts=("flappy",),
            flap_reports={"flappy": FlapReport("flappy", 4, 60.0, 1.0, 30.0, 1)},
        )
    )
    assert plan.held == ("flappy",)
    assert plan.decisions == ()


def test_a_part_out_of_restart_budget_is_held(capacity):
    from parts.resource_governor.part_restart_budgeter import RestartBudget

    budget = RestartBudget("crashy", EXHAUSTED, 5, 3, 40.0, "spent", 1)
    plan = planner().plan(
        GovernorInputs(capacity=capacity, usages=(usage("hardware-scanner"),), restart_requests=("crashy",), restart_budgets={"crashy": budget})
    )
    assert plan.held == ("crashy",)


def test_offs_are_ordered_before_ons(capacity):
    from parts.resource_governor.hog_detector import HogReport

    subject = planner()
    # One sweep with the newcomer absent, so the plan below is the one it is due
    # on -- and the machine turns contended on that same plan.
    subject.plan(
        GovernorInputs(
            capacity=capacity, running_parts=("greedy",), usages=(usage("greedy"),), admitted_parts=("newcomer",)
        )
    )
    plan = subject.plan(
        GovernorInputs(
            capacity=capacity,
            running_parts=("greedy",),
            usages=(usage("greedy"),),
            hog_reports=(HogReport("greedy", CPU, 8.0, 1.0, 8.0, True, 1),),
            admitted_parts=("newcomer",),
        )
    )
    assert [d.action for d in plan.decisions] == [TURN_OFF, TURN_ON]


def test_imminent_memory_exhaustion_switches_off_the_fastest_grower(capacity):
    from parts.resource_governor.memory_pressure_forecaster import MemoryForecast

    forecast = MemoryForecast(30.0, 1e9, 3 * 10**10, "leaky", 1e9, 8, "growing", 1)
    plan = planner().plan(
        GovernorInputs(capacity=capacity, running_parts=("leaky", "quiet"), usages=(usage("leaky"), usage("quiet")), memory_forecast=forecast)
    )
    assert [(d.part_id, d.action) for d in plan.decisions] == [("leaky", TURN_OFF)]


def hog_report(part_id, times_fair_share=8.0):
    from parts.resource_governor.hog_detector import HogReport

    return HogReport(part_id, CPU, times_fair_share, 1.0, times_fair_share, True, 1)


def test_a_part_shed_for_hogging_comes_back_when_the_contention_stops(capacity):
    """The defect this closes, measured on the live spine 2026-08-25 20:05 to
    21:08: 152 switch-records, every one an off and every one for hogging, and
    switched_on 0 across 30,150 plans. order-book-reader, venue-quote-stream-reader
    and tick-size-resolver were among the 42 left off, and the bot placed no order
    for the eight hours that followed."""
    subject = planner()
    contended = GovernorInputs(
        capacity=capacity, running_parts=("greedy", "quiet"),
        usages=(usage("greedy"), usage("quiet")), hog_reports=(hog_report("greedy"),),
    )
    assert [(d.part_id, d.action) for d in subject.plan(contended).decisions] == [("greedy", TURN_OFF)]

    # The machine is quiet: hog-detector publishes nothing at all while
    # uncontended, which is the same evidence the off was made on.
    quiet = GovernorInputs(capacity=capacity, running_parts=("quiet",), usages=(usage("quiet"),))
    back = plan_after_a_full_sweep(subject, quiet)
    assert [(d.part_id, d.action) for d in back.decisions] == [("greedy", TURN_ON)]
    assert [d.reason for d in back.decisions] == ["capacity-available"]

    # Restored is counted from the metering, not from the ask: a part is back
    # when the sweep sees it, and an on the actuator could not flip is not.
    assert subject.standing.restored == 0
    running_again = GovernorInputs(
        capacity=capacity, running_parts=("greedy", "quiet"), usages=(usage("greedy"), usage("quiet")),
    )
    subject.plan(running_again)
    assert subject.standing.restored == 1
    assert subject.standing.off_until_the_pressure_clears == 0


def test_a_part_still_stopping_is_not_a_part_that_came_back(capacity):
    """A part takes about two seconds to stop and the sweep it was measured in is
    a second old, so the metering either side of an off still carries it. Reading
    that as a return counted 112 restorations against 80 offs and zero ons on the
    live spine at 2026-08-26 04:35, and forgot each shed as it was made."""
    subject = planner()
    contended = GovernorInputs(
        capacity=capacity, running_parts=("greedy", "quiet"),
        usages=(usage("greedy"), usage("quiet")), hog_reports=(hog_report("greedy"),),
    )
    subject.plan(contended)

    # The next two sweeps still carry it, because it has not finished stopping.
    subject.plan(contended)
    subject.plan(contended)
    assert subject.standing.restored == 0
    assert subject.standing.off_until_the_pressure_clears == 1
    # And it is not shed twice for the same reason while it is on its way out.
    assert subject.standing.switched_off == 1

    quiet = GovernorInputs(capacity=capacity, running_parts=("quiet",), usages=(usage("quiet"),))
    back = plan_after_a_full_sweep(subject, quiet)
    assert [(d.part_id, d.action) for d in back.decisions] == [("greedy", TURN_ON)]


def test_an_on_the_actuator_could_not_flip_is_asked_again(capacity):
    """A part that never came back is still off, and forgetting it is how a
    silent hole opens: nothing else in the governor asks for a part the governor
    itself switched off. Asked again, but not on the next plan: gate-actuator
    recorded 17 refusals reading "PartAlreadyRunning" between 05:21 and 05:40 on
    2026-08-26, one for every part re-asked before it had been metered."""
    subject = planner(plans_between_on_retries=2)
    subject.plan(
        GovernorInputs(
            capacity=capacity, running_parts=("greedy",), usages=(usage("greedy"),),
            hog_reports=(hog_report("greedy"),),
        )
    )
    quiet = GovernorInputs(capacity=capacity, usages=(usage("quiet"),))
    assert [d.action for d in plan_after_a_full_sweep(subject, quiet).decisions] == [TURN_ON]

    # Nothing started: the part is still absent from every sweep, so the ask is
    # made again -- but only after the retry window has passed and a whole sweep
    # has then missed it, never on the next plan.
    assert subject.plan(quiet).decisions == ()
    assert subject.plan(quiet).decisions == ()
    assert [d.action for d in subject.plan(quiet).decisions] == [TURN_ON]


def test_a_part_stays_off_while_the_contention_that_shed_it_lasts(capacity):
    subject = planner()
    contended = GovernorInputs(
        capacity=capacity, running_parts=("greedy", "quiet"),
        usages=(usage("greedy"), usage("quiet")), hog_reports=(hog_report("greedy"),),
    )
    subject.plan(contended)

    # Still contended -- hog-detector is still naming someone, even though the
    # part that is off can no longer be named by it.
    still = GovernorInputs(
        capacity=capacity, running_parts=("quiet",), usages=(usage("quiet"),),
        hog_reports=(hog_report("quiet"),),
    )
    for _ in range(4):
        assert all(d.action != TURN_ON for d in subject.plan(still).decisions)
    assert subject.standing.off_until_the_pressure_clears == 2


def test_a_part_shed_outside_its_duty_cycle_comes_back_in_its_own_hours(capacity):
    from parts.resource_governor.duty_cycle_planner import DutyCycle

    subject = planner()
    duty = {"nightly": DutyCycle("nightly", (2, 3), 2, 14, 48, "quietest hours observed", 1)}
    asleep = GovernorInputs(
        capacity=capacity, running_parts=("nightly",), usages=(usage("nightly"),),
        duty_cycles=duty, current_hour=14,
    )
    assert [(d.part_id, d.action) for d in subject.plan(asleep).decisions] == [("nightly", TURN_OFF)]

    awake = GovernorInputs(capacity=capacity, usages=(usage("other"),), duty_cycles=duty, current_hour=2)
    plan = plan_after_a_full_sweep(subject, awake)
    assert [(d.part_id, d.action) for d in plan.decisions] == [("nightly", TURN_ON)]


def test_one_measurement_sheds_one_hog(capacity):
    """Fair share is an equal slice among the parts running, so at 327 parts every
    part doing real work is over three times it the moment the machine is
    contended. The reading that named one hog named sixteen on 2026-08-25 21:08,
    and gate-actuator flipped all sixteen off two seconds apart on that one
    measurement."""
    subject = planner()
    plan = subject.plan(
        GovernorInputs(
            capacity=capacity,
            running_parts=("worst", "middling", "least"),
            usages=(usage("worst"), usage("middling"), usage("least")),
            hog_reports=(hog_report("middling", 5.0), hog_report("worst", 9.0), hog_report("least", 3.1)),
        )
    )
    assert [(d.part_id, d.action) for d in plan.decisions] == [("worst", TURN_OFF)]
    assert subject.standing.hogs_deferred == 2


def test_the_control_path_is_never_switched_off(capacity):
    """A governor that sheds its own instruments plans the next question against
    the last levels they managed to publish. It shed memory-pressure-forecaster,
    duty-cycle-planner and part-restart-budgeter on 2026-08-25."""
    subject = planner(never_switched_off_priority_ceiling=27)
    plan = subject.plan(
        GovernorInputs(
            capacity=capacity,
            running_parts=("memory-pressure-forecaster", "ordinary"),
            usages=(usage("memory-pressure-forecaster"), usage("ordinary")),
            hog_reports=(hog_report("memory-pressure-forecaster", 40.0),),
            priorities={"memory-pressure-forecaster": 20},
        )
    )
    assert plan.decisions == ()


def test_an_ordinary_part_is_still_shed_while_the_control_path_is_not(capacity):
    subject = planner(never_switched_off_priority_ceiling=27)
    plan = subject.plan(
        GovernorInputs(
            capacity=capacity,
            running_parts=("gate-actuator", "ordinary"),
            usages=(usage("gate-actuator"), usage("ordinary")),
            hog_reports=(hog_report("gate-actuator", 40.0), hog_report("ordinary", 4.0)),
            priorities={"gate-actuator": 18},
        )
    )
    assert [(d.part_id, d.action) for d in plan.decisions] == [("ordinary", TURN_OFF)]


def test_a_restored_part_that_flaps_is_held_rather_than_started(capacity):
    """The return path is not allowed to become an oscillation: what shed a part
    can be true again the moment it is back, and switch-oscillation-damper is
    what says so."""
    from parts.resource_governor.switch_oscillation_damper import FlapReport

    subject = planner()
    subject.plan(
        GovernorInputs(
            capacity=capacity, running_parts=("greedy",), usages=(usage("greedy"),),
            hog_reports=(hog_report("greedy"),),
        )
    )
    quiet = GovernorInputs(
        capacity=capacity, usages=(usage("quiet"),),
        flap_reports={"greedy": FlapReport("greedy", 4, 60.0, 1.0, 30.0, 1)},
    )
    plan = plan_after_a_full_sweep(subject, quiet)
    assert plan.decisions == () and "greedy" in plan.held


# ---- gate-actuator -----------------------------------------------------------

def test_every_decision_is_flipped_and_recorded(capacity):
    flipped = []
    actuator = GateActuator(switch_part=lambda part_id, action: flipped.append((part_id, action)))
    plan = plan_after_a_full_sweep(
        planner(), GovernorInputs(capacity=capacity, usages=(usage("hardware-scanner"),), admitted_parts=("a", "b"))
    )
    records = actuator.apply(plan)
    assert len(records) == 2 and all(r.outcome == FLIPPED for r in records)
    assert flipped == [(r.part_id, r.action) for r in records]


def test_a_failed_flip_is_recorded_and_the_rest_still_run(capacity):
    def switch(part_id, action):
        if part_id == "broken":
            raise OSError("no such cgroup")

    actuator = GateActuator(switch_part=switch)
    plan = plan_after_a_full_sweep(
        planner(), GovernorInputs(capacity=capacity, usages=(usage("hardware-scanner"),), admitted_parts=("broken", "fine"))
    )
    records = {r.part_id: r for r in actuator.apply(plan)}
    assert records["broken"].outcome == FAILED
    assert records["fine"].outcome == FLIPPED
    assert actuator.standing.failed == 1


def test_an_unplannable_plan_flips_nothing():
    actuator = GateActuator(switch_part=lambda *_: pytest.fail("must not switch"))
    plan = planner().plan(GovernorInputs(capacity=None))
    assert actuator.apply(plan) == ()
    assert actuator.standing.plans_refused == 1


# ---- off-state-verifier ------------------------------------------------------

def switch_record(part_id, action="off", outcome="flipped"):
    from parts.resource_governor.gate_actuator import SwitchRecord

    return SwitchRecord(part_id, action, outcome, "reason", 1, 2)


def verifier(clock):
    return OffStateVerifier(
        grace_seconds=5.0,
        released_memory_bytes=1_000_000,
        released_cpu_seconds_per_second=0.01,
        monotonic=clock.monotonic,
    )


def test_a_part_that_let_go_is_verified_not_faulted():
    clock = Clock()
    check = verifier(clock)
    check.observe_switch_record(switch_record("gone"))
    clock.now += 6
    assert check.verify([usage("gone", cpu=0.0, memory=0)]) == ()
    assert check.standing.verified == 1


def test_a_part_still_holding_memory_is_faulted():
    """T-3: off means genuinely off, and this is the only check of it."""
    clock = Clock()
    check = verifier(clock)
    check.observe_switch_record(switch_record("clinging"))
    clock.now += 6
    faults = check.verify([usage("clinging", cpu=0.5, memory=500_000_000)])
    assert len(faults) == 1 and faults[0].kind == STILL_HOLDING
    # `stalled` is what makes the warden's needs_restarting true: the part was
    # switched off and did not let go, and something has to make it.
    assert faults[0].severity == "stalled"
    assert check.reading_for("clinging").memory_bytes_still_held == 500_000_000


def test_nothing_is_judged_before_the_grace_period():
    clock = Clock()
    check = verifier(clock)
    check.observe_switch_record(switch_record("dying"))
    clock.now += 1
    assert check.verify([usage("dying", cpu=1.0, memory=10**9)]) == ()
    assert check.standing.pending == 1


def test_a_part_gone_from_the_usage_report_counts_as_released():
    clock = Clock()
    check = verifier(clock)
    check.observe_switch_record(switch_record("vanished"))
    clock.now += 6
    assert check.verify([]) == ()
    assert check.standing.verified == 1


def test_an_unreadable_part_is_unverifiable_not_healthy():
    clock = Clock()
    check = verifier(clock)
    check.observe_switch_record(switch_record("opaque"))
    clock.now += 6
    faults = check.verify([usage("opaque", measured=False)])
    # The shared PartFault since 2026-08-25: this part published its own type on
    # part-fault, and unattended-run-warden -- which reads a fault's kind --
    # crashed on the first one. The reading it made is still its own, beside it.
    assert faults[0].kind == UNVERIFIABLE
    assert faults[0].severity == "degraded", "a reading nobody could take is not a restart"
    assert check.reading_for("opaque").verdict == UNVERIFIABLE
    assert check.standing.unverifiable == 1


def test_only_a_successful_off_is_watched():
    clock = Clock()
    check = verifier(clock)
    check.observe_switch_record(switch_record("on-part", action="on"))
    check.observe_switch_record(switch_record("failed-off", outcome="failed"))
    clock.now += 6
    assert check.verify([]) == ()
    assert check.standing.pending == 0
