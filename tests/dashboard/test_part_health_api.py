"""dashboard/part_health_api.py's RL-070 dots on the /api/board payload.

The predicate itself (part_is_measured_complete / block_completion) is tested
against dashboard/build_part_monitor.py in test_build_part_monitor.py, which
also proves the wiring-check edge cases. This file tests the one thing that
is part_health_api's own responsibility: that build_board_payload() carries
the SAME verdict through to the payload the React board reads, computed by
calling dashboard/completion.py directly rather than by re-deriving it.

Lives outside tests/runtime/ for the same reason as the other dashboard
tests: this exercises standalone dashboard scripts, not a package member.
"""

import shutil
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent.parent
DASHBOARD_DIR = PROJECT / "dashboard"

def _an_unbuilt_part():
    """A part that is still DECLARED, chosen from the blueprint at run time.

    Pinned to a name, this test went stale the moment that part was actually
    built -- the scratch files stopped controlling its rung and the assertions
    inverted. Choosing at run time means it keeps testing the mechanism for as
    long as anything is unbuilt, and fails honestly once nothing is.
    """
    if str(DASHBOARD_DIR) not in sys.path:
        sys.path.insert(0, str(DASHBOARD_DIR))
    import part_health_api

    payload = part_health_api.build_board_payload("live")
    for block in payload["blocks"]:
        # DECLARED specifically, not merely incomplete: a part with a source
        # file but no test falls back to IMPLEMENTED when the scratch files go,
        # and this test asserts it falls all the way back to DECLARED.
        unbuilt = [
            part
            for part in payload["parts"]
            if part["block"] == block["id"] and part["rung"] == "DECLARED"
        ]
        # A block with more than one unbuilt part, so turning one green leaves
        # the block red -- which is the thing this file exists to check.
        if len(unbuilt) > 1:
            return unbuilt[0]["id"]
    raise AssertionError(
        "every part is built, so this test can no longer distinguish a green dot "
        "from a vacuous one; replace it with one that does"
    )


def _blueprint_declaration(part_id: str):
    """The part's real consumes and produces.

    Written into the scratch file rather than a fixed pair, so the wiring check
    sees a declaration that matches the blueprint. A mismatched one makes the
    part FAILING, which is a different verdict from the one this file tests.
    """
    from runtime.part_declaration import load_declaration_from_blueprint

    return load_declaration_from_blueprint(part_id)


def _import_part_health_api():
    if str(DASHBOARD_DIR) not in sys.path:
        sys.path.insert(0, str(DASHBOARD_DIR))
    import part_health_api

    return part_health_api


@pytest.fixture
def scratch_part_files():
    """A real source file (and a real test file) for SUBJECT_PART_ID, on disk
    where find_source_files() actually looks -- same fixture shape as
    test_build_part_monitor.py's, kept separate rather than shared because
    the two files exercise different modules and must not become coupled.
    """
    part_id = _an_unbuilt_part()
    declaration = _blueprint_declaration(part_id)
    scratch_dir = PROJECT / "rl070_scratch_part_health_api"
    scratch_dir.mkdir(exist_ok=True)
    module_name = f"rl070_scratch_ph_{part_id.replace('-', '_')}"
    source_path = scratch_dir / f"{module_name}.py"
    test_path = scratch_dir / f"test_{module_name}.py"
    test_path.write_text("def test_scratch_placeholder():\n    assert True\n")
    source_path.write_text(
        "from runtime.part_declaration import PartDeclaration\n"
        "\n"
        "PART_DECLARATION = PartDeclaration(\n"
        f"    part_id={part_id!r},\n"
        f"    consumes={declaration.consumes!r},\n"
        f"    produces={declaration.produces!r},\n"
        f"    resource_class={declaration.resource_class.value!r},\n"
        f"    rate_risk={declaration.rate_risk.value!r},\n"
        f"    skipped_tick_effect={declaration.skipped_tick_effect.value!r},\n"
        ")\n"
    )
    try:
        yield part_id, source_path
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def test_a_green_dot_means_a_part_that_really_is_built():
    # This asserted that every dot was red until 2026-08-22, when
    # venue-trade-stream-reader landed. A board that can never go green is not a
    # board, so what is pinned now is the direction the inference may run: a dot
    # is green only where a part reached the top rung, never the other way round.
    part_health_api = _import_part_health_api()
    payload = part_health_api.build_board_payload("live")
    complete = [part for part in payload["parts"] if part["is_complete"]]
    for part in complete:
        assert part["rung"] in ("TESTED", "RUNNING"), part
        assert part["dot_proof"]
    # A block is green only if every part in it is, so it cannot lead its parts.
    parts_by_block = {}
    for part in payload["parts"]:
        parts_by_block.setdefault(part["block"], []).append(part)
    for block in payload["blocks"]:
        if block["is_complete"]:
            assert all(part["is_complete"] for part in parts_by_block[block["id"]])


def test_every_part_dot_carries_its_proof():
    # Rule 8: a dot with no provenance is not a status.
    part_health_api = _import_part_health_api()
    payload = part_health_api.build_board_payload("live")
    for part in payload["parts"]:
        assert part["dot_proof"]
    for block in payload["blocks"]:
        assert block["dot_proof"]


def test_payload_reuses_completion_py_rather_than_reimplementing_it():
    # The constraint that matters most: one predicate, imported, not re-derived.
    part_health_api = _import_part_health_api()
    completion = sys.modules.get("completion") or __import__("completion")
    payload = part_health_api.build_board_payload("live")
    states = {s.part_id: s for s in part_health_api.measure_parts()}
    for part in payload["parts"]:
        expected = completion.part_is_measured_complete(states[part["id"]])
        assert part["is_complete"] == expected


def test_a_built_and_tested_part_turns_its_dot_green_while_its_block_stays_red(
    scratch_part_files,
):
    part_id, _ = scratch_part_files
    part_health_api = _import_part_health_api()
    payload = part_health_api.build_board_payload("live")

    subject = next(p for p in payload["parts"] if p["id"] == part_id)
    assert subject["is_complete"] is True
    assert subject["rung"] == "TESTED"

    block = next(b for b in payload["blocks"] if b["id"] == subject["block"])
    # Green is not vacuous: the block has other, unbuilt siblings, so it must
    # stay red even though this one part just turned green.
    assert block["n_parts"] > 1
    assert block["is_complete"] is False


def test_removing_the_scratch_files_turns_the_part_dot_red_again(scratch_part_files):
    part_id, source_path = scratch_part_files
    part_health_api = _import_part_health_api()
    payload = part_health_api.build_board_payload("live")
    subject = next(p for p in payload["parts"] if p["id"] == part_id)
    assert subject["is_complete"] is True

    source_path.unlink()
    (source_path.parent / f"test_{source_path.stem}.py").unlink()

    payload = part_health_api.build_board_payload("live")
    subject = next(p for p in payload["parts"] if p["id"] == part_id)
    assert subject["is_complete"] is False
    assert subject["rung"] == "DECLARED"


def test_live_and_snapshot_payloads_declare_their_own_mode():
    part_health_api = _import_part_health_api()
    assert part_health_api.build_board_payload("live")["mode"] == "live"
    assert part_health_api.build_board_payload("snapshot")["mode"] == "snapshot"
