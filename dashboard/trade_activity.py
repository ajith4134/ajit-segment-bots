#!/usr/bin/env python3
"""What the bot is holding and what it has closed, cheaply enough to poll.

`build_trade_board.py` answers the same question by streaming both journals end
to end. That is the right thing for a page built once -- it can prove the digest
chain and attribute every entry -- and the wrong thing for a live board: the
journals are gigabytes and growing, and one build takes the better part of ten
minutes. A view that costs ten minutes is a view nobody refreshes.

So this reads two much smaller things:

**Open positions come from `position-close-detector`'s own checkpoint.** That is
the file the part restores from -- the same lots the bot is actually acting on,
not a second copy kept for a board, which would be free to disagree with it. It
is a few kilobytes however long the run has been.

**Closed trades come from the tail of the position journal.** A board shows the
recent ones; reading the whole file to render twenty rows would be the expensive
mistake again. The tail is bounded in bytes and the count actually found is
reported, so a reader is never told "these are all of them" when they are the
last few.

Current prices come off the tape, per held symbol, through that venue's own
adapter -- so a mark-to-market is the venue's number and carries the age of the
print it came from. A price with no time beside it is a number a reader has to
trust; a price with one is a number they can judge.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
from dataclasses import dataclass

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parent
for path in (str(PROJECT), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

NOT_MEASURED = "NOT MEASURED"

# How much of the journal's end to read for closed trades. Bounded because the
# file is gigabytes: 16 MiB is a few thousand entries at the observed entry size,
# far more than any board renders, and a fixed cost whatever the journal grows to.
CLOSED_TRADE_TAIL_BYTES = 16 * 1024 * 1024

# How many closed trades to hand back. The tail may hold many more.
MOST_CLOSED_TRADES = 100


@dataclass(frozen=True)
class JournalTail:
    """The last stretch of a journal, and how much of it that was."""

    entries: list[dict]
    bytes_read: int
    file_bytes: int
    is_whole_file: bool


def read_journal_tail(path: pathlib.Path, kind: str, tail_bytes: int) -> JournalTail:
    """Entries of one kind from the end of a JSONL journal.

    The first line of the tail is almost always a fragment of an entry, so it is
    dropped rather than guessed at -- unless the tail is the whole file, in which
    case the first line is a real one and dropping it would lose an entry.
    """
    if not path.exists():
        return JournalTail(entries=[], bytes_read=0, file_bytes=0, is_whole_file=True)

    file_bytes = path.stat().st_size
    start = max(0, file_bytes - tail_bytes)
    with open(path, "rb") as handle:
        handle.seek(start)
        blob = handle.read()

    lines = blob.split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]

    entries = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if entry.get("kind") == kind:
            entries.append(entry)
    return JournalTail(
        entries=entries,
        bytes_read=len(blob),
        file_bytes=file_bytes,
        is_whole_file=start == 0,
    )


def read_state_directory() -> pathlib.Path:
    from build_trade_board import STATE_DIRECTORY

    return STATE_DIRECTORY


def read_open_positions() -> tuple[list[dict], dict]:
    """What the bot holds, from the checkpoint the closing part restores from."""
    from runtime.lot_book_checkpoint import book_key_of

    try:
        from runtime.settings_reader import load_settings_document, settings_directory

        document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
        root = pathlib.Path(str(document.read_value("position_state_root"))).expanduser()
    except Exception as refusal:
        return [], {"ok": False, "proof": f"settings refused position_state_root ({refusal})"}

    path = root / "position-close-detector.positions.json"
    if not path.exists():
        return [], {
            "ok": False,
            # Never "no open positions" -- a part that has not written a checkpoint
            # and a part holding nothing are different facts (Rule 8).
            "proof": (
                f"no checkpoint at {path}: position-close-detector has not written one, "
                "which is a different fact from holding nothing"
            ),
        }

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as failure:
        return [], {"ok": False, "proof": f"{path} could not be read: {failure}"}

    state = document.get("state") or {}
    books = state.get("books") or {}
    fees = state.get("fees") or {}
    opened_at = state.get("opened_at") or {}
    direction = state.get("direction") or {}
    entry_cost = state.get("entry_cost") or {}
    entered = state.get("entered_quantity") or {}
    excursion = state.get("excursion") or {}

    positions = []
    for key, lots in sorted(books.items()):
        venue_id, symbol = book_key_of(key)
        quantity = sum(float(lot["quantity"]) for lot in lots)
        if quantity <= 0:
            continue
        cost = float(entry_cost.get(key, 0.0))
        entered_quantity = float(entered.get(key, 0.0) or 0.0)
        best, worst = (excursion.get(key) or [None, None])[:2]
        positions.append(
            {
                "venue_id": venue_id,
                "symbol": symbol,
                "direction": direction.get(key),
                "quantity": quantity,
                "entry_price": cost / entered_quantity if entered_quantity else None,
                "capital_in": cost,
                "fees_paid": float(fees.get(key, 0.0)),
                "opened_at_ns": int(opened_at.get(key) or 0) or None,
                "lots": len(lots),
                "best_unrealised": best,
                "worst_unrealised": worst,
            }
        )

    return positions, {
        "ok": True,
        "proof": (
            f"{path}, written {(time.time_ns() - int(document.get('saved_at_ns') or 0)) / 1e9:.0f}s "
            f"ago -- the same file position-close-detector restores from"
        ),
        "saved_at_ns": document.get("saved_at_ns"),
    }


def attach_live_prices(positions: list[dict]) -> None:
    """Mark each held position against the tape, in place.

    Per held symbol, so the cost is the number of open positions rather than the
    size of the universe. A symbol the tape cannot answer for is left as
    `NOT MEASURED` rather than marked at its entry price, which would render a
    losing position as flat.
    """
    from build_trade_board import read_price_window

    for position in positions:
        try:
            window = read_price_window(
                position["venue_id"], position["symbol"], position.get("opened_at_ns")
            )
        except Exception:
            window = None
        if window is None:
            position["price_now"] = None
            position["price_age_seconds"] = None
            position["unrealised_pnl"] = None
            position["price_proof"] = f"{NOT_MEASURED}: the tape has no record for this symbol today"
            continue
        entry = position.get("entry_price")
        is_short = position.get("direction") == "short"
        moved = None
        if entry:
            moved = (entry - window.last_price) if is_short else (window.last_price - entry)
        position["price_now"] = window.last_price
        position["price_age_seconds"] = window.age_seconds
        position["highest_since_open"] = window.highest
        position["lowest_since_open"] = window.lowest
        position["trades_seen"] = window.trades_seen
        position["unrealised_pnl"] = None if moved is None else moved * position["quantity"]
        position["price_proof"] = (
            f"tape, {window.trades_seen} trade(s) since this position opened, "
            f"last print {window.age_seconds:.0f}s ago"
        )


def read_closed_trades() -> tuple[list[dict], dict]:
    """Recent round trips, from the end of the position journal."""
    journal = read_state_directory() / "journal.position-recorder.sqlite"
    tail = read_journal_tail(journal, "closed-trade", CLOSED_TRADE_TAIL_BYTES)

    trades = []
    for entry in tail.entries[-MOST_CLOSED_TRADES:]:
        payload = entry.get("payload") or {}
        realised = payload.get("realised_pnl")
        fees = payload.get("fees_paid")
        trades.append(
            {
                "venue_id": payload.get("venue_id"),
                "symbol": payload.get("symbol"),
                "direction": payload.get("direction"),
                "quantity": payload.get("quantity"),
                "entry_price": payload.get("entry_price"),
                "exit_price": payload.get("exit_price"),
                "realised_pnl": realised,
                "fees_paid": fees,
                # What the trade actually made. Gross minus fees, because a board
                # showing gross would call a fee-eaten loser a winner.
                "net_pnl": None if realised is None or fees is None else realised - fees,
                "holding_seconds": payload.get("holding_seconds"),
                "best_unrealised": payload.get("best_unrealised"),
                "worst_unrealised": payload.get("worst_unrealised"),
                "closed_at_ns": payload.get("closed_at_ns"),
                "opened_at_ns": payload.get("opened_at_ns"),
            }
        )
    trades.reverse()

    if not journal.exists():
        proof = f"no journal at {journal}: nothing has recorded a position yet"
    elif tail.is_whole_file:
        proof = f"{journal} read whole ({tail.file_bytes / 1024 ** 2:.0f} MiB)"
    else:
        proof = (
            f"the last {tail.bytes_read / 1024 ** 2:.0f} MiB of {journal} "
            f"({tail.file_bytes / 1024 ** 3:.1f} GiB): {len(tail.entries)} closed trade(s) in that "
            f"stretch, of which the newest {len(trades)} are shown. Older ones are in the journal, "
            f"not on this board"
        )
    return trades, {"ok": journal.exists(), "proof": proof, "is_whole_file": tail.is_whole_file}


def summarise_closed(trades: list[dict]) -> dict:
    """What the shown trades add up to. Said of the shown ones, never of all time."""
    scored = [t for t in trades if t["net_pnl"] is not None]
    if not scored:
        return {"count": 0, "net_pnl": None, "wins": 0, "win_rate": None, "fees_paid": None}
    wins = [t for t in scored if t["net_pnl"] > 0]
    return {
        "count": len(scored),
        "net_pnl": sum(t["net_pnl"] for t in scored),
        "gross_pnl": sum(t["realised_pnl"] for t in scored),
        "fees_paid": sum(t["fees_paid"] for t in scored),
        "wins": len(wins),
        "win_rate": len(wins) / len(scored),
    }


def build_trade_activity(with_prices: bool = True) -> dict:
    positions, position_provenance = read_open_positions()
    if with_prices and positions:
        attach_live_prices(positions)
    closed, closed_provenance = read_closed_trades()
    return {
        "generated_at_ns": time.time_ns(),
        "open": {
            "positions": positions,
            "count": len(positions),
            "capital_in": sum(p["capital_in"] for p in positions),
            "unrealised_pnl": sum(
                p["unrealised_pnl"] for p in positions if p.get("unrealised_pnl") is not None
            ) if any(p.get("unrealised_pnl") is not None for p in positions) else None,
            "provenance": position_provenance,
        },
        "closed": {
            "trades": closed,
            "summary": summarise_closed(closed),
            "provenance": closed_provenance,
        },
    }


if __name__ == "__main__":
    activity = build_trade_activity()
    open_side, closed_side = activity["open"], activity["closed"]
    print(f"OPEN {open_side['count']}  ({open_side['provenance']['proof']})")
    for position in open_side["positions"]:
        price = position.get("price_now")
        print(f"  {position['symbol']:<14} {position['direction'] or '?':<6} "
              f"qty {position['quantity']:<12.6g} entry {position['entry_price'] or 0:<12.6g} "
              f"now {price if price is not None else NOT_MEASURED}")
    summary = closed_side["summary"]
    print(f"\nCLOSED {summary['count']} shown  net {summary['net_pnl']}  "
          f"win rate {summary['win_rate']}")
    print(f"  {closed_side['provenance']['proof']}")
    for trade in closed_side["trades"][:8]:
        print(f"  {trade['symbol']:<14} {trade['direction'] or '?':<6} "
              f"net {trade['net_pnl']:>8.3f}  held {trade['holding_seconds'] or 0:>7.1f}s")
