"""universal-symbol-sweeper: every symbol, against every watch condition, every tick.

RL-009 made concrete. The user's instruction was that the system must not pick a
few symbols from its universe and ignore the rest when the rest have the same
opportunity in them. This is the part that makes that true: whatever the system
has proven, it is tested everywhere, continuously.

The work is genuinely large -- symbols times conditions on every tick -- so the
order of the filters is the design. Cheap, high-rejection tests run first:

1. **Already holding it.** A symbol with an open position is not a new entry.
2. **Liquidity.** A symbol that cannot be traded at this size cannot be traded,
   however good the signal is, and this is a lookup.
3. **The condition itself**, evaluated only on what survives.

**A symbol that cannot be measured is skipped and counted, never assumed to
pass.** At full universe an unmeasurable symbol appearing to satisfy every
condition would flood the system with candidates for exactly the symbols it knows
least about.

**Nothing is truncated silently.** If the sweep cannot finish its work in the
tick it is given, it reports how many symbols it did not reach -- because a sweep
that quietly covered the first four hundred symbols would look identical to one
that covered them all, and RL-009 is specifically about not ignoring the rest.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.price_frames import levels_in
from runtime.market_signal import SignalCalibrator, make_candidate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "universal-symbol-sweeper"
# The grades the grader publishes that mean a symbol can be traded. Strings
# rather than an import: a part names data, never another part (T-4).
DEEP_GRADE = "deep"
TRADEABLE_GRADE = "tradeable"

PART_DECLARATION = PartDeclaration(
    part_id="universal-symbol-sweeper",
    consumes=(
        "symbol-price-frame", "symbol-universe", "watch-condition", "liquidity-grade",
        "position", "cross-segment-signal", "venue-announcement", "consolidated-price",
    ),
    produces=("entry-candidate", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

SWEPT = "swept"
HELD_ALREADY = "already-holding-this-symbol"
NOT_TRADEABLE = "liquidity-grade-refuses-it"
UNMEASURABLE = "measurement-missing"
CONDITION_NOT_MET = "condition-not-met"


@dataclass(frozen=True)
class SweepReport:
    """What one sweep covered, and what it did not reach."""

    symbols_in_universe: int
    symbols_swept: int
    symbols_not_reached: int
    conditions_tested: int
    candidates: int
    skipped_held: int
    skipped_untradeable: int
    skipped_unmeasurable: int
    duration_seconds: float
    reason: str
    swept_at_ns: int

    @property
    def covered_everything(self) -> bool:
        return self.symbols_not_reached == 0


@dataclass
class SweeperStanding:
    sweeps: int = 0
    candidates: int = 0
    symbols_not_reached_total: int = 0
    incomplete_sweeps: int = 0
    skipped_held: int = 0
    skipped_untradeable: int = 0
    skipped_unmeasurable: int = 0
    slowest_sweep_seconds: float = 0.0
    by_condition: dict = field(default_factory=dict)


class UniversalSymbolSweeper:
    """Tests every symbol against every condition, and says what it could not reach."""

    def __init__(
        self,
        sweep_budget_seconds: float,
        calibrator: SignalCalibrator,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if sweep_budget_seconds <= 0:
            raise ValueError("a sweep with no time budget cannot bound its own work")
        self._budget = sweep_budget_seconds
        self._calibrator = calibrator
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._measurements: dict[tuple[str, str], dict] = {}
        self._previous: dict[tuple[str, str], dict] = {}
        self._held: set[tuple[str, str]] = set()
        self._grades: dict[tuple[str, str], bool] = {}
        self._resume_from = 0
        self.standing = SweeperStanding()

    def observe_measurements(self, venue_id: str, symbol: str, measurements: dict) -> None:
        """The current value of every named measurement for one symbol."""
        key = (venue_id, symbol)
        if key in self._measurements:
            self._previous[key] = self._measurements[key]
        self._measurements[key] = dict(measurements)

    def set_held(self, venue_id: str, symbol: str, is_held: bool) -> None:
        key = (venue_id, symbol)
        if is_held:
            self._held.add(key)
        else:
            self._held.discard(key)

    def set_tradeable(self, venue_id: str, symbol: str, is_tradeable: bool) -> None:
        self._grades[(venue_id, symbol)] = is_tradeable

    def sweep(self, universe, conditions) -> tuple[tuple, SweepReport]:
        """One pass over the universe. Resumes where the last pass ran out of time."""
        started = self._monotonic()
        self.standing.sweeps += 1
        candidates = []
        swept = 0
        skipped_held = skipped_untradeable = skipped_unmeasurable = 0

        # Resume from where the last sweep stopped, so a universe too large for
        # one tick is covered across several rather than always the same prefix.
        ordered = list(universe)
        start = self._resume_from % len(ordered) if ordered else 0
        rotated = ordered[start:] + ordered[:start]

        for index, (venue_id, symbol) in enumerate(rotated):
            if self._monotonic() - started > self._budget:
                self._resume_from = (start + index) % max(1, len(ordered))
                break
            swept += 1

            key = (venue_id, symbol)
            if key in self._held:
                skipped_held += 1
                continue
            if self._grades.get(key) is False:
                skipped_untradeable += 1
                continue

            measurements = self._measurements.get(key)
            if measurements is None:
                skipped_unmeasurable += 1
                continue

            previous = self._previous.get(key, {})
            for condition in conditions:
                value = measurements.get(condition.measurement)
                if value is None:
                    skipped_unmeasurable += 1
                    continue
                if not condition.evaluate(value, previous.get(condition.measurement)):
                    continue

                self.standing.by_condition[condition.condition_id] = (
                    self.standing.by_condition.get(condition.condition_id, 0) + 1
                )
                confidence = self._calibrator.confidence(PART_ID, condition.condition_id)
                candidates.append(
                    make_candidate(
                        detector=PART_ID,
                        venue_id=venue_id,
                        symbol=symbol,
                        direction=condition.direction,
                        expectation=condition.expectation,
                        signal_strength=abs(value - condition.threshold),
                        confidence=confidence,
                        horizon_seconds=condition.horizon_seconds,
                        evidence={
                            "condition_id": condition.condition_id,
                            "instruction_id": condition.instruction_id,
                            "measurement": condition.measurement,
                            "value": value,
                            "threshold": condition.threshold,
                            "comparison": condition.comparison,
                        },
                        reason=(
                            f"{condition.measurement} is {value:g}, {condition.comparison} "
                            f"{condition.threshold:g}, which is what instruction "
                            f"{condition.instruction_id} watches for. That has held "
                            f"{confidence.value:.0%} of the time "
                            f"({'measured' if confidence.is_fitted else 'the prior'})"
                        ),
                        now_ns=self._now_ns,
                    )
                )
        else:
            self._resume_from = 0

        duration = self._monotonic() - started
        not_reached = max(0, len(ordered) - swept)
        self.standing.candidates += len(candidates)
        self.standing.skipped_held += skipped_held
        self.standing.skipped_untradeable += skipped_untradeable
        self.standing.skipped_unmeasurable += skipped_unmeasurable
        self.standing.slowest_sweep_seconds = max(self.standing.slowest_sweep_seconds, duration)
        if not_reached:
            self.standing.incomplete_sweeps += 1
            self.standing.symbols_not_reached_total += not_reached

        report = SweepReport(
            symbols_in_universe=len(ordered),
            symbols_swept=swept,
            symbols_not_reached=not_reached,
            conditions_tested=len(conditions),
            candidates=len(candidates),
            skipped_held=skipped_held,
            skipped_untradeable=skipped_untradeable,
            skipped_unmeasurable=skipped_unmeasurable,
            duration_seconds=duration,
            reason=(
                f"swept {swept} of {len(ordered)} symbols against {len(conditions)} condition(s) "
                f"in {duration:.3f}s"
                + (
                    f"; {not_reached} not reached this tick and will be swept first next tick"
                    if not_reached
                    else ""
                )
            ),
            swept_at_ns=self._now_ns(),
        )
        return tuple(candidates), report

    def observe_outcome(self, condition_id: str, was_right: bool) -> None:
        self._calibrator.observe_outcome(PART_ID, condition_id, was_right)


def describe_sweeps(sweeper: UniversalSymbolSweeper) -> dict:
    return {
        "part_id": PART_ID,
        "sweeps": sweeper.standing.sweeps,
        "candidates": sweeper.standing.candidates,
        "incomplete_sweeps": sweeper.standing.incomplete_sweeps,
        "symbols_not_reached_total": sweeper.standing.symbols_not_reached_total,
        "skipped_already_held": sweeper.standing.skipped_held,
        "skipped_untradeable": sweeper.standing.skipped_untradeable,
        "skipped_unmeasurable": sweeper.standing.skipped_unmeasurable,
        "slowest_sweep_seconds": sweeper.standing.slowest_sweep_seconds,
        "by_condition": dict(sweeper.standing.by_condition),
    }


def run_universal_symbol_sweeper(
    sweeper: UniversalSymbolSweeper, control_socket, read_universe, publish,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        universe, conditions = read_universe(sweeper)
        publish(*sweeper.sweep(universe, conditions))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_sweeps(sweeper),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The universe is what the catalogue reader publishes; the measurements
    per symbol are computed from the latest trade, the trade one window ago
    and the consolidated price, in the shared vocabulary the compiler
    checks conditions against. Held positions and untradeable grades are
    skipped as the sweeper is told of them. A sweep runs once per health
    interval and resumes where its budget ran out.
    """
    import time as _time

    from runtime.input_assembly import Batch, LatestByKey
    from runtime.rolling_statistics import RollingWindow
    from runtime.sweep_measurements import add_cross_sectional, measure_symbol

    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    universe = LatestByKey(read=context.bus.reader("symbol-universe"), key_of=lambda e: (e.venue_id, e.symbol))
    conditions = LatestByKey(read=context.bus.reader("watch-condition"), key_of=lambda c: c.condition_id)
    grades = Batch(read=context.bus.reader("liquidity-grade"))
    positions = Batch(read=context.bus.reader("position"))
    signals = Batch(read=context.bus.reader("cross-segment-signal"))
    announcements = Batch(read=context.bus.reader("venue-announcement"))
    consolidated = LatestByKey(read=context.bus.reader("consolidated-price"), key_of=lambda p: p.symbol)
    publish_candidates = context.bus.publisher_for("entry-candidate")
    sweeper = UniversalSymbolSweeper(
        sweep_budget_seconds=context.number("sweep_budget"),
        calibrator=SignalCalibrator(
            prior_hit_rate=context.number("signal_prior_hit_rate"),
            prior_weight=context.number("signal_prior_weight"),
            half_life_observations=context.number("signal_half_life_observations"),
            minimum_observations=int(context.number("signal_minimum_observations")),
        ),
    )
    window_length = int(context.number("detector_window_length"))
    minimum_observations = int(context.number("detector_minimum_observations"))
    short_window_fraction = context.number("sweep_short_window_fraction")
    minimum_symbols = int(context.number("sweep_minimum_symbols_for_cross_section"))
    windows: dict[tuple[str, str], RollingWindow] = {}
    last_sweep = [float("-inf")]

    def read_universe(_sweeper):
        signals.payloads()
        announcements.payloads()
        for trade in levels_in(trades.payloads()):
            key = (trade.venue_id, trade.symbol)
            window = windows.get(key)
            if window is None:
                window = windows[key] = RollingWindow(length=window_length)
            window.observe(trade.price)
        for grade in grades.payloads():
            sweeper.set_tradeable(grade.venue_id, grade.symbol, grade.grade in (DEEP_GRADE, TRADEABLE_GRADE))
        for position in positions.payloads():
            sweeper.set_held(position.venue_id, position.symbol, not position.is_flat)
        # Windows are fed on every tick -- a price not observed is a price gone --
        # but measuring is guarded by the same due-check as the sweep itself.
        # Measuring is the expensive half: it walks every window and sorts the
        # whole universe four times, and doing that on every tick to answer a
        # question asked once a second is how heartbeat-collector came to spend
        # 73% of a core. The governor was already shedding this part as a hog.
        now = _time.monotonic()
        if now - last_sweep[0] < context.health_interval_seconds:
            return (), ()
        last_sweep[0] = now

        consolidated_by_symbol = consolidated.mapping()
        # Measured per symbol first, then completed across the universe. The
        # cross-sectional half cannot be computed one symbol at a time, and this
        # part is the only one in the block holding every symbol at once -- which
        # is why a universe-relative measurement belongs here and in no detector.
        measured: dict[tuple[str, str], dict] = {}
        for key, window in windows.items():
            price = consolidated_by_symbol.get(key[1])
            measured[key] = measure_symbol(
                window,
                consolidated=None if price is None else price.price,
                minimum_observations=minimum_observations,
                short_window_fraction=short_window_fraction,
            )
        add_cross_sectional(measured, minimum_symbols=minimum_symbols)
        for key, found in measured.items():
            sweeper.observe_measurements(key[0], key[1], found)
        return tuple(sorted(universe.mapping())), tuple(conditions.mapping().values())

    def publish(candidates, _report) -> None:
        if candidates:
            publish_candidates(tuple(candidates))

    return run_universal_symbol_sweeper(
        sweeper=sweeper,
        control_socket=context.control_socket,
        read_universe=read_universe,
        publish=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
