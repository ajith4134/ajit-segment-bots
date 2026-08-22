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

import dataclasses
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent.parent
DASHBOARD_DIR = PROJECT / "dashboard"

def _import_part_health_api():
    if str(DASHBOARD_DIR) not in sys.path:
        sys.path.insert(0, str(DASHBOARD_DIR))
    import part_health_api

    return part_health_api


def _states_with_one_part_at(rung: str, proof: str):
    """The real measured states, with one part forced to a chosen rung.

    Until 2026-08-22 this file wrote scratch source files for a part that was
    still unbuilt, and read the payload back. The last of the 321 parts landed
    that day and there was no unbuilt part left to borrow -- and the mechanism
    being tested was never the scratch files. It is that the payload carries
    completion.py's verdict through unchanged, in both directions. Substituting
    one state proves that directly, and keeps a red dot reachable in a payload
    where every real part is green.
    """
    part_health_api = _import_part_health_api()
    states = list(part_health_api.measure_parts())
    subject = states[0]
    states[0] = dataclasses.replace(subject, rung=rung, proof=proof)
    return subject.part_id, states


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


def test_a_declared_part_paints_a_red_dot_and_takes_its_block_red(monkeypatch):
    # Red must stay reachable in this payload. Every real part is green now, so
    # the state is substituted rather than manufactured on disk -- what is under
    # test is the payload's inference, not the probe's, which
    # test_build_part_monitor.py covers against the real tree.
    part_health_api = _import_part_health_api()
    part_id, states = _states_with_one_part_at("DECLARED", "no source file on disk")
    monkeypatch.setattr(part_health_api, "measure_parts", lambda: states)

    payload = part_health_api.build_board_payload("live")
    subject = next(part for part in payload["parts"] if part["id"] == part_id)
    assert subject["is_complete"] is False
    assert subject["rung"] == "DECLARED"
    assert subject["dot_proof"]

    block = next(b for b in payload["blocks"] if b["id"] == subject["block"])
    assert block["is_complete"] is False


def test_a_tested_part_paints_a_green_dot(monkeypatch):
    # The complement, in the same payload: a colour observed only in one
    # direction is not proven.
    part_health_api = _import_part_health_api()
    part_id, states = _states_with_one_part_at("TESTED", "a source file plus a test")
    monkeypatch.setattr(part_health_api, "measure_parts", lambda: states)

    payload = part_health_api.build_board_payload("live")
    subject = next(part for part in payload["parts"] if part["id"] == part_id)
    assert subject["is_complete"] is True
    assert subject["rung"] == "TESTED"


def test_a_failing_part_is_never_green_however_far_it_climbed(monkeypatch):
    # The case that matters most: a part with a source file and a test whose
    # wiring disagrees with the blueprint has climbed the ladder and is still
    # wrong, and the payload must not reward the climbing.
    part_health_api = _import_part_health_api()
    part_id, states = _states_with_one_part_at(
        "FAILING", "its declaration disagrees with the blueprint"
    )
    monkeypatch.setattr(part_health_api, "measure_parts", lambda: states)

    payload = part_health_api.build_board_payload("live")
    subject = next(part for part in payload["parts"] if part["id"] == part_id)
    assert subject["is_complete"] is False


def test_live_and_snapshot_payloads_declare_their_own_mode():
    part_health_api = _import_part_health_api()
    assert part_health_api.build_board_payload("live")["mode"] == "live"
    assert part_health_api.build_board_payload("snapshot")["mode"] == "snapshot"
