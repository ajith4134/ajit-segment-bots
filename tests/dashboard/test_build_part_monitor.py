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

def _a_declared_part():
    """A part still at DECLARED, chosen from the blueprint at run time.

    This was pinned to a part name until 2026-08-22, when that part was built
    and every assertion here inverted -- the scratch files stopped controlling
    its rung because it had a real source file of its own. Choosing at run time
    keeps the mechanism under test for as long as anything is unbuilt, and
    fails honestly once nothing is.
    """
    build_part_monitor = _import_build_part_monitor()
    for state in build_part_monitor.measure_parts():
        if state.rung == build_part_monitor.DECLARED:
            return state.part_id
    raise AssertionError(
        "every part is built, so there is no DECLARED part to exercise the scratch-file "
        "mechanism against; replace this test with one that does"
    )


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


@pytest.fixture
def scratch_part_files():
    """A real source file (and a real test file) for a still-DECLARED part, written
    under the project root -- outside tests/ so pytest's own collection never
    sees it, and outside build_part_monitor's EXCLUDED_DIRS so its own
    find_source_files() does. Removed whether the test passes or fails.
    """
    part_id = _a_declared_part()
    scratch_dir = PROJECT / "rl070_scratch_wiring_check"
    scratch_dir.mkdir(exist_ok=True)
    module_name = f"rl070_scratch_{part_id.replace('-', '_')}"
    source_path = scratch_dir / f"{module_name}.py"
    test_path = scratch_dir / f"test_{module_name}.py"
    test_path.write_text("def test_scratch_placeholder():\n    assert True\n")
    try:
        yield source_path, module_name, part_id
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


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
    part_id = _a_declared_part()
    states = build_part_monitor.measure_parts()
    subject = next(s for s in states if s.part_id == part_id)
    assert subject.rung == build_part_monitor.DECLARED
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
    source_path, module_name, part_id = scratch_part_files
    declaration = _blueprint_declaration(part_id)
    _write_declaration(
        source_path, part_id, consumes=declaration.consumes, produces=declaration.produces
    )

    build_part_monitor = _import_build_part_monitor()
    states, wiring = build_part_monitor._measure_build_state()
    subject = next(s for s in states if s.part_id == part_id)

    assert subject.rung == build_part_monitor.TESTED
    assert build_part_monitor.part_is_measured_complete(subject) is True
    assert part_id in wiring.checked_part_ids
    assert part_id not in wiring.mismatches


def test_removing_the_scratch_files_turns_the_dot_red_again(scratch_part_files):
    # The complement of the test above, in one test: green is reachable AND
    # reversible -- a colour observed once and never un-observed is not proven.
    source_path, module_name, part_id = scratch_part_files
    declaration = _blueprint_declaration(part_id)
    _write_declaration(
        source_path, part_id, consumes=declaration.consumes, produces=declaration.produces
    )

    build_part_monitor = _import_build_part_monitor()
    states, _wiring = build_part_monitor._measure_build_state()
    subject = next(s for s in states if s.part_id == part_id)
    assert build_part_monitor.part_is_measured_complete(subject) is True

    source_path.unlink()
    (source_path.parent / f"test_{module_name}.py").unlink()

    states, wiring = build_part_monitor._measure_build_state()
    subject = next(s for s in states if s.part_id == part_id)
    assert subject.rung == build_part_monitor.DECLARED
    assert build_part_monitor.part_is_measured_complete(subject) is False
    # Scoped to the subject rather than to the project total: parts that really
    # are built stay checked, and a test that demanded an empty list would have
    # to be edited every time one landed.
    assert part_id not in wiring.checked_part_ids


def test_a_built_part_whose_wiring_disagrees_is_painted_red_naming_both_sides(scratch_part_files):
    source_path, module_name, part_id = scratch_part_files
    declaration = _blueprint_declaration(part_id)
    _write_declaration(
        source_path, part_id, consumes=("wrong-data",), produces=declaration.produces
    )

    build_part_monitor = _import_build_part_monitor()
    states, wiring = build_part_monitor._measure_build_state()
    subject = next(s for s in states if s.part_id == part_id)

    assert subject.rung == build_part_monitor.FAILING
    assert build_part_monitor.part_is_measured_complete(subject) is False
    assert part_id in wiring.mismatches
    proof = wiring.mismatches[part_id]
    # Both sides named, not just "mismatch found".
    assert declaration.consumes[0] in proof  # what the blueprint declares
    assert "wrong-data" in proof  # what the built module declares


def test_a_built_part_with_no_part_declaration_is_unverifiable_not_green(scratch_part_files):
    source_path, module_name, part_id = scratch_part_files
    source_path.write_text("# a part module that never states its own wiring\n")

    build_part_monitor = _import_build_part_monitor()
    states, wiring = build_part_monitor._measure_build_state()
    subject = next(s for s in states if s.part_id == part_id)

    assert subject.rung == build_part_monitor.FAILING
    assert build_part_monitor.part_is_measured_complete(subject) is False
    assert part_id in wiring.mismatches


def test_a_built_part_with_a_non_literal_declaration_is_red_not_green(scratch_part_files):
    # RL-070's correction: the mistake a future author will make by accident --
    # reaching for the enum member the way ordinary Python writes one, instead
    # of the literal string the static reader requires -- must paint the part
    # red, not silently pass because the value "looks right" to a human.
    source_path, module_name, part_id = scratch_part_files
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

    build_part_monitor = _import_build_part_monitor()
    states, wiring = build_part_monitor._measure_build_state()
    subject = next(s for s in states if s.part_id == part_id)

    assert subject.rung == build_part_monitor.FAILING
    assert build_part_monitor.part_is_measured_complete(subject) is False
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
