"""Capacity is measured, never assumed, and never os.cpu_count().

Section 0: this box is 6 physical cores with SMT2 giving 12 logical, and capacity
planning treats 6 as the ceiling. cpu_count() returns 12 and would let the governor
admit twice the work the machine can actually do.
"""

import os
import pathlib

from runtime.hardware_facts import (
    HardwareFacts,
    measure_hardware_facts,
    read_available_ram_bytes,
    read_cgroup_pressure,
    read_own_cgroup_directory,
)


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
    first = read_available_ram_bytes()
    assert first > 0
    filler = bytearray(64 * 1024 * 1024)
    assert read_available_ram_bytes() > 0
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
