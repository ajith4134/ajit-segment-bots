"""closed_broker_candles_on_the_tape's own bucket-closing rule, real bug
2026-09-02: reusing closed_candles_on_the_tape's crypto heuristic against a
broker tape would read an early, incomplete restatement of a forming bar as
if it were the close -- see the function's own docstring for why the two
tapes are shaped differently. The bytes here are a deterministic pattern,
not market data, same as tape.py's own tests (RL-063 binds real captured
data to the parts that decide something from it, not to the tape format's
own correctness)."""

import datetime
import json

import pytest

from runtime.candle_history import closed_broker_candles_on_the_tape
from runtime.tape import StreamKind, TapeWriter, resolve_tape_root

VENUE = "upstox"
SYMBOL = "NIFTY 24500 CE"
DAY = "2026-08-21"
DAY_START_NS = 1787270400_000_000_000  # 2026-08-21T00:00:00Z
ONE_MINUTE_NS = 60_000_000_000
NOW = datetime.datetime(2026, 8, 21, 12, 0, 0, tzinfo=datetime.UTC)

WRITEBACK_INTERVAL = 8 * 1024 * 1024


@pytest.fixture
def tape_root(durable_tmp_path):
    return resolve_tape_root(durable_tmp_path / "tape")


def _write_bar(writer, open_time_ns, open_, high, low, close, volume, interval="I1"):
    payload = json.dumps({
        "instrument_key": "NSE_FO|1001", "interval": interval,
        "open": open_, "high": high, "low": low, "close": close, "volume": volume,
        "bar_time_ms": open_time_ns // 1_000_000, "is_closed": None,
    }).encode("utf-8")
    writer.append(
        StreamKind.CANDLE, payload,
        received_at_ns=DAY_START_NS + 43_200_000_000_000,  # noon, well inside DAY
        venue_time_ns=open_time_ns,
    )


def test_returns_empty_tuple_when_the_tape_has_nothing(tape_root):
    assert closed_broker_candles_on_the_tape(
        tape_root=tape_root, venue_id=VENUE, symbol=SYMBOL,
        wanted_interval="I1", interval_ns=ONE_MINUTE_NS, wanted=10, now=NOW,
    ) == ()


def test_the_last_restatement_of_a_closed_bucket_wins_not_the_first(tape_root):
    """The real bug this guards: bar A is restated three times as it fills in
    (volume climbing 100 -> 300 -> 500), and only closes once bar B's own
    first tick proves the tape moved past A's bucket. The seeded candle must
    carry A's final state (close=101.0, volume=500), not its first partial
    tick -- the crypto heuristic's "first few records of the next bucket"
    rule would have picked bar B's own opening print instead, an entirely
    different bar."""
    bar_a_open = DAY_START_NS + 43_200_000_000_000
    bar_b_open = bar_a_open + ONE_MINUTE_NS
    with TapeWriter(
        tape_root, VENUE, SYMBOL, WRITEBACK_INTERVAL, stream_kind=StreamKind.CANDLE,
    ) as writer:
        _write_bar(writer, bar_a_open, 100.0, 100.5, 99.5, 100.2, 100.0)
        _write_bar(writer, bar_a_open, 100.0, 101.0, 99.5, 100.8, 300.0)
        _write_bar(writer, bar_a_open, 100.0, 101.0, 99.0, 101.0, 500.0)
        _write_bar(writer, bar_b_open, 101.0, 101.2, 100.8, 101.1, 50.0)

    result = closed_broker_candles_on_the_tape(
        tape_root=tape_root, venue_id=VENUE, symbol=SYMBOL,
        wanted_interval="I1", interval_ns=ONE_MINUTE_NS, wanted=10, now=NOW,
    )
    assert len(result) == 1
    assert result[0].open_time_ns == bar_a_open
    assert result[0].close == 101.0
    assert result[0].volume == 500.0


def test_a_record_at_a_different_interval_is_filtered_out(tape_root):
    """A real feed message bundles several granularities together (module
    docstring); a daily bar sharing this tape must never be picked up when
    the caller asked for 1-minute history."""
    bar_open = DAY_START_NS + 43_200_000_000_000
    with TapeWriter(
        tape_root, VENUE, SYMBOL, WRITEBACK_INTERVAL, stream_kind=StreamKind.CANDLE,
    ) as writer:
        _write_bar(writer, bar_open, 100.0, 100.5, 99.5, 100.2, 100.0, interval="1d")
        _write_bar(writer, bar_open, 100.0, 100.5, 99.5, 100.2, 100.0, interval="I1")
        _write_bar(writer, bar_open + ONE_MINUTE_NS, 100.2, 100.6, 99.8, 100.4, 60.0, interval="I1")

    result = closed_broker_candles_on_the_tape(
        tape_root=tape_root, venue_id=VENUE, symbol=SYMBOL,
        wanted_interval="I1", interval_ns=ONE_MINUTE_NS, wanted=10, now=NOW,
    )
    assert len(result) == 1
    assert result[0].open_time_ns == bar_open


def test_the_still_forming_final_bucket_is_excluded(tape_root):
    """Nothing on the tape proves the newest bar closed -- no later record's
    bucket differs from it -- so it must not be seeded as history."""
    bar_open = DAY_START_NS + 43_200_000_000_000
    with TapeWriter(
        tape_root, VENUE, SYMBOL, WRITEBACK_INTERVAL, stream_kind=StreamKind.CANDLE,
    ) as writer:
        _write_bar(writer, bar_open, 100.0, 100.5, 99.5, 100.2, 100.0)

    result = closed_broker_candles_on_the_tape(
        tape_root=tape_root, venue_id=VENUE, symbol=SYMBOL,
        wanted_interval="I1", interval_ns=ONE_MINUTE_NS, wanted=10, now=NOW,
    )
    assert result == ()
