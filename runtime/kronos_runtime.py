"""Turning the Kronos artefact on this machine into something a part can call.

Off-diagram substrate (RL-069): `kronos-forecaster` is the part, and this is the
loader it is handed. The split is what lets the part say MODEL_NOT_LOADED
honestly on a machine with no weights, and it is why nothing here decides
anything -- it loads, it predicts, and it reports what it could not do.

**What is installed, and where each piece comes from.** The model code is the
upstream repository (`shiyu-coder/Kronos`, MIT, AAAI 2026), cloned to this
machine at a pinned commit rather than vendored into this repository: it is
third-party code that is re-fetchable, and a copy inside this repo would drift
from upstream silently. The weights are the published model zoo on Hugging Face
(`NeoQuasar/Kronos-*`), downloaded to this machine for the same reason the tape
lives outside the repo. Both paths are settings, so a box without them produces
the refusal rather than an exception.

**Sampling is a batch, not a loop.** Kronos is autoregressive and one sampled
path is a draw wearing the authority of an estimate, so the part asks for
several. `predict` averages its own `sample_count` internally and returns one
mean path, so N paths means N series in one `predict_batch` call. Measured on
this box, 2026-08-26, 64 candles and 5 steps: eight paths in 4.46 s batched
against 6.44 s looped on Kronos-small, and 0.87 s batched on Kronos-mini.

**The device is whatever the box has.** This one has no accelerator, so it is a
CPU, and the cost above is the CPU cost. Nothing here asks the governor for a
slot: the part does that, because the control path is the part's business (T-2).
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Column names the upstream predictor requires on the frame it is given.
PRICE_COLUMNS = ("open", "high", "low", "close")
VOLUME_COLUMN = "volume"
AMOUNT_COLUMN = "amount"


class KronosNotInstalled(RuntimeError):
    """The model code or its weights are not on this machine.

    Raised by the builder and turned into a refusal by the caller, never into a
    forecast: a zero forecast reads as "no move expected", which is a confident
    claim from the one component that could not run (Rule 8).
    """


@dataclass(frozen=True)
class KronosArtefact:
    """Where the pieces are, and how the model is to be sampled."""

    source_root: Path
    tokenizer_path: Path
    model_path: Path
    maximum_context: int
    sampling_temperature: float
    top_p: float
    device: str


def _import_kronos(source_root: Path):
    """Import the upstream package from where it was cloned.

    Added to `sys.path` rather than installed, because upstream ships no package
    metadata -- it is a repository with a `model/` directory, and pretending
    otherwise by copying it here would make the pinned commit a lie.
    """
    if not (source_root / "model" / "kronos.py").exists():
        raise KronosNotInstalled(
            f"no Kronos source at {source_root}: expected the upstream repository "
            f"(shiyu-coder/Kronos) cloned there, with its model/ package"
        )
    root = str(source_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from model import Kronos, KronosPredictor, KronosTokenizer
    except Exception as failure:  # noqa: BLE001 -- the reason is the message
        raise KronosNotInstalled(
            f"the Kronos source at {source_root} could not be imported: "
            f"{type(failure).__name__}: {failure}"
        ) from failure
    return Kronos, KronosTokenizer, KronosPredictor


def build_kronos_predictor(artefact: KronosArtefact):
    """Load the weights and return `predict(candles, steps, paths) -> list[list[float]]`.

    Raises KronosNotInstalled when anything is missing. Every other failure is
    the caller's to see: a model that loaded and then produced nothing is a
    different fault from one that was never there.
    """
    Kronos, KronosTokenizer, KronosPredictor = _import_kronos(artefact.source_root)
    for path, what in ((artefact.tokenizer_path, "tokenizer"), (artefact.model_path, "model")):
        if not (path / "config.json").exists():
            raise KronosNotInstalled(f"no Kronos {what} weights at {path}")

    tokenizer = KronosTokenizer.from_pretrained(str(artefact.tokenizer_path))
    model = Kronos.from_pretrained(str(artefact.model_path))
    predictor = KronosPredictor(
        model, tokenizer, device=artefact.device, max_context=artefact.maximum_context
    )

    import pandas

    def predict(candles, steps: int, paths: int) -> list:
        """`paths` independent continuations of this window, as closing prices.

        The same window is handed to the batch `paths` times rather than looped:
        upstream averages `sample_count` internally, so asking for eight samples
        gives one mean path and not eight paths.
        """
        if not candles or steps < 1 or paths < 1:
            return []
        frame = pandas.DataFrame(
            {
                "open": [candle.open for candle in candles],
                "high": [candle.high for candle in candles],
                "low": [candle.low for candle in candles],
                "close": [candle.close for candle in candles],
                VOLUME_COLUMN: [candle.volume for candle in candles],
                AMOUNT_COLUMN: [candle.quote_volume for candle in candles],
            }
        )
        observed_at = pandas.Series(
            [pandas.Timestamp(candle.open_time_ns, unit="ns") for candle in candles]
        )
        # The spacing the venue actually used, taken from the window rather than
        # from a setting: a window of one-minute candles and a window of
        # five-minute candles are different series and the model is told which.
        spacing = (
            candles[-1].open_time_ns - candles[-2].open_time_ns
            if len(candles) > 1
            else 60 * 1_000_000_000
        )
        forecast_at = pandas.Series(
            [
                pandas.Timestamp(candles[-1].open_time_ns + spacing * (step + 1), unit="ns")
                for step in range(steps)
            ]
        )
        predictions = predictor.predict_batch(
            [frame] * paths,
            [observed_at] * paths,
            [forecast_at] * paths,
            pred_len=steps,
            T=artefact.sampling_temperature,
            top_p=artefact.top_p,
            sample_count=1,
            verbose=False,
        )
        return [list(prediction["close"]) for prediction in predictions]

    return predict


def describe_installation(artefact: KronosArtefact) -> dict:
    """What is on this machine, for a probe that has to say so without loading it."""
    return {
        "source_present": (artefact.source_root / "model" / "kronos.py").exists(),
        "tokenizer_present": (artefact.tokenizer_path / "config.json").exists(),
        "model_present": (artefact.model_path / "config.json").exists(),
        "device": artefact.device,
        "checked_at_ns": time.time_ns(),
    }
