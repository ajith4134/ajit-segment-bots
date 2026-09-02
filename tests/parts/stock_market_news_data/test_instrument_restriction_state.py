"""Reports here are the exact shapes trading-restriction-reader builds from the
live NSE responses captured 2026-09-02."""

import datetime

import pytest

from runtime.market_conditions import InstrumentRestrictionReport, RestrictionKind
from parts.stock_market_news_data.instrument_restriction_state import (
    InstrumentRestrictionState,
)

DAY = datetime.date(2026, 9, 2)
ONE_SECOND_NS = 1_000_000_000
NOW_NS = 1_756_800_000_000_000_000


def _report(symbol="LICHSGFIN", kind=RestrictionKind.FNO_BAN,
            source="nse-fo-secban", observed_at_ns=NOW_NS):
    return InstrumentRestrictionReport(
        symbol=symbol, kind=kind, source=source, stated_for=DAY,
        detail="Securities in Ban For Trade Date 02-SEP-2026", observed_at_ns=observed_at_ns,
    )


def test_a_reported_symbol_may_not_open_a_new_position():
    state = InstrumentRestrictionState(maximum_age_seconds=3600.0)
    state.observe(_report())
    restriction = state.restriction_for("LICHSGFIN", NOW_NS)
    assert restriction is not None
    assert restriction.may_open_new_position is False


def test_an_unreported_symbol_has_no_restriction_at_all():
    """Absence is absence. A restriction object saying 'not restricted' would
    make every consumer's 'do I have one' check meaningless."""
    state = InstrumentRestrictionState(maximum_age_seconds=3600.0)
    state.observe(_report())
    assert state.restriction_for("RELIANCE", NOW_NS) is None


def test_two_sources_on_one_symbol_merge_into_one_restriction():
    state = InstrumentRestrictionState(maximum_age_seconds=3600.0)
    state.observe(_report(symbol="SAIL"))
    state.observe(_report(symbol="SAIL", kind=RestrictionKind.ASM_SHORT_TERM,
                          source="nse-asm-shortterm"))
    restriction = state.restriction_for("SAIL", NOW_NS)
    assert set(restriction.kinds) == {RestrictionKind.FNO_BAN, RestrictionKind.ASM_SHORT_TERM}
    assert set(restriction.sources) == {"nse-fo-secban", "nse-asm-shortterm"}


def test_one_source_restating_its_claim_does_not_duplicate_it():
    """Every poll republishes the whole list. A restriction naming
    ('nse-fo-secban', 'nse-fo-secban', ...) would grow without bound and
    change on every poll, defeating the level publisher's change check."""
    state = InstrumentRestrictionState(maximum_age_seconds=3600.0)
    state.observe(_report())
    state.observe(_report(observed_at_ns=NOW_NS + ONE_SECOND_NS))
    restriction = state.restriction_for("LICHSGFIN", NOW_NS + ONE_SECOND_NS)
    assert restriction.sources == ("nse-fo-secban",)
    assert restriction.kinds == (RestrictionKind.FNO_BAN,)


def test_a_claim_older_than_its_bound_is_absent_not_old():
    """The whole reason this part exists. NSE stops publishing a name the day
    its ban lifts -- it does not publish an un-ban. Without the bound the name
    stays banned forever and the bot never trades it again."""
    state = InstrumentRestrictionState(maximum_age_seconds=3600.0)
    state.observe(_report(observed_at_ns=NOW_NS))
    later_ns = NOW_NS + 3601 * ONE_SECOND_NS
    assert state.restriction_for("LICHSGFIN", later_ns) is None


def test_a_claim_inside_its_bound_still_stands():
    state = InstrumentRestrictionState(maximum_age_seconds=3600.0)
    state.observe(_report(observed_at_ns=NOW_NS))
    later_ns = NOW_NS + 3599 * ONE_SECOND_NS
    assert state.restriction_for("LICHSGFIN", later_ns) is not None


def test_one_source_expiring_leaves_the_other_source_s_claim_standing():
    state = InstrumentRestrictionState(maximum_age_seconds=3600.0)
    state.observe(_report(symbol="SAIL", observed_at_ns=NOW_NS))
    fresh_ns = NOW_NS + 3000 * ONE_SECOND_NS
    state.observe(_report(symbol="SAIL", kind=RestrictionKind.ASM_SHORT_TERM,
                          source="nse-asm-shortterm", observed_at_ns=fresh_ns))
    at_ns = NOW_NS + 3601 * ONE_SECOND_NS
    restriction = state.restriction_for("SAIL", at_ns)
    assert restriction is not None
    assert restriction.kinds == (RestrictionKind.ASM_SHORT_TERM,)


def test_restrictions_lists_every_symbol_still_standing():
    state = InstrumentRestrictionState(maximum_age_seconds=3600.0)
    state.observe(_report(symbol="LICHSGFIN"))
    state.observe(_report(symbol="SAIL"))
    assert {r.symbol for r in state.restrictions(NOW_NS)} == {"LICHSGFIN", "SAIL"}


def test_restrictions_is_ordered_so_an_unchanged_level_compares_equal():
    """LevelPublisher digests the whole tuple. Iteration order that wandered
    would republish an unchanged level on every tick while the skip counter
    claimed the opposite -- the exact failure the first level_publishing had."""
    state = InstrumentRestrictionState(maximum_age_seconds=3600.0)
    for symbol in ("SAIL", "LICHSGFIN", "AASTHA"):
        state.observe(_report(symbol=symbol))
    assert [r.symbol for r in state.restrictions(NOW_NS)] == ["AASTHA", "LICHSGFIN", "SAIL"]


def test_an_unbounded_state_is_refused_at_construction():
    """A bound of zero expires the report that just arrived; a negative one
    expires nothing. Both read as 'staleness is handled' while doing the
    opposite, which is why neither is accepted."""
    with pytest.raises(ValueError):
        InstrumentRestrictionState(maximum_age_seconds=0.0)
