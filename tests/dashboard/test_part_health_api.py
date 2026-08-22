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

SUBJECT_PART_ID = "kline-window-builder"
SUBJECT_CONSUMES = ("market-data",)
SUBJECT_PRODUCES = ("kline-window", "part-health")


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
    scratch_dir = PROJECT / "rl070_scratch_part_health_api"
    scratch_dir.mkdir(exist_ok=True)
    module_name = "rl070_scratch_ph_kline_window_builder"
    source_path = scratch_dir / f"{module_name}.py"
    test_path = scratch_dir / f"test_{module_name}.py"
    test_path.write_text("def test_scratch_placeholder():\n    assert True\n")
    source_path.write_text(
        "from runtime.part_declaration import PartDeclaration\n"
        "\n"
        "PART_DECLARATION = PartDeclaration(\n"
        f"    part_id={SUBJECT_PART_ID!r},\n"
        f"    consumes={SUBJECT_CONSUMES!r},\n"
        f"    produces={SUBJECT_PRODUCES!r},\n"
        '    resource_class="bandwidth-bound",\n'
        '    rate_risk="changes-the-answer",\n'
        '    skipped_tick_effect="corrupts",\n'
        ")\n"
    )
    try:
        yield source_path
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
    part_health_api = _import_part_health_api()
    payload = part_health_api.build_board_payload("live")

    subject = next(p for p in payload["parts"] if p["id"] == SUBJECT_PART_ID)
    assert subject["is_complete"] is True
    assert subject["rung"] == "TESTED"

    block = next(b for b in payload["blocks"] if b["id"] == subject["block"])
    # Green is not vacuous: the block has other, unbuilt siblings, so it must
    # stay red even though this one part just turned green.
    assert block["n_parts"] > 1
    assert block["is_complete"] is False


def test_removing_the_scratch_files_turns_the_part_dot_red_again(scratch_part_files):
    part_health_api = _import_part_health_api()
    payload = part_health_api.build_board_payload("live")
    subject = next(p for p in payload["parts"] if p["id"] == SUBJECT_PART_ID)
    assert subject["is_complete"] is True

    scratch_part_files.unlink()
    (scratch_part_files.parent / f"test_{scratch_part_files.stem}.py").unlink()

    payload = part_health_api.build_board_payload("live")
    subject = next(p for p in payload["parts"] if p["id"] == SUBJECT_PART_ID)
    assert subject["is_complete"] is False
    assert subject["rung"] == "DECLARED"


def test_live_and_snapshot_payloads_declare_their_own_mode():
    part_health_api = _import_part_health_api()
    assert part_health_api.build_board_payload("live")["mode"] == "live"
    assert part_health_api.build_board_payload("snapshot")["mode"] == "snapshot"
