"""Do the features the bull conviction model trains on predict anything?

`bull-conviction-model` reports a mean absolute error of **0.494** over 57,765
labelled observations, against a base rate of 48.5% positives -- which is what
always predicting the base rate scores. Its largest standardised weight is 0.096,
so its predictions barely leave that rate.

That has two very different causes and the model cannot tell them apart from the
inside: the features may carry no signal, or the online setup may be unable to
find it. Both of the operator's own settings notes say the same thing --
*"what would settle it is the model's out-of-sample error against a sweep, which
needs closed trades that do not exist yet"* (`bull_learning_rate`) and
*"to be replaced by a value chosen on held-out data once there is held-out data"*
(`bull_l2_regularisation`). There is data now.

This builds the (features, outcome) pairs from what actually happened:

- **candidates** from `trade-lifecycle-recorder`'s journal -- the real
  `entry-candidate` messages the detectors published, with their evidence;
- **outcomes** from this project's own captured tape: the symbol's price at the
  moment of detection and again one horizon later, and whether it moved the way
  the candidate expected.

Then it fits a logistic regression on the earlier 70% and scores the later 30%,
so the test set is strictly after the training set and no outcome leaks backwards.

Run: .venv/bin/python measurements/2026-09-16-does-the-conviction-model-have-signal/measure_signal_in_the_conviction_features.py
"""

from __future__ import annotations

import bisect
import collections
import json
import math
import pathlib
import sys

import numpy

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from runtime.tape import read_payload, read_tape_index  # noqa: E402

BASE = pathlib.Path.home() / ".local/share/ajit-segment-bots"
TAPE = BASE / "tape/upstox"
JOURNAL = BASE / "journal.trade-lifecycle-recorder.sqlite"
HORIZONS = (300.0, 900.0, 1800.0, 3600.0)
TRAIN_FRACTION = 0.7


def candidates():
    """Every entry-candidate the journal holds, oldest first."""
    rows = []
    with JOURNAL.open(errors="ignore") as lines:
        for line in lines:
            if '"entry-candidate"' not in line:
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue
            if record.get("kind") == "entry-candidate":
                rows.append(record["payload"])
    rows.sort(key=lambda row: row["detected_at_ns"])
    return rows


def price_series(symbol: str):
    """(times, prices) for one symbol across every captured day, oldest first."""
    directory = TAPE / symbol
    if not directory.is_dir():
        return (), ()
    times, prices = [], []
    for index_path in sorted(directory.glob("*.index")):
        if index_path.name.count(".") != 1:   # skip .book/.candle/.greeks streams
            continue
        blob_path = index_path.parent / index_path.name.replace(".index", ".blob")
        for record in read_tape_index(index_path):
            try:
                payload = json.loads(read_payload(blob_path, record))
            except Exception:
                continue
            price = payload.get("last_traded_price")
            if price is None or float(price) <= 0:
                continue
            times.append(int(record[0]))
            prices.append(float(price))
    return times, prices


def price_at(times, prices, at_ns):
    position = bisect.bisect_right(times, at_ns) - 1
    return prices[position] if position >= 0 else None


def features_of(row) -> dict:
    """Everything numeric the candidate states about itself."""
    features = {
        "signal_strength": float(row.get("signal_strength") or 0.0),
        "confidence": float((row.get("confidence") or {}).get("value") or 0.0),
        "horizon_seconds": float(row.get("horizon_seconds") or 0.0),
        "is_short": 1.0 if row.get("direction") == "short" else 0.0,
    }
    for name, value in (row.get("evidence") or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            features[f"evidence_{name}"] = float(value)
    return features


def fit_logistic(x, y, steps=400, learning_rate=0.5, l2=1e-4):
    """Plain batch gradient descent on standardised features."""
    weights = numpy.zeros(x.shape[1])
    bias = 0.0
    for _ in range(steps):
        predicted = 1.0 / (1.0 + numpy.exp(-(x @ weights + bias)))
        error = y - predicted
        weights += learning_rate * (x.T @ error / len(y) - l2 * weights)
        bias += learning_rate * error.mean()
    return weights, bias


def area_under_curve(scores, outcomes) -> float:
    order = numpy.argsort(scores)
    ranks = numpy.empty(len(scores), dtype=float)
    ranks[order] = numpy.arange(1, len(scores) + 1)
    positives = outcomes.sum()
    negatives = len(outcomes) - positives
    if positives == 0 or negatives == 0:
        return float("nan")
    return (ranks[outcomes == 1].sum() - positives * (positives + 1) / 2) / (positives * negatives)


def main() -> int:
    rows = candidates()
    print(f"entry-candidates in the journal: {len(rows):,}")
    if not rows:
        print("nothing to measure")
        return 0
    by_detector = collections.Counter(row["detector"] for row in rows)
    print(f"by detector: {dict(by_detector.most_common(4))}\n")

    series: dict[str, tuple] = {}
    for horizon in HORIZONS:
        examples, labels, times = [], [], []
        for row in rows:
            symbol = row["symbol"]
            if symbol not in series:
                series[symbol] = price_series(symbol)
            symbol_times, symbol_prices = series[symbol]
            if not symbol_times:
                continue
            at = row["detected_at_ns"]
            entry = price_at(symbol_times, symbol_prices, at)
            later = price_at(symbol_times, symbol_prices, at + int(horizon * 1e9))
            if entry is None or later is None or entry <= 0:
                continue
            if symbol_times[-1] < at + int(horizon * 1e9):
                continue          # the horizon has not finished yet
            move = (later - entry) / entry
            wanted_up = row.get("direction") != "short"
            labels.append(1 if (move > 0) == wanted_up else 0)
            examples.append(features_of(row))
            times.append(at)

        if len(labels) < 200:
            print(f"horizon {horizon:>6.0f}s: {len(labels)} usable examples -- too few to split")
            continue

        # Measured twice: with the candidate's direction, and without it. Nearly
        # every candidate is a short, so a model given `is_short` can score well
        # by learning "shorts won" -- a base rate, not a discrimination. Dropping
        # it asks whether anything else in the evidence separates the outcomes.
        for dropped in ((), ("is_short",)):
            run_one(examples, labels, times, horizon, dropped)
        continue

    return 0


def run_one(examples, labels, times, horizon, dropped) -> None:
        names = sorted(
            {name for example in examples for name in example} - set(dropped)
        )
        x = numpy.array([[example.get(name, 0.0) for name in names] for example in examples])
        y = numpy.array(labels, dtype=float)
        order = numpy.argsort(times)
        x, y = x[order], y[order]

        cut = int(len(y) * TRAIN_FRACTION)
        mean, deviation = x[:cut].mean(0), x[:cut].std(0)
        deviation[deviation == 0] = 1.0
        x = (x - mean) / deviation

        weights, bias = fit_logistic(x[:cut], y[:cut])
        held_out = 1.0 / (1.0 + numpy.exp(-(x[cut:] @ weights + bias)))
        base_rate = y[:cut].mean()

        model_error = numpy.abs(y[cut:] - held_out).mean()
        base_error = numpy.abs(y[cut:] - base_rate).mean()
        accuracy = ((held_out > 0.5).astype(float) == y[cut:]).mean()
        auc = area_under_curve(held_out, y[cut:].astype(int))

        label = "all features" if not dropped else f"without {', '.join(dropped)}"
        print(f"horizon {horizon:>6.0f}s [{label}]: {len(y):,} examples "
              f"({cut:,} train / {len(y)-cut:,} held out), base rate {base_rate:.3f}")
        print(f"    held-out mean |error|   model {model_error:.4f}   "
              f"base rate alone {base_error:.4f}")
        print(f"    held-out accuracy {accuracy:.3f}   AUC {auc:.3f}   "
              f"({'no better than chance' if abs(auc - 0.5) < 0.02 else 'separates'})")
        strongest = sorted(zip(names, weights), key=lambda pair: -abs(pair[1]))[:4]
        print("    strongest: " + ", ".join(f"{n} {w:+.3f}" for n, w in strongest))
        print()


if __name__ == "__main__":
    raise SystemExit(main())
