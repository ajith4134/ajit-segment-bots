"""Which closed trades may be learned from, and which record a defect instead.

**Substrate, not a part (RL-069).** Twenty closed-trade-decoding parts read
`closed-trade`, and every one of them must refuse the same rows for the same
reason. Putting the test in one of them would make the other nineteen import a
part (T-4); writing it twenty times would let the twenty drift apart, and a
decoder that quietly disagreed with its neighbours about which trades are real
is worse than one that refuses everything.

**The defect this guards, measured.** `position-close-detector` backed a round
trip's entry price out of its closing fill, so a trade sold in slices reported the
whole trip's profit divided by whatever fraction happened to close last. Ten of
the 113 trades on this machine carry an entry the market never printed -- AAVE at
217.02 falling to 132.57 in 37 seconds, ETH at 3297 falling to 2517 -- and the
smaller the last slice, the further from the truth. `quantity` was wrong the same
way, reporting the last slice while the profit and fees covered the whole trip.

**Why a timestamp and not a repair.** The journal is append-only and
digest-chained. Rewriting history to hide a defect is the one repair that costs
more than the defect: the chain is what makes the record evidence, and a record
edited to look better is not evidence of anything. So the bad rows stay, and what
changes is who is willing to learn from them.

**Why the boundary is when the spine RAN the fix, not when it was committed.**
Those are forty-eight minutes apart here, and every trade closed between them was
recorded by the old code. Code being in git is not code being in the process.
"""

from __future__ import annotations

from dataclasses import dataclass

# Why a trade is being refused, as a state rather than a message. A board renders
# each differently, and "recorded before the entry-price fix" is a fact about this
# system's history rather than a fault in the trade.
TRUSTED = "trusted"
RECORDED_BEFORE_THE_ENTRY_PRICE_FIX = "recorded-before-the-entry-price-fix"
NO_CLOSING_TIME = "the-trade-carries-no-closing-time"


@dataclass(frozen=True)
class TrustVerdict:
    """Whether one closed trade may be learned from, and why not when it may not."""

    is_trusted: bool
    reason: str

    def __bool__(self) -> bool:
        return self.is_trusted


def judge_closed_trade(closed_at_ns, trustworthy_after_ns) -> TrustVerdict:
    """Whether this closed trade was recorded by code that priced it correctly.

    `trustworthy_after_ns` has no default and none is invented (RL-061): it is the
    operator's `closed_trades_trustworthy_after_ns`, and a caller that has not read
    it has not decided anything. Passing None means the operator stated no bound,
    and then every trade is trusted -- which is the right answer on a machine whose
    journal only ever held correct rows.
    """
    if closed_at_ns is None:
        # A trade with no closing time cannot be placed on either side of the
        # boundary. Refused rather than assumed recent: assuming recent is how a
        # bad row gets learned from.
        return TrustVerdict(False, NO_CLOSING_TIME)
    if trustworthy_after_ns is None:
        return TrustVerdict(True, TRUSTED)
    if int(closed_at_ns) < int(trustworthy_after_ns):
        return TrustVerdict(False, RECORDED_BEFORE_THE_ENTRY_PRICE_FIX)
    return TrustVerdict(True, TRUSTED)


@dataclass
class TrustStanding:
    """What a decoder saw and what it declined to learn from.

    Counted rather than silently dropped. A decoder that refused every trade and a
    decoder that was handed none look identical from the outside, and the whole
    point of the boundary is that the difference is visible (Rule 8).
    """

    trades_seen: int = 0
    trades_trusted: int = 0
    refused_recorded_before_the_fix: int = 0
    refused_no_closing_time: int = 0

    def record(self, verdict: TrustVerdict) -> bool:
        self.trades_seen += 1
        if verdict.is_trusted:
            self.trades_trusted += 1
            return True
        if verdict.reason == RECORDED_BEFORE_THE_ENTRY_PRICE_FIX:
            self.refused_recorded_before_the_fix += 1
        else:
            self.refused_no_closing_time += 1
        return False

    def as_standing(self) -> dict:
        return {
            "trades_seen": self.trades_seen,
            "trades_trusted": self.trades_trusted,
            "refused_recorded_before_the_fix": self.refused_recorded_before_the_fix,
            "refused_no_closing_time": self.refused_no_closing_time,
        }


def read_trustworthy_after_ns(context) -> int | None:
    """The operator's boundary, or None when they have set none.

    None is a real answer and not a failure: a machine whose journal only ever
    held correctly priced trades needs no boundary, and inventing one there would
    refuse rows for a defect that never touched them.
    """
    try:
        return int(context.number("closed_trades_trustworthy_after_ns"))
    except Exception:
        return None
