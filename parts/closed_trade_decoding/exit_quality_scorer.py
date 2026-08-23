"""exit-quality-scorer: how much of the move the exit kept, and what it sat through.

One number cannot describe an exit. Capturing 60% of the favourable move having
never gone underwater is a good exit; capturing the same 60% after sitting through a
drawdown as large as the eventual gain is a different trade that happened to end in
the same place. So this part reports both sides of the excursion (RL-042).

Three properties that stop the score being hindsight dressed as measurement:

- **The benchmark is the excursion that actually occurred, not the best price of the
  day.** An exit is measured against what the position reached while it was open,
  which is the only range any exit rule could have acted within.
- **Capturing everything is not the target.** Exiting at the exact peak is
  unrepeatable, and a score that rewards it teaches a system to hold for peaks and
  give back real gains. A high capture on one trade is noted; it becomes evidence
  only across many.
- **An exit that avoided a larger loss scores as a good exit.** Measuring only the
  favourable side makes every stop look like a failure, which is exactly backwards
  -- the stops that worked are invisible in a capture-only view.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.trade_decoding_types import ExitQuality
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "exit-quality-scorer"

PART_DECLARATION = PartDeclaration(
    part_id="exit-quality-scorer",
    consumes=("trade-episode", "peak-excursion"),
    produces=("exit-quality", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

SCORED = "scored"
NO_EXCURSION = "no-excursion-was-recorded-for-this-trade"
NEVER_WENT_FAVOURABLE = "the-position-never-moved-in-its-favour"


@dataclass(frozen=True)
class ExitScore:
    trade_id: str
    state: str
    quality: ExitQuality | None
    avoided_a_larger_loss: bool
    reason: str
    scored_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state in (SCORED, NEVER_WENT_FAVOURABLE) and self.quality is not None


@dataclass
class ExitScorerStanding:
    exits_examined: int = 0
    scored: int = 0
    without_an_excursion: int = 0
    never_went_favourable: int = 0
    exits_that_avoided_a_larger_loss: int = 0
    exits_that_gave_back_most_of_the_move: int = 0
    total_captured: float = 0.0


class ExitQualityScorer:
    """Reports what an exit captured and what it sat through, never one number."""

    def __init__(self, gave_back_threshold: float, now_ns=time.time_ns) -> None:
        if not 0.0 < gave_back_threshold < 1.0:
            raise ValueError(
                "the give-back threshold is the fraction of a favourable move handed "
                "back before the exit is worth naming as late"
            )
        self._gave_back_threshold = gave_back_threshold
        self._now_ns = now_ns
        self._excursions: dict[str, dict] = {}
        self.standing = ExitScorerStanding()

    def observe_excursion(
        self, trade_id: str, entry_price: float, best_price: float, worst_price: float,
        direction: str, price_after_exit: float | None = None,
    ) -> None:
        """The range the position actually reached while it was open."""
        self._excursions[trade_id] = {
            "entry": entry_price, "best": best_price, "worst": worst_price,
            "direction": direction, "after": price_after_exit,
        }

    def score(self, trade_id: str, exit_price: float) -> ExitScore:
        self.standing.exits_examined += 1
        excursion = self._excursions.get(trade_id)
        if excursion is None:
            self.standing.without_an_excursion += 1
            return self._score(
                trade_id, NO_EXCURSION, None, False,
                "no excursion was recorded, so there is no range this exit could be "
                "measured within",
            )

        entry = excursion["entry"]
        is_long = excursion["direction"] == "long"
        sign = 1.0 if is_long else -1.0

        favourable = sign * (excursion["best"] - entry)
        adverse = -sign * (excursion["worst"] - entry)
        captured_amount = sign * (exit_price - entry)

        # An exit that avoided a larger loss is a good exit, and a capture-only view
        # makes every stop that worked invisible.
        avoided = False
        if excursion["after"] is not None:
            after = sign * (excursion["after"] - entry)
            avoided = after < captured_amount
        if avoided:
            self.standing.exits_that_avoided_a_larger_loss += 1

        if favourable <= 0:
            self.standing.never_went_favourable += 1
            return self._score(
                trade_id, NEVER_WENT_FAVOURABLE,
                ExitQuality(
                    trade_id=trade_id, captured_fraction=None, gave_back=None,
                    suffered_fraction=(adverse / abs(entry) if entry else None),
                    exit_price=exit_price, peak_price=excursion["best"],
                    is_measurable=True,
                    reason=(
                        "the position never moved in its favour, so there was nothing to "
                        "capture"
                        + (
                            ". The exit did avoid a larger loss, which a capture-only "
                            "view would have scored as a failure"
                            if avoided
                            else ""
                        )
                    ),
                    scored_at_ns=self._now_ns(),
                ),
                avoided,
                "nothing favourable to capture",
            )

        captured_fraction = captured_amount / favourable
        gave_back = favourable - captured_amount
        suffered_fraction = adverse / favourable if favourable > 0 else None

        if gave_back / favourable >= self._gave_back_threshold:
            self.standing.exits_that_gave_back_most_of_the_move += 1

        self.standing.scored += 1
        self.standing.total_captured += captured_fraction

        return self._score(
            trade_id, SCORED,
            ExitQuality(
                trade_id=trade_id,
                captured_fraction=captured_fraction,
                gave_back=gave_back,
                suffered_fraction=suffered_fraction,
                exit_price=exit_price,
                peak_price=excursion["best"],
                is_measurable=True,
                reason=(
                    f"captured {captured_fraction:.0%} of the favourable move, having sat "
                    f"through {suffered_fraction:.0%} of it against"
                    if suffered_fraction is not None
                    else f"captured {captured_fraction:.0%} of the favourable move"
                ) + (
                    ". Exiting at the exact peak is unrepeatable, so a high capture on one "
                    "trade is noted rather than rewarded"
                    if captured_fraction > 0.95
                    else ""
                ),
                scored_at_ns=self._now_ns(),
            ),
            avoided,
            f"captured {captured_fraction:.0%}, suffered "
            + (f"{suffered_fraction:.0%}" if suffered_fraction is not None else "nothing"),
        )

    def _score(self, trade_id, state, quality, avoided, reason) -> ExitScore:
        return ExitScore(
            trade_id=trade_id, state=state, quality=quality,
            avoided_a_larger_loss=avoided, reason=reason, scored_at_ns=self._now_ns(),
        )


def describe_exit_scoring(scorer: ExitQualityScorer) -> dict:
    return {
        "part_id": PART_ID,
        "exits_examined": scorer.standing.exits_examined,
        "scored": scorer.standing.scored,
        "without_an_excursion": scorer.standing.without_an_excursion,
        "never_went_favourable": scorer.standing.never_went_favourable,
        "exits_that_avoided_a_larger_loss": (
            scorer.standing.exits_that_avoided_a_larger_loss
        ),
        "exits_that_gave_back_most_of_the_move": (
            scorer.standing.exits_that_gave_back_most_of_the_move
        ),
        "mean_captured_fraction": (
            scorer.standing.total_captured / scorer.standing.scored
            if scorer.standing.scored
            else None
        ),
        "reports_one_number": False,
        "benchmarks_against_the_sessions_best_price": False,
    }


def run_exit_quality_scorer(
    scorer: ExitQualityScorer, control_socket, read_exits, publish_qualities,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for trade_id, exit_price in read_exits():
            scored = scorer.score(trade_id, exit_price)
            if scored.is_usable:
                publish_qualities(scored.quality)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
