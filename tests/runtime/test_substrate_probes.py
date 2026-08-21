"""The substrate is off-diagram but not unmeasured (RL-069, section 12).

Rule 8 governs what these may return: a state traces to a probe that ran, absence
of evidence is its own state and never green, and each status carries its proof.
"""

import pytest

from runtime.probes.substrate_probes import (
    SubstrateProbeResult,
    probe_blas_is_pinned,
    probe_forkserver_is_forkable,
    probe_interpreter_build,
    probe_measured_capacity,
    probe_settings_are_readable,
    probe_state_store_opens,
    run_all_substrate_probes,
)

VALID_STATES = {"OK", "NOT BUILT", "FAILING", "NOT MEASURED"}


@pytest.mark.parametrize(
    "probe",
    [
        probe_interpreter_build,
        probe_forkserver_is_forkable,
        probe_blas_is_pinned,
        probe_state_store_opens,
        probe_settings_are_readable,
        probe_measured_capacity,
    ],
)
def test_every_probe_returns_a_valid_state_and_names_its_proof(probe):
    result = probe()
    assert isinstance(result, SubstrateProbeResult)
    assert result.state in VALID_STATES
    assert result.label.strip()
    assert result.proof.strip(), "a status whose provenance cannot be named is not a status"


def test_no_probe_raises_and_every_one_is_included():
    results = run_all_substrate_probes()
    assert len(results) == 6
    assert all(result.state in VALID_STATES for result in results)


def test_the_interpreter_probe_reports_the_build_it_actually_found():
    result = probe_interpreter_build()
    assert result.state == "OK"
    assert "3.14" in result.value


def test_a_probe_that_cannot_establish_its_fact_says_so_rather_than_guessing(monkeypatch):
    # The Rule 8 property that matters: an unreadable source is NOT MEASURED,
    # never OK and never quietly omitted.
    import runtime.probes.substrate_probes as probes

    monkeypatch.setattr(
        probes, "settings_directory",
        lambda: probes.pathlib.Path("/nonexistent/settings/directory"),
    )
    result = probe_settings_are_readable()
    assert result.state in {"NOT MEASURED", "NOT BUILT"}
    assert result.state != "OK"
