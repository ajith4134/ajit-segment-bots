"""label-builder, run over this project's own real closed trades (RL-063).

Every trade here is one this system actually opened and closed on the Indian
options segments on 2026-09-07 and 2026-09-08, taken verbatim out of
`position-recorder`'s journal along with the `peak-excursion` that preceded each
close -- the same two inputs `start_part` pairs on the bus. Nothing is invented:
a fixture is exactly what would have hidden the defect these tests exist for.

**What they are guarding.** Until 2026-09-12 `THE_SIZE_WAS_RIGHT` read

    excursion.peak_adverse_fraction * multiple <= abs(self._stop_distance(trade))
    if self._stop_distance(trade) else True

against a `ClosedTradeRecord` that never declared `stop_distance_fraction`. The
`getattr` default therefore returned 0.0 for every trade ever labelled, 0.0 is
falsy, and the `else True` branch fired every time: **the part asserted that the
size was right on every label it could ever produce**, including the seven NIFTY
trades in this fixture that are 91.4% of every rupee this project has lost.

Nothing reported it. A component that is always true is indistinguishable from a
component that is never wrong, and only reading the code apart from its output
tells the two apart.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from parts.learning_loop.label_builder import (
    ClosedTradeRecord, ExcursionRecord, LabelBuilder,
)
from runtime.learning_types import (
    THE_ENTRY_WAS_TIMED, THE_EXIT_WAS_TIMED, THE_SETUP_WAS_RIGHT, THE_SIZE_WAS_RIGHT,
)

CAPTURE = (
    pathlib.Path(__file__).resolve().parents[3]
    / "tests" / "captured" / "upstox" / "2026-09-08-closed-trades-and-excursions.jsonl"
)

# The operator's own live values, read from
# ~/.config/ajit-segment-bots/settings/runtime.toml on 2026-09-12. Stated here
# rather than read, so the test measures the part and not the settings file.
FAVOURABLE_THRESHOLD = 0.005
ADVERSE_ENTRY_THRESHOLD = 0.01
EXIT_CAPTURE_THRESHOLD = 0.5
SIZE_SURVIVAL_MULTIPLE = 1.0

# What `execution-cost-model` was actually publishing for these symbols: an
# unfitted estimate. Its live standing on 2026-09-12 read 628,233 estimates and
# 0 symbols_fitted, so this is the real cost the builder would have been handed,
# not a convenient one.
ROUND_TRIP_COST_FRACTION = 0.0085

# The trade that is 27.4% of every rupee this project has lost.
WORST_TRADE = "NIFTY 24550 CE 08 SEP 26"


def _builder() -> LabelBuilder:
    return LabelBuilder(
        favourable_threshold=FAVOURABLE_THRESHOLD,
        adverse_entry_threshold=ADVERSE_ENTRY_THRESHOLD,
        exit_capture_threshold=EXIT_CAPTURE_THRESHOLD,
        size_survival_multiple=SIZE_SURVIVAL_MULTIPLE,
    )


@pytest.fixture(scope="module")
def real_closed_trades():
    """Every real closed trade in the capture, paired with its own excursion.

    Pairs exactly the way `start_part` does: excursions are held per (venue,
    symbol) as they arrive and popped by the close that follows them.
    """
    assert CAPTURE.exists(), (
        f"{CAPTURE} is missing. These tests run on trades this system really took "
        f"(RL-063); there is no hand-written fallback."
    )
    latest: dict[tuple[str, str], dict] = {}
    paired: list[tuple[dict, dict | None]] = []
    for line in CAPTURE.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        payload = record["payload"]
        key = (payload["venue_id"], payload["symbol"])
        if record["kind"] == "peak-excursion":
            latest[key] = payload
        else:
            paired.append((payload, latest.pop(key, None)))
    assert len(paired) > 50, "the capture must hold enough closed trades to mean something"
    return paired


def _record(trade: dict, stop_distance_fraction: float = 0.0) -> ClosedTradeRecord:
    return ClosedTradeRecord(
        venue_id=trade["venue_id"],
        symbol=trade["symbol"],
        detector="unknown",
        regime="unknown",
        side=trade["direction"],
        entry_price=trade["entry_price"],
        exit_price=trade["exit_price"],
        quantity=trade["quantity"],
        opened_at_ns=trade["opened_at_ns"],
        closed_at_ns=trade["closed_at_ns"],
        horizon_seconds=3600.0,
        features={},
        stop_distance_fraction=stop_distance_fraction,
    )


def _excursion(trade: dict, excursion: dict) -> ExcursionRecord:
    entry = trade["entry_price"] or 1.0
    return ExcursionRecord(
        peak_favourable_fraction=abs(excursion["best_price"] - entry) / entry,
        peak_adverse_fraction=abs(excursion["worst_price"] - entry) / entry,
        seconds_to_peak_favourable=0.0,
        seconds_to_peak_adverse=0.0,
        observations=excursion["samples"],
    )


def _feed(builder: LabelBuilder, paired, stop_distance_fraction: float = 0.0):
    """Label every trade that has an excursion, and return the labels built."""
    labels = []
    for trade, excursion in paired:
        if excursion is None:
            continue
        record = _record(trade, stop_distance_fraction)
        builder.observe_excursion(
            trade["venue_id"], trade["symbol"], trade["opened_at_ns"],
            _excursion(trade, excursion),
        )
        builder.observe_cost_estimate(
            trade["venue_id"], trade["symbol"], ROUND_TRIP_COST_FRACTION,
        )
        label, _reason = builder.build(record)
        if label is not None:
            labels.append((trade, label))
    return labels


def test_the_part_builds_labels_from_trades_this_system_really_took(real_closed_trades):
    """The loop from a closed trade to something a model can learn from works.

    `label-builder` joined the live spine at 09:56 on 2026-09-08, after the last
    trade of that session closed, and nothing has closed since -- so its live
    standing has read `trades_seen 0` ever since, which is NOT MEASURED and not
    a defect. This is the measurement.
    """
    builder = _builder()
    labels = _feed(builder, real_closed_trades)

    assert labels, "no label was built from any real closed trade"
    assert builder.standing.trades_seen == len(
        [pair for pair in real_closed_trades if pair[1] is not None]
    )
    assert builder.standing.labels_built == len(labels)
    assert builder.standing.refused_no_costs == 0, (
        "every trade was given a cost estimate, so none may be refused for wanting one"
    )
    for _trade, label in labels:
        assert label.is_complete, "setup, entry and exit must all be judged"


def test_size_is_left_unjudged_when_no_stop_distance_is_known(real_closed_trades):
    """The regression guard for the defect this file exists for.

    With no stop distance the component is unknowable, and an unknowable
    component is omitted -- never asserted true. Before 2026-09-12 every one of
    these labels claimed the size was right.
    """
    builder = _builder()
    labels = _feed(builder, real_closed_trades, stop_distance_fraction=0.0)

    assert labels
    for _trade, label in labels:
        assert THE_SIZE_WAS_RIGHT not in label.labels, (
            "with no stop distance the size cannot be judged; asserting it was right is "
            "what taught every model that size is never the problem"
        )
        assert label.label_for(THE_SIZE_WAS_RIGHT) is None

    assert builder.standing.size_not_judgeable == len(labels), (
        "every unjudgeable size must be counted, so the gap is visible rather than silent"
    )
    assert not any(
        key.startswith(f"{THE_SIZE_WAS_RIGHT}:") for key in builder.standing.by_component
    ), "an omitted component must not be counted as either true or false"


def test_size_is_judged_when_the_stop_distance_is_known(real_closed_trades):
    """Given a real stop distance the component is computed, both ways.

    The seven NIFTY trades of 2026-09-08 were bound with a stop at 0.05 against
    a decision price of 0.10 -- half the entry away, read off the `bounded-order`
    records in the trade-lifecycle journal. A tight stop must be able to make
    this component false, or it is the old hardcoded true wearing arithmetic.
    """
    tight = _builder()
    tight_labels = _feed(tight, real_closed_trades, stop_distance_fraction=0.01)
    wide = _builder()
    wide_labels = _feed(wide, real_closed_trades, stop_distance_fraction=0.5)

    assert tight_labels and wide_labels
    assert tight.standing.size_not_judgeable == 0
    assert wide.standing.size_not_judgeable == 0

    judged_false = [
        label for _trade, label in tight_labels if label.label_for(THE_SIZE_WAS_RIGHT) is False
    ]
    judged_true = [
        label for _trade, label in wide_labels if label.label_for(THE_SIZE_WAS_RIGHT) is True
    ]
    assert judged_false, "a 1% stop must be breached by some real trade's adverse excursion"
    assert judged_true, "a 50% stop must survive some real trade's adverse excursion"


def test_the_worst_trade_this_project_ever_took_is_labelled_at_all(real_closed_trades):
    """The single trade that is 27.4% of the total loss reaches the learning loop.

    Deliberately not an assertion about which way each component falls -- that is
    the model's business. What is asserted is that the trade is labelled rather
    than dropped, because a loss the learning loop never sees is a loss the
    system is free to repeat.
    """
    builder = _builder()
    labels = _feed(builder, real_closed_trades)

    worst = [label for trade, label in labels if trade["symbol"] == WORST_TRADE]
    assert worst, f"{WORST_TRADE} produced no label at all"
    for label in worst:
        assert label.is_complete
