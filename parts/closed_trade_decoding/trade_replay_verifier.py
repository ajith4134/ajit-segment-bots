"""trade-replay-verifier: does the record agree with what the venue actually did.

Every conclusion in this block is computed from the journal. If the journal has
drifted from the fills, every one of those conclusions is confidently wrong, and
nothing else in the system is positioned to notice -- each downstream part trusts its
input by construction.

So this part replays the journal against the venue's own fills and reports
disagreements. What it checks, and why each has actually gone wrong in systems like
this:

- **Quantity.** Partial fills recorded as complete, or a rounded quantity, and every
  per-unit number downstream is wrong by that ratio.
- **Price.** An intended price recorded where an achieved price belonged, which
  makes slippage vanish -- the single most flattering error available.
- **Fees.** Omitted or estimated rather than reported, which turns a marginal
  strategy into a profitable one on paper.
- **Timestamps.** Local clock recorded instead of venue time, which corrupts every
  ordering-based analysis including the sequence miner.
- **Existence.** A fill the venue reports that the journal has no record of at all,
  which is the most serious case: the position is real and the system does not know.

Severity is graded because not every mismatch matters equally, and a verifier that
treats a rounding difference like a missing fill trains everyone to ignore it. What
it never does is repair anything: silently correcting the journal to match the venue
destroys the evidence that the recording path is broken.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.trade_decoding_types import ReplayMismatch
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "trade-replay-verifier"

PART_DECLARATION = PartDeclaration(
    part_id="trade-replay-verifier",
    consumes=("closed-trade", "journal-entry", "fill"),
    produces=("replay-mismatch", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

AGREES = "the-record-matches-the-venue"
MISMATCHED = "the-record-disagrees-with-the-venue"
NO_VENUE_RECORD = "the-venue-reported-nothing-for-this-trade"

# How much a disagreement matters. A verifier that treats a rounding difference like
# a missing fill trains everyone to ignore it.
CRITICAL = "critical"
SERIOUS = "serious"
MINOR = "minor"

QUANTITY = "quantity"
PRICE = "price"
FEE = "fee"
TIMESTAMP = "timestamp"
EXISTENCE = "existence"

CHECKED_FIELDS = (QUANTITY, PRICE, FEE, TIMESTAMP, EXISTENCE)


@dataclass(frozen=True)
class VerificationOutcome:
    trade_id: str
    state: str
    mismatches: tuple
    fields_checked: tuple
    reason: str
    verified_at_ns: int

    @property
    def is_usable(self) -> bool:
        return bool(self.mismatches)

    @property
    def worst_severity(self) -> str | None:
        for severity in (CRITICAL, SERIOUS, MINOR):
            if any(mismatch.severity == severity for mismatch in self.mismatches):
                return severity
        return None


@dataclass
class VerifierStanding:
    trades_verified: int = 0
    trades_that_agreed: int = 0
    mismatches_found: int = 0
    critical: int = 0
    serious: int = 0
    minor: int = 0
    fills_the_journal_never_saw: int = 0
    repairs_made: int = 0


class TradeReplayVerifier:
    """Replays the journal against venue fills and reports every disagreement."""

    def __init__(
        self,
        quantity_tolerance: float,
        price_tolerance: float,
        fee_tolerance: float,
        timestamp_tolerance_seconds: float,
        now_ns=time.time_ns,
    ) -> None:
        for name, value in (
            ("quantity", quantity_tolerance), ("price", price_tolerance),
            ("fee", fee_tolerance), ("timestamp", timestamp_tolerance_seconds),
        ):
            if value < 0:
                raise ValueError(f"the {name} tolerance is not negative")
        self._quantity_tolerance = quantity_tolerance
        self._price_tolerance = price_tolerance
        self._fee_tolerance = fee_tolerance
        self._timestamp_tolerance = timestamp_tolerance_seconds
        self._now_ns = now_ns
        self._journal: dict[str, dict] = {}
        self._venue: dict[str, dict] = {}
        self.standing = VerifierStanding()

    def observe_journal_entry(self, fill_id: str, entry: dict) -> None:
        self._journal[fill_id] = dict(entry)

    def observe_venue_fill(self, fill_id: str, fill: dict) -> None:
        self._venue[fill_id] = dict(fill)

    def verify(self, trade_id: str, fill_ids) -> VerificationOutcome:
        self.standing.trades_verified += 1
        mismatches = []

        for fill_id in fill_ids:
            venue = self._venue.get(fill_id)
            journal = self._journal.get(fill_id)

            if venue is not None and journal is None:
                # The position is real and the system does not know about it.
                self.standing.fills_the_journal_never_saw += 1
                mismatches.append(
                    self._mismatch(
                        trade_id, EXISTENCE, None, fill_id, None, CRITICAL,
                        "the venue reports a fill the journal has no record of. The "
                        "position is real and this system does not know it exists",
                    )
                )
                continue

            if venue is None:
                mismatches.append(
                    self._mismatch(
                        trade_id, EXISTENCE, fill_id, None, None, SERIOUS,
                        "the journal records a fill the venue never reported",
                    )
                )
                continue

            for field_name, tolerance, severity in (
                (QUANTITY, self._quantity_tolerance, CRITICAL),
                (PRICE, self._price_tolerance, SERIOUS),
                (FEE, self._fee_tolerance, MINOR),
            ):
                recorded = journal.get(field_name)
                actual = venue.get(field_name)
                if recorded is None or actual is None:
                    continue
                difference = recorded - actual
                if abs(difference) > tolerance * max(abs(actual), 1e-12):
                    mismatches.append(
                        self._mismatch(
                            trade_id, field_name, recorded, actual, difference, severity,
                            {
                                QUANTITY: (
                                    "every per-unit number downstream is wrong by this "
                                    "ratio"
                                ),
                                PRICE: (
                                    "an intended price recorded where an achieved price "
                                    "belonged makes slippage vanish, which is the most "
                                    "flattering error available"
                                ),
                                FEE: (
                                    "fees estimated rather than reported turn a marginal "
                                    "strategy into a profitable one on paper"
                                ),
                            }[field_name],
                        )
                    )

            recorded_at = journal.get(TIMESTAMP)
            actual_at = venue.get(TIMESTAMP)
            if recorded_at is not None and actual_at is not None:
                drift = abs(recorded_at - actual_at) / 1e9
                if drift > self._timestamp_tolerance:
                    mismatches.append(
                        self._mismatch(
                            trade_id, TIMESTAMP, recorded_at, actual_at, drift, SERIOUS,
                            f"the journal's time differs from the venue's by {drift:.3f}s. "
                            f"A local clock recorded instead of venue time corrupts every "
                            f"ordering-based analysis",
                        )
                    )

        for mismatch in mismatches:
            self.standing.mismatches_found += 1
            if mismatch.severity == CRITICAL:
                self.standing.critical += 1
            elif mismatch.severity == SERIOUS:
                self.standing.serious += 1
            else:
                self.standing.minor += 1

        if not mismatches:
            self.standing.trades_that_agreed += 1
            return VerificationOutcome(
                trade_id=trade_id, state=AGREES, mismatches=(),
                fields_checked=CHECKED_FIELDS,
                reason=(
                    f"the journal matches the venue on every checked field across "
                    f"{len(list(fill_ids))} fill(s)"
                ),
                verified_at_ns=self._now_ns(),
            )

        return VerificationOutcome(
            trade_id=trade_id, state=MISMATCHED, mismatches=tuple(mismatches),
            fields_checked=CHECKED_FIELDS,
            reason=(
                f"{len(mismatches)} disagreement(s). Nothing is repaired: silently "
                f"correcting the journal to match the venue destroys the evidence that "
                f"the recording path is broken"
            ),
            verified_at_ns=self._now_ns(),
        )

    def _mismatch(
        self, trade_id, field_name, journal_value, venue_value, difference, severity,
        reason,
    ) -> ReplayMismatch:
        return ReplayMismatch(
            trade_id=trade_id, field=field_name, journal_value=journal_value,
            venue_value=venue_value, difference=difference, severity=severity,
            reason=reason, found_at_ns=self._now_ns(),
        )


def describe_replay_verification(verifier: TradeReplayVerifier) -> dict:
    return {
        "part_id": PART_ID,
        "trades_verified": verifier.standing.trades_verified,
        "trades_that_agreed": verifier.standing.trades_that_agreed,
        "mismatches_found": verifier.standing.mismatches_found,
        "critical": verifier.standing.critical,
        "serious": verifier.standing.serious,
        "minor": verifier.standing.minor,
        "fills_the_journal_never_saw": verifier.standing.fills_the_journal_never_saw,
        "fields_checked": list(CHECKED_FIELDS),
        "repairs_the_journal": False,
        "repairs_made": verifier.standing.repairs_made,
    }


def run_trade_replay_verifier(
    verifier: TradeReplayVerifier, control_socket, read_trades, publish_mismatches,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for trade_id, fill_ids in read_trades():
            outcome = verifier.verify(trade_id, fill_ids)
            for mismatch in outcome.mismatches:
                publish_mismatches(mismatch)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The journal's record of a fill and the venue's own fill are compared
    field by field when the trade closes. No venue fills arrive in phase 1
    (no key), so every verification finds the venue side missing -- which
    the verifier reports as a mismatch on existence, honestly, until the
    venue reader runs.
    """
    from dataclasses import asdict

    from runtime.input_assembly import Batch
    from runtime.trade_identity import closed_trade_id

    closed = Batch(read=context.bus.reader("closed-trade"))
    entries = Batch(read=context.bus.reader("journal-entry"))
    fills = Batch(read=context.bus.reader("fill"))
    publish_mismatches = context.bus.publisher_for("replay-mismatch")
    verifier = TradeReplayVerifier(
        quantity_tolerance=context.number("replay_quantity_tolerance"),
        price_tolerance=context.number("replay_price_tolerance"),
        fee_tolerance=context.number("replay_fee_tolerance"),
        timestamp_tolerance_seconds=context.number("replay_timestamp_tolerance"),
    )
    fills_of_symbol: dict[tuple[str, str], list] = {}

    def read_trades():
        for entry in entries.payloads():
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if entry.kind == "fill" and payload.get("fill_id"):
                verifier.observe_journal_entry(str(payload["fill_id"]), payload)
        for fill in fills.payloads():
            if not fill.is_paper:
                verifier.observe_venue_fill(fill.fill_id, asdict(fill))
            fills_of_symbol.setdefault((fill.venue_id, fill.symbol), []).append(fill.fill_id)
        jobs = []
        for trade in closed.payloads():
            ids = fills_of_symbol.pop((trade.venue_id, trade.symbol), [])
            jobs.append((closed_trade_id(trade), tuple(ids)))
        return tuple(jobs)

    return run_trade_replay_verifier(
        verifier=verifier,
        control_socket=context.control_socket,
        read_trades=read_trades,
        publish_mismatches=lambda mismatch: publish_mismatches((mismatch,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
