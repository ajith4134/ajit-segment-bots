"""label-builder: turning a closed trade into what a model can actually learn from.

The single most consequential part in the learning loop, because everything
downstream trains on what it produces -- and the obvious implementation is wrong
in a way that is invisible for months.

**Profit is not a label.** A trade's result is the sum of four things: whether the
setup was right, whether the entry was timed, whether the exit was timed, and
whether the size was appropriate. A model trained on the sum learns whichever
component happened to dominate the sample, and it cannot be told which. So this
part produces a label **per component**:

- **The setup was right** if price went the expected way at any point within the
  horizon, whatever the exit did. This is what a conviction model should learn:
  it decided the setup, not the exit.
- **The entry was timed** if the trade did not first go substantially against
  before working. A right setup entered early is a right setup.
- **The exit was timed** if what was realised is close to the peak excursion
  (RL-042). A trade that reached 8% and closed at 2% has a right setup and a
  wrong exit, and only this comparison separates them.
- **The size was right** if the position survived its own adverse excursion. A
  trade stopped out by size rather than by thesis is a sizing failure wearing a
  setup failure's clothes.

**Costs are subtracted before labelling, never after.** A setup that is right only
before fees is not right, and a model trained on gross labels learns to produce
trades the desk cannot afford.

**A trade that did not resolve inside its horizon is labelled as not resolving**,
not as a loss and not as a win. It is its own outcome, and folding it into either
teaches the model to hold or to cut for reasons that have nothing to do with the
setup.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learning_types import (
    THE_ENTRY_WAS_TIMED, THE_EXIT_WAS_TIMED, THE_SETUP_WAS_RIGHT, THE_SIZE_WAS_RIGHT,
    TrainingLabel,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "label-builder"

PART_DECLARATION = PartDeclaration(
    part_id="label-builder",
    consumes=("closed-trade", "peak-excursion", "cost-estimate"),
    produces=("training-label", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

LABELLED = "labelled"
NO_EXCURSION_RECORD = "no-peak-excursion-recorded-for-this-trade"
NO_COST_ESTIMATE = "no-cost-estimate-for-this-trade"

LONG = "long"
SHORT = "short"


@dataclass(frozen=True)
class ClosedTradeRecord:
    """One finished trade, with everything needed to label its components apart."""

    venue_id: str
    symbol: str
    detector: str
    regime: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    opened_at_ns: int
    closed_at_ns: int
    horizon_seconds: float
    features: dict


@dataclass(frozen=True)
class ExcursionRecord:
    """How far a trade went either way while it was open (RL-042)."""

    peak_favourable_fraction: float
    peak_adverse_fraction: float
    seconds_to_peak_favourable: float
    seconds_to_peak_adverse: float
    observations: int


@dataclass
class BuilderStanding:
    trades_seen: int = 0
    labels_built: int = 0
    refused_no_excursion: int = 0
    refused_no_costs: int = 0
    unresolved_within_horizon: int = 0
    by_component: dict = field(default_factory=dict)
    largest_exit_shortfall: float | None = None


class LabelBuilder:
    """Labels each component of a trade separately, after costs."""

    def __init__(
        self,
        favourable_threshold: float,
        adverse_entry_threshold: float,
        exit_capture_threshold: float,
        size_survival_multiple: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < exit_capture_threshold <= 1.0:
            raise ValueError(
                "exit quality is what fraction of the peak was captured, so its threshold is "
                "a fraction inside (0, 1]"
            )
        if size_survival_multiple <= 0:
            raise ValueError(
                "a position must survive some multiple of its own adverse excursion for size "
                "to have been right"
            )
        self._favourable = favourable_threshold
        self._adverse_entry = adverse_entry_threshold
        self._exit_capture = exit_capture_threshold
        self._size_multiple = size_survival_multiple
        self._now_ns = now_ns
        self._excursions: dict[tuple[str, str, int], ExcursionRecord] = {}
        self._costs: dict[tuple[str, str], float] = {}
        self.standing = BuilderStanding()

    def observe_excursion(self, venue_id: str, symbol: str, opened_at_ns: int, excursion: ExcursionRecord) -> None:
        self._excursions[(venue_id, symbol, opened_at_ns)] = excursion

    def observe_cost_estimate(self, venue_id: str, symbol: str, round_trip_fraction: float) -> None:
        self._costs[(venue_id, symbol)] = round_trip_fraction

    def build(self, trade: ClosedTradeRecord) -> tuple[TrainingLabel | None, str]:
        self.standing.trades_seen += 1
        excursion = self._excursions.get((trade.venue_id, trade.symbol, trade.opened_at_ns))
        if excursion is None:
            self.standing.refused_no_excursion += 1
            return None, NO_EXCURSION_RECORD

        cost = self._costs.get((trade.venue_id, trade.symbol))
        if cost is None:
            # A setup that is right only before fees is not right, and a model
            # trained on gross labels learns to produce trades the desk cannot
            # afford.
            self.standing.refused_no_costs += 1
            return None, NO_COST_ESTIMATE

        held_seconds = (trade.closed_at_ns - trade.opened_at_ns) / 1e9
        resolved = held_seconds <= trade.horizon_seconds
        if not resolved:
            self.standing.unresolved_within_horizon += 1

        realised = self._realised_fraction(trade) - cost
        favourable_after_costs = excursion.peak_favourable_fraction - cost

        labels = {
            # The setup: did price go the expected way at all, whatever the exit
            # did? This is what a conviction model decided.
            THE_SETUP_WAS_RIGHT: favourable_after_costs >= self._favourable,
            # The entry: did it work without first going substantially against?
            # A right setup entered early is still a right setup.
            THE_ENTRY_WAS_TIMED: excursion.peak_adverse_fraction <= self._adverse_entry,
            # The exit: how much of the peak was captured? A trade that reached
            # 8% and closed at 2% has a right setup and a wrong exit.
            THE_EXIT_WAS_TIMED: (
                favourable_after_costs <= 0
                or realised >= favourable_after_costs * self._exit_capture
            ),
            # The size: did the position survive its own adverse excursion? A
            # trade stopped out by size is a sizing failure wearing a setup
            # failure's clothes.
            THE_SIZE_WAS_RIGHT: (
                excursion.peak_adverse_fraction * self._size_multiple
                <= abs(self._stop_distance(trade))
                if self._stop_distance(trade)
                else True
            ),
        }

        for component, value in labels.items():
            key = f"{component}:{'true' if value else 'false'}"
            self.standing.by_component[key] = self.standing.by_component.get(key, 0) + 1

        shortfall = max(0.0, favourable_after_costs - realised)
        if (
            self.standing.largest_exit_shortfall is None
            or shortfall > self.standing.largest_exit_shortfall
        ):
            self.standing.largest_exit_shortfall = shortfall

        self.standing.labels_built += 1
        return (
            TrainingLabel(
                venue_id=trade.venue_id,
                symbol=trade.symbol,
                detector=trade.detector,
                regime=trade.regime,
                labels=labels,
                horizon_seconds=trade.horizon_seconds,
                seconds_to_resolve=held_seconds,
                resolved_within_horizon=resolved,
                features=dict(trade.features),
                built_at_ns=self._now_ns(),
            ),
            LABELLED,
        )

    def _realised_fraction(self, trade: ClosedTradeRecord) -> float:
        if trade.entry_price <= 0:
            return 0.0
        move = (trade.exit_price - trade.entry_price) / trade.entry_price
        return move if trade.side == LONG else -move

    def _stop_distance(self, trade: ClosedTradeRecord) -> float:
        """How far the stop sat, from what the trade actually carried."""
        return getattr(trade, "stop_distance_fraction", 0.0)


def describe_labelling(builder: LabelBuilder) -> dict:
    return {
        "part_id": PART_ID,
        "trades_seen": builder.standing.trades_seen,
        "labels_built": builder.standing.labels_built,
        "refused_no_excursion_record": builder.standing.refused_no_excursion,
        "refused_no_cost_estimate": builder.standing.refused_no_costs,
        "unresolved_within_their_horizon": builder.standing.unresolved_within_horizon,
        "by_component": dict(sorted(builder.standing.by_component.items())),
        "largest_exit_shortfall": builder.standing.largest_exit_shortfall,
        "components": [
            THE_SETUP_WAS_RIGHT, THE_ENTRY_WAS_TIMED, THE_EXIT_WAS_TIMED, THE_SIZE_WAS_RIGHT
        ],
    }


def run_label_builder(
    builder: LabelBuilder, control_socket, read_closed_trades, publish_labels,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        labels = []
        for trade in read_closed_trades(builder):
            label, _ = builder.build(trade)
            if label is not None:
                labels.append(label)
        publish_labels(tuple(labels))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
