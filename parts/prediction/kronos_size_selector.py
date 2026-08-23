"""kronos-size-selector: which model size to run, chosen by measured accuracy.

Kronos comes in sizes: mini at 4.1M parameters with a 2048-candle context, small
at 24.7M and base at 102.3M with 512. Bigger is not better here -- mini's longer
context can beat base on a symbol whose signal is slow, and base can beat mini on
one whose signal is in the last hour.

**It chooses by accuracy, never by what the machine has free.** Choosing by
hardware would be a feature reaching into the control plane, which T-2 forbids:
the governor independently decides whether the chosen part runs at all, and a
selector that anticipated the governor would be making the governor's decision
badly and invisibly.

**A size with no measured record is sampled rather than ranked.** Otherwise the
first size that happened to be tried keeps being chosen, and the others never
produce the accuracy that would displace it -- the same trap the bot weight
sampler avoids, for the same reason.

**Choices are per symbol and per horizon.** One size over all symbols is an
average that describes none of them, and the cost of being wrong is paid on every
forecast.

**Switching has a hysteresis.** A selector that swapped size whenever one pulled
half a point ahead would spend its life switching, and every switch discards the
accuracy record that was being accumulated for the size it left.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "kronos-size-selector"

PART_DECLARATION = PartDeclaration(
    part_id="kronos-size-selector",
    consumes=("forecast-accuracy",),
    produces=("model-choice", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

CHOSEN_ON_ACCURACY = "chosen-on-measured-accuracy"
SAMPLING_AN_UNMEASURED_SIZE = "sampling-a-size-with-no-record-yet"
HELD_BY_HYSTERESIS = "held-because-the-lead-is-inside-the-switching-margin"
NOTHING_MEASURED = "no-size-has-a-record-for-this-symbol-and-horizon"


@dataclass(frozen=True)
class ModelChoice:
    """Which size to run for one symbol and horizon, and on what evidence."""

    venue_id: str
    symbol: str
    horizon_seconds: float
    model_name: str
    state: str
    accuracy: float | None
    runner_up: str | None
    runner_up_accuracy: float | None
    forecasts_behind_it: int
    reason: str
    chosen_at_ns: int

    @property
    def is_measured(self) -> bool:
        return self.state == CHOSEN_ON_ACCURACY


@dataclass
class SelectorStanding:
    choices_made: int = 0
    chosen_on_accuracy: int = 0
    sampled_unmeasured: int = 0
    held_by_hysteresis: int = 0
    switches: int = 0
    nothing_measured: int = 0
    by_model: dict = field(default_factory=dict)


class KronosSizeSelector:
    """Picks the model size whose measured accuracy earns it, and samples the rest."""

    def __init__(
        self,
        sizes: tuple,
        minimum_forecasts: int,
        switch_margin: float,
        sample_every: int,
        now_ns=time.time_ns,
    ) -> None:
        if len(sizes) < 2:
            raise ValueError("a selector with one size chooses nothing")
        if switch_margin <= 0:
            raise ValueError(
                "with no margin the selector switches on noise, and every switch discards the "
                "record it was accumulating"
            )
        if sample_every < 1:
            raise ValueError(
                "a size that is never sampled can never produce the accuracy that would "
                "displace the incumbent"
            )
        self._sizes = tuple(sizes)
        self._minimum = minimum_forecasts
        self._switch_margin = switch_margin
        self._sample_every = sample_every
        self._now_ns = now_ns
        self._accuracy: dict[tuple[str, str, float, str], tuple] = {}
        self._current: dict[tuple[str, str, float], str] = {}
        self._calls: dict[tuple[str, str, float], int] = {}
        self.standing = SelectorStanding()

    def observe_accuracy(self, accuracy) -> None:
        """One measured record: a size's directional accuracy on a symbol and horizon."""
        key = (accuracy.venue_id, accuracy.symbol, accuracy.horizon_seconds, accuracy.model_name)
        self._accuracy[key] = (
            accuracy.directional_accuracy.value,
            accuracy.forecasts_scored,
            accuracy.directional_accuracy.is_fitted,
        )

    def choose(self, venue_id: str, symbol: str, horizon_seconds: float) -> ModelChoice:
        self.standing.choices_made += 1
        context = (venue_id, symbol, horizon_seconds)
        self._calls[context] = self._calls.get(context, 0) + 1

        measured = {}
        unmeasured = []
        for size in self._sizes:
            record = self._accuracy.get((*context, size))
            if record is None or record[1] < self._minimum:
                unmeasured.append(size)
            else:
                measured[size] = record

        # A size nobody has tried is sampled on a fixed cadence. Without this the
        # first size that happened to be tried keeps being chosen and the others
        # never earn a record.
        if unmeasured and self._calls[context] % self._sample_every == 0:
            size = unmeasured[self._calls[context] // self._sample_every % len(unmeasured)]
            self.standing.sampled_unmeasured += 1
            return self._choice(
                context, size, SAMPLING_AN_UNMEASURED_SIZE, None, None, None, 0,
                f"{size} has fewer than {self._minimum} scored forecast(s) on {symbol} at "
                f"{horizon_seconds:.0f}s, so it is being sampled rather than ranked -- a size "
                f"never tried can never produce the record that would displace the incumbent",
            )

        if not measured:
            self.standing.nothing_measured += 1
            incumbent = self._current.get(context, self._sizes[0])
            return self._choice(
                context, incumbent, NOTHING_MEASURED, None, None, None, 0,
                f"no size has {self._minimum} scored forecast(s) on {symbol} at "
                f"{horizon_seconds:.0f}s yet, so {incumbent} stands and this is not a "
                f"measured choice",
            )

        ranked = sorted(measured.items(), key=lambda entry: entry[1][0], reverse=True)
        best, (best_accuracy, best_count, _) = ranked[0]
        runner_up, runner_up_accuracy = (
            (ranked[1][0], ranked[1][1][0]) if len(ranked) > 1 else (None, None)
        )

        incumbent = self._current.get(context)
        if incumbent is not None and incumbent != best and incumbent in measured:
            lead = best_accuracy - measured[incumbent][0]
            if lead < self._switch_margin:
                self.standing.held_by_hysteresis += 1
                return self._choice(
                    context, incumbent, HELD_BY_HYSTERESIS, measured[incumbent][0],
                    best, best_accuracy, measured[incumbent][1],
                    f"{best} leads {incumbent} by {lead:.1%}, inside the "
                    f"{self._switch_margin:.1%} margin; switching on that would spend this "
                    f"selector's life switching and discard the record being accumulated",
                )

        if incumbent is not None and incumbent != best:
            self.standing.switches += 1

        self.standing.chosen_on_accuracy += 1
        return self._choice(
            context, best, CHOSEN_ON_ACCURACY, best_accuracy, runner_up, runner_up_accuracy,
            best_count,
            f"{best} is right on direction {best_accuracy:.1%} of the time over {best_count} "
            f"scored forecast(s) on {symbol} at {horizon_seconds:.0f}s"
            + (
                f", against {runner_up} at {runner_up_accuracy:.1%}"
                if runner_up is not None
                else ""
            )
            + ". Chosen on accuracy rather than on what the machine has free -- the governor "
            "decides independently whether this runs at all",
        )

    def _choice(
        self, context, model_name, state, accuracy, runner_up, runner_up_accuracy, count, reason
    ) -> ModelChoice:
        self._current[context] = model_name
        self.standing.by_model[model_name] = self.standing.by_model.get(model_name, 0) + 1
        venue_id, symbol, horizon = context
        return ModelChoice(
            venue_id=venue_id,
            symbol=symbol,
            horizon_seconds=horizon,
            model_name=model_name,
            state=state,
            accuracy=accuracy,
            runner_up=runner_up,
            runner_up_accuracy=runner_up_accuracy,
            forecasts_behind_it=count,
            reason=reason,
            chosen_at_ns=self._now_ns(),
        )


def describe_size_selection(selector: KronosSizeSelector) -> dict:
    return {
        "part_id": PART_ID,
        "sizes": list(selector._sizes),
        "choices_made": selector.standing.choices_made,
        "chosen_on_measured_accuracy": selector.standing.chosen_on_accuracy,
        "sampled_an_unmeasured_size": selector.standing.sampled_unmeasured,
        "held_by_hysteresis": selector.standing.held_by_hysteresis,
        "switches": selector.standing.switches,
        "contexts_with_nothing_measured": selector.standing.nothing_measured,
        "by_model": dict(sorted(selector.standing.by_model.items())),
    }


def run_kronos_size_selector(
    selector: KronosSizeSelector, control_socket, read_accuracy, publish_choices,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        contexts = read_accuracy(selector)
        publish_choices(
            tuple(selector.choose(venue_id, symbol, horizon) for venue_id, symbol, horizon in contexts)
        )

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
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    accuracies = Batch(read=context.bus.reader("forecast-accuracy"))
    publish_choices = context.bus.publisher_for("model-choice")
    selector = KronosSizeSelector(
        sizes=tuple(str(s) for s in context.setting("kronos_sizes").value),
        minimum_forecasts=int(context.number("learning_minimum_observations")),
        switch_margin=context.number("kronos_switch_margin"),
        sample_every=int(context.number("kronos_sample_every")),
    )

    def read_accuracy(_selector):
        touched = set()
        for accuracy in accuracies.payloads():
            if accuracy.forecaster != "kronos-forecaster":
                continue
            selector.observe_accuracy(accuracy)
            touched.add((accuracy.venue_id, accuracy.symbol, accuracy.horizon_seconds))
        return tuple(sorted(touched))

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_choices(kept)

    return run_kronos_size_selector(
        selector=selector,
        control_socket=context.control_socket,
        read_accuracy=read_accuracy,
        publish_choices=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
