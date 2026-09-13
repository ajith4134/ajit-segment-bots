#!/usr/bin/env python3
"""Rebuild every position book from the fills that actually happened.

    .venv/bin/python operate/rebuild_position_books_from_the_fill_journal.py --dry-run
    .venv/bin/python operate/rebuild_position_books_from_the_fill_journal.py

**Why this exists.** Four parts keep a book of the same portfolio, each applying
the same `fill` stream: `paper-account-keeper` (cash and positions per segment),
`fill-reconciler`, `cost-basis-tracker` and `position-close-detector`. Measured
2026-09-13 they disagreed on 22 instruments, for reasons that are fixed in code
now (commit 02dc183) but whose results are still in the checkpoints:

- the keeper refused 114 executed fills on 2026-09-07 for want of cash;
- three fills reused an earlier fill's id and every book dropped them;
- `operate/trim_positions_to_the_capital_ceiling.py` cut the two lot books on
  2026-09-07 and never touched the keeper, with no fill and no proceeds;
- a re-cut stop was never withdrawn and sold on flat positions, which the lot
  books refused as stale exits and the reconciler netted as shorts.

**The detector replays with shorts allowed, and only here.** Those stale sells
executed in the paper book, and the system then bought to cover the shorts they
made -- `stop-order-manager` even rested buy stops on them. Net of every fill each
such instrument is flat, which is what the other three books say. Replayed under
its live rule (a sell against nothing is refused), `position-close-detector` keeps
the covering buy as a phantom long: measured, 12 refusals, 28,935.687 units, 11
instruments no other book holds. Allowing the short for the replay records what
the paper book did; the live part keeps its rule, and this refuses to write a book
that ends holding a short, which the live part could not have made.

**The source is the fill journal.** `trade-lifecycle-recorder` recorded every
fill, hash-chained. Replayed in time order through the four parts' own engines --
not a reimplementation of them -- the books cannot disagree, because they are
built from one sequence by the code that runs live.

**The trim is kept** (operator, 2026-09-13), as what it should have been: paper
sells. Its record states each symbol, its capital before and the price, but not
the quantity removed, so the quantity is recomputed with that script's own
`trim()` on the lot book replayed to the moment it ran (11:23:14 UTC, spine
stopped, after every fill that day and before the next). Each symbol's replayed
capital must match `capital_before` in the record, or this refuses: a trim
applied to a different book than the one it was computed on would be a different
trim.

**Fill ids that collided are reissued**, as `<id>-reissued-<filled_at_ns>` for
every fill after the first to carry an id, so each real fill is applied once.

**It refuses to run while the spine is up** (the parts would checkpoint over the
result), and keeps the old checkpoints in `positions-before-book-rebuild-<time>/`.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from parts.paper_live_trading.paper_account_keeper import (  # noqa: E402
    CHECKPOINT_COMPONENT as KEEPER_COMPONENT,
    PART_ID as KEEPER_PART,
    PaperAccountKeeper,
)
from parts.portfolio_state.cost_basis_tracker import (  # noqa: E402
    CHECKPOINT_COMPONENT as COST_COMPONENT,
    PART_ID as COST_PART,
    CostBasisTracker,
)
from parts.portfolio_state.fill_reconciler import (  # noqa: E402
    CHECKPOINT_COMPONENT as RECONCILER_COMPONENT,
    PART_ID as RECONCILER_PART,
    FillReconciler,
)
from parts.portfolio_state.position_close_detector import (  # noqa: E402
    CHECKPOINT_COMPONENT as DETECTOR_COMPONENT,
    PART_ID as DETECTOR_PART,
    PositionCloseDetector,
)
from runtime.durable_state import DurableStateStore  # noqa: E402
from runtime.settings_reader import load_settings_document, settings_directory  # noqa: E402
from runtime.trading_types import SELL, Fill  # noqa: E402

PROJECT = pathlib.Path(__file__).resolve().parent.parent
STATE = pathlib.Path.home() / ".local/share/ajit-segment-bots"
JOURNAL_GLOB = "journal.trade-lifecycle-recorder*.sqlite"
TRIM_RECORD = STATE / "position-trim-2026-09-07.json"
VENUE = "upstox"
FILL_FIELDS = (
    "fill_id", "venue_id", "symbol", "side", "price", "quantity", "fee",
    "filled_at_ns", "order_id", "is_paper", "leverage", "segment",
)


def ns_of(text: str) -> int:
    moment = datetime.datetime.fromisoformat(text).astimezone(datetime.UTC)
    return int(moment.timestamp()) * 1_000_000_000 + moment.microsecond * 1_000


# When the trim ran: its record's own write time, with the spine stopped from
# 11:23:00 (live-spine.jsonl) until 11:25:01. After every 2026-09-07 fill (the last
# is before 10:00) and before the first 2026-09-08 fill.
TRIM_RAN_AT_NS = ns_of("2026-09-07T11:23:14.776979+00:00")

# Each built segment's allotment, and when it took that value, from the segment
# files' own provenance notes. The 2026-09-12 raise has no time of day in its note;
# no fill was made between 2026-09-08 10:00 and 2026-09-12, so any moment that day
# gives the same result.
ALLOTMENT_HISTORY = {
    "index-options": (
        (0, 500_000.0),
        (ns_of("2026-09-07T05:56:54+00:00"), 5_000_000.0),
        (ns_of("2026-09-12T00:00:00+00:00"), 7_500_000.0),
    ),
    "stock-options": (
        (0, 500_000.0),
        (ns_of("2026-09-07T06:00:19+00:00"), 5_000_000.0),
        (ns_of("2026-09-12T00:00:00+00:00"), 7_500_000.0),
    ),
}


def read_the_fill_journal(fills_file: pathlib.Path | None) -> list[dict]:
    """Every recorded fill on the Indian venue, oldest first, collided ids reissued."""
    if fills_file is not None:
        lines = fills_file.open(encoding="utf-8")
        sources = [str(fills_file)]
    else:
        paths = sorted(STATE.glob(JOURNAL_GLOB))
        sources = [str(path) for path in paths]
        lines = (
            line.decode("utf-8", "replace")
            for path in paths
            for line in path.open("rb")
            if b'"kind": "fill"' in line
        )
    records = []
    for line in lines:
        record = json.loads(line)
        if record.get("kind") != "fill":
            continue
        payload = record["payload"]
        if payload.get("venue_id") != VENUE:
            continue
        records.append((payload["filled_at_ns"], record["sequence"], payload))
    records.sort(key=lambda entry: (entry[0], entry[1]))

    seen: dict[str, tuple] = {}
    fills, reissued, rerecorded = [], [], 0
    for _, _, payload in records:
        content = (payload["side"], payload["quantity"], payload["price"], payload["filled_at_ns"])
        first = seen.get(payload["fill_id"])
        if first == content:
            rerecorded += 1
            continue
        if first is not None:
            new_id = f"{payload['fill_id']}-reissued-{payload['filled_at_ns']}"
            reissued.append((payload["fill_id"], new_id))
            payload = {**payload, "fill_id": new_id}
        seen[payload["fill_id"]] = content
        fills.append(payload)
    print(f"fills read: {len(fills)} from {len(sources)} journal file(s); "
          f"{len(reissued)} collided id(s) reissued; {rerecorded} re-recorded fill(s) skipped")
    for old, new in reissued:
        print(f"  reissued {old} -> {new}")
    return fills


def load_the_trim_script():
    path = PROJECT / "operate/trim_positions_to_the_capital_ceiling.py"
    spec = importlib.util.spec_from_file_location("trim_positions_to_the_capital_ceiling", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def as_fill(payload: dict) -> Fill:
    return Fill(**{name: payload[name] for name in FILL_FIELDS if name in payload})


class Books:
    """The four parts' engines, fed one fill at a time on the fill's own clock."""

    def __init__(self, quantity_increment: float, remembered_fill_ids: int) -> None:
        self.clock = [0]
        now = lambda: self.clock[0]  # noqa: E731
        self.reconciler = FillReconciler(quantity_tolerance=quantity_increment, now_ns=now)
        self.cost_basis = CostBasisTracker(
            quantity_increment=quantity_increment, now_ns=now,
            remembered_fill_ids=remembered_fill_ids,
        )
        self.detector = PositionCloseDetector(
            quantity_increment=quantity_increment, now_ns=now,
            remembered_fill_ids=remembered_fill_ids,
            may_open_a_short=True,
        )
        self.accounts = {segment: PaperAccountKeeper(segment, now_ns=now) for segment in ALLOTMENT_HISTORY}
        self._next_allotment = {segment: 0 for segment in ALLOTMENT_HISTORY}

    def _fund_up_to(self, at_ns: int) -> None:
        for segment, history in ALLOTMENT_HISTORY.items():
            while (
                self._next_allotment[segment] < len(history)
                and history[self._next_allotment[segment]][0] <= at_ns
            ):
                self.accounts[segment].set_allotment(history[self._next_allotment[segment]][1])
                self._next_allotment[segment] += 1

    def apply(self, payload: dict) -> None:
        self.clock[0] = int(payload["filled_at_ns"])
        self._fund_up_to(self.clock[0])
        fill = as_fill(payload)
        self.reconciler.observe_fill(fill)
        self.cost_basis.observe_fill(fill)
        self.detector.observe_fill(fill)
        account = self.accounts.get(payload.get("segment", ""))
        if account is not None:
            account.apply_fill(fill)

    def finish(self) -> None:
        self._fund_up_to(max(entry[-1][0] for entry in ALLOTMENT_HISTORY.values()))


def the_trim_as_fills(books: Books, segment_of: dict, ceiling: float) -> list[dict]:
    """The 2026-09-07 trim, recomputed on the replayed book and expressed as paper sells."""
    trim = load_the_trim_script().trim
    record = json.loads(TRIM_RECORD.read_text())
    if float(record["ceiling"]) != ceiling:
        raise SystemExit(f"the trim record's ceiling {record['ceiling']} is not {ceiling}")
    lot_books = books.detector.read_checkpoint_state()["books"]
    fills, mismatched = [], []
    for entry in record["trimmed"]:
        symbol = entry["symbol"]
        lots = lot_books.get(f"{VENUE}|{symbol}", [])
        capital = sum(float(lot["quantity"]) * float(lot["price"]) for lot in lots)
        if abs(capital - float(entry["capital_before"])) > 1.0:
            mismatched.append((symbol, capital, float(entry["capital_before"])))
            continue
        _kept, removed = trim(lots, ceiling)
        quantity = float(sum(Decimal(str(lot["quantity"])) for lot in removed))
        print(f"  trim {symbol:30s} capital {capital:>13,.2f} (record {entry['capital_before']:>13,.2f}) "
              f"sell {quantity:>12,.4f} at {entry['price']}")
        fills.append({
            "fill_id": f"trim-2026-09-07-{symbol}",
            "venue_id": VENUE,
            "symbol": symbol,
            "side": SELL,
            "price": float(entry["price"]),
            "quantity": quantity,
            # The trim charged nothing, and its own realised figure was computed
            # without a fee; inventing one here would be a cost nobody paid.
            "fee": 0.0,
            "filled_at_ns": TRIM_RAN_AT_NS,
            "order_id": "operate/trim_positions_to_the_capital_ceiling.py",
            "is_paper": True,
            "leverage": 1.0,
            "segment": segment_of[symbol],
        })
    if mismatched:
        for symbol, replayed, recorded in mismatched:
            print(f"  MISMATCH {symbol}: replayed capital {replayed:,.2f}, record {recorded:,.2f}")
        raise SystemExit("the replayed book is not the book the trim was computed on; refusing")
    return fills


def held_by_book(books: Books) -> dict[str, dict[str, float]]:
    """Each book's signed quantity per instrument, zeros left out."""
    def lots_net(state):
        return {
            key: float(sum(Decimal(str(lot["quantity"])) for lot in lots))
            for key, lots in state["books"].items()
        }

    reconciler = {
        key: float(value["quantity"])
        for key, value in books.reconciler.read_checkpoint_state()["positions"].items()
    }
    detector = lots_net(books.detector.read_checkpoint_state())
    cost_basis = lots_net(books.cost_basis.read_checkpoint_state())
    account = {}
    for keeper in books.accounts.values():
        for key, value in keeper.read_checkpoint_state()["positions"].items():
            account[key] = float(value["quantity"])
    return {
        name: {key: quantity for key, quantity in book.items() if abs(quantity) > 1e-6}
        for name, book in (
            ("paper-account-keeper", account), ("fill-reconciler", reconciler),
            ("cost-basis-tracker", cost_basis), ("position-close-detector", detector),
        )
    }


def spine_is_running() -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "ajit-spine"], capture_output=True, text=True
    )
    return result.stdout.strip() == "active"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fills-file", type=pathlib.Path, default=None,
                        help="journal fill lines already extracted, to skip scanning 22 GB")
    arguments = parser.parse_args()

    runtime = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    root = pathlib.Path(str(runtime.read_value("position_state_root"))).expanduser()
    quantity_increment = float(runtime.read_value("order_quantity_increment"))
    remembered = int(runtime.read_value("remembered_fill_ids"))
    ceiling = min(load_the_trim_script().ceiling_per_segment().values())

    fills = read_the_fill_journal(arguments.fills_file)
    segment_of = {fill["symbol"]: fill["segment"] for fill in fills}
    books = Books(quantity_increment, remembered)
    before_trim = [fill for fill in fills if fill["filled_at_ns"] < TRIM_RAN_AT_NS]
    after_trim = [fill for fill in fills if fill["filled_at_ns"] >= TRIM_RAN_AT_NS]
    for fill in before_trim:
        books.apply(fill)
    print(f"\nreplayed {len(before_trim)} fill(s) up to the trim; recomputing it:")
    trim_fills = the_trim_as_fills(books, segment_of, ceiling)
    for fill in trim_fills + after_trim:
        books.apply(fill)
    books.finish()
    print(f"replayed {len(trim_fills)} trim sell(s) and {len(after_trim)} later fill(s)")

    held = held_by_book(books)
    keys = sorted(set().union(*held.values()))
    print(f"\n{'instrument':38s}" + "".join(f"{name[:18]:>20s}" for name in held))
    disagreements = 0
    for key in keys:
        row = [held[name].get(key, 0.0) for name in held]
        agree = max(row) - min(row) < 1e-6
        disagreements += 0 if agree else 1
        print(f"{key[len(VENUE) + 1:][:38]:38s}" + "".join(f"{value:>20,.3f}" for value in row)
              + ("" if agree else "   <- DISAGREE"))
    for segment, keeper in books.accounts.items():
        standing = keeper.standing
        print(f"\n{segment}: cash {keeper.read_checkpoint_state()['cash']:,.2f}, fills applied "
              f"{standing.fills_applied}, beyond cash {standing.fills_applied_beyond_cash} "
              f"(shortfall {standing.cash_shortfall_total:,.2f}), duplicates refused "
              f"{standing.duplicates_refused}")
    print(f"\ninstruments held: {len(keys)}; books disagreeing on {disagreements}")
    detector = books.detector.standing
    print(f"sells refused as stale exits by the lot books: "
          f"{detector.refused_a_sell_that_would_open_a_short} "
          f"({detector.quantity_refused_as_an_unmatched_exit:,.3f} units)")

    shorts = sorted({key for book in held.values() for key, quantity in book.items() if quantity < 0})
    if shorts:
        print(f"the rebuilt books end holding shorts, which Phase A cannot hold: {shorts}; nothing written")
        return 1
    if disagreements:
        print("the rebuilt books still disagree; nothing written")
        return 1
    if arguments.dry_run:
        print("dry run: nothing written")
        return 0
    if spine_is_running():
        raise SystemExit("ajit-spine is running and would checkpoint over the rebuild; stop it first")

    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H%M%S")
    backup = root.parent / f"positions-before-book-rebuild-{stamp}"
    shutil.copytree(root, backup)
    print(f"\nold checkpoints kept in {backup}")

    store = DurableStateStore(root)
    written = [
        store.save(RECONCILER_PART, RECONCILER_COMPONENT, books.reconciler.read_checkpoint_state(),
                   {"quantity_tolerance": quantity_increment}),
        store.save(COST_PART, COST_COMPONENT, books.cost_basis.read_checkpoint_state(),
                   {"remembered_fill_ids": float(remembered)}),
        store.save(DETECTOR_PART, DETECTOR_COMPONENT, books.detector.read_checkpoint_state(),
                   {"remembered_fill_ids": float(remembered)}),
    ]
    for segment, keeper in books.accounts.items():
        written.append(store.save(KEEPER_PART, f"{KEEPER_COMPONENT}-{segment}",
                                  keeper.read_checkpoint_state(), {}))
    for path in written:
        print(f"wrote {path.name}")

    record = root.parent / f"position-book-rebuild-{stamp}.json"
    record.write_text(json.dumps({
        "why": "four books of one portfolio disagreed; rebuilt from the fill journal (commit 02dc183)",
        "fills_replayed": len(fills),
        "trim_sells": trim_fills,
        "held": held,
        "backup": str(backup),
    }, indent=1) + "\n")
    print(f"wrote {record}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
