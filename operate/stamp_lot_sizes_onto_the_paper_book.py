"""Write each open paper position's own lot size into the account checkpoint.

`paper-account-keeper` learns an instrument's quantity step from the fills that
build a position (2026-09-16) and treats a holding smaller than one step as flat,
because no order snapped to that step could ever sell it. A position restored
from a checkpoint written before that field existed carries no step, so the
keeper falls back to `order_quantity_increment` -- one global 0.001 -- and a
residue of 19.188 units against a 75 lot survives as an open position.

It cannot clear itself: a closing order is snapped to whole lots, so a sub-lot
holding rounds to a zero-quantity order that is never sent, while the position
keeps its symbol marked as held and the segment can never open on it again.

This stamps the step from the broker's own instrument master onto every open
position, so the rule already in the keeper can act at the next restart. It
writes nothing else -- no quantity, no price, no cash -- and it is idempotent.

    python3 operate/stamp_lot_sizes_onto_the_paper_book.py            # report only
    python3 operate/stamp_lot_sizes_onto_the_paper_book.py --write    # and write

The spine must be stopped before --write: it rewrites this file on every fill and
would overwrite the stamp with what it holds in memory.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BOOK = pathlib.Path.home() / ".local/share/ajit-segment-bots/positions"


def lot_by_trading_symbol() -> dict:
    from runtime.brokers.instrument_master import fetch_and_parse_listings
    from runtime.brokers.upstox import UpstoxAdapter

    return {
        listing.trading_symbol: float(listing.lot_size)
        for listing in fetch_and_parse_listings(UpstoxAdapter())
        if listing.lot_size
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="write the stamped checkpoints, after backing them up")
    arguments = parser.parse_args()

    lots = lot_by_trading_symbol()
    print(f"instrument master: {len(lots):,} trading symbols with a lot size\n")

    for path in sorted(BOOK.glob("paper-account-keeper.paper-account-*.json")):
        document = json.loads(path.read_text())
        positions = (document.get("state") or {}).get("positions") or {}
        stamped = unknown = already = 0
        would_be_flat = []
        for key, held in positions.items():
            symbol = key.partition("|")[2]
            lot = lots.get(symbol)
            if lot is None:
                unknown += 1
                continue
            if held.get("quantity_increment"):
                already += 1
            else:
                stamped += 1
            held["quantity_increment"] = lot
            if abs(float(held["quantity"])) < lot:
                would_be_flat.append((symbol, float(held["quantity"]), lot,
                                      float(held.get("margin_posted", 0.0))))
        print(f"{path.name}")
        print(f"  {len(positions)} open; {stamped} stamped, {already} already carried one, "
              f"{unknown} not in the master")
        print(f"  {len(would_be_flat)} hold less than one lot and become flat at the next start:")
        for symbol, quantity, lot, margin in would_be_flat:
            print(f"    {symbol:34} {quantity:>22} of a {lot:g} lot   margin {margin:,.2f}")
        print(f"  margin that returns to cash: "
              f"{sum(margin for *_, margin in would_be_flat):,.2f}")
        if arguments.write:
            backup = path.with_suffix(f".json.before-lot-stamp-{time.strftime('%Y%m%dT%H%M%S')}")
            shutil.copy2(path, backup)
            path.write_text(json.dumps(document, indent=1, sort_keys=True))
            print(f"  written; previous file kept at {backup.name}")
        print()

    if not arguments.write:
        print("report only -- pass --write to stamp, with the spine stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
