"""corporate-action-adjuster: state the price adjustment an action implies.

NSE states what an action is only in free text -- "Bonus 1:1", "Face Value
Split From Rs 10/- To Rs 2/-", "Dividend - Rs 3.50 Per Share" -- so the ratio
is read from the wording or the action is refused.

**Refusing is the safe direction and guessing is not.** An action this parser
does not understand leaves the price series unadjusted: wrong, visible, and
recoverable the moment the wording is added. A guessed ratio silently rewrites
a price history, and every consumer downstream believes it. Which wordings were
refused is therefore counted and kept: it is this parser's real coverage gap and
the input to its next iteration.

A dividend is carried with a factor of 1.0 rather than dropped. It does move the
price on ex-date, but by an absolute amount rather than a ratio, so it is not a
series adjustment -- and saying so explicitly is different from losing the event.

Bonus is matched before split because NSE publishes combined wordings, and the
bonus is the half carrying a quantity change a position must have.
"""

from __future__ import annotations

import re

from runtime.market_conditions import CorporateAction
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "corporate-action-adjuster"

PART_DECLARATION = PartDeclaration(
    part_id=PART_ID,
    consumes=("corporate-action-report", "broker-instrument-listing"),
    produces=("corporate-action", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# "Bonus 1:1", "Bonus Issue 1:2" -- new shares per share already held.
BONUS = re.compile(r"bonus.*?(\d+)\s*:\s*(\d+)", re.IGNORECASE)
# "Face Value Split From Rs 10/- To Rs 2/-"
SPLIT = re.compile(
    r"split.*?from\s*rs\.?\s*([\d.]+).*?to\s*rs\.?\s*([\d.]+)", re.IGNORECASE
)
DIVIDEND = re.compile(r"dividend", re.IGNORECASE)

# How many distinct refused wordings are kept for the standing. A cap because
# the standing rides on every health message and an uncapped set would grow
# with the corporate-action calendar itself.
REFUSED_WORDINGS_KEPT = 20


class CorporateActionAdjuster:
    """Reads NSE's own wording into a factor, or refuses it."""

    def __init__(self) -> None:
        self._understood = 0
        self._refused = 0
        self._wordings_refused: list[str] = []

    def action_for(self, report) -> CorporateAction | None:
        found = self._factors_from(report.subject)
        if found is None:
            self._refused += 1
            self._remember_refusal(report.subject)
            return None
        kind, price_factor, quantity_factor = found
        self._understood += 1
        return CorporateAction(
            symbol=report.symbol, kind=kind, price_factor=price_factor,
            quantity_factor=quantity_factor, ex_date=report.ex_date,
            stated_from=report.subject, observed_at_ns=report.observed_at_ns,
        )

    def _remember_refusal(self, subject: str) -> None:
        if subject in self._wordings_refused:
            return
        if len(self._wordings_refused) >= REFUSED_WORDINGS_KEPT:
            return
        self._wordings_refused.append(subject)

    def _factors_from(self, subject: str):
        bonus = BONUS.search(subject)
        if bonus:
            new_shares, per_held = int(bonus.group(1)), int(bonus.group(2))
            if per_held <= 0:
                return None
            quantity_factor = (per_held + new_shares) / per_held
            return "bonus", 1.0 / quantity_factor, quantity_factor
        split = SPLIT.search(subject)
        if split:
            face_before, face_after = float(split.group(1)), float(split.group(2))
            if face_before <= 0 or face_after <= 0:
                return None
            price_factor = face_after / face_before
            return "split", price_factor, 1.0 / price_factor
        if DIVIDEND.search(subject):
            return "dividend", 1.0, 1.0
        return None

    @property
    def understood(self) -> int:
        return self._understood

    @property
    def refused(self) -> int:
        return self._refused

    @property
    def wordings_refused(self) -> tuple[str, ...]:
        return tuple(self._wordings_refused)


def describe_adjuster(adjuster: CorporateActionAdjuster) -> dict:
    return {
        "part_id": PART_ID,
        "actions_understood": adjuster.understood,
        "wordings_refused_count": adjuster.refused,
        "wordings_refused": list(adjuster.wordings_refused),
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    broker-instrument-listing is consumed to keep the adjuster's symbol
    vocabulary the broker's own, so an action on a name the broker does not
    list is not published as an adjustment nothing can apply.
    """
    from runtime.input_assembly import Batch

    reports = Batch(read=context.bus.reader("corporate-action-report"))
    listings = Batch(read=context.bus.reader("broker-instrument-listing"))
    publish_actions = context.bus.publisher_for("corporate-action")
    adjuster = CorporateActionAdjuster()
    known_symbols: set[str] = set()

    def tick() -> None:
        for listing in listings.payloads():
            known_symbols.add(listing.trading_symbol)
        actions = tuple(
            action
            for report in reports.payloads()
            if (action := adjuster.action_for(report)) is not None
        )
        if actions:
            publish_actions(actions)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_adjuster(adjuster)
        | {"instruments_known": len(known_symbols)},
    )


__all__ = [
    "CorporateActionAdjuster",
    "PART_DECLARATION",
    "PART_ID",
    "REFUSED_WORDINGS_KEPT",
    "describe_adjuster",
    "start_part",
]
