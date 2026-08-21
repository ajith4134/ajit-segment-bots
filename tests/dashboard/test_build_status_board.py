"""dashboard/build_status_board.py's substrate tile group (Task 14, RL-069).

The one behaviour Rule 8 depends on most here: a board that cannot import the
substrate must say so with a tile carrying the unmeasured state and a proof,
never omit the group -- an absent tile reads as "nothing to measure here" and
that is exactly the failure Rule 8 exists to prevent. Before this file, that
guard was checked only in prose and an uncommitted ad hoc script.

Lives outside tests/runtime/ on purpose: this exercises a dashboard script, not
a member of the runtime package under test, and dashboard/build_status_board.py
imports itself the way a standalone script does (its own directory on sys.path,
a bare `from render_blueprint import ...`) rather than as part of any package --
so it needs its own import setup, not tests/runtime/conftest.py's.
"""

import sys
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parent.parent.parent / "dashboard"


def _import_build_status_board():
    """Import the script the way running it directly would: its own directory
    on sys.path first, so its bare `from render_blueprint import ...` resolves."""
    if str(DASHBOARD_DIR) not in sys.path:
        sys.path.insert(0, str(DASHBOARD_DIR))
    import build_status_board

    return build_status_board


def test_a_board_that_cannot_import_the_substrate_reports_it_rather_than_omitting_the_tile(
    monkeypatch,
):
    build_status_board = _import_build_status_board()

    # A None entry in sys.modules is CPython's own way of saying "this import
    # fails" -- it makes the module's own `from runtime.probes.substrate_probes
    # import run_all_substrate_probes` raise ImportError without touching the
    # real, working package on disk.
    monkeypatch.setitem(sys.modules, "runtime.probes.substrate_probes", None)

    results = build_status_board.collect_substrate_results()

    assert len(results) == 1, "an unmeasurable substrate is one tile, not zero and not six"
    tile = results[0]
    assert tile.label == "Part runtime substrate"
    assert tile.state == build_status_board.UNMEASURED
    assert tile.proof.strip(), "a status whose provenance cannot be named is not a status"


def test_a_board_that_can_import_the_substrate_reports_all_six_probes():
    # The complement of the test above: the guard must not mask a real success
    # either -- a board that always showed the fallback would be just as wrong
    # as one that always omitted the group.
    build_status_board = _import_build_status_board()

    results = build_status_board.collect_substrate_results()

    assert len(results) == 6
    assert all(result.proof.strip() for result in results)
