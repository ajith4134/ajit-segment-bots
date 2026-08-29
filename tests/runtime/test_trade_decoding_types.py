from runtime.trade_decoding_types import (
    StopAudit, ExitCounterfactual, STOP_VERDICTS, SEQUENCE_KINDS,
    INSIDE_THE_NOISE, TOO_WIDE, WELL_PLACED_AND_HIT, WELL_PLACED_AND_NOT_HIT,
    NEVER_APPROACHED, NO_STOP, NOT_MEASURABLE,
    STREAKS, SIZE_DRIFT, SESSION_DECAY, OUTCOME_CONDITIONING,
)


def test_stop_audit_carries_identity_and_a_magnitude():
    audit = StopAudit(
        trade_id="t1", stop_price=100.0, distance=5.0, typical_movement=1.0,
        distance_in_typical_movements=5.0, was_hit=True, would_have_recovered=None,
        verdict=INSIDE_THE_NOISE, is_measurable=True, reason="r", audited_at_ns=1,
        venue_id="binance-usdm", symbol="BTCUSDT", adverse_excursion_fraction=0.03,
    )
    assert audit.venue_id == "binance-usdm"
    assert audit.symbol == "BTCUSDT"
    assert audit.adverse_excursion_fraction == 0.03


def test_exit_counterfactual_carries_identity_and_a_trail_fraction():
    counterfactual = ExitCounterfactual(
        trade_id="t1", rule_name="tail-exit-plan:trail", exit_price=99.0,
        realised_pnl=1.0, difference=0.5, would_have_been_reachable=True,
        is_hindsight=True, reason="r", replayed_at_ns=1,
        venue_id="binance-usdm", symbol="BTCUSDT", trail_fraction=0.02,
    )
    assert counterfactual.venue_id == "binance-usdm"
    assert counterfactual.symbol == "BTCUSDT"
    assert counterfactual.trail_fraction == 0.02


def test_stop_verdicts_and_sequence_kinds_are_the_seven_and_four_real_constants():
    assert STOP_VERDICTS == (
        INSIDE_THE_NOISE, TOO_WIDE, WELL_PLACED_AND_HIT, WELL_PLACED_AND_NOT_HIT,
        NEVER_APPROACHED, NO_STOP, NOT_MEASURABLE,
    )
    assert SEQUENCE_KINDS == (STREAKS, SIZE_DRIFT, SESSION_DECAY, OUTCOME_CONDITIONING)
