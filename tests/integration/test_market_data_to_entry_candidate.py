"""Real trades off the tape, through the sampler, to one entry candidate.

This is the first test in the project where parts talk to each other. Nothing is
mocked: `price-level-sampler`, `regime-classifier`, `cointegration-pair-finder`
and `spread-reversion-detector` run as their own processes under the launcher, wired
only by what the blueprint says they consume and produce, and what flows into them
is trades this machine actually recorded from Binance and Bybit.

What stands in for `venue-trade-stream-reader` is its own publisher -- the same
`Publisher`, built from the same derived wiring, sending to the same addresses. The
reason it is not the real part is the network: that part opens sockets to a venue,
and a test that needed a live exchange would be a test that fails when a venue has
an outage rather than when this code is wrong.

The replay is paced. An inbox holds 167 messages of this size and a part that is
behind loses the rest -- which is the bus working as designed, and would make this
test measure the buffer instead of the chain.
"""

from __future__ import annotations

import datetime
import os
import pathlib
import shutil
import time

import pytest

from tests.conftest import most_recent_day_the_tape_holds

from runtime.bus import Inbox, Publisher
from runtime.part_launcher import PartLauncher
from runtime.tape import read_payload, read_tape_index
from runtime.venues.adapter_registry import load_venue_adapter
from runtime.wiring_plan import derive_wiring

TAPE_ROOT = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape"
# The venues whose captured prints this test replays. Still the crypto pair: the
# chain under test is venue-agnostic (T-4) and these are the only tapes with the
# trade-by-trade depth it needs. Porting to the Upstox broker tape is real work
# and outstanding -- it is a different tape shape with no VenueAdapter behind it.
CAPTURED_VENUES = ("binance-usdm", "bybit-linear")
THREAD_CEILING = 1
PLACEMENT_DEADLINE_SECONDS = 0.5
PLACEMENT_POLL_SECONDS = 0.002
STOP_DEADLINE_SECONDS = 10.0
RECEIVE_BUFFER_BYTES = 212_992
MAXIMUM_MESSAGE_BYTES = 131_072

# The chain under test, in the order data moves through it.
# The sampler stands between the feed and every part that reads a price level.
# Publishing market-data straight at those parts stopped reaching them on
# 2026-08-24: they read symbol-price-frame now, and the frame is what this
# part makes out of the prints.
SPINE = (
    "price-level-sampler",
    "regime-classifier",
    "cointegration-pair-finder",
    "spread-reversion-detector",
)

# Enough symbols for pairs to exist, few enough that the rotation reaches them all.
SYMBOLS_PER_VENUE = 6
# The parts in this chain fill 256-observation windows, and since 2026-08-24 an
# observation is a sampled level, not a print: the sampler publishes four frames a
# second whatever the replay's print rate, so the windows fill with wall time.
# 256 observations at 4 Hz is 64 seconds, and what sets how long the replay runs
# is how many trades it has to send -- 72,000 trades at 600 a second is 120
# seconds, which fills every window with time to spare for the verdicts to flow.
TRADES_PER_SYMBOL = 6_000
# Paced at roughly twice the rate the tape is actually recording -- measured,
# 285.3 messages a second across 62 symbols on 2026-08-22. Publishing as fast as
# the loop can go measures the kernel instead of the chain: a burst of 4.7 million
# datagrams was refused for 96% of its sends, and the parts then saw 0.4% of the
# market and correctly concluded nothing.
REPLAY_BATCH = 12
REPLAY_PAUSE_SECONDS = 0.02
PATIENCE_SECONDS = 240.0


def busiest_symbols(venue_id: str, day: str, count: int) -> list[str]:
    venue_root = TAPE_ROOT / venue_id
    if not venue_root.is_dir():
        return []
    sized = []
    for symbol_directory in venue_root.iterdir():
        index_path = symbol_directory / f"{day}.index"
        if index_path.exists() and index_path.stat().st_size > 0:
            sized.append((index_path.stat().st_size, symbol_directory.name))
    sized.sort(reverse=True)
    return [symbol for _size, symbol in sized[:count]]


def read_trades_in_time_order(day: str) -> list:
    """Real trades from both venues, in the order the bus would deliver them."""
    merged = []
    for venue_id in CAPTURED_VENUES:
        adapter = load_venue_adapter(venue_id)
        for symbol in busiest_symbols(venue_id, day, SYMBOLS_PER_VENUE):
            index_path = TAPE_ROOT / venue_id / symbol / f"{day}.index"
            blob_path = TAPE_ROOT / venue_id / symbol / f"{day}.blob"
            read = 0
            for record in read_tape_index(index_path):
                for trade in adapter.read_trades(read_payload(blob_path, record)):
                    merged.append(trade)
                    read += 1
                if read >= TRADES_PER_SYMBOL:
                    break
    merged.sort(key=lambda trade: trade.venue_time_ns)
    return merged


@pytest.fixture
def bus_root():
    root = pathlib.Path(os.environ["XDG_RUNTIME_DIR"]) / "vertical-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    root.chmod(0o700)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def launcher(bus_root):
    started = PartLauncher(
        place_in_scope=False,
        thread_ceiling=THREAD_CEILING,
        placement_confirmation_deadline_seconds=PLACEMENT_DEADLINE_SECONDS,
        placement_confirmation_poll_interval_seconds=PLACEMENT_POLL_SECONDS,
        runtime_directory=bus_root,
    )
    yield started
    started.stop_all(STOP_DEADLINE_SECONDS)
    started.close()


def wait_for_address(address: pathlib.Path, patience_seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + patience_seconds
    while time.monotonic() < deadline:
        if address.exists():
            return True
        time.sleep(0.02)
    return False


def price_inbox(wiring, part_id: str) -> pathlib.Path:
    """The address a part hears the market at.

    The sampler is the one part in this chain still fed prints; everything after
    it reads the frame it makes out of them.
    """
    inboxes = wiring[part_id].inboxes
    return inboxes.get("market-data") or inboxes["symbol-price-frame"]


@pytest.fixture(scope="module")
def todays_trades():
    """Real trades from both venues, read once and shared by the tests below."""
    # The latest day the tape actually holds, not today. The crypto spine went
    # inactive on 2026-09-01 with the pivot to Indian markets, so "today" has had
    # no prints since and these tests errored on every run. See
    # most_recent_day_the_tape_holds for why the day may move without weakening
    # what is proved.
    day = most_recent_day_the_tape_holds(CAPTURED_VENUES)
    if day is None:
        pytest.skip(
            "no captured tape for any of "
            f"{CAPTURED_VENUES}; these tests replay real venue prints (RL-063) "
            "and there are none on this machine to replay"
        )
    trades = read_trades_in_time_order(day)
    assert len(trades) > TRADES_PER_SYMBOL, (
        f"the tape holds only {len(trades)} trades for {day}; these tests read what was "
        f"actually recorded today and cannot run without it"
    )
    return trades


def replay_until(feed, trades, drain, stop_when, patience_seconds=PATIENCE_SECONDS,
                 arriving_now=None):
    """Publish real trades at a pace no inbox has to swallow whole, draining as we go.

    `arriving_now` dates each batch as though the market had just printed it. The
    tape read here starts at the beginning of today, so without it every part that
    judges how old a price is refuses the whole replay -- correctly, because those
    prints really are hours old. `spread-reversion-detector` in particular refuses
    a spread whose legs are stale, since a frozen leg is what makes a spread look
    maximally stretched. Only a replay has to say when it is pretending to be
    (RL-071).
    """
    restamp = arriving_now or (lambda batch: batch)
    deadline = time.monotonic() + patience_seconds
    position = 0
    published = 0
    while time.monotonic() < deadline:
        batch = trades[position : position + REPLAY_BATCH]
        position += REPLAY_BATCH
        if position >= len(trades):
            # Loop the tape rather than fall silent. The windows downstream fill
            # with wall time now, not with prints, and a feed that stops publishes
            # no frames -- so a replay shorter than the windows would starve the
            # chain it is testing. Every batch is restamped to now either way.
            position = 0
        feed.publish("market-data", restamp(batch))
        published += len(batch)
        time.sleep(REPLAY_PAUSE_SECONDS)
        drain()
        if stop_when():
            return published
    settle = time.monotonic() + 5.0
    while time.monotonic() < settle and not stop_when():
        drain()
        time.sleep(0.05)
    return published


def open_feed(wiring):
    return Publisher(
        part_id="venue-trade-stream-reader",
        outbound=wiring["venue-trade-stream-reader"].outbound,
        maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
    )


@pytest.mark.slow
def test_real_trades_become_pair_verdicts(launcher, bus_root, todays_trades, arriving_now):
    """The finder judges real symbol pairs, and says which state each is in.

    The verdicts are read at `spread-reversion-detector`'s own address, with that
    part deliberately not started: it is the only consumer of `cointegrated-pair`
    in the blueprint, and binding its inbox while it ran would take its input away.
    """
    wiring = derive_wiring(runtime_directory=bus_root)
    verdicts = Inbox(
        part_id="spread-reversion-detector",
        data_type="cointegrated-pair",
        address=wiring["spread-reversion-detector"].inboxes["cointegrated-pair"],
        receive_buffer_bytes=RECEIVE_BUFFER_BYTES,
    )
    feed = open_feed(wiring)
    seen = []
    try:
        for part_id in ("price-level-sampler", "regime-classifier", "cointegration-pair-finder"):
            launcher.start(part_id)
            assert wait_for_address(price_inbox(wiring, part_id))

        replay_until(
            feed,
            todays_trades,
            drain=lambda: seen.extend(verdicts.drain()),
            stop_when=lambda: any(message.payload.is_tradeable for message in seen),
            arriving_now=arriving_now,
        )
        delivered = feed.standing["market-data"].delivered
    finally:
        verdicts.close()
        feed.close()

    assert delivered > 0, "no trade reached any part"
    assert seen, f"the finder published no verdict after {delivered} trades were delivered"

    states = {}
    for message in seen:
        states[message.payload.state] = states.get(message.payload.state, 0) + 1
    assert all(message.producer_part_id == "cointegration-pair-finder" for message in seen)
    assert any(payload.is_tradeable for payload in (m.payload for m in seen)), (
        f"no pair was cointegrated. States seen: {states}. That is a finding about this "
        f"market or these thresholds, not a broken chain -- and it is the finding that "
        f"decides whether the first paper fill can happen at all."
    )


@pytest.mark.slow
def test_a_tradeable_pair_becomes_an_entry_candidate(
    launcher, bus_root, todays_trades, arriving_now
):
    wiring = derive_wiring(runtime_directory=bus_root)

    # Where an entry candidate would land. A real consumer of the type, bound here
    # rather than started, because what is under test ends at the candidate.
    candidate_reader = next(
        part_id
        for part_id, part in sorted(wiring.items())
        if "entry-candidate" in part.inboxes and part_id not in SPINE
    )
    candidates = Inbox(
        part_id=candidate_reader,
        data_type="entry-candidate",
        address=wiring[candidate_reader].inboxes["entry-candidate"],
        receive_buffer_bytes=RECEIVE_BUFFER_BYTES,
    )
    feed = open_feed(wiring)
    seen_candidates = []
    try:
        for part_id in SPINE:
            launcher.start(part_id)
            assert wait_for_address(price_inbox(wiring, part_id)), (
                f"{part_id} never bound the inbox it hears the market at"
            )

        replay_until(
            feed,
            todays_trades,
            drain=lambda: seen_candidates.extend(candidates.drain()),
            stop_when=lambda: bool(seen_candidates),
            arriving_now=arriving_now,
        )
        delivered = feed.standing["market-data"].delivered
    finally:
        candidates.close()
        feed.close()

    assert delivered > 0, "no trade reached any part"
    assert seen_candidates, (
        f"no entry candidate after {delivered} trades reached three running parts. "
        f"Whether that is the market, the thresholds, or the chain is answered by "
        f"test_real_trades_become_pair_verdicts, which reads the step before this one."
    )

    candidate = seen_candidates[0].payload
    assert candidate.detector == "spread-reversion-detector"
    assert candidate.venue_id in set(CAPTURED_VENUES)
    assert candidate.signal_strength != 0
    assert seen_candidates[0].producer_part_id == "spread-reversion-detector"
