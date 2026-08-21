"""Section 6 admits a part to a rate ladder only on both counts, not either.

(a) does a lower rate change the number produced, or only its arrival time?
(b) does a skipped tick corrupt a monotone invariant, or merely delay it?
Only latency-risk-only on both may be throttled. Everything else gets a floor.
"""

import sys
from pathlib import Path

import pytest

from runtime.part_declaration import (
    PartDeclaration,
    RateRisk,
    ResourceClass,
    SkippedTickEffect,
    load_declaration_from_blueprint,
    may_enter_rate_ladder,
)

DASHBOARD_DIR = Path(__file__).resolve().parent.parent.parent / "dashboard"


def _import_render_blueprint():
    """Import dashboard/render_blueprint.py the way running it directly would:
    its own directory on sys.path first, matching tests/dashboard's own import
    setup for the same reason -- it is a standalone script, not a package member.
    """
    if str(DASHBOARD_DIR) not in sys.path:
        sys.path.insert(0, str(DASHBOARD_DIR))
    import render_blueprint

    return render_blueprint


def _declare(rate_risk: RateRisk, effect: SkippedTickEffect) -> PartDeclaration:
    return PartDeclaration(
        part_id="kline-window-builder",
        consumes=("market-data",),
        produces=("kline-window", "part-health"),
        resource_class=ResourceClass.BANDWIDTH_BOUND,
        rate_risk=rate_risk,
        skipped_tick_effect=effect,
    )


def test_a_part_that_only_arrives_later_may_be_throttled():
    assert may_enter_rate_ladder(_declare(RateRisk.LATENCY_ONLY, SkippedTickEffect.DELAYS)) is True


def test_a_part_whose_answer_changes_may_never_be_throttled():
    # Every IIR indicator: EMA, RSI, ATR, MACD. A shorter window is a different,
    # silently biased answer that looks identical to a healthy one.
    assert may_enter_rate_ladder(
        _declare(RateRisk.CHANGES_THE_ANSWER, SkippedTickEffect.DELAYS)
    ) is False


def test_a_part_whose_skipped_tick_corrupts_may_never_be_throttled():
    # Order-book reconstruction and balance reconciliation: binary correctness.
    assert may_enter_rate_ladder(
        _declare(RateRisk.LATENCY_ONLY, SkippedTickEffect.CORRUPTS)
    ) is False


def test_both_together_are_still_refused():
    assert may_enter_rate_ladder(
        _declare(RateRisk.CHANGES_THE_ANSWER, SkippedTickEffect.CORRUPTS)
    ) is False


def test_a_declaration_loaded_from_the_blueprint_matches_what_the_blueprint_says():
    # RL-067: what is built matches the diagrams. A part's real consumes and
    # produces equal what features.json declares, or the probe in Task 14 fails.
    declaration = load_declaration_from_blueprint("kline-window-builder")
    assert declaration.consumes == ("market-data",)
    assert "part-health" in declaration.produces


def test_loading_a_part_that_is_not_in_the_blueprint_refuses():
    with pytest.raises(KeyError):
        load_declaration_from_blueprint("a-part-nobody-declared")


def test_the_three_vocabularies_agree_with_the_contract_checkers_own_copy():
    # Each of resource_class, rate_risk, and skipped_tick_effect is declared
    # twice: once as a StrEnum here, once as a frozenset in
    # dashboard/render_blueprint.py, which check_contracts.py uses to validate
    # every part in the blueprint. Nothing pins the two copies equal today --
    # they agree because a reviewer checked by hand, and a divergence would be
    # silent because the checker that is supposed to refuse a bad commit would
    # itself be reading a stale vocabulary.
    render_blueprint = _import_render_blueprint()

    assert {member.value for member in ResourceClass} == render_blueprint.KNOWN_RESOURCE_CLASSES
    assert {member.value for member in RateRisk} == render_blueprint.KNOWN_RATE_RISKS
    assert {member.value for member in SkippedTickEffect} == render_blueprint.KNOWN_SKIPPED_TICK_EFFECTS
