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


# --- the Upstox tape, for tests that used to replay the crypto pair -----------

UPSTOX_VENUE = "upstox"
UPSTOX_INSTRUMENT_MASTER = (
    pathlib.Path.home()
    / ".local/share/ajit-segment-bots/instrument-master/complete.json.gz"
)


def upstox_days_newest_first(tape_root=None) -> list[str]:
    """Every day the Upstox tape holds an index for, newest first."""
    root = (tape_root or pathlib.Path.home() / ".local/share/ajit-segment-bots/tape") / UPSTOX_VENUE
    if not root.is_dir():
        return []
    days = set()
    for instrument_directory in root.iterdir():
        for index_path in instrument_directory.glob("*.index"):
            if index_path.stat().st_size > 0:
                days.add(index_path.stem.split(".")[0])
    return sorted(days, reverse=True)


def most_recent_upstox_trading_day(minimum_prints: int, instruments: int = 6,
                                   tape_root=None) -> str | None:
    """The newest day the tape holds enough real prints on to replay.

    **Not simply the newest day.** The Indian market is shut at weekends and the
    feed keeps its connection open through them, so a Saturday or Sunday holds a
    handful of records restating Friday's last price -- measured 2026-09-06, the
    six busiest NSE_FO instruments held 42 records of which 7 carried a real
    price, against 34,688 on Friday the 4th. A replay of the weekend is a replay
    of nothing, and it would fail as though the chain were broken.

    Returns None rather than guessing when no day qualifies, so a caller can skip
    with that as the stated reason -- the same contract
    `most_recent_day_the_tape_holds` already has.
    """
    for day in upstox_days_newest_first(tape_root=tape_root):
        keys = busiest_upstox_instruments(day, instruments, tape_root=tape_root)
        if not keys:
            continue
        if len(upstox_trades_for(day, keys, minimum_prints, tape_root=tape_root)) >= minimum_prints:
            return day
    return None


def upstox_listings_by_key(master_path=None) -> dict:
    """Upstox's own instrument master, parsed by Upstox's own adapter.

    Parsed rather than restated: `read_instrument_listings` is where paise become
    rupees and where every field this project reads gets its name, and a test that
    built `InstrumentListing` by hand would be testing a second implementation of
    the thing under test.
    """
    import gzip
    import json as _json

    from runtime.brokers.upstox import UpstoxAdapter

    path = master_path or UPSTOX_INSTRUMENT_MASTER
    if not path.exists():
        return {}
    rows = _json.loads(gzip.open(path, "rt", encoding="utf-8").read())
    return {
        listing.instrument_key: listing
        for listing in UpstoxAdapter().read_instrument_listings(rows)
    }


def busiest_upstox_instruments(day: str, count: int, segments=("NSE_FO",), tape_root=None) -> list:
    """The instrument keys with the most captured prints that day, by segment.

    Segment-filtered because the subscription carries plenty a segment bot does
    not trade, and a test that replayed a currency-derivative chain would prove
    nothing about the bots that exist.
    """
    root = (tape_root or pathlib.Path.home() / ".local/share/ajit-segment-bots/tape") / UPSTOX_VENUE
    if not root.is_dir():
        return []
    sized = []
    for instrument_directory in root.iterdir():
        if segments and not instrument_directory.name.split("|")[0] in segments:
            continue
        index_path = instrument_directory / f"{day}.index"
        if index_path.exists() and index_path.stat().st_size > 0:
            sized.append((index_path.stat().st_size, instrument_directory.name))
    sized.sort(reverse=True)
    return [key for _size, key in sized[:count]]


def busiest_upstox_option_chain(day: str, count: int, tape_root=None) -> list[str]:
    """The day's busiest contracts that share ONE underlying, as a real chain.

    Selecting the busiest contracts outright picks them across unrelated
    underlyings, and two options on different underlyings have no reason to move
    together. `cointegration-pair-finder` then finds nothing and every part
    behind it correctly raises nothing -- which reads as a broken scanner and is
    not one.

    Measured on the captured tape of 2026-09-04: the six busiest NSE_FO
    contracts spanned four underlyings and produced **0 cointegrated pairs**,
    while the six busiest NIFTY contracts produced **6 tradeable ones**. An
    option chain is also what the index-options bot actually trades, so this is
    the shape the test should have been using.
    """
    listings = upstox_listings_by_key()
    ranked = busiest_upstox_instruments(day, count * 20, tape_root=tape_root)
    by_underlying: dict[str, list[str]] = {}
    for key in ranked:
        listing = listings.get(key)
        underlying = getattr(listing, "underlying_symbol", None) if listing else None
        if underlying:
            by_underlying.setdefault(underlying, []).append(key)
    if not by_underlying:
        return []
    # The chain with the most captured contracts, in the order they were ranked,
    # so what is replayed is the busiest part of the busiest chain.
    busiest = max(by_underlying.values(), key=len)
    return busiest[:count]


def upstox_trades_for(day, instrument_keys, trades_per_instrument, tape_root=None) -> list:
    """Real captured Upstox prints, as `market-data`, oldest first.

    Runs the prints through `BrokerMarketDataBridge` -- the part the live spine
    uses -- rather than building `NormalisedTrade` here, so a test replays what
    the running system would actually have seen. That is also what carries the
    2026-09-06 correction: Upstox states no size on about three quarters of its
    LTP updates and none at all on an index, and the bridge passes those through
    with `quantity=None` instead of dropping them.
    """
    import json as _json

    from parts.market_data_feed.broker_market_data_bridge import BrokerMarketDataBridge
    from runtime.brokers.broker_adapter import LtpUpdate
    from runtime.tape import read_payload, read_tape_index

    root = (tape_root or pathlib.Path.home() / ".local/share/ajit-segment-bots/tape") / UPSTOX_VENUE
    listings = upstox_listings_by_key()
    bridge = BrokerMarketDataBridge(held_instrument_limit=max(1, len(instrument_keys)))
    for key in instrument_keys:
        listing = listings.get(key)
        if listing is not None:
            bridge.observe_listing(listing)

    merged = []
    for key in instrument_keys:
        index_path = root / key / f"{day}.index"
        blob_path = root / key / f"{day}.blob"
        if not index_path.exists():
            continue
        read = 0
        for record in read_tape_index(index_path):
            payload = _json.loads(read_payload(blob_path, record))
            update = LtpUpdate(
                instrument_key=payload["instrument_key"],
                last_traded_price=payload["last_traded_price"],
                last_traded_quantity=payload.get("last_traded_quantity"),
                last_traded_time_ms=payload["last_traded_time_ms"],
                close_price=payload.get("close_price"),
                broker_time_ns=payload["broker_time_ns"],
            )
            trade = bridge.trade_for(update)
            # A record with no price and no venue time is the shut market's own
            # restatement, not a print: the feed holds its connection through a
            # weekend and Upstox answers with zeroes. Replaying those as trades
            # would put a price of 0.00 in front of every part downstream.
            if trade is not None and trade.price > 0 and trade.venue_time_ns > 0:
                merged.append(trade)
                read += 1
            if read >= trades_per_instrument:
                break
    merged.sort(key=lambda trade: trade.venue_time_ns)
    return merged
