"""No part reads a field its producers do not carry.

This is the check that would have caught all three of the defects that appeared
on 2026-08-25, each of which could only fail on the day the producing block was
first switched on:

    stop-target-placer  LiquidationMap.cluster_prices          crashed
    stop-target-placer  VolatilityForecast.expected_move_fraction  silent
    position-sizer      LockedAllocation.segment               crashed 1,587 times

The contract checker proves a wire exists (R-01). This proves what travels on it
has the shape the reader expects -- and a wire carrying the wrong shape is worse
than an absent one, because every board reports it as alive.
"""

from __future__ import annotations

from dashboard.check_payload_reads import (
    collect_producer_attribute_names,
    find_reads_no_producer_carries,
    import_every_part,
)

MODULES = import_every_part()
PRODUCER_NAMES = collect_producer_attribute_names(MODULES)


def test_every_read_names_a_field_some_producer_carries():
    unmatched, _, _ = find_reads_no_producer_carries(MODULES, PRODUCER_NAMES)
    assert not unmatched, "\n".join(
        f"{read.part_id} reads {read.data_type}.{read.attribute} "
        f"({read.source_file}:{read.line})"
        for read in sorted(unmatched, key=lambda r: (r.part_id, r.data_type, r.attribute))
    )


def test_no_part_reads_a_data_type_nothing_produces():
    """A consumer of an unproduced type is a wire to nothing, whatever its shape."""
    _, unproduced, _ = find_reads_no_producer_carries(MODULES, PRODUCER_NAMES)
    assert not unproduced, "\n".join(
        f"{read.part_id} reads {read.data_type}.{read.attribute}" for read in unproduced
    )


def test_the_checker_still_follows_the_reads_it_used_to():
    """A parser that silently stopped following anything would report zero defects."""
    _, _, every_read = find_reads_no_producer_carries(MODULES, PRODUCER_NAMES)
    assert len(every_read) > 1_500
