"""kronos-finetuner: adapting Kronos to the assets actually traded here.

The base model was trained on a broad corpus. This system trades a specific set
of crypto perpetuals with their own microstructure, and finetuning is what closes
that gap -- both halves of the model, because the tokenizer's quantisation is
fitted to a price distribution and a tokenizer tuned on equities quantises crypto
badly however good the transformer above it is.

**It never touches the live model.** Finetuning produces a new artefact under the
challenger role, and promotion is a separate decision delivered as
`champion-choice` (T-2). A finetuner that swapped the model in place would make
every retrain a live change with no way back.

**Training data is held out honestly.** The validation split is the *most recent*
candles, never a random sample: a random split lets the model see the future of
its own training rows through overlapping windows, and the resulting accuracy is
a number that cannot be reproduced live. This is the single most common way a
finetuned forecaster looks good and is not.

**A run that does not improve is discarded, not shipped.** The finetuner compares
the new artefact against the current champion on the held-out tail, and a model
that did not beat it is reported as such and not offered for promotion. Without
that, retraining on drift makes the model worse every time the drift was noise.

**It cannot run without the trainer being present**, and says so rather than
producing an untrained artefact -- an artefact that was never trained but carries
a version number is worse than none, because everything downstream trusts the
version.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "kronos-finetuner"

PART_DECLARATION = PartDeclaration(
    part_id="kronos-finetuner",
    consumes=(
        "kline-window", "model-drift-alert", "retrain-request", "sample-weight",
        "accelerator-slot",
    ),
    produces=("finetuned-model", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

TRAINED = "trained"
NO_TRAINER = "no-trainer-is-installed-on-this-machine"
NO_ACCELERATOR = "no-accelerator-slot-was-granted"
TOO_LITTLE_DATA = "too-few-windows-to-finetune-on"
DID_NOT_IMPROVE = "the-new-model-did-not-beat-the-champion-on-held-out-data"


@dataclass(frozen=True)
class FinetunedModel:
    """One trained artefact, with what it was trained on and how it scored.

    `validation_score` is on the held-out tail, never on training rows, and the
    field exists so nothing downstream has to take the improvement on trust.
    """

    name: str
    base_model: str
    trained_on_symbols: tuple
    training_windows: int
    validation_windows: int
    validation_score: float
    champion_score: float | None
    epochs: int
    tokenizer_was_refitted: bool
    artefact_path: str
    trained_at_ns: int

    @property
    def beat_the_champion(self) -> bool:
        return self.champion_score is None or self.validation_score > self.champion_score


@dataclass
class FinetunerStanding:
    runs_requested: int = 0
    runs_completed: int = 0
    refused_no_trainer: int = 0
    refused_no_accelerator: int = 0
    refused_too_little_data: int = 0
    discarded_no_improvement: int = 0
    windows_held: int = 0
    drift_alerts_seen: int = 0
    longest_run_seconds: float = 0.0
    by_symbol: dict = field(default_factory=dict)


class KronosFinetuner:
    """Finetunes the tokenizer and the predictor, and refuses to ship a model that did not help."""

    def __init__(
        self,
        minimum_training_windows: int,
        validation_fraction: float,
        epochs: int,
        refit_tokenizer: bool,
        maximum_windows_held: int,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < validation_fraction < 0.5:
            raise ValueError(
                "the held-out tail must be a real fraction and must not be most of the data"
            )
        if minimum_training_windows < 2:
            raise ValueError("a finetune on one window fits that window")
        self._minimum_windows = minimum_training_windows
        self._validation_fraction = validation_fraction
        self._epochs = epochs
        self._refit_tokenizer = refit_tokenizer
        self._maximum_windows = maximum_windows_held
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._windows: list = []
        self._sample_weights: dict[str, float] = {}
        self._trainer = None
        self._accelerator_granted = True
        self._champion_score: float | None = None
        self._pending_request: str | None = None
        self.standing = FinetunerStanding()

    def install_trainer(self, trainer) -> None:
        """The real training routine. Nothing here implements one.

        `trainer(training, validation, epochs, refit_tokenizer, weights)` returns
        `(artefact_path, validation_score, tokenizer_was_refitted)`.
        """
        self._trainer = trainer

    def observe_window(self, window) -> None:
        """One window of training data, kept in arrival order so the split stays honest."""
        self._windows.append(window)
        del self._windows[: max(0, len(self._windows) - self._maximum_windows)]
        self.standing.windows_held = len(self._windows)
        self.standing.by_symbol[window.symbol] = (
            self.standing.by_symbol.get(window.symbol, 0) + 1
        )

    def observe_sample_weight(self, symbol: str, weight: float) -> None:
        if weight <= 0:
            raise ValueError("a non-positive sample weight would erase the symbol from training")
        self._sample_weights[symbol] = weight

    def observe_drift_alert(self, reason: str) -> None:
        """Drift is a reason to retrain, and the reason travels with the request."""
        self.standing.drift_alerts_seen += 1
        self._pending_request = f"model drift: {reason}"

    def observe_retrain_request(self, reason: str) -> None:
        self._pending_request = reason

    def set_accelerator_slot(self, granted: bool) -> None:
        self._accelerator_granted = granted

    def observe_champion_score(self, score: float) -> None:
        """What the live model scores on the same held-out measure, to beat."""
        self._champion_score = score

    @property
    def has_a_pending_request(self) -> bool:
        return self._pending_request is not None

    def split(self) -> tuple[list, list]:
        """Training rows and the held-out tail.

        The tail, never a random sample: overlapping windows mean a random split
        lets the model see the future of its own training rows, and the accuracy
        that produces cannot be reproduced live.
        """
        cut = int(len(self._windows) * (1.0 - self._validation_fraction))
        return self._windows[:cut], self._windows[cut:]

    def finetune(self) -> tuple[FinetunedModel | None, str]:
        self.standing.runs_requested += 1
        reason = self._pending_request or "requested"
        self._pending_request = None

        if self._trainer is None:
            self.standing.refused_no_trainer += 1
            return None, NO_TRAINER

        if not self._accelerator_granted:
            self.standing.refused_no_accelerator += 1
            return None, NO_ACCELERATOR

        if len(self._windows) < self._minimum_windows:
            self.standing.refused_too_little_data += 1
            return None, TOO_LITTLE_DATA

        training, validation = self.split()
        if not training or not validation:
            self.standing.refused_too_little_data += 1
            return None, TOO_LITTLE_DATA

        started = self._monotonic()
        artefact_path, validation_score, tokenizer_refitted = self._trainer(
            training, validation, self._epochs, self._refit_tokenizer, dict(self._sample_weights)
        )
        duration = self._monotonic() - started
        self.standing.longest_run_seconds = max(self.standing.longest_run_seconds, duration)

        model = FinetunedModel(
            name=f"kronos-finetuned-{self._now_ns()}",
            base_model=getattr(self._trainer, "base_model", "kronos"),
            trained_on_symbols=tuple(sorted({window.symbol for window in training})),
            training_windows=len(training),
            validation_windows=len(validation),
            validation_score=validation_score,
            champion_score=self._champion_score,
            epochs=self._epochs,
            tokenizer_was_refitted=tokenizer_refitted,
            artefact_path=artefact_path,
            trained_at_ns=self._now_ns(),
        )

        if not model.beat_the_champion:
            # Discarded rather than shipped. Retraining on drift that was noise
            # makes the model worse every time, and nothing downstream would see
            # it happen.
            self.standing.discarded_no_improvement += 1
            return model, DID_NOT_IMPROVE

        self.standing.runs_completed += 1
        return model, TRAINED

    def release(self) -> None:
        """Drop the held windows. T-3: these are the largest thing this part holds."""
        self._windows.clear()
        self.standing.windows_held = 0


def describe_finetuning(finetuner: KronosFinetuner) -> dict:
    return {
        "part_id": PART_ID,
        "trainer_is_installed": finetuner._trainer is not None,
        "runs_requested": finetuner.standing.runs_requested,
        "runs_that_produced_a_better_model": finetuner.standing.runs_completed,
        "discarded_for_no_improvement": finetuner.standing.discarded_no_improvement,
        "refused_no_trainer_installed": finetuner.standing.refused_no_trainer,
        "refused_no_accelerator_slot": finetuner.standing.refused_no_accelerator,
        "refused_too_little_data": finetuner.standing.refused_too_little_data,
        "drift_alerts_seen": finetuner.standing.drift_alerts_seen,
        "windows_held": finetuner.standing.windows_held,
        "longest_run_seconds": finetuner.standing.longest_run_seconds,
        "windows_by_symbol": dict(sorted(finetuner.standing.by_symbol.items())),
    }


def run_kronos_finetuner(
    finetuner: KronosFinetuner, control_socket, read_windows_and_requests, publish_model,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_windows_and_requests(finetuner)
        if finetuner.has_a_pending_request:
            model, outcome = finetuner.finetune()
            publish_model(model, outcome)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
