"""Kronos, the weights on this machine, and real candles off the tape.

`test_prediction_block` proves what the forecaster does with a model it was
handed. This proves the model is actually here and actually runs: the upstream
package imports, the published weights load, and a window of candles this
machine recorded comes back as a set of continuations that differ from each
other -- which is the whole reason the part samples several.

**Real candles, never a fixture (RL-063).** The window is taken from the candle
tape, so a run that fails because the venue changed its kline payload fails as
that rather than as this code being wrong.

Skipped, not failed, when the weights are absent: MODEL_NOT_LOADED is a state
this part is designed to report honestly, and a box without the artefact is that
state rather than a broken test.
"""

from __future__ import annotations

import datetime
import pathlib

import pytest

from parts.prediction.kronos_forecaster import CHAMPION, KronosForecaster, LoadedModel
from runtime.forecast_types import AVAILABLE, WINDOW_TOO_SHORT, Candle, KlineWindow
from runtime.kronos_runtime import KronosArtefact, KronosNotInstalled, build_kronos_predictor
from runtime.settings_reader import load_settings_document, settings_directory
from runtime.tape import read_payload, read_tape_index
from runtime.venues.adapter_registry import load_venue_adapter

TAPE_ROOT = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape"
VENUE = "binance-usdm"
# Enough index records to reach the window length. A candle stream restates the
# open bar on every update, so records and closed candles are nothing like the
# same count: measured on this tape, about 170 records per closed minute.


def runtime_settings() -> dict:
    document = load_settings_document(settings_directory() / "runtime.toml", scope="runtime")
    return document.entries


def artefact_from_settings() -> KronosArtefact:
    entries = runtime_settings()
    root = pathlib.Path(str(entries["kronos_model_root"].value)).expanduser()
    return KronosArtefact(
        source_root=pathlib.Path(str(entries["kronos_source_root"].value)).expanduser(),
        tokenizer_path=root / str(entries["kronos_tokenizer_name"].value),
        model_path=root / str(entries["kronos_model_name"].value),
        maximum_context=int(entries["kronos_maximum_context"].value),
        sampling_temperature=float(entries["kronos_sampling_temperature"].value),
        top_p=float(entries["kronos_top_p"].value),
        device=str(entries["kronos_device"].value),
    )


def candles_off_the_tape(length: int, records_read: int = 60_000) -> tuple:
    """The most recent closed candles of whichever symbol has most of today."""
    day = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    venue_root = TAPE_ROOT / VENUE
    if not venue_root.is_dir():
        return ()
    sized = [
        (path.stat().st_size, path.parent.name)
        for path in venue_root.glob(f"*/{day}.candle.index")
        if path.stat().st_size > 0
    ]
    if not sized:
        return ()
    _, symbol = max(sized)
    adapter = load_venue_adapter(VENUE)
    index_path = venue_root / symbol / f"{day}.candle.index"
    blob_path = venue_root / symbol / f"{day}.candle.blob"
    by_open: dict[int, Candle] = {}
    for record in list(read_tape_index(index_path))[-records_read:]:
        for candle in adapter.read_candles(read_payload(blob_path, record)):
            if not candle.is_closed:
                continue
            by_open[candle.open_time_ns] = Candle(
                open_time_ns=candle.open_time_ns,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                quote_volume=candle.quote_volume,
                trades=candle.trades or 0,
                is_closed=True,
            )
    ordered = [by_open[key] for key in sorted(by_open)]
    return tuple(ordered[-length:]), symbol


@pytest.fixture(scope="module")
def loaded_predictor():
    try:
        return build_kronos_predictor(artefact_from_settings())
    except KronosNotInstalled as absent:
        pytest.skip(f"Kronos is not installed on this machine: {absent}")


@pytest.fixture(scope="module")
def window_off_the_tape():
    minimum = int(runtime_settings()["kronos_minimum_window_candles"].value)
    taken = candles_off_the_tape(minimum)
    if not taken:
        pytest.skip("no candle tape for today yet")
    candles, symbol = taken
    if len(candles) < minimum:
        pytest.skip(
            f"{symbol} has {len(candles)} closed candle(s) on today's tape, and this "
            f"machine's own setting asks for {minimum}"
        )
    return KlineWindow(
        venue_id=VENUE, symbol=symbol, interval="1m", candles=tuple(candles),
        length_requested=len(candles), gaps=(), built_at_ns=candles[-1].open_time_ns,
    )


@pytest.mark.slow
def test_the_weights_on_this_machine_produce_differing_continuations(
    loaded_predictor, window_off_the_tape
):
    """Several paths, and they must not be the same path: one draw presented as a
    forecast is a sample wearing the authority of an estimate."""
    paths = loaded_predictor(window_off_the_tape.candles, 5, 4)
    assert len(paths) == 4
    assert all(len(path) == 5 for path in paths)
    assert len({round(path[-1], 10) for path in paths}) > 1


@pytest.mark.slow
def test_the_part_turns_those_continuations_into_a_forecast(
    loaded_predictor, window_off_the_tape
):
    minimum = int(runtime_settings()["kronos_minimum_window_candles"].value)
    forecaster = KronosForecaster(
        forecast_steps=5, sample_paths=4, interval_quantile=0.8, interval_seconds=60.0,
    )
    forecaster.load_model(
        CHAMPION,
        LoadedModel(
            name="Kronos-under-test", context_length=minimum, predict=loaded_predictor,
            device="cpu", finetuned_on=None,
        ),
    )
    forecast = forecaster.forecast(window_off_the_tape)
    assert forecast.state == AVAILABLE
    assert forecast.lower_return <= forecast.expected_return <= forecast.upper_return
    assert forecaster.standing.forecasts_produced == 1


@pytest.mark.slow
def test_a_window_shorter_than_the_model_asks_for_is_refused_not_padded(
    loaded_predictor, window_off_the_tape
):
    """Padding would feed the model a series it never saw."""
    forecaster = KronosForecaster(
        forecast_steps=5, sample_paths=4, interval_quantile=0.8, interval_seconds=60.0,
    )
    forecaster.load_model(
        CHAMPION,
        LoadedModel(
            name="Kronos-under-test", context_length=len(window_off_the_tape.candles) + 1,
            predict=loaded_predictor, device="cpu", finetuned_on=None,
        ),
    )
    assert forecaster.forecast(window_off_the_tape).state == WINDOW_TOO_SHORT
