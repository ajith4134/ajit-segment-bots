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
    """What the bot holds, from the checkpoint the closing part restores from.

    Everything a reader judges the position by is computed from **the lots still
    held**, not from the round trip's running totals. The two are the same number
    only for a position that has never been scaled out of, and on 2026-08-28 they
    were not: `binance-usdm|AKEUSDT` had 410,652 units entered and 231,812 held,
    so the cumulative entry cost overstated what was at risk by 44% and the
    average entry it implied was an entry the remaining lots never paid.

    The round trip's totals are still carried -- as `entered_capital` and
    `entered_quantity` -- because what a position has cost so far is a real fact.
    They are just not what "capital in" means for something still open.
    """
    from runtime.lot_book_checkpoint import book_key_of
    from runtime.trading_types import Lot, LotBook, exact_quantity

    try:
        from runtime.settings_reader import load_settings_document, settings_directory

        document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
        root = pathlib.Path(str(document.read_value("position_state_root"))).expanduser()
        # The same bound `position-close-detector` decides flatness with. Read
        # from settings rather than defaulted, so the board and the bot cannot
        # disagree about what counts as holding nothing.
        quantity_increment = float(document.read_value("order_quantity_increment"))
    except Exception as refusal:
        return [], {
            "ok": False,
            "proof": (
                f"settings refused position_state_root or order_quantity_increment "
                f"({refusal})"
            ),
        }

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
    # Realised so far on a position that is still open: a lot sold back before the
    # rest. Zero and absent are different -- absent means this key was never
    # scaled out of -- so the key's presence is what decides, not the number.
    realised = state.get("realised") or {}
    # Notional entered at each leverage. Absent on a checkpoint written before
    # position-close-detector recorded it, and absent stays absent: unlevered and
    # unknown are different claims and only one of them may render as a number.
    notional_at_leverage = state.get("notional_at_leverage") or {}

    positions = []
    residues_skipped = []
    for key, lots in sorted(books.items()):
        venue_id, symbol = book_key_of(key)
        book = LotBook([
            Lot(exact_quantity(lot["quantity"]), float(lot["price"]),
                int(lot.get("opened_at_ns") or 0), float(lot.get("fee") or 0.0))
            for lot in lots
        ])
        quantity = float(book.total_quantity)
        if quantity <= 0:
            continue
        # A book below one quantity step holds nothing any order could sell. It
        # is not a small position and must not be shown as one: 13 such books
        # reported 19,862 USDT open against a 10,000 allotment on 2026-08-28,
        # each still carrying the full cost of a round trip that had ended.
        if book.is_flat_within(quantity_increment):
            residues_skipped.append(f"{symbol} ({quantity:.6g})")
            continue
        # What the lots still held cost, and what they averaged. Both from the
        # book, so a position scaled out of reports what is still in it.
        held_cost = book.held_cost
        held_entry_price = book.average_price
        entered_cost = float(entry_cost.get(key, 0.0))
        entered_quantity = float(entered.get(key, 0.0) or 0.0)
        # The leverage this position was opened at, weighted by notional across
        # its entering fills. `entry_cost / notional_at_leverage` inverts the sum
        # the detector kept, because that sum is the notional divided by leverage
        # fill by fill -- which is exactly what a blended multiplier means.
        committed_when_entered = notional_at_leverage.get(key)
        leverage = (
            entered_cost / float(committed_when_entered)
            if committed_when_entered and float(committed_when_entered) > 0
            else None
        )
        best, worst = (excursion.get(key) or [None, None])[:2]
        positions.append(
            {
                "venue_id": venue_id,
                "symbol": symbol,
                "direction": direction.get(key),
                "quantity": quantity,
                "entry_price": held_entry_price,
                # What is at risk now: the notional of the lots still held.
                "capital_in": held_cost,
                # And what that notional actually ties up, which is the number
                # `maximum_capital_per_trade` bounds. NOT MEASURED, never 1x,
                # when the checkpoint predates leverage being recorded.
                "leverage": leverage,
                "capital_committed": None if leverage is None else held_cost / leverage,
                "leverage_proof": (
                    f"weighted across this position's entering fills: "
                    f"{entered_cost:,.2f} of notional committed "
                    f"{float(committed_when_entered):,.2f}"
                    if leverage is not None else
                    f"{NOT_MEASURED}: this position was checkpointed before "
                    f"position-close-detector recorded the leverage a fill was sized at"
                ),
                # The round trip so far, which is a different fact from what is
                # held now and is kept rather than folded into it.
                "entered_capital": entered_cost,
                "entered_quantity": entered_quantity,
                "fees_paid": float(fees.get(key, 0.0)),
                "opened_at_ns": int(opened_at.get(key) or 0) or None,
                "lots": len(book.lots),
                "held_lots": [
                    {"quantity": float(lot.quantity), "price": lot.price}
                    for lot in book.lots
                ],
                "best_unrealised": best,
                "worst_unrealised": worst,
                "realised_so_far": (
                    float(realised[key]) if key in realised else None
                ),
            }
        )

    residue_note = (
        ""
        if not residues_skipped
        else (
            f". {len(residues_skipped)} book(s) held less than one "
            f"{quantity_increment:g}-unit order step and are not positions: "
            + ", ".join(sorted(residues_skipped)[:6])
            + ("…" if len(residues_skipped) > 6 else "")
        )
    )
    return positions, {
        "ok": True,
        "residue_books_skipped": len(residues_skipped),
        "proof": (
            f"{path}, written {(time.time_ns() - int(document.get('saved_at_ns') or 0)) / 1e9:.0f}s "
            f"ago -- the same file position-close-detector restores from"
            f"{residue_note}"
        ),
        "saved_at_ns": document.get("saved_at_ns"),
    }


def read_excursion_settings() -> dict | None:
    """Where the profiler checkpoints and which quantiles it prices, or None.

    The quantiles are read from settings rather than defaulted here, because a
    default would be a number nobody chose being reported as what the bot plans
    against (RL-061). They are not on the checkpoint -- it carries only
    `minimum_claims` and `window` -- so settings is the one place that has them,
    and it is the same place the profiler itself reads them from.
    """
    try:
        from runtime.settings_reader import load_settings_document, settings_directory

        document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
        targets = list(document.read_value("bull_exit_target_quantiles"))
        return {
            "root": pathlib.Path(str(document.read_value("learned_state_root"))).expanduser(),
            # The first target is the one a plan reaches first, so it is the one a
            # board showing a single expected move should show.
            "target_quantile": float(targets[0]),
            "adverse_quantile": float(document.read_value("signal_excursion_adverse_quantile")),
        }
    except Exception:
        return None


def attach_learned_excursions(positions: list[dict]) -> dict:
    """Attach what this symbol has historically done to a call of this direction.

    Read from `signal-excursion-profiler`'s own checkpoint -- the same file that
    part restores from -- rather than from a second measurement kept for the
    board, which would be free to disagree with the one the bot acts on. That is
    the rule the learning tile already follows for the conviction model.

    Two numbers per position, both in the units a reader can act on:

      * the favourable move this symbol reached on calls of this side that came
        right, at the quantile `bull_exit_target_quantiles` names first -- which
        is where `bull-exit-plan-proposer` looks its own targets up;
      * the adverse move a correct call survived, at
        `signal_excursion_adverse_quantile` -- which is the only honest basis for
        a stop, because a stop inside it converts winners into losers.

    Both are fractions of the entry price, so they are also shown against this
    position's own capital. The claim count rides along: a quantile over four
    settled claims is a number, not a measurement, and the profiler itself
    refuses to publish one below `signal_excursion_minimum_claims`. A position
    whose symbol and side have not settled that many claims is marked
    NOT MEASURED rather than shown a quantile nobody should size against.
    """
    from runtime.trade_profiles import quantile_of

    chosen = read_excursion_settings()
    if chosen is None:
        for position in positions:
            position["prediction_proof"] = (
                f"{NOT_MEASURED}: settings refused learned_state_root, "
                f"bull_exit_target_quantiles or signal_excursion_adverse_quantile"
            )
        return {
            "ok": False,
            "proof": (
                "settings refused learned_state_root, bull_exit_target_quantiles or "
                "signal_excursion_adverse_quantile -- the quantiles are not defaulted "
                "here, because a default is a number nobody chose reported as a plan"
            ),
        }

    path = chosen["root"] / "signal-excursion-profiler.excursions.json"
    if not path.exists():
        for position in positions:
            position["prediction_proof"] = (
                f"{NOT_MEASURED}: signal-excursion-profiler has not checkpointed, "
                f"which is a different fact from having learned nothing"
            )
        return {
            "ok": False,
            "proof": (
                f"no checkpoint at {path}: signal-excursion-profiler has not written one, "
                f"which is a different fact from having learned nothing"
            ),
        }

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as failure:
        for position in positions:
            position["prediction_proof"] = f"{NOT_MEASURED}: {path} could not be read"
        return {"ok": False, "proof": f"{path} could not be read: {failure}"}

    state = document.get("state") or {}
    favourable = state.get("favourable") or {}
    adverse = state.get("adverse") or {}
    settings = document.get("settings") or {}
    # The quantiles come from settings, which is where the profiler reads them
    # from too. `minimum_claims` comes off the checkpoint, because that is the
    # bar the sample in this file was actually gathered against -- a setting
    # changed since it was written would describe a different sample.
    target_quantile = chosen["target_quantile"]
    adverse_quantile = chosen["adverse_quantile"]
    minimum_claims = int(settings.get("minimum_claims") or 0)

    for position in positions:
        key = f"{position['venue_id']}|{position['symbol']}|{position.get('direction')}"
        favourable_moves = favourable.get(key) or []
        adverse_moves = adverse.get(key) or []
        claims = len(favourable_moves)
        position["prediction_claims"] = claims
        if claims < max(minimum_claims, 1):
            position["expected_favourable_fraction"] = None
            position["expected_adverse_fraction"] = None
            position["expected_favourable_quote"] = None
            position["expected_adverse_quote"] = None
            position["prediction_proof"] = (
                f"{NOT_MEASURED}: {claims} settled claim(s) for {position['symbol']} "
                f"{position.get('direction')}, below the {minimum_claims} this profiler "
                f"takes a quantile over"
            )
            continue
        up = quantile_of(favourable_moves, target_quantile)
        down = quantile_of(adverse_moves, adverse_quantile) if adverse_moves else None
        capital = position.get("capital_in") or 0.0
        position["expected_favourable_fraction"] = up
        position["expected_adverse_fraction"] = down
        position["expected_favourable_quote"] = None if up is None else up * capital
        position["expected_adverse_quote"] = None if down is None else down * capital
        position["prediction_proof"] = (
            f"{path.name}: {claims} settled claim(s) for {position['symbol']} "
            f"{position.get('direction')}; favourable at the {target_quantile:.0%} "
            f"quantile, adverse a correct call survived at {adverse_quantile:.0%}"
        )

    return {
        "ok": True,
        "proof": (
            f"{path}, written "
            f"{(time.time_ns() - int(document.get('saved_at_ns') or 0)) / 1e9:.0f}s ago -- "
            f"the same file signal-excursion-profiler restores from. "
            f"{len(favourable)} symbol-and-side key(s) learned, "
            f"{state.get('claims_recorded', 0):,} claim(s) recorded"
        ),
        "keys_learned": len(favourable),
        "target_quantile": target_quantile,
        "adverse_quantile": adverse_quantile,
    }


def attach_resting_exits(positions: list[dict]) -> dict:
    """Attach the stop and target actually resting for each position.

    Read from `stop-order-manager`'s own checkpoint -- the file that part restores
    from -- for the same reason the prediction is read from the profiler's: a
    second record kept for the board would be free to disagree with the one the
    bot acts on, and on a protective stop that disagreement is the whole story.

    The checkpoint exists as of 2026-08-26. Before it, these exits lived in that
    part's memory alone, so every restart forgot every stop -- and because it only
    ever hears about a stop when something upstream proposes a *new* one, a
    position already open was left with no protective order and nothing said so.
    A board could not show the column because nothing anywhere held the answer.

    A position with no entry here is reported as unprotected rather than blank.
    That is the state worth seeing: it means this position is open with no stop
    resting for it, which is a fact about the trade, not a gap in the board.
    """
    try:
        from runtime.settings_reader import load_settings_document, settings_directory

        document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
        root = pathlib.Path(str(document.read_value("position_state_root"))).expanduser()
    except Exception as refusal:
        for position in positions:
            position["exit_proof"] = f"{NOT_MEASURED}: settings refused position_state_root"
        return {"ok": False, "proof": f"settings refused position_state_root ({refusal})"}

    path = root / "stop-order-manager.resting-exits.json"
    if not path.exists():
        for position in positions:
            position["stop_price"] = None
            position["target_price"] = None
            position["exit_proof"] = (
                f"{NOT_MEASURED}: stop-order-manager has not written a checkpoint, "
                f"which is a different fact from nothing being protected"
            )
        return {
            "ok": False,
            "proof": (
                f"no checkpoint at {path}: stop-order-manager has not written one, which "
                f"is a different fact from no position being protected"
            ),
        }

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as failure:
        for position in positions:
            position["exit_proof"] = f"{NOT_MEASURED}: {path} could not be read"
        return {"ok": False, "proof": f"{path} could not be read: {failure}"}

    resting = (document.get("state") or {}).get("resting") or {}
    for position in positions:
        held = resting.get(f"{position['venue_id']}|{position['symbol']}")
        if held is None:
            position["stop_price"] = None
            position["target_price"] = None
            position["is_protected"] = False
            position["exit_proof"] = (
                f"{path.name} holds no resting exit for {position['symbol']}: this "
                f"position is open with no stop protecting it"
            )
            continue
        position["stop_price"] = held.get("stop_price")
        position["target_price"] = held.get("target_price")
        # How much of the position the resting stop would actually close. A stop
        # is not a yes-or-no fact: on 2026-08-28 `AKEUSDT` held 231,812 units
        # with a stop resting for 198.634 -- 0.09% of it -- and the board painted
        # that as protected. A partial stop is its own state, and it renders as
        # the fraction rather than as a colour (Rule 8).
        resting_quantity = held.get("quantity")
        quantity = position.get("quantity") or 0.0
        covered = (
            None
            if resting_quantity is None or quantity <= 0
            else min(1.0, float(resting_quantity) / quantity)
        )
        position["stop_quantity"] = (
            None if resting_quantity is None else float(resting_quantity)
        )
        position["stop_covers_fraction"] = covered
        # Protected means the whole position is behind the stop. Anything less is
        # partly unprotected and says by how much.
        position["is_protected"] = covered is not None and covered >= 1.0
        entry = position.get("entry_price")
        stop = position.get("stop_price")
        # How far the stop sits from entry, which is what a reader actually judges
        # -- a stop at 78,900 says nothing until you know the entry was 79,544.
        position["stop_distance_fraction"] = (
            None if not entry or stop is None else abs(entry - stop) / entry
        )
        position["exit_proof"] = (
            f"{path.name}: order {held.get('order_id')} resting at {held.get('stop_price')}"
            + (
                f", target {held.get('target_price')}"
                if held.get("target_price") is not None
                else ", no target"
            )
            + (
                ""
                if covered is None or covered >= 1.0
                else (
                    f". It rests for {float(resting_quantity):,.6g} of "
                    f"{quantity:,.6g} held -- {covered:.2%} of this position is "
                    f"behind it and the rest is not"
                )
            )
        )

    protected = sum(1 for position in positions if position.get("is_protected"))
    partly = sum(
        1 for position in positions
        if not position.get("is_protected")
        and (position.get("stop_covers_fraction") or 0) > 0
    )
    return {
        "ok": True,
        "protected": protected,
        "partly_protected": partly,
        "unprotected": len(positions) - protected - partly,
        "proof": (
            f"{path}, written "
            f"{(time.time_ns() - int(document.get('saved_at_ns') or 0)) / 1e9:.0f}s ago -- "
            f"the same file stop-order-manager restores from. "
            f"{protected} of {len(positions)} open position(s) are fully behind a "
            f"stop, {partly} partly, "
            f"{len(positions) - protected - partly} with none"
        ),
    }


def attach_live_prices(positions: list[dict]) -> None:
    """Mark each held position against the tape's latest print, in place.

    Per held symbol, so the cost is the number of open positions rather than the
    size of the universe. A symbol the tape cannot answer for is left as
    `NOT MEASURED` rather than marked at its entry price, which would render a
    losing position as flat.

    **The latest print, not the day's window.** This walked every record since each
    position opened so it could state a high and a low -- three fields no panel
    renders -- and marking 16 positions took 81 seconds for one `/api/trades`
    response on 2026-08-26. The browser gives up long before that, so the rows
    arrived with no prices and the panel looked like a system that had lost its
    positions. The extremes a reader actually sees are `best_unrealised` and
    `worst_unrealised`, which come from `peak-excursion-tracker`'s own checkpoint
    and are measured continuously rather than re-derived per request.
    """
    from build_trade_board import read_last_price

    for position in positions:
        try:
            latest = read_last_price(position["venue_id"], position["symbol"])
        except Exception:
            latest = None
        if latest is None:
            position["price_now"] = None
            position["price_age_seconds"] = None
            position["unrealised_pnl"] = None
            position["price_proof"] = f"{NOT_MEASURED}: the tape has no record for this symbol today"
            continue
        price, read_at_ns = latest
        age_seconds = max(0.0, (time.time_ns() - read_at_ns) / 1e9)
        is_short = position.get("direction") == "short"
        position["price_now"] = price
        position["price_age_seconds"] = age_seconds
        # Marked lot by lot, against the price each lot actually entered at.
        # Marking the whole position against one blended entry is only the same
        # number while nothing has been sold: the blend includes lots that are
        # gone, so on a position scaled out of it prices the remainder at an
        # average the remainder never paid. `binance-usdm|AKEUSDT` held 231,812
        # of 410,652 entered on 2026-08-28, and 44% of its entry basis belonged
        # to lots the bot no longer owned.
        lots = position.get("held_lots") or []
        if not lots:
            position["unrealised_pnl"] = None
            position["price_proof"] = (
                f"{NOT_MEASURED}: the checkpoint carries no lots for this position, "
                f"so there is no entry price to mark against"
            )
            continue
        position["unrealised_pnl"] = sum(
            ((lot["price"] - price) if is_short else (price - lot["price"]))
            * lot["quantity"]
            for lot in lots
        )
        position["price_proof"] = (
            f"tape, last print {age_seconds:.0f}s ago"
        )


def read_trustworthy_after_ns() -> int | None:
    """The operator's boundary for a closed trade worth believing, or None."""
    try:
        from runtime.settings_reader import load_settings_document, settings_directory

        document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
        return int(document.read_value("closed_trades_trustworthy_after_ns"))
    except Exception:
        return None


def capital_in_of(payload: dict) -> float | None:
    """What a closed trade put in, from what the journal recorded.

    None rather than zero when either half is missing: a trade whose entry price
    or quantity was never recorded has an unknown capital, and zero would read as
    a trade that cost nothing (Rule 8).
    """
    entry_price = payload.get("entry_price")
    quantity = payload.get("quantity")
    if not entry_price or not quantity:
        return None
    return abs(float(quantity)) * float(entry_price)


def read_closed_trades() -> tuple[list[dict], dict]:
    """Recent round trips, from the end of the position journal."""
    from runtime.closed_trade_trust import judge_closed_trade

    journal = read_state_directory() / "journal.position-recorder.sqlite"
    tail = read_journal_tail(journal, "closed-trade", CLOSED_TRADE_TAIL_BYTES)
    boundary = read_trustworthy_after_ns()

    trades = []
    for entry in tail.entries[-MOST_CLOSED_TRADES:]:
        payload = entry.get("payload") or {}
        realised = payload.get("realised_pnl")
        fees = payload.get("fees_paid")
        # Shown, never hidden, and never silently. Ten of these rows carry an entry
        # price the market never printed, and a board that dropped them would be
        # quietly editing the record while a board that showed them unmarked would
        # be presenting fiction as measurement. Marked is the only honest third
        # option (Rule 8).
        verdict = judge_closed_trade(payload.get("closed_at_ns"), boundary)
        trades.append(
            {
                "is_trusted": verdict.is_trusted,
                "trust_reason": verdict.reason,
                "venue_id": payload.get("venue_id"),
                "symbol": payload.get("symbol"),
                "direction": payload.get("direction"),
                "quantity": payload.get("quantity"),
                "entry_price": payload.get("entry_price"),
                "exit_price": payload.get("exit_price"),
                # What went into the position, in the quote currency, at the price
                # it was opened at. The same figure the open-positions table shows
                # as `capital_in`, so a reader can compare a closed trade with a
                # live one without doing arithmetic in their head.
                #
                # The notional, not the margin posted: margin is notional divided
                # by the leverage the trade was opened at, and nothing records a
                # per-trade leverage yet, so a margin figure would mean inventing
                # the divisor.
                "capital_in": capital_in_of(payload),
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

    attributions, attribution_provenance = read_attributions()
    for trade in trades:
        # The same identity every decoder derives, from runtime/trade_identity.py:
        # venue, symbol and opening time are what make two round trips distinct,
        # and a board inventing its own key would match nothing.
        trade["trade_id"] = (
            f"{trade.get('venue_id')}:{trade.get('symbol')}:{trade.get('opened_at_ns')}"
        )
        trade["attribution"] = attributions.get(trade["trade_id"])

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
    return trades, {
        "ok": journal.exists(), "proof": proof, "is_whole_file": tail.is_whole_file,
        "attribution_proof": attribution_provenance["proof"],
        "attributions_found": len(attributions),
    }


def read_attributions() -> tuple[dict, dict]:
    """Where the money came from, per trade, from learning-recorder's journal.

    The attribution lives on the bus and nothing recorded it until 2026-08-25, so
    a board had nothing on disk to read. `learning-recorder` now journals it
    (docs/proposals/a-conclusion-nobody-records-is-a-conclusion-nobody-has.md),
    which is why this can exist at all.

    The residual travels with the components, always. `pnl-attributor`'s own
    docstring calls a large residual the most useful thing it produces -- it says
    the model of where PnL comes from is missing something -- and a board showing
    the components without it would present an incomplete reconciliation as a
    complete one.
    """
    journal = read_state_directory() / "journal.learning-recorder.sqlite"
    tail = read_journal_tail(journal, "pnl-attribution", CLOSED_TRADE_TAIL_BYTES)

    by_trade: dict[str, dict] = {}
    for entry in tail.entries:
        payload = entry.get("payload") or {}
        trade_id = payload.get("trade_id")
        if not trade_id:
            continue
        # Latest wins: an attribution recomputed for the same trade supersedes.
        by_trade[str(trade_id)] = {
            "components": payload.get("components") or {},
            "residual": payload.get("residual"),
            "reconciles": payload.get("reconciles"),
            "quote_currency": payload.get("quote_currency"),
            "realised_pnl": payload.get("realised_pnl"),
        }

    if not journal.exists():
        proof = (
            f"no journal at {journal}: learning-recorder has not recorded an "
            f"attribution yet, which is a different fact from a trade having none"
        )
    else:
        proof = (
            f"{len(by_trade)} attribution(s) from the last "
            f"{tail.bytes_read / 1024 ** 2:.0f} MiB of {journal.name}"
        )
    return by_trade, {"ok": journal.exists(), "proof": proof}


def summarise_closed(trades: list[dict]) -> dict:
    """What the shown trades add up to. Said of the shown ones, never of all time.

    Totalled over the trusted rows only. A net figure that included trades whose
    entry price never happened would be an arithmetic answer to a question nobody
    asked, and it would move whenever one of those rows scrolled into the window.
    """
    scored = [t for t in trades if t["net_pnl"] is not None and t.get("is_trusted", True)]
    untrusted = len([t for t in trades if not t.get("is_trusted", True)])
    if not scored:
        return {
            "count": 0, "untrusted_count": untrusted, "net_pnl": None,
            "wins": 0, "win_rate": None, "fees_paid": None, "capital_in": None,
            "return_on_capital": None,
        }
    wins = [t for t in scored if t["net_pnl"] > 0]
    # Only over the rows whose capital is known, and the count of those rows
    # travels with it: a return computed over a subset and presented as the
    # return is the same fiction as a green tile off no measurement.
    with_capital = [t for t in scored if t.get("capital_in")]
    capital_in = sum(t["capital_in"] for t in with_capital) or None
    net_of_those = sum(t["net_pnl"] for t in with_capital)
    return {
        "count": len(scored),
        "untrusted_count": len([t for t in trades if not t.get("is_trusted", True)]),
        "net_pnl": sum(t["net_pnl"] for t in scored),
        "gross_pnl": sum(t["realised_pnl"] for t in scored),
        "fees_paid": sum(t["fees_paid"] for t in scored),
        "wins": len(wins),
        "win_rate": len(wins) / len(scored),
        # What was put in across these round trips, and what came back on it.
        "capital_in": capital_in,
        "trades_with_a_known_capital": len(with_capital),
        "return_on_capital": None if capital_in is None else net_of_those / capital_in,
    }


def build_trade_activity(with_prices: bool = True) -> dict:
    positions, position_provenance = read_open_positions()
    if with_prices and positions:
        attach_live_prices(positions)
    # Attached whether or not prices are: what a symbol has historically done to a
    # call of this direction is read off a checkpoint, not off the tape, so it
    # costs nothing the price marking does and is the same answer either way.
    prediction_provenance = attach_learned_excursions(positions)
    exit_provenance = attach_resting_exits(positions)
    for position in positions:
        position.pop("held_lots", None)
    closed, closed_provenance = read_closed_trades()
    return {
        "generated_at_ns": time.time_ns(),
        "open": {
            "positions": positions,
            "count": len(positions),
            "capital_in": sum(p["capital_in"] for p in positions),
            # What those positions actually tie up, over only the ones whose
            # leverage was recorded -- and how many that is, so a total over
            # half the rows can never read as a total over all of them.
            "capital_committed": sum(
                p["capital_committed"] for p in positions
                if p.get("capital_committed") is not None
            ) if any(p.get("capital_committed") is not None for p in positions) else None,
            "positions_with_a_known_leverage": sum(
                1 for p in positions if p.get("leverage") is not None
            ),
            "unrealised_pnl": sum(
                p["unrealised_pnl"] for p in positions if p.get("unrealised_pnl") is not None
            ) if any(p.get("unrealised_pnl") is not None for p in positions) else None,
            "provenance": position_provenance,
            "prediction_provenance": prediction_provenance,
            "exit_provenance": exit_provenance,
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
