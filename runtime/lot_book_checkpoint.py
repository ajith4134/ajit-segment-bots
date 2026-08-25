"""Writing a lot book down, so an open position survives the off switch.

**Substrate, not a part (RL-069).** Two parts keep lot books -- `cost-basis-tracker`
and `position-close-detector` -- and both need the same three things: a JSON-safe
spelling for a `(venue, symbol)` key, a JSON-safe spelling for a `Lot`, and the
start-up dance of restoring a checkpoint and arming the next write. Putting that in
either part would make the other import it, and a part that imports a part is wired
to a part (T-4). It lives below the diagram instead, wired to neither.

**The failure it closes, measured rather than supposed.** The spine had started 46
times. 855 positions had been opened and 115 round trips closed. The lot books lived
only in the process, so every start forgot every position that was open -- and a
position whose lots are forgotten can never reach flat, never emits a `closed-trade`,
and can never be scored by anything downstream of it. 86% of everything this system
had ever opened was unaccounted for, and nothing reported that as a fault.

**Quantities are stored as strings.** `json` has no decimal type. Writing a
`Decimal` as a float and reading it back restores the binary approximation, which
puts back exactly the residue `exact_quantity` exists to keep out -- a position that
can never reach flat, rebuilt by the very thing meant to preserve it.
"""

from __future__ import annotations

import pathlib

from runtime.durable_state import (
    CheckpointSchedule,
    DurableStateStore,
    restore_and_arm_checkpoint,
)
from runtime.trading_types import Lot, LotBook, exact_quantity

# A checkpoint is JSON and JSON keys are strings, so the (venue, symbol) pair is
# written as one. The separator is a character neither a venue id nor a symbol
# contains, so splitting it back is unambiguous rather than merely usually right.
KEY_SEPARATOR = "|"


def book_key_text(key: tuple[str, str]) -> str:
    return f"{key[0]}{KEY_SEPARATOR}{key[1]}"


def book_key_of(text: str) -> tuple[str, str]:
    venue_id, _, symbol = text.partition(KEY_SEPARATOR)
    return (venue_id, symbol)


def lots_as_documents(book: LotBook) -> list[dict]:
    """One lot book as JSON, with its quantities exact."""
    return [
        {
            "quantity": str(lot.quantity),
            "price": lot.price,
            "opened_at_ns": lot.opened_at_ns,
            "fee": lot.fee,
        }
        for lot in book.lots
    ]


def lots_from_documents(documents) -> LotBook:
    """One lot book back, in the order it was written -- FIFO decides realised profit."""
    book = LotBook()
    for document in documents or ():
        book.add(
            Lot(
                exact_quantity(document["quantity"]),
                float(document["price"]),
                int(document["opened_at_ns"]),
                float(document.get("fee", 0.0)),
            )
        )
    return book


def books_as_documents(books: dict) -> dict:
    """Only the books that hold something. An empty book is not a position."""
    return {
        book_key_text(key): lots_as_documents(book)
        for key, book in books.items()
        if book.lots
    }


def books_from_documents(documents) -> dict:
    return {
        book_key_of(text): lots_from_documents(lots)
        for text, lots in (documents or {}).items()
    }


def restore_and_arm_lot_checkpoint(context, part_id: str, component: str, holder):
    """Bring back what was open, and return the function that writes it down.

    `holder` is anything with `read_checkpoint_state()`, `restore_from_checkpoint()`
    and a `standing` -- which is the two lot-book parts, named by shape rather than
    by name so this stays substrate rather than a list of parts (T-4).

    A checkpoint that cannot be read starts the part cold and says so in the
    standing. Refusing to start would be worse: a system that will not trade
    because it cannot remember an old position has turned a recoverable gap into
    an outage.
    """
    store = DurableStateStore(
        pathlib.Path(str(context.setting("position_state_root").value)).expanduser()
    )
    schedule = CheckpointSchedule(int(context.number("position_state_checkpoint_interval")))
    # The window is what gives the stored fill ids their meaning, so a change to it
    # is a change the store must notice rather than quietly reinterpret.
    settings = {"remembered_fill_ids": float(context.number("remembered_fill_ids"))}

    return restore_and_arm_checkpoint(store, schedule, part_id, component, holder, settings)
