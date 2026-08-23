"""symbolic-hypothesis-miner: searching for a formula, and counting every one it tries.

The only part in this system that searches a space rather than reasoning about
one. That makes it the most productive source of hypotheses and the most
dangerous, because a search over enough formulas finds something that fits any
data set including pure noise.

The design is entirely about that danger:

- **Every formula evaluated is a trial, counted before it is judged.** A miner
  that reported only its best find would report one trial when it ran ten
  thousand, and every significance test downstream would be wrong by four orders
  of magnitude.
- **The search space is declared and bounded.** Which measurements, which
  operators, which depth. An unbounded space cannot have its trial count stated,
  and a trial count that cannot be stated cannot be corrected for.
- **Formulas are symbolic and readable.** `z_score < -2 and book_imbalance > 0.3`
  can be argued with, checked against a mechanism, and refuted. A weight vector
  cannot.
- **Held-out evaluation, always.** A formula is fitted on one period and scored on
  the next; the fitted score is never reported as evidence.

**A formula that fits the training data perfectly is reported as suspect**, not
as excellent. In a search this size a perfect fit is what overfitting looks like,
and reporting it as the best find is how a miner poisons everything downstream.

**Complexity is penalised.** A three-term formula that scores marginally better
than a one-term formula is worse: it had more ways to fit noise, and it will
generalise less.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "symbolic-hypothesis-miner"

PART_DECLARATION = PartDeclaration(
    part_id="symbolic-hypothesis-miner",
    consumes=("feature-reliability", "trade-episode", "kline-window", "training-label"),
    produces=("candidate-formula", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

MINED = "mined"
SUSPECT_PERFECT_FIT = "it-fits-the-training-data-too-well-to-believe"
NO_HELD_OUT_DATA = "no-held-out-period-to-score-on"
SPACE_EXHAUSTED = "every-formula-in-the-declared-space-has-been-tried"
TOO_LITTLE_DATA = "too-few-labelled-examples-to-search-on"

ABOVE = "above"
BELOW = "below"


@dataclass(frozen=True)
class Term:
    """One condition. Readable on purpose: a weight vector cannot be argued with."""

    measurement: str
    comparison: str
    threshold: float

    def holds_for(self, features: dict) -> bool | None:
        value = features.get(self.measurement)
        if value is None:
            return None
        return value > self.threshold if self.comparison == ABOVE else value < self.threshold

    def __str__(self) -> str:
        return f"{self.measurement} {self.comparison} {self.threshold:.4g}"


@dataclass(frozen=True)
class CandidateFormula:
    """One mined formula, with its held-out score and the trial count behind it."""

    formula_id: str
    family: str
    terms: tuple
    state: str
    fitted_hit_rate: float
    held_out_hit_rate: float | None
    held_out_trades: int
    base_rate: float
    complexity: int
    complexity_penalty: float
    trials_in_family: int
    reason: str
    mined_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == MINED and self.held_out_hit_rate is not None

    @property
    def held_out_excess(self) -> float | None:
        if self.held_out_hit_rate is None:
            return None
        return self.held_out_hit_rate - self.base_rate - self.complexity_penalty

    def __str__(self) -> str:
        return " and ".join(str(term) for term in self.terms)


@dataclass
class MinerStanding:
    formulas_evaluated: int = 0
    formulas_returned: int = 0
    suspect_perfect_fits: int = 0
    refused_no_held_out: int = 0
    refused_too_little_data: int = 0
    space_size: int = 0
    best_held_out_excess: float | None = None
    by_complexity: dict = field(default_factory=dict)


class SymbolicHypothesisMiner:
    """Searches a declared, bounded space of readable formulas, counting every trial."""

    def __init__(
        self,
        measurements: tuple,
        thresholds_per_measurement: tuple,
        maximum_terms: int,
        minimum_examples: int,
        minimum_held_out_examples: int,
        held_out_fraction: float,
        complexity_penalty_per_term: float,
        perfect_fit_threshold: float,
        now_ns=time.time_ns,
    ) -> None:
        if not measurements:
            raise ValueError(
                "an unbounded search space cannot have its trial count stated, and a trial "
                "count that cannot be stated cannot be corrected for"
            )
        if maximum_terms < 1:
            raise ValueError("a formula with no terms is not a formula")
        if not 0.0 < held_out_fraction < 1.0:
            raise ValueError("the held-out period is a fraction of the data")
        if complexity_penalty_per_term <= 0:
            raise ValueError(
                "without a complexity penalty the miner always prefers the formula with the "
                "most ways to fit noise"
            )
        self._measurements = tuple(measurements)
        self._thresholds = tuple(thresholds_per_measurement)
        self._maximum_terms = maximum_terms
        self._minimum_examples = minimum_examples
        self._minimum_held_out = minimum_held_out_examples
        self._held_out_fraction = held_out_fraction
        self._penalty = complexity_penalty_per_term
        self._perfect_fit = perfect_fit_threshold
        self._now_ns = now_ns
        self._examples: list = []
        self._trials: dict[str, int] = {}
        self._tried: set = set()
        self.standing = MinerStanding()
        self.standing.space_size = self.space_size()

    def space_size(self) -> int:
        """How many formulas the declared space contains. Stated, not estimated."""
        per_term = len(self._measurements) * len(self._thresholds) * 2
        total = 0
        for terms in range(1, self._maximum_terms + 1):
            total += math.comb(per_term, terms) if per_term >= terms else 0
        return total

    def observe_example(self, features: dict, label: bool, at_ns: int) -> None:
        """One labelled example, kept in time order so the split stays honest."""
        self._examples.append((dict(features), label, at_ns))

    def split(self) -> tuple[list, list]:
        """Fit on the earlier period, score on the later one. Never the reverse."""
        ordered = sorted(self._examples, key=lambda entry: entry[2])
        cut = int(len(ordered) * (1.0 - self._held_out_fraction))
        return ordered[:cut], ordered[cut:]

    def evaluate(self, terms: tuple, examples: list) -> tuple[float, int]:
        """A formula's hit rate on a set of examples, and how many it fired on."""
        fired = [
            label
            for features, label, _ in examples
            if all(term.holds_for(features) for term in terms)
        ]
        if not fired:
            return 0.0, 0
        return sum(1 for label in fired if label) / len(fired), len(fired)

    def mine(self, family: str, terms: tuple) -> tuple[CandidateFormula | None, str]:
        """One formula from the declared space, counted as a trial before it is judged."""
        self.standing.formulas_evaluated += 1

        # Counted here, before anything is scored. A miner reporting only its
        # best find would report one trial when it ran ten thousand.
        self._trials[family] = self._trials.get(family, 0) + 1
        trials = self._trials[family]
        self._tried.add(tuple(str(term) for term in terms))

        if len(self._examples) < self._minimum_examples:
            self.standing.refused_too_little_data += 1
            return None, TOO_LITTLE_DATA

        training, held_out = self.split()
        if len(held_out) < self._minimum_held_out:
            self.standing.refused_no_held_out += 1
            return None, NO_HELD_OUT_DATA

        fitted, fitted_count = self.evaluate(terms, training)
        held_out_rate, held_out_count = self.evaluate(terms, held_out)
        base_rate = (
            sum(1 for _, label, _ in self._examples if label) / len(self._examples)
        )
        complexity = len(terms)
        penalty = self._penalty * complexity
        self.standing.by_complexity[complexity] = (
            self.standing.by_complexity.get(complexity, 0) + 1
        )

        if fitted >= self._perfect_fit and fitted_count > 0:
            # In a space this size a perfect fit is what overfitting looks like,
            # and reporting it as the best find poisons everything downstream.
            self.standing.suspect_perfect_fits += 1
            return (
                self._formula(
                    family, trials, terms, SUSPECT_PERFECT_FIT, fitted, held_out_rate,
                    held_out_count, base_rate, complexity, penalty,
                    f"it fits {fitted:.0%} of {fitted_count} training example(s), at or above "
                    f"the {self._perfect_fit:.0%} that is what overfitting looks like in a "
                    f"space of {self.standing.space_size:,} formulas. Reported as suspect "
                    f"rather than as the best find",
                ),
                SUSPECT_PERFECT_FIT,
            )

        if held_out_count == 0:
            self.standing.refused_no_held_out += 1
            return None, NO_HELD_OUT_DATA

        excess = held_out_rate - base_rate - penalty
        if (
            self.standing.best_held_out_excess is None
            or excess > self.standing.best_held_out_excess
        ):
            self.standing.best_held_out_excess = excess

        self.standing.formulas_returned += 1
        return (
            self._formula(
                family, trials, terms, MINED, fitted, held_out_rate, held_out_count,
                base_rate, complexity, penalty,
                f"{' and '.join(str(term) for term in terms)} fits {fitted:.0%} in training and "
                f"{held_out_rate:.0%} on {held_out_count} held-out example(s) against a "
                f"{base_rate:.0%} base rate; after a {penalty:.1%} penalty for {complexity} "
                f"term(s) the excess is {excess:+.1%}. Trial {trials} in {family}, out of a "
                f"declared space of {self.standing.space_size:,}",
            ),
            MINED,
        )

    def already_tried(self, terms: tuple) -> bool:
        return tuple(str(term) for term in terms) in self._tried

    def trials_in(self, family: str) -> int:
        return self._trials.get(family, 0)

    def _formula(
        self, family, trials, terms, state, fitted, held_out, held_out_count,
        base_rate, complexity, penalty, reason,
    ) -> CandidateFormula:
        return CandidateFormula(
            formula_id=f"{family}:formula-{trials}",
            family=family,
            terms=tuple(terms),
            state=state,
            fitted_hit_rate=fitted,
            held_out_hit_rate=held_out if held_out_count else None,
            held_out_trades=held_out_count,
            base_rate=base_rate,
            complexity=complexity,
            complexity_penalty=penalty,
            trials_in_family=trials,
            reason=reason,
            mined_at_ns=self._now_ns(),
        )


def describe_mining(miner: SymbolicHypothesisMiner) -> dict:
    return {
        "part_id": PART_ID,
        "declared_space_size": miner.standing.space_size,
        "formulas_evaluated": miner.standing.formulas_evaluated,
        "formulas_returned": miner.standing.formulas_returned,
        "suspect_perfect_fits": miner.standing.suspect_perfect_fits,
        "refused_no_held_out_data": miner.standing.refused_no_held_out,
        "refused_too_little_data": miner.standing.refused_too_little_data,
        "best_held_out_excess": miner.standing.best_held_out_excess,
        "by_complexity": dict(sorted(miner.standing.by_complexity.items())),
        "trials_by_family": dict(sorted(miner._trials.items())),
        "measurements": list(miner._measurements),
    }


def run_symbolic_hypothesis_miner(
    miner: SymbolicHypothesisMiner, control_socket, read_examples, publish_formulas,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        candidates = read_examples(miner)
        formulas = []
        for family, terms in candidates:
            formula, _ = miner.mine(family, terms)
            if formula is not None:
                formulas.append(formula)
        publish_formulas(tuple(formulas))

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

    Examples are labelled trades: a training label's features with its
    "the setup was right" label, and a trade episode's conditions with whether
    it made money. The thresholds are estimated, never typed: once enough
    examples have arrived, every numeric feature is read as its percentile
    among the values this system has actually seen, and the terms are placed
    at the operator's quantiles -- so "imbalance above 0.75" means the top
    quarter for every feature alike. The declared space is walked in order, a
    bounded number of formulas per tick, so the trial count is the count of
    formulas actually evaluated and no tick runs long.

    Feature reliability and kline windows are consumed and drained: the
    miner's examples are labelled trades, and those two carry no label.
    """
    import bisect
    import itertools

    from runtime.input_assembly import Batch
    from runtime.learning_types import THE_SETUP_WAS_RIGHT

    labels = Batch(read=context.bus.reader("training-label"))
    episodes = Batch(read=context.bus.reader("trade-episode"))
    drained = (Batch(read=context.bus.reader("feature-reliability")), Batch(read=context.bus.reader("kline-window")))
    publish_formulas = context.bus.publisher_for("candidate-formula")

    quantiles = tuple(float(q) for q in context.setting("miner_threshold_quantiles").value)
    maximum_terms = int(context.number("miner_maximum_terms"))
    minimum_examples = int(context.number("miner_minimum_examples"))
    minimum_held_out = int(context.number("decoding_minimum_trades"))
    held_out_fraction = context.number("miner_held_out_fraction")
    penalty_per_term = context.number("miner_complexity_penalty_per_term")
    perfect_fit = context.number("miner_perfect_fit_threshold")
    formulas_per_tick = int(context.number("miner_formulas_per_tick"))

    raw_examples: list = []
    sorted_values: dict[str, list] = {}
    holder: dict = {"miner": None, "space": None}

    def numeric(features) -> dict:
        if not isinstance(features, dict):
            return {}
        return {
            str(name): float(value)
            for name, value in features.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }

    def as_percentiles(features: dict) -> dict:
        out = {}
        for name, value in features.items():
            seen = sorted_values.get(name)
            if seen:
                out[name] = bisect.bisect_right(seen, value) / len(seen)
        return out

    def build_miner_from_what_was_seen() -> None:
        by_feature: dict[str, list] = {}
        for features, _, _ in raw_examples:
            for name, value in features.items():
                by_feature.setdefault(name, []).append(value)
        # A feature present in every example has a distribution worth a threshold.
        measurements = tuple(sorted(name for name, values in by_feature.items() if len(values) == len(raw_examples)))
        if not measurements:
            return
        for name in measurements:
            sorted_values[name] = sorted(by_feature[name])
        miner = SymbolicHypothesisMiner(
            measurements=measurements, thresholds_per_measurement=quantiles,
            maximum_terms=maximum_terms, minimum_examples=minimum_examples,
            minimum_held_out_examples=minimum_held_out, held_out_fraction=held_out_fraction,
            complexity_penalty_per_term=penalty_per_term, perfect_fit_threshold=perfect_fit,
        )
        for features, label, at_ns in raw_examples:
            miner.observe_example(as_percentiles(features), label, at_ns)
        single_terms = [
            Term(measurement=name, comparison=comparison, threshold=threshold)
            for name in measurements for threshold in quantiles for comparison in (ABOVE, BELOW)
        ]

        def walk_the_space():
            for size in range(1, maximum_terms + 1):
                for terms in itertools.combinations(single_terms, size):
                    if len({term.measurement for term in terms}) < size:
                        continue  # two thresholds on one feature is one term written twice
                    yield "+".join(sorted({term.measurement for term in terms})), terms

        holder["miner"] = miner
        holder["space"] = walk_the_space()

    def take_examples() -> None:
        for label in labels.payloads():
            verdict = label.labels.get(THE_SETUP_WAS_RIGHT) if isinstance(label.labels, dict) else None
            features = numeric(label.features)
            if verdict is None or not features:
                continue
            raw_examples.append((features, bool(verdict), int(label.built_at_ns)))
            if holder["miner"] is not None:
                holder["miner"].observe_example(as_percentiles(features), bool(verdict), int(label.built_at_ns))
        for episode in episodes.payloads():
            features = numeric(episode.conditions)
            if not features:
                continue
            made_money = float(episode.realised) > 0.0
            raw_examples.append((features, made_money, int(episode.opened_at_ns)))
            if holder["miner"] is not None:
                holder["miner"].observe_example(as_percentiles(features), made_money, int(episode.opened_at_ns))

    def tick() -> None:
        for source in drained:
            source.payloads()
        take_examples()
        if holder["miner"] is None:
            if len(raw_examples) < minimum_examples:
                return
            build_miner_from_what_was_seen()
            if holder["miner"] is None:
                return
        miner = holder["miner"]
        formulas = []
        for _ in range(formulas_per_tick):
            candidate = next(holder["space"], None)
            if candidate is None:
                break
            family, terms = candidate
            if miner.already_tried(terms):
                continue
            formula, _ = miner.mine(family, terms)
            if formula is not None:
                formulas.append(formula)
        if formulas:
            publish_formulas(tuple(formulas))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
    )
