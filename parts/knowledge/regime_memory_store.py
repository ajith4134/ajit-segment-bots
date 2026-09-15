"""regime-memory-store: what each regime taught, kept so the next one can be recognised.

Regimes recur. The chop that follows a rally is the same chop that followed the
last one, and a system that starts from nothing each time relearns the same
lessons at the same cost. This store is what makes the second occurrence cheaper
than the first.

What it keeps per regime, and why:

- **Which instructions worked and which did not.** The most directly useful
  thing: when a regime is recognised, its instructions can be reweighted
  immediately rather than after another hundred trades.
- **What the regime looked like.** Its volatility, its correlation level, its
  typical move -- so a new regime can be matched against the remembered ones
  rather than merely labelled as new.
- **How long it lasted and how it ended.** A regime that has always lasted three
  weeks is different information from one that has lasted between a day and a
  year, and the second means the memory should be trusted less.

**A recurrence is recognised on the signature, never on the name.** Names are
assigned by a classifier that can change; the signature is what the market did.

**Memory of a regime does not become confidence in it.** A remembered regime's
instructions are reweighted, not reinstated -- the recurrence may be superficial,
and the evidence for that only arrives after trading it again.

**Occurrences are counted.** A regime seen once is a story; one seen five times
with consistent lessons is a memory worth acting on, and the count is what
separates them.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "regime-memory-store"

PART_DECLARATION = PartDeclaration(
    part_id="regime-memory-store",
    consumes=(
        "trade-episode", "market-regime", "instruction-scorecard", "regime-transition-flag",
    ),
    produces=("regime-memory", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

RECOGNISED = "recognised-from-its-signature"
NEW_REGIME = "nothing-remembered-looks-like-this"
TOO_FEW_OCCURRENCES = "seen-once-is-a-story-rather-than-a-memory"


@dataclass(frozen=True)
class RegimeSignature:
    """What a regime looked like, which is what a recurrence is recognised on.

    Never the name: names are assigned by a classifier that can change, and the
    signature is what the market actually did.
    """

    volatility: float
    correlation: float
    typical_move: float
    hurst: float

    def distance_to(self, other: "RegimeSignature") -> float:
        return math.sqrt(
            sum(
                ((a - b) / max(abs(a), abs(b), 1e-9)) ** 2
                for a, b in (
                    (self.volatility, other.volatility),
                    (self.correlation, other.correlation),
                    (self.typical_move, other.typical_move),
                    (self.hurst, other.hurst),
                )
            )
        )


@dataclass(frozen=True)
class RegimeMemory:
    """What one regime taught, and how much to trust it."""

    regime: str
    state: str
    signature: RegimeSignature | None
    occurrences: int
    instructions_that_worked: tuple
    instructions_that_did_not: tuple
    median_duration_seconds: float | None
    duration_spread_seconds: float | None
    how_it_usually_ended: str | None
    matched_distance: float | None
    reason: str
    recalled_at_ns: int

    @property
    def is_worth_acting_on(self) -> bool:
        return self.state == RECOGNISED

    @property
    def duration_is_predictable(self) -> bool:
        """A regime lasting between a day and a year should be trusted less."""
        if self.median_duration_seconds is None or self.duration_spread_seconds is None:
            return False
        return self.duration_spread_seconds < self.median_duration_seconds


@dataclass
class StoreStanding:
    occurrences_recorded: int = 0
    regimes_remembered: int = 0
    recognitions: int = 0
    new_regimes: int = 0
    too_few_occurrences: int = 0
    instruction_outcomes_recorded: int = 0
    by_regime: dict = field(default_factory=dict)


class RegimeMemoryStore:
    """Remembers what each regime taught, and recognises recurrences by signature."""

    def __init__(
        self,
        minimum_occurrences: int,
        signature_tolerance: float,
        minimum_instruction_trades: int,
        prior_hit_rate: float,
        prior_weight: float,
        half_life_observations: float,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_occurrences < 2:
            raise ValueError(
                "a regime seen once is a story; a memory worth acting on needs a recurrence"
            )
        if signature_tolerance <= 0:
            raise ValueError(
                "without a tolerance nothing ever matches and every regime is new"
            )
        self._minimum_occurrences = minimum_occurrences
        self._tolerance = signature_tolerance
        self._minimum_trades = minimum_instruction_trades
        self._prior_hit_rate = prior_hit_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._now_ns = now_ns
        self._signatures: dict[str, list] = {}
        self._durations: dict[str, list] = {}
        self._endings: dict[str, dict] = {}
        self._instruction_rates: dict[tuple[str, str], RateEstimator] = {}
        self.standing = StoreStanding()

    def record_occurrence(
        self, regime: str, signature: RegimeSignature, duration_seconds: float, how_it_ended: str
    ) -> None:
        """One completed occurrence of a regime, with what it looked like and how it ended."""
        self._signatures.setdefault(regime, []).append(signature)
        self._durations.setdefault(regime, []).append(duration_seconds)
        self._endings.setdefault(regime, {})
        self._endings[regime][how_it_ended] = self._endings[regime].get(how_it_ended, 0) + 1
        self.standing.occurrences_recorded += 1
        self.standing.regimes_remembered = len(self._signatures)
        self.standing.by_regime[regime] = len(self._signatures[regime])

    def record_instruction_outcome(self, regime: str, instruction_id: str, was_right: bool) -> None:
        """What one instruction did in this regime, so it can be reweighted on recurrence."""
        self._rate_for(regime, instruction_id).observe(was_right)
        self.standing.instruction_outcomes_recorded += 1

    def recognise(self, signature: RegimeSignature) -> RegimeMemory:
        """Which remembered regime this looks like, on the signature rather than the name."""
        best_regime = None
        best_distance = None

        for regime, signatures in self._signatures.items():
            if len(signatures) < self._minimum_occurrences:
                continue
            average = self._average_signature(signatures)
            distance = signature.distance_to(average)
            if best_distance is None or distance < best_distance:
                best_regime, best_distance = regime, distance

        if best_regime is None:
            self.standing.too_few_occurrences += 1
            return self._memory(
                "", TOO_FEW_OCCURRENCES, None, 0, (), (), None, None, None, None,
                f"no regime has {self._minimum_occurrences} recorded occurrence(s). A regime "
                f"seen once is a story; one seen several times with consistent lessons is a "
                f"memory, and the count is what separates them",
            )

        if best_distance > self._tolerance:
            self.standing.new_regimes += 1
            return self._memory(
                "", NEW_REGIME, signature, 0, (), (), None, None, None, best_distance,
                f"the nearest remembered regime is {best_regime} at a distance of "
                f"{best_distance:.2f}, past the {self._tolerance:.2f} that counts as a "
                f"recurrence. This is genuinely new",
            )

        self.standing.recognitions += 1
        return self.memory_of(best_regime, matched_distance=best_distance)

    def memory_of(self, regime: str, matched_distance: float | None = None) -> RegimeMemory:
        """What this regime taught, with its instructions split by what they did."""
        signatures = self._signatures.get(regime, [])
        durations = self._durations.get(regime, [])
        occurrences = len(signatures)

        worked = []
        did_not = []
        for (kept_regime, instruction_id), estimator in sorted(self._instruction_rates.items()):
            if kept_regime != regime:
                continue
            estimate = estimator.estimate(self._minimum_trades)
            if not estimate.is_fitted:
                continue
            (worked if estimate.value > 0.5 else did_not).append(instruction_id)

        median = sorted(durations)[len(durations) // 2] if durations else None
        spread = self._spread(durations)
        endings = self._endings.get(regime, {})
        usual_ending = max(endings, key=endings.get) if endings else None

        return self._memory(
            regime, RECOGNISED, self._average_signature(signatures) if signatures else None,
            occurrences, tuple(worked), tuple(did_not), median, spread, usual_ending,
            matched_distance,
            f"{regime} has occurred {occurrences} time(s)"
            + (
                f", lasting {median / 86400:.1f} day(s) typically"
                + (
                    f" with a spread of {spread / 86400:.1f} day(s) -- wide enough that the "
                    f"duration should be trusted less"
                    if spread is not None and median is not None and spread >= median
                    else ""
                )
                if median is not None
                else ""
            )
            + (f", usually ending in {usual_ending}" if usual_ending else "")
            + f". {len(worked)} instruction(s) worked here and {len(did_not)} did not, so they "
            f"can be reweighted on recurrence rather than relearned over another hundred "
            f"trades. Reweighted, not reinstated: the recurrence may be superficial, and the "
            f"evidence for that only arrives after trading it again",
        )

    def _average_signature(self, signatures) -> RegimeSignature:
        count = len(signatures)
        return RegimeSignature(
            volatility=sum(entry.volatility for entry in signatures) / count,
            correlation=sum(entry.correlation for entry in signatures) / count,
            typical_move=sum(entry.typical_move for entry in signatures) / count,
            hurst=sum(entry.hurst for entry in signatures) / count,
        )

    def _spread(self, values) -> float | None:
        if len(values) < 2:
            return None
        mean = sum(values) / len(values)
        return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))

    def _rate_for(self, regime: str, instruction_id: str) -> RateEstimator:
        key = (regime, instruction_id)
        estimator = self._instruction_rates.get(key)
        if estimator is None:
            estimator = RateEstimator(
                prior=self._prior_hit_rate, prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._instruction_rates[key] = estimator
        return estimator

    def _memory(
        self, regime, state, signature, occurrences, worked, did_not,
        median, spread, ending, distance, reason,
    ) -> RegimeMemory:
        return RegimeMemory(
            regime=regime,
            state=state,
            signature=signature,
            occurrences=occurrences,
            instructions_that_worked=worked,
            instructions_that_did_not=did_not,
            median_duration_seconds=median,
            duration_spread_seconds=spread,
            how_it_usually_ended=ending,
            matched_distance=distance,
            reason=reason,
            recalled_at_ns=self._now_ns(),
        )


def describe_regime_memory(store: RegimeMemoryStore) -> dict:
    return {
        "part_id": PART_ID,
        "occurrences_recorded": store.standing.occurrences_recorded,
        "regimes_remembered": store.standing.regimes_remembered,
        "recognitions": store.standing.recognitions,
        "new_regimes": store.standing.new_regimes,
        "too_few_occurrences": store.standing.too_few_occurrences,
        "instruction_outcomes_recorded": store.standing.instruction_outcomes_recorded,
        "by_regime": dict(sorted(store.standing.by_regime.items())),
        "recognises_on": "signature",
    }


def run_regime_memory_store(
    store: RegimeMemoryStore, control_socket, read_regimes, publish_memories,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        signatures = read_regimes(store)
        publish_memories(tuple(store.recognise(signature) for signature in signatures))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_regime_memory(store),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A regime's signature is built from what the classifier measured --
    its volatility and Hurst exponent. The signature's other two
    dimensions, correlation and typical move, are on no input this part
    declares and are held constant, so they contribute nothing to any
    distance rather than a number nobody measured. An occurrence is
    recorded when a transition flag says a trade ended in a different
    regime from the one it opened in, with the trade's own duration; an
    instruction's outcome is recorded against the regime the episode that
    produced it names. Every classified regime is recognised each tick.
    """
    from runtime.input_assembly import Batch, LatestByKey

    episodes = Batch(read=context.bus.reader("trade-episode"))
    regimes = LatestByKey(read=context.bus.reader("market-regime"), key_of=lambda r: (r.venue_id, r.symbol))
    scorecards = Batch(read=context.bus.reader("instruction-scorecard"))
    flags = Batch(read=context.bus.reader("regime-transition-flag"))
    publish_memories = context.bus.publisher_for("regime-memory")
    store = RegimeMemoryStore(
        minimum_occurrences=int(context.number("regime_memory_minimum_occurrences")),
        signature_tolerance=context.number("regime_memory_signature_tolerance"),
        minimum_instruction_trades=int(context.number("decoding_minimum_trades")),
        prior_hit_rate=context.number("learning_prior_hit_rate"),
        prior_weight=context.number("learning_prior_weight"),
        half_life_observations=context.number("learning_half_life_observations"),
    )
    HELD_CONSTANT = 0.0  # the unmeasured dimensions, identical in every signature
    episode_by_trade: dict[str, object] = {}
    signature_of_regime: dict[str, RegimeSignature] = {}

    def signature_for(reading) -> RegimeSignature | None:
        if reading.volatility is None or reading.hurst is None:
            return None
        return RegimeSignature(
            volatility=float(reading.volatility), correlation=HELD_CONSTANT,
            typical_move=HELD_CONSTANT, hurst=float(reading.hurst),
        )

    def read_regimes(_store):
        scorecards.payloads()
        for reading in regimes.mapping().values():
            signature = signature_for(reading)
            if signature is not None:
                signature_of_regime[reading.regime] = signature
        for episode in episodes.payloads():
            trade_id = episode.trade_id
            episode_by_trade[trade_id] = episode
            conditions = episode.conditions if isinstance(episode.conditions, dict) else {}
            instruction_id = conditions.get("instruction_id")
            if instruction_id is not None:
                store.record_instruction_outcome(episode.regime, str(instruction_id), episode.realised > 0)
        for flag in flags.payloads():
            if not flag.changed or flag.regime_at_entry is None:
                continue
            signature = signature_of_regime.get(flag.regime_at_entry)
            episode = episode_by_trade.get(flag.trade_id)
            if signature is None or episode is None:
                continue
            duration = max(0.0, ((flag.changed_at_ns or episode.closed_at_ns) - episode.opened_at_ns) / 1e9)
            store.record_occurrence(flag.regime_at_entry, signature, duration, str(flag.regime_at_exit))
        return tuple(signature_of_regime[name] for name in sorted(signature_of_regime))

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_memories(kept)

    return run_regime_memory_store(
        store=store,
        control_socket=context.control_socket,
        read_regimes=read_regimes,
        publish_memories=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
