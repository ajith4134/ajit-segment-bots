"""The plan must equal the blueprint, and must not be able to invent a wire.

The interesting test here is the first one: the wiring plan and the contract
checker derive the same set of edges from the same file by different code, and any
difference is a defect in one of them rather than a matter of opinion. That is the
only check that can catch a plan which is internally consistent and wrong.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys

import pytest

from runtime.wiring_plan import (
    INBOX_DIRECTORY_MODE,
    SUN_PATH_USABLE_BYTES,
    InboxAddressTooLong,
    PeerBlocksWired,
    create_inbox_root,
    derive_edges_from_wiring,
    derive_wiring,
    inbox_address,
    inbox_root,
    load_blueprint,
)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "dashboard"))
from render_blueprint import derive_edges, load_feature_registry  # noqa: E402


@pytest.fixture
def short_runtime_root():
    """A scratch root short enough for sun_path.

    pytest's tmp_path is 50-odd bytes of test name, which alone eats half the
    kernel's limit -- so a test using it would fail on address length rather than
    on what it meant to check. This is the same filesystem the real bus binds on.
    """
    root = pathlib.Path(os.environ["XDG_RUNTIME_DIR"]) / "wiring-plan-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def blueprint():
    return load_blueprint()


@pytest.fixture(scope="module")
def wiring(blueprint):
    return derive_wiring(blueprint)


def test_the_plan_derives_exactly_the_blueprints_edges(wiring):
    """Two independent derivations of the same set. A difference is a bug, not a view."""
    from_the_plan = derive_edges_from_wiring(wiring)
    from_the_checker = set(derive_edges(load_feature_registry()))
    assert from_the_plan == from_the_checker, (
        f"the wiring plan and the contract checker disagree: "
        f"{len(from_the_plan - from_the_checker)} wires only the plan would create, "
        f"{len(from_the_checker - from_the_plan)} the checker expects and the plan omits"
    )


def test_every_part_in_the_blueprint_is_wired(wiring, blueprint):
    assert set(wiring) == {feature["id"] for feature in blueprint["features"]}


def test_a_part_binds_one_inbox_per_consumed_type(wiring, blueprint):
    for feature in blueprint["features"]:
        part = wiring[feature["id"]]
        assert set(part.inboxes) == set(feature["consumes"])
        assert set(part.outbound) == set(feature["produces"])


def test_no_part_ever_receives_its_own_message(wiring, blueprint):
    """Fifteen parts consume a type they also produce; none may be wired to itself."""
    self_producing = [
        feature["id"]
        for feature in blueprint["features"]
        if set(feature["consumes"]) & set(feature["produces"])
    ]
    assert self_producing, "the blueprint used to contain parts that consume what they produce"
    for part_id in self_producing:
        part = wiring[part_id]
        for data_type, addresses in part.outbound.items():
            assert part.inboxes.get(data_type) not in addresses, (
                f"{part_id} would send '{data_type}' to its own inbox"
            )


def test_a_producer_reaches_every_consumer_of_the_type(wiring, blueprint):
    """The fan-out is the blueprint's, not a subset somebody trimmed."""
    market_data_consumers = [
        feature["id"] for feature in blueprint["features"] if "market-data" in feature["consumes"]
    ]
    producer = next(
        feature["id"] for feature in blueprint["features"] if "market-data" in feature["produces"]
    )
    reached = {address.name.split(".", 1)[0] for address in wiring[producer].outbound["market-data"]}
    assert reached == set(market_data_consumers) - {producer}


def test_health_reaches_the_eleven_readers_from_every_part(wiring, blueprint):
    readers = [
        feature["id"] for feature in blueprint["features"] if "part-health" in feature["consumes"]
    ]
    for part in wiring.values():
        expected = len(readers) - (1 if part.part_id in readers else 0)
        assert part.consumer_count("part-health") == expected


def test_peer_blocks_are_refused_where_the_wire_would_be_made(blueprint):
    """R-03 asserted at the plan, not only where the diagram is drawn."""
    bull = next(f for f in blueprint["features"] if f["category"] == "bull-bot" and f["produces"])
    bear = next(f for f in blueprint["features"] if f["category"] == "bear-bot")
    tampered = {
        **blueprint,
        "features": [
            {**f, "consumes": sorted(set(f["consumes"]) | {bull["produces"][0]})}
            if f["id"] == bear["id"]
            else f
            for f in blueprint["features"]
        ],
    }
    with pytest.raises(PeerBlocksWired) as refusal:
        derive_wiring(tampered)
    assert bull["id"] in str(refusal.value)
    assert bear["id"] in str(refusal.value)


def test_an_address_too_long_for_the_kernel_is_refused_by_name():
    over_by = 1
    part_id = "p" * (SUN_PATH_USABLE_BYTES + over_by)
    with pytest.raises(InboxAddressTooLong) as refusal:
        inbox_address(part_id, "market-data")
    assert part_id in str(refusal.value)
    assert "market-data" in str(refusal.value)


def test_every_address_the_blueprint_needs_fits_today(wiring):
    """The headroom is small enough that the suite has to hold the line on it."""
    longest = max(
        (len(str(address).encode()), part.part_id, data_type)
        for part in wiring.values()
        for data_type, address in part.inboxes.items()
    )
    assert longest[0] <= SUN_PATH_USABLE_BYTES, (
        f"'{longest[1]}' consuming '{longest[2]}' needs {longest[0]} bytes of a "
        f"{SUN_PATH_USABLE_BYTES}-byte limit"
    )


def test_the_inbox_root_is_private(short_runtime_root):
    """The 0700 mode is what makes a pickled payload admissible at all."""
    root = create_inbox_root(short_runtime_root)
    assert root.exists()
    assert root.stat().st_mode & 0o777 == INBOX_DIRECTORY_MODE


def test_the_bus_refuses_to_run_without_a_per_user_runtime_directory(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    with pytest.raises(RuntimeError) as refusal:
        inbox_root()
    assert "deserialised" in str(refusal.value)


def test_a_caller_supplied_root_is_used_verbatim(short_runtime_root, blueprint):
    wiring = derive_wiring(blueprint, runtime_directory=short_runtime_root)
    some_part = next(iter(wiring.values()))
    address = next(iter(some_part.inboxes.values()))
    assert str(address).startswith(str(short_runtime_root))
