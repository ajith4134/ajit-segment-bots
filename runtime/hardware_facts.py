"""What this machine actually has, measured now rather than remembered.

RL-061 in its clearest form: the governor's capacity numbers are read from the
kernel, never written in. Section 0 records what this box measured on 2026-08-20,
and this module is how that stays true after a resize.

The important distinction is physical cores against logical CPUs. This box is 6
physical with SMT2 giving 12, and capacity planning treats 6 as the ceiling --
os.cpu_count() would say 12 and let the governor admit twice the real work. E2 is
GCP's cost-optimised family, so even the 6 carry host-level variance nothing here
can observe; the ceiling is a ceiling, not a promise.

A fact this module cannot measure comes back as None, never as a guessed number.
A kernel or container whose /proc/cpuinfo publishes no topology has no honest
answer for "how many physical cores" -- falling back to the logical count would
silently hand the governor the doubled-capacity number this module exists to
refuse, and nothing in a plain int would tell a consumer the number was guessed
rather than measured. None is that tell, and Task 14's tile renders it as
NOT MEASURED rather than as a number nobody measured (Rule 8).
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


class MeminfoFieldMissing(LookupError):
    """A required /proc/meminfo field was not present in the file that was read.

    MemAvailable was added in Linux 3.14 (2014); a kernel older than that, or a
    container whose /proc is a restricted view, may not publish it. This is kept
    as a loud failure rather than a default -- a missing MemAvailable is a fact
    about the machine, and substituting MemFree or zero for it would be exactly
    the kind of guess this module exists to refuse.
    """


@dataclass(frozen=True)
class HardwareFacts:
    """One measurement of this machine, with the moment it was taken.

    physical_cores, logical_cpus, and numa_nodes are int | None. None means the
    fact could not be measured on this machine -- a kernel or container that
    publishes no CPU topology, an interpreter that cannot report os.cpu_count(),
    a NUMA sysfs tree that is not mounted -- and never stands in for a default or
    a guess. A caller that needs one of these to plan capacity and finds None
    must refuse to plan rather than treat it as zero, as the other field's
    value, or as any other substitute; that is the same fail-closed shape as
    every other guard in this substrate.
    """

    physical_cores: int | None
    logical_cpus: int | None
    total_ram_bytes: int
    available_ram_bytes: int
    swap_total_bytes: int
    numa_nodes: int | None
    measured_at_ns: int


def _read_meminfo_kibibytes() -> dict[str, int]:
    fields: dict[str, int] = {}
    for line in MEMINFO_PATH.read_text().splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            fields[name] = int(parts[0])
    return fields


def _require_meminfo_field(meminfo: dict[str, int], field: str) -> int:
    """The named /proc/meminfo field, or a loud, specific failure -- never a guess."""
    try:
        return meminfo[field]
    except KeyError as exc:
        raise MeminfoFieldMissing(
            f"{field!r} was not found in {MEMINFO_PATH} -- expected it among the "
            f"fields this machine's kernel publishes ({sorted(meminfo)}); a "
            "pre-3.14 kernel or a restricted /proc view can omit it, and this "
            "module refuses to substitute a default for a number it cannot read"
        ) from exc


def parse_physical_core_count(cpuinfo_text: str) -> int | None:
    """How many distinct (physical id, core id) pairs this cpuinfo text names.

    SMT siblings collapse to one physical core. Returns None -- not a fallback
    to the logical count -- when the text publishes no topology fields at all,
    which is the case a kernel without physical id / core id lines, or a
    container's restricted /proc, produces. Pure function: takes text, returns
    a count or None, so it is testable without a fake /proc/cpuinfo file.
    """
    cores: set[tuple[str, str]] = set()
    package = core = None
    for line in cpuinfo_text.splitlines():
        name, _, value = line.partition(":")
        name, value = name.strip(), value.strip()
        if name == "physical id":
            package = value
        elif name == "core id":
            core = value
            if package is not None:
                cores.add((package, core))
    return len(cores) or None


def count_physical_cores() -> int | None:
    """Physical cores on this real machine, or None if its cpuinfo has no topology."""
    return parse_physical_core_count(CPUINFO_PATH.read_text())


def read_available_ram_bytes() -> int:
    """MemAvailable, read live. This is what off-state-verifier watches move."""
    meminfo = _read_meminfo_kibibytes()
    return _require_meminfo_field(meminfo, "MemAvailable") * KIBIBYTE


def measure_hardware_facts() -> HardwareFacts:
    """Take one reading of everything the governor is allowed to plan against."""
    meminfo = _read_meminfo_kibibytes()
    numa_node_count = (
        len(list(NUMA_NODE_ROOT.glob("node[0-9]*"))) if NUMA_NODE_ROOT.is_dir() else 0
    )
    return HardwareFacts(
        physical_cores=count_physical_cores(),
        logical_cpus=os.cpu_count(),
        total_ram_bytes=_require_meminfo_field(meminfo, "MemTotal") * KIBIBYTE,
        available_ram_bytes=_require_meminfo_field(meminfo, "MemAvailable") * KIBIBYTE,
        swap_total_bytes=meminfo.get("SwapTotal", 0) * KIBIBYTE,
        numa_nodes=numa_node_count or None,
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
