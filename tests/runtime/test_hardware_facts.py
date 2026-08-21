"""Capacity is measured, never assumed, and never os.cpu_count().

Section 0: this box is 6 physical cores with SMT2 giving 12 logical, and capacity
planning treats 6 as the ceiling. cpu_count() returns 12 and would let the governor
admit twice the work the machine can actually do.

A fact this module cannot measure must come back as None, never as a guess wearing
the shape of a measurement (Rule 8) -- so several tests here exist to prove the
*absence* of topology produces None, not a fallback to the logical count.
"""

import mmap
import os
import pathlib
import time

import pytest

import runtime.hardware_facts as hardware_facts
from runtime.hardware_facts import (
    HardwareFacts,
    MeminfoFieldMissing,
    count_physical_cores,
    measure_hardware_facts,
    parse_physical_core_count,
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
# was touched, which is what made the first version of this test flaky: it
# assumed how much of a freed allocation the allocator would hand back to the OS,
# and that fraction turned out to depend on allocator/kernel state this test has
# no business knowing.
IDLE_BAND_MARGIN_MULTIPLIER = 10

# A minimal cpuinfo excerpt describing two sockets... no, one socket, two physical
# cores, SMT2 -- four logical CPUs (0,2 share physical core 0; 1,3 share core 1).
# Only the fields parse_physical_core_count actually reads are included, matching
# what a real /proc/cpuinfo interleaves them with other fields this parser ignores.
CPUINFO_TEXT_WITH_TOPOLOGY = """\
processor\t: 0
physical id\t: 0
core id\t: 0

processor\t: 1
physical id\t: 0
core id\t: 1

processor\t: 2
physical id\t: 0
core id\t: 0

processor\t: 3
physical id\t: 0
core id\t: 1
"""

# What a kernel or container that publishes no CPU topology looks like -- no
# "physical id" / "core id" lines at all, just processor numbers and other
# fields this parser does not read.
CPUINFO_TEXT_WITHOUT_TOPOLOGY = """\
processor\t: 0
vendor_id\t: GenuineIntel
model name\t: a CPU whose cpuinfo carries no topology fields
bogomips\t: 4000.00

processor\t: 1
vendor_id\t: GenuineIntel
model name\t: a CPU whose cpuinfo carries no topology fields
bogomips\t: 4000.00
"""


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


def _attempt_live_ram_measurement() -> dict[str, int]:
    """One idle-band-then-touch cycle.

    Measures this box's own idle noise, then allocates and page-touches a block
    sized from the live reading, and reports how much MemAvailable dropped.
    Skips (via pytest.skip) rather than returning if the idle band is already
    wide enough that no touch of this size could clear it even ideally -- that
    case means this attempt cannot answer the question, not that it failed one.
    """
    idle_low, idle_high, idle_band = _measure_idle_band(
        IDLE_READING_COUNT, IDLE_READING_INTERVAL_SECONDS
    )

    before = read_available_ram_bytes()
    assert before > 0

    block_bytes = before // AVAILABLE_RAM_TOUCH_DIVISOR
    required_drop = idle_band * IDLE_BAND_MARGIN_MULTIPLIER
    # The touch can drop MemAvailable by at most the amount it touches -- that is
    # the ideal case, every touched byte actually counted as no-longer-available.
    # If even that ideal case could not clear the box's own measured noise, no
    # outcome of this attempt could distinguish "live" from "noisy but frozen," so
    # the honest result is neither a pass nor a fail but a skip carrying the
    # numbers. On a machine busy enough that this triggers every run, this test
    # skips every run and guards nothing -- that cost is real, and the skip
    # reason is written so a permanently-skipping run is impossible to miss.
    if required_drop >= block_bytes:
        pytest.skip(
            f"idle band over {IDLE_READING_COUNT} readings ({IDLE_READING_INTERVAL_SECONDS}s "
            f"apart) was {idle_band} bytes ({idle_low}..{idle_high}); "
            f"{IDLE_BAND_MARGIN_MULTIPLIER}x that band is {required_drop} bytes, which a "
            f"touch of {block_bytes} bytes could not clear even in the ideal case "
            "(the whole touched block counted as no-longer-available); this box is too "
            "noisy right now for this measurement to distinguish a live reading from a "
            "frozen one"
        )

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
    del filler

    return {
        "idle_low": idle_low,
        "idle_high": idle_high,
        "idle_band": idle_band,
        "block_bytes": block_bytes,
        "required_drop": required_drop,
        "before": before,
        "after": after,
        "drop": drop,
    }


def _describe_attempt(label: str, attempt: dict[str, int]) -> str:
    return (
        f"{label}: idle band over {IDLE_READING_COUNT} readings "
        f"({IDLE_READING_INTERVAL_SECONDS}s apart) was {attempt['idle_band']} bytes "
        f"({attempt['idle_low']}..{attempt['idle_high']}); touching {attempt['block_bytes']} "
        f"bytes only dropped MemAvailable by {attempt['drop']} bytes "
        f"(before={attempt['before']}, after={attempt['after']}), which does not clear "
        f"{IDLE_BAND_MARGIN_MULTIPLIER}x that idle band ({attempt['required_drop']} bytes)"
    )


def test_counts_physical_cores_not_logical_ones_on_this_real_box():
    # Section 0: this box is 6 physical cores with SMT2 giving 12 logical. This is
    # the fact the whole module argues from, read from the real /proc/cpuinfo.
    facts = measure_hardware_facts()
    assert isinstance(facts, HardwareFacts)
    assert facts.logical_cpus == os.cpu_count()
    assert facts.physical_cores is not None, (
        "this box's real /proc/cpuinfo does publish topology; a None here would "
        "mean the parse regressed, not that the box changed"
    )
    assert facts.physical_cores < facts.logical_cpus, (
        "SMT is on here, so physical cores must be fewer than logical CPUs"
    )
    assert facts.physical_cores * 2 == facts.logical_cpus, "SMT2, per section 0"
    assert facts.physical_cores == 6, (
        "section 0 of the spec records 6 physical cores on this box; if this "
        "fails, the machine changed and the spec is stale -- do not adjust this "
        "assertion to make it pass"
    )


def test_parses_physical_core_count_from_cpuinfo_text_with_topology():
    # A pure-function test: no /proc/cpuinfo involved, just text in and a count
    # out, per Rule 7 -- the parse is tested independently of the read.
    assert parse_physical_core_count(CPUINFO_TEXT_WITH_TOPOLOGY) == 2


def test_parses_no_physical_core_count_without_topology_fields():
    # The case that motivated the change: a kernel or container whose cpuinfo
    # carries no "physical id" / "core id" lines has no honest physical-core
    # count. The old implementation returned os.cpu_count() here -- the doubled
    # logical count the whole module exists to refuse -- with nothing in the
    # return value distinguishing a guess from a measurement. This must be None,
    # never a fallback.
    assert parse_physical_core_count(CPUINFO_TEXT_WITHOUT_TOPOLOGY) is None


def test_count_physical_cores_delegates_to_the_real_cpuinfo_file():
    assert count_physical_cores() == parse_physical_core_count(
        hardware_facts.CPUINFO_PATH.read_text()
    )


def test_numa_nodes_is_none_when_the_sysfs_tree_is_not_there(monkeypatch):
    # Mirrors the physical-core case: an unmeasurable NUMA topology must come
    # back as None, not the old silent fallback to 1.
    monkeypatch.setattr(
        hardware_facts, "NUMA_NODE_ROOT", pathlib.Path("/nonexistent/numa/node/root")
    )
    assert measure_hardware_facts().numa_nodes is None


def test_a_missing_meminfo_field_names_itself_rather_than_a_bare_keyerror(
    durable_tmp_path, monkeypatch
):
    # The real case this guards: MemAvailable was added in Linux 3.14, so an
    # older kernel's /proc/meminfo omits it. A bare KeyError would say nothing
    # about which field, or where it was looked for -- this must say both.
    fake_meminfo = durable_tmp_path / "meminfo"
    fake_meminfo.write_text("MemTotal:       30791696 kB\nSwapTotal:             0 kB\n")
    monkeypatch.setattr(hardware_facts, "MEMINFO_PATH", fake_meminfo)

    with pytest.raises(MeminfoFieldMissing) as excinfo:
        read_available_ram_bytes()

    assert "MemAvailable" in str(excinfo.value)
    assert str(fake_meminfo) in str(excinfo.value)


def test_available_ram_is_a_live_reading_not_a_cached_one():
    # A test that only checks positivity before and after would pass against a
    # frozen constant -- Rule 8 inverted, a reading certified live that never
    # moved. So this measures the box's own idle noise first, then touches real
    # memory and requires the drop to clearly clear that noise -- not an assumed
    # fraction of what was touched (see IDLE_BAND_MARGIN_MULTIPLIER).
    #
    # A single attempt can fall short for a reason that has nothing to do with
    # whether the reading is live: the allocator sometimes declines to hand freed
    # pages back to the OS on a given run, so the *actual* drop undershoots the
    # ideal one even though the reading itself moved correctly. That is variance
    # in the instrument, not a fact about read_available_ram_bytes(), so one retry
    # is legitimate here in a way it would not be for a test of code under test.
    # This cannot mask a real regression: a frozen or cached reading has zero
    # drop on every attempt, so it fails both attempts deterministically -- there
    # is nothing intermittent about staleness for a retry to paper over.
    first = _attempt_live_ram_measurement()
    if first["drop"] > first["required_drop"]:
        return

    second = _attempt_live_ram_measurement()
    assert second["drop"] > second["required_drop"], (
        "two attempts, neither drop cleared its required threshold -- "
        + _describe_attempt("attempt 1", first)
        + "; "
        + _describe_attempt("attempt 2", second)
        + "; the reading did not distinguish the touch from its own idle noise on "
        "either attempt, so it is not a live measurement"
    )


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
