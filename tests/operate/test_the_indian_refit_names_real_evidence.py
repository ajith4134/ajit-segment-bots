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


def test_no_setting_appears_in_two_states(refit):
    """The three states are exclusive, and a setting in two of them is a bug.

    Real case (2026-09-12): `whale_minimum_quote_value` was given an Indian
    derivation and then also listed as inert, because nothing reads it. Both
    statements were true and the pair is still wrong -- converted beats inert,
    since a setting right for this market stays right if its reader comes back.
    """
    states = {
        "already-Indian": set(refit.ALREADY_INDIAN),
        "refitted": set(refit.REFITS),
        "inert": set(refit.INERT_WITH_THE_CRYPTO_PATH),
    }
    for left in states:
        for right in states:
            if left >= right:
                continue
            overlap = states[left] & states[right]
            assert overlap == set(), f"{overlap} is both {left} and {right}"
