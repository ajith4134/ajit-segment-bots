"""Fixtures shared by every test in this project.

At the repository root rather than per directory because both things set up here
are needed by every test that touches the runtime or a part, and a fixture that
exists in one directory is a fixture the next directory silently does without.

pytest's own tmp_path lands under /tmp, which is tmpfs on this box. Any test that
measures durability or memory there measures nothing. So durable_tmp_path is used
instead, and it asserts what it handed back.

The BLAS thread caps are applied here, before any test module anywhere can import
numpy. Measured: with them unset, import numpy alone puts 12 kernel threads in the
process. pytest imports every conftest.py from the rootdir down before it imports
any test module, so this is the one place guaranteed to run first.

The captured-payload readers import the capture script itself rather than
re-implementing its format: two definitions of the fixture format would be two
things to keep in step, and the failure when they drifted would look like a venue
changing its messages.
"""

import importlib.util
import json
import pathlib
import shutil

import pytest

from runtime.forkserver_launcher import apply_blas_thread_caps
from runtime.storage_facts import require_durable_directory

apply_blas_thread_caps()

PROJECT = pathlib.Path(__file__).resolve().parent.parent
DURABLE_TEST_ROOT = pathlib.Path.home() / ".cache" / "ajit-segment-bots" / "tests"
CAPTURED_ROOT = PROJECT / "tests" / "captured"
CAPTURE_SCRIPT = CAPTURED_ROOT / "capture_venue_payloads.py"


@pytest.fixture
def durable_tmp_path(request) -> pathlib.Path:
    """A scratch directory on real disk, proven so before it is handed over."""
    DURABLE_TEST_ROOT.mkdir(parents=True, exist_ok=True)
    require_durable_directory(DURABLE_TEST_ROOT)
    path = DURABLE_TEST_ROOT / request.node.name.replace("/", "_")[:120]
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _load_capture_script():
    """Import the capture script by path -- tests/captured is fixtures, not a package."""
    specification = importlib.util.spec_from_file_location("capture_venue_payloads", CAPTURE_SCRIPT)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def read_captured_payloads():
    """Return a reader: venue and file name -> [(received_at_ns, raw payload bytes)].

    Fails rather than skips when a fixture is absent. A missing capture is not a
    machine that cannot run this test; it is a test that has stopped checking
    anything, which is the thing Rule 8 says must never render as fine.
    """
    read_payload_lines = _load_capture_script().read_payload_lines

    def read(venue: str, name: str) -> list[tuple[int, bytes]]:
        path = CAPTURED_ROOT / venue / name
        assert path.exists(), (
            f"{path} is missing. Recapture it with "
            f"`.venv/bin/python tests/captured/capture_venue_payloads.py {venue} --day <YYYY-MM-DD>` "
            f"-- these tests run on what the venue actually sent (RL-063), so there is no "
            f"fallback to a fixture written by hand."
        )
        records = read_payload_lines(path)
        assert records, f"{path} is empty"
        return records

    return read


@pytest.fixture(scope="session")
def read_captured_json():
    """Return a reader for a captured REST response."""

    def read(venue: str, name: str):
        path = CAPTURED_ROOT / venue / name
        assert path.exists(), f"{path} is missing; recapture it with capture_venue_payloads.py"
        return json.loads(path.read_text())

    return read


@pytest.fixture(scope="session")
def capture_manifest():
    """What was captured, when, from where, and how it was subset."""
    return json.loads((CAPTURED_ROOT / "capture-manifest.json").read_text())


@pytest.fixture(scope="session")
def read_captured_trades(read_captured_payloads):
    """Return a reader: real trades off the captured tape, in this system's terms.

    Decoded by the venue's own adapter rather than by a parser written here, for
    the same reason the payload readers import the capture script: a second
    definition of what a venue's message means is a second thing to keep in step.

    RL-063 -- a test about staleness needs prices whose spacing in time is the
    market's own. Invented timestamps would make the test agree with whatever the
    code does.
    """
    from runtime.venues.adapter_registry import load_venue_adapter

    def read(venue: str = "binance-usdm", name: str = "2026-08-22-btcusdt-aggtrade-run.jsonl",
             limit: int | None = None):
        adapter = load_venue_adapter(venue)
        trades = []
        for _, payload in read_captured_payloads(venue, name):
            trades.extend(adapter.read_trades(payload))
            if limit is not None and len(trades) >= limit:
                return trades[:limit]
        assert trades, f"{venue}/{name} decoded to no trades"
        return trades

    return read


@pytest.fixture(scope="session")
def arriving_now():
    """Return a restamper: real captured trades, dated as though just printed.

    The prices, sizes, sides and the spacing between prints are the venue's own,
    and none of it is altered -- that is what RL-063 is for. What is restamped is
    only when each print says it happened.

    This is the seam RL-071 names. A part that judges how old a price is compares
    the print's time against now, so a fixture recorded this morning, or on
    2026-08-22, is correctly refused as stale when replayed this afternoon. The
    live spine never needs this: its prints carry the market's own time and are
    genuinely current. Only a replay has to say when it is pretending to be, and
    saying so here, once, keeps the pretence out of the parts.

    Restamp at each publish rather than once per run: an integration test that
    takes minutes would otherwise watch its own fixture age past every bound
    halfway through.
    """
    import dataclasses
    import time

    def restamp(trades):
        if not trades:
            return trades
        shift_ns = time.time_ns() - max(trade.venue_time_ns for trade in trades)
        return [
            dataclasses.replace(trade, venue_time_ns=trade.venue_time_ns + shift_ns)
            for trade in trades
        ]

    return restamp


def most_recent_day_the_tape_holds(venue_ids, tape_root=None) -> str | None:
    """The latest day these venues actually recorded, or None if they never did.

    Three integration tests read "today's" trades, which was right while the tape
    was being written around the clock: a crypto venue prints every day and today
    always existed. It stopped being right on 2026-09-01, when the goal pivoted to
    Indian markets and the crypto spine went inactive. From then on those tests
    errored on every run -- "the tape holds only 0 trades for <today>" -- six of
    them, permanently, which is a red suite that says nothing about the code and
    buries the failures that do.

    The day is only which prints to read. `arriving_now` restamps them to now, so
    the age a part judges is unaffected and RL-063 still holds: these are the
    venue's own prints, in the venue's own order, at the venue's own spacing.

    Returns None rather than guessing when no venue has a single recorded day, so
    a caller can skip with that as the stated reason instead of failing on an
    assertion about a tape that was never going to be there.
    """
    import pathlib

    root = tape_root or pathlib.Path.home() / ".local/share/ajit-segment-bots/tape"
    days = set()
    for venue_id in venue_ids:
        venue_root = root / venue_id
        if not venue_root.is_dir():
            continue
        for symbol_directory in venue_root.iterdir():
            for index_path in symbol_directory.glob("*.index"):
                if index_path.stat().st_size > 0:
                    days.add(index_path.stem.split(".")[0])
    return max(days) if days else None
