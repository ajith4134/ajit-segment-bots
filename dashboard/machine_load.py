#!/usr/bin/env python3
"""What this server is actually spending, read from /proc on every call.

Nothing here is estimated and nothing is cached from a config file. CPU comes
from `/proc/stat`, memory from `/proc/meminfo`, load from `/proc/loadavg`, disk
from `statvfs` of the directory the state actually lives in, and per-part cost
from `/proc/<pid>/stat` for pids the spine's own supervisor log says it started.

**A CPU percentage is a rate, so it needs two samples.** `/proc/stat` counts
jiffies since boot; one reading of it says how busy the machine has been since it
was switched on, which is very nearly never the question. So the first call
reports `NOT MEASURED` rather than a number, exactly as the part activity reader
does -- a figure that looks like a measurement and is an average over three weeks
is worse than an honest gap (Rule 8).

Memory is not a rate and is reported on the first call.

**`MemAvailable`, not `MemFree`.** Free memory on a busy Linux box is close to
zero by design, because the kernel spends the rest on page cache it will hand
back the moment anything asks. A board reporting "free" would show this machine
permanently at the edge of exhaustion while it was comfortable. Available is the
kernel's own estimate of what a new allocation could actually get.
"""

from __future__ import annotations

import os
import pathlib
import sys
import time
from dataclasses import dataclass

PROJECT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

STAT_PATH = pathlib.Path("/proc/stat")
MEMINFO_PATH = pathlib.Path("/proc/meminfo")
LOADAVG_PATH = pathlib.Path("/proc/loadavg")

NOT_MEASURED = "NOT MEASURED"
BYTES_PER_KIBIBYTE = 1024

# /proc/stat's cpu line, in the order the kernel writes it. Named rather than
# indexed by number so the two that are not "busy" can be said out loud.
CPU_FIELDS = (
    "user", "nice", "system", "idle", "iowait",
    "irq", "softirq", "steal", "guest", "guest_nice",
)
# Idle and iowait are the machine having nothing to do and the machine waiting on
# a disk. Neither is work, and counting iowait as busy is the usual way a board
# reports 100% CPU on a box that is asleep waiting for a read.
IDLE_FIELDS = ("idle", "iowait")


@dataclass(frozen=True)
class CpuSample:
    """One reading of /proc/stat, per cpu and in total."""

    total: dict[str, int]
    per_cpu: dict[str, dict[str, int]]
    taken_at_ns: int


def read_cpu_sample(now_ns=None) -> CpuSample:
    total: dict[str, int] = {}
    per_cpu: dict[str, dict[str, int]] = {}
    for line in STAT_PATH.read_text().splitlines():
        if not line.startswith("cpu"):
            continue
        name, _, rest = line.partition(" ")
        values = [int(v) for v in rest.split()]
        fields = dict(zip(CPU_FIELDS, values))
        if name == "cpu":
            total = fields
        else:
            per_cpu[name] = fields
    return CpuSample(
        total=total, per_cpu=per_cpu, taken_at_ns=now_ns or time.time_ns()
    )


def busy_fraction_between(before: dict[str, int], after: dict[str, int]) -> float | None:
    """How much of the elapsed CPU time was work, between two /proc/stat readings.

    None when no time passed between the samples: dividing by that would be a
    number invented out of a zero, and no reading at all is the honest answer.
    """
    elapsed = sum(after.values()) - sum(before.values())
    if elapsed <= 0:
        return None
    idle = sum(after.get(f, 0) - before.get(f, 0) for f in IDLE_FIELDS)
    return max(0.0, min(1.0, (elapsed - idle) / elapsed))


def read_memory() -> dict:
    """What this machine's memory is doing, from /proc/meminfo."""
    fields: dict[str, int] = {}
    for line in MEMINFO_PATH.read_text().splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            fields[name] = int(parts[0]) * BYTES_PER_KIBIBYTE

    total = fields.get("MemTotal")
    available = fields.get("MemAvailable")
    swap_total = fields.get("SwapTotal", 0)
    swap_free = fields.get("SwapFree", 0)
    used = None if total is None or available is None else total - available
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": used,
        "used_fraction": None if not total or used is None else used / total,
        "cached_bytes": fields.get("Cached"),
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_total - swap_free,
        "is_measured": total is not None and available is not None,
        "proof": f"{MEMINFO_PATH}: MemTotal and MemAvailable",
    }


def read_load_average() -> dict:
    """The kernel's own run-queue averages, and how they compare to the core count."""
    text = LOADAVG_PATH.read_text().split()
    one, five, fifteen = (float(v) for v in text[:3])
    cpus = os.cpu_count()
    return {
        "one_minute": one,
        "five_minutes": five,
        "fifteen_minutes": fifteen,
        "logical_cpus": cpus,
        # Load above the core count means work is queueing rather than running.
        "per_cpu": None if not cpus else one / cpus,
        "proof": str(LOADAVG_PATH),
    }


def read_disk(path: pathlib.Path) -> dict:
    """Space where the state actually lives, not where the code happens to sit."""
    try:
        stat = os.statvfs(path)
    except OSError as failure:
        return {"is_measured": False, "proof": f"{path} could not be read: {failure}"}
    total = stat.f_blocks * stat.f_frsize
    free = stat.f_bavail * stat.f_frsize
    return {
        "path": str(path),
        "total_bytes": total,
        "free_bytes": free,
        "used_bytes": total - free,
        "used_fraction": None if not total else (total - free) / total,
        "is_measured": True,
        "proof": f"statvfs({path})",
    }


def read_part_costs(previous: dict[str, tuple[int, int]] | None, elapsed_ns: int | None) -> tuple:
    """Per-part CPU and memory, for the pids the spine says it started.

    The supervisor log says what was started and `/proc/<pid>` says what is still
    there; trusting the log alone would report a part that died an hour ago as
    spending CPU. Same join the trade board's *Parts alive* tile uses, so the two
    can never disagree about which parts are running.
    """
    from build_trade_board import read_running_parts

    ticks_per_second = os.sysconf("SC_CLK_TCK")
    page_size = os.sysconf("SC_PAGE_SIZE")
    running = read_running_parts()

    costs = []
    current: dict[str, tuple[int, int]] = {}
    for part_id, pid in sorted(running.items()):
        try:
            fields = pathlib.Path(f"/proc/{pid}/stat").read_text()
            # The comm field is parenthesised and may itself contain spaces, so the
            # split starts after the last ')' rather than at the second field.
            after_comm = fields[fields.rindex(")") + 2 :].split()
            utime, stime = int(after_comm[11]), int(after_comm[12])
            resident_pages = int(after_comm[21])
        except (OSError, ValueError, IndexError):
            # A part that exited between the log read and this one. Not an error,
            # and not a zero either -- it simply is not measured this round.
            continue
        jiffies = utime + stime
        current[part_id] = (jiffies, resident_pages)
        busy = None
        if previous and part_id in previous and elapsed_ns:
            spent = (jiffies - previous[part_id][0]) / ticks_per_second
            busy = max(0.0, spent / (elapsed_ns / 1e9))
        costs.append(
            {
                "part_id": part_id,
                "pid": pid,
                "cpu_fraction": busy,
                "resident_bytes": resident_pages * page_size,
                "is_measured": busy is not None,
            }
        )
    return costs, current


class MachineLoadReader:
    """Reads the machine's load, remembering the last sample so a rate can exist.

    Held as an object for the same reason the activity reader is: a CPU
    percentage is a difference between two moments, and the second one has to be
    compared against something this process actually saw.
    """

    def __init__(self, state_path: pathlib.Path | None = None) -> None:
        self._previous_cpu: CpuSample | None = None
        self._previous_parts: dict[str, tuple[int, int]] | None = None
        self._state_path = state_path

    def read_machine_load(self) -> dict:
        now_ns = time.time_ns()
        sample = read_cpu_sample(now_ns)
        elapsed_ns = None
        total_busy = None
        per_cpu_busy = []

        if self._previous_cpu is not None:
            elapsed_ns = sample.taken_at_ns - self._previous_cpu.taken_at_ns
            total_busy = busy_fraction_between(self._previous_cpu.total, sample.total)
            for name in sorted(sample.per_cpu, key=lambda n: int(n[3:] or 0)):
                before = self._previous_cpu.per_cpu.get(name)
                per_cpu_busy.append(
                    {
                        "cpu": name,
                        "busy_fraction": None if before is None
                        else busy_fraction_between(before, sample.per_cpu[name]),
                    }
                )

        part_costs, current_parts = read_part_costs(self._previous_parts, elapsed_ns)
        self._previous_cpu = sample
        self._previous_parts = current_parts

        return {
            "cpu": {
                "busy_fraction": total_busy,
                "per_cpu": per_cpu_busy,
                "logical_cpus": os.cpu_count(),
                "is_measured": total_busy is not None,
                "over_seconds": None if elapsed_ns is None else elapsed_ns / 1e9,
                # Said out loud because a board that counts iowait as busy shows
                # 100% CPU on a machine that is asleep waiting for a disk.
                "proof": f"{STAT_PATH}, busy = everything but idle and iowait",
            },
            "memory": read_memory(),
            "load": read_load_average(),
            "disk": read_disk(self._resolve_state_path()),
            "parts": sorted(
                part_costs,
                key=lambda c: (c["cpu_fraction"] or 0, c["resident_bytes"]),
                reverse=True,
            ),
            "taken_at_ns": now_ns,
        }

    def _resolve_state_path(self) -> pathlib.Path:
        """Where the tape and the journals live, from settings rather than guessed."""
        if self._state_path is not None:
            return self._state_path
        try:
            from runtime.settings_reader import load_settings_document, settings_directory

            document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
            path = pathlib.Path(str(document.read_value("tape_root"))).expanduser()
            self._state_path = path if path.exists() else path.parent
        except Exception:
            self._state_path = pathlib.Path.home()
        return self._state_path


if __name__ == "__main__":
    reader = MachineLoadReader()
    reader.read_machine_load()
    time.sleep(2)
    load = reader.read_machine_load()
    cpu, memory, disk = load["cpu"], load["memory"], load["disk"]
    gib = 1024 ** 3
    print(f"cpu       {cpu['busy_fraction']:.1%} of {cpu['logical_cpus']} logical cpus"
          if cpu["is_measured"] else f"cpu       {NOT_MEASURED}")
    print(f"memory    {memory['used_bytes'] / gib:.2f} / {memory['total_bytes'] / gib:.2f} GiB "
          f"({memory['used_fraction']:.1%})")
    print(f"load      {load['load']['one_minute']:.2f} "
          f"({load['load']['per_cpu']:.2f} per cpu)")
    print(f"disk      {disk['used_bytes'] / gib:.1f} / {disk['total_bytes'] / gib:.1f} GiB "
          f"({disk['used_fraction']:.1%}) at {disk['path']}")
    print(f"parts     {len(load['parts'])} measured")
    for cost in load["parts"][:8]:
        share = f"{cost['cpu_fraction']:.2%}" if cost["is_measured"] else NOT_MEASURED
        print(f"  {cost['part_id']:<34} cpu {share:>8}  rss {cost['resident_bytes'] / 1024 ** 2:6.1f} MiB")
