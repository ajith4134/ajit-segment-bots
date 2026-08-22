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
import pathlib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

PROJECT_HOME = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT_HOME) not in sys.path:
    sys.path.insert(0, str(PROJECT_HOME))

from parts.ledger.trade_lifecycle_recorder import (  # noqa: E402
    LIFECYCLE_STAGES,
    REPEATABLE_STAGES,
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

# What a trade needs before it can close, and none of it is written. Listed by
# part id with what it would produce, so the board names the work rather than
# leaving a dark tile nobody can act on.
CLOSING_CHAIN = (
    ("fill-reconciler", "position"),
    ("peak-excursion-tracker", "peak-excursion"),
    ("stop-target-placer", "stop-target-plan"),
    ("exit-order-chainer", "stop-adjustment"),
    ("stop-order-manager", "order-request"),
    ("position-close-detector", "closed-trade"),
)

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
    # True only when a fill of this trade was recorded after the trading half was
    # first started live. A trade nobody filled is not an opened trade, and a test
    # run's fill does not become one because a detector saw the symbol again.
    is_from_a_live_run: bool = False

    @property
    def average_price(self) -> float | None:
        return self.notional / self.quantity if self.quantity else None

    @property
    def is_open(self) -> bool:
        """Filled and never closed. Every trade is open: nothing can close one yet."""
        return self.fills > 0


def read_journal_path() -> pathlib.Path:
    """Where the operator's settings say the journal is written."""
    document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    return pathlib.Path(str(document.read_value("journal_path"))).expanduser()


def read_journal_entries(path: pathlib.Path) -> tuple[list[dict], str | None]:
    """Every entry on the journal, and the first line that would not parse.

    A line that cannot be read is returned rather than skipped: a ledger with a
    hole in it must not render as a ledger with fewer trades.
    """
    if not path.exists():
        return [], None
    entries = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as failure:
            return entries, f"line {number} of {path} is not readable: {failure}"
    return entries, None


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
    furthest: dict[str, int] = {}
    open_trade: dict[str, RecordedTrade] = {}
    collected: list[RecordedTrade] = []

    for entry in entries:
        payload = entry.get("payload") or {}
        trade_id = payload.get("trade_id")
        if trade_id is None or entry["kind"] not in LIFECYCLE_STAGES:
            continue
        position = LIFECYCLE_STAGES.index(entry["kind"])
        recorded_at = int(entry["recorded_at_ns"])

        # A repeated stage ends the group unless the recorder allows it to repeat:
        # a fill may arrive more than once for one order, because a partial fill is
        # ordinary, and splitting there would count one trade as two.
        repeats_legitimately = (
            position == furthest.get(trade_id, -1) and entry["kind"] in REPEATABLE_STAGES
        )
        starts_a_new_trade = (
            trade_id not in open_trade
            or (position <= furthest.get(trade_id, -1) and not repeats_legitimately)
        )
        if starts_a_new_trade:
            trade = RecordedTrade(trade_id=trade_id)
            open_trade[trade_id] = trade
            collected.append(trade)
            furthest[trade_id] = position
        else:
            trade = open_trade[trade_id]
            furthest[trade_id] = max(furthest[trade_id], position)

        trade.stages.append(entry["kind"])
        if trade.first_recorded_at_ns is None:
            trade.first_recorded_at_ns = recorded_at
        trade.last_recorded_at_ns = recorded_at
        trade.venue_id = payload.get("venue_id") or trade.venue_id
        trade.symbol = payload.get("symbol") or trade.symbol
        trade.side = payload.get("side") or trade.side
        if entry["kind"] == "fill":
            trade.fills += 1
            quantity = float(payload.get("quantity") or 0.0)
            price = float(payload.get("price") or 0.0)
            trade.quantity += quantity
            trade.notional += quantity * price
            trade.fees += float(payload.get("fee") or 0.0)
            if live_from_ns is not None and recorded_at >= live_from_ns:
                trade.is_from_a_live_run = True

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
    if live_from_ns is None:
        return ProbeResult(
            "Setups noticed", NOT_BUILT, "no live run yet", f"no spine including {RECORDER} in {SUPERVISOR_LOG}"
        )
    candidates = [
        entry
        for entry in entries
        if entry["kind"] == "entry-candidate" and int(entry["recorded_at_ns"]) >= live_from_ns
    ]
    if not candidates:
        return ProbeResult(
            "Setups noticed",
            WAITING,
            "none on this run",
            "no entry-candidate has been journalled since the trading half was started",
        )
    newest = max(int(entry["recorded_at_ns"]) for entry in candidates)
    symbols = {entry["payload"].get("symbol") for entry in candidates}
    return ProbeResult(
        "Setups noticed",
        OK,
        f"{len(candidates)} on {len(symbols)} symbols",
        f"entry-candidate entries journalled since the live run began; most recent "
        f"{as_time(newest)} UTC",
    )


def probe_closed() -> ProbeResult:
    """Nothing can close a trade yet, and the board says which parts that needs."""
    return ProbeResult(
        "Trades closed",
        NOT_BUILT,
        f"{len(CLOSING_CHAIN)} parts unwritten",
        "a close needs "
        + ", ".join(f"{part_id} → {produces}" for part_id, produces in CLOSING_CHAIN),
    )


def probe_journal_chain(entries: list[dict], unreadable: str | None, path: pathlib.Path) -> ProbeResult:
    if unreadable:
        return ProbeResult("The record", FAILING, "unreadable", unreadable)
    if not entries:
        return ProbeResult("The record", WAITING, "empty", f"{path} holds no entries")
    chains, broken = verify_journal_chain(entries)
    if broken:
        return ProbeResult("The record", FAILING, "chain broken", broken)
    return ProbeResult(
        "The record",
        OK,
        f"{len(entries)} entries in {chains} chain(s)",
        f"every digest in {path} recomputed from its own content and its predecessor",
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
        return ProbeResult("Tamper evidence", FAILING, "chain broken", broken)
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
    """The one thing this board cannot see, said as plainly as the rest.

    The bull bot forms no opinion until its conviction model has trained on
    labels, and nothing writes that count anywhere: it lives in the model's own
    process. So "how far from the first decision" is unmeasured, and the board
    says so rather than implying the wait is short or long.
    """
    return ProbeResult(
        "Learning progress",
        UNMEASURED,
        "no probe exists",
        "bull-conviction-model holds its training count in memory and publishes it nowhere; "
        "measuring it needs the learned parts to write their state where a probe can read it, "
        "which would also make it survive a restart",
    )


def run_all_probes() -> tuple[list[ProbeResult], list[RecordedTrade], pathlib.Path, int | None]:
    journal_path = read_journal_path()
    entries, unreadable = read_journal_entries(journal_path)
    live_from_ns = find_first_live_recorder_start_ns()
    trades = collect_trades(entries, live_from_ns)
    running = read_running_parts()
    results = [
        probe_money_mode(),
        probe_trading_half(running),
        probe_feed(read_tape_last_write_seconds()),
        probe_noticed(entries, live_from_ns),
        probe_opened(trades, journal_path),
        probe_closed(),
        probe_journal_chain(entries, unreadable, journal_path),
        probe_chain_continuity(entries),
        probe_learning(),
    ]
    return results, trades, journal_path, live_from_ns


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


# A candidate is not a trade. The detectors journal one every time a setup looks
# interesting -- 1 474 in the first seven minutes of the live run -- and listing
# them as trades would bury the ones that became orders in the ones that did not.
BEFORE_A_TRADE_EXISTS = LIFECYCLE_STAGES[0]


def trades_worth_listing(trades: list[RecordedTrade]) -> list[RecordedTrade]:
    """The trades that got past being noticed: an intent was formed about them."""
    return [
        trade
        for trade in trades
        if any(stage != BEFORE_A_TRADE_EXISTS for stage in trade.stages)
    ]


def render_trades(trades: list[RecordedTrade], journal_path: pathlib.Path) -> str:
    if not trades:
        return (
            '<p class="empty">No trade has been recorded. '
            f"{html.escape(str(journal_path))} holds no entry carrying a trade id, which is "
            "what a system that has not traded looks like.</p>"
        )
    rows = []
    for trade in trades:
        price = trade.average_price
        origin = (
            '<span class="chip live">live run</span>'
            if trade.is_from_a_live_run
            else '<span class="chip test">test run</span>'
        )
        rows.append(
            "<tr>"
            f'<td class="mono">{html.escape(trade.trade_id)}{origin}</td>'
            f"<td>{html.escape(trade.side or '—')}</td>"
            f'<td class="mono">{trade.quantity:g}</td>'
            f'<td class="mono">{f"{price:,.2f}" if price else "—"}</td>'
            f'<td class="mono">{trade.fees:,.4f}</td>'
            f'<td class="mono">{as_time(trade.first_recorded_at_ns)}</td>'
            f'<td class="mono">{html.escape(" → ".join(trade.stages))}</td>'
            f'<td>{"open" if trade.is_open else "no fill"}</td>'
            "</tr>"
        )
    return (
        '<div class="table-wrap"><table>'
        "<thead><tr><th>trade</th><th>side</th><th>quantity</th><th>average price</th>"
        "<th>fees</th><th>first recorded (UTC)</th><th>stages journalled</th><th>state</th>"
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
    if live:
        return (
            f"{len(live)} trade(s) opened on the live run. None can close yet: the six parts "
            f"that turn an exit plan into a closing order are not written."
        )
    return (
        "No trade has opened on a live run. The chain that opens one is proven and the parts "
        "are on, and what stands between here and the first trade is the bull bot's own "
        "conviction model, which forms no opinion until it has been trained on labels that "
        "accrue in real time."
    )


def build_page() -> str:
    results, trades, journal_path, live_from_ns = run_all_probes()
    return PAGE.format(
        verdict=html.escape(compose_verdict(results, trades)),
        measured_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        probe_count=len(results),
        journal_path=html.escape(str(journal_path)),
        tiles="".join(render_tile(result) for result in results),
        listed_note=html.escape(
            f"{len(trades_worth_listing(trades))} of {len(trades)} things the journal holds got "
            f"past being noticed. The rest are entry candidates: a detector saying a setup looks "
            f"interesting, which is not a trade and is counted above rather than listed here."
        ),
        trades=render_trades(trades_worth_listing(trades), journal_path),
        live_from=html.escape(as_time(live_from_ns)) if live_from_ns else "no live run yet",
    )


def main() -> int:
    BOARD_PATH.write_text(build_page())
    results, trades, _path, _live = run_all_probes()
    print(f"{BOARD_PATH}: {len(results)} probes, {len(trades)} trade(s) on the record")
    for result in results:
        print(f"  {result.state:<13} {result.label}: {result.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
