"""Is this system trading, and if not, what is missing before it could be.

The question the operator actually asks, answered from measurement rather than
from the plan. Today the answer is no, and "no" is a real state with a reason --
not a blank space on the board.

The rule this follows is the one that matters most here: **a tile may never
suggest trading is happening unless a fill has actually been recorded.** A green
"ready" tile on a system that has never placed an order would be the single most
dangerous thing on this board, so readiness is reported as what is built and what
is not, and the trading tile stays NOT BUILT until real orders exist.

Where the state lives:

    ~/.local/share/ajit-segment-bots/trading/orders.jsonl    every order sent
    ~/.local/share/ajit-segment-bots/trading/fills.jsonl     every fill received
    ~/.local/share/ajit-segment-bots/trading/positions.json  what is held now

None of those exist yet, and that absence is what the probes report.
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

TRADING_STATE_DIRECTORY = pathlib.Path.home() / ".local" / "share" / "ajit-segment-bots" / "trading"
ORDERS_PATH = TRADING_STATE_DIRECTORY / "orders.jsonl"
FILLS_PATH = TRADING_STATE_DIRECTORY / "fills.jsonl"
POSITIONS_PATH = TRADING_STATE_DIRECTORY / "positions.json"

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


def _count_lines(path: pathlib.Path) -> int | None:
    try:
        with open(path, "rb") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return None


def _last_record(path: pathlib.Path) -> dict | None:
    """The last complete JSON object in a JSONL file, read from its tail."""
    try:
        size = path.stat().st_size
        if size == 0:
            return None
        with open(path, "rb") as handle:
            window = min(size, 64 * 1024)
            handle.seek(size - window)
            tail = handle.read(window)
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(record, dict):
            return record
    return None


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


def probe_trading_state() -> TradingProbeResult:
    """Whether an order has ever actually been placed by this system.

    Reads the record of orders, not the presence of code. Code that can place an
    order and has not is not trading, and the difference is the whole question.
    """
    if not ORDERS_PATH.exists():
        return TradingProbeResult(
            "Trading",
            NOT_BUILT,
            "NOT TRADING -- no order has ever been placed",
            f"{ORDERS_PATH} does not exist",
        )

    orders = _count_lines(ORDERS_PATH)
    if orders is None:
        return TradingProbeResult(
            "Trading", NOT_MEASURED, "the order record could not be read", str(ORDERS_PATH)
        )
    if orders == 0:
        return TradingProbeResult(
            "Trading", NOT_BUILT, "NOT TRADING -- the order record is empty", str(ORDERS_PATH)
        )

    last = _last_record(ORDERS_PATH) or {}
    placed_at = last.get("placed_at_ns")
    if not isinstance(placed_at, int):
        return TradingProbeResult(
            "Trading",
            NOT_MEASURED,
            f"{orders} orders recorded, none carrying a timestamp",
            str(ORDERS_PATH),
        )

    quiet_for = (time.time_ns() - placed_at) / NANOSECONDS_PER_SECOND
    mode = "paper" if last.get("is_paper", True) else "LIVE MONEY"
    if quiet_for > STALE_TRADING_SECONDS:
        return TradingProbeResult(
            "Trading",
            FAILING,
            f"STOPPED -- {orders} orders in {mode}, last one {quiet_for / 3600:.1f}h ago",
            f"last order in {ORDERS_PATH}, against {STALE_TRADING_SECONDS / 3600:.0f}h",
        )
    return TradingProbeResult(
        "Trading",
        OK,
        f"TRADING in {mode} -- {orders} orders, last {quiet_for:.0f}s ago",
        f"last order in {ORDERS_PATH}",
    )


def probe_open_positions() -> TradingProbeResult:
    """What is held right now. Nothing held is a state, not an absence."""
    if not POSITIONS_PATH.exists():
        return TradingProbeResult(
            "Open positions",
            NOT_BUILT,
            "none -- nothing has ever held a position",
            f"{POSITIONS_PATH} does not exist",
        )
    try:
        positions = json.loads(POSITIONS_PATH.read_text())
    except (OSError, json.JSONDecodeError) as failure:
        return TradingProbeResult(
            "Open positions", NOT_MEASURED, f"{type(failure).__name__}: {failure}", str(POSITIONS_PATH)
        )
    open_positions = [p for p in positions if p.get("quantity")]
    if not open_positions:
        return TradingProbeResult(
            "Open positions", OK, "flat -- no position open", str(POSITIONS_PATH)
        )
    detail = ", ".join(f"{p['symbol']} {p['quantity']:+g}" for p in open_positions[:5])
    return TradingProbeResult(
        "Open positions", OK, f"{len(open_positions)} open ({detail})", str(POSITIONS_PATH)
    )


def probe_realised_result() -> TradingProbeResult:
    """What trading has actually produced, in USDT, from recorded fills."""
    if not FILLS_PATH.exists():
        return TradingProbeResult(
            "Realised result",
            NOT_BUILT,
            "no fills -- nothing has been bought or sold",
            f"{FILLS_PATH} does not exist",
        )
    fills = _count_lines(FILLS_PATH)
    if fills is None:
        return TradingProbeResult(
            "Realised result", NOT_MEASURED, "the fill record could not be read", str(FILLS_PATH)
        )
    if fills == 0:
        return TradingProbeResult(
            "Realised result", NOT_BUILT, "the fill record is empty", str(FILLS_PATH)
        )

    realised = 0.0
    counted = 0
    try:
        with open(FILLS_PATH, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                if isinstance(record.get("realised_pnl_usdt"), (int, float)):
                    realised += float(record["realised_pnl_usdt"])
                    counted += 1
    except (OSError, json.JSONDecodeError) as failure:
        return TradingProbeResult(
            "Realised result", NOT_MEASURED, f"{type(failure).__name__}: {failure}", str(FILLS_PATH)
        )
    if counted == 0:
        return TradingProbeResult(
            "Realised result",
            NOT_MEASURED,
            f"{fills} fills recorded, none carrying a realised result",
            str(FILLS_PATH),
        )
    return TradingProbeResult(
        "Realised result",
        OK,
        f"{realised:+,.2f} USDT over {counted} of {fills} fills",
        f"summed realised_pnl_usdt in {FILLS_PATH}",
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
