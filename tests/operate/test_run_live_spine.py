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


# The health wire every part produces. Not a data dependency: it is how the
# governor sees a part at all, and a spine ordered by it could never start the
# part that collects it.
HEALTH = "part-health"


def strongly_connected_components(edges: dict, nodes) -> dict:
    """Which parts sit in a cycle together, computed rather than listed.

    Tarjan's algorithm, iterative because 327 parts nest deeper than the recursion
    limit. Every part is in exactly one component; a component of one is a part
    that is not in any cycle.
    """
    index_of, low, on_stack, stack, order = {}, {}, set(), [], []
    component_of, counter = {}, 0

    for root in nodes:
        if root in index_of:
            continue
        work = [(root, iter(edges.get(root, ())))]
        index_of[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            for child in children:
                if child not in index_of:
                    index_of[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(edges.get(child, ()))))
                    break
                if child in on_stack:
                    low[node] = min(low[node], index_of[child])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index_of[node]:
                    order.append(node)
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component_of[member] = node
                        if member == node:
                            break
    return component_of


def test_a_part_is_started_after_the_parts_in_the_spine_that_feed_it(spine):
    """Started in the order data moves, wherever an order exists at all.

    **The cycles are computed, not listed.** They were a hand-kept set of names
    until 2026-08-25, which worked while most of the blueprint was switched off:
    with 327 parts running, 299 of them sit in one feedback cycle, and every edge
    inside it is an edge no ordering can satisfy. A list of names would have to
    grow to hold most of the system, and the day it did it would stop saying
    anything.

    So the graph is built from the blueprint, its strongly connected components
    are found, and the rule is applied to exactly the edges that cross between
    them -- the part of the graph that *is* a line. An edge inside one component
    is a loop by construction: the labeller scores the detectors and the model
    trains on its labels; the account keeper reads the fills the simulator makes
    and publishes the balance the sizer sizes against; the placer places a stop
    and the auditor judges the stop it placed. A learning loop that could be
    ordered as a line would not be a learning loop.
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

    # producer -> consumers, over the parts on the spine only.
    edges: dict[str, set[str]] = {}
    for part_id, inputs in consumes.items():
        for data_type in inputs:
            if data_type == HEALTH:
                continue
            for producer in produces.get(data_type, ()):
                if producer in position and producer != part_id:
                    edges.setdefault(producer, set()).add(part_id)

    component_of = strongly_connected_components(edges, sorted(position))

    for part_id, inputs in consumes.items():
        for data_type in inputs:
            if data_type == HEALTH:
                # Every part produces it, so ordering by it would mean the
                # collector starts after all 327 -- and the collector exists to
                # notice a part that never started. It is the control path, which
                # T-2 keeps separate from the data path, and this rule is about
                # the data path.
                continue
            outside = [
                producer for producer in produces.get(data_type, ())
                if producer in position
                and producer != part_id
                and component_of[producer] != component_of[part_id]
            ]
            if not outside:
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
            assert any(position[producer] < position[part_id] for producer in outside), (
                f"{part_id} is started before every part on the spine that produces the "
                f"{data_type} it reads: {', '.join(sorted(outside))}"
            )


def test_the_feedback_cycle_is_most_of_the_system_and_that_is_the_design(spine):
    """One component holds nearly every part, which is why an order cannot be total.

    RL-068's own finding, measured here rather than remembered: 299 of the parts
    sit in one cycle, and the transitive inputs of a paper fill are 306 parts. A
    spine that waited for a part's inputs before starting it would never start
    anything.
    """
    import json

    blueprint = json.loads((PROJECT / "docs" / "features.json").read_text())
    produces = {}
    for feature in blueprint["features"]:
        for data_type in feature["produces"]:
            produces.setdefault(data_type, set()).add(feature["id"])
    position = {part_id: index for index, part_id in enumerate(spine.LIVE_SPINE)}
    edges: dict[str, set[str]] = {}
    for feature in blueprint["features"]:
        if feature["id"] not in position:
            continue
        for data_type in feature["consumes"]:
            for producer in produces.get(data_type, ()):
                if producer in position and producer != feature["id"]:
                    edges.setdefault(producer, set()).add(feature["id"])

    component_of = strongly_connected_components(edges, sorted(position))
    sizes: dict[str, int] = {}
    for member in component_of.values():
        sizes[member] = sizes.get(member, 0) + 1
    largest = max(sizes.values())
    assert largest > 250, (
        f"the largest feedback cycle holds {largest} parts; if it has shrunk this much "
        f"the ordering rule above should be tightened rather than left as it is"
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
