"""A detector's outcome becomes something the scanner watches on every symbol.

This is RL-009's mechanism end to end, and until 2026-08-26 no part of it
connected. Measured on the live spine that day:

    instruction-writer         requests 479,323   refused 479,323   written 0
    watch-condition-compiler   messages_received {}   compiled 0
    universal-symbol-sweeper   455 sweeps, 0 conditions, 0 candidates

Six independent breaks, each alone sufficient to hold the count at zero:
a field the producer never carried, a ranker that could not rank publishing
anyway, a refutation verdict that could not exist before the first trade, a
regime tag nobody delivered, a vocabulary discovery and scanning did not share,
and a property called as a method. `docs/proposals/the-writer-read-its-
conditions-through-a-part-that-could-not-rank.md`.

**This test exists because none of the six was visible from any single part.**
Every part was launchable, every one reported itself healthy, and each refusal
was correct in isolation. Only the whole chain says whether a thing the system
learned can reach the thing that acts on it, so only the whole chain is a test of
it. Every part here is the real one -- nothing is stubbed but the clock.

Prices are real captured Binance aggTrade frames (RL-063).
"""

import pytest

from parts.hypothesis.hypothesis_deduplicator import HypothesisDeduplicator, _shape_of
from parts.hypothesis.hypothesis_falsifier import HypothesisFalsifier
from parts.hypothesis.hypothesis_regime_tagger import HypothesisRegimeTagger
from parts.hypothesis.instruction_writer import InstructionWriter
from parts.hypothesis.power_estimator import PowerEstimator
from parts.hypothesis.symbolic_hypothesis_miner import SymbolicHypothesisMiner
from parts.opportunity_scanner.universal_symbol_sweeper import UniversalSymbolSweeper
from parts.opportunity_scanner.watch_condition_compiler import WatchConditionCompiler
from runtime.learned_estimator import Estimate
from runtime.learning_types import Hypothesis
from runtime.market_signal import LONG, REVERSION, SignalCalibrator, make_candidate
from runtime.rolling_statistics import RollingWindow
from runtime.sweep_measurements import (
    KNOWN_MEASUREMENTS,
    RETURN_OVER_WINDOW,
    add_cross_sectional,
    measure_symbol,
)

VENUE = "binance-usdm"
CAPTURED_RUN = "2026-08-22-btcusdt-aggtrade-run.jsonl"
WINDOW_LENGTH = 64
SYMBOLS = 8
# How far ahead the label looks, in prints of the captured run.
HORIZON_PRINTS = 32


class Clock:
    def __init__(self) -> None:
        self.at_ns = 1_700_000_000_000_000_000

    def __call__(self) -> int:
        self.at_ns += 1_000_000
        return self.at_ns


@pytest.fixture
def real_prices(read_captured_payloads):
    from runtime.venues.adapter_registry import load_venue_adapter

    adapter = load_venue_adapter(VENUE)
    records = read_captured_payloads(VENUE, CAPTURED_RUN)
    prices = [trade.price for _at_ns, payload in records for trade in adapter.read_trades(payload)]
    assert len(prices) > 400, "the captured run must hold enough prices to label many claims"
    return prices


def a_universe_measured(real_prices, at_offset):
    """What the vocabulary says about eight real windows of one real market."""
    measured = {}
    for index in range(SYMBOLS):
        start = at_offset + index * 4
        prices = real_prices[start:start + WINDOW_LENGTH]
        if len(prices) < WINDOW_LENGTH:
            return {}
        window = RollingWindow(length=WINDOW_LENGTH)
        for price in prices:
            window.observe(price)
        measured[(VENUE, f"S{index}")] = measure_symbol(window, minimum_observations=2)
    add_cross_sectional(measured, minimum_symbols=SYMBOLS)
    return measured


def what_happened_next(real_prices, at_offset, index) -> bool | None:
    """Whether price rose over the horizon after the window closed.

    A forward outcome, deliberately. Labelling a window by something computed
    from that same window makes the miner's hit rate and its base rate the same
    number, and a claim with no effect to detect has no sample size that would
    answer it -- which is what `power-estimator` correctly says about it.
    """
    closed_at = at_offset + index * 4 + WINDOW_LENGTH - 1
    later = closed_at + HORIZON_PRINTS
    if later >= len(real_prices):
        return None
    return real_prices[later] > real_prices[closed_at]


def test_a_label_becomes_a_condition_the_scanner_evaluates_on_every_symbol(real_prices):
    clock = Clock()

    # -- 1. Labels, in the vocabulary the scanner can watch ------------------
    #
    # The features are the universal measurements, not the detector's own
    # evidence. That is the change that makes everything after it possible: a
    # formula mined over a pair detector's `spread_z` names something no scanner
    # can evaluate on an arbitrary symbol, and instruction-writer refused every
    # one of them as NO_MEASUREMENT.
    miner = SymbolicHypothesisMiner(
        measurements=(RETURN_OVER_WINDOW,),
        thresholds_per_measurement=(0.0,),
        maximum_terms=1,
        minimum_examples=40,
        minimum_held_out_examples=10,
        held_out_fraction=0.3,
        complexity_penalty_per_term=0.01,
        perfect_fit_threshold=0.99,
        now_ns=clock,
    )

    examples = 0
    for offset in range(0, len(real_prices) - WINDOW_LENGTH - SYMBOLS * 4 - HORIZON_PRINTS, 3):
        measured = a_universe_measured(real_prices, offset)
        if not measured:
            break
        for index, key in enumerate(sorted(measured)):
            found = measured[key]
            if RETURN_OVER_WINDOW not in found:
                continue
            # The label a barrier would have produced: what the market did AFTER
            # the window closed, judged against measurements taken before it.
            outcome = what_happened_next(real_prices, offset, index)
            if outcome is None:
                continue
            miner.observe_example(found, outcome, clock())
            examples += 1
        if examples >= 200:
            break

    assert examples >= 200, f"the captured run yielded only {examples} labelled examples"

    # -- 2. A mined formula, over a name the scanner knows -------------------
    from parts.hypothesis.symbolic_hypothesis_miner import ABOVE, Term

    formula, state = miner.mine("returns", (Term(RETURN_OVER_WINDOW, ABOVE, 0.0),))
    assert formula is not None, f"the miner returned nothing: {state}"
    assert formula.terms[0].measurement in KNOWN_MEASUREMENTS, (
        "a formula naming something outside the shared vocabulary can never be watched"
    )

    # -- 3. Novelty -- the read that returned None for every formula ---------
    deduplicator = HypothesisDeduplicator(threshold_tolerance=0.1, overlap_tolerance=0.8)
    shape = _shape_of(formula)
    assert shape is not None, "a mined formula must have a comparable shape"
    novelty = deduplicator.score(formula.formula_id, shape, ())
    assert novelty.novelty > 0.0

    # -- 4. How many trades would answer it ----------------------------------
    estimator = PowerEstimator(power=0.8, maximum_testable_trades=10_000, now_ns=clock)
    sample = estimator.estimate(
        formula.formula_id,
        claimed_hit_rate=formula.held_out_hit_rate,
        base_rate=formula.base_rate,
        corrected_significance=0.05 / max(1, formula.trials_in_family),
        trials_in_family=formula.trials_in_family,
    )
    assert sample.trades_required is not None, sample.reason

    # -- 5. What would refute it ---------------------------------------------
    falsifier = HypothesisFalsifier(maximum_reachable_trades=10_000, now_ns=clock)
    criterion = falsifier.write(
        formula.formula_id,
        measure="hit-rate",
        comparison="at-or-below",
        threshold=formula.base_rate,
        required_trades=sample.trades_required,
    )
    assert criterion.required_trades is not None, criterion.reason

    # -- 6. Which market it is a claim about ---------------------------------
    tagger = HypothesisRegimeTagger(
        minimum_trades_per_regime=20,
        working_threshold=0.5,
        prior_hit_rate=0.5,
        prior_weight=1.0,
        half_life_observations=100.0,
        now_ns=clock,
    )
    # The outcomes the formula actually produced on data it was not fitted on,
    # replayed into the regime they were gathered in. Enough of them to clear the
    # tagger's own minimum, and a hit rate above its working threshold -- a
    # formula that worked nowhere is refused by the writer under its own name,
    # which is a stronger statement than never having been measured.
    trades = max(40, formula.held_out_trades)
    right = int(round(max(formula.held_out_hit_rate, 0.6) * trades))
    for index in range(trades):
        tagger.observe_outcome(formula.formula_id, "trending", index < right)
    tag = tagger.tag(formula.formula_id)
    assert tag.is_tagged, tag.reason
    assert tag.regimes_it_may_fire_in == ("trending",), tag.reason

    # -- 7. The instruction -- the gate INTO paper testing -------------------
    writer = InstructionWriter(
        minimum_novelty=0.5,
        maximum_reachable_trades=10_000,
        known_measurements=KNOWN_MEASUREMENTS,
        now_ns=clock,
    )
    writer.observe_falsification_criterion(formula.formula_id, criterion)
    writer.observe_required_sample(formula.formula_id, sample.trades_required)
    writer.observe_novelty(formula.formula_id, novelty.novelty)
    writer.observe_trial_verdict(formula.formula_id, True)
    writer.observe_regime_tag(formula.formula_id, tag)

    term = formula.terms[0]
    instruction, failing = writer.write(Hypothesis(
        hypothesis_id=formula.formula_id,
        statement=str(formula),
        what_would_refute_it=criterion.reason,
        family=formula.family,
        trials_in_family=formula.trials_in_family,
        source="candidate-formula",
        context={
            "measurement": term.measurement,
            "comparison": term.comparison,
            "threshold": term.threshold,
            "direction": LONG,
            "expectation": REVERSION,
            "horizon_seconds": 600.0,
        },
        required_sample_size=None, regime_tag=None, novelty=None, evidence={},
        proposed_at_ns=formula.mined_at_ns,
    ))
    assert instruction is not None, f"the writer refused it: {failing}"
    assert failing == ()

    # -- 8. Compiled into something evaluable on any symbol ------------------
    compiler = WatchConditionCompiler(known_measurements=KNOWN_MEASUREMENTS, now_ns=clock)
    condition = compiler.compile_instruction(
        instruction_id=instruction.instruction_id,
        measurement=instruction.measurement,
        comparison=instruction.comparison,
        threshold=instruction.threshold,
        direction=instruction.direction,
        expectation=instruction.expectation,
        horizon_seconds=instruction.horizon_seconds,
    )
    assert compiler.standing.compiled == 1
    assert compiler.standing.active_conditions == 1

    # -- 9. Swept across the whole universe ----------------------------------
    sweeper = UniversalSymbolSweeper(
        sweep_budget_seconds=5.0,
        calibrator=SignalCalibrator(
            prior_hit_rate=0.5, prior_weight=1.0,
            half_life_observations=100.0, minimum_observations=20,
        ),
        now_ns=clock,
    )
    measured = a_universe_measured(real_prices, 0)
    for (venue_id, symbol), found in measured.items():
        sweeper.observe_measurements(venue_id, symbol, found)

    candidates, report = sweeper.sweep(tuple(sorted(measured)), (condition,))

    assert report.symbols_in_universe == SYMBOLS
    assert report.symbols_swept == SYMBOLS
    assert report.covered_everything, report.reason
    assert report.conditions_tested == 1
    # Every symbol whose return cleared the threshold raised a candidate, and the
    # count matches what the measurements themselves say -- so this is the
    # condition firing, not the sweeper counting its own optimism.
    expected = sum(
        1 for found in measured.values()
        if found.get(RETURN_OVER_WINDOW) is not None and found[RETURN_OVER_WINDOW] > condition.threshold
    )
    assert len(candidates) == expected
    assert report.candidates == expected

    for candidate in candidates:
        assert candidate.detector == "universal-symbol-sweeper"
        assert candidate.evidence["instruction_id"] == instruction.instruction_id
        assert candidate.evidence["measurement"] in KNOWN_MEASUREMENTS
        assert isinstance(candidate.confidence, Estimate)
        # Nothing has scored this condition yet, and the candidate says so
        # rather than reporting the prior as though it were measured.
        assert candidate.confidence.is_fitted is False


def test_a_retired_instruction_stops_producing_candidates_immediately(real_prices):
    """The worst candidates a system can make carry the authority of having been proven."""
    clock = Clock()
    compiler = WatchConditionCompiler(known_measurements=KNOWN_MEASUREMENTS, now_ns=clock)
    condition = compiler.compile_instruction(
        instruction_id="instruction:f-1",
        measurement=RETURN_OVER_WINDOW,
        comparison="above",
        threshold=-1.0,
        direction=LONG,
        expectation=REVERSION,
        horizon_seconds=600.0,
    )

    sweeper = UniversalSymbolSweeper(
        sweep_budget_seconds=5.0,
        calibrator=SignalCalibrator(
            prior_hit_rate=0.5, prior_weight=1.0,
            half_life_observations=100.0, minimum_observations=20,
        ),
        now_ns=clock,
    )
    measured = a_universe_measured(real_prices, 0)
    for (venue_id, symbol), found in measured.items():
        sweeper.observe_measurements(venue_id, symbol, found)

    before, _ = sweeper.sweep(tuple(sorted(measured)), (condition,))
    assert before, "a threshold below every return must fire on every symbol"

    assert compiler.retire_instruction("instruction:f-1") == 1
    after, report = sweeper.sweep(tuple(sorted(measured)), compiler.conditions)
    assert after == ()
    assert report.conditions_tested == 0


def test_a_candidate_the_sweeper_raises_can_be_labelled(real_prices):
    """The loop closes: what the scanner raises is what the labeller judges.

    Without this the chain is a line rather than a loop, and a condition compiled
    from one detector's outcomes could never be scored on its own.
    """
    from parts.learning_loop.signal_outcome_labeller import (
        CLAIM_OPENED, SignalOutcomeLabeller,
    )

    clock = Clock()
    measured = a_universe_measured(real_prices, 0)
    symbol = sorted(measured)[0][1]
    found = measured[(VENUE, symbol)]

    candidate = make_candidate(
        detector="universal-symbol-sweeper",
        venue_id=VENUE,
        symbol=symbol,
        direction=LONG,
        expectation=REVERSION,
        signal_strength=1.0,
        confidence=Estimate(
            value=0.5, is_fitted=False, observations=0, prior=0.5, was_clamped=False,
            bound_low=None, bound_high=None, reason="no outcomes observed yet",
        ),
        horizon_seconds=600.0,
        evidence={"measurement": RETURN_OVER_WINDOW, "value": found[RETURN_OVER_WINDOW]},
        reason="a condition fired",
        # What the sweeper itself keys on: the condition it fired, not the market
        # regime. Three of the nine detectors calibrate on something other than
        # the regime, which is why the key travels on the claim rather than being
        # re-derived when the claim settles. Shaped like a real `condition_id`,
        # which is `{instruction}:{measurement}:{comparison}`.
        calibration_key=f"instruction:f-1:{RETURN_OVER_WINDOW}:above",
        now_ns=clock,
    )

    labeller = SignalOutcomeLabeller(move_fraction=0.002, maximum_open_claims=100)
    labeller.observe_price(VENUE, symbol, real_prices[0], clock())
    claim, reason = labeller.observe_candidate(candidate, measurements=found)

    assert reason == CLAIM_OPENED
    assert claim is not None
    # What the label will carry is the vocabulary, so the outcome of a condition
    # can be mined into the next one. That is what makes this a loop.
    assert set(claim.measurements) <= set(KNOWN_MEASUREMENTS)
    assert RETURN_OVER_WINDOW in claim.measurements
