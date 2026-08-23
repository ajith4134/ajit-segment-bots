"""Observability: the parts that make a wrong system visible rather than convincing.

Rule 8 is the thread through all of them. Each test below is a way a monitoring
system reassures when it should not: showing a dead part's last good news, alerting
so often nobody reads it, a tile with no provenance, a published board older than
its source. That last one is not hypothetical -- it happened in this project, and
`stale-board-watch` exists because of it.
"""

import importlib

import pytest

from parts.observability.ablation_harness import (
    ABLATING, INCONCLUSIVE, MATTERED, NO_MEASURABLE_EFFECT, PROTECTED, RECOVERING,
    AblationHarness,
)
from parts.observability.alert_raiser import CRITICAL, HIGH, LOW, AlertRaiser
from parts.observability.board_publisher import (
    FAILED, PUBLISHED, UNCHANGED, BoardPublisher,
)
from parts.observability.board_snapshot_builder import (
    FAILING, NOT_BUILT, NOT_MEASURED, OK, BoardSnapshotBuilder, TileWithoutProof,
)
from parts.observability.clock_skew_monitor import (
    DRIFTING, IN_STEP as CLOCK_IN_STEP, REJECTING, ClockSkewMonitor,
)
from parts.observability.drawdown_episode_tracker import (
    OPEN, RECOVERED, DrawdownEpisodeTracker,
)
from parts.observability.fund_conservation_auditor import (
    CONSERVED, DIVERGED, FundConservationAuditor,
)
from parts.observability.heartbeat_collector import (
    LATE, NEVER_REPORTED, REPORTING, SILENT, HeartbeatCollector,
)
from parts.observability.probe_runner import (
    FAILED as PROBE_FAILED, MEASURED, NOT_RUN, TIMED_OUT, ProbeRunner,
)
from parts.observability.stale_board_watch import (
    IN_STEP, LINK_STALE, NEVER_PUBLISHED, SOURCE_STOPPED, StaleBoardWatch,
)
from runtime.part_declaration import load_declaration_from_blueprint
from runtime.part_process import PartHealth
from runtime.trading_types import BUY, SELL, Fill

BLOCK_PARTS = {
    "ablation-harness": "parts.observability.ablation_harness",
    "heartbeat-collector": "parts.observability.heartbeat_collector",
    "probe-runner": "parts.observability.probe_runner",
    "alert-raiser": "parts.observability.alert_raiser",
    "board-snapshot-builder": "parts.observability.board_snapshot_builder",
    "board-publisher": "parts.observability.board_publisher",
    "stale-board-watch": "parts.observability.stale_board_watch",
    "drawdown-episode-tracker": "parts.observability.drawdown_episode_tracker",
    "fund-conservation-auditor": "parts.observability.fund_conservation_auditor",
    "clock-skew-monitor": "parts.observability.clock_skew_monitor",
}

VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def health(part_id, state="on"):
    return PartHealth(part_id, state, 1.0, 0.1, 1, None)


@pytest.mark.parametrize("part_id", sorted(BLOCK_PARTS))
def test_every_built_declaration_equals_the_blueprint(part_id):
    module = importlib.import_module(BLOCK_PARTS[part_id])
    assert module.PART_DECLARATION == load_declaration_from_blueprint(part_id)


# ---- heartbeat-collector -----------------------------------------------------

def collector(clock, late=5.0, silent=30.0):
    return HeartbeatCollector(
        late_after_seconds=late, silent_after_seconds=silent, monotonic=clock.monotonic
    )


def test_a_part_that_never_started_appears_in_the_table():
    """A report-driven table can never contain the part that never started."""
    subject = collector(Clock())
    subject.expect_part("the-sizer")
    table = subject.read_table()
    assert table.heartbeats[0].state == NEVER_REPORTED
    assert table.never_reported == 1


def test_a_dead_parts_last_good_news_is_never_shown_as_current():
    clock = Clock()
    subject = collector(clock, late=5.0, silent=30.0)
    subject.observe_health(health("the-sizer"))
    assert subject.heartbeat_of("the-sizer").state == REPORTING

    clock.now += 6
    assert subject.heartbeat_of("the-sizer").state == LATE

    clock.now += 30
    beat = subject.heartbeat_of("the-sizer")
    assert beat.state == SILENT
    assert beat.reported_state == "on", "the last report is kept, but not as current"
    assert "exactly why it must not be shown as current" in beat.reason


def test_a_part_reporting_again_stops_being_silent():
    clock = Clock()
    subject = collector(clock, late=5.0, silent=10.0)
    subject.observe_health(health("a"))
    clock.now += 11
    assert subject.heartbeat_of("a").state == SILENT
    subject.observe_health(health("a"))
    assert subject.heartbeat_of("a").state == REPORTING


def test_a_deliberately_stopped_part_is_no_longer_expected():
    subject = collector(Clock())
    subject.observe_health(health("a"))
    subject.forget_part("a")
    assert subject.read_table().heartbeats == ()


# ---- probe-runner ------------------------------------------------------------

def test_a_probe_keeps_the_command_that_produced_it():
    """A result nobody can re-run is one they must take on trust."""
    runner = ProbeRunner(timeout_seconds=1.0)
    runner.register("records", lambda: 42, command="wc -l tape/*.index")
    result = runner.run("records")
    assert result.outcome == MEASURED
    assert result.value == "42"
    assert result.command == "wc -l tape/*.index"


def test_a_probe_registered_without_a_command_is_refused():
    with pytest.raises(ValueError):
        ProbeRunner(timeout_seconds=1.0).register("x", lambda: 1, command="  ")


def test_a_failing_probe_is_a_result_not_a_gap():
    runner = ProbeRunner(timeout_seconds=1.0)

    def explode():
        raise OSError("the disk went away")

    runner.register("disk", explode, command="df -h")
    result = runner.run("disk")
    assert result.outcome == PROBE_FAILED
    assert "the disk went away" in result.failure
    assert result.command == "df -h"


def test_a_probe_past_its_deadline_is_recorded_as_slow():
    clock = Clock()
    runner = ProbeRunner(timeout_seconds=0.5, monotonic=clock.monotonic)

    def slow():
        clock.now += 2.0
        return "done"

    runner.register("slow", slow, command="sleep 2")
    result = runner.run("slow")
    assert result.outcome == TIMED_OUT
    assert result.value == "done", "the value arrived; the slowness is what is reported"


def test_an_unregistered_probe_is_not_run():
    assert ProbeRunner(timeout_seconds=1.0).run("nothing").outcome == NOT_RUN


# ---- alert-raiser ------------------------------------------------------------

def raiser(clock, cooldown=60.0):
    return AlertRaiser(cooldown_seconds=cooldown, monotonic=clock.monotonic)


def test_an_alert_carries_its_proof():
    alert = raiser(Clock()).raise_alert(
        source="part-fault", subject="the-sizer", message="still holding memory",
        proof="memory.current says 500 MB 6s after being switched off",
    )
    assert alert.proof
    assert alert.needs_immediate_attention is True


def test_a_recurring_condition_is_one_alert_with_a_count():
    """A part faulting every second must not be a list nobody will read."""
    clock = Clock()
    subject = raiser(clock, cooldown=60.0)
    subject.raise_alert("part-fault", "the-sizer", "faulted", "proof")
    for _ in range(10):
        assert subject.raise_alert("part-fault", "the-sizer", "faulted", "proof") is None
    active = subject.active_alerts()
    assert len(active) == 1
    assert active[0].occurrences == 11
    assert subject.standing.suppressed_duplicates == 10


def test_the_cooldown_expiring_lets_it_speak_again():
    clock = Clock()
    subject = raiser(clock, cooldown=60.0)
    subject.raise_alert("part-fault", "a", "m", "p")
    clock.now += 61
    assert subject.raise_alert("part-fault", "a", "m", "p") is not None


def test_severity_comes_from_the_consequence_not_the_source():
    subject = raiser(Clock())
    journal = subject.raise_alert("journal-gap", "seq 5", "m", "p")
    hog = subject.raise_alert("hog-report", "the-trainer", "m", "p")
    assert journal.severity == CRITICAL
    assert hog.severity == LOW


def test_the_worst_alerts_come_first():
    subject = raiser(Clock())
    subject.raise_alert("hog-report", "a", "m", "p")
    subject.raise_alert("journal-gap", "b", "m", "p")
    assert subject.active_alerts()[0].severity == CRITICAL


def test_a_resolved_condition_leaves_the_list():
    subject = raiser(Clock())
    subject.raise_alert("part-fault", "a", "m", "p")
    assert subject.resolve("part-fault", "a") is True
    assert subject.active_alerts() == ()


# ---- board-snapshot-builder --------------------------------------------------

def builder(clock, stale=60.0):
    return BoardSnapshotBuilder(stale_input_seconds=stale, monotonic=clock.monotonic)


def test_a_tile_without_proof_is_refused():
    """A tile whose provenance cannot be named is not a status (RL-012)."""
    with pytest.raises(TileWithoutProof):
        builder(Clock()).offer_tile("Tape", OK, "4.5M records", proof="")


def test_the_three_not_fine_states_stay_distinct():
    subject = builder(Clock())
    subject.offer_tile("Tape", OK, "recording", "the index files")
    subject.offer_tile("Trading", NOT_BUILT, "no order ever placed", "orders.jsonl absent")
    subject.mark_unmeasurable("BLAS", "no pool reported", "threadpoolctl")
    subject.offer_tile("Wiring", FAILING, "3 mismatches", "check_contracts.py")
    snapshot = subject.build()
    assert (snapshot.ok, snapshot.not_built, snapshot.not_measured, snapshot.failing) == (1, 1, 1, 1)


def test_something_expected_but_never_offered_is_unmeasured_not_absent():
    snapshot = builder(Clock()).build(expected_labels=("Tape", "Trading"))
    assert snapshot.not_measured == 2
    assert snapshot.is_complete is False


def test_a_tile_whose_probe_ran_long_ago_stops_being_green():
    """A board assembled from old probes is a historical document."""
    clock = Clock()
    subject = builder(clock, stale=60.0)
    subject.offer_tile("Tape", OK, "recording", "the index files")
    assert subject.build().ok == 1
    clock.now += 61
    snapshot = subject.build()
    assert snapshot.ok == 0
    assert snapshot.not_measured == 1


def test_the_snapshot_reports_its_oldest_input():
    clock = Clock()
    subject = builder(clock, stale=600.0)
    subject.offer_tile("old", OK, "v", "p")
    clock.now += 100
    subject.offer_tile("new", OK, "v", "p")
    assert subject.build().oldest_input_age_seconds == pytest.approx(100.0)


# ---- board-publisher ---------------------------------------------------------

class Snapshot:
    def __init__(self, tiles, built_at_ns=1):
        self.tiles = tiles
        self.built_at_ns = built_at_ns


class TileStub:
    def __init__(self, label, state="OK", value="v", proof="p"):
        self.label = label
        self.state = state
        self.value = value
        self.proof = proof


def test_a_changed_board_is_published():
    sent = []
    publisher = BoardPublisher("https://example/board", publish=lambda url, s: sent.append(url))
    link = publisher.publish_snapshot(Snapshot((TileStub("a"),)))
    assert link.state == PUBLISHED
    assert link.is_live is True
    assert sent == ["https://example/board"]


def test_an_unchanged_board_is_not_republished():
    """Republishing everything would hide which boards really did change."""
    sent = []
    publisher = BoardPublisher("https://example/board", publish=lambda url, s: sent.append(url))
    snapshot = Snapshot((TileStub("a"),))
    publisher.publish_snapshot(snapshot)
    link = publisher.publish_snapshot(Snapshot((TileStub("a"),), built_at_ns=999))
    assert link.state == UNCHANGED
    assert len(sent) == 1


def test_a_failed_publish_keeps_the_old_link_and_says_it_is_now_stale():
    def refuse(url, snapshot):
        raise OSError("network down")

    publisher = BoardPublisher("https://example/board", publish=refuse)
    link = publisher.publish_snapshot(Snapshot((TileStub("a"),)))
    assert link.state == FAILED
    assert "older than its source" in link.reason


def test_the_digest_covers_content_not_the_build_time():
    publisher = BoardPublisher("https://example/board", publish=lambda url, s: None)
    one = publisher.digest_of(Snapshot((TileStub("a"),), built_at_ns=1))
    two = publisher.digest_of(Snapshot((TileStub("a"),), built_at_ns=2))
    assert one == two


# ---- stale-board-watch -------------------------------------------------------

def watch(clock, lag=60.0, stopped=300.0):
    return StaleBoardWatch(
        maximum_lag_seconds=lag, source_stopped_after_seconds=stopped, monotonic=clock.monotonic
    )


def test_a_link_carrying_the_current_snapshot_is_in_step():
    clock = Clock()
    subject = watch(clock)
    subject.observe_snapshot("digest-1")
    subject.observe_link("digest-1")
    assert subject.check().state == IN_STEP


def test_a_published_board_behind_its_source_is_the_failure_this_part_exists_for():
    """It happened here: four boards correct on disk, two days stale at the link."""
    clock = Clock()
    subject = watch(clock, lag=60.0)
    subject.observe_snapshot("digest-1")
    subject.observe_link("digest-1")
    clock.now += 10
    subject.observe_snapshot("digest-2")
    clock.now += 200
    reading = subject.check()
    assert reading.state == LINK_STALE
    assert "convincing" in reading.reason
    assert subject.alert_for(reading).severity == HIGH


def test_a_stopped_source_is_not_reported_as_in_step():
    """A watch that only compared the two would call this perfectly in step."""
    clock = Clock()
    subject = watch(clock, stopped=300.0)
    subject.observe_snapshot("digest-1")
    subject.observe_link("digest-1")
    clock.now += 301
    assert subject.check().state == SOURCE_STOPPED


def test_a_board_never_published_at_all_is_named():
    clock = Clock()
    subject = watch(clock)
    subject.observe_snapshot("digest-1")
    subject.observe_link(None)
    assert subject.check().state == NEVER_PUBLISHED


# ---- drawdown-episode-tracker ------------------------------------------------

def tracker(clock, minimum=0.01):
    return DrawdownEpisodeTracker(minimum_depth_fraction=minimum, monotonic=clock.monotonic)


def test_an_episode_opens_on_a_fall_from_the_peak():
    clock = Clock()
    subject = tracker(clock)
    subject.observe_equity(1000.0)
    episode = subject.observe_equity(900.0)
    assert episode.state == OPEN
    assert episode.depth_fraction == pytest.approx(0.1)


def test_a_recovery_closes_the_episode_with_its_timings():
    """Two systems with the same worst drawdown are different businesses."""
    clock = Clock()
    subject = tracker(clock)
    subject.observe_equity(1000.0)
    clock.now += 10
    subject.observe_equity(900.0)
    clock.now += 50
    recovered = subject.observe_equity(1001.0)
    assert recovered.state == RECOVERED
    assert recovered.time_to_trough_seconds == pytest.approx(0.0)
    assert recovered.recovery_seconds == pytest.approx(50.0)
    assert recovered.duration_seconds == pytest.approx(50.0)


def test_the_open_episode_is_the_one_a_person_needs_to_see():
    clock = Clock()
    subject = tracker(clock)
    subject.observe_equity(1000.0)
    subject.observe_equity(900.0)
    clock.now += 100
    subject.observe_equity(850.0)
    open_now = subject.open_episode
    assert open_now.state == OPEN
    assert open_now.trough_equity == 850.0
    assert open_now.duration_seconds == pytest.approx(100.0)


def test_a_dip_shallower_than_the_minimum_is_not_an_episode():
    subject = tracker(Clock(), minimum=0.05)
    subject.observe_equity(1000.0)
    assert subject.observe_equity(990.0) is None


# ---- fund-conservation-auditor -----------------------------------------------

def a_fill(fill_id="f1", side=BUY, price=100.0, quantity=2.0, fee=0.5):
    return Fill(fill_id, VENUE, SYMBOL, side, price, quantity, fee, 1, "o1", True)


def test_a_correct_fill_conserves():
    auditor = FundConservationAuditor(tolerance=1e-9)
    check = auditor.check_fill(
        a_fill(), reported_cash_change=-(200.0 + 0.5), reported_position_value_change=200.0
    )
    assert check.verdict == CONSERVED
    assert auditor.audit_alert(check) is None


def test_a_fee_applied_twice_is_caught_and_the_step_is_named():
    auditor = FundConservationAuditor(tolerance=1e-9)
    check = auditor.check_fill(
        a_fill(fee=0.5), reported_cash_change=-(200.0 + 1.0), reported_position_value_change=200.0
    )
    assert check.verdict == DIVERGED
    assert "cash side" in check.reason
    alert = auditor.audit_alert(check)
    assert alert.severity == "critical"
    assert alert.proof


def test_a_sell_moves_cash_the_other_way():
    auditor = FundConservationAuditor(tolerance=1e-9)
    check = auditor.check_fill(
        a_fill(side=SELL), reported_cash_change=200.0 - 0.5, reported_position_value_change=-200.0
    )
    assert check.verdict == CONSERVED


def test_the_same_fill_audited_twice_is_a_divergence():
    auditor = FundConservationAuditor(tolerance=1e-9)
    auditor.check_fill(a_fill(), -200.5, 200.0)
    repeat = auditor.check_fill(a_fill(), -200.5, 200.0)
    assert repeat.verdict == DIVERGED
    assert auditor.standing.duplicates_caught == 1


# ---- clock-skew-monitor ------------------------------------------------------

def monitor(clock, drift=1.0, rejections=3, window=300.0):
    return ClockSkewMonitor(
        drift_warning_seconds=drift, rejections_before_alert=rejections,
        window_seconds=window, monotonic=clock.monotonic,
    )


def test_a_clock_in_step_raises_nothing():
    subject = monitor(Clock())
    subject.observe_venue_time(VENUE, venue_time_ns=1_000_000_000, local_time_ns=1_100_000_000)
    reading = subject.read(VENUE)
    assert reading.state == CLOCK_IN_STEP
    assert subject.alert_for(reading) is None


def test_drift_past_the_warning_is_caught_before_orders_fail():
    """There is a window between the first rejection and the last successful order."""
    subject = monitor(Clock(), drift=1.0)
    subject.observe_venue_time(VENUE, venue_time_ns=1_000_000_000, local_time_ns=3_000_000_000)
    reading = subject.read(VENUE)
    assert reading.state == DRIFTING
    assert subject.alert_for(reading).severity == HIGH


def test_a_venues_timestamp_rejection_is_recognised():
    subject = monitor(Clock(), rejections=3)
    assert subject.observe_status(VENUE, "Timestamp for this request is outside of the recvWindow")
    assert subject.observe_status(VENUE, "some other error") is False


def test_recurring_rejections_become_critical():
    subject = monitor(Clock(), rejections=3)
    for _ in range(3):
        subject.observe_status(VENUE, "invalid timestamp")
    reading = subject.read(VENUE)
    assert reading.state == REJECTING
    assert subject.alert_for(reading).severity == CRITICAL


def test_rejections_fall_out_of_the_window():
    clock = Clock()
    subject = monitor(clock, rejections=2, window=100.0)
    subject.observe_status(VENUE, "invalid timestamp")
    subject.observe_status(VENUE, "invalid timestamp")
    assert subject.read(VENUE).state == REJECTING
    clock.now += 101
    assert subject.read(VENUE).state != REJECTING


# ---- ablation-harness --------------------------------------------------------

def harness(clock, samples=3, recovery=10.0, significant=0.1, protected=()):
    return AblationHarness(
        measurement_samples=samples, recovery_seconds=recovery,
        significant_change_fraction=significant, protected_parts=protected,
        monotonic=clock.monotonic,
    )


def test_a_protected_part_is_never_switched_off():
    """Switching off the risk limiter to see what happens is an experiment with real money."""
    subject = harness(Clock(), protected=("halt-enforcer",))
    assert subject.begin_ablation("halt-enforcer") == PROTECTED
    assert subject.is_ablating is False


def test_a_part_whose_absence_changes_things_is_scored_as_mattering():
    clock = Clock()
    subject = harness(clock, samples=3, significant=0.1)
    for _ in range(5):
        subject.observe_baseline({"fills_per_minute": 100.0})
    subject.begin_ablation("the-router")
    for _ in range(3):
        subject.observe_ablated({"fills_per_minute": 10.0})
    scorecard = subject.end_ablation()
    assert scorecard.verdict == MATTERED
    assert scorecard.changes["fills_per_minute"] < -0.5


def test_no_measurable_effect_is_not_a_verdict_that_it_is_useless():
    clock = Clock()
    subject = harness(clock, samples=3, significant=0.1)
    for _ in range(5):
        subject.observe_baseline({"fills_per_minute": 100.0})
    subject.begin_ablation("a-safety-net")
    for _ in range(3):
        subject.observe_ablated({"fills_per_minute": 100.0})
    scorecard = subject.end_ablation()
    assert scorecard.verdict == NO_MEASURABLE_EFFECT
    assert "not proof it is useless" in scorecard.reason


def test_too_few_samples_are_inconclusive():
    clock = Clock()
    subject = harness(clock, samples=5)
    subject.observe_baseline({"x": 1.0})
    subject.begin_ablation("a")
    subject.observe_ablated({"x": 2.0})
    assert subject.end_ablation().verdict == INCONCLUSIVE


def test_only_one_part_is_ablated_at_a_time():
    """Two ablations at once cannot be attributed."""
    clock = Clock()
    subject = harness(clock, recovery=0.0)
    subject.begin_ablation("a")
    subject.begin_ablation("b")
    assert subject.standing.currently_ablating == "a"


def test_the_system_recovers_before_the_next_ablation():
    clock = Clock()
    subject = harness(clock, samples=2, recovery=30.0)
    for _ in range(3):
        subject.observe_baseline({"x": 1.0})
    subject.begin_ablation("a")
    for _ in range(2):
        subject.observe_ablated({"x": 1.0})
    subject.end_ablation()
    assert subject.begin_ablation("b") == RECOVERING
    clock.now += 31
    assert subject.begin_ablation("b") == ABLATING


# ---- heartbeat-collector: the table as a file, and what rides on it -----------

def test_input_loss_rides_on_the_heartbeat():
    """PartHealth carried input_loss since the substrate was built and nothing
    read it: on 2026-08-23 a symbol's price sat frozen 56 minutes in a part
    whose health read fine. The table is the first consumer."""
    subject = collector(Clock())
    subject.observe_health(
        PartHealth("the-model", "on", 1.0, 0.1, 1, None, input_loss=(("market-data", 12),))
    )
    assert subject.heartbeat_of("the-model").input_loss == (("market-data", 12),)


def test_the_table_file_round_trips_and_is_read_by_nothing_older(tmp_path):
    from parts.observability.heartbeat_collector import (
        read_heartbeat_table_file, write_heartbeat_table,
    )

    clock = Clock()
    subject = collector(clock, late=5.0, silent=30.0)
    subject.observe_health(PartHealth("a", "on", 1.0, 0.2, 1, None, input_loss=(("fill", 1),)))
    clock.now += 6
    subject.observe_health(PartHealth("b", "on", 0.5, 0.1, 2, None))
    path = tmp_path / "heartbeat-table.json"

    write_heartbeat_table(path, subject.read_table(), subject.standing)
    document = read_heartbeat_table_file(path)

    assert document["reporting"] == 1 and document["late"] == 1
    by_id = {beat["part_id"]: beat for beat in document["heartbeats"]}
    assert by_id["a"]["state"] == LATE
    assert by_id["a"]["input_loss"] == [["fill", 1]]
    assert by_id["b"]["state"] == REPORTING and by_id["b"]["rate_ratio"] == 0.5
    assert not list(tmp_path.glob(".*.partial")), "the temp file is replaced, never left"


def test_a_missing_or_half_written_table_reads_as_none_not_as_quiet(tmp_path):
    from parts.observability.heartbeat_collector import read_heartbeat_table_file

    assert read_heartbeat_table_file(tmp_path / "absent.json") is None
    (tmp_path / "half.json").write_text('{"schema_version": 1, "heartbeats": [')
    assert read_heartbeat_table_file(tmp_path / "half.json") is None
    (tmp_path / "other.json").write_text('{"schema_version": 99}')
    assert read_heartbeat_table_file(tmp_path / "other.json") is None


def test_heartbeat_collector_carries_the_entry_point_the_launcher_needs():
    from parts.observability import heartbeat_collector
    from runtime.part_launcher import PART_ENTRY_POINT

    assert callable(getattr(heartbeat_collector, PART_ENTRY_POINT))
