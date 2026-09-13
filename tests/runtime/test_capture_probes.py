"""The capture tiles, including the red one.

A board with no way to render red has never been tested against a real failure,
and the failure these tiles exist for is the quiet one: a capture that stopped
while everything around it kept looking fine. So these tests build real tapes on
real disk, write real records into them, and then make them stale.
"""

import json
import time

import pytest

from runtime import market_session_answer
from runtime.probes import capture_probes
from runtime.probes.capture_probes import (
    FAILING,
    NOT_BUILT,
    NOT_MEASURED,
    OK,
    STALE_HEALTH_SECONDS,
    STALE_TAPE_SECONDS,
    probe_capture_readers,
    probe_tape_freshness,
    probe_tape_records,
    probe_tape_size,
    run_all_capture_probes,
)
from runtime.tape import StreamKind, TapeWriter, resolve_tape_root

VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"
WRITEBACK_INTERVAL = 8 * 1024 * 1024
A_PAYLOAD = b'{"e":"aggTrade","p":"64000.10","q":"0.5"}'
NANOSECONDS_PER_SECOND = 1_000_000_000


@pytest.fixture
def tape_root(durable_tmp_path, monkeypatch):
    """Point the probes at a tape this test owns, through the same reader they use."""
    root = durable_tmp_path / "tape"
    monkeypatch.setattr(
        capture_probes, "_read_tape_root", lambda: (root, "a tape this test wrote")
    )
    # This suite writes a binance-usdm tape as a live venue. Which venues are
    # retired is read from the operator's own settings, so it is pinned here to
    # none; the retirement tests below pin it themselves.
    monkeypatch.setattr(capture_probes, "_retired_venues", lambda: frozenset())
    return root


def write_records(root, count, at_ns=None, venue=VENUE, symbol=SYMBOL):
    resolve_tape_root(root)
    at_ns = at_ns if at_ns is not None else time.time_ns()
    with TapeWriter(root, venue, symbol, WRITEBACK_INTERVAL) as writer:
        for offset in range(count):
            writer.append(StreamKind.TRADE, A_PAYLOAD, received_at_ns=at_ns + offset)
    return at_ns


def write_health(root, venue, observed_at_ns):
    directory = root.parent / "capture-health"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{venue}.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"observed_at_ns": observed_at_ns, "event": "capture-standing"}) + "\n")
    return path


def test_no_tape_at_all_is_not_built_rather_than_broken(tape_root):
    """A fresh clone has never captured, which is true and unalarming."""
    for probe in (probe_tape_records, probe_tape_freshness, probe_tape_size, probe_capture_readers):
        result = probe()
        assert result.state == NOT_BUILT, f"{result.label} said {result.state}"
        assert result.proof


def test_records_are_counted_from_the_index_files_own_size(tape_root):
    write_records(tape_root, 250)
    result = probe_tape_records()
    assert result.state == OK
    assert "250" in result.value
    assert VENUE in result.value


def test_a_tape_root_that_exists_but_holds_nothing_is_not_built(tape_root):
    resolve_tape_root(tape_root)
    assert probe_tape_records().state == NOT_BUILT
    assert probe_tape_freshness().state == NOT_BUILT


def test_a_fresh_tape_reads_as_fresh(tape_root):
    write_records(tape_root, 5)
    result = probe_tape_freshness()
    assert result.state == OK
    assert VENUE in result.value


def test_a_tape_that_stopped_being_written_goes_red(tape_root):
    """The failure this tile exists for, and the one nothing else would show.

    The tape itself cannot tell a stopped reader from a silent market -- so the
    board says stale, loudly, rather than saying nothing at all.
    """
    stale_ns = time.time_ns() - int((STALE_TAPE_SECONDS + 30) * NANOSECONDS_PER_SECOND)
    write_records(tape_root, 5, at_ns=stale_ns)
    result = probe_tape_freshness()
    assert result.state == FAILING
    assert VENUE in result.value
    assert "stopped writing" in result.value
    assert str(int(STALE_TAPE_SECONDS)) in result.proof


def test_one_stale_venue_reddens_the_tile_even_when_the_other_is_live(tape_root):
    """A half-dead capture must not read as a healthy one."""
    write_records(tape_root, 5)
    write_records(
        tape_root,
        5,
        at_ns=time.time_ns() - int((STALE_TAPE_SECONDS + 30) * NANOSECONDS_PER_SECOND),
        venue="bybit-linear",
    )
    result = probe_tape_freshness()
    assert result.state == FAILING
    assert "bybit-linear" in result.value


def test_a_reader_still_reporting_reads_as_reporting(tape_root):
    write_records(tape_root, 1)
    write_health(tape_root, VENUE, time.time_ns())
    result = probe_capture_readers()
    assert result.state == OK
    assert VENUE in result.value


def test_a_reader_that_stopped_reporting_goes_red(tape_root):
    """Read from what the reader wrote, not from whether a process exists.

    A process that is alive and has stopped reading is exactly the failure worth
    catching, and only the reader's own reports can tell the difference.
    """
    write_records(tape_root, 1)
    write_health(
        tape_root, VENUE, time.time_ns() - int((STALE_HEALTH_SECONDS + 30) * NANOSECONDS_PER_SECOND)
    )
    result = probe_capture_readers()
    assert result.state == FAILING
    assert "stopped reporting" in result.value


def test_a_health_log_of_junk_is_unmeasured_not_healthy(tape_root):
    write_records(tape_root, 1)
    directory = tape_root.parent / "capture-health"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{VENUE}.jsonl").write_text("not json at all\n")
    result = probe_capture_readers()
    assert result.state == NOT_MEASURED
    assert VENUE in result.value


def test_the_health_tail_is_read_from_the_end_not_the_whole_file(tape_root):
    """A board build must not get slower every hour the capture runs."""
    write_records(tape_root, 1)
    now = time.time_ns()
    for index in range(5000):
        write_health(tape_root, VENUE, now - (5000 - index))
    started = time.monotonic()
    result = probe_capture_readers()
    assert result.state == OK
    assert time.monotonic() - started < 1.0


def test_unreadable_settings_are_unmeasured_rather_than_absent(monkeypatch):
    """Rule 8: a fact that could not be established is its own state, never a gap."""
    monkeypatch.setattr(capture_probes, "_read_tape_root", lambda: (None, "settings unreadable"))
    for result in run_all_capture_probes():
        assert result.state == NOT_MEASURED
        assert result.proof


def test_the_size_tile_reports_what_was_written_and_what_is_left(tape_root):
    write_records(tape_root, 100)
    result = probe_tape_size()
    assert result.state == OK
    assert "GB written" in result.value and "GB free" in result.value


def test_a_probe_that_crashes_reports_unmeasured_rather_than_vanishing(tape_root, monkeypatch):
    def explode():
        raise RuntimeError("the disk went away")

    monkeypatch.setattr(capture_probes, "probe_tape_records", explode)
    monkeypatch.setattr(capture_probes, "CAPTURE_PROBES", (explode,))
    results = run_all_capture_probes()
    assert len(results) == 1
    assert results[0].state == NOT_MEASURED
    assert "RuntimeError" in results[0].value


# ---- retired venues and the newest day (2026-09-13) --------------------------

def test_a_retired_venue_is_named_but_does_not_redden_the_freshness_tile(tape_root, monkeypatch):
    now = time.time_ns()
    write_records(tape_root, 3, at_ns=now - int(3600 * NANOSECONDS_PER_SECOND), venue=VENUE)
    write_records(tape_root, 3, at_ns=now, venue="upstox", symbol="NSE_EQ|INE002A01018")
    monkeypatch.setattr(capture_probes, "_retired_venues", lambda: frozenset({VENUE}))

    result = probe_tape_freshness()

    assert result.state == OK, result.value
    assert "retired: binance-usdm" in result.value


def test_a_tape_holding_only_retired_venues_is_not_built_rather_than_failing(tape_root, monkeypatch):
    write_records(tape_root, 3, at_ns=time.time_ns() - int(3600 * NANOSECONDS_PER_SECOND))
    monkeypatch.setattr(capture_probes, "_retired_venues", lambda: frozenset({VENUE}))
    assert probe_tape_freshness().state == NOT_BUILT


def test_a_retired_reader_is_named_but_does_not_redden_the_readers_tile(tape_root, monkeypatch):
    write_health(tape_root, VENUE, time.time_ns() - int(3600 * NANOSECONDS_PER_SECOND))
    monkeypatch.setattr(capture_probes, "_retired_venues", lambda: frozenset({VENUE}))
    result = probe_capture_readers()
    assert result.state == NOT_BUILT
    assert "retired: binance-usdm" in result.value


def test_a_live_venue_that_stopped_is_still_red_beside_a_retired_one(tape_root, monkeypatch):
    stale = time.time_ns() - int(3600 * NANOSECONDS_PER_SECOND)
    write_records(tape_root, 3, at_ns=stale, venue=VENUE)
    write_records(tape_root, 3, at_ns=stale, venue="upstox", symbol="NSE_EQ|INE002A01018")
    monkeypatch.setattr(capture_probes, "_retired_venues", lambda: frozenset({VENUE}))
    monkeypatch.setattr(capture_probes, "_session_answer", lambda: (capture_probes.IN_SESSION, "calendar"))
    result = probe_tape_freshness()
    assert result.state == FAILING
    assert result.value.startswith("upstox stopped writing")


def test_freshness_reads_only_each_venues_newest_day(tape_root, monkeypatch):
    now = time.time_ns()
    write_records(tape_root, 3, at_ns=now - int(3 * 86400 * NANOSECONDS_PER_SECOND))
    write_records(tape_root, 3, at_ns=now)
    opened = []
    real = capture_probes._newest_record_time_ns
    monkeypatch.setattr(
        capture_probes, "_newest_record_time_ns",
        lambda path: opened.append(path.name) or real(path),
    )

    assert probe_tape_freshness().state == OK
    days = {name.split(".", 1)[0] for name in opened}
    assert len(days) == 1, opened


def test_the_retired_set_is_derived_from_the_adapters_and_the_setting():
    """Against this build and the operator's own settings: the two crypto adapters, not Upstox."""
    retired = capture_probes._retired_venues()
    assert "upstox" not in retired
    assert "news" not in retired
    assert retired <= {"binance-usdm", "bybit-linear"}



# ---- the broker tape is judged against the exchange session (2026-09-13) -----

UPSTOX = "upstox"
AN_OPTION = "NSE_FO|12345"


def a_stale_upstox_tape(tape_root, seconds_old=1800):
    write_records(
        tape_root, 3, at_ns=time.time_ns() - int(seconds_old * NANOSECONDS_PER_SECOND),
        venue=UPSTOX, symbol=AN_OPTION,
    )


def test_a_quiet_broker_tape_out_of_session_is_not_failing(tape_root, monkeypatch):
    """Sunday 2026-09-13 read `upstox` FAILING at 1,804s stale. The market was shut."""
    a_stale_upstox_tape(tape_root)
    monkeypatch.setattr(capture_probes, "_session_answer", lambda: (capture_probes.OUT_OF_SESSION, "calendar"))
    result = probe_tape_freshness()
    assert result.state == OK, result.value
    assert "NSE out of session" in result.value


def test_a_quiet_broker_tape_in_session_is_failing(tape_root, monkeypatch):
    a_stale_upstox_tape(tape_root)
    monkeypatch.setattr(capture_probes, "_session_answer", lambda: (capture_probes.IN_SESSION, "calendar"))
    assert probe_tape_freshness().state == FAILING


def test_with_no_session_answer_a_quiet_broker_tape_is_unmeasured_not_green(tape_root, monkeypatch):
    a_stale_upstox_tape(tape_root)
    monkeypatch.setattr(capture_probes, "_session_answer", lambda: (None, "no answer"))
    result = probe_tape_freshness()
    assert result.state == NOT_MEASURED
    assert "cannot tell" in result.value


def test_a_venue_with_no_broker_module_keeps_the_plain_bound(tape_root, monkeypatch):
    """`news` has no session: the calendar does not excuse it."""
    write_records(
        tape_root, 3, at_ns=time.time_ns() - int(1800 * NANOSECONDS_PER_SECOND),
        venue="news", symbol="upstox-news",
    )
    monkeypatch.setattr(capture_probes, "_session_answer", lambda: (capture_probes.OUT_OF_SESSION, "calendar"))
    assert probe_tape_freshness().state == FAILING


def a_heartbeat_table(path, collected_seconds_ago, standing, state="reporting"):
    path.write_text(json.dumps({
        "collected_at_ns": time.time_ns() - int(collected_seconds_ago * NANOSECONDS_PER_SECOND),
        "heartbeats": [{"part_id": "market-session-calendar", "state": state, "standing": standing}],
    }))


@pytest.fixture
def heartbeat_table(durable_tmp_path, monkeypatch):
    path = durable_tmp_path / "heartbeat-table.json"
    values = {"heartbeat_table_path": str(path), "heartbeat_silent_after_seconds": 10.0}

    class Document:
        def read_value(self, name):
            return values[name]

    monkeypatch.setattr(market_session_answer, "load_settings_document", lambda *_: Document())
    return path


@pytest.mark.parametrize(
    ("standing", "expected"),
    [
        ({"is_open": 1.0, "has_a_holiday_list": 1.0}, "in session"),
        ({"is_open": 0.0, "has_a_holiday_list": 1.0}, "out of session"),
        ({"is_open": 0.0, "has_a_holiday_list": 0.0}, None),
        ({"holidays_known": 20.0}, None),
    ],
)
def test_the_session_is_read_from_the_calendars_own_standing(heartbeat_table, standing, expected):
    a_heartbeat_table(heartbeat_table, 1, standing)
    assert capture_probes._session_answer()[0] == expected


def test_a_stale_heartbeat_table_is_no_session_answer(heartbeat_table):
    a_heartbeat_table(heartbeat_table, 60, {"is_open": 0.0, "has_a_holiday_list": 1.0})
    answer, proof = capture_probes._session_answer()
    assert answer is None
    assert "old" in proof


def test_a_silent_calendar_is_no_session_answer(heartbeat_table):
    a_heartbeat_table(heartbeat_table, 1, {"is_open": 0.0, "has_a_holiday_list": 1.0}, state="silent")
    assert capture_probes._session_answer()[0] is None
