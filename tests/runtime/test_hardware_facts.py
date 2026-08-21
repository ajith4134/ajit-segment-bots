"""Capacity is measured, never assumed, and never os.cpu_count().

Section 0: this box is 6 physical cores with SMT2 giving 12 logical, and capacity
planning treats 6 as the ceiling. cpu_count() returns 12 and would let the governor
admit twice the work the machine can actually do.
"""

import mmap
import os
import pathlib
import time

from runtime.hardware_facts import (
    HardwareFacts,
    measure_hardware_facts,
    read_available_ram_bytes,
    read_cgroup_pressure,
    read_own_cgroup_directory,
)

# The touched block is a fraction of what MemAvailable reports right now, not a
# literal byte count, so the test scales with the box instead of assuming a
# particular amount of free RAM. On this box (~30 GB available) this is a few
# hundred MiB, which is easily distinguished from background churn.
AVAILABLE_RAM_TOUCH_DIVISOR = 64

# How many idle readings establish this box's own noise band, and how far apart.
# 20 readings at 10ms apart is a ~0.2s window -- long enough to see the same kind
# of background churn (page cache reclaim, other processes) the touch will
# compete with, short enough that the test stays fast.
IDLE_READING_COUNT = 20
IDLE_READING_INTERVAL_SECONDS = 0.01

# The drop from a deliberate touch must clear the box's own measured idle noise
# by an unambiguous multiple, not merely exceed it -- a margin of exactly 1x would
# still call a touch "live" if it happened to be barely above the noise on a
# given run. The multiplier is applied to a *measured* quantity (this box's idle
# band, taken immediately before the touch), not to an assumed fraction of what
# was touched, which is what made the previous version of this test flaky: it
# assumed how much of a freed allocation the allocator would hand back to the OS,
# and that fraction turned out to depend on allocator/kernel state this test has
# no business knowing.
IDLE_BAND_MARGIN_MULTIPLIER = 10


def _measure_idle_band(reading_count: int, interval_seconds: float) -> tuple[int, int, int]:
    """How much read_available_ram_bytes() moves on its own, with nothing touched.

    Returns (low, high, band) over `reading_count` readings spaced
    `interval_seconds` apart.
    """
    readings = []
    for _ in range(reading_count):
        readings.append(read_available_ram_bytes())
        time.sleep(interval_seconds)
    return min(readings), max(readings), max(readings) - min(readings)


def test_counts_physical_cores_not_logical_ones():
    facts = measure_hardware_facts()
    assert isinstance(facts, HardwareFacts)
    assert facts.logical_cpus == os.cpu_count()
    assert facts.physical_cores < facts.logical_cpus, (
        "SMT is on here, so physical cores must be fewer than logical CPUs"
    )
    assert facts.physical_cores * 2 == facts.logical_cpus, "SMT2, per section 0"


def test_reports_that_this_box_has_no_swap():
    # Section 5 depends on this: with no swap, dirty pages have nowhere to go and a
    # writer that outruns writeback is OOM-killed rather than slowed.
    assert measure_hardware_facts().swap_total_bytes == 0


def test_available_ram_is_a_live_reading_not_a_cached_one():
    # A test that only checks positivity before and after would pass against a
    # frozen constant -- Rule 8 inverted, a reading certified live that never
    # moved. So this measures the box's own idle noise first, then touches real
    # memory and requires the drop to clearly clear that noise -- not an assumed
    # fraction of what was touched, which depends on allocator/kernel behavior
    # this test has no business assuming (see IDLE_BAND_MARGIN_MULTIPLIER).
    idle_low, idle_high, idle_band = _measure_idle_band(
        IDLE_READING_COUNT, IDLE_READING_INTERVAL_SECONDS
    )

    before = read_available_ram_bytes()
    assert before > 0

    block_bytes = before // AVAILABLE_RAM_TOUCH_DIVISOR
    filler = bytearray(block_bytes)
    # CPython's bytearray(n) happens to zero-fill the buffer at construction,
    # which faults in every page as a side effect -- but that is an internal
    # detail this test should not depend on. Writing one byte per page
    # explicitly is what makes "this really touches physical memory" true on
    # its own terms, regardless of how the allocator behaves under the hood.
    page_size = mmap.PAGESIZE
    page_offsets = range(0, block_bytes, page_size)
    filler[0 : len(page_offsets) * page_size : page_size] = bytes(len(page_offsets))

    after = read_available_ram_bytes()
    drop = before - after
    required_drop = idle_band * IDLE_BAND_MARGIN_MULTIPLIER
    assert drop > required_drop, (
        f"idle band over {IDLE_READING_COUNT} readings ({IDLE_READING_INTERVAL_SECONDS}s "
        f"apart) was {idle_band} bytes ({idle_low}..{idle_high}); touching "
        f"{block_bytes} bytes only dropped MemAvailable by {drop} bytes "
        f"({before=}, {after=}), which does not clear {IDLE_BAND_MARGIN_MULTIPLIER}x "
        f"that idle band ({required_drop} bytes); the reading did not distinguish "
        "the touch from its own idle noise, so it is not a live measurement"
    )

    del filler


def test_every_fact_carries_when_it_was_measured():
    assert measure_hardware_facts().measured_at_ns > 0


def test_finds_its_own_cgroup_directory():
    directory = read_own_cgroup_directory()
    assert directory.is_dir()
    assert (directory / "cgroup.procs").exists()


def test_reads_the_per_cgroup_pressure_lines():
    # The per-cgroup 'full' line is the usable one. The system-wide 'full' line is
    # zero by definition, so a governor reading /proc/pressure/cpu would see a
    # signal that never fires.
    pressure = read_cgroup_pressure(read_own_cgroup_directory(), resource="cpu")
    assert "some_avg10" in pressure
    assert "full_avg10" in pressure
    assert all(isinstance(value, float) for value in pressure.values())
