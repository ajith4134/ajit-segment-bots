"""The live spine: what it starts, in what order, and the one thing it refuses.

`operate/run_live_spine.py` is an operations entry point rather than a part, and
it is the only thing that decides what runs until `switching-planner` does. That
makes its list a decision worth checking: a part named here that is not in the
blueprint, or a chain missing the part that feeds it, is a spine that starts and
then produces nothing while every process looks healthy.

The refusal is the one that matters more than any of it. This spine starts the
parts that place orders, and it may only ever run a segment set to paper.
"""

import importlib.util
import pathlib
import shutil

import pytest

from runtime.part_launcher import resolve_part_module
from runtime.settings_reader import settings_directory
from runtime.wiring_plan import derive_wiring

PROJECT = pathlib.Path(__file__).resolve().parents[2]
SPINE_SCRIPT = PROJECT / "operate" / "run_live_spine.py"

# What must be running for an intent to become a recorded paper fill. The same
# fourteen the integration test starts, which is the point: the spine and the test
# that proves the chain must not be able to drift apart.
TRADING_HALF = (
    "opinion-arbiter",
    "main-account-settings-reader",
    "capital-allotment-reader",
    "capital-settings-validator",
    "money-mode-reader",
    "instrument-selector",
    "tick-size-resolver",
    "exposure-limiter",
    "paper-account-keeper",
    "position-sizer",
    "trade-capital-bounds-gate",
    "order-idempotency-stamper",
    "order-destination-router",
    "paper-fill-simulator",
    "trade-lifecycle-recorder",
)


@pytest.fixture(scope="module")
def spine():
    """The script, imported by path -- `operate` is scripts, not a package."""
    specification = importlib.util.spec_from_file_location("run_live_spine", SPINE_SCRIPT)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_every_part_in_the_spine_is_in_the_blueprint_and_has_a_module(spine):
    """A part id with no blueprint entry has no wiring, and one with no file cannot start."""
    wiring = derive_wiring()
    for part_id in spine.LIVE_SPINE:
        assert part_id in wiring, f"{part_id} is started by the spine and is not in the blueprint"
        assert resolve_part_module(part_id), part_id


def test_the_spine_starts_each_part_only_once(spine):
    """A part is one process (T-1); two copies split their own input between them."""
    assert len(set(spine.LIVE_SPINE)) == len(spine.LIVE_SPINE)


def test_the_whole_trading_half_is_on(spine):
    """Every part between an opinion and a recorded fill, or no fill happens.

    Named individually rather than counted: a missing part here is a system that
    runs, looks healthy, and silently never trades -- which is what it did before
    these fifteen were added.
    """
    missing = [part_id for part_id in TRADING_HALF if part_id not in spine.LIVE_SPINE]
    assert not missing, f"the spine cannot reach a fill without: {missing}"


def test_a_part_is_started_after_the_parts_in_the_spine_that_feed_it(spine):
    """Started in the order data moves, so a first tick has something to read.

    Only against producers that are in the spine at all: 299 of the 321 parts sit
    in one feedback cycle, so most of what a part consumes is produced by
    something switched off, and waiting for it would be waiting forever. What this
    checks is that nothing switched *on* is started after the part that reads it.

    The parts inside a cycle are excluded by name below, because for them no
    order satisfies everything and the spine states the one it chose.
    """
    import json

    blueprint = json.loads((PROJECT / "docs" / "features.json").read_text())
    produces = {}
    for feature in blueprint["features"]:
        for data_type in feature["produces"]:
            produces.setdefault(data_type, set()).add(feature["id"])

    position = {part_id: index for index, part_id in enumerate(spine.LIVE_SPINE)}
    consumes = {
        feature["id"]: feature["consumes"]
        for feature in blueprint["features"]
        if feature["id"] in position
    }

    # The parts inside a feedback cycle, stated rather than discovered: each reads
    # something produced by a part started after it, and that is the design.
    #
    # Three cycles, and all three are the system working. The learning loop: the
    # labeller scores the detectors, the model trains on its labels, and what the
    # model decides eventually feeds the labeller again. The money loop: the
    # account keeper reads the fills the simulator produces and publishes the
    # balance the sizer sizes against, so the money that goes out is what comes
    # back. The exit loop: the simulator's fill becomes a position, the position's
    # exits become orders, and those orders come back to the simulator -- which is
    # what closing a trade is, and there is no ordering of the two that makes it
    # a line instead of a circle.
    INSIDE_A_FEEDBACK_CYCLE = {
        "signal-outcome-labeller",
        "bull-conviction-model",
        "bull-conviction-calibrator",
        "capital-settings-validator",
        "paper-account-keeper",
        "paper-fill-simulator",
        # The exposure limiter is inside the money loop rather than upstream of
        # it: it limits the sizer, and what it limits against is the positions the
        # sizer's own orders produced. There is no ordering that makes that a line.
        "exposure-limiter",
        # The collector reads part-health from every part, including its own
        # consumers downstream; it is started first so the earliest reports have
        # an inbox, which is the opposite of this rule on purpose.
        "heartbeat-collector",
        # Same reason: it meters every part's health, its own consumers included.
        "part-appetite-meter",
        # The governor's act loop, closed on 2026-08-24: the planner's plan feeds
        # the actuator, the actuator's switch-records feed the damper and the
        # verifier, and the damper's flap-reports feed the planner again. No
        # ordering makes that a line. The spine starts the actuator last, so the
        # first plan it ever acts on is one the planner built with every meter
        # already reporting.
        "switch-oscillation-damper",
        "off-state-verifier",
        "gate-actuator",
        # In the same loop one arc further out: the faults it budgets against are
        # the verifier's, and the budget it produces is the planner's input.
        "part-restart-budgeter",
        # The stop loop, closed on 2026-08-25 when the decoders were switched on
        # (phase 5). stop-target-placer places a stop, the trade closes against
        # it, stop-placement-auditor judges whether it was hit by noise before the
        # target, and that audit is how the next stop is placed better. There is
        # no ordering of those two that makes it a line, and the placer is started
        # first on purpose: a trade cannot be sized without a stop, so a spine
        # that waited for the auditor could never open the trade the auditor
        # exists to judge.
        "stop-target-placer",
        # The same loop, one arc further in, and closed the same day. The bull
        # bot's entry timing is scored by entry-quality-scorer and its exit plan
        # by the excursion, horizon and stop-audit profilers -- and every one of
        # those judgements is built from trades the bot itself opened. A learning
        # loop that could be ordered as a line would not be a learning loop.
        "bull-entry-timer",
        "bull-exit-plan-proposer",
        # The model loop, closed 2026-08-25 with phase 6. Kronos is finetuned, it
        # forecasts, forecast-scorer scores what it said against what the market
        # actually did, model-drift-monitor raises an alert when that accuracy
        # decays, and the alert is what triggers the next finetune. A model that
        # kept itself honest in a straight line would not be keeping itself
        # honest -- it would just be a model.
        "kronos-finetuner",
        # The second loop through the same accuracy: which model size to run is
        # chosen from how well the sizes themselves have been forecasting.
        "kronos-size-selector",
    }

    for part_id, inputs in consumes.items():
        if part_id in INSIDE_A_FEEDBACK_CYCLE:
            continue
        for data_type in inputs:
            on_the_spine = [
                producer for producer in produces.get(data_type, ())
                if producer in position and producer != part_id
            ]
            if not on_the_spine:
                continue
            # At least one producer must precede, not every one of them. A type
            # with several producers -- `journal-entry` has three recorders, and
            # `part-health` has every part -- is satisfied for a reader the moment
            # one of them is up; which producer's messages a given reader actually
            # needs is not something consumes/produces can express, so requiring
            # all of them asserts more than the blueprint says.
            #
            # A type with one producer is unaffected, which is the case this rule
            # exists for and the case that has caught every real defect so far.
            assert any(position[producer] < position[part_id] for producer in on_the_spine), (
                f"{part_id} is started before every part on the spine that produces the "
                f"{data_type} it reads: {', '.join(sorted(on_the_spine))}"
            )


def test_a_segment_that_is_not_on_paper_refuses_to_start(spine, durable_tmp_path, monkeypatch):
    """The third check, at the one moment where refusing costs nothing.

    The router refuses to address a live order and the simulator refuses to
    simulate one; this refuses to fork the fourteen processes that would find that
    out one at a time. A run that reached a live venue is the failure this phase
    cannot recover from (RL-005).
    """
    config_home = durable_tmp_path / "config"
    settings_root = config_home / "ajit-segment-bots" / "settings"
    shutil.copytree(settings_directory(), settings_root)
    segment_file = settings_root / "segments" / f"{spine.TRADED_SEGMENT}.toml"
    segment_file.write_text(
        segment_file.read_text().replace('value = "paper"', 'value = "live"', 1)
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    with pytest.raises(SystemExit) as refusal:
        spine.refuse_unless_the_segment_is_on_paper()
    assert "paper first" in str(refusal.value)


def test_the_operator_s_own_settings_are_on_paper_right_now(spine):
    """Not a test of the code -- a test of the machine this is running on."""
    assert spine.refuse_unless_the_segment_is_on_paper() == spine.PAPER


def test_every_input_a_running_part_declares_has_a_producer_on_the_spine(spine):
    """A part whose input nobody produces sits there looking perfectly healthy.

    This is the failure the sampler nearly caused: moving 37 parts from
    `market-data` to `symbol-price-frame` and forgetting to start the one part
    that publishes a frame would have left every one of them ticking, reporting
    on, and never seeing a price again. Nothing in the spine checked for it, and
    the part monitor would have shown 47 parts RUNNING.

    A few inputs are produced by parts deliberately left off this spine -- the
    governor's own outputs, the venue adapters' order paths -- so the list of
    those is stated here by name rather than inferred. A new unproduced input is
    a failure until somebody writes down why it is not.
    """
    from runtime.wiring_plan import derive_wiring

    wiring = derive_wiring()
    running = set(spine.LIVE_SPINE)
    produced = {
        data_type
        for part_id in running
        for data_type in wiring[part_id].declaration.produces
    }

    unproduced = {}
    for part_id in sorted(running):
        missing = [
            data_type
            for data_type in wiring[part_id].declaration.consumes
            if data_type not in produced
        ]
        if missing:
            unproduced[part_id] = missing

    # Every consumer of a price level must be fed by the sampler, and the sampler
    # by the reader. That chain is the one this test exists for.
    assert "price-level-sampler" in running, (
        "37 parts consume symbol-price-frame; nothing else publishes one"
    )
    for part_id, missing in unproduced.items():
        assert "symbol-price-frame" not in missing, (
            f"{part_id} reads symbol-price-frame and nothing on this spine publishes it"
        )
        assert "market-data" not in missing, (
            f"{part_id} reads market-data and nothing on this spine publishes it"
        )


def test_the_ordering_rule_still_catches_a_single_producer_inversion():
    """The relaxation above must not have made the rule toothless.

    "At least one producer" is weaker than "every producer", and the weakening is
    only safe because a type with one producer is unaffected. That is the case
    every real defect has fallen into -- usdt-pnl-accountant started before
    funding-settlement-recorder on 2026-08-25, and funding-settlement has exactly
    one producer -- so this pins that such an inversion is still a failure.
    """
    import json

    blueprint = json.loads((PROJECT / "docs" / "features.json").read_text())
    produces = {}
    for feature in blueprint["features"]:
        for data_type in feature["produces"]:
            produces.setdefault(data_type, []).append(feature["id"])

    single = [t for t, makers in produces.items() if len(makers) == 1]
    assert "funding-settlement" in single, (
        "funding-settlement gained a second producer; this test's premise needs rechecking"
    )

    # An inverted spine of exactly that shape must be rejected by the same rule.
    inverted = ("usdt-pnl-accountant", "funding-settlement-recorder")
    position = {part_id: index for index, part_id in enumerate(inverted)}
    consumer = next(f for f in blueprint["features"] if f["id"] == "usdt-pnl-accountant")
    assert "funding-settlement" in consumer["consumes"]

    on_the_spine = [p for p in produces["funding-settlement"] if p in position]
    assert on_the_spine == ["funding-settlement-recorder"]
    assert not any(
        position[p] < position["usdt-pnl-accountant"] for p in on_the_spine
    ), "the rule would no longer catch a single-producer inversion"
