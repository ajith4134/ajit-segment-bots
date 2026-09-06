"""Real NSE option prints, through the sampler, to one entry candidate.

This is the first test in the project where parts talk to each other. Nothing is
mocked: `price-level-sampler`, `regime-classifier`, `cointegration-pair-finder`
and `spread-reversion-detector` run as their own processes under the launcher, wired
only by what the blueprint says they consume and produce, and what flows into them
is prints this machine actually recorded from **Upstox** -- one real NIFTY option
chain, which is what the index-options segment bot actually trades.

Ported off the Binance/Bybit tape on 2026-09-06. The reason it had stayed on the
crypto pair was stated here as *"a different tape shape with no VenueAdapter
behind it"*, and that is no longer true: `tests/conftest.upstox_trades_for` runs
the captured Upstox prints through `broker-market-data-bridge` -- the part the
live spine itself uses -- so what arrives is `market-data`, the same type
`price-level-sampler` consumed before. The chain under test is unchanged; only
the market it is fed is. That matters because the two temporary goals in
CLAUDE.md ask for exactly this substitution, and because a chain proven only on
perpetuals had never been shown to raise a candidate on an Indian instrument.

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

from tests.conftest import (
    busiest_upstox_option_chain, most_recent_upstox_trading_day, upstox_trades_for,
)

from runtime.bus import Inbox, Publisher
from runtime.part_launcher import PartLauncher
from runtime.wiring_plan import derive_wiring

# The venue whose captured prints this test replays. The chain under test is
# venue-agnostic (T-4), so this is the one line that decides which market proves
# it -- and it is the market the segment bots actually trade.
CAPTURED_VENUE = "upstox"
CAPTURED_VENUES = (CAPTURED_VENUE,)
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

# Enough contracts for pairs to exist, few enough that the rotation reaches them
# all. Deliberately ONE underlying's chain rather than the six busiest contracts
# outright: measured on the captured tape of 2026-09-04, the six busiest NSE_FO
# contracts spanned four unrelated underlyings and produced **0 cointegrated
# pairs**, while the six busiest NIFTY contracts produced **6 tradeable ones**.
# Two options on different underlyings have no reason to move together, so the
# scanner correctly finds nothing and the test reads as a broken chain.
CONTRACTS_IN_THE_CHAIN = 6
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


def read_trades_in_time_order(day: str) -> list:
    """One real NIFTY chain's prints, in the order the bus would deliver them.

    `upstox_trades_for` puts them through `broker-market-data-bridge` rather than
    building `NormalisedTrade` here, so what this test replays is what the running
    system would actually have seen -- including the 2026-09-06 correction that
    Upstox states no size on about three quarters of its LTP updates.
    """
    chain = busiest_upstox_option_chain(day, CONTRACTS_IN_THE_CHAIN)
    if not chain:
        return []
    return upstox_trades_for(day, chain, TRADES_PER_SYMBOL)


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
    """One real NIFTY chain's prints, read once and shared by the tests below."""
    # The latest day the tape holds enough real prints on to replay, not today:
    # a market that was shut yesterday is not a broken test. The day is allowed
    # to move because what is proved does not depend on which session it was --
    # see most_recent_upstox_trading_day.
    day = most_recent_upstox_trading_day(TRADES_PER_SYMBOL, instruments=CONTRACTS_IN_THE_CHAIN)
    if day is None:
        pytest.skip(
            f"no captured Upstox tape holds {TRADES_PER_SYMBOL} prints on "
            f"{CONTRACTS_IN_THE_CHAIN} contracts; these tests replay real broker "
            "prints (RL-063) and there are none on this machine to replay"
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
    assert candidate.venue_id == CAPTURED_VENUE
    assert candidate.signal_strength != 0
    assert seen_candidates[0].producer_part_id == "spread-reversion-detector"
