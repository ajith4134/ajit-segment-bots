#!/usr/bin/env python3
"""Measurement harness: standard CPython 3.14 vs free-threaded 3.14t, for the
segment-bots part-runtime architecture (one OS process per part, started via
multiprocessing forkserver, moved into its own systemd user scope).

Runs identically on both builds. Detects which build it is on via
sysconfig.get_config_var('Py_GIL_DISABLED') and sys._is_gil_enabled() and
reports raw numbers only -- no verdicts, no opinions. The caller (or a
separate diff step) decides what the numbers mean.

Usage:
    <venv>/bin/python measure_interpreter.py --json
    <venv>/bin/python measure_interpreter.py --json --parts 12 --items 200000 --threads 6

Every measurement is wrapped so a single failed section records
{"error": "..."} under its key and every other section still runs.

RL-061 note: every tunable that controls HOW MUCH work this script does
(part count, item count, thread/process count, timeouts) is an argparse
parameter with a documented default -- there are no bare tuning literals in
the control flow. The synthetic OHLCV generator uses small numeric literals
to *shape* fake market data (price step size, spread size); those are data
fixture constants, not measurement tuning, and are called out as such below.
"""

from __future__ import annotations

import os

# RULE (task spec): these must be set to "1" before numpy is imported
# anywhere -- in this process or in any forkserver-spawned child, which
# inherits this environment because forkserver's server process is only
# started (and its env captured) once this module has already set these.
# The part-runtime architecture pins BLAS threading to 1 per part and lets
# the governor -- not BLAS -- own core allocation; this harness measures
# under that same pin so results transfer to the real system.
for _var in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ[_var] = "1"

import argparse
import datetime
import importlib
import importlib.metadata
import json
import math
import multiprocessing as mp
import multiprocessing.forkserver as _forkserver_mod
import queue
import random
import statistics
import sys
import sysconfig
import threading
import time
import traceback

# --------------------------------------------------------------------------
# Module-level worker/target functions.
#
# multiprocessing's forkserver start method pickles process/pool targets by
# qualified name, so every function used as a Process(target=...) or
# Pool.map(...) callable MUST be defined at module level (not a closure/
# lambda/nested def) or the child cannot locate it. All such targets live
# in this block.
# --------------------------------------------------------------------------


def _read_proc_stat_num_threads(pid: int) -> int:
    """Field 20 (num_threads) of /proc/<pid>/stat.

    The comm field (field 2) is wrapped in parens and may itself contain
    spaces or parens, so we split at the LAST ')' rather than on whitespace,
    per proc(5). Fields after that point are 1-indexed from field 3 (state)
    onward, so num_threads (field 20) is index 20 - 3 = 17 in that tail.
    """
    with open(f"/proc/{pid}/stat", "r") as fh:
        content = fh.read()
    idx = content.rfind(")")
    tail = content[idx + 1 :].split()
    return int(tail[17])


def _read_smaps_rollup_kb(pid: int) -> dict:
    """Rss and Pss, in KB, from /proc/<pid>/smaps_rollup."""
    out = {}
    with open(f"/proc/{pid}/smaps_rollup", "r") as fh:
        for line in fh:
            if line.startswith("Rss:"):
                out["rss_kb"] = int(line.split()[1])
            elif line.startswith("Pss:"):
                out["pss_kb"] = int(line.split()[1])
    return out


def _read_meminfo_field_kb(field_name: str):
    with open("/proc/meminfo", "r") as fh:
        for line in fh:
            if line.startswith(field_name + ":"):
                return int(line.split()[1])
    return None


def _forkserver_part_worker(result_queue) -> None:
    """(b) child target: report whether the forkserver preload actually
    landed numpy in this child's sys.modules before any import statement
    ran here, this child's own OS thread count, and whether the GIL is
    enabled in this child (free-threading is a per-interpreter-process
    property; a forkserver child inherits it from the server, but a
    non-opted-in C-extension import could still flip it back on)."""
    try:
        already = "numpy" in sys.modules
        num_threads = _read_proc_stat_num_threads(os.getpid())
        is_gil_enabled = getattr(sys, "_is_gil_enabled", lambda: True)
        result_queue.put(
            {
                "numpy_preloaded": already,
                "thread_count": num_threads,
                "gil_enabled": is_gil_enabled(),
            }
        )
    except Exception as exc:  # noqa: BLE001 - report, never crash the run
        try:
            result_queue.put({"error": repr(exc)})
        except Exception:
            pass


def _idle_numpy_part_worker(ready_event, stop_event) -> None:
    """(e)/(f) child target: import numpy (the realistic per-part cost),
    signal readiness, then block until told to exit -- an idle part sitting
    on its own RSS/PSS, exactly the state the governor holds a running-but-
    quiescent part in."""
    try:
        import numpy  # noqa: F401
    except Exception:
        pass
    ready_event.set()
    stop_event.wait()


def _aggregate_stream(items):
    """The glue workload (d): pure-Python object work with no numpy
    involved -- build a dict from a tuple, append to a list, maintain a
    running high/low/close. This is exactly the class of work free-
    threading's documented benefit (in-process multi-threaded CPU scaling)
    could apply to, and exactly the class of work T-1/T-2 push out of a
    single process and onto the governor's process-level parallelism
    instead."""
    bars = []
    append = bars.append
    running_high = float("-inf")
    running_low = float("inf")
    running_close = None
    for ts, o, h, l, c, v in items:
        append({"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v})
        if h > running_high:
            running_high = h
        if l < running_low:
            running_low = l
        running_close = c
    return len(bars), running_high, running_low, running_close


def _glue_process_worker(chunk):
    """(d)(iii) Pool.map target: aggregate one chunk in a forkserver child,
    return only the count (the dicts/floats stay in the child; only the
    small int crosses the pickle boundary, so this measures compute cost,
    not IPC serialization cost)."""
    return _aggregate_stream(chunk)[0]


# --------------------------------------------------------------------------
# Synthetic OHLCV stream generator.
#
# These numeric literals shape the FAKE data (price step size, spread
# width, volume range) -- they are fixture constants, not measurement
# tuning parameters. Reproducibility is controlled by --seed (an argparse
# parameter), and stream LENGTH is controlled by --items (also argparse).
# --------------------------------------------------------------------------


def _generate_ohlcv_stream(n_items: int, seed: int):
    rng = random.Random(seed)
    items = []
    price = 100.0
    ts0 = 1_700_000_000  # arbitrary fixed epoch anchor for reproducible timestamps
    bar_seconds = 60
    for i in range(n_items):
        drift = rng.uniform(-0.5, 0.5)
        open_p = price
        close_p = max(0.01, open_p + drift)
        high_p = max(open_p, close_p) + rng.uniform(0.0, 0.3)
        low_p = max(0.01, min(open_p, close_p) - rng.uniform(0.0, 0.3))
        volume = rng.uniform(0.1, 50.0)
        ts = ts0 + i * bar_seconds
        items.append((ts, open_p, high_p, low_p, close_p, volume))
        price = close_p
    return items


def _chunk_list(items, k: int):
    n = len(items)
    if k <= 0 or n == 0:
        return [items]
    size = math.ceil(n / k)
    chunks = [items[i : i + size] for i in range(0, n, size)]
    return chunks or [[]]


def _percentile(data, pct: float):
    if not data:
        return None
    s = sorted(data)
    k = (len(s) - 1) * pct
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def _discover_other_importable_modules(exclude=frozenset()):
    """Every top-level importable module name from every installed
    distribution in this venv, best-effort, excluding `exclude`.

    Prefers each distribution's declared top_level.txt; falls back to the
    distribution name (hyphens -> underscores) when that file is absent.
    Not a general-purpose package resolver -- a best-effort discovery of
    "everything installed in this venv" for section (a).
    """
    names = set()
    try:
        for dist in importlib.metadata.distributions():
            top_level = None
            try:
                top_level = dist.read_text("top_level.txt")
            except Exception:
                top_level = None
            if top_level:
                for line in top_level.splitlines():
                    line = line.strip()
                    if line and not line.startswith("_"):
                        names.add(line)
            else:
                raw = (dist.metadata.get("Name") or "").replace("-", "_")
                if raw and not raw.startswith("_"):
                    names.add(raw)
    except Exception:
        pass
    names -= set(exclude)
    names = {n for n in names if n.isidentifier()}
    return sorted(names)


# --------------------------------------------------------------------------
# Measurement sections (a)-(f)
# --------------------------------------------------------------------------


def measure_build_identity() -> dict:
    """(a) Build identity and GIL state before/after numpy, and after every
    other installed package this venv has."""
    result = {}
    is_gil_enabled = getattr(sys, "_is_gil_enabled", None)
    result["python_version"] = sys.version
    result["python_version_info"] = list(sys.version_info)
    result["py_gil_disabled_config"] = sysconfig.get_config_var("Py_GIL_DISABLED")
    result["has_is_gil_enabled"] = is_gil_enabled is not None
    result["gil_enabled_before_any_import"] = (
        is_gil_enabled() if is_gil_enabled else None
    )

    try:
        import numpy  # noqa: PLC0415

        result["numpy_version"] = numpy.__version__
        result["gil_enabled_after_numpy_import"] = (
            is_gil_enabled() if is_gil_enabled else None
        )
    except Exception as exc:  # noqa: BLE001
        result["numpy_import_error"] = repr(exc)

    other = {}
    for mod_name in _discover_other_importable_modules(exclude={"numpy"}):
        try:
            importlib.import_module(mod_name)
            other[mod_name] = {
                "imported": True,
                "gil_enabled_after": is_gil_enabled() if is_gil_enabled else None,
            }
        except Exception as exc:  # noqa: BLE001
            other[mod_name] = {"imported": False, "error": repr(exc)}
    result["other_installed_packages"] = other
    result["gil_enabled_final"] = is_gil_enabled() if is_gil_enabled else None
    return result


def measure_forkserver_viability(n_parts: int, queue_timeout: float, join_timeout: float) -> dict:
    """(b) + (c): forkserver_preload(['numpy']), start N parts serially,
    each reports numpy-preloaded / thread-count / gil-enabled; record the
    per-part fork latency distribution; and report the forkserver parent's
    own OS thread count (the fork-safety invariant: forkserver's main()
    loop must stay single-threaded on both builds for `fork()` inside it
    to remain safe)."""
    result = {"n_parts": n_parts}
    try:
        ctx = mp.get_context("forkserver")
        try:
            ctx.set_forkserver_preload(["numpy"])
            result["preload_set"] = True
        except Exception as exc:  # noqa: BLE001
            result["preload_set"] = False
            result["preload_error"] = repr(exc)

        latencies = []
        children = []
        for _ in range(n_parts):
            q = ctx.Queue()
            t0 = time.perf_counter()
            p = ctx.Process(target=_forkserver_part_worker, args=(q,))
            p.start()
            t1 = time.perf_counter()
            latencies.append(t1 - t0)
            try:
                report = q.get(timeout=queue_timeout)
            except Exception as exc:  # noqa: BLE001
                report = {"error": f"queue.get failed: {exc!r}"}
            p.join(timeout=join_timeout)
            report["exitcode"] = p.exitcode
            children.append(report)
            q.close()
            q.join_thread()

        result["fork_latency_seconds"] = {
            "median": statistics.median(latencies) if latencies else None,
            "p95": _percentile(latencies, 0.95),
            "min": min(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
            "n": len(latencies),
            "all": latencies,
        }
        result["children"] = children

        try:
            fs_pid = _forkserver_mod._forkserver._forkserver_pid  # noqa: SLF001
            result["forkserver_pid"] = fs_pid
            result["forkserver_parent_thread_count"] = (
                _read_proc_stat_num_threads(fs_pid) if fs_pid else None
            )
        except Exception as exc:  # noqa: BLE001
            result["forkserver_parent_thread_count_error"] = repr(exc)
    except Exception as exc:  # noqa: BLE001
        result["error"] = repr(exc)
        result["traceback"] = traceback.format_exc()
    return result


def measure_glue_workload(n_items: int, k: int, seed: int) -> dict:
    """(d): pure-Python bar-aggregation over a synthetic OHLCV stream, run
    (i) single-threaded, (ii) across K threads, (iii) across K forkserver
    processes. Same input data, same aggregation function, in all three."""
    result = {"n_items": n_items, "k": k, "seed": seed}
    try:
        items = _generate_ohlcv_stream(n_items, seed)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"stream generation failed: {exc!r}"
        return result

    # (i) single-threaded
    try:
        t0 = time.perf_counter()
        count, hi, lo, close = _aggregate_stream(items)
        t1 = time.perf_counter()
        result["single_threaded"] = {
            "wall_seconds": t1 - t0,
            "items_processed": count,
        }
    except Exception as exc:  # noqa: BLE001
        result["single_threaded"] = {"error": repr(exc)}

    # (ii) K threads
    try:
        chunks = _chunk_list(items, k)
        result_q: "queue.Queue" = queue.Queue()

        def _thread_target(chunk):
            result_q.put(_aggregate_stream(chunk)[0])

        threads = [threading.Thread(target=_thread_target, args=(c,)) for c in chunks]
        t0 = time.perf_counter()
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        t1 = time.perf_counter()
        counts = []
        while not result_q.empty():
            counts.append(result_q.get_nowait())
        result["k_threads"] = {
            "wall_seconds": t1 - t0,
            "items_processed": sum(counts),
            "n_threads": len(threads),
        }
    except Exception as exc:  # noqa: BLE001
        result["k_threads"] = {"error": repr(exc)}

    # (iii) K processes via forkserver
    try:
        chunks = _chunk_list(items, k)
        ctx = mp.get_context("forkserver")
        t0 = time.perf_counter()
        with ctx.Pool(processes=k) as pool:
            counts = pool.map(_glue_process_worker, chunks)
        t1 = time.perf_counter()
        result["k_processes"] = {
            "wall_seconds": t1 - t0,
            "items_processed": sum(counts),
            "n_processes": k,
        }
    except Exception as exc:  # noqa: BLE001
        result["k_processes"] = {"error": repr(exc)}

    return result


def measure_idle_part_rss_pss(ready_timeout: float, join_timeout: float) -> dict:
    """(e): RSS/PSS of one idle part that has imported numpy, on this build."""
    result = {}
    p = None
    try:
        ctx = mp.get_context("forkserver")
        ready_evt = ctx.Event()
        stop_evt = ctx.Event()
        p = ctx.Process(target=_idle_numpy_part_worker, args=(ready_evt, stop_evt))
        p.start()
        got_ready = ready_evt.wait(timeout=ready_timeout)
        result["ready_signal_received"] = got_ready
        if got_ready:
            result["pid"] = p.pid
            try:
                result.update(_read_smaps_rollup_kb(p.pid))
            except Exception as exc:  # noqa: BLE001
                result["smaps_error"] = repr(exc)
        else:
            result["error"] = "idle part did not signal ready within timeout"
        stop_evt.set()
        p.join(timeout=join_timeout)
        result["exitcode"] = p.exitcode
    except Exception as exc:  # noqa: BLE001
        result["error"] = repr(exc)
        result["traceback"] = traceback.format_exc()
    finally:
        if p is not None and p.is_alive():
            p.terminate()
            p.join(timeout=join_timeout)
    return result


def measure_memory_return(
    n_parts: int, ready_timeout: float, join_timeout: float, settle_seconds: float
) -> dict:
    """(f): MemAvailable before starting parts, with parts running, and
    after they exit -- whether an off part's RAM genuinely returns (T-3)."""
    result = {}
    procs = []
    try:
        result["mem_available_kb_before"] = _read_meminfo_field_kb("MemAvailable")
        ctx = mp.get_context("forkserver")
        ready_events = [ctx.Event() for _ in range(n_parts)]
        stop_events = [ctx.Event() for _ in range(n_parts)]
        for i in range(n_parts):
            proc = ctx.Process(
                target=_idle_numpy_part_worker, args=(ready_events[i], stop_events[i])
            )
            proc.start()
            procs.append(proc)

        all_ready = True
        for e in ready_events:
            all_ready = e.wait(timeout=ready_timeout) and all_ready
        result["all_parts_ready"] = all_ready
        result["mem_available_kb_with_parts_running"] = _read_meminfo_field_kb(
            "MemAvailable"
        )
        result["parts_running_pids"] = [p.pid for p in procs]

        for e in stop_events:
            e.set()
        for proc in procs:
            proc.join(timeout=join_timeout)
        result["exit_codes"] = [p.exitcode for p in procs]

        if settle_seconds > 0:
            time.sleep(settle_seconds)
        result["mem_available_kb_after_exit"] = _read_meminfo_field_kb("MemAvailable")
    except Exception as exc:  # noqa: BLE001
        result["error"] = repr(exc)
        result["traceback"] = traceback.format_exc()
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=join_timeout)
    return result


# --------------------------------------------------------------------------
# CLI / orchestration
# --------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Measure standard vs free-threaded CPython 3.14 for the "
            "segment-bots part-runtime architecture. Prints raw numbers "
            "only; no verdicts."
        )
    )
    p.add_argument(
        "--parts",
        type=int,
        default=6,
        help=(
            "N: number of forkserver-started child parts for the forkserver "
            "viability test (b/c) and the memory-return test (f). Default 6 "
            "is a quiet-box stand-in for 'tens running at once' -- large "
            "enough to see a distribution, small enough to run serially in "
            "a smoke test."
        ),
    )
    p.add_argument(
        "--items",
        type=int,
        default=200_000,
        help=(
            "Number of synthetic OHLCV tuples in the glue-workload stream "
            "(d). Default chosen so single-threaded wall time is long "
            "enough (roughly hundreds of ms on this box) to be dominated "
            "by the aggregation loop itself rather than thread/process "
            "start overhead."
        ),
    )
    p.add_argument(
        "--threads",
        type=int,
        default=6,
        help=(
            "K: number of threads and number of processes used for the "
            "glue workload's threaded and multiprocess variants (d). "
            "Default 6 matches this box's 6 physical cores."
        ),
    )
    p.add_argument(
        "--seed",
        type=int,
        default=1337,
        help="RNG seed for the synthetic OHLCV stream generator (d); fixed for reproducibility across runs and builds.",
    )
    p.add_argument(
        "--queue-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for a forkserver child's report on its result Queue before recording a timeout error (b).",
    )
    p.add_argument(
        "--join-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for a child process to exit after being signalled (b, e, f).",
    )
    p.add_argument(
        "--ready-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for a spawned idle part to signal it has finished importing numpy and is ready (e, f).",
    )
    p.add_argument(
        "--settle-seconds",
        type=float,
        default=0.0,
        help=(
            "Optional pause after process join before re-reading "
            "/proc/meminfo (f). Default 0.0 because join() already "
            "confirms the process has exited and its memory has been "
            "reaped by the kernel; set >0 to double-check for any delayed "
            "reclaim effect."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Print the full result as one JSON object to stdout. Without this flag, a human-readable summary is printed instead.",
    )
    return p


def run_all(args: argparse.Namespace) -> dict:
    result = {
        "measured_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "args": vars(args),
    }

    sections = [
        ("build_identity", lambda: measure_build_identity()),
        (
            "forkserver_viability",
            lambda: measure_forkserver_viability(
                args.parts, args.queue_timeout, args.join_timeout
            ),
        ),
        (
            "glue_workload",
            lambda: measure_glue_workload(args.items, args.threads, args.seed),
        ),
        (
            "idle_part_memory",
            lambda: measure_idle_part_rss_pss(args.ready_timeout, args.join_timeout),
        ),
        (
            "memory_return",
            lambda: measure_memory_return(
                args.parts, args.ready_timeout, args.join_timeout, args.settle_seconds
            ),
        ),
    ]

    for key, fn in sections:
        try:
            result[key] = fn()
        except Exception as exc:  # noqa: BLE001 - never let one section kill the run
            result[key] = {"error": repr(exc), "traceback": traceback.format_exc()}

    # (c) is also surfaced at top level per the task spec, sourced from the
    # same forkserver instance measured in (b) -- forkserver is a
    # persistent server process, not restarted per section.
    try:
        result["forkserver_parent_thread_count"] = result["forkserver_viability"].get(
            "forkserver_parent_thread_count"
        )
    except Exception:  # noqa: BLE001
        result["forkserver_parent_thread_count"] = None

    return result


def _print_human_summary(result: dict) -> None:
    bi = result.get("build_identity", {})
    fv = result.get("forkserver_viability", {})
    gw = result.get("glue_workload", {})
    ip = result.get("idle_part_memory", {})
    mr = result.get("memory_return", {})

    print(f"measured_at_utc: {result.get('measured_at_utc')}")
    print(f"python_version: {bi.get('python_version')}")
    print(f"py_gil_disabled_config: {bi.get('py_gil_disabled_config')}")
    print(f"gil_enabled before/after numpy: {bi.get('gil_enabled_before_any_import')} / {bi.get('gil_enabled_after_numpy_import')}")
    print(f"gil_enabled final (after all other packages): {bi.get('gil_enabled_final')}")
    print(f"forkserver fork_latency median/p95 (s): {fv.get('fork_latency_seconds', {}).get('median')} / {fv.get('fork_latency_seconds', {}).get('p95')}")
    print(f"forkserver_parent_thread_count: {result.get('forkserver_parent_thread_count')}")
    for label in ("single_threaded", "k_threads", "k_processes"):
        d = gw.get(label, {})
        print(f"glue_workload[{label}]: wall_seconds={d.get('wall_seconds')} items_processed={d.get('items_processed')} error={d.get('error')}")
    print(f"idle_part rss_kb/pss_kb: {ip.get('rss_kb')} / {ip.get('pss_kb')}")
    print(f"mem_available_kb before/running/after: {mr.get('mem_available_kb_before')} / {mr.get('mem_available_kb_with_parts_running')} / {mr.get('mem_available_kb_after_exit')}")


def main(argv=None) -> dict:
    args = build_arg_parser().parse_args(argv)
    result = run_all(args)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_human_summary(result)
    return result


if __name__ == "__main__":
    main()
