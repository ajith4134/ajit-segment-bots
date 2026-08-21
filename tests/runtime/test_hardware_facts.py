"""Capacity is measured, never assumed, and never os.cpu_count().

Section 0: this box is 6 physical cores with SMT2 giving 12 logical, and capacity
planning treats 6 as the ceiling. cpu_count() returns 12 and would let the governor
admit twice the work the machine can actually do.
"""

import mmap
import os
import pathlib

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

# A one-sided bound: the drop only has to be a meaningful fraction of what was
# touched, not all of it, since other processes' concurrent allocation and
# reclaim add noise in both directions. This is the same shape as Task 8's
# cadence test -- a lower bound robust to timing/scheduling jitter, not an
# exact-equality check.
MEANINGFUL_DROP_FRACTION = 0.5


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
    # moved. So this touches real memory and requires MemAvailable to actually
    # reflect it.
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
    assert drop > block_bytes * MEANINGFUL_DROP_FRACTION, (
        f"touched {block_bytes} bytes but MemAvailable only dropped by {drop} "
        f"bytes ({before=}, {after=}); the reading did not reflect what was "
        "actually touched, so it is not a live measurement"
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
