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
    # How far the protective stop sat from the entry, as a fraction of it. Zero
    # means nobody told this part where the stop was, which is NOT the same as a
    # stop at the entry price: `THE_SIZE_WAS_RIGHT` is then unjudgeable and is
    # left out of the label rather than asserted (see `build`).
    #
    # Declared as a real field because it was not one until 2026-09-12, and
    # `_stop_distance` read it with `getattr(trade, "stop_distance_fraction",
    # 0.0)` off a record that has never carried it. The getattr always returned
    # 0.0, 0.0 is falsy, and the conditional beneath it therefore always took its
    # `else True` branch: **every label this part could ever build asserted that
    # the size was right**, including the seven NIFTY trades of 2026-09-08 that
    # were 91.4% of every rupee this project has lost. A default that can never
    # be overridden is a hardcoded answer wearing a parameter's clothes.
    stop_distance_fraction: float = 0.0


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
    # Labels built with no stop distance, so `THE_SIZE_WAS_RIGHT` could not be
    # judged and was left out. Its own number because it is the difference
    # between "the size was right" and "nobody measured the size", and until
    # 2026-09-12 this part reported the first while meaning the second.
    size_not_judgeable: int = 0


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
        }

        # The size: did the position survive its own adverse excursion? A trade
        # stopped out by size is a sizing failure wearing a setup failure's
        # clothes.
        #
        # **Omitted, never defaulted, when the stop distance is unknown.** This
        # component used to read `... if stop_distance else True`, against a
        # record that never carried a stop distance, so it asserted "the size was
        # right" for every trade ever labelled. A model trained on that learns
        # that size is never the problem. Absence of evidence renders as its own
        # state here exactly as it does on a board (Rule 8): the key is left out,
        # and `size_not_judgeable` counts how often, so the gap is visible rather
        # than silently green.
        stop_distance = abs(self._stop_distance(trade))
        if stop_distance > 0:
            labels[THE_SIZE_WAS_RIGHT] = (
                excursion.peak_adverse_fraction * self._size_multiple <= stop_distance
            )
        else:
            self.standing.size_not_judgeable += 1

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
                opened_at_ns=trade.opened_at_ns,
                # Left at their defaults until 2026-08-30: a reader keyed by
                # symbol alone (bull/bear-setup-weight-learner) cannot tell a
                # long trade's outcome from a short one on the same symbol
                # without this, and the magnitude is what a short book's
                # tail-loss discount is measured from -- both already computed
                # here for the label's own components, just never carried out.
                direction=trade.side,
                best_favourable_fraction=excursion.peak_favourable_fraction,
                worst_adverse_fraction=excursion.peak_adverse_fraction,
            ),
            LABELLED,
        )

    def _realised_fraction(self, trade: ClosedTradeRecord) -> float:
        if trade.entry_price <= 0:
            return 0.0
        move = (trade.exit_price - trade.entry_price) / trade.entry_price
        return move if trade.side == LONG else -move

    def _stop_distance(self, trade: ClosedTradeRecord) -> float:
        """How far the stop sat, from what the trade actually carried.

        Reads the record's own field rather than `getattr`-with-a-default. The
        default was the whole defect: `ClosedTradeRecord` never declared this,
        so the lookup could not fail and could not succeed either -- it returned
        0.0 for every trade, forever, and the caller read that as "no stop" and
        asserted the size was right. A record that must carry a number carries
        it as a field, where a missing one is a construction error rather than a
        silent zero.
        """
        return trade.stop_distance_fraction


def describe_labelling(builder: LabelBuilder) -> dict:
    return {
        "part_id": PART_ID,
        "trades_seen": builder.standing.trades_seen,
        "labels_built": builder.standing.labels_built,
        "refused_no_excursion_record": builder.standing.refused_no_excursion,
        "refused_no_cost_estimate": builder.standing.refused_no_costs,
        "unresolved_within_their_horizon": builder.standing.unresolved_within_horizon,
        "labels_whose_size_could_not_be_judged": builder.standing.size_not_judgeable,
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
        read_standing=lambda: describe_labelling(builder),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Excursions arrive while a position is open and are kept per symbol until
    the closed trade arrives; the builder then labels the trade. The closed
    trade on the bus carries no detector or regime, so both are "unknown"
    here -- the signal labeller's labels carry the detector, and this part's
    labels are about the trade's own stages: setup, entry, exit, size.
    """
    from runtime.input_assembly import Batch
    from runtime.trading_types import LONG, SHORT

    closed = Batch(read=context.bus.reader("closed-trade"))
    excursions = Batch(read=context.bus.reader("peak-excursion"))
    costs = Batch(read=context.bus.reader("cost-estimate"))
    publish_labels = context.bus.publisher_for("training-label")
    builder = LabelBuilder(
        favourable_threshold=context.number("label_favourable_threshold"),
        adverse_entry_threshold=context.number("label_adverse_entry_threshold"),
        exit_capture_threshold=context.number("label_exit_capture_threshold"),
        size_survival_multiple=context.number("label_size_survival_multiple"),
    )
    horizon = context.number("spread_reversion_horizon") * context.number("bull_exit_conviction_horizon_multiple")
    latest_excursion: dict[tuple[str, str], object] = {}

    def read_closed_trades(_builder):
        for estimate in costs.payloads():
            if estimate.notional > 0:
                builder.observe_cost_estimate(
                    estimate.venue_id, estimate.symbol,
                    (estimate.fee + estimate.half_spread + estimate.expected_impact) / estimate.notional,
                )
        for excursion in excursions.payloads():
            latest_excursion[(excursion.venue_id, excursion.symbol)] = excursion
        records = []
        for trade in closed.payloads():
            excursion = latest_excursion.pop((trade.venue_id, trade.symbol), None)
            if excursion is not None:
                entry = trade.entry_price or 1.0
                builder.observe_excursion(
                    trade.venue_id, trade.symbol, trade.opened_at_ns,
                    ExcursionRecord(
                        peak_favourable_fraction=abs(excursion.best_price - entry) / entry,
                        peak_adverse_fraction=abs(excursion.worst_price - entry) / entry,
                        seconds_to_peak_favourable=0.0,
                        seconds_to_peak_adverse=0.0,
                        observations=excursion.samples,
                    ),
                )
            records.append(
                ClosedTradeRecord(
                    venue_id=trade.venue_id, symbol=trade.symbol, detector="unknown", regime="unknown",
                    side=LONG if trade.direction == LONG else SHORT,
                    entry_price=trade.entry_price, exit_price=trade.exit_price, quantity=trade.quantity,
                    opened_at_ns=trade.opened_at_ns, closed_at_ns=trade.closed_at_ns,
                    horizon_seconds=horizon, features={},
                )
            )
        return tuple(records)

    def publish(labels) -> None:
        if labels:
            publish_labels(labels)

    return run_label_builder(
        builder=builder,
        control_socket=context.control_socket,
        read_closed_trades=read_closed_trades,
        publish_labels=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
