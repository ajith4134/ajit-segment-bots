#!/usr/bin/env python3
"""Bring open positions back under `maximum_capital_per_trade`, at real prices.

    .venv/bin/python operate/trim_positions_to_the_capital_ceiling.py --dry-run
    .venv/bin/python operate/trim_positions_to_the_capital_ceiling.py

**Why this exists.** `trade-capital-bounds-gate` capped one *order* at the
operator's ceiling and never saw the position it was adding to, so adds stacked:
measured 2026-09-07, six of ten open positions stood above the 200,000 rupee
ceiling and one at **6.4 times** it, Rs 1,277,445 in BHARTIARTL 1860 PE across
214 lots. The gate reads `position` now and bounds what an order would *make* the
position, so this cannot recur -- but the positions already open were built before
that and would carry the breach into the next session.

**A one-off, not a part.** Nothing here belongs on the spine: it repairs state a
defect produced, the way `operate/seed_reconciler_from_the_lot_book.py` and
`operate/repair_interleaved_tape.py` did before it. Run it with the spine
stopped, or the running detector will checkpoint over the result.

**Every price is a real print (RL-063).** A trim is a close, and a close has a
price. Using the entry price would invent a flat outcome and using a made-up mark
would invent a profit, so each removal is priced at the contract's own **last
traded price on this project's tape**, with the moment it printed recorded beside
it. A contract with no print on the tape is left alone and reported, because the
alternative is closing it at a number nobody measured.

**Oldest lots go first**, which is `LotBook.take`'s own FIFO rule -- the same
order a real partial close would consume, so what is left is what would have been
left had the gate bounded the position all along.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from runtime.lot_book_checkpoint import book_key_of
from runtime.settings_reader import load_settings_document, settings_directory
from runtime.tape import read_payload, read_tape_index

LOT_BOOK_FILES = (
    "position-close-detector.positions.json",
    "cost-basis-tracker.lots.json",
)
TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
MASTER = pathlib.Path.home() / ".local/share/ajit-segment-bots/instrument-master/complete.json.gz"


def position_state_root() -> pathlib.Path:
    document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    return pathlib.Path(str(document.read_value("position_state_root"))).expanduser()


def ceiling_per_segment() -> dict[str, float]:
    """Every built segment's own `maximum_capital_per_trade`."""
    runtime = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    main = load_settings_document(settings_directory() / "main-account.toml", "main-account")
    account_ceiling = float(main.read_value("maximum_capital_per_trade"))
    ceilings = {}
    for segment in runtime.read_value("built_segments"):
        document = load_settings_document(
            settings_directory() / "segments" / f"{segment}.toml", str(segment)
        )
        # The tighter of the two binds, exactly as `capital-allotment-reader`
        # computes it -- a trim to a looser bound would leave the real one broken.
        ceilings[str(segment)] = min(
            float(document.read_value("maximum_capital_per_trade")), account_ceiling
        )
    return ceilings


def last_traded_price(symbol: str, day: str) -> tuple[float, int] | None:
    """The contract's own last print on the tape that day, with when it printed."""
    import gzip

    rows = json.load(gzip.open(MASTER))
    key = next(
        (row["instrument_key"] for row in rows if row.get("trading_symbol") == symbol), None
    )
    if key is None:
        return None
    directory = TAPE / key
    index_path = directory / f"{day}.index"
    if not index_path.exists():
        return None
    index = read_tape_index(index_path)
    for position in range(len(index) - 1, -1, -1):
        try:
            payload = json.loads(read_payload(directory / f"{day}.blob", index[position]))
        except Exception:
            continue
        price = payload.get("last_traded_price")
        if price:
            return float(price), int(index[position]["received_at_ns"])
    return None


def trim(lots: list[dict], to_capital: float) -> tuple[list[dict], list[dict]]:
    """Remove oldest lots until what remains is within `to_capital`.

    FIFO, the same order `LotBook.take` consumes, and a lot is split rather than
    dropped whole when only part of it has to go -- otherwise a trim would remove
    more than the breach and the result would be a different arbitrary size.
    """
    remaining = list(lots)
    removed: list[dict] = []
    held = sum(float(lot["quantity"]) * float(lot["price"]) for lot in remaining)
    while remaining and held > to_capital:
        lot = remaining[0]
        quantity, price = float(lot["quantity"]), float(lot["price"])
        excess = held - to_capital
        if quantity * price <= excess:
            removed.append(dict(lot))
            remaining.pop(0)
            held -= quantity * price
            continue
        part = excess / price
        removed.append({**lot, "quantity": repr(part)})
        remaining[0] = {**lot, "quantity": repr(quantity - part)}
        held -= part * price
    return remaining, removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--day", default=time.strftime("%Y-%m-%d"),
                        help="which tape day to price the trim from")
    arguments = parser.parse_args()

    root = position_state_root()
    ceilings = ceiling_per_segment()
    ceiling = min(ceilings.values())
    print(f"ceiling per trade: Rs {ceiling:,.0f}  (tightest of {ceilings})")
    print(f"pricing the trim from the tape for {arguments.day}\n")

    detector_path = root / LOT_BOOK_FILES[0]
    state = json.loads(detector_path.read_text())["state"]
    plan = []
    for key, lots in sorted(state["books"].items()):
        capital = sum(float(lot["quantity"]) * float(lot["price"]) for lot in lots)
        if capital <= ceiling:
            continue
        _, symbol = book_key_of(key)
        priced = last_traded_price(symbol, arguments.day)
        if priced is None:
            print(f"  LEFT ALONE  {symbol}: no print on the tape for {arguments.day}; "
                  f"closing it would need a price nobody measured")
            continue
        plan.append((key, symbol, capital, priced))

    if not plan:
        print("nothing to trim")
        return 0

    realised = 0.0
    for key, symbol, capital, (price, at_ns) in plan:
        kept, removed = trim(state["books"][key], ceiling)
        removed_quantity = sum(float(lot["quantity"]) for lot in removed)
        cost = sum(float(lot["quantity"]) * float(lot["price"]) for lot in removed)
        proceeds = removed_quantity * price
        realised += proceeds - cost
        after = sum(float(lot["quantity"]) * float(lot["price"]) for lot in kept)
        print(f"  {symbol:34s} Rs {capital:>11,.0f} -> {after:>10,.0f}   "
              f"removed {removed_quantity:>11,.2f} at {price:>8.2f}  "
              f"P&L {proceeds - cost:>+12,.0f}")
        if not arguments.dry_run:
            state["books"][key] = kept

    print(f"\nrealised on the trim: Rs {realised:+,.0f} at real last-traded prices")

    if arguments.dry_run:
        print("\ndry run: nothing written")
        return 0

    for name in LOT_BOOK_FILES:
        path = root / name
        document = json.loads(path.read_text())
        for key, _, _, _ in plan:
            if key in (document["state"].get("books") or {}):
                document["state"]["books"][key] = state["books"][key]
        path.write_text(json.dumps(document, indent=1) + "\n")
        print(f"wrote {path.name}")

    record = root.parent / "position-trim-2026-09-07.json"
    record.write_text(json.dumps({
        "why": "trade-capital-bounds-gate bounded one order, not the position",
        "ceiling": ceiling,
        "priced_from": f"this project's own tape for {arguments.day}, last traded price",
        "realised": realised,
        "trimmed": [
            {"symbol": s, "capital_before": c, "price": p, "priced_at_ns": t}
            for _, s, c, (p, t) in plan
        ],
    }, indent=1) + "\n")
    print(f"wrote {record}")
    print("\nNow re-seed the reconciler from the trimmed book:")
    print("  .venv/bin/python operate/seed_reconciler_from_the_lot_book.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
