"""Is this system trading, and if not, what is missing before it could be.

The question the operator actually asks, answered from measurement rather than
from the plan. Today the answer is no, and "no" is a real state with a reason --
not a blank space on the board.

The rule this follows is the one that matters most here: **a tile may never
suggest trading is happening unless a fill has actually been recorded.** A green
"ready" tile on a system that has never placed an order would be the single most
dangerous thing on this board, so readiness is reported as what is built and what
is not, and the trading tile stays NOT BUILT until real orders exist.

Where the state lives, since the Indian pivot (every probe here reads these):

    <position_state_root>/paper-account-keeper.paper-account-<segment>.json
        each built segment's paper account: fills applied, cash, realised, held
    <position_state_root>/position-close-detector.positions.json
        the lot book every open position is closed against
    journal.position-recorder.sqlite
        every position change and every closed trade, hash-chained JSONL

**Until 2026-09-13 two of these tiles read `trading/orders.jsonl` and
`trading/positions.json`**, a crypto-era location nothing has written since the
pivot and which does not exist on this machine. So "Trading" said "no order has
ever been placed" and "Open positions" said "nothing has ever held a position" on
a project with 2,768 paper fills and 15 open option positions -- the same
absence-as-evidence inversion `probe_realised_result` was fixed for the day before.
"""

from __future__ import annotations

import json
import pathlib
import time
from dataclasses import dataclass

from runtime.part_declaration import BLUEPRINT_PATH

OK = "OK"
NOT_BUILT = "NOT BUILT"
FAILING = "FAILING"
NOT_MEASURED = "NOT MEASURED"

# The checkpoints `paper-account-keeper` and `position-close-detector` restore
# from, under the operator's `position_state_root`. The same files the parts act
# on, so the board cannot disagree with the book the bots trade against.
PAPER_ACCOUNT_FILE = "paper-account-keeper.paper-account-{segment}.json"
LOT_BOOK_FILE = "position-close-detector.positions.json"

# Where a realised result really lives (2026-09-12). `position-recorder` writes a
# hash-chained JSONL -- the `.sqlite` extension is historical and wrong -- and a
# `closed-trade` record in it is one finished round trip with its own fees.
#
# **The three paths above are a crypto-era location nothing has written since the
# pivot**, and `TRADING_STATE_DIRECTORY` does not exist on this machine at all.
# `probe_realised_result` therefore reported NOT BUILT with the detail "no fills
# -- nothing has been bought or sold", on a project with 910 closed trades and a
# realised result of about -1,249,500 rupees. Asserting a fact about trading from
# the absence of a file that stopped being written is the exact inversion of
# Rule 8: absence of evidence rendered as evidence of absence.
CLOSED_TRADE_JOURNAL = (
    pathlib.Path.home() / ".local" / "share" / "ajit-segment-bots"
    / "journal.position-recorder.sqlite"
)
# How much of the journal's tail to read. It only grows, and a probe that streams
# a 4 GB rotated journal is a probe nobody runs -- `build_trade_board.py` does
# exactly that and takes ten minutes. The tile says what it counted, so a partial
# read is honest rather than silently partial.
CLOSED_TRADE_TAIL_BYTES = 8 * 1024 * 1024

# The currency a realised result is stated in. Read from the operator's own
# settings where they are readable, because a probe that hardcodes one states a
# result in a currency nobody chose -- this said "USDT" until 2026-09-12, on a
# desk whose `settlement_currency` has read "INR" since the pivot.
def _settlement_currency() -> str:
    try:
        from runtime.settings_reader import load_settings_document, settings_directory

        document = load_settings_document(
            settings_directory() / "runtime.toml", scope="machine"
        )
        return str(document.entries["settlement_currency"].value)
    except Exception:  # noqa: BLE001 -- a probe never fails on its own settings read
        # Named rather than blank: a result with no unit is not a result, and the
        # market this project trades settles in rupees.
        return "INR"


SETTLEMENT_CURRENCY = _settlement_currency()

# The blocks that must be built before an order can be placed at all. Naming
# blocks rather than a count keeps this honest as parts land: the tile says which
# blocks are still empty, so "not trading" carries its own explanation.
BLOCKS_REQUIRED_TO_TRADE = (
    "market-data-feed",
    "opportunity-scanner",
    "ai-brain",
    "risk-capital-allocation",
    "execution-venue-adapter",
    "paper-live-trading",
    "portfolio-state",
    "ledger",
)

# How long since the last fill before a system that *was* trading is called
# stopped. Long enough that a quiet strategy is not reported as broken, short
# enough that a dead execution path is not reported as a quiet strategy.
STALE_TRADING_SECONDS = 3600.0

NANOSECONDS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True)
class TradingProbeResult:
    """One measured fact about trading. Field names match the board's ProbeResult."""

    label: str
    state: str
    value: str
    proof: str


def _built_part_ids() -> set[str]:
    """Which parts have an implementation file, read the way the board reads it."""
    import sys

    project = BLUEPRINT_PATH.parent.parent
    dashboard = project / "dashboard"
    if str(dashboard) not in sys.path:
        sys.path.insert(0, str(dashboard))
    import build_part_monitor

    sources = build_part_monitor.find_source_files()
    registry = json.loads(BLUEPRINT_PATH.read_text())
    return {
        feature["id"]
        for feature in registry["features"]
        if build_part_monitor.find_implementation_file(feature["id"], sources) is not None
    }


def _runtime_settings():
    from runtime.settings_reader import load_settings_document, settings_directory

    return settings_directory(), load_settings_document(
        settings_directory() / "runtime.toml", scope="runtime"
    )


def _position_state_root() -> pathlib.Path:
    _, document = _runtime_settings()
    return pathlib.Path(str(document.read_value("position_state_root"))).expanduser()


def _built_segments_and_money_modes() -> list[tuple[str, str]]:
    """Each segment this spine trades, with the money mode its own file states."""
    from runtime.settings_reader import load_settings_document

    directory, document = _runtime_settings()
    segments = []
    for segment in document.read_value("built_segments"):
        segment_document = load_settings_document(
            directory / "segments" / f"{segment}.toml", scope=f"segment:{segment}"
        )
        segments.append((str(segment), str(segment_document.read_value("money_mode"))))
    return segments


def _newest_position_change_ns() -> int | None:
    """When a position last changed, from the tail of `position-recorder`'s journal."""
    if not CLOSED_TRADE_JOURNAL.exists():
        return None
    newest = None
    size = CLOSED_TRADE_JOURNAL.stat().st_size
    with open(CLOSED_TRADE_JOURNAL, encoding="utf-8", errors="replace") as handle:
        if size > CLOSED_TRADE_TAIL_BYTES:
            handle.seek(size - CLOSED_TRADE_TAIL_BYTES)
            handle.readline()
        for line in handle:
            try:
                changed_at = (json.loads(line).get("payload") or {}).get("updated_at_ns")
            except json.JSONDecodeError:
                continue
            if isinstance(changed_at, int) and (newest is None or changed_at > newest):
                newest = changed_at
    return newest


def probe_trading_state() -> TradingProbeResult:
    """Whether the segment bots have traded, from their own paper accounts.

    Reads the fills each built segment's `paper-account-keeper` has applied, not
    the presence of code. How recently is read from the newest position change in
    `position-recorder`'s journal, and judged against the exchange session asked of
    `market-session-calendar`: a bot that has not traded for an hour while NSE is
    open is STOPPED; one that has not traded since the close is not.
    """
    from runtime.market_session_answer import IN_SESSION, read_the_calendars_live_answer

    try:
        root = _position_state_root()
        segments = _built_segments_and_money_modes()
    except Exception as failure:  # noqa: BLE001 -- unreadable settings are unmeasured
        return TradingProbeResult(
            "Trading", NOT_MEASURED, f"settings unreadable: {type(failure).__name__}", str(failure)
        )

    fills_by_segment: dict[str, int] = {}
    unreadable = []
    for segment, _mode in segments:
        path = root / PAPER_ACCOUNT_FILE.format(segment=segment)
        if not path.exists():
            fills_by_segment[segment] = 0
            continue
        try:
            fills_by_segment[segment] = int(json.loads(path.read_text())["state"]["fills_applied"])
        except (OSError, ValueError, KeyError, TypeError):
            unreadable.append(segment)
    proof = f"fills_applied in {root}/{PAPER_ACCOUNT_FILE}, newest updated_at_ns in {CLOSED_TRADE_JOURNAL}"
    if unreadable and not fills_by_segment:
        return TradingProbeResult(
            "Trading", NOT_MEASURED, f"paper accounts unreadable: {', '.join(unreadable)}", proof
        )

    modes = sorted({mode for _segment, mode in segments})
    mode = "LIVE MONEY" if any(m != "paper" for m in modes) else "paper"
    detail = ", ".join(f"{segment} {count:,}" for segment, count in fills_by_segment.items())
    if unreadable:
        detail += f"; unreadable: {', '.join(unreadable)}"
    total = sum(fills_by_segment.values())
    if total == 0:
        return TradingProbeResult(
            "Trading", NOT_BUILT, f"NOT TRADING -- no segment has applied a fill ({detail})", proof
        )

    newest = _newest_position_change_ns()
    if newest is None:
        return TradingProbeResult(
            "Trading", NOT_MEASURED,
            f"{total:,} fills in {mode} ({detail}), but when the last one was is not readable",
            proof,
        )
    quiet_for = (time.time_ns() - newest) / NANOSECONDS_PER_SECOND
    session, session_proof = read_the_calendars_live_answer()
    summary = f"{total:,} fills in {mode} ({detail}), last position change {quiet_for / 3600:.1f}h ago"
    proof = f"{proof}; session: {session_proof}"
    if quiet_for <= STALE_TRADING_SECONDS:
        return TradingProbeResult("Trading", OK, f"TRADING -- {summary}", proof)
    if session is None:
        return TradingProbeResult(
            "Trading", NOT_MEASURED, f"cannot tell stopped from shut -- {summary}", proof
        )
    if session == IN_SESSION:
        return TradingProbeResult(
            "Trading", FAILING, f"STOPPED while NSE is in session -- {summary}",
            f"{proof}, against {STALE_TRADING_SECONDS / 3600:.0f}h",
        )
    return TradingProbeResult("Trading", OK, f"NSE out of session -- {summary}", proof)


def probe_open_positions() -> TradingProbeResult:
    """What is held right now, from the lot book positions are closed against.

    Nothing held is a state (`flat`), not an absence. Quantities are summed as
    `Decimal` from their strings, the way `position-close-detector` itself sums
    them, so a book the part reads as flat is flat here too.
    """
    from decimal import Decimal, InvalidOperation

    try:
        path = _position_state_root() / LOT_BOOK_FILE
    except Exception as failure:  # noqa: BLE001 -- unreadable settings are unmeasured
        return TradingProbeResult(
            "Open positions", NOT_MEASURED, f"settings unreadable: {type(failure).__name__}", str(failure)
        )
    if not path.exists():
        return TradingProbeResult(
            "Open positions", NOT_BUILT, "no lot book yet -- nothing has opened a position", str(path)
        )
    try:
        checkpoint = json.loads(path.read_text())
        books = checkpoint["state"]["books"]
        held = {
            key: sum((Decimal(str(lot["quantity"])) for lot in lots), Decimal(0))
            for key, lots in books.items()
        }
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation) as failure:
        return TradingProbeResult(
            "Open positions", NOT_MEASURED, f"{type(failure).__name__}: {failure}", str(path)
        )
    age_hours = (time.time_ns() - int(checkpoint.get("saved_at_ns", 0))) / NANOSECONDS_PER_SECOND / 3600
    proof = f"summed lots in {path}, checkpointed {age_hours:.1f}h ago"
    open_positions = sorted(
        ((key.split("|", 1)[-1], quantity) for key, quantity in held.items() if quantity != 0),
        key=lambda entry: entry[0],
    )
    if not open_positions:
        return TradingProbeResult("Open positions", OK, "flat -- no position open", proof)
    shown = ", ".join(f"{symbol} {float(quantity):+,.0f}" for symbol, quantity in open_positions[:5])
    more = f" and {len(open_positions) - 5} more" if len(open_positions) > 5 else ""
    return TradingProbeResult(
        "Open positions", OK, f"{len(open_positions)} open ({shown}{more})", proof
    )


def probe_realised_result() -> TradingProbeResult:
    """What trading has actually produced, net of fees, from closed trades.

    Reads `position-recorder`'s journal rather than the crypto-era
    `trading/fills.jsonl`, which nothing has written since the pivot and which
    does not exist on this machine. Net, never gross: a board showing gross calls
    a fee-eaten loser a winner, which is the same reasoning `/api/trades`
    already carries.
    """
    if not CLOSED_TRADE_JOURNAL.exists():
        return TradingProbeResult(
            "Realised result",
            NOT_MEASURED,
            "no position journal yet -- nothing has recorded a closed trade",
            f"{CLOSED_TRADE_JOURNAL} does not exist",
        )

    realised = 0.0
    counted = 0
    unreadable = 0
    try:
        size = CLOSED_TRADE_JOURNAL.stat().st_size
        with open(CLOSED_TRADE_JOURNAL, encoding="utf-8", errors="replace") as handle:
            if size > CLOSED_TRADE_TAIL_BYTES:
                handle.seek(size - CLOSED_TRADE_TAIL_BYTES)
                handle.readline()  # the partial line the seek landed inside
            for line in handle:
                if '"closed-trade"' not in line:
                    continue
                try:
                    payload = json.loads(line).get("payload") or {}
                except json.JSONDecodeError:
                    unreadable += 1
                    continue
                try:
                    quantity = float(payload["quantity"])
                    entry = float(payload["entry_price"])
                    exit_price = float(payload["exit_price"])
                    fees = float(payload["fees_paid"])
                except (KeyError, TypeError, ValueError):
                    unreadable += 1
                    continue
                direction = 1.0 if payload.get("direction") == "long" else -1.0
                realised += (exit_price - entry) * quantity * direction - fees
                counted += 1
    except OSError as failure:
        return TradingProbeResult(
            "Realised result",
            NOT_MEASURED,
            f"{type(failure).__name__}: {failure}",
            str(CLOSED_TRADE_JOURNAL),
        )

    if counted == 0:
        return TradingProbeResult(
            "Realised result",
            NOT_MEASURED,
            "no closed trade in the journal's tail",
            f"last {CLOSED_TRADE_TAIL_BYTES // (1024 * 1024)} MB of {CLOSED_TRADE_JOURNAL}",
        )
    caveat = f", {unreadable} record(s) unreadable" if unreadable else ""
    return TradingProbeResult(
        "Realised result",
        OK,
        f"{realised:+,.2f} {SETTLEMENT_CURRENCY} net of fees over {counted} closed trade(s){caveat}",
        f"closed-trade records in the last "
        f"{CLOSED_TRADE_TAIL_BYTES // (1024 * 1024)} MB of {CLOSED_TRADE_JOURNAL}",
    )


def probe_readiness_to_trade() -> TradingProbeResult:
    """Which blocks are still empty before an order could be placed at all.

    Deliberately never green on its own. It answers "how far off is trading",
    and a tile that went green for having the code would be read as "it is
    trading", which is what `probe_trading_state` alone may say.
    """
    try:
        built = _built_part_ids()
        registry = json.loads(BLUEPRINT_PATH.read_text())
    except Exception as failure:
        return TradingProbeResult(
            "Ready to trade",
            NOT_MEASURED,
            f"could not read what is built: {type(failure).__name__}: {failure}",
            str(BLUEPRINT_PATH),
        )

    by_block: dict[str, list[str]] = {}
    for feature in registry["features"]:
        by_block.setdefault(feature["category"], []).append(feature["id"])

    missing = []
    complete = []
    for block in BLOCKS_REQUIRED_TO_TRADE:
        parts = by_block.get(block, [])
        built_here = sum(1 for part_id in parts if part_id in built)
        if built_here == len(parts) and parts:
            complete.append(block)
        else:
            missing.append(f"{block} {built_here}/{len(parts)}")

    if missing:
        return TradingProbeResult(
            "Ready to trade",
            NOT_BUILT,
            f"no -- {len(complete)} of {len(BLOCKS_REQUIRED_TO_TRADE)} blocks complete; needs {'; '.join(missing)}",
            f"implementation files under the project, against {BLUEPRINT_PATH.name}",
        )
    return TradingProbeResult(
        "Ready to trade",
        OK,
        f"every block an order needs is built ({len(complete)} of {len(BLOCKS_REQUIRED_TO_TRADE)})",
        f"implementation files under the project, against {BLUEPRINT_PATH.name}",
    )


TRADING_PROBES = (
    probe_trading_state,
    probe_open_positions,
    probe_realised_result,
    probe_readiness_to_trade,
)


def run_all_trading_probes() -> list[TradingProbeResult]:
    """Every trading fact, with a crashed probe reported rather than missing."""
    results: list[TradingProbeResult] = []
    for probe in TRADING_PROBES:
        try:
            results.append(probe())
        except Exception as failure:
            results.append(
                TradingProbeResult(
                    probe.__name__.replace("probe_", "").replace("_", " ").capitalize(),
                    NOT_MEASURED,
                    f"probe raised {type(failure).__name__}: {failure}",
                    f"runtime.probes.trading_probes.{probe.__name__}",
                )
            )
    return results
