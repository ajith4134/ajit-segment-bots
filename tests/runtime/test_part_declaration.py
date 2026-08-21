"""Section 6 admits a part to a rate ladder only on both counts, not either.

(a) does a lower rate change the number produced, or only its arrival time?
(b) does a skipped tick corrupt a monotone invariant, or merely delay it?
Only latency-risk-only on both may be throttled. Everything else gets a floor.
"""

import pytest

from runtime.part_declaration import (
    PartDeclaration,
    RateRisk,
    ResourceClass,
    SkippedTickEffect,
    load_declaration_from_blueprint,
    may_enter_rate_ladder,
)


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


@pytest.mark.xfail(reason="the three declarations land in Task 13", strict=True)
def test_a_declaration_loaded_from_the_blueprint_matches_what_the_blueprint_says():
    # RL-067: what is built matches the diagrams. A part's real consumes and
    # produces equal what features.json declares, or the probe in Task 14 fails.
    declaration = load_declaration_from_blueprint("kline-window-builder")
    assert declaration.consumes == ("market-data",)
    assert "part-health" in declaration.produces


def test_loading_a_part_that_is_not_in_the_blueprint_refuses():
    with pytest.raises(KeyError):
        load_declaration_from_blueprint("a-part-nobody-declared")
