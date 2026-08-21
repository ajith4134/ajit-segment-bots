"""What this machine actually has, measured now rather than remembered.

RL-061 in its clearest form: the governor's capacity numbers are read from the
kernel, never written in. Section 0 records what this box measured on 2026-08-20,
and this module is how that stays true after a resize.

The important distinction is physical cores against logical CPUs. This box is 6
physical with SMT2 giving 12, and capacity planning treats 6 as the ceiling --
os.cpu_count() would say 12 and let the governor admit twice the real work. E2 is
GCP's cost-optimised family, so even the 6 carry host-level variance nothing here
can observe; the ceiling is a ceiling, not a promise.
"""

from __future__ import annotations

import os
import pathlib
import time
from dataclasses import dataclass

from runtime.scope_placer import CGROUP_ROOT, read_process_cgroup

CPUINFO_PATH = pathlib.Path("/proc/cpuinfo")
MEMINFO_PATH = pathlib.Path("/proc/meminfo")
NUMA_NODE_ROOT = pathlib.Path("/sys/devices/system/node")

KIBIBYTE = 1024


@dataclass(frozen=True)
class HardwareFacts:
    """One measurement of this machine, with the moment it was taken."""

    physical_cores: int
    logical_cpus: int
    total_ram_bytes: int
    available_ram_bytes: int
    swap_total_bytes: int
    numa_nodes: int
    measured_at_ns: int


def _read_meminfo_kibibytes() -> dict[str, int]:
    fields: dict[str, int] = {}
    for line in MEMINFO_PATH.read_text().splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            fields[name] = int(parts[0])
    return fields


def count_physical_cores() -> int:
    """Count distinct (physical id, core id) pairs -- SMT siblings collapse to one."""
    cores: set[tuple[str, str]] = set()
    package = core = None
    for line in CPUINFO_PATH.read_text().splitlines():
        name, _, value = line.partition(":")
        name, value = name.strip(), value.strip()
        if name == "physical id":
            package = value
        elif name == "core id":
            core = value
            if package is not None:
                cores.add((package, core))
    # A kernel that does not publish topology leaves this empty; fall back to the
    # logical count rather than returning zero, and let the tile show the difference.
    return len(cores) or (os.cpu_count() or 1)


def read_available_ram_bytes() -> int:
    """MemAvailable, read live. This is what off-state-verifier watches move."""
    return _read_meminfo_kibibytes()["MemAvailable"] * KIBIBYTE


def measure_hardware_facts() -> HardwareFacts:
    """Take one reading of everything the governor is allowed to plan against."""
    meminfo = _read_meminfo_kibibytes()
    numa_nodes = len(list(NUMA_NODE_ROOT.glob("node[0-9]*"))) if NUMA_NODE_ROOT.is_dir() else 1
    return HardwareFacts(
        physical_cores=count_physical_cores(),
        logical_cpus=os.cpu_count() or 1,
        total_ram_bytes=meminfo["MemTotal"] * KIBIBYTE,
        available_ram_bytes=meminfo["MemAvailable"] * KIBIBYTE,
        swap_total_bytes=meminfo.get("SwapTotal", 0) * KIBIBYTE,
        numa_nodes=numa_nodes or 1,
        measured_at_ns=time.time_ns(),
    )


def read_own_cgroup_directory() -> pathlib.Path:
    """Where this process's cgroup files live.

    Reuses scope_placer.read_process_cgroup rather than re-parsing
    /proc/self/cgroup a third time in this codebase -- page_cache_discipline
    already does its own read of that file for a different purpose (memory.peak),
    and scope_placer's read_process_cgroup(pid) plus CGROUP_ROOT is the exact
    composition this function needs, already exercised by test_scope_placer.py.
    """
    return CGROUP_ROOT / read_process_cgroup(os.getpid()).lstrip("/")


def read_cgroup_pressure(cgroup_directory: pathlib.Path, resource: str) -> dict[str, float]:
    """Parse one cgroup's pressure file into flat named averages.

    The per-cgroup 'full' line measures every task in the cgroup stalled at once and
    is the signal section 5 admits against. The system-wide 'full' line is zero by
    definition, so /proc/pressure/<resource> is the wrong file for this purpose.
    """
    readings: dict[str, float] = {}
    for line in (cgroup_directory / f"{resource}.pressure").read_text().splitlines():
        fields = line.split()
        if not fields:
            continue
        scope = fields[0]
        for field in fields[1:]:
            key, _, value = field.partition("=")
            readings[f"{scope}_{key}"] = float(value)
    return readings
