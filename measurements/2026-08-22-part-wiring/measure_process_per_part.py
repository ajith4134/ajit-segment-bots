"""Can this box actually run one OS process per part -- all 321 of them?

Section 1 of the runtime spec says a part is one OS process, and phase 0 measured a
single idle forkserver child at 0.76 MB PSS. That figure was taken before any part
existed. This measures the real thing: every part module imported, a control socket
per part, the part loop running, and the whole set alive at once.

Four questions, because each one can end the design on its own:

  1. What does the full set cost in memory? PSS, not RSS -- forkserver children
     share the parent's pages, and RSS counts every shared page in every child.
  2. What does it cost to switch them all on? Fork time is the switch-on cost the
     governor pays (2.78 ms measured in phase 0, for one child).
  3. Does turning them off actually release it (T-3)? Measured as the difference,
     not asserted.
  4. What does the file-descriptor budget look like -- one control socket per part
     plus the data-plane channels the wiring implies?

Run:  .venv/bin/python measurements/2026-08-22-part-wiring/measure_process_per_part.py
"""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import statistics
import time

from runtime.control_channel import (
    COMMAND_TURN_OFF,
    create_control_socket_pair,
    send_command,
)
from runtime.forkserver_launcher import apply_blas_thread_caps, spawn_part, start_forkserver
from runtime.part_process import run_part

# The forkserver preloads what every part imports, so those pages are shared rather
# than paid for once per child.
NARROW_PRELOAD = ("runtime.part_process", "runtime.control_channel", "runtime.part_declaration")

# Everything the parts import between them. A module preloaded into the forkserver
# is paged once and shared by every child; a module imported after the fork is
# private to that child and paid for 321 times. Which of those two the launcher
# does is the whole question this variant answers.
WIDE_PRELOAD = NARROW_PRELOAD + (
    "runtime.trading_types",
    "runtime.market_signal",
    "runtime.trade_intent",
    "runtime.bot_opinion",
    "runtime.risk_types",
    "runtime.learned_estimator",
    "runtime.rolling_statistics",
    "runtime.online_learner",
    "runtime.numeric_state",
    "runtime.state_store",
    "runtime.journal",
    "runtime.settings_reader",
)

# Phase 0 measured 12 kernel threads after an uncapped numpy import; one is the
# only safe number to fork from.
SINGLE_THREADED = 1

HEALTH_INTERVAL_SECONDS = 60.0
SETTLE_SECONDS = 2.0
LONG_SETTLE_SECONDS = 10.0
KIBIBYTE = 1024


def idle_as_a_part(part_module_name: str, part_attribute: str, control_socket) -> None:
    """One part process: import the part, then run its loop doing nothing.

    The tick is empty on purpose. This measures what a part costs to *exist* --
    its module, its declaration, its loop and its control socket. What it costs to
    work is the part's own business and is measured per part, not here.
    """
    module = importlib.import_module(part_module_name)
    declaration = getattr(module, part_attribute)
    run_part(
        declaration=declaration,
        control_socket=control_socket,
        do_one_tick=lambda: None,
        emit_health=lambda health: None,
        health_interval_seconds=HEALTH_INTERVAL_SECONDS,
    )


def list_part_modules() -> list[str]:
    """Every part module in the tree, as an importable name."""
    names = []
    for path in sorted(pathlib.Path("parts").rglob("*.py")):
        if path.name == "__init__.py":
            continue
        names.append(str(path.with_suffix("")).replace(os.sep, "."))
    return names


def read_proportional_set_size_kib(pid: int) -> int:
    """PSS for one process: its private pages plus its share of the shared ones."""
    rollup = pathlib.Path(f"/proc/{pid}/smaps_rollup")
    for line in rollup.read_text().splitlines():
        if line.startswith("Pss:"):
            return int(line.split()[1])
    raise ValueError(f"no Pss line in {rollup}")


def read_private_set_size_kib(pid: int) -> int:
    """The pages this process alone holds -- what actually frees when it exits."""
    rollup = pathlib.Path(f"/proc/{pid}/smaps_rollup")
    private = 0
    for line in rollup.read_text().splitlines():
        if line.startswith(("Private_Clean:", "Private_Dirty:")):
            private += int(line.split()[1])
    return private


def read_available_ram_kib() -> int:
    for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])
    raise ValueError("no MemAvailable in /proc/meminfo")


def count_open_descriptors(pid: int | str = "self") -> int:
    return len(list(pathlib.Path(f"/proc/{pid}/fd").iterdir()))


def read_meminfo_megabytes(field: str) -> float:
    for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
        if line.startswith(f"{field}:"):
            return round(int(line.split()[1]) / KIBIBYTE, 1)
    raise ValueError(f"no {field} in /proc/meminfo")


def measure_every_part_as_a_process(preload_modules: tuple[str, ...]) -> dict:
    apply_blas_thread_caps()
    module_names = list_part_modules()
    context = start_forkserver(preload_modules)

    available_before_kib = read_available_ram_kib()
    descriptors_before = count_open_descriptors()

    governor_ends = []
    processes = []
    fork_costs = []
    refused = []

    for module_name in module_names:
        governor_end, part_end = create_control_socket_pair()
        at = time.perf_counter()
        try:
            process = spawn_part(
                context=context,
                entry_point=idle_as_a_part,
                arguments=(module_name, "PART_DECLARATION", part_end),
                thread_ceiling=SINGLE_THREADED,
            )
        except Exception as failure:  # a refusal is a finding, not a crash
            refused.append(f"{module_name}: {type(failure).__name__}: {failure}")
            governor_end.close()
            part_end.close()
            continue
        fork_costs.append(time.perf_counter() - at)
        part_end.close()  # the child owns it now
        governor_ends.append(governor_end)
        processes.append(process)

    time.sleep(SETTLE_SECONDS)

    alive = [process for process in processes if process.is_alive()]
    proportional = []
    private = []
    for process in alive:
        try:
            proportional.append(read_proportional_set_size_kib(process.pid))
            private.append(read_private_set_size_kib(process.pid))
        except (FileNotFoundError, ProcessLookupError):
            pass

    available_with_parts_kib = read_available_ram_kib()
    descriptors_with_parts = count_open_descriptors()

    switch_off_at = time.perf_counter()
    for governor_end in governor_ends:
        try:
            send_command(governor_end, COMMAND_TURN_OFF, {})
        except OSError:
            pass
    for process in processes:
        process.join(timeout=30.0)
    switch_off_seconds = time.perf_counter() - switch_off_at

    still_running = [process.pid for process in processes if process.is_alive()]
    for governor_end in governor_ends:
        governor_end.close()

    time.sleep(SETTLE_SECONDS)
    available_after_kib = read_available_ram_kib()
    # T-3 says an off part releases its memory. Reclaim is not instant, so the
    # question is answered by reading twice, not by reading once and assuming.
    time.sleep(LONG_SETTLE_SECONDS)
    available_long_after_kib = read_available_ram_kib()

    return {
        "preload_modules": len(preload_modules),
        "parts_attempted": len(module_names),
        "parts_forked": len(processes),
        "parts_alive_together": len(alive),
        "forks_refused": refused,
        "fork_milliseconds_median": round(statistics.median(fork_costs) * 1e3, 3),
        "fork_milliseconds_p95": round(sorted(fork_costs)[int(len(fork_costs) * 0.95)] * 1e3, 3),
        "seconds_to_switch_all_on": round(sum(fork_costs), 3),
        "pss_megabytes_per_part_median": round(statistics.median(proportional) / KIBIBYTE, 3),
        "pss_megabytes_total": round(sum(proportional) / KIBIBYTE, 1),
        "private_megabytes_per_part_median": round(statistics.median(private) / KIBIBYTE, 3),
        "available_ram_megabytes_before": round(available_before_kib / KIBIBYTE, 1),
        "available_ram_megabytes_with_all_parts": round(available_with_parts_kib / KIBIBYTE, 1),
        "available_ram_megabytes_after_switch_off": round(available_after_kib / KIBIBYTE, 1),
        "megabytes_the_full_set_cost": round((available_before_kib - available_with_parts_kib) / KIBIBYTE, 1),
        "megabytes_returned_by_switching_off": round((available_after_kib - available_with_parts_kib) / KIBIBYTE, 1),
        "available_ram_megabytes_long_after_switch_off": round(available_long_after_kib / KIBIBYTE, 1),
        "megabytes_still_not_returned": round((available_before_kib - available_long_after_kib) / KIBIBYTE, 1),
        "page_cache_megabytes_now": read_meminfo_megabytes("Cached"),
        "descriptors_in_governor_before": descriptors_before,
        "descriptors_in_governor_with_all_parts": descriptors_with_parts,
        "seconds_to_switch_all_off": round(switch_off_seconds, 3),
        "parts_that_would_not_stop": still_running,
    }


def main() -> None:
    findings = {
        "narrow_preload": measure_every_part_as_a_process(NARROW_PRELOAD),
        "wide_preload": measure_every_part_as_a_process(WIDE_PRELOAD),
    }
    print(json.dumps(findings, indent=2))


if __name__ == "__main__":
    main()
