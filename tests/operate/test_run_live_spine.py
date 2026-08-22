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
    # Two cycles, and both are the system working. The learning loop: the labeller
    # scores the detectors, the model trains on its labels, and what the model
    # decides eventually feeds the labeller again. The money loop: the account
    # keeper reads the fills the simulator produces and publishes the balance the
    # sizer sizes against, so the money that goes out is what comes back.
    INSIDE_A_FEEDBACK_CYCLE = {
        "signal-outcome-labeller",
        "bull-conviction-model",
        "bull-conviction-calibrator",
        "capital-settings-validator",
        "paper-account-keeper",
    }

    for part_id, inputs in consumes.items():
        if part_id in INSIDE_A_FEEDBACK_CYCLE:
            continue
        for data_type in inputs:
            for producer in produces.get(data_type, ()):
                if producer in position and producer != part_id:
                    assert position[producer] < position[part_id], (
                        f"{part_id} is started before {producer}, which produces the "
                        f"{data_type} it reads"
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
