"""trader-record-verifier: how much of a claimed record actually checks out.

Every number a trader publishes about themselves is a marketing number until it is
reconstructed from positions. This part reconstructs what it can and, more
importantly, reports what it could not -- because the gap between claimed and
verifiable is the single most useful statistic about a public trader.

Four decompositions do the work, and each one exists because it catches a specific
way an impressive record is manufactured:

- **How long.** A 300% quarter is a coin that landed the same way for ninety days.
  Days covered is reported as its own number so nothing can average a short record
  into looking established.
- **How many trades.** Return per trade over 900 trades is a process; the same
  return over 4 trades is an outcome. `trades_seen` separates them.
- **The largest single contribution.** If one position produced most of the record,
  the record describes that position. This is the shape behind nearly every
  spectacular public account, so it is computed explicitly rather than left for a
  reader to notice.
- **Leverage used.** A return is not comparable across leverage. A 40% return at
  2x and at 50x are different skills, and one of them is unrepeatable.

The verifier never upgrades a claim. If the positions it can see support less than
the claim, the smaller number is what it reports, and the claim is kept beside it
rather than replaced -- a claim that turned out to be inflated is evidence about
the trader, and deleting it destroys that evidence.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.external_research_types import VerifiedRecord
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "trader-record-verifier"

PART_DECLARATION = PartDeclaration(
    part_id="trader-record-verifier",
    consumes=("tracked-trader", "external-position"),
    produces=("verified-record", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

VERIFIED = "verified"
PARTLY_VERIFIED = "part-of-it-checks-out"
UNVERIFIABLE = "nothing-can-be-reconstructed"
TOO_SHORT = "the-record-is-too-short-to-mean-anything"
CONTRADICTED = "the-positions-do-not-support-the-claim"
ONE_TRADE = "one-position-produced-most-of-it"


@dataclass
class VerifierStanding:
    traders_examined: int = 0
    verified: int = 0
    partly_verified: int = 0
    unverifiable: int = 0
    contradicted: int = 0
    too_short: int = 0
    one_trade_records: int = 0
    total_claim_shortfall: float = 0.0


class TraderRecordVerifier:
    """Reconstructs a record from positions and reports the gap to the claim."""

    def __init__(
        self,
        minimum_days: float,
        minimum_trades: int,
        contradiction_tolerance: float,
        single_trade_share: float,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_days <= 0 or minimum_trades < 1:
            raise ValueError("a record needs some length and some trades to be a record")
        if not 0.0 < contradiction_tolerance < 1.0:
            raise ValueError(
                "the tolerance is the fraction by which reconstruction may fall short "
                "before the claim is called contradicted"
            )
        if not 0.0 < single_trade_share < 1.0:
            raise ValueError("the single-trade share is a fraction of the record")
        self._minimum_days = minimum_days
        self._minimum_trades = minimum_trades
        self._contradiction_tolerance = contradiction_tolerance
        self._single_trade_share = single_trade_share
        self._now_ns = now_ns
        # trader_id -> {position_key: realised contribution}
        self._contributions: dict[str, dict] = {}
        self._first_at_ns: dict[str, int] = {}
        self._last_at_ns: dict[str, int] = {}
        self._max_leverage: dict[str, float] = {}
        self._claims: dict[str, float] = {}
        self.standing = VerifierStanding()

    def observe_claim(self, trader_id: str, claimed_return: float) -> None:
        self._claims[trader_id] = claimed_return

    def observe_closed_position(
        self, trader_id: str, position_key: str, realised_return: float,
        opened_at_ns: int, closed_at_ns: int, leverage: float | None = None,
    ) -> None:
        """A position whose outcome is known. Only these can verify anything."""
        self._contributions.setdefault(trader_id, {})[position_key] = realised_return
        first = self._first_at_ns.get(trader_id)
        self._first_at_ns[trader_id] = opened_at_ns if first is None else min(first, opened_at_ns)
        last = self._last_at_ns.get(trader_id)
        self._last_at_ns[trader_id] = closed_at_ns if last is None else max(last, closed_at_ns)
        if leverage is not None:
            self._max_leverage[trader_id] = max(
                self._max_leverage.get(trader_id, 0.0), leverage
            )

    def verify(self, trader_id: str) -> VerifiedRecord:
        self.standing.traders_examined += 1
        claim = self._claims.get(trader_id)
        contributions = self._contributions.get(trader_id, {})
        trades = len(contributions)
        days = self._days_covered(trader_id)
        leverage = self._max_leverage.get(trader_id)

        if trades == 0:
            self.standing.unverifiable += 1
            return self._record(
                trader_id, claim, None, 0, days, None, leverage, UNVERIFIABLE,
                "no closed position could be reconstructed, so the claim is a claim. "
                "That is not the same as the claim being false, and it is not evidence "
                "either way",
            )

        # Returns compound rather than add: a record is the product of its legs.
        verifiable = 1.0
        for contribution in contributions.values():
            verifiable *= 1.0 + contribution
        verifiable -= 1.0

        largest_share = self._largest_share(contributions, verifiable)
        if largest_share is not None and largest_share > self._single_trade_share:
            self.standing.one_trade_records += 1

        if days < self._minimum_days or trades < self._minimum_trades:
            self.standing.too_short += 1
            return self._record(
                trader_id, claim, verifiable, trades, days, largest_share, leverage,
                TOO_SHORT,
                f"{trades} trade(s) over {days:.1f} day(s), below the "
                f"{self._minimum_trades}/{self._minimum_days:.0f} bar. A short record is a "
                f"coin that landed the same way, and averaging cannot lengthen it",
            )

        if claim is not None and verifiable < claim * (1.0 - self._contradiction_tolerance):
            self.standing.contradicted += 1
            self.standing.total_claim_shortfall += claim - verifiable
            return self._record(
                trader_id, claim, verifiable, trades, days, largest_share, leverage,
                CONTRADICTED,
                f"claimed {claim:+.1%}, reconstructed {verifiable:+.1%} from {trades} "
                f"closed position(s). The smaller number is what is reported; the claim "
                f"is kept beside it because an inflated claim is evidence about the trader",
            )

        if largest_share is not None and largest_share > self._single_trade_share:
            return self._record(
                trader_id, claim, verifiable, trades, days, largest_share, leverage,
                ONE_TRADE,
                f"{largest_share:.0%} of the record came from one position. The record "
                f"describes that position, not a process, and one position does not repeat",
            )

        if claim is None:
            self.standing.partly_verified += 1
            return self._record(
                trader_id, claim, verifiable, trades, days, largest_share, leverage,
                PARTLY_VERIFIED,
                f"{verifiable:+.1%} reconstructed from {trades} position(s) over "
                f"{days:.1f} day(s), with nothing claimed to compare it against",
            )

        self.standing.verified += 1
        return self._record(
            trader_id, claim, verifiable, trades, days, largest_share, leverage, VERIFIED,
            f"{verifiable:+.1%} reconstructed against {claim:+.1%} claimed, from {trades} "
            f"position(s) over {days:.1f} day(s)"
            + (f" at up to {leverage:.0f}x" if leverage else "")
            + ". The leverage is reported because a return is not comparable without it",
        )

    def _largest_share(self, contributions, total) -> float | None:
        if not contributions or total <= 0:
            return None
        gains = [value for value in contributions.values() if value > 0]
        if not gains:
            return None
        return max(gains) / sum(gains)

    def _days_covered(self, trader_id) -> float:
        first = self._first_at_ns.get(trader_id)
        last = self._last_at_ns.get(trader_id)
        if first is None or last is None:
            return 0.0
        return max(last - first, 0) / 86_400e9

    def _record(
        self, trader_id, claim, verifiable, trades, days, largest_share, leverage,
        state, reason,
    ) -> VerifiedRecord:
        return VerifiedRecord(
            trader_id=trader_id, claimed_return=claim, verifiable_return=verifiable,
            trades_seen=trades, days_covered=days,
            largest_single_contribution=largest_share, was_leveraged_beyond=leverage,
            verification_state=state, reason=reason, verified_at_ns=self._now_ns(),
        )


def describe_verification(verifier: TraderRecordVerifier) -> dict:
    return {
        "part_id": PART_ID,
        "traders_examined": verifier.standing.traders_examined,
        "verified": verifier.standing.verified,
        "partly_verified": verifier.standing.partly_verified,
        "unverifiable": verifier.standing.unverifiable,
        "claims_contradicted": verifier.standing.contradicted,
        "records_too_short": verifier.standing.too_short,
        "records_that_are_one_trade": verifier.standing.one_trade_records,
        "total_claim_shortfall": verifier.standing.total_claim_shortfall,
        "ever_upgrades_a_claim": False,
    }


def run_trader_record_verifier(
    verifier: TraderRecordVerifier, control_socket, read_traders, publish_records,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for trader_id in read_traders(verifier):
            publish_records(verifier.verify(trader_id))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
