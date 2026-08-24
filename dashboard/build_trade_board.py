#!/usr/bin/env python3
"""Generate the trade board: every trade this system has recorded, and its proof.

    .venv/bin/python dashboard/build_trade_board.py

Writes dashboard/trade-board.html beside this script.

Rule 8, applied to the one board an operator will look at to answer "is it
trading". Every number here comes from a file this script read on this run:

- the **journal** the operator's settings name, which is what
  `trade-lifecycle-recorder` wrote -- the trades, their stages, and the digest
  chain that says the record was not edited;
- the **supervisor log** of `operate/run_live_spine.py`, which says which parts
  were started, when, and what was restarted;
- the **process table**, which says whether those parts are still alive;
- the **tape**, whose last write says whether prices are still arriving.

What it must never do is imply a trade happened because the machinery for one
exists. A system that has opened nothing renders as having opened nothing, and
the parts that would close a trade render as NOT BUILT with their names on the
board, because six of them are not written yet.

**A journal entry recorded before the trading half was ever started live is
marked as such.** The integration test runs the same fourteen parts, and before
it was given a ledger of its own it wrote into this one. Which side of that line
an entry falls on is read from the supervisor log rather than guessed.
"""

from __future__ import annotations

import html
import json
import os
import pathlib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

PROJECT_HOME = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT_HOME) not in sys.path:
    sys.path.insert(0, str(PROJECT_HOME))

from parts.ledger.position_recorder import TRADE_CLOSED as CLOSED_TRADE_KIND  # noqa: E402
from parts.ledger.trade_lifecycle_recorder import (  # noqa: E402
    LIFECYCLE_STAGES,
    REPEATABLE_STAGES,
)
from parts.observability.heartbeat_collector import (  # noqa: E402
    REPORTING as HEARTBEAT_REPORTING,
    read_heartbeat_table_file,
)
from runtime.journal import GENESIS_DIGEST, compute_digest  # noqa: E402
from runtime.settings_reader import load_settings_document, settings_directory  # noqa: E402

BOARD_PATH = PROJECT_HOME / "dashboard" / "trade-board.html"
STATE_DIRECTORY = pathlib.Path.home() / ".local/share/ajit-segment-bots"
SUPERVISOR_LOG = STATE_DIRECTORY / "live-spine.jsonl"
TAPE_ROOT = STATE_DIRECTORY / "tape"

# The part whose presence in a started spine makes the journal a live record
# rather than a test's. Named here because it is the one thing that separates the
# two, and guessing it would put a test's fills on the board as trades.
RECORDER = "trade-lifecycle-recorder"

# What has to be running before a paper trade can open at all, in the order the
# data moves. Each is probed against the process table; a missing one is why
# nothing opened, and it is more useful than any summary of it.
TRADING_HALF = (
    "opinion-arbiter",
    "instrument-selector",
    "position-sizer",
    "trade-capital-bounds-gate",
    "order-idempotency-stamper",
    "order-destination-router",
    "paper-fill-simulator",
    "trade-lifecycle-recorder",
)

# What a trade needs before it can close, by part id and what each produces. Not a
# claim that any of them is missing: which of them exist, which can start, and
# which are running is probed below. Asserting "unwritten" here is the mistake
# this list was written with, and every one of the six turned out to have code.
CLOSING_CHAIN = (
    ("fill-reconciler", "position"),
    ("peak-excursion-tracker", "peak-excursion"),
    ("stop-target-placer", "stop-target-plan"),
    ("exit-order-chainer", "stop-adjustment"),
    ("stop-order-manager", "order-request"),
    ("position-close-detector", "closed-trade"),
)

# The columns the board cannot fill from a file, and the part that would fill each
# one. Rendered as their own state in the table rather than left out: a column that
# is missing tells the reader nothing, and one silently blank tells them something
# false.
COLUMNS_A_PART_WOULD_FILL = {
    "target": ("stop-target-placer", "stop-target-plan"),
    "trailing stop": ("exit-order-chainer", "stop-adjustment"),
    "forecast price": ("kronos-forecaster", "price-forecast"),
}

# How many recent decisions the freshness probe ages against the tape. Each one
# walks the tape for that symbol, so this is a cost, and the worst of a few dozen
# is what the tile is about rather than an average over everything ever decided.
MOST_DECISIONS_AGED = 25
# How wide a window around the decision the tape is read over. Wide enough that a
# quiet symbol still has a print in it, narrow enough that the price found is the
# price at the decision rather than a later one.
DECISION_WINDOW_NS = 30_000_000_000
# A decision this far from the market is drifting; this far is broken. Both are
# multiples of the 0.133% BTCUSDT traded through in the 28 captured seconds of
# 2026-08-22: ordinary movement between a decision and its record is well under
# the first, and the 6% seen on 2026-08-23 is far past the second.
DRIFTED_DECISION = 0.004
BADLY_STALE_DECISION = 0.02

OK = "OK"
NOT_BUILT = "NOT BUILT"
FAILING = "FAILING"
UNMEASURED = "NOT MEASURED"
WAITING = "NOTHING YET"

STATE_CLASS = {
    OK: "ok",
    NOT_BUILT: "not-built",
    FAILING: "failing",
    UNMEASURED: "unmeasured",
    WAITING: "waiting",
}

# How long the tape may go unwritten before the feed counts as stopped. Not a
# threshold about the market: 62 symbols printed 285 trades a second on
# 2026-08-22, so a minute of silence is four orders of magnitude past normal and
# means the reader is gone rather than that the market is quiet.
TAPE_SILENCE_SECONDS = 60.0


@dataclass
class ProbeResult:
    """One measured fact and the evidence it came from."""

    label: str
    state: str
    value: str
    proof: str


@dataclass
class RecordedTrade:
    """One trade as the journal holds it: its stages, and the fills under them."""

    trade_id: str
    stages: list[str] = field(default_factory=list)
    first_recorded_at_ns: int | None = None
    last_recorded_at_ns: int | None = None
    venue_id: str | None = None
    symbol: str | None = None
    side: str | None = None
    quantity: float = 0.0
    notional: float = 0.0
    fees: float = 0.0
    fills: int = 0
    # The side the first fill opened on, and what has been sold back since. A
    # trade's fills are not all entries once positions can close: an exit arrives
    # as a fill on the opposite side, and adding it to the quantity would report a
    # closed position as twice the size it ever was.
    opened_side: str | None = None
    exit_quantity: float = 0.0
    exit_notional: float = 0.0
    # True only when a fill of this trade was recorded after the trading half was
    # first started live. A trade nobody filled is not an opened trade, and a test
    # run's fill does not become one because a detector saw the symbol again.
    is_from_a_live_run: bool = False
    # What the decision said, taken from the stages that carry it: the stop the
    # exit plan proposed, how long the intent expected to be held, and how sure
    # the bull bot was. Each is None when no stage recorded it, which is a real
    # state for a trade that never got past being noticed.
    stop_price: float | None = None
    horizon_seconds: float | None = None
    conviction: float | None = None
    # Filled from the tape when the row is rendered, not while entries are read:
    # it costs a walk over the venue's own records and only the trades that are
    # listed are worth it.
    prices: "PriceWindow | None" = None

    @property
    def average_price(self) -> float | None:
        return self.notional / self.quantity if self.quantity else None

    @property
    def capital_in_quote(self) -> float | None:
        """What was actually put into this position, in the quote currency.

        Entry price times quantity -- the notional the position was opened at.
        Not the margin posted: margin is notional divided by the leverage the
        trade was opened at, and nothing records a per-trade leverage yet, so
        stating a margin figure would mean inventing the divisor.
        """
        return self.notional or None

    @property
    def is_open(self) -> bool:
        """Filled, and not sold back.

        Measured from the fills rather than asserted. A trade whose exit fills
        have taken the quantity to zero is closed, and the quantity is what says
        so -- there is no separate flag for a reader to trust instead.
        """
        if self.fills <= 0:
            return False
        return self.quantity > 0

    @property
    def exit_price(self) -> float | None:
        return self.exit_notional / self.exit_quantity if self.exit_quantity else None

    @property
    def is_long(self) -> bool:
        return (self.side or "buy") == "buy"

    def profit_at(self, price: float | None) -> float | None:
        """What this position is worth at a price, in quote currency, after fees.

        Signed by the side: a short is worth the distance the price fell. Fees are
        the ones actually charged on the fills, so this is money, not a move.
        """
        entry = self.average_price
        if price is None or entry is None or not self.quantity:
            return None
        move = (price - entry) if self.is_long else (entry - price)
        return move * self.quantity - self.fees

    @property
    def peak_profit(self) -> float | None:
        """The best this position has been worth since it opened."""
        if self.prices is None:
            return None
        return self.profit_at(self.prices.highest if self.is_long else self.prices.lowest)

    @property
    def worst_loss(self) -> float | None:
        """The worst it has been worth since it opened -- what it went through."""
        if self.prices is None:
            return None
        return self.profit_at(self.prices.lowest if self.is_long else self.prices.highest)

    @property
    def profit_now(self) -> float | None:
        return self.profit_at(self.prices.last_price if self.prices else None)


def read_journal_path() -> pathlib.Path:
    """Where the operator's settings say the journal is written."""
    document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    return pathlib.Path(str(document.read_value("journal_path"))).expanduser()


def journal_paths() -> list[pathlib.Path]:
    """Every journal file on this machine: the base, and one per recorder.

    Each recorder writes its own file, because every entry carries the digest of
    the one before it and two processes appending to one file interleave into no
    chain at all. So the board reads them all and verifies each on its own -- a
    break in one recorder's chain is a fact about that recorder, and merging them
    would report it as a fact about the ledger.

    The base path is included because it is where the record lived before the
    recorders were split, and a board that stopped reading it would lose every
    trade made before 2026-08-23.
    """
    base = read_journal_path()
    found = [base] if base.exists() else []
    found.extend(sorted(
        path for path in base.parent.glob(f"{base.stem}.*{base.suffix}")
        if path != base
    ))
    return found


def read_journal_entries(path: pathlib.Path) -> tuple[list[dict], str | None]:
    """Every entry on the journal, and the first line that would not parse.

    A line that cannot be read is returned rather than skipped: a ledger with a
    hole in it must not render as a ledger with fewer trades.

    For a file that fits in memory. The board's own pass streams instead --
    `stream_journal_file` -- because the recorders write gigabytes a day and a
    list of every entry is what OOM-killed this builder on 2026-08-24.
    """
    if not path.exists():
        return [], None
    report = JournalFileReport(path=path)
    entries = list(stream_journal_file(path, report))
    return entries, report.problem


@dataclass
class JournalFileReport:
    """What one pass over one recorder's file measured about the file itself."""

    path: pathlib.Path
    count: int = 0
    chains: int = 0
    broken: str | None = None
    problem: str | None = None


def stream_journal_file(path: pathlib.Path, report: JournalFileReport):
    """Yield each entry once, verifying the chain as the entries pass.

    One streaming pass carries everything the board needs -- the entries for
    whoever is aggregating them, and the file's own count and chain verdict on
    the report -- while holding one line in memory at a time. The recorders
    write gigabytes a day; reading a file of that size into a list is how this
    builder died at 22 GB on 2026-08-24, with the board stale behind it.
    """
    if not path.exists():
        return
    previous = None
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as failure:
                report.problem = f"line {number} of {path} is not readable: {failure}"
                return
            report.count += 1
            if report.broken is None:
                expected = compute_digest(
                    sequence=entry["sequence"],
                    kind=entry["kind"],
                    part_id=entry["part_id"],
                    payload=entry["payload"],
                    previous_digest=entry["previous_digest"],
                )
                if expected != entry["digest"]:
                    report.broken = (
                        f"entry {entry['sequence']} ({entry['kind']}) does not hash to "
                        f"its own digest"
                    )
                elif entry["previous_digest"] == GENESIS_DIGEST:
                    report.chains += 1
                elif previous is not None and entry["previous_digest"] != previous:
                    report.broken = (
                        f"entry {entry['sequence']} follows a digest that is not the one "
                        f"entry {entry['sequence'] - 1} produced"
                    )
                previous = entry["digest"]
            yield entry


def find_first_live_recorder_start_ns() -> int | None:
    """When a spine that included the recorder was first started, from its own log.

    None when no such run has ever happened, which is the state that says every
    journal entry present came from somewhere other than a live run.
    """
    if not SUPERVISOR_LOG.exists():
        return None
    for line in SUPERVISOR_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "spine-starting" or RECORDER not in event.get("parts", ()):
            continue
        return int(
            datetime.strptime(event["observed_at"], "%Y-%m-%dT%H:%M:%S%z").timestamp() * 1e9
        )
    return None


def collect_trades(entries: list[dict], live_from_ns: int | None) -> list[RecordedTrade]:
    """Group journal entries into trades, in the order they were first recorded.

    A trade id is not unique over time. Before `order-idempotency-stamper` gives
    an order its identity the recorder correlates stages by venue and symbol, so
    every trade ever made on BTCUSDT shares one id -- and grouping on it alone
    merges a candidate noticed this minute into a fill from a test three hours
    ago, then reports the whole thing as a live trade. So a group ends when the
    lifecycle goes backwards: a stage at or before the furthest one already seen
    is the next trade on that symbol, not more of the last one.

    `is_from_a_live_run` is decided by the group's own fills. A trade opened on
    the live run is one whose *fill* was recorded after the trading half was
    started -- anything weaker marks a test's fill live the moment a detector
    notices the same symbol again.
    """
    collector = TradeCollector(live_from_ns=live_from_ns, keep_unfilled=True)
    for entry in entries:
        collector.observe(entry)
    return collector.trades()


class TradeCollector:
    """`collect_trades`, one entry at a time, keeping only what will be listed.

    The grouping is `collect_trades`' own -- that function now feeds this. What
    the class adds is a memory rule for the streaming pass: with
    `keep_unfilled=False`, a group that ends without ever seeing a fill is
    counted and dropped rather than kept, because the detectors journal
    millions of candidates a day and a board that held a `RecordedTrade` for
    each was OOM-killed on 2026-08-24. Every group is still counted in
    `groups_seen`, and the groups anything renders -- filled ones, and the
    still-open ones -- are all retained.
    """

    def __init__(self, live_from_ns: int | None, keep_unfilled: bool) -> None:
        self._live_from_ns = live_from_ns
        self._keep_unfilled = keep_unfilled
        self._furthest: dict[str, int] = {}
        self._open: dict[str, RecordedTrade] = {}
        self._collected: list[RecordedTrade] = []
        self.groups_seen = 0

    def observe(self, entry: dict) -> None:
        payload = entry.get("payload") or {}
        trade_id = payload.get("trade_id")
        if trade_id is None or entry["kind"] not in LIFECYCLE_STAGES:
            return
        position = LIFECYCLE_STAGES.index(entry["kind"])
        recorded_at = int(entry["recorded_at_ns"])

        # A repeated stage ends the group unless the recorder allows it to repeat:
        # a fill may arrive more than once for one order, because a partial fill is
        # ordinary, and splitting there would count one trade as two.
        repeats_legitimately = (
            position == self._furthest.get(trade_id, -1)
            and entry["kind"] in REPEATABLE_STAGES
        )
        starts_a_new_trade = trade_id not in self._open or (
            position <= self._furthest.get(trade_id, -1) and not repeats_legitimately
        )
        if starts_a_new_trade:
            trade = RecordedTrade(trade_id=trade_id)
            self.groups_seen += 1
            self._open[trade_id] = trade
            if self._keep_unfilled:
                self._collected.append(trade)
            self._furthest[trade_id] = position
        else:
            trade = self._open[trade_id]
            self._furthest[trade_id] = max(self._furthest[trade_id], position)

        trade.stages.append(entry["kind"])
        if trade.first_recorded_at_ns is None:
            trade.first_recorded_at_ns = recorded_at
        trade.last_recorded_at_ns = recorded_at
        trade.venue_id = payload.get("venue_id") or trade.venue_id
        trade.symbol = payload.get("symbol") or trade.symbol
        trade.side = payload.get("side") or trade.side
        if payload.get("stop_price") is not None:
            trade.stop_price = float(payload["stop_price"])
        if payload.get("horizon_seconds") is not None:
            trade.horizon_seconds = float(payload["horizon_seconds"])
        conviction = payload.get("conviction")
        if isinstance(conviction, dict) and conviction.get("value") is not None:
            trade.conviction = float(conviction["value"])
        if entry["kind"] == "fill":
            if not self._keep_unfilled and trade.fills == 0:
                # Its first fill is what makes a group worth holding on to.
                self._collected.append(trade)
            trade.fills += 1
            quantity = float(payload.get("quantity") or 0.0)
            price = float(payload.get("price") or 0.0)
            side = payload.get("side")
            if trade.opened_side is None:
                trade.opened_side = side
            if side == trade.opened_side:
                trade.quantity += quantity
                trade.notional += quantity * price
            else:
                # The other side of the same trade: this is the exit closing it.
                trade.quantity -= quantity
                trade.exit_quantity += quantity
                trade.exit_notional += quantity * price
            trade.fees += float(payload.get("fee") or 0.0)
            if self._live_from_ns is not None and recorded_at >= self._live_from_ns:
                trade.is_from_a_live_run = True

    def trades(self) -> list[RecordedTrade]:
        collected = self._collected
        if not self._keep_unfilled:
            # The still-open groups were deferred awaiting a fill; a group that is
            # open at the end of the pass is state, not spam, and is listed.
            held = {id(trade) for trade in collected}
            collected = collected + [
                trade for trade in self._open.values()
                if trade.fills == 0 and id(trade) not in held
            ]
        return sorted(collected, key=lambda trade: trade.first_recorded_at_ns or 0)


def verify_journal_chain(entries: list[dict]) -> tuple[int, str | None]:
    """Recompute every digest. Returns how many chains the file holds, and the
    first entry that does not hold.

    **The file holds one chain per process, not one chain.** `Journal` starts at
    the genesis digest whenever a recorder starts, and appends to the file the
    previous recorder wrote -- so a run boundary is a place where the chain
    legitimately restarts, and checking the file as a single chain reports an
    edit at every restart. What that costs is stated on the board rather than
    hidden here: within a run an edit is detected, and a whole run removed from
    the file leaves nothing behind that says it was there.
    """
    previous = None
    chains = 0
    for entry in entries:
        expected = compute_digest(
            sequence=entry["sequence"],
            kind=entry["kind"],
            part_id=entry["part_id"],
            payload=entry["payload"],
            previous_digest=entry["previous_digest"],
        )
        if expected != entry["digest"]:
            return chains, (
                f"entry {entry['sequence']} ({entry['kind']}) does not hash to its own digest"
            )
        starts_a_chain = entry["previous_digest"] == GENESIS_DIGEST
        if starts_a_chain:
            chains += 1
        elif previous is not None and entry["previous_digest"] != previous:
            return chains, (
                f"entry {entry['sequence']} follows a digest that is not the one entry "
                f"{entry['sequence'] - 1} produced"
            )
        previous = entry["digest"]
    return chains, None


def read_running_parts() -> dict[str, int]:
    """Which spine parts are alive right now, by pid, from the supervisor log and /proc.

    The log says what was started; /proc says what is still there. Trusting the
    log alone would report a spine that died an hour ago as running.
    """
    if not SUPERVISOR_LOG.exists():
        return {}
    started: dict[str, int] = {}
    for line in SUPERVISOR_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "part-started":
            started[event["part_id"]] = int(event["pid"])
        elif event.get("event") in ("spine-stopped", "part-exited"):
            for part_id in list(started):
                if event.get("part_id") in (None, part_id):
                    started.pop(part_id, None)
    return {
        part_id: pid
        for part_id, pid in started.items()
        if pathlib.Path(f"/proc/{pid}").exists()
    }


@dataclass
class PriceWindow:
    """What a symbol did between one moment and now, off the tape.

    The tape is the venue's own record and it is being written continuously by
    `venue-trade-stream-reader`, so this is as current as the feed is -- which is
    why `read_at_ns` travels with it. A price with no time beside it is a number
    a reader has to trust; a price with one is a number they can judge.
    """

    last_price: float
    read_at_ns: int
    highest: float
    lowest: float
    trades_seen: int

    @property
    def age_seconds(self) -> float:
        return max(0.0, datetime.now(timezone.utc).timestamp() - self.read_at_ns / 1e9)


def read_price_window(venue_id: str, symbol: str, since_ns: int | None) -> PriceWindow | None:
    """The last price for a symbol, and its high and low since a moment.

    Reads the venue's own payloads back through that venue's adapter rather than
    re-deriving a price format here: what a trade message means is the adapter's
    business everywhere else in this system, and a board that parsed it itself
    would be a second definition to keep in step.

    Only today's file is read. A position older than midnight would have its
    excursion measured from the start of the day, which is why the window says how
    many trades it actually saw -- an excursion over 40 trades and one over 40 000
    are not the same claim.
    """
    from runtime.tape import day_of_timestamp_ns, read_payload, read_tape_index, tape_paths_for
    from runtime.venues.adapter_registry import load_venue_adapter

    now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
    index_path, blob_path = tape_paths_for(
        TAPE_ROOT, venue_id, symbol, day_of_timestamp_ns(now_ns)
    )
    if not index_path.exists() or not blob_path.exists():
        return None
    index = read_tape_index(index_path)
    if len(index) == 0:
        return None

    try:
        adapter = load_venue_adapter(venue_id)
    except Exception:
        return None

    first = 0
    if since_ns is not None:
        first = int(index["received_at_ns"].searchsorted(since_ns, side="left"))
        # A trade opened before today, or before this file's first record, takes
        # the whole file rather than nothing: the window says how much it covered.
        first = min(first, len(index) - 1)

    highest = lowest = last_price = None
    read_at_ns = 0
    seen = 0
    with open(blob_path, "rb") as handle:
        for record in index[first:]:
            handle.seek(int(record["blob_offset"]))
            payload = handle.read(int(record["blob_length"]))
            try:
                trades = adapter.read_trades(payload)
            except Exception:
                continue
            for trade in trades:
                if trade.symbol != symbol:
                    continue
                seen += 1
                last_price = trade.price
                read_at_ns = int(record["received_at_ns"])
                highest = trade.price if highest is None else max(highest, trade.price)
                lowest = trade.price if lowest is None else min(lowest, trade.price)

    if last_price is None:
        return None
    return PriceWindow(
        last_price=last_price,
        read_at_ns=read_at_ns,
        highest=highest,
        lowest=lowest,
        trades_seen=seen,
    )


def read_tape_last_write_seconds() -> float | None:
    """How long ago anything was written to the tape, or None if there is no tape."""
    newest = None
    for blob in TAPE_ROOT.rglob("*.blob"):
        modified = blob.stat().st_mtime
        newest = modified if newest is None else max(newest, modified)
    if newest is None:
        return None
    return max(0.0, datetime.now(timezone.utc).timestamp() - newest)


def read_money_mode() -> tuple[str | None, str]:
    """The segment's money mode and the file that said so."""
    runtime = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    segment = str(runtime.read_value("segment_id"))
    path = settings_directory() / "segments" / f"{segment}.toml"
    if not path.exists():
        return None, f"{path} does not exist"
    document = load_settings_document(path, segment)
    return str(document.read_value("money_mode")), str(path)


def probe_money_mode() -> ProbeResult:
    mode, proof = read_money_mode()
    if mode is None:
        return ProbeResult("Money mode", UNMEASURED, "no segment settings file", proof)
    if mode != "paper":
        return ProbeResult(
            "Money mode", FAILING, f"{mode} — real money", f"{proof} says money_mode = {mode!r}"
        )
    return ProbeResult("Money mode", OK, "paper", f"{proof} says money_mode = 'paper'")


def probe_trading_half(running: dict[str, int]) -> ProbeResult:
    missing = [part_id for part_id in TRADING_HALF if part_id not in running]
    if not running:
        return ProbeResult(
            "The trading half",
            NOT_BUILT,
            "nothing is running",
            f"no live pid for any of the {len(TRADING_HALF)} parts in {SUPERVISOR_LOG}",
        )
    if missing:
        return ProbeResult(
            "The trading half",
            FAILING,
            f"{len(TRADING_HALF) - len(missing)} of {len(TRADING_HALF)} up",
            "not running: " + ", ".join(missing),
        )
    return ProbeResult(
        "The trading half",
        OK,
        f"{len(TRADING_HALF)} of {len(TRADING_HALF)} up",
        "every pid confirmed in /proc: "
        + ", ".join(f"{part_id}={running[part_id]}" for part_id in TRADING_HALF),
    )


def probe_feed(seconds: float | None) -> ProbeResult:
    if seconds is None:
        return ProbeResult("The feed", UNMEASURED, "no tape", f"nothing under {TAPE_ROOT}")
    if seconds > TAPE_SILENCE_SECONDS:
        return ProbeResult(
            "The feed",
            FAILING,
            f"silent for {seconds / 60:.0f} min",
            f"newest .blob under {TAPE_ROOT} was written {seconds:.0f}s ago, past the "
            f"{TAPE_SILENCE_SECONDS:.0f}s a live feed can be quiet for",
        )
    return ProbeResult(
        "The feed",
        OK,
        f"written {seconds:.0f}s ago",
        f"newest .blob under {TAPE_ROOT}",
    )


def probe_opened(trades: list[RecordedTrade], journal_path: pathlib.Path) -> ProbeResult:
    """How many trades were opened on the live run, counting only filled ones.

    Candidates are excluded from the count and from the comparison: a detector
    noticing a setup is not an opened trade, and 2 400 of them beside no fill is
    the state this board exists to show plainly.
    """
    filled = [trade for trade in trades if trade.fills]
    live = [trade for trade in filled if trade.is_from_a_live_run]
    if not filled:
        return ProbeResult(
            "Trades opened",
            WAITING,
            "none, ever",
            f"{journal_path} holds no fill at all",
        )
    if not live:
        return ProbeResult(
            "Trades opened",
            WAITING,
            f"none live ({len(filled)} filled by tests)",
            f"every fill in {journal_path} was recorded before the trading half was first "
            f"started live, so it was made by the integration test rather than by the bot",
        )
    return ProbeResult(
        "Trades opened",
        OK,
        f"{len(live)} on the live run",
        f"{journal_path}, fills recorded after the trading half was first started",
    )


def probe_noticed(entries: list[dict], live_from_ns: int | None) -> ProbeResult:
    """How many setups the detectors have found on the live run, and how recently.

    The closest thing to progress this board can actually measure. A candidate is
    the detector saying "this looks like something"; whether it becomes a trade is
    the bull bot's decision, and the bot forms none until its model is trained.
    So a large number here with nothing opened is not a fault -- it is the system
    noticing while it is still learning, which is what it should be doing.
    """
    scan = NoticedScan(live_from_ns)
    for entry in entries:
        scan.observe(entry)
    return scan.result()


class NoticedScan:
    """`probe_noticed`'s counting, one entry at a time, holding three numbers.

    The detectors journal millions of candidates a day; the probe needs their
    count, their symbols and the newest timestamp, not the entries.
    """

    def __init__(self, live_from_ns: int | None) -> None:
        self._live_from_ns = live_from_ns
        self._count = 0
        self._newest = 0
        self._symbols: set = set()

    def observe(self, entry: dict) -> None:
        if self._live_from_ns is None or entry["kind"] != "entry-candidate":
            return
        recorded_at = int(entry["recorded_at_ns"])
        if recorded_at < self._live_from_ns:
            return
        self._count += 1
        self._newest = max(self._newest, recorded_at)
        self._symbols.add((entry.get("payload") or {}).get("symbol"))

    def result(self) -> ProbeResult:
        if self._live_from_ns is None:
            return ProbeResult(
                "Setups noticed", NOT_BUILT, "no live run yet",
                f"no spine including {RECORDER} in {SUPERVISOR_LOG}",
            )
        if not self._count:
            return ProbeResult(
                "Setups noticed",
                WAITING,
                "none on this run",
                "no entry-candidate has been journalled since the trading half was started",
            )
        return ProbeResult(
            "Setups noticed",
            OK,
            f"{self._count} on {len(self._symbols)} symbols",
            f"entry-candidate entries journalled since the live run began; most recent "
            f"{as_time(self._newest)} UTC",
        )


def how_far_a_part_is_built(part_id: str, running: dict[str, int]) -> str:
    """Where one part stands: running, startable, written, or not written at all.

    Measured, not assumed. The first version of this board asserted that the six
    parts a close needs were unwritten; all six had code, and what they were
    missing was the twenty-line `start_part` that lets the launcher fork them.
    """
    if part_id in running:
        return "running"
    stem = part_id.replace("-", "_")
    matches = [path for path in (PROJECT_HOME / "parts").rglob(f"{stem}.py") if path.stem == stem]
    if not matches:
        return "no module"
    if "def start_part" not in matches[0].read_text():
        return "no start_part"
    return "startable"


def probe_closed(running: dict[str, int]) -> ProbeResult:
    """Whether a trade can close, and exactly what each part of the chain lacks."""
    standing = {part_id: how_far_a_part_is_built(part_id, running) for part_id, _ in CLOSING_CHAIN}
    proof = ", ".join(f"{part_id}: {state}" for part_id, state in standing.items())
    if all(state == "running" for state in standing.values()):
        return ProbeResult("Trades closed", OK, "the chain is on", proof)
    return ProbeResult(
        "Trades closed",
        NOT_BUILT,
        f"{sum(1 for state in standing.values() if state != 'running')} of "
        f"{len(CLOSING_CHAIN)} not running",
        proof,
    )


def probe_journal_chain(per_file: dict, unreadable: str | None, path: pathlib.Path) -> ProbeResult:
    """Every recorder's chain, verified on its own file.

    On its own file, because a chain is a property of one writer. Two recorders
    appending to one path interleave, and every entry then points at whatever the
    other wrote last -- which is what broke this tile on 2026-08-23, once
    `position-recorder` was wired to the same file as `trade-lifecycle-recorder`.
    Merging the entries and verifying once would report that as a broken ledger
    rather than as two ledgers written into one file.
    """
    if unreadable:
        return ProbeResult("The record", FAILING, "unreadable", unreadable)
    normalised = {}
    for journal, value in per_file.items():
        if isinstance(value, JournalFileReport):
            normalised[journal] = (value.count, value.chains, value.broken)
        else:
            read, _problem = value
            chains, broken = verify_journal_chain(read) if read else (0, None)
            normalised[journal] = (len(read), chains, broken)
    total = sum(count for count, _chains, _broken in normalised.values())
    if not total:
        return ProbeResult("The record", WAITING, "empty", f"{path} holds no entries")

    verdicts = {}
    broken_files = []
    for journal, (count, chains, broken) in normalised.items():
        if not count:
            continue
        verdicts[journal.name] = (count, chains, broken)
        if broken:
            broken_files.append(f"{journal.name}: {broken}")
    if broken_files:
        return ProbeResult(
            "The record", FAILING, f"{len(broken_files)} of {len(verdicts)} chain(s) broken",
            "; ".join(broken_files),
        )
    proof = "; ".join(
        f"{name}: {count} entries in {chains} chain(s)"
        for name, (count, chains, _broken) in sorted(verdicts.items())
    )
    return ProbeResult(
        "The record", OK, f"{total} entries across {len(verdicts)} file(s)",
        f"every digest recomputed from its own content and its predecessor -- {proof}",
    )


def probe_chain_continuity(entries: list[dict]) -> ProbeResult:
    """Whether the record is one chain or one per run, which decides what it proves.

    A chain that restarts at every process start detects an edit inside a run and
    nothing at all about a whole run removed. It is the difference between a
    ledger and a pile of ledgers, and it is not visible from the trades.
    """
    if not entries:
        return ProbeResult(
            "Tamper evidence", UNMEASURED, "no entries", "there is nothing to chain yet"
        )
    chains, broken = verify_journal_chain(entries)
    if broken:
        # Merged entries from several recorders are expected not to chain: each
        # file is its own chain and "The record" verifies them separately. What
        # this tile answers is the different question of whether a whole run could
        # be removed unnoticed, and a break here says only that more than one
        # recorder wrote.
        return ProbeResult(
            "Tamper evidence", NOT_BUILT, "one chain per recorder, per run",
            "each recorder writes its own file and starts a fresh chain when it starts, so an "
            "edit inside one run is detected and a whole run removed from a file is not; "
            "the per-file chains are verified by the record probe above",
        )
    if chains > 1:
        return ProbeResult(
            "Tamper evidence",
            NOT_BUILT,
            f"{chains} separate chains",
            "the journal starts again from the genesis digest every time a recorder starts, so "
            "an edit inside one run is detected and a whole run deleted from the file is not; "
            "continuing the chain needs the recorder to read the last digest before it appends",
        )
    return ProbeResult(
        "Tamper evidence",
        OK,
        "one chain, unbroken",
        "every entry follows the digest of the one before it, across every run",
    )


def probe_learning() -> ProbeResult:
    """How far the bull bot is from its first decision, read from its checkpoint.

    Unmeasurable until 2026-08-23: the model held its training count in its own
    process and wrote it nowhere, so this tile could only say so. It now
    checkpoints what it has learned to the learned-state directory, and this reads
    that file -- which means the number here is the same number the model will
    restore from, not a second count kept for the board.

    Absence is still its own state. No file means the model has not run since
    checkpointing existed, which is different from a model that has run and
    learned nothing, and both are different from a trained one.
    """
    needed = int(load_settings_document(
        settings_directory() / "runtime.toml", "runtime"
    ).read_value("bull_minimum_training_observations"))
    checkpoint, path = read_learned_checkpoint("bull-conviction-model.conviction")
    if checkpoint is None:
        return ProbeResult(
            "Learning progress",
            UNMEASURED,
            "the model has not checkpointed",
            f"nothing readable at {path}; bull-conviction-model writes its first checkpoint "
            f"on its first tick, so this means the part has not run since checkpointing existed",
        )
    try:
        state = checkpoint["state"]
        model = state["models"][state["live"]]
    except (KeyError, TypeError) as unreadable:
        return ProbeResult(
            "Learning progress", FAILING, "the checkpoint could not be read", f"{path}: {unreadable}"
        )

    trained = int(model["observations"])
    positives = int(model["positives"])
    losses = trained - positives
    saved_at = as_time(checkpoint.get("saved_at_ns"))
    proof = (
        f"{path}, saved {saved_at} UTC: {trained} labelled outcome(s), {positives} where the "
        f"setup was right and {losses} where it was not; {needed} of each class are needed "
        f"before the conviction is a measurement rather than a starting point"
    )
    if trained >= needed and positives > 0 and losses > 0:
        return ProbeResult("Learning progress", OK, f"trained on {trained}", proof)
    return ProbeResult(
        "Learning progress", WAITING, f"{trained} of {needed} labelled outcomes", proof
    )


def read_learned_checkpoint(name: str) -> tuple[dict | None, str]:
    """One learned part's checkpoint, or why there is nothing to read.

    Every learned part in this system writes the same kind of document to the same
    directory, so one reader serves them all -- and the number a tile shows is the
    number the part itself restores from, never a second count kept for the board.
    """
    document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    root = pathlib.Path(str(document.read_value("learned_state_root"))).expanduser()
    path = root / f"{name}.json"
    if not path.exists():
        return None, str(path)
    try:
        return json.loads(path.read_text()), str(path)
    except (OSError, ValueError) as unreadable:
        return None, f"{path}: {unreadable}"


def probe_exit_plans() -> ProbeResult:
    """How far the bot is from the first exit plan it has ever been able to build.

    The binding constraint after the conviction model becomes trained, and it was
    invisible until 2026-08-23: `bull-exit-plan-proposer` refuses without a fitted
    excursion profile, the gate is **per symbol and per side**, and a healthy
    total can be thirty symbols with four claims each. So this reports the symbol
    closest to the bar rather than the total, because the total is the number that
    reads as nearly-there when the wait has barely started (Rule 8).
    """
    needed = int(load_settings_document(
        settings_directory() / "runtime.toml", "runtime"
    ).read_value("signal_excursion_minimum_claims"))
    checkpoint, where = read_learned_checkpoint("signal-excursion-profiler.excursions")
    if checkpoint is None:
        return ProbeResult(
            "Exit plans",
            UNMEASURED,
            "the profiler has not checkpointed",
            f"nothing readable at {where}; signal-excursion-profiler writes its first "
            f"checkpoint on its first tick, so this means the part has not run",
        )

    adverse = (checkpoint.get("state") or {}).get("adverse") or {}
    counts = {key: len(values) for key, values in adverse.items()}
    fitted = sorted(key for key, count in counts.items() if count >= needed)
    saved_at = as_time(checkpoint.get("saved_at_ns"))
    closest = max(counts.items(), key=lambda item: item[1], default=None)

    if fitted:
        return ProbeResult(
            "Exit plans",
            OK,
            f"{len(fitted)} symbol/side(s) can be planned",
            f"{where}, saved {saved_at} UTC: {', '.join(fitted[:6])} each have {needed}+ "
            f"settled claims that came right, which is what a stop distance is measured from",
        )
    if closest is None:
        return ProbeResult(
            "Exit plans",
            WAITING,
            "no claim has come right yet",
            f"{where}, saved {saved_at} UTC: the profiler has recorded no settled claim that "
            f"came right, and only those are measured -- how far price runs when a call was "
            f"wrong is unbounded and would place every stop too far away",
        )
    key, count = closest
    return ProbeResult(
        "Exit plans",
        WAITING,
        f"{count} of {needed} on the closest symbol",
        f"{where}, saved {saved_at} UTC: {key} is nearest, with {count} settled claim(s) that "
        f"came right of the {needed} needed; {len(counts)} symbol/side(s) are accumulating. "
        f"The gate is per symbol, so the total across all of them is not the wait",
    )


def probe_parts_alive(now_ns: int | None = None) -> ProbeResult:
    """Every running part's own word about itself: its age, its staleness, what it lost.

    This is the first consumer of `PartHealth.input_loss` and `staleness_seconds`.
    Both had been reported by every part since the substrate was built, and
    nothing read them -- which is how a symbol's price sat frozen for 56 minutes
    on 2026-08-23 inside a part whose health read fine, and the bot decided a
    trade on it. Read from the table heartbeat-collector writes, the same file
    the part monitor's RUNNING rung is read from.

    FAILING when any part is silent, or any part reports input loss, or any
    part's staleness is past the silence threshold: each of those is a decision
    made on data that is not what the market is saying now.
    """
    runtime = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    path = heartbeat_table_path()
    silent_after = float(runtime.read_value("heartbeat_silent_after_seconds"))
    document = read_heartbeat_table_file(path)
    if document is None:
        return ProbeResult(
            "Parts alive", UNMEASURED, "no heartbeat table",
            f"{path} does not exist or is unreadable: heartbeat-collector has not run, so "
            f"no part's staleness or input loss has been read by anything",
        )
    if now_ns is None:
        import time

        now_ns = time.time_ns()
    table_age = (now_ns - int(document.get("collected_at_ns", 0))) / 1e9
    if table_age >= silent_after:
        return ProbeResult(
            "Parts alive", FAILING, f"collector silent {table_age:.0f}s",
            f"{path} was written {table_age:.0f}s ago, past the {silent_after:.0f}s silence "
            f"threshold; the collector itself has stopped and nothing in it is current",
        )
    beats = document.get("heartbeats", [])
    silent = [b["part_id"] for b in beats if b.get("state") != HEARTBEAT_REPORTING]
    # Loss between a part's last two reports is a feed being lost now. Loss since
    # start that is not growing is history -- most of it the burst at startup
    # before every inbox was bound -- and is named, not painted red.
    lossy = [
        (b["part_id"], b.get("input_loss_since_previous"))
        for b in beats if b.get("input_loss_since_previous")
    ]
    lost_once = [
        (b["part_id"], b.get("input_loss"))
        for b in beats if b.get("input_loss") and not b.get("input_loss_since_previous")
    ]
    stale = [
        (b["part_id"], float(b.get("staleness_seconds") or 0.0))
        for b in beats
        if b.get("staleness_seconds") is not None
        and float(b["staleness_seconds"]) >= silent_after
    ]
    reporting = document.get("reporting", 0)
    total = len(beats)
    proof = f"{path}, written {table_age:.0f}s ago: {reporting} of {total} reporting"
    if silent:
        return ProbeResult(
            "Parts alive", FAILING, f"{len(silent)} not reporting",
            f"{proof}; not reporting: {', '.join(sorted(silent))}",
        )
    if lossy:
        worst = max(lossy, key=lambda item: sum(count for _kind, count in item[1]))
        return ProbeResult(
            "Parts alive", FAILING, f"{len(lossy)} losing input",
            f"{proof}; losing input now: {', '.join(sorted(part for part, _loss in lossy))}; "
            f"worst {worst[0]} lost {worst[1]} since its previous report",
        )
    if stale:
        worst = max(stale, key=lambda item: item[1])
        return ProbeResult(
            "Parts alive", FAILING, f"{len(stale)} stale",
            f"{proof}; ticked more than {silent_after:.0f}s ago: {worst[0]} at {worst[1]:.0f}s",
        )
    if total == 0:
        return ProbeResult("Parts alive", WAITING, "nothing has reported", proof)
    worst_staleness = max((float(b.get("staleness_seconds") or 0.0) for b in beats), default=0.0)
    history = (
        f"; lost earlier and not since: "
        + ", ".join(f"{part} {loss}" for part, loss in sorted(lost_once))
        if lost_once else ", no input lost"
    )
    return ProbeResult(
        "Parts alive", OK, f"{reporting} of {total} reporting",
        f"{proof}{history}, worst staleness {worst_staleness:.1f}s",
    )


def probe_decision_freshness(entries: list[dict]) -> ProbeResult:
    """How far the price a decision was made at sits from the price it filled at.

    The measurement that was invisible until 2026-08-23, and the one that mattered
    most. Each part keeps a per-symbol price level that updates only when a
    market-data message for that symbol reaches it, so under input loss a symbol
    can freeze while the part still looks busy and its health still reads fine. On
    the live run at 10:25:15 a trade was decided at an ENAUSDT price of 0.17019 --
    the real market at 09:29:08, fifty-six minutes earlier -- and filled at
    0.18043, six per cent away, with both exits already through their triggers
    before they were ever placed.

    Both numbers are already in the ledger: the decision price the recorder
    journalled on the order, and the price the book filled it at. No part has to
    report anything and no tape has to be walked -- the gap between what the bot
    thought the market was and what it actually got is the whole measurement.

    The fill has to be the right one. A fill is matched to its own order by trade
    id and direction, and only the first counts: an exit fill compared against the
    entry it closes measures the trade's profit, and a later partial measures how
    far the order walked the book. Neither is what the bot believed the market was.
    """
    scan = FreshnessScan()
    for entry in entries:
        scan.observe(entry)
    return scan.result()


class FreshnessScan:
    """`probe_decision_freshness`'s scan, one entry at a time.

    Holds one decided order per trade id and the most recent drifts -- only as
    many as the probe ages -- rather than every journal entry.

    **A fill is matched to its own order, by trade id and by side.** Keyed on the
    symbol instead, as this was until 2026-08-24, a closing fill is compared
    against the entry it closes: the two share a trade id and differ in side, and
    the gap between them is the trade's profit or loss. That made a winning trade
    read as a decision taken 4% away from the market, which is what this tile
    calls badly stale -- so the one measurement built to catch a stale decision
    reported ordinary successful trades as the failure.

    Only the first fill of an order counts. Later partials are the order working
    through the book, which is slippage: real, worth measuring, and not this.
    """

    ENTRY_FILL = "the first fill on this order, in the order's own direction"

    def __init__(self) -> None:
        self._orders: dict = {}
        self._measured: set = set()
        self._drifts: list = []

    def observe(self, entry: dict) -> None:
        payload = entry.get("payload") or {}
        kind = entry.get("kind")
        trade_id = payload.get("trade_id")
        if kind == "bounded-order" and payload.get("entry_price") and trade_id:
            self._orders[trade_id] = (
                float(payload["entry_price"]), payload.get("side"), payload.get("symbol")
            )
        elif kind == "fill" and payload.get("price") and trade_id:
            order = self._orders.get(trade_id)
            if order is None or trade_id in self._measured:
                return
            decided, side, symbol = order
            if side is not None and payload.get("side") != side:
                # The closing fill. Its distance from the entry is the trade's
                # result, and reading it here is how a profit became a defect.
                return
            if decided <= 0:
                return
            self._measured.add(trade_id)
            filled = float(payload["price"])
            self._drifts.append((abs(filled - decided) / decided, symbol, decided, filled))
            if len(self._drifts) > MOST_DECISIONS_AGED:
                self._drifts.pop(0)

    def result(self) -> ProbeResult:
        drifts = self._drifts
        if not drifts:
            return ProbeResult(
                "Decision freshness", WAITING, "nothing has filled yet",
                "this compares the price each decision was made at against the price it "
                "filled at; no order has both numbers on the ledger yet",
            )

        recent = drifts[-MOST_DECISIONS_AGED:]
        recent.sort(reverse=True)
        worst, symbol, at, filled = recent[0]
        median = recent[len(recent) // 2][0]
        proof = (
            f"{len(recent)} most recent opening fill(s) against the price their own decision "
            f"was made at, matched by trade id and direction: median gap {median:.2%}, worst "
            f"{worst:.2%} on {symbol}, decided at {at:g} and filled at {filled:g}"
        )
        if worst > BADLY_STALE_DECISION:
            return ProbeResult("Decision freshness", FAILING, f"worst {worst:.1%} adrift", proof)
        if worst > DRIFTED_DECISION:
            return ProbeResult("Decision freshness", WAITING, f"worst {worst:.1%} adrift", proof)
        return ProbeResult("Decision freshness", OK, f"worst {worst:.2%} adrift", proof)


def heartbeat_table_path() -> pathlib.Path:
    """Where heartbeat-collector writes, from the settings that name it."""
    runtime = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    return pathlib.Path(str(runtime.read_value("heartbeat_table_path"))).expanduser()


def probe_stale_price_refusals(document) -> ProbeResult:
    """How many decisions each part declined because the price it had was too old.

    The counter that closes the loop on 2026-08-23. A part that refuses a stale
    price is doing the right thing, and a part that refuses every one of them is a
    bound nothing can satisfy -- which stops the bot while looking, from outside,
    exactly like a market with nothing worth trading. Both are the same number,
    and the difference is whether anything was chosen alongside the refusals.

    Read from the standing every part now reports on its own health, through the
    table heartbeat-collector writes. Nothing here asks a part a question: it is
    the part's own count of its own refusals.
    """
    label = "Stale prices refused"
    if not document:
        return ProbeResult(
            label, UNMEASURED, "no heartbeat table",
            "heartbeat-collector has not written a table, so no part's own counters "
            "have been read by anything",
        )
    refusals, chosen, by_part = 0.0, 0.0, []
    reported_any = False
    for beat in document.get("heartbeats", ()):
        standing = beat.get("standing") or {}
        if standing:
            reported_any = True
        refused = float(standing.get("refused_for_a_stale_price", 0) or 0)
        refused += float(standing.get("refused_for_no_price_ever", 0) or 0)
        chosen += float(standing.get("chosen", 0) or 0)
        if refused:
            refusals += refused
            by_part.append((refused, beat.get("part_id")))
    if not reported_any:
        return ProbeResult(
            label, UNMEASURED, "no part reported a standing",
            "every part in the table reported health without any of its own counters; "
            "a counter nothing reported is not a count of zero",
        )
    if not refusals:
        return ProbeResult(
            label, OK, "none refused",
            "no part has refused a decision for the age of the price it had",
        )
    by_part.sort(reverse=True)
    detail = ", ".join(f"{part_id} {count:.0f}" for count, part_id in by_part[:4])
    proof = f"{refusals:.0f} refusal(s) for a price too old to act on: {detail}"
    if chosen <= 0:
        return ProbeResult(
            label, FAILING, f"{refusals:.0f} refused, none placed",
            proof + ". Alongside them nothing was chosen at all, so the bound is "
            "refusing everything rather than the stale ones",
        )
    return ProbeResult(label, OK, f"{refusals:.0f} refused", proof)


class RefusedDecisionScan:
    """How many decisions to trade actually reached the book, one entry at a time.

    Between a decision and an order stand every part that can say no: the
    instrument selector, the sizer, the risk limiters, the capital bounds. Each of
    those refusals is legitimate on its own, and none of them is journalled -- so
    a system that had quietly stopped being able to trade at all would show on
    this board as an absence of trades, which is exactly what a quiet market looks
    like. That is the reassurance Rule 8 exists to refuse.

    Identity is the decision -- venue, symbol, side, action -- and not the message.
    The arbiter republishes a standing opinion every tick, so one decision arrives
    thousands of times; counting messages would report a bot refusing thousands of
    trades a minute while it was in fact holding one view.

    Standing aside is not counted. It is a decision not to trade, and the opposite
    of a trade the system could not place.

    No threshold is invented. Refusals are ordinary, the number is shown, and the
    only verdict passed is on the unambiguous case: decisions were made, and not
    one of them reached the book.
    """

    STAND_ASIDE = "stand-aside"

    def __init__(self) -> None:
        self._decided: set = set()
        self._reached_the_book: set = set()

    def observe(self, entry: dict) -> None:
        payload = entry.get("payload") or {}
        kind = entry.get("kind")
        if kind == "trade-intent":
            if payload.get("action") == self.STAND_ASIDE:
                return
            venue, symbol = payload.get("venue_id"), payload.get("symbol")
            side, action = payload.get("side"), payload.get("action")
            if symbol and action:
                self._decided.add(f"{venue}|{symbol}|{side}|{action}")
        elif kind == "bounded-order":
            intent_id = payload.get("intent_id")
            if intent_id:
                self._reached_the_book.add(intent_id)

    def result(self) -> ProbeResult:
        label = "Decisions reaching the book"
        decided = len(self._decided)
        if not decided:
            return ProbeResult(
                label, WAITING, "no actionable decision yet",
                "every trade-intent on the ledger so far says stand aside, or none has been "
                "recorded; a bot that has not decided to trade has refused nothing",
            )
        placed = len(self._decided & self._reached_the_book)
        proof = (
            f"{placed} of {decided} distinct decision(s) on the ledger became an order. "
            f"A decision is venue, symbol, side and action, so one view republished every "
            f"tick counts once"
        )
        if placed == 0:
            return ProbeResult(
                label, FAILING, f"none of {decided} placed", proof
                + ". Every decision was refused between the brain and the book -- which reads "
                "on a board as an absence of trades and is not one",
            )
        return ProbeResult(label, OK, f"{placed} of {decided} placed", proof)


def probe_chain_continuity_from_reports(per_file: dict) -> ProbeResult:
    """`probe_chain_continuity`'s verdict from the streaming pass's own facts.

    The list-based probe re-verifies merged entries and reads a break at every
    point two recorders interleaved as "one chain per recorder". The same three
    verdicts fall out of the per-file reports directly: more than one recorder
    writing is the interleaving case, one file with several chains is the
    restart case, and one file with one unbroken chain is the ledger.
    """
    with_entries = [report for report in per_file.values() if report.count]
    if not with_entries:
        return ProbeResult(
            "Tamper evidence", UNMEASURED, "no entries", "there is nothing to chain yet"
        )
    if len(with_entries) > 1:
        return ProbeResult(
            "Tamper evidence", NOT_BUILT, "one chain per recorder, per run",
            "each recorder writes its own file and starts a fresh chain when it starts, so an "
            "edit inside one run is detected and a whole run removed from a file is not; "
            "the per-file chains are verified by the record probe above",
        )
    only = with_entries[0]
    if only.broken:
        return ProbeResult(
            "Tamper evidence", FAILING, "chain broken", f"{only.path.name}: {only.broken}"
        )
    if only.chains > 1:
        return ProbeResult(
            "Tamper evidence",
            NOT_BUILT,
            f"{only.chains} separate chains",
            "the journal starts again from the genesis digest every time a recorder starts, so "
            "an edit inside one run is detected and a whole run deleted from the file is not; "
            "continuing the chain needs the recorder to read the last digest before it appends",
        )
    return ProbeResult(
        "Tamper evidence",
        OK,
        "one chain, unbroken",
        "every entry follows the digest of the one before it, across every run",
    )


def run_all_probes():
    """Every probe, the trades, and the closed trades -- from one read of the journal.

    One *streaming* read: the recorders write gigabytes a day, and the list of
    every entry this used to build is what OOM-killed the builder at 22 GB on
    2026-08-24 -- a board that cannot build is a stale board wearing a
    timestamp. Each file is streamed once, its chain verified as the entries
    pass, and the entries are merged in recorded order into aggregators that
    keep counts and the trades worth listing rather than the journal itself.
    """
    import heapq

    journal_path = read_journal_path()
    live_from_ns = find_first_live_recorder_start_ns()

    # Stream every recorder's file. The entries are merged in recorded order for
    # the tables, and each file's chain is verified on its own as it streams --
    # those are different questions and answering the second on merged entries
    # reports a break at every point where two recorders interleaved.
    per_file = {path: JournalFileReport(path=path) for path in journal_paths()}
    streams = [
        stream_journal_file(path, report) for path, report in per_file.items()
    ]
    collector = TradeCollector(live_from_ns=live_from_ns, keep_unfilled=False)
    noticed = NoticedScan(live_from_ns)
    freshness = FreshnessScan()
    refused = RefusedDecisionScan()
    closed_entries: list[dict] = []
    for entry in heapq.merge(*streams, key=lambda entry: entry.get("recorded_at_ns", 0)):
        collector.observe(entry)
        noticed.observe(entry)
        freshness.observe(entry)
        refused.observe(entry)
        if entry.get("kind") == CLOSED_TRADE_KIND:
            closed_entries.append(entry)

    unreadable = next(
        (report.problem for report in per_file.values() if report.problem), None
    )
    trades = collector.trades()
    closed = collect_closed_trades(closed_entries)
    running = read_running_parts()
    results = [
        probe_money_mode(),
        probe_trading_half(running),
        probe_parts_alive(),
        probe_feed(read_tape_last_write_seconds()),
        noticed.result(),
        probe_opened(trades, journal_path),
        probe_closed(running),
        probe_journal_chain(per_file, unreadable, journal_path),
        probe_chain_continuity_from_reports(per_file),
        probe_learning(),
        probe_exit_plans(),
        freshness.result(),
        refused.result(),
        probe_stale_price_refusals(read_heartbeat_table_file(heartbeat_table_path())),
    ]
    return results, trades, journal_path, live_from_ns, closed


def as_time(nanoseconds: int | None) -> str:
    if nanoseconds is None:
        return "—"
    return datetime.fromtimestamp(nanoseconds / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def render_tile(result: ProbeResult) -> str:
    return (
        f'<article class="tile {STATE_CLASS[result.state]}">'
        f'<div class="tile-label">{html.escape(result.label)}</div>'
        f'<div class="tile-state">{html.escape(result.state)}</div>'
        f'<div class="tile-value">{html.escape(result.value)}</div>'
        f'<div class="tile-proof">{html.escape(result.proof)}</div>'
        f"</article>"
    )


@dataclass
class ClosedTrade:
    """One round trip as `position-recorder` journalled it.

    Read from the ledger rather than recomputed from fills: the close detector
    resolved which lots the exit closed and what that realised, and a board that
    re-derived it from the same fills would be a second implementation of the same
    arithmetic, free to disagree with the one the system actually acted on.
    """

    venue_id: str | None
    symbol: str | None
    direction: str | None
    quantity: float
    entry_price: float | None
    exit_price: float | None
    realised_pnl: float
    fees_paid: float
    holding_seconds: float | None
    best_unrealised: float | None
    worst_unrealised: float | None
    closed_at_ns: int | None

    @property
    def capital_in_quote(self) -> float | None:
        """What was put into the position, in quote currency, at the entry price."""
        if self.entry_price is None or not self.quantity:
            return None
        return self.entry_price * self.quantity

    @property
    def net_pnl(self) -> float:
        """What the round trip made after the venue was paid for both fills."""
        return self.realised_pnl - self.fees_paid


def collect_closed_trades(entries: list[dict]) -> list[ClosedTrade]:
    """Every round trip the ledger holds, newest first."""
    closed = []
    for entry in entries:
        if entry.get("kind") != CLOSED_TRADE_KIND:
            continue
        payload = entry.get("payload") or {}
        closed.append(ClosedTrade(
            venue_id=payload.get("venue_id"),
            symbol=payload.get("symbol"),
            direction=payload.get("direction"),
            quantity=float(payload.get("quantity") or 0.0),
            entry_price=_as_float(payload.get("entry_price")),
            exit_price=_as_float(payload.get("exit_price")),
            realised_pnl=float(payload.get("realised_pnl") or 0.0),
            fees_paid=float(payload.get("fees_paid") or 0.0),
            holding_seconds=_as_float(payload.get("holding_seconds")),
            best_unrealised=_as_float(payload.get("best_unrealised")),
            worst_unrealised=_as_float(payload.get("worst_unrealised")),
            closed_at_ns=int(entry["recorded_at_ns"]),
        ))
    return sorted(closed, key=lambda trade: trade.closed_at_ns or 0, reverse=True)


def _as_float(value) -> float | None:
    return None if value is None else float(value)


def compose_closed_note(closed: list[ClosedTrade]) -> str:
    if not closed:
        return (
            "Nothing has closed yet. The chain that closes a position is on the live spine "
            "-- the exits rest in the paper book and fill when a live price reaches one of "
            "them -- so an empty table here means no position has yet reached its stop or "
            "its target, not that closing is unbuilt."
        )
    net = sum(trade.net_pnl for trade in closed)
    won = sum(1 for trade in closed if trade.net_pnl > 0)
    listed = min(len(closed), MOST_TRADES_LISTED)
    note = (
        f"{len(closed)} round trip(s) on the ledger, {won} of them profitable after fees, "
        f"{net:+,.4f} in quote currency net."
    )
    if listed < len(closed):
        note += f" The {listed} most recent are listed."
    return note


def render_closed_trades(closed: list[ClosedTrade]) -> str:
    if not closed:
        return (
            '<div class="table-wrap"><table><thead><tr><th>closed trades</th></tr></thead>'
            '<tbody><tr><td class="mono">NOTHING YET</td></tr></tbody></table></div>'
        )
    rows = []
    for trade in closed:
        rows.append(
            "<tr>"
            f'<td class="mono">{html.escape(trade.symbol or "—")}'
            f'<span class="age">{as_time(trade.closed_at_ns)}</span></td>'
            f'<td>{html.escape(trade.direction or "—")}</td>'
            f'<td class="mono">{trade.quantity:g}</td>'
            f'<td class="mono">{f"{trade.entry_price:,.2f}" if trade.entry_price else "—"}</td>'
            f'<td class="mono">{f"{trade.exit_price:,.2f}" if trade.exit_price else "—"}</td>'
            f'<td class="mono">'
            f'{f"{trade.capital_in_quote:,.2f}" if trade.capital_in_quote else "—"}</td>'
            f'<td class="mono">'
            f'{f"{trade.holding_seconds:,.0f}s" if trade.holding_seconds is not None else "—"}</td>'
            f'<td class="mono{profit_class(trade.best_unrealised)}">'
            f'{as_money(trade.best_unrealised)}</td>'
            f'<td class="mono{profit_class(trade.worst_unrealised)}">'
            f'{as_money(trade.worst_unrealised)}</td>'
            f'<td class="mono">{trade.fees_paid:,.4f}</td>'
            f'<td class="mono{profit_class(trade.net_pnl)}">{as_money(trade.net_pnl)}</td>'
            "</tr>"
        )
    return (
        '<div class="table-wrap"><table>'
        "<thead><tr>"
        "<th>symbol</th><th>direction</th><th>quantity</th><th>entry</th><th>exit</th>"
        "<th>capital in (usdt)</th><th>held for</th><th>peak profit</th><th>worst loss</th>"
        "<th>fees</th><th>net</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


# A candidate is not a trade. The detectors journal one every time a setup looks
# interesting -- 1 474 in the first seven minutes of the live run -- and listing
# them as trades would bury the ones that became orders in the ones that did not.
BEFORE_A_TRADE_EXISTS = LIFECYCLE_STAGES[0]


# The most open trades this page lists. A cap that dropped rows silently would be
# a board reporting a smaller book than the one that exists, so what it dropped is
# said in the note above the table.
MOST_TRADES_LISTED = 50


def trades_worth_listing(trades: list[RecordedTrade]) -> list[RecordedTrade]:
    """The open positions: filled, and nothing has closed them.

    A trade the bot decided against, or one whose order never filled, is counted
    above and not listed -- the table is about money that is currently at risk,
    and 129 intents that produced no position would bury the ones that did.

    Nothing closes a position yet, so every filled trade is open. When
    `position-close-detector` runs, a closed one leaves this table and the count
    beside it changes; that is the difference the tile will report.
    """
    filled = [trade for trade in trades if trade.fills > 0]
    return sorted(filled, key=lambda trade: trade.first_recorded_at_ns or 0, reverse=True)


def as_money(amount: float | None) -> str:
    """A quote-currency figure, signed, or an em dash when it could not be computed."""
    if amount is None:
        return "—"
    return f"{amount:+,.2f}"


def profit_class(amount: float | None) -> str:
    if amount is None:
        return ""
    return " up" if amount > 0 else (" down" if amount < 0 else "")


def not_built_cell(column: str, running: dict | None = None) -> str:
    """A column with no value for this trade, saying honestly why there is none.

    Two different reasons, and they were reported as one until 2026-08-23. A part
    that has never been written is a different fact from a part that is running and
    simply published nothing for the trade in this row -- and calling a running
    part "not built" is the board asserting something false about its own system.
    """
    part_id, produces = COLUMNS_A_PART_WOULD_FILL[column]
    state = how_far_a_part_is_built(part_id, running or {})
    if state == "running":
        return (
            f'<td class="gap" title="{html.escape(part_id)} is running; it published no '
            f'{html.escape(produces)} for this trade">none recorded</td>'
        )
    return (
        f'<td class="gap" title="{html.escape(part_id)} would publish {html.escape(produces)} '
        f'({html.escape(state)})">not built</td>'
    )


def price_cell(window: "PriceWindow | None") -> str:
    """The last traded price and how old it is, because a price without its age
    is a number the reader has to trust rather than judge.

    This page is a snapshot: it is generated, then published. The age is measured
    at generation and stated, so a board read an hour later shows an hour-old
    price *saying* it is an hour old, rather than a stale number wearing the
    present tense.
    """
    if window is None:
        return '<td class="mono">—<span class="age">no tape for this symbol today</span></td>'
    age = window.age_seconds
    staleness = "" if age <= TAPE_SILENCE_SECONDS else " stale"
    return (
        f'<td class="mono">{window.last_price:,.2f}'
        f'<span class="age{staleness}">{age:,.0f}s old, {window.trades_seen:,} trades</span></td>'
    )


def with_prices(trades: list[RecordedTrade]) -> list[RecordedTrade]:
    """Attach each trade's price window, read off the tape once per symbol pair.

    Once per (venue, symbol) rather than once per trade: several trades on one
    symbol walk the same records, and the tape is the largest thing on this
    machine.
    """
    windows: dict[tuple[str, str], PriceWindow | None] = {}
    for trade in trades:
        if not trade.venue_id or not trade.symbol:
            continue
        key = (trade.venue_id, trade.symbol)
        since = trade.first_recorded_at_ns
        if key not in windows:
            windows[key] = read_price_window(trade.venue_id, trade.symbol, since)
        trade.prices = windows[key]
    return trades


def render_trades(
    trades: list[RecordedTrade], journal_path: pathlib.Path, running: dict | None = None
) -> str:
    if not trades:
        return (
            '<p class="empty">No position is open. '
            f"{html.escape(str(journal_path))} holds no fill that nothing has closed, which is "
            "what a system that has not traded looks like.</p>"
        )
    rows = []
    for trade in trades:
        entry = trade.average_price
        origin = (
            '<span class="chip live">live run</span>'
            if trade.is_from_a_live_run
            else '<span class="chip test">test run</span>'
        )
        rows.append(
            "<tr>"
            f'<td class="mono">{html.escape(trade.symbol or trade.trade_id)}{origin}'
            f'<span class="age">{as_time(trade.first_recorded_at_ns)}</span></td>'
            f"<td>{html.escape(trade.side or '—')}</td>"
            f'<td class="mono">{trade.quantity:g}</td>'
            f'<td class="mono">{f"{entry:,.2f}" if entry else "—"}</td>'
            f'<td class="mono">'
            f'{f"{trade.capital_in_quote:,.2f}" if trade.capital_in_quote else "—"}</td>'
            + price_cell(trade.prices)
            + f'<td class="mono">{f"{trade.stop_price:,.2f}" if trade.stop_price else "—"}</td>'
            + not_built_cell("target", running)
            + not_built_cell("trailing stop", running)
            + not_built_cell("forecast price", running)
            + f'<td class="mono">'
            f'{f"{trade.conviction:.0%}" if trade.conviction is not None else "—"}</td>'
            f'<td class="mono{profit_class(trade.peak_profit)}">{as_money(trade.peak_profit)}</td>'
            f'<td class="mono{profit_class(trade.worst_loss)}">{as_money(trade.worst_loss)}</td>'
            f'<td class="mono{profit_class(trade.profit_now)}">{as_money(trade.profit_now)}</td>'
            f'<td class="mono">{trade.fees:,.4f}</td>'
            f'<td>{"open" if trade.is_open else "no fill"}'
            f'<span class="age">{html.escape(" → ".join(dict.fromkeys(trade.stages)))}</span></td>'
            "</tr>"
        )
    return (
        '<div class="table-wrap"><table>'
        "<thead><tr>"
        "<th>symbol</th><th>side</th><th>quantity</th><th>entry</th>"
        "<th>capital in (usdt)</th>"
        "<th>price now</th><th>stop</th><th>target</th><th>trailing</th><th>forecast</th>"
        "<th>conviction</th><th>peak profit</th><th>worst loss</th><th>profit now</th>"
        "<th>fees</th><th>state</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


PAGE = """<title>Segment Bots Trade Board</title>
<style>
  :root {{
    --bg: #f7f6f3; --panel: #fff; --ink: #16150f; --muted: #6b675c;
    --line: #e2ded3; --line-strong: #cfc9ba;
    --ok: #1f7a4d; --warn: #a8710f; --fail: #a3311f; --dark: #3f3b32;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg: #14130f; --panel: #1c1b16; --ink: #f2efe6; --muted: #a09a8b;
      --line: #2e2c25; --line-strong: #423f35;
      --ok: #4cbd85; --warn: #d99f3c; --fail: #e0705c; --dark: #cfc9ba;
    }}
  }}
  :root[data-theme="dark"] {{
    --bg: #14130f; --panel: #1c1b16; --ink: #f2efe6; --muted: #a09a8b;
    --line: #2e2c25; --line-strong: #423f35;
    --ok: #4cbd85; --warn: #d99f3c; --fail: #e0705c; --dark: #cfc9ba;
  }}
  body {{
    background: var(--bg); color: var(--ink); margin: 0;
    font-family: "Iowan Old Style", Georgia, serif; line-height: 1.55;
  }}
  .page {{ max-width: 68rem; margin: 0 auto; padding: 2.5rem 1.5rem 4rem; }}
  .eyebrow {{
    font-family: ui-monospace, monospace; font-size: .68rem; letter-spacing: .18em;
    text-transform: uppercase; color: var(--muted);
  }}
  h1 {{ font-size: 2rem; margin: .3rem 0 .6rem; letter-spacing: -.01em; }}
  .verdict {{ margin: 0 0 1rem; color: var(--muted); max-width: 46rem; }}
  .stamp {{
    display: flex; flex-wrap: wrap; gap: 1.2rem;
    font-family: ui-monospace, monospace; font-size: .72rem; color: var(--muted);
    border-top: 1px solid var(--line); padding-top: .8rem;
  }}
  h2 {{ font-size: 1.15rem; margin: 2.4rem 0 .9rem; }}
  .grid {{ display: grid; gap: .8rem; grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr)); }}
  .tile {{
    background: var(--panel); border: 1px solid var(--line);
    border-top: 3px solid var(--line-strong); border-radius: 3px; padding: .9rem 1rem;
  }}
  .tile-label {{ font-weight: 600; font-size: .95rem; }}
  .tile-state {{
    font-family: ui-monospace, monospace; font-size: .64rem; letter-spacing: .12em;
    text-transform: uppercase; margin: .35rem 0 .2rem;
  }}
  .tile-value {{ font-size: 1.05rem; }}
  .tile-proof {{ font-size: .78rem; color: var(--muted); margin-top: .45rem; }}
  .tile.ok {{ border-top-color: var(--ok); }} .tile.ok .tile-state {{ color: var(--ok); }}
  .tile.failing {{ border-top-color: var(--fail); }} .tile.failing .tile-state {{ color: var(--fail); }}
  .tile.not-built {{ border-top-color: var(--dark); border-style: dashed; }}
  .tile.not-built .tile-state {{ color: var(--muted); }}
  .tile.unmeasured {{ border-top-color: var(--muted); border-style: dashed; }}
  .tile.unmeasured .tile-state {{ color: var(--muted); }}
  .tile.waiting {{ border-top-color: var(--warn); }} .tile.waiting .tile-state {{ color: var(--warn); }}
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .86rem; min-width: 52rem; }}
  th {{
    text-align: left; font-family: ui-monospace, monospace; font-size: .66rem;
    letter-spacing: .1em; text-transform: uppercase; color: var(--muted);
    font-weight: 500; padding: .5rem .7rem; border-bottom: 1px solid var(--line-strong);
  }}
  td {{ padding: .55rem .7rem; border-bottom: 1px solid var(--line); color: var(--muted); }}
  td:first-child {{ color: var(--ink); }}
  .mono {{
    font-family: ui-monospace, monospace; font-size: .78rem;
    font-variant-numeric: tabular-nums;
  }}
  .tile-value {{ font-variant-numeric: tabular-nums; }}
  .chip {{
    display: inline-block; margin-left: .5rem; font-family: ui-monospace, monospace;
    font-size: .58rem; letter-spacing: .08em; text-transform: uppercase;
    border: 1px solid currentColor; border-radius: 2px; padding: .08rem .28rem;
  }}
  .age {{
    display: block; font-size: .62rem; letter-spacing: .04em; color: var(--muted);
    margin-top: .15rem;
  }}
  .age.stale {{ color: var(--fail); }}
  .up {{ color: var(--ok); }}
  .down {{ color: var(--fail); }}
  .gap {{
    font-family: ui-monospace, monospace; font-size: .68rem; letter-spacing: .06em;
    text-transform: uppercase; color: var(--muted);
  }}
  .chip.live {{ color: var(--ok); }}
  .chip.test {{ color: var(--muted); }}
  .empty {{ color: var(--muted); border: 1px dashed var(--line-strong); padding: 1rem; border-radius: 3px; }}
  .note {{ color: var(--muted); font-size: .85rem; margin: 0 0 1rem; max-width: 46rem; }}
  footer {{
    margin-top: 3rem; border-top: 1px solid var(--line); padding-top: 1rem;
    font-family: ui-monospace, monospace; font-size: .72rem; color: var(--muted);
    line-height: 1.9; overflow-x: auto;
  }}
</style>

<div class="page">
  <header>
    <div class="eyebrow">ajit-segment-bots</div>
    <h1>Trade Board</h1>
    <p class="verdict">{verdict}</p>
    <div class="stamp">
      <span>measured <strong>{measured_at}</strong></span>
      <span>probes run <strong>{probe_count}</strong></span>
      <span>journal <strong>{journal_path}</strong></span>
    </div>
  </header>

  <section>
    <h2>Probed state</h2>
    <div class="grid">{tiles}</div>
  </section>

  <section>
    <h2>Every trade on the record</h2>
    <p class="note">{listed_note}</p>
    {trades}
  </section>

  <section>
    <h2>Closed trades</h2>
    <p class="note">{closed_note}</p>
    {closed_trades}
  </section>

  <footer>
    Generated by dashboard/build_trade_board.py. Every tile above is a probe this run
    executed; nothing on this page is written by hand, and anything a probe could not
    establish reads as its own state rather than as healthy (Rule 8).<br>
    The trading half was first started live at <strong>{live_from}</strong>.
    Entries recorded before that were written by the integration test, which ran the
    same fourteen parts against a captured price series, and are marked as such.
  </footer>
</div>
"""


def compose_verdict(results: list[ProbeResult], trades: list[RecordedTrade]) -> str:
    failing = [result for result in results if result.state == FAILING]
    if failing:
        return (
            f"{len(failing)} probe(s) failing: "
            + "; ".join(result.label.lower() for result in failing)
            + ". Nothing below is inferred — each is what a file on this machine says."
        )
    live = [trade for trade in trades if trade.is_from_a_live_run and trade.fills]
    closed = [result for result in results if result.label == "Trades closed"]
    if live:
        closing = closed[0].value if closed else "not measured"
        return (
            f"{len(live)} trade(s) opened on the live run. Closing: {closing}. Every number "
            f"below is what a file on this machine says, not what any part asserts."
        )

    # Which gate the bot is actually behind, taken from the probes rather than
    # written down. The first version of this line named the conviction model, and
    # kept naming it after the model had trained -- a verdict that goes stale is a
    # board telling the reader something false with a fresh timestamp on it.
    waiting = [
        result for result in results
        if result.state in (WAITING, UNMEASURED)
        and result.label in ("Learning progress", "Exit plans")
    ]
    if waiting:
        blocker = waiting[0]
        return (
            f"No trade has opened on a live run. The chain that opens one is proven and its "
            f"parts are on; what stands between here and the first trade is "
            f"{blocker.label.lower()} — {blocker.value}. It accrues in real time and cannot "
            f"be caught up on."
        )
    return (
        "No trade has opened on a live run, and nothing measured says why. Every gate the "
        "board can see is passed, which makes the next refusal one no probe covers yet."
    )


def compose_listing_note(trades: list[RecordedTrade]) -> str:
    """What the table shows, and what it leaves out, in the numbers themselves."""
    open_trades = trades_worth_listing(trades)
    decided = [
        trade
        for trade in trades
        if trade.fills == 0 and any(stage != BEFORE_A_TRADE_EXISTS for stage in trade.stages)
    ]
    noticed = len(trades) - len(open_trades) - len(decided)
    note = (
        f"{len(open_trades)} open position(s). Every price, peak and profit below is quote "
        f"currency, computed from the venue's own trades on the tape between the entry and "
        f"the moment this page was generated. "
        f"{len(decided)} decision(s) never filled and {noticed} setup(s) were noticed without "
        f"becoming one; both are counted above rather than listed here."
    )
    if len(open_trades) > MOST_TRADES_LISTED:
        note += (
            f" Only the {MOST_TRADES_LISTED} most recent are listed; "
            f"{len(open_trades) - MOST_TRADES_LISTED} older open position(s) are not shown."
        )
    return note


def build_page() -> str:
    results, trades, journal_path, live_from_ns, closed = run_all_probes()
    return PAGE.format(
        verdict=html.escape(compose_verdict(results, trades)),
        measured_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        probe_count=len(results),
        journal_path=html.escape(str(journal_path)),
        tiles="".join(render_tile(result) for result in results),
        listed_note=html.escape(compose_listing_note(trades)),
        trades=render_trades(
            with_prices(trades_worth_listing(trades)[:MOST_TRADES_LISTED]),
            journal_path,
            read_running_parts(),
        ),
        closed_note=html.escape(compose_closed_note(closed)),
        closed_trades=render_closed_trades(closed[:MOST_TRADES_LISTED]),
        live_from=html.escape(as_time(live_from_ns)) if live_from_ns else "no live run yet",
    )


def main() -> int:
    # The builder walks gigabytes of journal and tape beside a live spine, and
    # its CPU pressure starved regime-classifier into real input loss on
    # 2026-08-24 -- which this board then measured and painted red. A probe must
    # not cause the failure it reports: the build yields to the spine and runs
    # full speed only when the machine is otherwise idle.
    os.nice(19)
    BOARD_PATH.write_text(build_page())
    results, trades, _path, _live, closed = run_all_probes()
    print(
        f"{BOARD_PATH}: {len(results)} probes, {len(trades)} trade(s) on the record, "
        f"{len(closed)} closed"
    )
    for result in results:
        print(f"  {result.state:<13} {result.label}: {result.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
