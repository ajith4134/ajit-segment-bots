"""Move a part into its own cgroup, and confirm it actually landed there.

A forkserver child inherits the forkserver's cgroup, and raw mkdir under the
systemd-owned session scope is refused -- systemd owns that subtree. So a part is
placed through the user bus rather than by hand-rolled cgroupfs, which is also
what gives it a per-part cpu.pressure file, the scarcity signal section 5 depends on.

The confirmation is the point of this module. StartTransientUnit queues an
asynchronous job and returns its object path, so a job that then fails to move the
PID still leaves the caller holding rc=0. Observed once in 88 attempts here: rc=0
in 7.6 ms while the child stayed in session-9.scope with memory.max unset, the real
outcome only in the user manager's journal. A part running outside its own scope is
a part the governor believes it has bounded and has not.
"""

from __future__ import annotations

import pathlib
import subprocess
import time
from dataclasses import dataclass

CGROUP_ROOT = pathlib.Path("/sys/fs/cgroup")
SYSTEMD_BUS_NAME = "org.freedesktop.systemd1"
SYSTEMD_OBJECT_PATH = "/org/freedesktop/systemd1"
SYSTEMD_MANAGER_INTERFACE = "org.freedesktop.systemd1.Manager"

# The files whose presence proves the part is bounded and observable.
SCOPE_LIMIT_FILES = ("memory.max", "cpu.weight", "pids.max", "cpu.pressure", "memory.pressure")


class PlacementNotConfirmed(RuntimeError):
    """The placement call was accepted but the process is not in the scope."""


@dataclass(frozen=True)
class ScopeLimits:
    """What the governor is bounding this part to.

    Every value here is decided by the governor from measurement -- hardware-scanner
    and part-appetite-meter -- never written in as a literal (RL-061). cpu.weight's
    range is [1, 10000] with a default of 100, and it matters only under contention.
    """

    memory_max_bytes: int
    cpu_weight: int
    pids_max: int | None = None
    cpu_quota_percent: int | None = None


def read_process_cgroup(pid: int) -> str:
    """The cgroup path a process is in right now, read from the kernel."""
    return pathlib.Path(f"/proc/{pid}/cgroup").read_text().strip().split("::")[1]


def has_process_landed_in_scope(pid: int, scope_name: str) -> bool:
    """Is this process in that scope? Read from /proc, never inferred from a return code."""
    try:
        return read_process_cgroup(pid).endswith(f"/{scope_name}.scope")
    except (OSError, IndexError):
        return False


def _build_transient_unit_call(pid: int, scope_name: str, limits: ScopeLimits) -> list[str]:
    """Build the busctl argv for StartTransientUnit.

    The signature is ssa(sv)a(sa(sv)): unit name, job mode, an array of properties,
    and an array of auxiliary units. busctl wants the *count of properties* before
    them, so they are built as whole properties and flattened once -- counting the
    flattened words instead is the easy way to send a malformed message that
    systemd rejects with a signature error.

    Each property is a name plus a variant. PIDs is au, an array of uint32, so its
    variant carries its own length. The rest are t, uint64.
    """
    properties: list[list[str]] = [
        ["PIDs", "au", "1", str(pid)],
        ["MemoryMax", "t", str(limits.memory_max_bytes)],
        ["CPUWeight", "t", str(limits.cpu_weight)],
    ]
    if limits.pids_max is not None:
        properties.append(["TasksMax", "t", str(limits.pids_max)])
    if limits.cpu_quota_percent is not None:
        # systemd takes CPUQuotaPerSecUSec: microseconds of CPU per wall second.
        # 100% of one CPU is 1 000 000 us, so a percentage is worth 10 000 us.
        microseconds_per_percent = 10_000
        properties.append(
            ["CPUQuotaPerSecUSec", "t", str(limits.cpu_quota_percent * microseconds_per_percent)]
        )
    flattened = [word for prop in properties for word in prop]
    return [
        "busctl", "--user", "call", SYSTEMD_BUS_NAME, SYSTEMD_OBJECT_PATH,
        SYSTEMD_MANAGER_INTERFACE, "StartTransientUnit", "ssa(sv)a(sa(sv))",
        f"{scope_name}.scope", "fail", str(len(properties)), *flattened, "0",
    ]


def place_process_in_scope(
    pid: int,
    scope_name: str,
    limits: ScopeLimits,
    confirmation_deadline_seconds: float,
    poll_interval_seconds: float,
) -> pathlib.Path:
    """Place a live PID in its own transient scope and confirm it arrived.

    Returns the cgroup directory. Raises PlacementNotConfirmed rather than
    returning a part the governor cannot bound.
    """
    call = _build_transient_unit_call(pid, scope_name, limits)
    completed = subprocess.run(call, capture_output=True, text=True)

    deadline = time.monotonic() + confirmation_deadline_seconds
    while time.monotonic() < deadline:
        if has_process_landed_in_scope(pid, scope_name):
            return CGROUP_ROOT / read_process_cgroup(pid).lstrip("/")
        time.sleep(poll_interval_seconds)

    try:
        observed = read_process_cgroup(pid)
    except OSError:
        observed = "the process no longer exists"
    raise PlacementNotConfirmed(
        f"{scope_name}.scope was requested for pid {pid} and busctl returned "
        f"{completed.returncode}, but /proc says the process is in {observed} after "
        f"{confirmation_deadline_seconds}s. StartTransientUnit queues an asynchronous "
        f"job, so its return code is not evidence the move happened. busctl stderr: "
        f"{completed.stderr.strip()[:200] or 'empty'}. The real reason is in "
        f"`journalctl --user -u {scope_name}.scope`."
    )


def read_scope_limits_in_effect(cgroup_directory: pathlib.Path) -> dict[str, str]:
    """Read back what the kernel is actually enforcing, for the switch record."""
    in_effect: dict[str, str] = {}
    for filename in SCOPE_LIMIT_FILES:
        try:
            in_effect[filename] = (cgroup_directory / filename).read_text().strip()
        except OSError:
            in_effect[filename] = "unreadable"
    return in_effect
