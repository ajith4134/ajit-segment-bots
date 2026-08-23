"""Learning that survives the off switch, and the four ways it refuses to.

The defect this covers was measured on the live run of 2026-08-22 17:37-18:42:
`bull-conviction-model` built its model fresh in `start_part`, so 184 412 noticed
setups produced no opinion and the training count went back to zero at the next
fork. A model that cannot carry what it learned across a restart cannot learn at
all on a machine that reboots.

The models here are trained on **real captured BTCUSDT trades** (RL-063). The
features are the ones a price series actually gives -- a return and a rolling
deviation -- and the label is whether the next trade went up. That is not the bull
bot's feature set and is not meant to be: what is under test is that a model's
numbers survive a process boundary unchanged, and a series with real
autocorrelation is what makes "unchanged" mean something. A model fed constants
would round-trip perfectly while learning nothing.
"""

import json
import math
import pathlib

import pytest

from runtime.learned_state import (
    RESTORED,
    SCHEMA_VERSION,
    STARTED_COLD_NO_CHECKPOINT,
    STARTED_COLD_SCHEMA_CHANGED,
    STARTED_COLD_SETTINGS_CHANGED,
    STARTED_COLD_UNREADABLE,
    CheckpointSchedule,
    LearnedStateStore,
    compare_settings,
)
from runtime.online_learner import OnlineLogisticModel, ProbabilityCalibrator

PART = "bull-conviction-model"
COMPONENT = "conviction"


@pytest.fixture(scope="module")
def real_trade_prices(read_captured_payloads):
    """What BTCUSDT actually traded at on 2026-08-22, in order."""
    prices = []
    for _, payload in read_captured_payloads("binance-usdm", "2026-08-22-btcusdt-aggtrade-run.jsonl"):
        message = json.loads(payload)
        if message.get("e") == "aggTrade":
            prices.append(float(message["p"]))
    assert len(prices) >= 500, f"only {len(prices)} real trades; too short to train on"
    return prices


def train_on_real_prices(model: OnlineLogisticModel, prices, limit: int = 400) -> int:
    """Train one model on the real series. Returns how many outcomes it saw.

    Both classes are reached because a real tick series goes both ways; a series
    that only rose would leave `is_fitted` false however many rows were fed, which
    is the model refusing correctly rather than the test failing.
    """
    trained = 0
    for index in range(2, min(limit, len(prices) - 1)):
        window = prices[max(0, index - 20):index]
        mean = sum(window) / len(window)
        spread = math.sqrt(sum((value - mean) ** 2 for value in window) / len(window))
        features = {
            "return_since_previous": (prices[index] - prices[index - 1]) / prices[index - 1],
            "distance_from_mean": (prices[index] - mean) / mean,
            "spread_fraction": spread / mean if mean else 0.0,
        }
        model.train(features, outcome=prices[index + 1] > prices[index])
        trained += 1
    return trained


def a_trained_model(prices, **overrides) -> OnlineLogisticModel:
    settings = dict(
        learning_rate=0.05,
        l2_regularisation=0.001,
        feature_half_life_observations=5000.0,
        minimum_feature_observations=20,
        minimum_training_observations=100,
    )
    settings.update(overrides)
    model = OnlineLogisticModel(**settings)
    train_on_real_prices(model, prices)
    return model


def a_feature_vector(prices) -> dict:
    recent = prices[-20:]
    mean = sum(recent) / len(recent)
    spread = math.sqrt(sum((value - mean) ** 2 for value in recent) / len(recent))
    return {
        "return_since_previous": (prices[-1] - prices[-2]) / prices[-2],
        "distance_from_mean": (prices[-1] - mean) / mean,
        "spread_fraction": spread / mean if mean else 0.0,
    }


# -- the round trip ---------------------------------------------------------


def test_a_model_trained_on_real_trades_believes_exactly_the_same_after_a_restart(
    durable_tmp_path, real_trade_prices
):
    """The whole point: same prices in, same probability out, across a process.

    Compared on the probability *and* the log-odds score, because two models with
    different coefficients can agree on one probability by coincidence and cannot
    agree on the score for an arbitrary vector.
    """
    before = a_trained_model(real_trade_prices)
    assert before.observations >= 100, "the real series did not train the model enough to test"
    assert before.is_fitted, "both classes must appear in a real tick series"

    store = LearnedStateStore(durable_tmp_path)
    store.save(PART, COMPONENT, before.state(), before.learned_settings())

    after = OnlineLogisticModel(
        learning_rate=0.05,
        l2_regularisation=0.001,
        feature_half_life_observations=5000.0,
        minimum_feature_observations=20,
        minimum_training_observations=100,
    )
    restoration = store.restore(PART, COMPONENT, after.learned_settings())
    assert restoration.verdict == RESTORED
    after.restore_state(restoration.state)

    vector = a_feature_vector(real_trade_prices)
    assert after.believe(vector).probability == before.believe(vector).probability
    assert after.believe(vector).score == before.believe(vector).score
    assert after.observations == before.observations
    assert after.is_fitted == before.is_fitted
    assert after.weights == before.weights


def test_a_restored_model_keeps_learning_from_where_it_stopped(
    durable_tmp_path, real_trade_prices
):
    """Training after a restart must continue the model, not restart it.

    A model that restored its weights but reset its count would report itself
    unfitted forever, which is the same outage in a different place.
    """
    first = a_trained_model(real_trade_prices)
    store = LearnedStateStore(durable_tmp_path)
    store.save(PART, COMPONENT, first.state(), first.learned_settings())

    second = OnlineLogisticModel(**dict(
        learning_rate=0.05, l2_regularisation=0.001,
        feature_half_life_observations=5000.0,
        minimum_feature_observations=20, minimum_training_observations=100,
    ))
    second.restore_state(store.restore(PART, COMPONENT, second.learned_settings()).state)
    counted_before = second.observations

    further = train_on_real_prices(second, real_trade_prices[300:], limit=100)
    assert further > 0
    assert second.observations == counted_before + further


def test_a_calibrator_carries_its_bins_across_a_restart(durable_tmp_path):
    """The calibrator is the other learned thing a bot holds, and it round-trips.

    Its observations are stated-probability/outcome pairs, which no captured
    market data contains -- they are produced by a model this project has not yet
    run to a closed trade. So the pairs here come from the real price series by
    way of a model trained on it, which is the nearest thing to real that exists
    until a bot has closed a trade.
    """
    calibrator = ProbabilityCalibrator(
        bin_count=10, minimum_observations=50, half_life_observations=1000.0
    )
    for index in range(200):
        stated = 0.05 + (index % 19) / 20.0
        calibrator.observe_outcome(min(0.99, stated), outcome=index % 3 != 0)
    assert calibrator.is_fitted

    store = LearnedStateStore(durable_tmp_path)
    store.save("bull-conviction-calibrator", "calibration",
               calibrator.state(), calibrator.learned_settings())

    restored = ProbabilityCalibrator(
        bin_count=10, minimum_observations=50, half_life_observations=1000.0
    )
    restoration = store.restore(
        "bull-conviction-calibrator", "calibration", restored.learned_settings()
    )
    assert restoration.verdict == RESTORED
    restored.restore_state(restoration.state)

    assert restored.observations == calibrator.observations
    for stated in (0.1, 0.35, 0.62, 0.9):
        assert restored.calibrate(stated).value == calibrator.calibrate(stated).value


def test_a_calibrator_refuses_a_checkpoint_whose_bands_are_a_different_width():
    """Ten stored bands may not be read into a calibrator with five.

    Silently reindexing them would map every stored frequency to the wrong stated
    probability -- a calibration that inverts the model rather than correcting it.
    """
    stored = ProbabilityCalibrator(bin_count=10, minimum_observations=1, half_life_observations=100.0)
    stored.observe_outcome(0.9, outcome=True)
    narrower = ProbabilityCalibrator(bin_count=5, minimum_observations=1, half_life_observations=100.0)
    with pytest.raises(ValueError, match="different band"):
        narrower.restore_state(stored.state())


# -- the four cold starts ---------------------------------------------------


def test_a_first_run_starts_cold_and_says_which_kind_of_cold(durable_tmp_path):
    store = LearnedStateStore(durable_tmp_path)
    restoration = store.restore(PART, COMPONENT, {"anything": 1})
    assert restoration.verdict == STARTED_COLD_NO_CHECKPOINT
    assert not restoration.was_restored
    assert restoration.saved_at_ns is None


def test_a_changed_half_life_discards_the_model_rather_than_reinterpreting_it(
    durable_tmp_path, real_trade_prices
):
    """The refusal that matters most.

    Every stored moment was decayed under one half-life. Restoring it into a model
    running another means every feature is standardised by a rule that no longer
    applies, while the model still reports itself trained -- worse than starting
    cold, because it looks fine.
    """
    trained = a_trained_model(real_trade_prices)
    store = LearnedStateStore(durable_tmp_path)
    store.save(PART, COMPONENT, trained.state(), trained.learned_settings())

    retuned = OnlineLogisticModel(
        learning_rate=0.05, l2_regularisation=0.001,
        feature_half_life_observations=1000.0,   # was 5000
        minimum_feature_observations=20, minimum_training_observations=100,
    )
    restoration = store.restore(PART, COMPONENT, retuned.learned_settings())
    assert restoration.verdict == STARTED_COLD_SETTINGS_CHANGED
    assert restoration.state is None
    assert "feature_half_life_observations" in restoration.detail
    assert restoration.saved_at_ns is not None, "the discarded checkpoint's age is still a fact"


def test_a_changed_learning_rate_does_not_discard_the_model(
    durable_tmp_path, real_trade_prices
):
    """Tuning the step size is ordinary and must not cost a day of learning.

    The learning rate governs the next step, not the meaning of the ones already
    taken, which is exactly why `learned_settings` leaves it out.
    """
    trained = a_trained_model(real_trade_prices)
    store = LearnedStateStore(durable_tmp_path)
    store.save(PART, COMPONENT, trained.state(), trained.learned_settings())

    retuned = OnlineLogisticModel(
        learning_rate=0.2,                        # was 0.05
        l2_regularisation=0.01,                   # was 0.001
        feature_half_life_observations=5000.0,
        minimum_feature_observations=20, minimum_training_observations=100,
    )
    restoration = store.restore(PART, COMPONENT, retuned.learned_settings())
    assert restoration.verdict == RESTORED
    retuned.restore_state(restoration.state)
    assert retuned.observations == trained.observations


def test_a_checkpoint_from_an_incompatible_version_is_refused(durable_tmp_path):
    store = LearnedStateStore(durable_tmp_path)
    path = store.path_for(PART, COMPONENT)
    path.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION + 1,
        "part_id": PART, "component": COMPONENT,
        "saved_at_ns": 1, "settings": {}, "state": {"bias": 0.0},
    }), encoding="utf-8")
    restoration = store.restore(PART, COMPONENT, {})
    assert restoration.verdict == STARTED_COLD_SCHEMA_CHANGED
    assert restoration.state is None


def test_a_truncated_checkpoint_is_refused_rather_than_crashing_the_part(durable_tmp_path):
    """A half-written file must cost the learning, never the process.

    `save` writes atomically so this should not arise from this code -- it arises
    from a full disk, a filesystem repair, or an operator with an editor.
    """
    store = LearnedStateStore(durable_tmp_path)
    store.path_for(PART, COMPONENT).write_text('{"schema_version": 1, "sta', encoding="utf-8")
    restoration = store.restore(PART, COMPONENT, {})
    assert restoration.verdict == STARTED_COLD_UNREADABLE
    assert restoration.state is None


# -- the write itself -------------------------------------------------------


def test_a_checkpoint_is_replaced_atomically_and_leaves_nothing_behind(
    durable_tmp_path, real_trade_prices
):
    """No partial file may survive a write, and the previous one stays readable.

    Checked by counting what is in the directory: a temp file left behind would
    accumulate one per checkpoint on a part that writes every 25 observations.
    """
    store = LearnedStateStore(durable_tmp_path)
    trained = a_trained_model(real_trade_prices)
    for _ in range(3):
        store.save(PART, COMPONENT, trained.state(), trained.learned_settings())

    files = sorted(path.name for path in durable_tmp_path.iterdir())
    assert files == [f"{PART}.{COMPONENT}.json"], f"stray files left behind: {files}"
    document = json.loads(store.path_for(PART, COMPONENT).read_text(encoding="utf-8"))
    assert document["schema_version"] == SCHEMA_VERSION
    assert document["part_id"] == PART
    assert document["state"]["observations"] == trained.observations


def test_the_store_refuses_a_memory_backed_directory():
    """/tmp is tmpfs on this box; a model checkpointed there is lost on reboot."""
    from runtime.storage_facts import VolatileStorageRefused

    with pytest.raises(VolatileStorageRefused):
        LearnedStateStore(pathlib.Path("/tmp"))


# -- when to write ----------------------------------------------------------


def test_the_first_checkpoint_is_written_before_anything_has_been_learned():
    """A document saying "0 outcomes, as of now" is what makes the board honest.

    Without it, a bot that has trained on nothing and a bot nobody started are the
    same absent file (Rule 8).
    """
    schedule = CheckpointSchedule(every_observations=25)
    assert schedule.is_due(0)
    schedule.record_written(0)
    assert not schedule.is_due(24)
    assert schedule.is_due(25)


def test_a_checkpoint_interval_below_one_observation_is_refused():
    with pytest.raises(ValueError, match="every training step"):
        CheckpointSchedule(every_observations=0)


def test_settings_are_compared_by_value_not_by_how_they_were_written():
    """5000 from TOML and 5000.0 from JSON are the same number.

    Reporting that as a change would discard a model on every single restart --
    the exact failure this module exists to end, arriving through the fix.
    """
    assert compare_settings({"half_life": 5000}, {"half_life": 5000.0}) == []
    assert compare_settings({"half_life": 5000}, {"half_life": 1000}) != []
    assert compare_settings({}, {"minimum": 20}) != []
