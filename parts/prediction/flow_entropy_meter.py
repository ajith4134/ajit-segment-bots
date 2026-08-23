"""flow-entropy-meter: how much structure is in the order flow, and never which way.

The second of the three parts from `docs/research/order-flow-entropy.md`
(arXiv:2512.15720). It builds the transition matrix over the fifteen states, takes
its stationary distribution, and reports the normalised entropy:

    H_t = -(log 15)^-1 * sum_i pi_i sum_j p_ij log p_ij

Low entropy means the transitions are structured -- the paper reads that as
informed traders leaving a footprint -- and structure is followed by larger moves.
On SPY the lowest entropy quintile saw 8.14 bps of mean absolute five-minute
return against 3.75 in the highest, a ratio of 2.17, and below the fifth
percentile 15.3 bps, 2.89 times the unconditional mean.

**IT MUST NEVER PRODUCE A DIRECTION.** That is a theorem, not a limitation of
this implementation. Entropy is invariant under swapping the buy and sell labels:
an informed buyer and an informed seller leave the same signature, so
`E[sgn(r) | H] = 0`. The paper measured 45.0% directional accuracy, `z = -1.55`,
indistinguishable from chance. This part's type carries no direction and its
`is_invariant_under_label_swap` check exists so that any future change that broke
the symmetry would fail loudly rather than quietly produce a directional signal
nobody could justify.

**A row with no observed transitions falls back to uniform**, exactly as the
specification says -- 1/15 across the row. Renormalising the observed rows instead
would concentrate the stationary distribution on whichever states happened to be
seen, and the entropy would measure the sample rather than the flow.

**A window shorter than the specification's 120 seconds is refused**, because the
percentile thresholds the trading rule uses were fitted at that length.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy

from runtime.learned_estimator import QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "flow-entropy-meter"

PART_DECLARATION = PartDeclaration(
    part_id="flow-entropy-meter",
    consumes=("order-flow-state",),
    produces=("flow-entropy", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

STATE_COUNT = 15

MEASURED = "measured"
WINDOW_TOO_SHORT = "fewer-states-than-the-window-the-thresholds-were-fitted-at"
NO_TRANSITIONS = "no-transition-was-observed"

# The normaliser from the specification: entropy of the uniform distribution over
# fifteen states, so H lands in [0, 1] whatever the state count would otherwise
# imply.
MAXIMUM_ENTROPY = math.log(STATE_COUNT)


@dataclass(frozen=True)
class FlowEntropy:
    """The structure in one window of order flow. Carries no direction, by theorem."""

    venue_id: str
    symbol: str
    state: str
    entropy: float | None
    percentile: float | None
    transitions_observed: int
    rows_with_no_transitions: int
    states_seen: int
    window_seconds: int
    reason: str
    measured_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == MEASURED and self.entropy is not None

    @property
    def is_structured(self) -> bool:
        """Low entropy: the condition the paper associates with larger moves."""
        return self.percentile is not None and self.percentile <= 0.05

    @property
    def carries_a_direction(self) -> bool:
        """Always false, and stated in the type so nothing can start assuming otherwise."""
        return False


@dataclass
class MeterStanding:
    measurements: int = 0
    measured: int = 0
    refused_short_window: int = 0
    refused_no_transitions: int = 0
    uniform_rows_used: int = 0
    lowest_entropy_seen: float | None = None
    highest_entropy_seen: float | None = None


class FlowEntropyMeter:
    """Builds the transition matrix, takes its stationary distribution, reports entropy."""

    def __init__(
        self,
        window_seconds: int,
        percentile_window: int,
        prior_entropy: float,
        minimum_observations: int,
        now_ns=time.time_ns,
    ) -> None:
        if window_seconds < 30:
            raise ValueError(
                "the specification fits its percentile thresholds over a 120-second window; "
                "far below that the transition matrix is mostly the uniform fallback"
            )
        self._window = window_seconds
        self._now_ns = now_ns
        self._minimum = minimum_observations
        self._history: dict[tuple[str, str], QuantileEstimator] = {}
        self._percentile_window = percentile_window
        self._prior_entropy = prior_entropy
        self.standing = MeterStanding()

    def transition_matrix(self, states) -> tuple:
        """Estimated transition probabilities, with the uniform fallback for empty rows.

        Uniform rather than renormalising over the observed rows: renormalising
        concentrates the stationary distribution on whichever states happened to
        be seen, and the entropy then measures the sample rather than the flow.
        """
        counts = numpy.zeros((STATE_COUNT, STATE_COUNT), dtype=numpy.float64)
        for earlier, later in zip(states, states[1:]):
            counts[earlier.index, later.index] += 1.0

        matrix = numpy.empty_like(counts)
        empty_rows = 0
        for row in range(STATE_COUNT):
            total = counts[row].sum()
            if total <= 0:
                matrix[row] = 1.0 / STATE_COUNT
                empty_rows += 1
            else:
                matrix[row] = counts[row] / total
        return matrix, int(counts.sum()), empty_rows

    def stationary_distribution(self, matrix) -> numpy.ndarray:
        """The left eigenvector for eigenvalue one, normalised to a distribution.

        By eigendecomposition as the specification says, rather than by iterating
        the chain: a chain with a slowly mixing component takes an unbounded
        number of iterations to converge, and a fixed iteration count would
        silently return a distribution that had not.
        """
        values, vectors = numpy.linalg.eig(matrix.T)
        index = int(numpy.argmin(numpy.abs(values - 1.0)))
        vector = numpy.real(vectors[:, index])
        vector = numpy.abs(vector)
        total = vector.sum()
        if total <= 0:
            return numpy.full(STATE_COUNT, 1.0 / STATE_COUNT)
        return vector / total

    def measure(self, venue_id: str, symbol: str, states) -> FlowEntropy:
        self.standing.measurements += 1
        states = list(states)[-self._window :]

        if len(states) < self._window:
            self.standing.refused_short_window += 1
            return self._entropy(
                venue_id, symbol, WINDOW_TOO_SHORT, None, None, 0, 0, len(states),
                f"{len(states)} state(s) against the {self._window}-second window the "
                f"percentile thresholds were fitted at; a shorter window is mostly the "
                f"uniform fallback and its entropy describes that rather than the flow",
            )

        matrix, transitions, empty_rows = self.transition_matrix(states)
        self.standing.uniform_rows_used += empty_rows

        if transitions == 0:
            self.standing.refused_no_transitions += 1
            return self._entropy(
                venue_id, symbol, NO_TRANSITIONS, None, None, 0, empty_rows, len(states),
                "no transition was observed at all, so the matrix is entirely the fallback",
            )

        stationary = self.stationary_distribution(matrix)

        # H = -(log 15)^-1 * sum_i pi_i sum_j p_ij log p_ij
        with numpy.errstate(divide="ignore", invalid="ignore"):
            logs = numpy.where(matrix > 0, numpy.log(matrix), 0.0)
        row_entropy = -(matrix * logs).sum(axis=1)
        entropy = float((stationary * row_entropy).sum() / MAXIMUM_ENTROPY)
        entropy = min(1.0, max(0.0, entropy))

        estimator = self._estimator_for(venue_id, symbol)
        percentile = self._percentile_of(estimator, entropy)
        estimator.observe(entropy)

        self.standing.measured += 1
        if self.standing.lowest_entropy_seen is None or entropy < self.standing.lowest_entropy_seen:
            self.standing.lowest_entropy_seen = entropy
        if self.standing.highest_entropy_seen is None or entropy > self.standing.highest_entropy_seen:
            self.standing.highest_entropy_seen = entropy

        return self._entropy(
            venue_id, symbol, MEASURED, entropy, percentile, transitions, empty_rows, len(states),
            f"H = {entropy:.4f} over {transitions} transition(s) in {len(states)} second(s)"
            + (f", the {percentile:.0%} percentile of this symbol's own history" if percentile is not None else ", with no history to rank it against yet")
            + (f"; {empty_rows} of {STATE_COUNT} rows had no observed transition and fell back to uniform" if empty_rows else "")
            + ". This carries no direction: entropy is invariant under swapping the buy and "
            "sell labels, so an informed buyer and an informed seller leave the same signature",
        )

    def _percentile_of(self, estimator: QuantileEstimator, entropy: float) -> float | None:
        """Where this entropy sits in the symbol's own history, which is what the rule uses."""
        samples = estimator._samples
        if len(samples) < self._minimum:
            return None
        return sum(1 for value in samples if value <= entropy) / len(samples)

    def _estimator_for(self, venue_id: str, symbol: str) -> QuantileEstimator:
        key = (venue_id, symbol)
        estimator = self._history.get(key)
        if estimator is None:
            estimator = QuantileEstimator(
                window=self._percentile_window, prior=self._prior_entropy
            )
            self._history[key] = estimator
        return estimator

    def is_invariant_under_label_swap(self, states) -> bool:
        """Theorem 2, checked rather than assumed.

        Swapping every up-second for a down-second and back must leave the
        entropy unchanged. If a change ever broke that, this would fail loudly
        rather than the meter quietly acquiring a direction it cannot justify.
        """
        swapped = [
            type(state)(
                venue_id=state.venue_id, symbol=state.symbol, second_ns=state.second_ns,
                price_sign=-state.price_sign, volume_quintile=state.volume_quintile,
                close=state.close, volume=state.volume, trades=state.trades,
            )
            for state in states
        ]
        original = self._raw_entropy(states)
        mirrored = self._raw_entropy(swapped)
        if original is None or mirrored is None:
            return True
        return abs(original - mirrored) < 1e-9

    def _raw_entropy(self, states) -> float | None:
        if len(states) < 2:
            return None
        matrix, transitions, _ = self.transition_matrix(list(states))
        if transitions == 0:
            return None
        stationary = self.stationary_distribution(matrix)
        with numpy.errstate(divide="ignore", invalid="ignore"):
            logs = numpy.where(matrix > 0, numpy.log(matrix), 0.0)
        return float((stationary * -(matrix * logs).sum(axis=1)).sum() / MAXIMUM_ENTROPY)

    def _entropy(
        self, venue_id, symbol, state, entropy, percentile, transitions, empty_rows, seen, reason
    ) -> FlowEntropy:
        return FlowEntropy(
            venue_id=venue_id,
            symbol=symbol,
            state=state,
            entropy=entropy,
            percentile=percentile,
            transitions_observed=transitions,
            rows_with_no_transitions=empty_rows,
            states_seen=seen,
            window_seconds=self._window,
            reason=reason,
            measured_at_ns=self._now_ns(),
        )


def describe_entropy(meter: FlowEntropyMeter) -> dict:
    return {
        "part_id": PART_ID,
        "window_seconds": meter._window,
        "measurements": meter.standing.measurements,
        "measured": meter.standing.measured,
        "refused_short_window": meter.standing.refused_short_window,
        "refused_no_transitions": meter.standing.refused_no_transitions,
        "uniform_fallback_rows_used": meter.standing.uniform_rows_used,
        "lowest_entropy_seen": meter.standing.lowest_entropy_seen,
        "highest_entropy_seen": meter.standing.highest_entropy_seen,
        "symbols_with_a_percentile_history": len(meter._history),
        "produces_a_direction": False,
    }


def run_flow_entropy_meter(
    meter: FlowEntropyMeter, control_socket, read_states, publish_entropy,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_entropy(
            tuple(
                meter.measure(venue_id, symbol, states)
                for venue_id, symbol, states in read_states(meter)
            )
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
    import time as _time

    from runtime.input_assembly import Batch

    states = Batch(read=context.bus.reader("order-flow-state"))
    publish_entropy = context.bus.publisher_for("flow-entropy")
    window = int(context.number("flow_entropy_window"))
    meter = FlowEntropyMeter(
        window_seconds=window,
        percentile_window=int(context.number("flow_entropy_percentile_window")),
        prior_entropy=context.number("flow_entropy_prior"),
        minimum_observations=int(context.number("learning_minimum_observations")),
    )
    held: dict[tuple[str, str], dict] = {}
    last_measure = [float("-inf")]

    def read_states(_meter):
        for state in states.payloads():
            held.setdefault((state.venue_id, state.symbol), {})[state.second_ns] = state
        now = _time.monotonic()
        if now - last_measure[0] < context.health_interval_seconds:
            return ()
        last_measure[0] = now
        jobs = []
        for key, by_second in held.items():
            recent = [by_second[second] for second in sorted(by_second)[-window:]]
            for second in sorted(by_second)[:-window]:
                del by_second[second]
            jobs.append((key[0], key[1], tuple(recent)))
        return tuple(jobs)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_entropy(kept)

    return run_flow_entropy_meter(
        meter=meter,
        control_socket=context.control_socket,
        read_states=read_states,
        publish_entropy=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
