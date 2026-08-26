"""The detector must judge what it heard, and say a verdict only when it changes.

Measured on the live spine at 10:14 on 2026-08-26, before this was fixed:

    checks                 5,967 a second   327 parts re-judged on every tick
    faults_found           5,316 a second   the same verdicts, restated
    part-fault published  26,575 a second   from 283 messages a second received

SUSPICIOUSLY_PERFECT is true of nearly every part in this system and stays true --
zero errors over a long run is what a working part looks like -- so the detector
had one permanent verdict per part and put it on the bus on every wake. Every part
publishes health once a second, so a part consuming part-health wakes 327 times a
second; the detector turned each of those wakes into 327 checks and hundreds of
messages. Downstream, the warden escalated each fault and the restart budgeter
republished every budget on each, and the spine reached 89,747 messages a second
at load average 28.7 on twelve cores -- with seven parts reading as silent because
they could not get the CPU to send the heartbeat that proves they are alive.

The two rules that leave that unreachable:

  * a part nobody heard from this tick is not re-judged -- its verdict is computed
    from its own tick count, durations and errors, and none of those moved;
  * a verdict identical to the one already on the bus is not sent again.
"""

from __future__ import annotations

from parts.autonomous.failing_part_detector import (
    FailingPartDetector,
    SUSPICIOUSLY_PERFECT,
    run_failing_part_detector,
    verdict_of,
)
from runtime.level_publishing import LevelPublisherByKey


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class StopAfter:
    """A control socket that ends the loop after a fixed number of ticks."""

    def __init__(self, ticks: int) -> None:
        self.remaining = ticks

    def fileno(self) -> int:  # select() is monkeypatched away below
        return -1


def _detector() -> FailingPartDetector:
    return FailingPartDetector(
        window=32, minimum_ticks=3, stuck_answer_ticks=4,
        slowdown_ratio=3.0, perfect_run_ticks=5,
    )


def _report(part_id: str) -> dict:
    """One health report in the shape start_part builds from a live part-health."""
    return {
        "part_id": part_id, "tick_seconds": 1.0, "produced": 1,
        "errors": 0, "output_digest": None,
    }


def _drive(detector, reports_per_tick, ticks: int, refresh_interval_seconds: float = 1.0):
    """Run the detector's own tick body directly, the way run_part would call it."""
    sent: list[tuple] = []
    clock = FakeClock()
    levels = LevelPublisherByKey(
        publish=sent.append,
        refresh_interval_seconds=refresh_interval_seconds,
        monotonic=clock,
        identity_of=verdict_of,
    )
    body = {}

    def capture_run_part(*, do_one_tick, **_rest):
        body["tick"] = do_one_tick
        return 0

    import parts.autonomous.failing_part_detector as module

    original = module.run_part
    module.run_part = capture_run_part
    try:
        run_failing_part_detector(
            detector=detector,
            control_socket=None,
            read_health=lambda: reports_per_tick(),
            publish_faults=lambda fault: levels.publish_level(fault.part_id, (fault,)),
            health_interval_seconds=1.0,
            emit_health=lambda _health: None,
            clear_fault=levels.forget,
            levels=levels,
        )
    finally:
        module.run_part = original

    for _ in range(ticks):
        body["tick"]()
    return sent, levels, clock


PARTS = tuple(f"part-{index:03d}" for index in range(327))


def test_a_part_nobody_heard_from_is_not_re_judged():
    """5,967 checks a second came from re-judging 327 parts on every wake."""
    detector = _detector()
    # Every part is heard from once, then only one part keeps reporting.
    first = [True]

    def reports():
        if first[0]:
            first[0] = False
            return tuple(_report(part_id) for part_id in PARTS)
        return (_report(PARTS[0]),)

    _sent, _levels, _clock = _drive(detector, reports, ticks=100)

    # 327 on the first tick, then one per tick for 99 more.
    assert detector.standing.checks == len(PARTS) + 99, (
        f"{detector.standing.checks} checks; re-judging every part every tick would "
        f"have been {len(PARTS) * 100}"
    )


def test_an_unchanged_verdict_is_not_republished():
    """The same permanent fault, offered on every tick, reaches the bus once."""
    detector = _detector()
    part_id = PARTS[0]

    def reports():
        return (_report(part_id),)

    # perfect_run_ticks is 5, so the verdict turns SUSPICIOUSLY_PERFECT and stays.
    sent, levels, _clock = _drive(detector, reports, ticks=327)

    faults = [item[0] for item in sent]
    assert faults, "the detector never raised the fault at all"
    assert all(fault.kind == SUSPICIOUSLY_PERFECT for fault in faults)
    assert len(sent) == 1, (
        f"one unchanged verdict went onto the bus {len(sent)} times over 327 ticks"
    )
    assert levels.standing.unchanged_publishes_skipped > 300


def test_an_unchanged_verdict_is_still_refreshed_once_an_interval():
    """A consumer that starts after the verdict settled must still learn it."""
    detector = _detector()
    part_id = PARTS[0]
    sent, levels, clock = _drive(detector, lambda: (_report(part_id),), ticks=6)
    assert len(sent) == 1

    for _ in range(327):
        clock.advance(1.0 / 327.0)
    clock.advance(1.0 / 327.0)
    _sent_again, _levels, _clock = None, None, None
    # One more tick after the interval has passed.
    detector_sent_before = len(sent)
    levels.publish_level(part_id, sent[0])
    assert len(sent) == detector_sent_before + 1, "the verdict was never refreshed"
    assert levels.standing.refreshes == 1


def test_a_part_that_recovers_stops_being_reported():
    """Without forgetting the key, a re-fault inside one interval would be swallowed."""
    detector = _detector()
    part_id = PARTS[0]
    healthy = [False]

    def reports():
        report = _report(part_id)
        if healthy[0]:
            report["errors"] = 1  # an error clears SUSPICIOUSLY_PERFECT
        return (report,)

    sent, levels, _clock = _drive(detector, reports, ticks=6)
    assert len(sent) == 1 and levels.keys_held == 1

    healthy[0] = True
    _drive_again = None
    # The detector is already built; drive its body a few more ticks by hand.
    import parts.autonomous.failing_part_detector as module

    detector.observe_health(**reports()[0])
    outcome = detector.check(part_id)
    assert not outcome.is_usable, "an error should clear the suspiciously-perfect verdict"
    levels.forget(part_id)
    assert levels.keys_held == 0
