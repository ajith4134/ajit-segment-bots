"""dashboard/build_part_monitor.py's RL-070 dots and RL-067 wiring check.

find_source_files() and the wiring check both scan the real project tree
(dashboard/build_part_monitor.py's PROJECT), not an isolated tmp_path -- a real
part will land there too, so a test proving a green dot is reachable has to put
a real file where the probe actually looks. scratch_part_files below does that,
under a directory this suite creates and removes itself.

The wiring check reads a part's PART_DECLARATION with `ast`, never by
importing the file (RL-070's correction: generating a dashboard must never run
part code), so these tests never touch sys.modules or sys.path for the
scratch files either -- there is nothing to import, so nothing to clean up
there.

Lives outside tests/runtime/ for the same reason as test_build_status_board.py:
this exercises a standalone dashboard script, not a package member.
"""

import shutil
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent.parent
DASHBOARD_DIR = PROJECT / "dashboard"

def _a_blueprint_part():
    """Any part in the blueprint. Which one does not matter to these tests.

    This asked for a part still at DECLARED until 2026-08-22, when the last of
    the 321 landed and there was no unbuilt part left to borrow. The mechanism
    under test was never really "an unbuilt part" -- it is the probe's rules:
    no source file means DECLARED, a source plus a test means TESTED, and a
    declaration that disagrees with the blueprint paints red. Those are exercised
    here by handing the probe the source list directly, which is deterministic and
    does not depend on anything being unfinished.
    """
    build_part_monitor = _import_build_part_monitor()
    return build_part_monitor.load_feature_registry().features[0]["id"]


def _blueprint_declaration(part_id: str):
    from runtime.part_declaration import load_declaration_from_blueprint

    return load_declaration_from_blueprint(part_id)


def _import_build_part_monitor():
    """Import the way running it directly would: its own directory on
    sys.path first, matching tests/dashboard's established convention."""
    if str(DASHBOARD_DIR) not in sys.path:
        sys.path.insert(0, str(DASHBOARD_DIR))
    import build_part_monitor

    return build_part_monitor


def _one_part_registry(part_id: str):
    """A registry holding exactly the part under test, so the wiring check
    compares that part and nothing else -- the 320 real ones are not the
    subject and their inclusion would make a failure ambiguous."""
    build_part_monitor = _import_build_part_monitor()
    return build_part_monitor.FeatureRegistry(features=[{"id": part_id}])


@pytest.fixture
def scratch_part_files():
    """A real source file (and a real test file) for a blueprint part, written
    under the project root -- outside tests/ so pytest's own collection never
    sees it, and outside build_part_monitor's EXCLUDED_DIRS so a probe handed
    the real tree would see it too. Removed whether the test passes or fails.
    """
    part_id = _a_blueprint_part()
    scratch_dir = PROJECT / "rl070_scratch_wiring_check"
    scratch_dir.mkdir(exist_ok=True)
    module_name = f"rl070_scratch_{part_id.replace('-', '_')}"
    source_path = scratch_dir / f"{module_name}.py"
    test_path = scratch_dir / f"test_{module_name}.py"
    test_path.write_text("def test_scratch_placeholder():\n    assert True\n")
    try:
        yield source_path, test_path, part_id
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def _scratch_sources(part_id: str, source_path: Path, test_path: Path | None):
    """The source list the probe is handed: the scratch file standing in for the
    part, named exactly as the part is so find_implementation_file matches it."""
    named_source = source_path.with_name(f"{part_id.replace('-', '_')}.py")
    source_path.rename(named_source)
    sources = [named_source]
    if test_path is not None:
        named_test = test_path.with_name(f"test_{part_id.replace('-', '_')}.py")
        test_path.rename(named_test)
        sources.append(named_test)
    return named_source, sources


def _write_declaration(
    source_path: Path, part_id: str, *, consumes: tuple[str, ...], produces: tuple[str, ...]
) -> None:
    """A literal PART_DECLARATION, exactly the shape read_declaration_from_source
    requires: every field a keyword, every value a literal (no enum-attribute
    access) -- consumes/produces come from the caller, resource_class/rate_risk/
    skipped_tick_effect are fixed to real vocabulary values for the subject part.
    """
    source_path.write_text(
        "from runtime.part_declaration import PartDeclaration\n"
        "\n"
        "PART_DECLARATION = PartDeclaration(\n"
        f"    part_id={part_id!r},\n"
        f"    consumes={consumes!r},\n"
        f"    produces={produces!r},\n"
        '    resource_class="bandwidth-bound",\n'
        '    rate_risk="changes-the-answer",\n'
        '    skipped_tick_effect="corrupts",\n'
        ")\n"
    )


def test_a_part_with_no_source_file_is_declared_and_red():
    build_part_monitor = _import_build_part_monitor()
    part_id = _a_blueprint_part()
    rung, proof = build_part_monitor.probe_part_rung(part_id, [])
    assert rung == build_part_monitor.DECLARED
    assert "no source file" in proof
    subject = build_part_monitor.PartState(
        part_id, "A", "role", "cat", rung, proof
    )
    assert build_part_monitor.part_is_measured_complete(subject) is False


def test_the_wiring_check_reports_exactly_the_parts_that_exist():
    # RL-070: it must report what it actually compared, never an agreement it
    # never checked. This was "nothing is built" until 2026-08-22, when
    # venue-trade-stream-reader landed and the board stopped being all-red --
    # which is the board working, not the test needing to be relaxed. What is
    # asserted now is the invariant that survives every part landing: every id
    # it claims to have checked has a real implementation file, and none of them
    # disagrees with the blueprint.
    build_part_monitor = _import_build_part_monitor()
    registry = build_part_monitor.load_feature_registry()
    sources = build_part_monitor.find_source_files()
    wiring = build_part_monitor.check_wiring_against_blueprint(registry, sources)
    assert wiring.unavailable is None
    assert wiring.mismatches == {}
    for part_id in wiring.checked_part_ids:
        assert build_part_monitor.find_implementation_file(part_id, sources) is not None


def test_a_source_file_and_a_matching_test_file_turn_the_dot_green(scratch_part_files):
    source_path, test_path, part_id = scratch_part_files
    declaration = _blueprint_declaration(part_id)
    _write_declaration(
        source_path, part_id, consumes=declaration.consumes, produces=declaration.produces
    )
    named_source, sources = _scratch_sources(part_id, source_path, test_path)

    build_part_monitor = _import_build_part_monitor()
    rung, proof = build_part_monitor.probe_part_rung(part_id, sources)
    wiring = build_part_monitor.check_wiring_against_blueprint(
        _one_part_registry(part_id), sources
    )

    assert rung == build_part_monitor.TESTED
    subject = build_part_monitor.PartState(part_id, "A", "role", "cat", rung, proof)
    assert build_part_monitor.part_is_measured_complete(subject) is True
    assert part_id in wiring.checked_part_ids
    assert part_id not in wiring.mismatches


def test_removing_the_scratch_files_turns_the_dot_red_again(scratch_part_files):
    # The complement of the test above, in one test: green is reachable AND
    # reversible -- a colour observed once and never un-observed is not proven.
    source_path, test_path, part_id = scratch_part_files
    declaration = _blueprint_declaration(part_id)
    _write_declaration(
        source_path, part_id, consumes=declaration.consumes, produces=declaration.produces
    )
    named_source, sources = _scratch_sources(part_id, source_path, test_path)

    build_part_monitor = _import_build_part_monitor()
    rung, proof = build_part_monitor.probe_part_rung(part_id, sources)
    assert build_part_monitor.part_is_measured_complete(
        build_part_monitor.PartState(part_id, "A", "role", "cat", rung, proof)
    ) is True

    for path in sources:
        path.unlink()

    rung, proof = build_part_monitor.probe_part_rung(part_id, [])
    wiring = build_part_monitor.check_wiring_against_blueprint(
        _one_part_registry(part_id), []
    )
    assert rung == build_part_monitor.DECLARED
    assert build_part_monitor.part_is_measured_complete(
        build_part_monitor.PartState(part_id, "A", "role", "cat", rung, proof)
    ) is False
    assert part_id not in wiring.checked_part_ids


def test_a_built_part_whose_wiring_disagrees_is_painted_red_naming_both_sides(scratch_part_files):
    source_path, test_path, part_id = scratch_part_files
    declaration = _blueprint_declaration(part_id)
    _write_declaration(
        source_path, part_id, consumes=("wrong-data",), produces=declaration.produces
    )
    named_source, sources = _scratch_sources(part_id, source_path, test_path)

    build_part_monitor = _import_build_part_monitor()
    wiring = build_part_monitor.check_wiring_against_blueprint(
        _one_part_registry(part_id), sources
    )

    assert part_id in wiring.mismatches
    proof = wiring.mismatches[part_id]
    # Both sides named, not just "mismatch found".
    assert declaration.consumes[0] in proof  # what the blueprint declares
    assert "wrong-data" in proof  # what the built module declares
    failing = build_part_monitor.PartState(
        part_id, "A", "role", "cat", build_part_monitor.FAILING, proof
    )
    assert build_part_monitor.part_is_measured_complete(failing) is False


def test_a_built_part_with_no_part_declaration_is_unverifiable_not_green(scratch_part_files):
    source_path, test_path, part_id = scratch_part_files
    source_path.write_text("# a part module that never states its own wiring\n")
    named_source, sources = _scratch_sources(part_id, source_path, test_path)

    build_part_monitor = _import_build_part_monitor()
    wiring = build_part_monitor.check_wiring_against_blueprint(
        _one_part_registry(part_id), sources
    )

    assert part_id in wiring.mismatches


def test_a_built_part_with_a_non_literal_declaration_is_red_not_green(scratch_part_files):
    # RL-070's correction: the mistake a future author will make by accident --
    # reaching for the enum member the way ordinary Python writes one, instead
    # of the literal string the static reader requires -- must paint the part
    # red, not silently pass because the value "looks right" to a human.
    source_path, test_path, part_id = scratch_part_files
    declaration = _blueprint_declaration(part_id)
    source_path.write_text(
        "from runtime.part_declaration import PartDeclaration, ResourceClass\n"
        "\n"
        "PART_DECLARATION = PartDeclaration(\n"
        f"    part_id={part_id!r},\n"
        f"    consumes={declaration.consumes!r},\n"
        f"    produces={declaration.produces!r},\n"
        "    resource_class=ResourceClass.BANDWIDTH_BOUND,\n"  # not a literal
        '    rate_risk="changes-the-answer",\n'
        '    skipped_tick_effect="corrupts",\n'
        ")\n"
    )
    named_source, sources = _scratch_sources(part_id, source_path, test_path)

    build_part_monitor = _import_build_part_monitor()
    wiring = build_part_monitor.check_wiring_against_blueprint(
        _one_part_registry(part_id), sources
    )

    assert part_id in wiring.mismatches
    assert "literal" in wiring.mismatches[part_id]


def test_a_block_is_green_only_when_every_part_in_it_is_green():
    build_part_monitor = _import_build_part_monitor()
    PartState = build_part_monitor.PartState

    all_tested = [
        PartState("a", "A", "role", "cat", build_part_monitor.TESTED, "a plus test"),
        PartState("b", "B", "role", "cat", build_part_monitor.TESTED, "b plus test"),
    ]
    is_complete, proof = build_part_monitor.block_completion(all_tested)
    assert is_complete is True
    assert "2 of 2" in proof

    one_declared = [
        PartState("a", "A", "role", "cat", build_part_monitor.TESTED, "a plus test"),
        PartState("b", "B", "role", "cat", build_part_monitor.DECLARED, "no source file"),
    ]
    is_complete, proof = build_part_monitor.block_completion(one_declared)
    assert is_complete is False
    assert "B" in proof


def test_a_block_with_no_parts_is_red_not_vacuously_green():
    build_part_monitor = _import_build_part_monitor()
    is_complete, proof = build_part_monitor.block_completion([])
    assert is_complete is False
    assert proof


# ---- RUNNING: read from the collector's table, never inferred ------------------

def _write_table(path: Path, collected_at_ns: int, beats):
    import json

    path.write_text(json.dumps({
        "schema_version": 1, "part_id": "heartbeat-collector",
        "collected_at_ns": collected_at_ns,
        "reporting": sum(1 for b in beats if b["state"] == "reporting"),
        "late": 0, "silent": sum(1 for b in beats if b["state"] == "silent"),
        "never_reported": 0, "heartbeats": beats,
    }))


def test_a_fresh_table_makes_a_reporting_part_alive(tmp_path):
    monitor = _import_build_part_monitor()
    table = tmp_path / "heartbeat-table.json"
    _write_table(table, 1_000_000_000_000, [
        {"part_id": "a", "state": "reporting", "age_seconds": 0.4, "rate_ratio": 1.0,
         "staleness_seconds": 0.3, "input_loss": []},
        {"part_id": "b", "state": "silent", "age_seconds": 40.0, "rate_ratio": 1.0,
         "staleness_seconds": 40.0, "input_loss": []},
    ])
    alive, note = monitor.probe_live_heartbeats(
        table, now_ns=1_000_000_000_000 + 2_000_000_000, silent_after_seconds=10.0
    )
    assert set(alive) == {"a"}
    assert "1 reporting" in note and "1 silent" in note


def test_a_stale_table_proves_nothing_about_any_part(tmp_path):
    """The collector's last word is not current either (Rule 8)."""
    monitor = _import_build_part_monitor()
    table = tmp_path / "heartbeat-table.json"
    _write_table(table, 1_000_000_000_000, [
        {"part_id": "a", "state": "reporting", "age_seconds": 0.4, "rate_ratio": 1.0,
         "staleness_seconds": 0.3, "input_loss": []},
    ])
    alive, note = monitor.probe_live_heartbeats(
        table, now_ns=1_000_000_000_000 + 60_000_000_000, silent_after_seconds=10.0
    )
    assert alive == {}
    assert "collector itself has stopped" in note


def test_no_table_is_its_own_reading(tmp_path):
    monitor = _import_build_part_monitor()
    alive, note = monitor.probe_live_heartbeats(tmp_path / "none.json", now_ns=1, silent_after_seconds=10.0)
    assert alive == {} and "has not written one" in note


def test_running_is_reached_only_from_tested(monkeypatch, tmp_path):
    """A heartbeat from an untested part does not climb; the proof says why."""
    monitor = _import_build_part_monitor()
    registry = monitor.load_feature_registry()
    tested_id = next(
        f["id"] for f in registry.features
        if monitor.probe_part_rung(f["id"], monitor.find_source_files())[0] == monitor.TESTED
    )
    monkeypatch.setattr(
        monitor, "probe_live_heartbeats",
        lambda *a, **k: ({tested_id: "heartbeat-table.json: reported 0.5s ago"}, "one alive"),
    )
    states = monitor.measure_parts()
    by_id = {s.part_id: s for s in states}
    assert by_id[tested_id].rung == monitor.RUNNING
    assert "reported 0.5s ago" in by_id[tested_id].proof
    assert monitor.part_is_measured_complete(by_id[tested_id])
