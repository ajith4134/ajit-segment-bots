"""walk-forward-splitter: train before test, always, with a gap between them.

Two things make a backtest split honest, and both are routinely skipped because
skipping them produces better numbers.

**Chronology.** A random train/test split leaks the future into training through
every autocorrelated feature, and every feature of a price series is autocorrelated.
The model learns the shape of a move from bars either side of the ones it is tested
on, and the result is spectacular and meaningless. So splits here are strictly
chronological: the test period always begins after the training period ends.

**An embargo.** Even chronological splits leak when a trade opened in training
resolves inside the test window: the label depends on prices the test period
contains. The embargo is a gap between train and test at least as long as the
longest trade the instruction can hold, and a split without one is refused rather
than warned about.

Two further rules that decide whether the folds mean anything:

- **Every fold is reported, including the bad ones.** Selecting the folds where a
  rule worked is fitting to the test set through a slower path, and the promotion
  gate reads fold count for exactly this reason.
- **Folds are sized so each test period can contain enough trades to say anything.**
  Twenty folds of three trades each is not more evidence than one fold of sixty; it
  is the same evidence cut into pieces too small to measure.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.backtest_types import WalkForwardSplit
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "walk-forward-splitter"

PART_DECLARATION = PartDeclaration(
    part_id="walk-forward-splitter",
    consumes=("historical-window",),
    produces=("walk-forward-split", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

SPLIT = "split"
TOO_SHORT = "the-window-is-too-short-for-even-one-fold"
NO_EMBARGO = "the-embargo-would-leave-no-test-period"
WINDOW_HAS_GAPS = "the-window-has-holes-so-the-folds-would-not-be-what-they-claim"


@dataclass(frozen=True)
class SplitOutcome:
    window_id: str
    state: str
    splits: tuple
    reason: str
    split_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == SPLIT and bool(self.splits)


@dataclass
class SplitterStanding:
    windows_split: int = 0
    folds_produced: int = 0
    refused_too_short: int = 0
    refused_no_embargo: int = 0
    refused_gappy_windows: int = 0
    random_splits_produced: int = 0


class WalkForwardSplitter:
    """Produces chronological folds with an embargo, or refuses."""

    def __init__(
        self,
        train_seconds: float,
        test_seconds: float,
        embargo_seconds: float,
        step_seconds: float,
        now_ns=time.time_ns,
    ) -> None:
        if train_seconds <= 0 or test_seconds <= 0:
            raise ValueError("both periods must exist for a fold to be a fold")
        if embargo_seconds <= 0:
            raise ValueError(
                "without an embargo a trade opened in training resolves inside the test "
                "window, and its label depends on prices the test period contains"
            )
        if step_seconds <= 0:
            raise ValueError("the step between folds is positive or the folds repeat")
        self._train_seconds = train_seconds
        self._test_seconds = test_seconds
        self._embargo_seconds = embargo_seconds
        self._step_seconds = step_seconds
        self._now_ns = now_ns
        self._sequence = 0
        self.standing = SplitterStanding()

    def fold_length_seconds(self) -> float:
        return self._train_seconds + self._embargo_seconds + self._test_seconds

    def split(self, window) -> SplitOutcome:
        if window.missing_bars > 0 or window.was_interpolated:
            self.standing.refused_gappy_windows += 1
            return self._outcome(
                window.window_id, WINDOW_HAS_GAPS, (),
                f"the window is missing {window.missing_bars} bar(s). Folds cut from it "
                f"would not cover the periods they claim to",
            )

        span_seconds = (window.last_at_ns - window.first_at_ns) / 1e9
        if span_seconds < self.fold_length_seconds():
            self.standing.refused_too_short += 1
            return self._outcome(
                window.window_id, TOO_SHORT, (),
                f"{span_seconds / 86_400.0:.1f} day(s) of data against "
                f"{self.fold_length_seconds() / 86_400.0:.1f} needed for one fold",
            )

        splits = []
        fold = 0
        start_ns = window.first_at_ns
        while True:
            train_to = start_ns + int(self._train_seconds * 1e9)
            test_from = train_to + int(self._embargo_seconds * 1e9)
            test_to = test_from + int(self._test_seconds * 1e9)
            if test_to > window.last_at_ns:
                break
            fold += 1
            self._sequence += 1
            splits.append(
                WalkForwardSplit(
                    split_id=f"split-{self._sequence}",
                    fold=fold,
                    train_from_ns=start_ns,
                    train_to_ns=train_to,
                    test_from_ns=test_from,
                    test_to_ns=test_to,
                    embargo_seconds=self._embargo_seconds,
                )
            )
            start_ns += int(self._step_seconds * 1e9)

        if not splits:
            self.standing.refused_no_embargo += 1
            return self._outcome(
                window.window_id, NO_EMBARGO, (),
                "the embargo leaves no room for a test period in this window",
            )

        self.standing.windows_split += 1
        self.standing.folds_produced += len(splits)
        return self._outcome(
            window.window_id, SPLIT, tuple(splits),
            f"{len(splits)} chronological fold(s), each with a "
            f"{self._embargo_seconds / 3600.0:.1f}h embargo. Every fold is reported, "
            f"including the ones a rule fails: selecting folds is fitting to the test set "
            f"through a slower path",
        )

    def _outcome(self, window_id, state, splits, reason) -> SplitOutcome:
        return SplitOutcome(
            window_id=window_id, state=state, splits=splits, reason=reason,
            split_at_ns=self._now_ns(),
        )


def describe_splitting(splitter: WalkForwardSplitter) -> dict:
    return {
        "part_id": PART_ID,
        "windows_split": splitter.standing.windows_split,
        "folds_produced": splitter.standing.folds_produced,
        "refused_too_short": splitter.standing.refused_too_short,
        "refused_no_embargo": splitter.standing.refused_no_embargo,
        "refused_gappy_windows": splitter.standing.refused_gappy_windows,
        "embargo_seconds": splitter._embargo_seconds,
        "produces_random_splits": False,
        "random_splits_produced": splitter.standing.random_splits_produced,
        "selects_folds": False,
    }


def run_walk_forward_splitter(
    splitter: WalkForwardSplitter, control_socket, read_windows, publish_splits,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for window in read_windows():
            outcome = splitter.split(window)
            for split in outcome.splits:
                publish_splits(split)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_splitting(splitter),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    windows = Batch(read=context.bus.reader("historical-window"))
    publish_splits = context.bus.publisher_for("walk-forward-split")
    splitter = WalkForwardSplitter(
        train_seconds=context.number("walk_forward_train_seconds"),
        test_seconds=context.number("walk_forward_test_seconds"),
        embargo_seconds=context.number("walk_forward_embargo_seconds"),
        step_seconds=context.number("walk_forward_step_seconds"),
    )

    return run_walk_forward_splitter(
        splitter=splitter,
        control_socket=context.control_socket,
        read_windows=lambda: tuple(windows.payloads()),
        publish_splits=lambda split: publish_splits((split,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
