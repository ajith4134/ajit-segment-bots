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


def test_zero_blas_pools_is_not_measured_rather_than_a_vacuous_ok(monkeypatch):
    # "every one of zero pools is pinned" is true of an empty set but measures
    # nothing -- no BLAS library is loaded, so the question has no live subject.
    # A green OK here would be exactly the Rule 8 failure: a reassuring tile
    # standing in for a reading that never happened.
    import threadpoolctl

    monkeypatch.setattr(threadpoolctl, "threadpool_info", lambda: [])
    result = probe_blas_is_pinned()
    assert result.state == "NOT MEASURED"
    assert result.state != "OK"


def test_capacity_with_an_unmeasured_field_is_not_measured_rather_than_a_green_none(
    monkeypatch,
):
    # A topology-less box (a container, an old kernel): count_physical_cores()
    # returns None, never a guessed number. A tile reporting OK over that None
    # is exactly the Rule 8 failure this probe exists to refuse --
    # "physical_cores=None" is a fact nobody measured, not a healthy reading.
    import runtime.hardware_facts as hardware_facts

    monkeypatch.setattr(hardware_facts, "count_physical_cores", lambda: None)
    result = probe_measured_capacity()
    assert result.state == "NOT MEASURED"
    assert result.state != "OK"
    assert "physical_cores" in result.value


def test_capacity_with_every_field_measured_is_ok():
    result = probe_measured_capacity()
    # This box (section 0) does publish full topology, so a healthy run here
    # must still reach OK -- the fix must not turn every reading NOT MEASURED.
    assert result.state == "OK"


def test_the_interpreter_probe_says_so_when_pyproject_is_unreadable(monkeypatch):
    # RL-061: the interpreter probe's OK/FAILING threshold is sourced from
    # pyproject.toml's own requires-python, not a literal in this module. If
    # that source cannot be read, the probe must not fall back to a guess.
    import runtime.probes.substrate_probes as probes

    monkeypatch.setattr(probes, "PYPROJECT_PATH", probes.pathlib.Path("/nonexistent/pyproject.toml"))
    result = probe_interpreter_build()
    assert result.state == "NOT MEASURED"


def test_the_interpreter_probe_fails_when_the_running_version_disagrees_with_the_pin(monkeypatch):
    # If pyproject.toml and the running interpreter ever disagree, the probe
    # must say so rather than silently testing a different version than the
    # project actually pins.
    import runtime.probes.substrate_probes as probes

    monkeypatch.setattr(probes, "_read_pinned_interpreter_version", lambda: (2, 7))
    result = probe_interpreter_build()
    assert result.state == "FAILING"
    assert "2.7" in result.value
