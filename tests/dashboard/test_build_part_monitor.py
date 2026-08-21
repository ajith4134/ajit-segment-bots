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

# A real blueprint part, used as the subject for every scratch-file test below.
# docs/features.json's own declaration for it is asserted against directly
# rather than assumed, so a future edit to the blueprint entry cannot make
# these tests silently test something else.
SUBJECT_PART_ID = "kline-window-builder"
SUBJECT_CONSUMES = ("market-data",)
SUBJECT_PRODUCES = ("kline-window", "part-health")


def _import_build_part_monitor():
    """Import the way running it directly would: its own directory on
    sys.path first, matching tests/dashboard's established convention."""
    if str(DASHBOARD_DIR) not in sys.path:
        sys.path.insert(0, str(DASHBOARD_DIR))
    import build_part_monitor

    return build_part_monitor


@pytest.fixture
def scratch_part_files():
    """A real source file (and a real test file) for SUBJECT_PART_ID, written
    under the project root -- outside tests/ so pytest's own collection never
    sees it, and outside build_part_monitor's EXCLUDED_DIRS so its own
    find_source_files() does. Removed whether the test passes or fails.
    """
    scratch_dir = PROJECT / "rl070_scratch_wiring_check"
    scratch_dir.mkdir(exist_ok=True)
    module_name = "rl070_scratch_kline_window_builder"
    source_path = scratch_dir / f"{module_name}.py"
    test_path = scratch_dir / f"test_{module_name}.py"
    test_path.write_text("def test_scratch_placeholder():\n    assert True\n")
    try:
        yield source_path, module_name
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def _write_declaration(source_path: Path, *, consumes: tuple[str, ...], produces: tuple[str, ...]) -> None:
    """A literal PART_DECLARATION, exactly the shape read_declaration_from_source
    requires: every field a keyword, every value a literal (no enum-attribute
    access) -- consumes/produces come from the caller, resource_class/rate_risk/
    skipped_tick_effect are fixed to real vocabulary values for the subject part.
    """
    source_path.write_text(
        "from runtime.part_declaration import PartDeclaration\n"
        "\n"
        "PART_DECLARATION = PartDeclaration(\n"
        f"    part_id={SUBJECT_PART_ID!r},\n"
        f"    consumes={consumes!r},\n"
        f"    produces={produces!r},\n"
        '    resource_class="bandwidth-bound",\n'
        '    rate_risk="changes-the-answer",\n'
        '    skipped_tick_effect="corrupts",\n'
        ")\n"
    )


def test_a_part_with_no_source_file_is_declared_and_red():
    build_part_monitor = _import_build_part_monitor()
    states = build_part_monitor.measure_parts()
    subject = next(s for s in states if s.part_id == SUBJECT_PART_ID)
    assert subject.rung == build_part_monitor.DECLARED
    assert build_part_monitor.part_is_measured_complete(subject) is False


def test_nothing_built_means_the_wiring_check_has_nothing_to_compare():
    # RL-070: today no part is implemented, so this must say "nothing to
    # compare" rather than report an agreement it never checked.
    build_part_monitor = _import_build_part_monitor()
    registry = build_part_monitor.load_feature_registry()
    sources = build_part_monitor.find_source_files()
    wiring = build_part_monitor.check_wiring_against_blueprint(registry, sources)
    assert wiring.unavailable is None
    assert wiring.checked_part_ids == ()
    assert wiring.mismatches == {}


def test_a_source_file_and_a_matching_test_file_turn_the_dot_green(scratch_part_files):
    source_path, module_name = scratch_part_files
    _write_declaration(source_path, consumes=SUBJECT_CONSUMES, produces=SUBJECT_PRODUCES)

    build_part_monitor = _import_build_part_monitor()
    states, wiring = build_part_monitor._measure_build_state()
    subject = next(s for s in states if s.part_id == SUBJECT_PART_ID)

    assert subject.rung == build_part_monitor.TESTED
    assert build_part_monitor.part_is_measured_complete(subject) is True
    assert SUBJECT_PART_ID in wiring.checked_part_ids
    assert SUBJECT_PART_ID not in wiring.mismatches


def test_removing_the_scratch_files_turns_the_dot_red_again(scratch_part_files):
    # The complement of the test above, in one test: green is reachable AND
    # reversible -- a colour observed once and never un-observed is not proven.
    source_path, module_name = scratch_part_files
    _write_declaration(source_path, consumes=SUBJECT_CONSUMES, produces=SUBJECT_PRODUCES)

    build_part_monitor = _import_build_part_monitor()
    states, _wiring = build_part_monitor._measure_build_state()
    subject = next(s for s in states if s.part_id == SUBJECT_PART_ID)
    assert build_part_monitor.part_is_measured_complete(subject) is True

    source_path.unlink()
    (source_path.parent / f"test_{module_name}.py").unlink()

    states, wiring = build_part_monitor._measure_build_state()
    subject = next(s for s in states if s.part_id == SUBJECT_PART_ID)
    assert subject.rung == build_part_monitor.DECLARED
    assert build_part_monitor.part_is_measured_complete(subject) is False
    assert wiring.checked_part_ids == ()


def test_a_built_part_whose_wiring_disagrees_is_painted_red_naming_both_sides(scratch_part_files):
    source_path, module_name = scratch_part_files
    _write_declaration(source_path, consumes=("wrong-data",), produces=SUBJECT_PRODUCES)

    build_part_monitor = _import_build_part_monitor()
    states, wiring = build_part_monitor._measure_build_state()
    subject = next(s for s in states if s.part_id == SUBJECT_PART_ID)

    assert subject.rung == build_part_monitor.FAILING
    assert build_part_monitor.part_is_measured_complete(subject) is False
    assert SUBJECT_PART_ID in wiring.mismatches
    proof = wiring.mismatches[SUBJECT_PART_ID]
    # Both sides named, not just "mismatch found".
    assert "market-data" in proof  # what the blueprint declares
    assert "wrong-data" in proof  # what the built module declares


def test_a_built_part_with_no_part_declaration_is_unverifiable_not_green(scratch_part_files):
    source_path, module_name = scratch_part_files
    source_path.write_text("# a part module that never states its own wiring\n")

    build_part_monitor = _import_build_part_monitor()
    states, wiring = build_part_monitor._measure_build_state()
    subject = next(s for s in states if s.part_id == SUBJECT_PART_ID)

    assert subject.rung == build_part_monitor.FAILING
    assert build_part_monitor.part_is_measured_complete(subject) is False
    assert SUBJECT_PART_ID in wiring.mismatches


def test_a_built_part_with_a_non_literal_declaration_is_red_not_green(scratch_part_files):
    # RL-070's correction: the mistake a future author will make by accident --
    # reaching for the enum member the way ordinary Python writes one, instead
    # of the literal string the static reader requires -- must paint the part
    # red, not silently pass because the value "looks right" to a human.
    source_path, module_name = scratch_part_files
    source_path.write_text(
        "from runtime.part_declaration import PartDeclaration, ResourceClass\n"
        "\n"
        "PART_DECLARATION = PartDeclaration(\n"
        f"    part_id={SUBJECT_PART_ID!r},\n"
        f"    consumes={SUBJECT_CONSUMES!r},\n"
        f"    produces={SUBJECT_PRODUCES!r},\n"
        "    resource_class=ResourceClass.BANDWIDTH_BOUND,\n"  # not a literal
        '    rate_risk="changes-the-answer",\n'
        '    skipped_tick_effect="corrupts",\n'
        ")\n"
    )

    build_part_monitor = _import_build_part_monitor()
    states, wiring = build_part_monitor._measure_build_state()
    subject = next(s for s in states if s.part_id == SUBJECT_PART_ID)

    assert subject.rung == build_part_monitor.FAILING
    assert build_part_monitor.part_is_measured_complete(subject) is False
    assert SUBJECT_PART_ID in wiring.mismatches
    assert "literal" in wiring.mismatches[SUBJECT_PART_ID]


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
