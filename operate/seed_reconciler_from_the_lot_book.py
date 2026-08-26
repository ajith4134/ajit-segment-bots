#!/usr/bin/env python3
"""Give `fill-reconciler` the positions that were open before it could remember.

    .venv/bin/python operate/seed_reconciler_from_the_lot_book.py --dry-run
    .venv/bin/python operate/seed_reconciler_from_the_lot_book.py

**Why this exists, and why it is a one-off rather than a part.**

`fill-reconciler` is where a fill becomes a `position`, so nothing downstream of a
fill has an input until it is running -- the excursion tracker, the stop manager,
the exposure limiter, the margin watch. It held its book in memory alone until
2026-08-26, and it learns a position only from a *fill*, which for one already
open never arrives again. Every restart therefore began blind to everything held.

It checkpoints now, so this cannot recur. But the twenty positions open on the day
that landed are in nobody's `fill-reconciler` checkpoint, because there has never
been one -- and without a seed they would stay invisible for the rest of their
lives. Measured on the live spine at 11:47 on 2026-08-26:

    peak-excursion-tracker    positions_tracked 0, prices_without_cost_basis 162,960
                              -- the excursion on the board was frozen at whatever
                              it last was, and 16 of 20 rows contradicted their own
                              live P&L: a "worst" of -4.91 on a position at -27.27
    stop-order-manager        0 of 20 open positions had a stop resting
    exposure-limiter          positions_seen 0, total_exposure 0

**The lot book is the source, because it is the one that survived.**
`position-close-detector` checkpoints the lots of every open round trip and has
done since 2026-08-25, so it knows what is held, at what cost, since when. This
reads that file and writes the reconciler's, converting one representation to the
other. It reads a *file*, not a part: this is operator work standing outside the
circuit, which is what `operate/` is for. A part reaching into another part's
checkpoint would be a part naming another part, and T-4 forbids exactly that.

**It refuses rather than overwrites.** Once `fill-reconciler` has a checkpoint of
its own, that file is the live book and this script must not touch it -- seeding
over a real book would replace what the running system knows with a reconstruction
of it. `--force` exists for a rerun the operator has decided on, and says so.

**`seen_fills` is deliberately left empty.** The reconciler uses it to refuse a
fill it has already applied. The lot book records fills by a different identity,
and inventing ids here would let a genuinely new fill be refused as a duplicate --
silently, and on the position it was meant to open. An empty set risks the
opposite: a fill from before the seed being applied twice. That cannot happen,
because a fill is delivered once on the bus and no part replays them.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from runtime.durable_state import DurableStateStore
from runtime.lot_book_checkpoint import book_key_of
from runtime.settings_reader import load_settings_document, settings_directory

CLOSE_DETECTOR_FILE = "position-close-detector.positions.json"
RECONCILER_PART = "fill-reconciler"
RECONCILER_COMPONENT = "held-positions"


def read_position_state_root() -> pathlib.Path:
    document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    return pathlib.Path(str(document.read_value("position_state_root"))).expanduser()


def read_quantity_tolerance() -> float:
    document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    return float(document.read_value("order_quantity_increment"))


def positions_from_the_lot_book(document: dict) -> dict:
    """The close detector's lots, as the reconciler's held positions.

    Quantity is summed from the lots still open and signed by the direction the
    detector recorded, because a Position carries the side in the sign of its
    quantity and a lot book carries it alongside.

    The average entry price is the entry cost over the quantity *entered*, which
    is what the detector already keeps -- not recomputed from the remaining lots,
    which would move the average of a partly closed position every time a lot was
    consumed.
    """
    state = document.get("state") or {}
    books = state.get("books") or {}
    direction = state.get("direction") or {}
    entry_cost = state.get("entry_cost") or {}
    entered = state.get("entered_quantity") or {}
    fees = state.get("fees") or {}
    realised = state.get("realised") or {}
    opened_at = state.get("opened_at") or {}

    held = {}
    for key, lots in sorted(books.items()):
        quantity = sum(float(lot["quantity"]) for lot in lots)
        if quantity <= 0:
            continue
        venue_id, symbol = book_key_of(key)
        entered_quantity = float(entered.get(key) or 0.0)
        cost = float(entry_cost.get(key) or 0.0)
        if direction.get(key) == "short":
            quantity = -quantity
        opened = int(opened_at.get(key) or 0)
        held[key] = {
            "venue_id": venue_id,
            "symbol": symbol,
            "quantity": quantity,
            "average_entry_price": (cost / entered_quantity) if entered_quantity else 0.0,
            "realised_pnl": float(realised.get(key) or 0.0),
            "fees_paid": float(fees.get(key) or 0.0),
            "opened_at_ns": opened,
            # The lot book records when the position opened, not when it last
            # moved. Stating the open time for both is honest -- it says "nothing
            # has been seen since" -- where stamping now would claim this book was
            # confirmed at a moment nobody confirmed it.
            "updated_at_ns": opened,
        }
    return held


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print what would be written")
    parser.add_argument(
        "--force", action="store_true",
        help="overwrite a checkpoint fill-reconciler has already written (it is the live book)",
    )
    arguments = parser.parse_args()

    root = read_position_state_root()
    source = root / CLOSE_DETECTOR_FILE
    if not source.exists():
        print(f"no lot book at {source}: nothing to seed from", file=sys.stderr)
        return 1

    store = DurableStateStore(root)
    destination = store.path_for(RECONCILER_PART, RECONCILER_COMPONENT)
    if destination.exists() and not arguments.force:
        existing = json.loads(destination.read_text(encoding="utf-8"))
        held = len((existing.get("state") or {}).get("positions") or {})
        print(
            f"{destination} already exists and holds {held} position(s). That file is the "
            f"live book fill-reconciler restores from, and seeding over it would replace "
            f"what the running system knows with a reconstruction of it. Pass --force to "
            f"overwrite deliberately.",
            file=sys.stderr,
        )
        return 1

    document = json.loads(source.read_text(encoding="utf-8"))
    held = positions_from_the_lot_book(document)

    print(f"lot book: {source}")
    print(f"open positions found: {len(held)}")
    for key, position in held.items():
        print(
            f"  {position['symbol']:14s} {position['venue_id']:14s} "
            f"qty {position['quantity']:+.8g} at {position['average_entry_price']:.8g} "
            f"fees {position['fees_paid']:.4f}"
        )

    if arguments.dry_run:
        print(f"\ndry run: nothing written. Would write {destination}")
        return 0

    store.save(
        RECONCILER_PART,
        RECONCILER_COMPONENT,
        {"positions": held, "seen_fills": []},
        {"quantity_tolerance": read_quantity_tolerance()},
    )
    print(f"\nwrote {destination}")
    print(
        "Restart the spine for fill-reconciler to restore it: "
        "`systemctl --user restart ajit-spine`. On its first tick it republishes every "
        "held position, and the excursion tracker, stop manager, exposure limiter and "
        "margin watch each get an input they have never had for these positions."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
