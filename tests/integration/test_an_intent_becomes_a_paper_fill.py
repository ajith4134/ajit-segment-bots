"""An intent, fourteen processes, and a simulated fill on a real price.

This is the back half of the vertical: everything from a decision having been made
to a fill being recorded. It starts at `trade-intent` rather than at market data
because the front half is proven separately and cannot reach here until the
conviction model has been trained on live signals -- which takes market time, not
test time.

Nothing about the money is pretended. The prices are real trades this machine
captured, the fee comes from the venues' published schedule, the balance is the
operator's paper allotment, and `money-mode-reader` reading anything other than
exactly 'paper' would stop the order at the router. What is simulated is the fill,
which is the one thing paper trading means.

The assertions worth reading are the last two: the fill is addressed to the paper
book, and the journal recorded the trade's stages in order. A run that filled
against a live venue, or filled without leaving a record, would be the failure this
phase cannot recover from.
"""

from __future__ import annotations

import datetime

import dataclasses
import json
import os
import pathlib
import shutil
import time

import pytest

from parts.market_data_feed.symbol_catalogue_reader import (
    CAPTURE_EVERY_SYMBOL,
    QUOTE_VOLUME_24H,
    select_capturable_symbols,
)
from parts.ledger.trade_lifecycle_recorder import LIFECYCLE_STAGES
from runtime.bus import Inbox, Publisher
from runtime.learned_estimator import Estimate
from runtime.market_conditions import MarketSessionState, SessionKind
from runtime.part_launcher import PartLauncher
from runtime.trade_intent import OPEN, SOLE_OPINION, TradeIntent
from runtime.trading_types import BUY
from runtime.venues.adapter_registry import load_venue_adapter
from runtime.wiring_plan import derive_wiring

THREAD_CEILING = 1
PLACEMENT_DEADLINE_SECONDS = 0.5
PLACEMENT_POLL_SECONDS = 0.002
STOP_DEADLINE_SECONDS = 10.0
RECEIVE_BUFFER_BYTES = 212_992
MAXIMUM_MESSAGE_BYTES = 131_072
PATIENCE_SECONDS = 120.0
# How long the feed and the catalogue run before the first intent is published.
# Fourteen processes each have to have read their inputs at least once, and every
# one of them ticks on its own clock.
WARM_UP_SECONDS = 5.0
PUBLISH_INTERVAL_SECONDS = 0.25
# How long the record is given to catch up with the fill it records.
RECORD_SETTLE_SECONDS = 20.0

# A trading session, stated rather than measured, because the calendar that
# measures one reads NSE over the network. `segment` is what the calendar would
# have published, not what the order names -- paper-fill-simulator asks only
# whether a session is tradeable, and cannot match a bot segment to an exchange
# one because no mapping between them exists (see its own read_orders).
def an_open_session_now() -> MarketSessionState:
    """Built fresh at each publish, for the same reason `arriving_now` restamps.

    A session is a level and its reader bounds the age of one
    (`market_condition_level_maximum_age_seconds`, 180s), so a session stamped
    once is expired by the time the run needs it and reads as no session at all
    -- which does not fill. Stamped at epoch it is expired on arrival, which is
    how the first version of this fixture reproduced the exact failure it was
    written to remove.
    """
    return MarketSessionState(
        segment="FO",
        kind=SessionKind.OPEN,
        as_of_date=datetime.date(2026, 9, 4),
        reason="stated by the test; the calendar reads NSE and a test may not",
        observed_at_ns=time.time_ns(),
    )

VENUE = "binance-usdm"
SYMBOL = "BTCUSDT"
# Which segment this run trades. The captured prices are a perpetual's, so the
# segment claiming perpetuals is the one this chain belongs to -- and it is
# stated in this run's own copied settings rather than borrowed from the
# operator's, whose three segments are Indian and claim no perpetual at all.
TRADED_SEGMENT = "futures"

# The stop the bot's exit plan would have proposed, as a fraction below entry. Half
# a percent is inside the measured range this symbol moves in a minute, so the
# resulting size is one the account can carry rather than one the bounds must cut
# to nothing.
STOP_BELOW_ENTRY = 0.005

# Everything that must be on for an intent to become a fill. Ordered so a part is
# started after the parts it reads from.
TRADING_HALF = (
    # instrument-selector reads a price level rather than every print since
    # 2026-08-24, and the sampler is what makes one. paper-fill-simulator still
    # reads market-data directly and deliberately: a resting stop triggers on the
    # market touching it, and a wick between frames must not be a wick that never
    # happened.
    "price-level-sampler",
    "main-account-settings-reader",
    "capital-allotment-reader",
    "capital-settings-validator",
    "money-mode-reader",
    "instrument-selector",
    "tick-size-resolver",
    "exposure-limiter",
    "paper-account-keeper",
    "position-sizer",
    "trade-capital-bounds-gate",
    "order-idempotency-stamper",
    "order-destination-router",
    "paper-fill-simulator",
    "trade-lifecycle-recorder",
)


@dataclasses.dataclass(frozen=True)
class SettingsForThisRun:
    """Where this run's copy of the operator's settings is, and what it writes to."""

    directory: pathlib.Path
    journal_path: pathlib.Path


@pytest.fixture
def bus_root():
    root = pathlib.Path(os.environ["XDG_RUNTIME_DIR"]) / "fill-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    root.chmod(0o700)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def isolated_settings(durable_tmp_path):
    """The operator's settings, copied, with the journal pointed at this run.

    Fourteen real parts read the operator's real settings here, and one of them
    writes the ledger. Left alone this test appends its paper fills to the
    journal the running system keeps -- test decisions inside the record of what
    the system actually decided, which is the one thing that cannot be
    re-derived from the tape. So the settings are copied, `journal_path` is
    rewritten to this run's own file, and the launcher is told which directory to
    start its parts against. Everything else is the operator's file byte for
    byte: the fees, the balance and the risk limits are what this test is meant
    to run on.

    Told to the launcher rather than set in the environment: every part is forked
    from one long-lived forkserver, so an environment variable set here reaches
    the parts of whichever test happened to start that server first and outlives
    this test's own directory.
    """
    from runtime.settings_reader import settings_directory

    config_home = durable_tmp_path / "config"
    settings_root = config_home / "ajit-segment-bots" / "settings"
    shutil.copytree(settings_directory(), settings_root)

    journal_path = durable_tmp_path / "journal.jsonl"
    runtime_settings = settings_root / "runtime.toml"
    lines = runtime_settings.read_text().splitlines()
    in_journal_setting = False
    in_built_segments = False
    rewritten = 0
    segments_rewritten = 0
    for index, line in enumerate(lines):
        if line.strip() == "[journal_path]":
            in_journal_setting = True
            in_built_segments = False
        elif line.strip() == "[built_segments]":
            in_built_segments = True
            in_journal_setting = False
        elif line.startswith("["):
            in_journal_setting = False
            in_built_segments = False
        elif in_journal_setting and line.startswith("value"):
            lines[index] = f'value = "{journal_path}"'
            rewritten += 1
        elif in_built_segments and line.startswith("value"):
            # The segment this run trades, stated rather than inherited. The
            # instrument below is a binance-usdm perpetual and the operator's own
            # three segments are Indian, so on their settings this chain refuses
            # every intent correctly -- `instrument-selector` reports the best
            # instrument as belonging to a segment that is not built, and nothing
            # downstream ever sees an order. That refusal is the machinery
            # working (2026-09-05): which segment an instrument belongs to is a
            # fact about the operator's settings now, not a table in the code.
            lines[index] = f'value = ["{TRADED_SEGMENT}"]'
            segments_rewritten += 1
    assert rewritten == 1, (
        f"journal_path was rewritten {rewritten} times in the copied settings; this test "
        f"must not be able to write into the operator's own ledger"
    )
    assert segments_rewritten == 1, (
        f"built_segments was rewritten {segments_rewritten} times; without it this run "
        f"trades the operator's Indian segments and its own perpetual belongs to none "
        f"of them"
    )
    runtime_settings.write_text("\n".join(lines) + "\n")

    # What that segment claims. Written here rather than assumed of the
    # operator's own file: the pair (instrument kind, underlying) is what
    # identifies a segment, and a segment claiming neither claims nothing.
    segment_file = settings_root / "segments" / f"{TRADED_SEGMENT}.toml"
    segment_file.write_text(
        segment_file.read_text().rstrip()
        + "\n\n[segment_instrument_types]\n"
        + 'value = ["perpetual-future", "dated-future"]\n'
        + 'unit  = "instrument type"\n'
        + 'note  = "this test\'s own segment: what it trades"\n\n'
        + "[segment_underlying_trading_symbols]\n"
        + f'value = ["{SYMBOL}"]\n'
        + 'unit  = "trading symbol"\n'
        + 'note  = "this test\'s own segment: the symbol its captured prices are for"\n'
    )

    return SettingsForThisRun(directory=settings_root, journal_path=journal_path)


@pytest.fixture
def launcher(bus_root, isolated_settings):
    started = PartLauncher(
        place_in_scope=False,
        thread_ceiling=THREAD_CEILING,
        placement_confirmation_deadline_seconds=PLACEMENT_DEADLINE_SECONDS,
        placement_confirmation_poll_interval_seconds=PLACEMENT_POLL_SECONDS,
        runtime_directory=bus_root,
        settings_directory=isolated_settings.directory,
    )
    yield started
    started.stop_all(STOP_DEADLINE_SECONDS)
    started.close()


@pytest.fixture
def real_prices(read_captured_payloads):
    adapter = load_venue_adapter(VENUE)
    records = read_captured_payloads(VENUE, "2026-08-22-btcusdt-aggtrade-run.jsonl")
    trades = [trade for _at_ns, payload in records for trade in adapter.read_trades(payload)]
    assert trades, "the captured run holds no trades"
    return trades


@pytest.fixture
def captured_universe(read_captured_json):
    """The symbols the venue listed, on the terms it listed them on.

    Built the way `symbol-catalogue-reader` builds it, out of the same responses
    the venue returned on 2026-08-22: the contract list, the tickers, and the two
    endpoints this venue splits funding across. Two parts in this run read it and
    neither can act without its own figure -- `tick-size-resolver` needs the price
    increment, and `instrument-selector` needs the funding rate and the settlement
    interval to price what holding the contract costs.
    """
    adapter = load_venue_adapter(VENUE)
    catalogue = read_captured_json(VENUE, "2026-08-22-catalogue-subset.json")
    tickers = read_captured_json(VENUE, "2026-08-22-ticker-24h-subset.json")
    listings = adapter.read_symbol_listings(catalogue)
    funding = adapter.read_funding_facts(
        listings,
        tickers,
        [
            read_captured_json(VENUE, "2026-08-22-funding-premiumIndex-subset.json"),
            read_captured_json(VENUE, "2026-08-22-funding-fundingInfo-subset.json"),
        ],
    )
    universe = select_capturable_symbols(
        adapter=adapter,
        listings=listings,
        quote_volumes=dict(adapter.read_quote_volumes(tickers)),
        captured_symbol_count=CAPTURE_EVERY_SYMBOL,
        selection_metric=QUOTE_VOLUME_24H,
        funding=funding,
    )
    traded = next((entry for entry in universe if entry.symbol == SYMBOL), None)
    assert traded is not None, f"the captured catalogue does not list {SYMBOL} as capturable"
    assert traded.price_increment, f"the captured catalogue declares no price increment for {SYMBOL}"
    assert traded.funding_rate_per_settlement is not None, (
        f"the captured responses declare no funding rate for {SYMBOL}, and a perpetual whose "
        f"carry cannot be priced is refused by the selector rather than assumed free"
    )
    assert traded.funding_settlements_per_day, (
        f"the captured responses declare no settlement interval for {SYMBOL}"
    )
    return list(universe)


def wait_for_address(address: pathlib.Path, patience_seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + patience_seconds
    while time.monotonic() < deadline:
        if address.exists():
            return True
        time.sleep(0.02)
    return False


def an_intent(entry_price: float) -> TradeIntent:
    """What the arbiter publishes when the bull bot is the only voice in the room."""
    return TradeIntent(
        venue_id=VENUE,
        symbol=SYMBOL,
        side=BUY,
        action=OPEN,
        conviction=Estimate(
            value=0.62,
            is_fitted=True,
            observations=140,
            prior=0.5,
            was_clamped=False,
            bound_low=None,
            bound_high=None,
            reason="140 labelled outcomes",
        ),
        horizon_seconds=60.0,
        stop_price=entry_price * (1 - STOP_BELOW_ENTRY),
        agreement=SOLE_OPINION,
        contributing_bots=("bull-bot",),
        dissenting_bots=(),
        opinion_weights={"bull-bot": 1.0},
        evidence={"spread_z": 2.4},
        reason="the spread is stretched and the model has been trained",
        formed_at_ns=time.time_ns(),
    )


@pytest.mark.slow
def test_an_intent_becomes_a_paper_fill(
    launcher, bus_root, real_prices, captured_universe, isolated_settings, arriving_now
):
    wiring = derive_wiring(runtime_directory=bus_root)

    def watch(owner: str, data_type: str) -> Inbox:
        return Inbox(
            part_id=owner,
            data_type=data_type,
            address=wiring[owner].inboxes[data_type],
            receive_buffer_bytes=RECEIVE_BUFFER_BYTES,
        )

    # sized-order and stamped-order each have exactly one consumer in the
    # blueprint, and both are running -- an observer would have to take a running
    # part's input away to see them. What the sizer did is visible in what the gate
    # published, which is the next thing along.
    watched = {
        "bounded-order": watch("fund-lock-ledger", "bounded-order"),
        "order-request": watch("order-state-poller", "order-request"),
        "fill": watch("position-close-detector", "fill"),
        "journal-entry": watch("journal-integrity-checker", "journal-entry"),
        # The book counts every branch it takes and says so on its health. Watched
        # here so a run that produces no fill can say which refusal produced none,
        # instead of only that none happened. On 2026-09-04 the live spine routed
        # 325 orders and filled nothing, and the reason existed as a counter the
        # whole time with nothing reading it -- this is that lesson applied to the
        # test that was supposed to catch it.
        "part-health": watch("failing-part-detector", "part-health"),
    }
    seen = {data_type: [] for data_type in watched}

    feed = Publisher(
        part_id="venue-trade-stream-reader",
        outbound=wiring["venue-trade-stream-reader"].outbound,
        maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
    )
    catalogue = Publisher(
        part_id="symbol-catalogue-reader",
        outbound=wiring["symbol-catalogue-reader"].outbound,
        maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
    )
    brain = Publisher(
        part_id="opinion-arbiter",
        outbound=wiring["opinion-arbiter"].outbound,
        maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
    )
    # The paper book fills nothing outside a trading session (2026-09-02), and
    # market-session-calendar is the only producer of that level. It is not in
    # TRADING_HALF and must not be: it reads NSE's holiday master over the
    # network, which a test may not depend on. So the session is stated here,
    # the way test_an_options_position_opens_and_closes states its own.
    #
    # Without it this test failed with bounded-order 24, order-request 24, fill 0
    # -- the whole chain working and the book refusing every order for a session
    # nobody had measured. That is the correct refusal on the part's side and a
    # missing input on the test's, and it hid a real defect for a day: the same
    # zero-fill shape was happening on the live spine for an entirely different
    # reason (the governor shedding the decision chain), and a red test that was
    # red for its own reason is a test nobody can read an outage from.
    calendar = Publisher(
        part_id="market-session-calendar",
        outbound=wiring["market-session-calendar"].outbound,
        maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
    )

    try:
        for part_id in TRADING_HALF:
            launcher.start(part_id)
        for part_id in TRADING_HALF:
            inboxes = wiring[part_id].inboxes
            if inboxes:
                assert wait_for_address(next(iter(inboxes.values()))), f"{part_id} never bound"

        entry_price = real_prices[-1].price

        # The feed and the catalogue run before any decision is made, and every
        # part in this chain holds what it last read as a level: the selector
        # needs the listing and a price, the sizer needs the risk limit, the gate
        # needs the settings verdict. So the world is served for a warm-up before
        # the intent arrives, which is the order these things happen in on a
        # running system -- an intent published into a cold circuit is consumed
        # once by whichever part was ready and is gone.
        warm_up_ends = time.monotonic() + WARM_UP_SECONDS
        while time.monotonic() < warm_up_ends:
            catalogue.publish("symbol-universe", captured_universe)
            calendar.publish("market-session-state", (an_open_session_now(),))
            feed.publish("market-data", arriving_now(real_prices[-40:]))
            time.sleep(PUBLISH_INTERVAL_SECONDS)

        # The intent is republished while it stands, which is what the arbiter
        # does: an opinion that is still held is still published, tick after
        # tick. Published once, an intent is consumed once by each part that
        # reads it -- and the sizer, which needs the selector's answer to the
        # same intent, drops it if the selector has not answered yet. That
        # produced a fill on some runs and silence on others.
        #
        # Repeating it is safe because it is designed to be: the same decision
        # produces the same bounded order, `order-idempotency-stamper` derives
        # the same client id from it, and the paper book fills one order per id.
        # A run that ended with two fills would be that chain broken.
        intent = an_intent(entry_price)
        deadline = time.monotonic() + PATIENCE_SECONDS
        while time.monotonic() < deadline and not seen["fill"]:
            catalogue.publish("symbol-universe", captured_universe)
            calendar.publish("market-session-state", (an_open_session_now(),))
            feed.publish("market-data", arriving_now(real_prices[-40:]))
            brain.publish("trade-intent", [intent])
            time.sleep(PUBLISH_INTERVAL_SECONDS)
            for data_type, inbox in watched.items():
                seen[data_type].extend(inbox.drain())
            time.sleep(PUBLISH_INTERVAL_SECONDS)

        # The record is written after the fill, by a part that reads the fill --
        # so the run keeps draining until the journal has caught up, rather than
        # stopping the moment the fill appeared and asking why nothing recorded
        # it. Bounded: a record that never arrives is the failure this waits to
        # be able to state.
        settled_by = time.monotonic() + RECORD_SETTLE_SECONDS
        while time.monotonic() < settled_by and not any(
            message.payload.kind == "fill" for message in seen["journal-entry"]
        ):
            calendar.publish("market-session-state", (an_open_session_now(),))
            feed.publish("market-data", arriving_now(real_prices[-40:]))
            time.sleep(PUBLISH_INTERVAL_SECONDS)
            for data_type, inbox in watched.items():
                seen[data_type].extend(inbox.drain())

        counted = {data_type: len(messages) for data_type, messages in seen.items()}
        still_running = [part_id for part_id in TRADING_HALF if launcher.is_running(part_id)]
    finally:
        for inbox in watched.values():
            inbox.close()
        feed.close()
        catalogue.close()
        brain.close()
        calendar.close()

    # Each recorder writes its own file beside the base the settings name, because
    # a chain is a property of one writer and two recorders appending to one path
    # interleave into no chain at all.
    from runtime.journal import journal_path_for

    recorded_to = journal_path_for(isolated_settings.journal_path, "trade-lifecycle-recorder")
    assert recorded_to.exists(), (
        f"nothing was journalled to {recorded_to}; if the parts wrote a ledger at all they "
        f"wrote the operator's, which this test must never touch"
    )

    assert still_running == list(TRADING_HALF), (
        f"parts died: {sorted(set(TRADING_HALF) - set(still_running))}"
    )
    # What the book itself said it was doing, from its own standing counters.
    book_said = {}
    for message in seen["part-health"]:
        if getattr(message.payload, "part_id", None) == "paper-fill-simulator":
            book_said = {name: value for name, value in message.payload.standing if value}
    counted["the paper book's own counters"] = book_said

    assert counted["bounded-order"] > 0, f"the bounds gate produced nothing: {counted}"
    assert counted["order-request"] > 0, f"the router produced nothing: {counted}"
    assert counted["fill"] > 0, f"no fill: {counted}"

    fill = seen["fill"][0].payload
    assert fill.venue_id == VENUE
    assert fill.symbol == SYMBOL
    assert fill.side == BUY
    assert fill.quantity > 0
    assert fill.price > 0
    assert fill.fee > 0, "a fill with no fee is a fill that cost nothing, which no venue offers"

    request = seen["order-request"][0].payload
    assert request.destination == "paper-book", (
        f"the order was addressed to {request.destination!r}; paper is the only destination "
        f"this phase may reach (RL-005)"
    )
    assert request.is_live_money is False

    # The intent stood for the whole run and was republished on every tick, the
    # way an arbiter publishes an opinion it still holds. One decision is one
    # order: one client id, and no more filled than the order asked for.
    assert len({message.payload.order_id for message in seen["fill"]}) == 1, (
        "one decision produced more than one order id, so the same trade was opened twice"
    )
    assert sum(message.payload.quantity for message in seen["fill"]) <= request.quantity + 1e-9, (
        "the paper book filled more than the order asked for"
    )

    # A journal entry is a JournalEntry, not a dict: `kind` is the stage and the
    # dict beside it is what that stage carried.
    stages = [message.payload.kind for message in seen["journal-entry"]]
    assert stages, "the trade was filled and nothing was journalled"
    assert "fill" in stages, f"the fill itself was not journalled: {stages}"

    # Ordering is per trade, not across the journal. A standing intent is
    # republished every tick and each tick's stages are real events, so the
    # journal interleaves them -- and a trade carries the symbol as its id until
    # the stamper gives it a client order id, so one decision appears under two.
    # What the recorder guarantees, and what a reconstruction depends on, is that
    # within one id no stage ever arrives behind one already recorded.
    by_trade: dict[str, list[str]] = {}
    for message in seen["journal-entry"]:
        by_trade.setdefault(message.payload.payload["trade_id"], []).append(message.payload.kind)
    for trade_id, recorded in by_trade.items():
        positions = [LIFECYCLE_STAGES.index(stage) for stage in recorded]
        assert positions == sorted(positions), (
            f"{trade_id} was recorded out of order: {recorded}"
        )

    filled = [message.payload for message in seen["journal-entry"] if message.payload.kind == "fill"]
    assert all(entry.payload["trade_id"] for entry in filled), (
        "a journalled fill with no trade id cannot be tied back to the decision that made it"
    )
    assert all(entry.digest and entry.previous_digest for entry in filled), (
        "the journal is a chain, and an entry with no digest is a link that proves nothing"
    )
