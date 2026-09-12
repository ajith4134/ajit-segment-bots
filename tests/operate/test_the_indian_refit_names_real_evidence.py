"""Every "already Indian" claim names evidence that exists (2026-09-12).

`operate/refit_settings_to_the_indian_market.py` records which settings were
re-derived for the Indian market in earlier sessions, so a probe can tell a
converted setting from one still fitted to crypto. That recording changes no
value -- it only asserts that somebody already did the work.

**An assertion like that is exactly how a drift guard starts lying.** If the
measurement it points at is gone, or was never there, the setting reads as
converted and nothing is counting it any more. So each entry names its proof and
this test checks the proof is really on disk.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE = ROOT / "operate/refit_settings_to_the_indian_market.py"


@pytest.fixture(scope="module")
def refit():
    spec = importlib.util.spec_from_file_location("refit_settings", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_named_artifact_is_really_in_the_tree(refit):
    missing = []
    for name, proof in refit.ALREADY_INDIAN.items():
        if proof.startswith(("measurements/", "runtime/")):
            if not (ROOT / proof).exists():
                missing.append((name, proof))
    assert missing == [], (
        f"these settings claim a derivation whose evidence is gone: {missing}. "
        f"A converted setting whose proof vanished is drift nothing is counting."
    )


def test_a_claim_that_is_not_an_artifact_is_an_identity_not_a_number(refit):
    """The only non-artifact proofs allowed are values that ARE the market.

    "INR" is not a number fitted to anything, so it needs no measurement. A
    threshold is, and one recorded here without an artifact would be a crypto
    number wearing an Indian label.
    """
    for name, proof in refit.ALREADY_INDIAN.items():
        if proof.startswith(("measurements/", "runtime/")):
            continue
        assert proof.startswith("the value "), (
            f"{name} claims '{proof}', which is neither an artifact in the tree nor "
            f"a statement that the value is itself an Indian identity"
        )


def test_recording_never_changes_a_value(refit):
    """The recorder appends provenance; it must not touch `value =`."""
    block = '[a_setting]\nvalue = 1.5\nunit  = "x"\nnote  = "was crypto."\n'
    updated, what = refit.record_already_indian(block, "a_setting", "runtime/price_staleness.py")

    assert what == "recorded"
    assert "value = 1.5" in updated
    assert refit.CONVERTED_MARKER in updated


def test_recording_is_idempotent(refit):
    block = '[a_setting]\nvalue = 1.5\nunit  = "x"\nnote  = "was crypto."\n'
    once, _ = refit.record_already_indian(block, "a_setting", "runtime/price_staleness.py")
    twice, what = refit.record_already_indian(once, "a_setting", "runtime/price_staleness.py")

    assert what == "already recorded"
    assert twice == once


def test_no_setting_is_both_recorded_and_refitted(refit):
    """A setting cannot be already-Indian and also need a new value."""
    overlap = set(refit.ALREADY_INDIAN) & set(refit.REFITS)
    assert overlap == set(), f"{overlap} appear in both lists"
