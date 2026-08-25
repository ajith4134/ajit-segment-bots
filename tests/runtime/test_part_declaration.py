"""Section 6 admits a part to a rate ladder only on both counts, not either.

(a) does a lower rate change the number produced, or only its arrival time?
(b) does a skipped tick corrupt a monotone invariant, or merely delay it?
Only latency-risk-only on both may be throttled. Everything else gets a floor.
"""

import sys
from pathlib import Path

import pytest

from runtime.part_declaration import (
    ModuleDeclaresNoWiring,
    PartDeclaration,
    RateRisk,
    ResourceClass,
    SkippedTickEffect,
    load_declaration_from_blueprint,
    may_enter_rate_ladder,
    read_declaration_from_source,
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
    # `candle` since 2026-08-25: market-data carried trades, candles and books at
    # once, and the first candle crashed a part that reads a trade's sequence.
    assert declaration.consumes == ("candle",)
    assert "part-health" in declaration.produces


def test_loading_a_part_that_is_not_in_the_blueprint_refuses():
    with pytest.raises(KeyError):
        load_declaration_from_blueprint("a-part-nobody-declared")


_VALID_DECLARATION_SOURCE = """
from runtime.part_declaration import PartDeclaration

PART_DECLARATION = PartDeclaration(
    part_id="kline-window-builder",
    consumes=("market-data",),
    produces=("kline-window", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)
"""


def test_a_literal_part_declaration_is_read_without_importing_anything(tmp_path):
    # If this test somehow imported the file instead of parsing it, the bad
    # import below would raise ImportError -- it never does, because
    # read_declaration_from_source never executes the file.
    source = tmp_path / "kline_window_builder.py"
    source.write_text("import this_module_does_not_exist_anywhere\n\n" + _VALID_DECLARATION_SOURCE)

    declaration = read_declaration_from_source(source)

    assert declaration.part_id == "kline-window-builder"
    assert declaration.consumes == ("market-data",)
    assert declaration.produces == ("kline-window", "part-health")
    assert declaration.resource_class is ResourceClass.BANDWIDTH_BOUND


def test_a_file_with_no_part_declaration_assignment_refuses(tmp_path):
    # Never green by inference: a built part that states nothing about its own
    # wiring must not be read as agreeing with the blueprint.
    source = tmp_path / "scratch_part.py"
    source.write_text("X = 1\n")
    with pytest.raises(ModuleDeclaresNoWiring, match="no PART_DECLARATION assignment"):
        read_declaration_from_source(source)


def test_a_part_declaration_that_is_not_a_partdeclaration_call_refuses(tmp_path):
    source = tmp_path / "scratch_part.py"
    source.write_text('PART_DECLARATION = "not a call at all"\n')
    with pytest.raises(ModuleDeclaresNoWiring, match="not a PartDeclaration"):
        read_declaration_from_source(source)


@pytest.mark.parametrize(
    "field_source",
    [
        pytest.param('resource_class=ResourceClass.BANDWIDTH_BOUND', id="enum-attribute-access"),
        pytest.param('consumes=tuple(["market-data"])', id="a-call-expression"),
        pytest.param('produces=("kline-window", SOME_CONSTANT)', id="a-name-reference"),
        pytest.param('part_id=f"kline-{1}"', id="an-f-string"),
    ],
)
def test_a_part_declaration_with_a_non_literal_argument_names_it(tmp_path, field_source):
    # RL-070's correction: a built part's PART_DECLARATION must be a literal --
    # no computation, no name resolved at runtime, no f-string. This is the
    # mistake a future author will make by accident (reaching for the enum
    # member the way ordinary Python code writes one), so it must be refused
    # with a message that says "literal", not a bare KeyError or a silent skip.
    base = dict(
        part_id='"kline-window-builder"',
        consumes='("market-data",)',
        produces='("kline-window", "part-health")',
        resource_class='"bandwidth-bound"',
        rate_risk='"changes-the-answer"',
        skipped_tick_effect='"corrupts"',
    )
    field_name = field_source.split("=", 1)[0]
    base[field_name] = field_source.split("=", 1)[1]
    body = ",\n    ".join(f"{name}={value}" for name, value in base.items())
    source = tmp_path / "scratch_part.py"
    source.write_text(f"from runtime.part_declaration import PartDeclaration\n\nPART_DECLARATION = PartDeclaration(\n    {body},\n)\n")

    with pytest.raises(ModuleDeclaresNoWiring, match="literal"):
        read_declaration_from_source(source)


def test_a_part_declaration_with_positional_arguments_refuses(tmp_path):
    source = tmp_path / "scratch_part.py"
    source.write_text(
        "from runtime.part_declaration import PartDeclaration\n\n"
        'PART_DECLARATION = PartDeclaration("kline-window-builder", ("market-data",), '
        '("kline-window",), "bandwidth-bound", "changes-the-answer", "corrupts")\n'
    )
    with pytest.raises(ModuleDeclaresNoWiring, match="positional"):
        read_declaration_from_source(source)


def test_a_file_that_does_not_parse_refuses(tmp_path):
    source = tmp_path / "scratch_part.py"
    source.write_text("def broken(:\n    pass\n")
    with pytest.raises(ModuleDeclaresNoWiring, match="does not parse"):
        read_declaration_from_source(source)


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
