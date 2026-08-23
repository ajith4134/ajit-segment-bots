"""signal-outcome-labeller: did the move a detector expected actually happen?

**This part exists because the system could not otherwise start.** Every route to a
trade needs a trained conviction model; the model's only source of `training-label`
was the outcome of a trade; and a trade needs the model. Measured, not inferred: an
untrained model returns `logistic(0)` and reports itself unfitted, so the composer
forms no opinion and nothing after it ever runs. The full argument is in
`docs/proposals/signal-outcome-labelling.md`.

A detector's candidate already states everything a label needs -- the direction it
expects, and the horizon it expects it in. The prices that arrive next say what
happened. That is a label, and it needs no trade, no position and no capital.

**Live prices, never a replay (RL-071).** This part sits on the bus like any other:
the candidates are the ones the detectors are raising now and the prices are the
ones the venues are sending now. The tape is the record of what arrived, not a
source anything here reads.

How a claim is judged, and why it is not simply "where did the price end up":

- **Two barriers and a clock.** A claim resolves *right* when the price moves the
  threshold in the claimed direction first, *wrong* when it moves the threshold
  against first, and *unresolved* when the horizon expires with neither reached.
  Labelling on the final price alone would call a claim wrong when the move
  happened and gave itself back, and right when nothing happened but the last tick
  drifted the correct way.
- **The threshold is a fraction of price, not a fixed amount**, because a move that
  is enormous in one symbol is nothing in another.
- **An unresolved claim is dropped, never labelled false.** "The move did not
  happen within the horizon" and "the move went the other way" are different facts,
  and training on their union teaches the model that quiet markets are wrong
  predictions.

What this label speaks to is deliberately narrow. It says the **setup** was right or
wrong -- nothing about entry timing, exit timing or size, which are decisions no
signal made. `TrainingLabel.labels` is keyed by component precisely so that a label
about one of them cannot be mistaken for a label about the trade.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learning_types import THE_SETUP_WAS_RIGHT, TrainingLabel
from runtime.market_signal import LONG, SHORT
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "signal-outcome-labeller"

PART_DECLARATION = PartDeclaration(
    part_id="signal-outcome-labeller",
    consumes=("entry-candidate", "market-data"),
    produces=("training-label", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

CLAIM_OPENED = "opened"
RESOLVED_RIGHT = "the-expected-move-happened"
RESOLVED_WRONG = "the-price-went-the-other-way"
UNRESOLVED = "neither-barrier-was-reached-inside-the-horizon"

REFUSED_NO_PRICE = "no-price-has-been-seen-for-this-symbol"
REFUSED_UNKNOWN_DIRECTION = "the-candidate-names-no-tradeable-direction"
REFUSED_AT_CAPACITY = "too-many-claims-are-already-open"
REFUSED_ALREADY_OPEN = "this-detector-already-has-an-open-claim-on-this-symbol"


@dataclass
class OpenClaim:
    """One detector's claim, waiting for the market to settle it."""

    detector: str
    venue_id: str
    symbol: str
    direction: str
    regime: str
    horizon_seconds: float
    evidence: dict
    price_at_claim: float
    claimed_at_ns: int
    deadline_ns: int
    best_favourable_fraction: float = 0.0
    worst_adverse_fraction: float = 0.0

    def move_fraction(self, price: float) -> float:
        """How far price has moved in the claimed direction, as a fraction.

        Signed so that positive is always *towards* what the detector said, whether
        it said long or short. A caller comparing raw prices would have to know
        which way round this claim was, and that is exactly the knowledge this
        method exists to hold in one place.
        """
        change = (price - self.price_at_claim) / self.price_at_claim
        return change if self.direction == LONG else -change


@dataclass
class LabellerStanding:
    """What this part has actually judged. Nothing here is asserted."""

    candidates_seen: int = 0
    claims_opened: int = 0
    claims_refused: int = 0
    labels_published: int = 0
    resolved_right: int = 0
    resolved_wrong: int = 0
    unresolved: int = 0
    prices_observed: int = 0
    open_claims: int = 0
    by_refusal: dict = field(default_factory=dict)
    by_detector: dict = field(default_factory=dict)

    @property
    def measured_hit_rate(self) -> float | None:
        """The share of resolved claims that were right, or None if none resolved.

        None rather than zero: a labeller that has resolved nothing has measured
        nothing, and a hit rate of 0.0 would read as a detector that is never right.
        """
        resolved = self.resolved_right + self.resolved_wrong
        return self.resolved_right / resolved if resolved else None


class SignalOutcomeLabeller:
    """Holds each claim open until the market resolves it, then labels it."""

    def __init__(
        self,
        move_fraction: float,
        maximum_open_claims: int,
        now_ns=time.time_ns,
    ) -> None:
        if move_fraction <= 0:
            raise ValueError(
                "a move fraction of zero resolves every claim right on the first tick that "
                "moves at all, which would label noise as prediction"
            )
        self._move_fraction = move_fraction
        self._maximum_open_claims = maximum_open_claims
        self._now_ns = now_ns
        self._latest_price: dict[tuple[str, str], float] = {}
        self._open: dict[tuple[str, str, str], OpenClaim] = {}
        # The same claims indexed by the symbol they are about. Every trade has to
        # update every claim on that symbol, and scanning all open claims per trade
        # is quadratic: at the 5000-claim bound and the measured 285 trades a second
        # that is over a million comparisons a second spent on symbols the trade
        # says nothing about. The index is what keeps this part able to keep up,
        # and a part that falls behind loses its input rather than delaying it.
        self._open_by_symbol: dict[tuple[str, str], dict] = {}
        self.standing = LabellerStanding()

    # -- watching the market -------------------------------------------------

    def observe_price(self, venue_id: str, symbol: str, price: float) -> None:
        """One live trade. Every open claim on this symbol is measured against it."""
        self.standing.prices_observed += 1
        self._latest_price[(venue_id, symbol)] = price
        for claim in self._open_by_symbol.get((venue_id, symbol), {}).values():
            moved = claim.move_fraction(price)
            claim.best_favourable_fraction = max(claim.best_favourable_fraction, moved)
            claim.worst_adverse_fraction = min(claim.worst_adverse_fraction, moved)

    def observe_candidate(self, candidate, regime_name: str = "any") -> tuple[OpenClaim | None, str]:
        """Open a claim on one candidate, or refuse it and say why."""
        self.standing.candidates_seen += 1

        if candidate.direction not in (LONG, SHORT):
            return None, self._refuse(REFUSED_UNKNOWN_DIRECTION)

        price = self._latest_price.get((candidate.venue_id, candidate.symbol))
        if price is None or price <= 0:
            # Without a price at the moment of the claim there is nothing to
            # measure the move against, and inventing one from the next tick would
            # give the detector a head start it did not have.
            return None, self._refuse(REFUSED_NO_PRICE)

        key = (candidate.detector, candidate.venue_id, candidate.symbol)
        if key in self._open:
            # One claim per detector per symbol. A detector that fires every tick
            # would otherwise open thousands of overlapping claims on one move and
            # teach the model that one event happened thousands of times.
            return None, self._refuse(REFUSED_ALREADY_OPEN)
        if len(self._open) >= self._maximum_open_claims:
            return None, self._refuse(REFUSED_AT_CAPACITY)

        claimed_at = self._now_ns()
        claim = OpenClaim(
            detector=candidate.detector,
            venue_id=candidate.venue_id,
            symbol=candidate.symbol,
            direction=candidate.direction,
            regime=regime_name,
            horizon_seconds=candidate.horizon_seconds,
            evidence=dict(candidate.evidence),
            price_at_claim=price,
            claimed_at_ns=claimed_at,
            deadline_ns=claimed_at + int(candidate.horizon_seconds * 1e9),
        )
        self._open[key] = claim
        self._open_by_symbol.setdefault((claim.venue_id, claim.symbol), {})[key] = claim
        self.standing.claims_opened += 1
        self.standing.open_claims = len(self._open)
        self.standing.by_detector[claim.detector] = self.standing.by_detector.get(claim.detector, 0) + 1
        return claim, CLAIM_OPENED

    # -- settling them -------------------------------------------------------

    def resolve_settled_claims(self) -> tuple[TrainingLabel, ...]:
        """Label every claim the market has settled, and drop those it has not.

        A claim resolves the moment a barrier is reached rather than at the
        horizon: waiting would let a move that happened and reversed be labelled by
        the reversal.
        """
        now = self._now_ns()
        labels = []
        settled_keys = []
        for key, claim in self._open.items():
            verdict = self._verdict_for(claim, now)
            if verdict is None:
                continue
            settled_keys.append(key)
            if verdict == UNRESOLVED:
                self.standing.unresolved += 1
                continue
            labels.append(self._label_for(claim, verdict, now))
        for key in settled_keys:
            claim = self._open.pop(key)
            on_symbol = self._open_by_symbol.get((claim.venue_id, claim.symbol))
            if on_symbol is not None:
                on_symbol.pop(key, None)
                if not on_symbol:
                    # Dropped rather than left empty: the map is keyed by every
                    # symbol ever claimed on, and an empty entry per symbol is a
                    # slow leak in a process that runs for weeks.
                    del self._open_by_symbol[(claim.venue_id, claim.symbol)]
        self.standing.open_claims = len(self._open)
        return tuple(labels)

    def _verdict_for(self, claim: OpenClaim, now_ns: int) -> str | None:
        """Right, wrong, unresolved -- or None while the claim is still open.

        Favourable is checked first when both barriers were crossed between two
        ticks: a claim whose price gapped through both is one this part cannot
        order, and the tie is broken towards the detector rather than against it,
        recorded here so it is a decision rather than an accident of evaluation
        order.
        """
        if claim.best_favourable_fraction >= self._move_fraction:
            return RESOLVED_RIGHT
        if -claim.worst_adverse_fraction >= self._move_fraction:
            return RESOLVED_WRONG
        if now_ns >= claim.deadline_ns:
            return UNRESOLVED
        return None

    def _label_for(self, claim: OpenClaim, verdict: str, now_ns: int) -> TrainingLabel:
        was_right = verdict == RESOLVED_RIGHT
        if was_right:
            self.standing.resolved_right += 1
        else:
            self.standing.resolved_wrong += 1
        self.standing.labels_published += 1
        return TrainingLabel(
            venue_id=claim.venue_id,
            symbol=claim.symbol,
            detector=claim.detector,
            regime=claim.regime,
            # One component only. This says the setup was right or wrong, and says
            # nothing about timing or size, which no signal decided.
            labels={THE_SETUP_WAS_RIGHT: was_right},
            horizon_seconds=claim.horizon_seconds,
            seconds_to_resolve=(now_ns - claim.claimed_at_ns) / 1e9,
            resolved_within_horizon=now_ns < claim.deadline_ns,
            features=dict(claim.evidence),
            built_at_ns=now_ns,
            claimed_at_ns=claim.claimed_at_ns,
            # What the claim measured while it was open. Reported rather than
            # dropped: these two numbers are the whole reason the system can
            # place a stop before it has ever closed a trade.
            direction=claim.direction,
            best_favourable_fraction=claim.best_favourable_fraction,
            worst_adverse_fraction=claim.worst_adverse_fraction,
        )

    def _refuse(self, reason: str) -> str:
        self.standing.claims_refused += 1
        self.standing.by_refusal[reason] = self.standing.by_refusal.get(reason, 0) + 1
        return reason

    @property
    def open_claims(self) -> tuple[OpenClaim, ...]:
        return tuple(self._open.values())


def describe_labelling(labeller: SignalOutcomeLabeller) -> dict:
    """Everything measured about this part. A hit rate of None means none resolved."""
    return {
        "part_id": PART_ID,
        "candidates_seen": labeller.standing.candidates_seen,
        "claims_opened": labeller.standing.claims_opened,
        "claims_refused": labeller.standing.claims_refused,
        "open_claims": labeller.standing.open_claims,
        "labels_published": labeller.standing.labels_published,
        "resolved_right": labeller.standing.resolved_right,
        "resolved_wrong": labeller.standing.resolved_wrong,
        "unresolved": labeller.standing.unresolved,
        "measured_hit_rate": labeller.standing.measured_hit_rate,
        "prices_observed": labeller.standing.prices_observed,
        "by_refusal": dict(sorted(labeller.standing.by_refusal.items())),
        "by_detector": dict(sorted(labeller.standing.by_detector.items())),
    }


def run_signal_outcome_labeller(
    labeller: SignalOutcomeLabeller,
    control_socket,
    read_prices_and_candidates,
    publish_labels,
    health_interval_seconds: float,
    emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_prices_and_candidates(labeller)
        publish_labels(labeller.resolve_settled_claims())

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

    Prices are observed before candidates are opened within a single tick, and the
    order matters: a claim needs the price at the moment it was made, and a
    candidate that arrived in the same batch as the trade that triggered it would
    otherwise be measured against a price from before the detector saw anything.
    """
    from runtime.input_assembly import Batch

    trades = Batch(read=context.bus.reader("market-data"))
    candidates = Batch(read=context.bus.reader("entry-candidate"))
    publish_labels = context.bus.publisher_for("training-label")

    def read_prices_and_candidates(labeller: SignalOutcomeLabeller) -> None:
        for trade in trades.payloads():
            labeller.observe_price(trade.venue_id, trade.symbol, trade.price)
        for candidate in candidates.payloads():
            labeller.observe_candidate(candidate)

    return run_signal_outcome_labeller(
        labeller=SignalOutcomeLabeller(
            move_fraction=context.number("signal_label_move_fraction"),
            maximum_open_claims=int(context.number("signal_label_maximum_open_claims")),
        ),
        control_socket=context.control_socket,
        read_prices_and_candidates=read_prices_and_candidates,
        publish_labels=publish_labels,
        health_interval_seconds=context.health_interval_seconds,
        emit_health=context.emit_health,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
    )


__all__ = [
    "CLAIM_OPENED",
    "LabellerStanding",
    "OpenClaim",
    "PART_DECLARATION",
    "PART_ID",
    "RESOLVED_RIGHT",
    "RESOLVED_WRONG",
    "SignalOutcomeLabeller",
    "UNRESOLVED",
    "describe_labelling",
    "run_signal_outcome_labeller",
    "start_part",
]
