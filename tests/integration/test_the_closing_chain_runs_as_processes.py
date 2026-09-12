"""Nine processes, a paper fill, and a position that closes across the bus.

`test_a_paper_position_opens_and_closes` proves the decisions are right by wiring
the engines together in one process. This proves the **wiring** is right: the same
chain, each part forked by the launcher exactly as the live spine forks it, every
message crossing a real process boundary, and nothing reaching across.

It exists because the failure it catches is invisible to the other test. Ten parts
had correct code and no `start_part`, and each `start_part` binds a part's inputs by
the types it declares -- a binding that reads the wrong field, unpacks a payload the
wrong way, or publishes a shape the next part cannot read produces silence, and
silence looks exactly like a market that has not moved.

**The prices are today's tape** (RL-063), replayed here rather than taken live so
that a venue outage fails as a venue outage rather than as this code being wrong.
The run being tested is the live one.

**The operator's own state is not touched.** The settings are copied and both the
journal and the learned-state directory are pointed at this run's own files: a test
that appended to the real ledger would put test decisions inside the record of what
the system actually decided.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import shutil
import time

import pytest

from tests.conftest import (
    busiest_upstox_instruments, most_recent_upstox_trading_day, upstox_trades_for,
)

from runtime.bus import Inbox, Publisher
from runtime.market_conditions import MarketSessionState, SessionKind
from runtime.part_launcher import PartLauncher
from runtime.trading_types import (
    BUY,
    MARKET,
    PAPER_BOOK,
    ROUTED,
    SELL,
    Fill,
    OrderRequest,
)
from runtime.wiring_plan import derive_wiring

# Ported to the Upstox tape on 2026-09-06. It replayed binance-usdm and
# bybit-linear until then, with a comment saying the port was "real work and
# outstanding" -- and the crypto spine has been inactive since 2026-09-01, so
# the prints it replayed were a frozen artefact of a retired venue. The
# temporary goal of 2026-09-05 says convert or replace every crypto-shaped part
# with the Indian-market equivalent, and a test is a part of the system too:
# one that can only be exercised by a venue this project no longer trades
# proves nothing about the bots that exist.
#
# `tests/conftest.upstox_trades_for` runs the captured Upstox prints through
# `broker-market-data-bridge` -- the part the live spine uses -- so what is
# replayed here is what the running system would actually have seen.
THREAD_CEILING = 1
PLACEMENT_DEADLINE_SECONDS = 0.5
PLACEMENT_POLL_SECONDS = 0.002
STOP_DEADLINE_SECONDS = 10.0
RECEIVE_BUFFER_BYTES = 212_992
MAXIMUM_MESSAGE_BYTES = 131_072
PATIENCE_SECONDS = 180.0
REPLAY_BATCH = 12
REPLAY_PAUSE_SECONDS = 0.02
TRADES_PER_SYMBOL = 4_000

# The chain that closes a position, in the order the live spine starts it.
CLOSING_CHAIN = (
    "money-mode-reader",
    "paper-fill-simulator",
    # The book does not fill an order until this part says the simulated round
    # trip has elapsed. It is in the chain because it is in the live spine, and
    # because leaving it out hid a crash: the fill simulator read
    # delayed-order-request as though it were an order-request and died on
    # `may_be_sent` 17 times in 50 minutes on 2026-08-26.
    "order-latency-simulator",
    "fill-reconciler",
    "cost-basis-tracker",
    "peak-excursion-tracker",
    "exit-order-chainer",
    "stop-order-manager",
    "position-close-detector",
    "inr-pnl-accountant",
)

# How much of the symbol's own movement the exits are placed inside. Not a round
# fraction: the exits must sit within the range the replayed run actually covers,
# or the test proves only that nothing happened. Halved so both sit inside it.
EXIT_INSIDE_THE_RUN_S_RANGE = 0.5


# How many of the day's busiest symbols to read before choosing one to replay.
# The choice needs movement, not volume, and movement can only be seen by reading;
# six keeps the read bounded while giving the choice something to choose between.
SYMBOLS_CONSIDERED = 6

# Whose money this run spends. The NIFTY contracts it replays are index options,
# and the segment has to be named: money-mode is published one per segment since
# three bots began sharing one spine, and an order that names none is refused.
SEGMENT_TRADED = "index-options"

# The smallest rise, as a fraction of the first print, this test will accept from
# the contract it replays. The exits sit at half of the run's own range, so one
# that moved less than this leaves a target inside the tick size and the test
# proves only that nothing happened -- which is how it failed on 2026-08-26, on
# the crypto perpetual SKHYNIXUSDT, whose whole replay rose 0.0025% (1214.16 to
# 1214.19). An option has far more room: measured on the captured Upstox tape of
# 2026-09-04, the five busiest NIFTY puts rose between 16.3% and 29.7% across
# 4,000 prints, so this bound is comfortable rather than marginal here.
SMALLEST_USABLE_RISE = 0.001


def busiest_contracts_today() -> tuple[tuple[str, ...], str]:
    """The instrument keys with most of a real session on the tape, busiest first.

    The newest day the Upstox tape holds enough prints on, which is not simply
    the newest day: the market is shut at weekends and the feed keeps its
    connection open, so a Sunday holds a handful of records restating Friday's
    last price.
    """
    day = most_recent_upstox_trading_day(minimum_prints=TRADES_PER_SYMBOL,
                                         instruments=SYMBOLS_CONSIDERED)
    if day is None:
        pytest.skip(
            "no captured Upstox day holds enough prints to replay; this test "
            "replays real broker prints (RL-063) and there are none on this "
            "machine with a session's worth of depth"
        )
    return tuple(busiest_upstox_instruments(day, SYMBOLS_CONSIDERED)), day


def todays_trades_of(instrument_key: str, day: str) -> list:
    return upstox_trades_for(day, [instrument_key], TRADES_PER_SYMBOL)


def book_counters(health_messages) -> str:
    """paper-fill-simulator's own standing, from the health it published.

    "Nothing filled" is not a diagnosis: the book counts *why* separately --
    refused for no price, held in flight, resting because the market is not
    open -- and those counters are the difference between a broken chain and a
    correctly cautious one. They ride on health, which this test already
    watches, so reading them costs nothing and guessing costs a run.
    """
    latest = None
    for message in health_messages:
        payload = message.payload
        if getattr(payload, "part_id", None) == "paper-fill-simulator":
            latest = payload
    if latest is None:
        return "paper-fill-simulator published no health at all."
    standing = dict(getattr(latest, "standing", ()) or ())
    moved = {name: value for name, value in standing.items() if value}
    return f"paper-fill-simulator standing: {moved or 'every counter at zero'}."


def rise_within(trades: list) -> float:
    """How far the run rises above its own first trade, as a fraction of it.

    This is the quantity the test actually depends on: the target is placed at a
    fraction of it, and a target the replay never reaches asserts nothing.
    """
    if len(trades) < 2 or trades[0].price <= 0:
        return 0.0
    return (max(trade.price for trade in trades[1:]) - trades[0].price) / trades[0].price


@pytest.fixture(scope="module")
def real_trades_of_one_symbol():
    """Today's trades in one symbol that actually moved, from this machine's tape.

    Busiest-by-bytes was the choice until 2026-08-26 and it is the wrong one: the
    symbol with the most prints is often the one being marked in ticks, and this
    test needs a symbol whose replay crosses a target after the exits are on the
    book. So the busiest few are read and the one that rose most is replayed.
    """
    candidates, day = busiest_contracts_today()
    assert candidates, (
        f"no Upstox tape for {day}. This test replays what the broker actually sent, so "
        f"there is no fixture to fall back on (RL-063) -- start the spine and try again."
    )
    considered = []
    for instrument_key in candidates[:SYMBOLS_CONSIDERED]:
        trades = todays_trades_of(instrument_key, day)
        if len(trades) >= 500:
            considered.append((rise_within(trades), trades[0].symbol, trades))
    assert considered, (
        f"none of the {SYMBOLS_CONSIDERED} busiest Upstox contracts has 500 prints on "
        f"the tape for {day}"
    )
    considered.sort(key=lambda entry: entry[0], reverse=True)
    rise, symbol, trades = considered[0]
    assert rise >= SMALLEST_USABLE_RISE, (
        f"the best of {day}'s {len(considered)} busiest contracts is {symbol}, which rose "
        f"{rise:.4%} across {len(trades):,} prints -- below the {SMALLEST_USABLE_RISE:.2%} this "
        f"test needs for a target to be reachable. Nothing is wrong with the code: the market "
        f"did not move enough that session for this replay to prove anything"
    )
    return trades


@pytest.fixture
def bus_root():
    root = pathlib.Path(os.environ["XDG_RUNTIME_DIR"]) / "closing-chain-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    root.chmod(0o700)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def isolated_settings(durable_tmp_path):
    """The operator's settings, copied, with everything this run writes redirected."""
    from runtime.settings_reader import settings_directory

    settings_root = durable_tmp_path / "config" / "ajit-segment-bots" / "settings"
    shutil.copytree(settings_directory(), settings_root)

    redirected = {
        "journal_path": durable_tmp_path / "journal.jsonl",
        "learned_state_root": durable_tmp_path / "learned",
        "position_state_root": durable_tmp_path / "positions",
    }
    (durable_tmp_path / "learned").mkdir(parents=True, exist_ok=True)
    (durable_tmp_path / "positions").mkdir(parents=True, exist_ok=True)

    runtime_settings = settings_root / "runtime.toml"
    lines = runtime_settings.read_text().splitlines()
    inside = None
    rewritten = {name: 0 for name in redirected}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            inside = stripped[1:-1]
        elif inside in redirected and line.startswith("value"):
            lines[index] = f'value = "{redirected[inside]}"'
            rewritten[inside] += 1
    assert all(count == 1 for count in rewritten.values()), (
        f"{rewritten} -- this test must not be able to write into the operator's own ledger "
        f"or over what the running bot has learned"
    )
    runtime_settings.write_text("\n".join(lines) + "\n")
    return settings_root


@pytest.fixture
def launcher(bus_root, isolated_settings):
    started = PartLauncher(
        place_in_scope=False,
        thread_ceiling=THREAD_CEILING,
        placement_confirmation_deadline_seconds=PLACEMENT_DEADLINE_SECONDS,
        placement_confirmation_poll_interval_seconds=PLACEMENT_POLL_SECONDS,
        runtime_directory=bus_root,
        settings_directory=isolated_settings,
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


def watch_at(wiring, owner: str, data_type: str) -> Inbox:
    """Read a type at a part that is deliberately not started in this run.

    An observer on a running part's inbox would take its input away, so every
    watcher here sits on a part that is off.
    """
    return Inbox(
        part_id=owner,
        data_type=data_type,
        address=wiring[owner].inboxes[data_type],
        receive_buffer_bytes=RECEIVE_BUFFER_BYTES,
    )


def an_entry_order(venue_id: str, symbol: str, price: float, quantity: float) -> OrderRequest:
    """The market order the router sends to open a position (operator, 2026-08-23).

    **It names its segment**, and has had to since 2026-09-05. Three segment bots
    share one spine, so `money-mode` is published one per segment, and
    `level_for_segment` hands an unstamped payload a level only when there is
    exactly one -- correctly refusing to guess when there are four. Left
    unstamped, `paper-fill-simulator` saw the order, resolved no mode, and
    refused it as a live order; the entry crossed the bus, nothing filled, no
    exit was ever chained, and the position read as naked. That refusal was the
    one on that part with no counter behind it, so the standing showed
    `orders_seen` climbing beside a row of zeroes (both fixed 2026-09-06).
    """
    return OrderRequest(
        client_order_id="closing-chain-entry",
        destination=PAPER_BOOK,
        venue_id=venue_id,
        symbol=symbol,
        side=BUY,
        quantity=quantity,
        limit_price=0.0,
        stop_price=0.0,
        order_type=MARKET,
        slice_sequence=1,
        slice_count=1,
        at_second=0.0,
        outcome=ROUTED,
        reason="this test stands in for the decision half, which is proven separately",
        routed_at_ns=time.time_ns(),
        segment=SEGMENT_TRADED,
    )


class StopTargetPlanForThisRun:
    """What `stop-target-placer` publishes, at prices this replay actually reaches."""

    def __init__(self, venue_id, symbol, side, entry_price, stop_price, target_price):
        self.venue_id = venue_id
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.stop_price = stop_price
        self.target_price = target_price
        self.outcome = "placed"
        self.stop_distance_fraction = abs(entry_price - stop_price) / entry_price
        self.reward_to_risk = None
        self.nearest_cluster_price = None
        self.distance_estimate = None
        self.reason = "placed by the integration test at prices inside the replayed range"
        self.planned_at_ns = time.time_ns()

    @property
    def is_placeable(self) -> bool:
        return True


@pytest.mark.slow
def test_a_position_opens_and_closes_across_nine_processes(
    launcher, bus_root, real_trades_of_one_symbol, isolated_settings
):
    # The entry is filled against the first trade of the run, and the exits are
    # placed before the rest of it is replayed. Splitting the series is what makes
    # this deterministic: a target computed over the whole run can sit at a high
    # the market reached before the exits were ever on the book, and the test would
    # then fail for a reason that has nothing to do with the code.
    trades = real_trades_of_one_symbol
    opening, replayed = trades[:1], trades[1:]
    venue_id, symbol = trades[0].venue_id, trades[0].symbol
    entry_price = opening[0].price
    prices = [trade.price for trade in replayed]
    highest, lowest = max(prices), min(prices)
    assert highest > entry_price, (
        f"{symbol} never rose above its first trade of the day ({entry_price} to {highest}) "
        f"in the {len(replayed)} trades that follow it; a target on this replay would never "
        f"be reached and this test would assert nothing"
    )
    target_price = entry_price + (highest - entry_price) * EXIT_INSIDE_THE_RUN_S_RANGE
    # Below anything this replay reaches, so the position closes on its target and
    # the stop is the exit that must be withdrawn.
    stop_price = lowest * 0.9

    wiring = derive_wiring(runtime_directory=bus_root)
    watched = {
        "position": watch_at(wiring, "exposure-limiter", "position"),
        "closed-trade": watch_at(wiring, "position-recorder", "closed-trade"),
        "inr-pnl-statement": watch_at(wiring, "board-snapshot-builder", "inr-pnl-statement"),
        # `stop-adjustment` has exactly one consumer in the blueprint and it is
        # running, so watching it would take the manager's input away. What the
        # chainer did is visible in what the manager published, which is the next
        # thing along and the thing that matters: the exits actually being sent.
        "order-request": watch_at(wiring, "order-state-poller", "order-request"),
        # What the book did with those orders. Watched because "nothing closed"
        # is not a diagnosis: a refused fill, a resting stop and an order held in
        # flight are three different failures and the outcome names which. Only
        # fills travel on `fill`, so the book's own counters come from its health.
        "fill": watch_at(wiring, "trade-lifecycle-recorder", "fill"),
        "part-health": watch_at(wiring, "heartbeat-collector", "part-health"),
    }
    seen = {data_type: [] for data_type in watched}

    feed = Publisher(
        part_id="venue-trade-stream-reader",
        outbound=wiring["venue-trade-stream-reader"].outbound,
        maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
    )
    router = Publisher(
        part_id="order-destination-router",
        outbound=wiring["order-destination-router"].outbound,
        maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
    )
    # The Indian market has session hours and crypto did not, so
    # paper-fill-simulator has consumed `market-session-state` since 2026-09-05
    # and parks an order until a session has been *measured* -- never inferring
    # one from a price arriving. Without this the entry order-request crosses the
    # bus, is held, never fills, and no exit is ever chained: the exact failure
    # this test reported until 2026-09-06.
    calendar = Publisher(
        part_id="market-session-calendar",
        outbound=wiring["market-session-calendar"].outbound,
        maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
    )
    placer = Publisher(
        part_id="stop-target-placer",
        outbound=wiring["stop-target-placer"].outbound,
        maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
    )

    try:
        for part_id in CLOSING_CHAIN:
            launcher.start(part_id)
        for part_id in CLOSING_CHAIN:
            inboxes = wiring[part_id].inboxes
            if inboxes:
                assert wait_for_address(next(iter(inboxes.values()))), f"{part_id} never bound"

        # The plan is published before the entry is sent, which is what the chainer
        # is for: a plan that arrived after the fill would be a plan made while the
        # position was already naked. It is republished with the feed because each
        # part reads its inbox on its own clock and a message published into a cold
        # circuit is consumed once by whoever happened to be ready.
        plan = StopTargetPlanForThisRun(
            venue_id, symbol, BUY, entry_price, stop_price, target_price
        )
        entry = an_entry_order(venue_id, symbol, entry_price, quantity=0.01)

        # Only the opening price is fed while the position is opened, so the
        # market cannot run past the exits before they exist.
        # Stated open, and the captured prints are themselves the evidence it
        # was: a print exists only because the market traded. The same reasoning
        # operate/replay_a_captured_session.py already applies, and the date is
        # the session being replayed rather than today.
        def session_now() -> MarketSessionState:
            """Stamped now, every time it is published.

            `paper-fill-simulator` reads sessions through a `LatestByKey` bounded
            by `market_condition_level_maximum_age_seconds`, so a session stamped
            0 is older than the bound the moment it arrives and expires before it
            can be used -- and an expired session is an unmeasured one, which is
            not an open one (Rule 8). The part is right; a fixed stamp was wrong.
            """
            return MarketSessionState(
                segment="NSE_FO",
                kind=SessionKind.OPEN,
                as_of_date=datetime.date.today(),
                reason="replaying captured Upstox prints, which exist because it traded",
                observed_at_ns=time.time_ns(),
            )

        warm_up_ends = time.monotonic() + 4.0
        while time.monotonic() < warm_up_ends:
            calendar.publish("market-session-state", [session_now()])
            placer.publish("stop-target-plan", [plan])
            feed.publish("market-data", opening)
            time.sleep(REPLAY_PAUSE_SECONDS)

        router.publish("order-request", [entry])

        # Both exits must be resting before the market is allowed to move. Until
        # then the run keeps feeding the opening price, which no exit reacts to.
        exits_placed_by = time.monotonic() + 60.0
        while time.monotonic() < exits_placed_by and len(seen["order-request"]) < 3:
            calendar.publish("market-session-state", [session_now()])
            placer.publish("stop-target-plan", [plan])
            feed.publish("market-data", opening)
            time.sleep(REPLAY_PAUSE_SECONDS)
            for data_type, inbox in watched.items():
                seen[data_type].extend(inbox.drain())
        assert len(seen["order-request"]) >= 3, (
            f"the entry and both exits should have crossed the bus; saw "
            f"{len(seen['order-request'])} order-request(s), so the position is naked and "
            f"nothing further this test asserts would mean anything. What did cross: "
            + ", ".join(
                f"{data_type} {len(messages)}"
                for data_type, messages in sorted(seen.items())
                if data_type != "part-health"
            )
            + ". A refused fill, a resting order and one held in flight are three "
            "different failures, and this line is where the difference shows. "
            + book_counters(seen["part-health"])
        )

        deadline = time.monotonic() + PATIENCE_SECONDS
        position = 0
        while position < len(replayed) and time.monotonic() < deadline:
            feed.publish("market-data", replayed[position : position + REPLAY_BATCH])
            position += REPLAY_BATCH
            time.sleep(REPLAY_PAUSE_SECONDS)
            for data_type, inbox in watched.items():
                seen[data_type].extend(inbox.drain())
            if seen["closed-trade"]:
                break

        settle = time.monotonic() + 8.0
        while time.monotonic() < settle:
            feed.publish("market-data", replayed[max(0, position - REPLAY_BATCH):position])
            for data_type, inbox in watched.items():
                seen[data_type].extend(inbox.drain())
            time.sleep(0.05)

        still_running = [part_id for part_id in CLOSING_CHAIN if launcher.is_running(part_id)]
    finally:
        for inbox in watched.values():
            inbox.close()
        feed.close()
        router.close()
        calendar.close()
        placer.close()

    counted = {data_type: len(messages) for data_type, messages in seen.items()}
    book_standing = {}
    for message in seen["part-health"]:
        if getattr(message.payload, "part_id", None) == "paper-fill-simulator":
            book_standing = dict(message.payload.standing)

    assert still_running == list(CLOSING_CHAIN), (
        f"parts died: {sorted(set(CLOSING_CHAIN) - set(still_running))}"
    )
    assert counted["position"] > 0, (
        f"the fill never became a position, so nothing downstream had anything to read: {counted}"
    )
    assert counted["order-request"] > 0, (
        f"no exit order was ever sent, which leaves the position naked: {counted}"
    )
    assert counted["closed-trade"] > 0, (
        f"{symbol} rose from {entry_price} to {highest} against a target at {target_price} "
        f"and nothing closed: {counted}. The book's own counters: {book_standing}"
    )

    closed = seen["closed-trade"][0].payload
    assert closed.symbol == symbol
    assert closed.exit_price >= target_price, (
        "a take-profit fills at the price the trigger found, at or beyond the target"
    )
    assert closed.fees_paid > 0, "a round trip pays the venue twice, and paper must too"

    assert counted["inr-pnl-statement"] > 0, (
        f"a closed trade with no statement is a trade nobody can add up: {counted}"
    )
    statement = seen["inr-pnl-statement"][0].payload
    assert statement.net_pnl_inr == pytest.approx(
        statement.gross_pnl_inr - statement.fees_inr + statement.funding_inr
    )
