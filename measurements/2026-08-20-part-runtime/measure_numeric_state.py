#!/usr/bin/env python3
"""Measurement harness: does numpy.memmap / multiprocessing.shared_memory
survive a REAL off/on cycle, where "off" is SIGKILL?

This is the empirical test the part-runtime spec calls for at
docs/superpowers/specs/2026-08-20-part-runtime-design.md section 4:
"numpy.memmap has no API to explicitly close the underlying mapping --
cleanup is refcount best-effort, so it must be tested under a real off/on
cycle before being relied on."

The state being modelled is a part's numeric state: a fixed-shape,
fixed-dtype float64 array (a rolling window of OHLCV bars plus learned
parameters) that a part updates while "on" and must find intact when it
next turns "on".

Every measurement is wrapped so one failure records {"error": ...} and the
rest still run (see `_safe`). Every number that controls scale, timing or
seeding comes from argparse, never a bare literal buried in decision logic.
Small integer constants that are properties of the hash/pattern-generation
algorithm itself (not tuning knobs) are named constants near their use and
documented as such -- they are not part-runtime decisions.

Run:
    <venv>/bin/python measure_numeric_state.py --json

Everything a *child* process runs is a module-level function (multiprocessing
forkserver/spawn require this, and it is also this project's stated rule for
this harness).
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import glob
import json
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from multiprocessing import shared_memory

# The governor, not BLAS, owns core allocation -- pin BLAS threading before
# numpy is imported, matching this project's stated runtime design.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np  # noqa: E402  (must follow the OPENBLAS_NUM_THREADS pin)

DTYPE = np.float64
OHLCV_COLUMNS = 5  # fixed domain shape (open,high,low,close,volume), not a tuning knob


# --------------------------------------------------------------------------
# Deterministic pattern generation -- pure integer hash mixed into [0, 1),
# so parent and child compute bit-identical float64 values independently.
# The multiplier/mask below are a standard multiplicative-hash constant
# (Knuth) and a 32-bit mask; they are properties of the hash, not something
# an operator would ever tune, so they are not argparse parameters.
# --------------------------------------------------------------------------
_HASH_MULTIPLIER = np.uint64(2654435761)
_HASH_MASK = np.uint64(0xFFFFFFFF)


def _deterministic_pattern(shape, seed):
    n = 1
    for d in shape:
        n *= d
    idx = np.arange(n, dtype=np.uint64)
    seed_u = np.uint64(seed & 0xFFFFFFFF)
    mixed = (idx * _HASH_MULTIPLIER + seed_u) & _HASH_MASK
    pattern = mixed.astype(np.float64) / np.float64(_HASH_MASK)
    return pattern.reshape(shape).astype(DTYPE)


def _bar_from_index(i):
    base = float(i)
    return np.array([base, base + 1.0, base - 1.0, base + 0.5, base * 10.0], dtype=DTYPE)


# --------------------------------------------------------------------------
# /proc readers
# --------------------------------------------------------------------------

def _read_mem_available_kb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1])
    raise RuntimeError("MemAvailable not found in /proc/meminfo")


def _read_smaps_rollup_rss_kb():
    with open("/proc/self/smaps_rollup") as f:
        for line in f:
            if line.startswith("Rss:"):
                return int(line.split()[1])
    raise RuntimeError("Rss not found in /proc/self/smaps_rollup")


def _count_open_fds():
    return len(os.listdir("/proc/self/fd"))


def _count_maps_lines():
    with open("/proc/self/maps") as f:
        return sum(1 for _ in f)


def _resolve_cgroup_memory_current_path():
    with open("/proc/self/cgroup") as f:
        line = f.read().strip()
    # cgroup v2 unified hierarchy line looks like "0::/user.slice/..."
    if "::" not in line:
        return None
    rel = line.split("::", 1)[1]
    return os.path.join("/sys/fs/cgroup", rel.lstrip("/"), "memory.current")


def _read_memory_current_kb(path):
    with open(path) as f:
        return int(f.read().strip()) // 1024


# --------------------------------------------------------------------------
# module-level child-process targets (required: forkserver/spawn need these
# importable by reference, and it is this harness's stated rule regardless)
# --------------------------------------------------------------------------

def _memmap_child_write(path, shape_list, dtype_str, seed, do_flush, ready_event, poll_interval):
    shape = tuple(shape_list)
    arr = np.memmap(path, dtype=np.dtype(dtype_str), mode="r+", shape=shape)
    arr[:] = _deterministic_pattern(shape, seed)
    if do_flush:
        arr.flush()
    ready_event.set()
    # Wait to be SIGKILLed by the parent. Bounded loop so a broken parent
    # (harness bug) does not hang the child forever instead of hanging the
    # test run visibly.
    deadline = time.monotonic() + 300.0
    while time.monotonic() < deadline:
        time.sleep(poll_interval)


def _shm_child_touch_and_wait(name, nbytes, seed, ready_event, poll_interval):
    shm = shared_memory.SharedMemory(name=name, create=True, size=nbytes)
    n = nbytes // np.dtype(DTYPE).itemsize
    arr = np.ndarray((n,), dtype=DTYPE, buffer=shm.buf)
    arr[:] = _deterministic_pattern((n,), seed)
    ready_event.set()
    deadline = time.monotonic() + 300.0
    while time.monotonic() < deadline:
        time.sleep(poll_interval)


def _start_child_and_kill(ctx, target, args_tuple, ready_event, ready_timeout, join_timeout):
    """Start a child running `target`, wait for it to signal ready, SIGKILL
    it, join it. Returns (ready_signal_received, pid, exitcode)."""
    p = ctx.Process(target=target, args=args_tuple)
    p.start()
    got_ready = ready_event.wait(timeout=ready_timeout)
    pid = p.pid
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    p.join(timeout=join_timeout)
    return got_ready, pid, p.exitcode


# --------------------------------------------------------------------------
# (a) durability under SIGKILL -- the crux
# --------------------------------------------------------------------------

def _create_zeroed_memmap_file(path, shape, dtype):
    mm = np.memmap(path, dtype=dtype, mode="w+", shape=shape)
    mm[:] = 0.0
    mm.flush()
    del mm


def _run_sigkill_durability_variant(work_dir, ctx, shape, seed, do_flush, ready_timeout, join_timeout, poll_interval):
    path = os.path.join(work_dir, f"durability_{'flush' if do_flush else 'noflush'}.bin")
    _create_zeroed_memmap_file(path, shape, DTYPE)
    ready = ctx.Event()
    got_ready, pid, exitcode = _start_child_and_kill(
        ctx, _memmap_child_write,
        (path, list(shape), "float64", seed, do_flush, ready, poll_interval),
        ready, ready_timeout, join_timeout,
    )
    check_arr = np.memmap(path, dtype=DTYPE, mode="r", shape=shape)
    expected = _deterministic_pattern(shape, seed)
    matches = int(np.sum(np.asarray(check_arr) == expected))
    total = int(expected.size)
    del check_arr
    os.remove(path)
    return {
        "do_flush": do_flush,
        "seed": seed,
        "child_ready_signal_received": bool(got_ready),
        "child_pid": pid,
        "child_exitcode": exitcode,
        "matched_elements": matches,
        "total_elements": total,
        "match_fraction": (matches / total) if total else None,
    }


def measure_sigkill_durability(args, work_dir, ctx):
    return {
        "no_flush": _run_sigkill_durability_variant(
            work_dir, ctx, args.shape, args.seed, False,
            args.child_ready_timeout_seconds, args.join_timeout_seconds, args.poll_interval_seconds,
        ),
        "with_flush": _run_sigkill_durability_variant(
            work_dir, ctx, args.shape, args.seed + 1, True,
            args.child_ready_timeout_seconds, args.join_timeout_seconds, args.poll_interval_seconds,
        ),
    }


# --------------------------------------------------------------------------
# (b) fd / mapping leak across many off/on cycles, measured on the
# long-lived parent process itself
# --------------------------------------------------------------------------

def measure_leak_across_cycles(args, work_dir, ctx):
    path = os.path.join(work_dir, "leak_cycle.bin")
    shape = tuple(args.shape)
    _create_zeroed_memmap_file(path, shape, DTYPE)
    series = []
    for cycle in range(args.cycles):
        ready = ctx.Event()
        got_ready, pid, exitcode = _start_child_and_kill(
            ctx, _memmap_child_write,
            (path, list(shape), "float64", args.seed + cycle, False, ready, args.poll_interval_seconds),
            ready, args.child_ready_timeout_seconds, args.join_timeout_seconds,
        )
        # Parent itself remaps to "verify" state after the off/on cycle
        # (standing in for a governor-side verifier), then drops its
        # reference the way numpy.memmap requires -- refcount only.
        mm = np.memmap(path, dtype=DTYPE, mode="r+", shape=shape)
        _touch = float(mm[0, 0]) if mm.ndim == 2 else float(mm[0])
        del mm
        gc.collect()
        series.append({
            "cycle": cycle,
            "child_ready_signal_received": bool(got_ready),
            "child_exitcode": exitcode,
            "parent_open_fds": _count_open_fds(),
            "parent_maps_lines": _count_maps_lines(),
            "parent_smaps_rollup_rss_kb": _read_smaps_rollup_rss_kb(),
        })
    os.remove(path)
    return {"cycles_run": args.cycles, "series": series}


# --------------------------------------------------------------------------
# (c) RAM return: memmap vs shared_memory, same four checkpoints
# --------------------------------------------------------------------------

def measure_memmap_ram_return(args, work_dir, ctx):
    path = os.path.join(work_dir, "ram_test_memmap.bin")
    shape = (args.ram_elements,)
    _create_zeroed_memmap_file(path, shape, DTYPE)

    before_mapping_kb = _read_mem_available_kb()

    ready = ctx.Event()
    p = ctx.Process(
        target=_memmap_child_write,
        args=(path, list(shape), "float64", args.seed, False, ready, args.poll_interval_seconds),
    )
    p.start()
    got_ready = ready.wait(timeout=args.child_ready_timeout_seconds)
    while_mapped_and_touched_kb = _read_mem_available_kb()

    try:
        os.kill(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    p.join(timeout=args.join_timeout_seconds)
    after_child_killed_kb = _read_mem_available_kb()

    # Parent turns the part back "on": remaps the same file and touches it.
    mm = np.memmap(path, dtype=DTYPE, mode="r+", shape=shape)
    _ = float(np.asarray(mm).sum())
    del mm
    gc.collect()
    after_parent_drops_reference_kb = _read_mem_available_kb()

    os.remove(path)
    return {
        "elements": args.ram_elements,
        "bytes": args.ram_elements * np.dtype(DTYPE).itemsize,
        "child_ready_signal_received": bool(got_ready),
        "mem_available_kb": {
            "before_mapping": before_mapping_kb,
            "while_mapped_and_touched": while_mapped_and_touched_kb,
            "after_child_killed": after_child_killed_kb,
            "after_parent_drops_reference": after_parent_drops_reference_kb,
        },
    }


def measure_shared_memory_ram_return(args, work_dir, ctx, run_prefix):
    name = f"{run_prefix}-ram-{uuid.uuid4().hex[:8]}"
    nbytes = args.ram_elements * np.dtype(DTYPE).itemsize

    before_mapping_kb = _read_mem_available_kb()

    ready = ctx.Event()
    p = ctx.Process(
        target=_shm_child_touch_and_wait,
        args=(name, nbytes, args.seed, ready, args.poll_interval_seconds),
    )
    p.start()
    got_ready = ready.wait(timeout=args.child_ready_timeout_seconds)
    while_mapped_and_touched_kb = _read_mem_available_kb()

    try:
        os.kill(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    p.join(timeout=args.join_timeout_seconds)
    after_child_killed_kb = _read_mem_available_kb()

    parent_could_attach = False
    try:
        shm = shared_memory.SharedMemory(name=name, create=False)
        parent_could_attach = True
        shm.close()
    except FileNotFoundError:
        parent_could_attach = False

    try:
        shm2 = shared_memory.SharedMemory(name=name, create=False)
        shm2.close()
        shm2.unlink()
    except FileNotFoundError:
        pass
    gc.collect()
    after_parent_drops_reference_kb = _read_mem_available_kb()

    return {
        "elements": args.ram_elements,
        "bytes": nbytes,
        "shm_name": name,
        "child_ready_signal_received": bool(got_ready),
        "parent_could_attach_after_kill": parent_could_attach,
        "mem_available_kb": {
            "before_mapping": before_mapping_kb,
            "while_mapped_and_touched": while_mapped_and_touched_kb,
            "after_child_killed": after_child_killed_kb,
            "after_parent_drops_reference": after_parent_drops_reference_kb,
        },
    }


# --------------------------------------------------------------------------
# (d) shared_memory after SIGKILL -- run in an isolated subprocess so the
# resource_tracker's leaked-object warning (only emitted once every holder
# of the tracker pipe's write end has exited) is actually observable inside
# this run, instead of only at final interpreter exit of this harness.
# --------------------------------------------------------------------------

_D_DRIVER_SOURCE = '''
import json, multiprocessing, os, signal, sys, time
from multiprocessing import shared_memory
import numpy as np

def _child(name, nbytes, seed, ready_event, poll_interval):
    shm = shared_memory.SharedMemory(name=name, create=True, size=nbytes)
    n = nbytes // 8
    arr = np.ndarray((n,), dtype=np.float64, buffer=shm.buf)
    arr[:] = float(seed)
    ready_event.set()
    deadline = time.monotonic() + 300.0
    while time.monotonic() < deadline:
        time.sleep(poll_interval)

if __name__ == "__main__":
    name = sys.argv[1]
    nbytes = int(sys.argv[2])
    seed = int(sys.argv[3])
    poll_interval = float(sys.argv[4])
    ready_timeout = float(sys.argv[5])
    join_timeout = float(sys.argv[6])
    mp_context = sys.argv[7]

    ctx = multiprocessing.get_context(mp_context)
    ready = ctx.Event()
    p = ctx.Process(target=_child, args=(name, nbytes, seed, ready, poll_interval))
    p.start()
    got_ready = ready.wait(timeout=ready_timeout)
    pid = p.pid
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    p.join(timeout=join_timeout)
    print(json.dumps({
        "ready_signal_received": bool(got_ready),
        "child_pid": pid,
        "child_exitcode": p.exitcode,
    }))
    sys.exit(0)
'''


def measure_shared_memory_sigkill_specifics(args, work_dir, run_prefix):
    name = f"{run_prefix}-sigkill-{uuid.uuid4().hex[:8]}"
    nbytes = args.d_elements * np.dtype(DTYPE).itemsize
    driver_path = os.path.join(work_dir, "d_driver.py")
    with open(driver_path, "w") as f:
        f.write(_D_DRIVER_SOURCE)

    mem_before_kb = _read_mem_available_kb()

    proc = subprocess.run(
        [sys.executable, driver_path, name, str(nbytes), str(args.seed),
         str(args.poll_interval_seconds), str(args.child_ready_timeout_seconds),
         str(args.join_timeout_seconds), args.mp_context],
        capture_output=True, text=True,
        timeout=args.child_ready_timeout_seconds + args.join_timeout_seconds + 30.0,
    )
    try:
        driver_info = json.loads(proc.stdout.strip()) if proc.stdout.strip() else {"error": "no stdout from driver", "stderr": proc.stderr[-2000:]}
    except json.JSONDecodeError:
        driver_info = {"error": "driver stdout not JSON", "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]}

    shm_path = f"/dev/shm/{name}"
    exists_immediately = os.path.exists(shm_path)
    size_immediately = os.path.getsize(shm_path) if exists_immediately else None

    time.sleep(args.tracker_settle_seconds)
    exists_after_settle = os.path.exists(shm_path)
    size_after_settle = os.path.getsize(shm_path) if exists_after_settle else None

    parent_can_attach = False
    content_matches_expected = None
    if exists_after_settle:
        try:
            shm = shared_memory.SharedMemory(name=name, create=False)
            parent_can_attach = True
            n = nbytes // np.dtype(DTYPE).itemsize
            arr = np.ndarray((n,), dtype=DTYPE, buffer=shm.buf)
            content_matches_expected = bool(np.all(arr == float(args.seed)))
            shm.close()
        except FileNotFoundError:
            parent_can_attach = False

    stderr_lower = proc.stderr.lower()
    tracker_warning_seen = ("leak" in stderr_lower) or ("resource_tracker" in stderr_lower)

    mem_after_driver_exit_kb = _read_mem_available_kb()

    # Explicit cleanup, reporting whether we or the resource tracker got
    # there first.
    explicitly_unlinked_by_parent = False
    try:
        shm2 = shared_memory.SharedMemory(name=name, create=False)
        shm2.close()
        shm2.unlink()
        explicitly_unlinked_by_parent = True
    except FileNotFoundError:
        explicitly_unlinked_by_parent = False
    exists_after_explicit_unlink = os.path.exists(shm_path)

    return {
        "shm_name": name,
        "requested_bytes": nbytes,
        "driver_info": driver_info,
        "driver_returncode": proc.returncode,
        "exists_in_dev_shm_immediately_after_driver_exit": exists_immediately,
        "size_bytes_immediately": size_immediately,
        "tracker_settle_wait_seconds": args.tracker_settle_seconds,
        "exists_in_dev_shm_after_settle_wait": exists_after_settle,
        "size_bytes_after_settle": size_after_settle,
        "parent_can_attach": parent_can_attach,
        "content_matches_expected_pattern": content_matches_expected,
        "resource_tracker_warning_seen_in_stderr": tracker_warning_seen,
        "driver_stderr_tail": proc.stderr[-2000:],
        "mem_available_kb_before_driver": mem_before_kb,
        "mem_available_kb_after_driver_exit_and_settle": mem_after_driver_exit_kb,
        "explicitly_unlinked_by_parent": explicitly_unlinked_by_parent,
        "exists_after_explicit_unlink": exists_after_explicit_unlink,
    }


# --------------------------------------------------------------------------
# (e) write throughput -- tiebreak only, kept short
# --------------------------------------------------------------------------

def measure_write_throughput(args, work_dir, run_prefix):
    window = args.window_size
    iterations = args.throughput_iterations
    results = {"window_size": window, "iterations": iterations}

    path = os.path.join(work_dir, "throughput_memmap.bin")
    mm = np.memmap(path, dtype=DTYPE, mode="w+", shape=(window, OHLCV_COLUMNS))
    mm[:] = 0.0
    t0 = time.perf_counter()
    for i in range(iterations):
        mm[:-1] = mm[1:]
        mm[-1] = _bar_from_index(i)
        _ = np.asarray(mm).mean(axis=0)
    elapsed = time.perf_counter() - t0
    del mm
    os.remove(path)
    results["memmap_elapsed_sec"] = elapsed
    results["memmap_items_per_sec"] = (iterations / elapsed) if elapsed > 0 else None

    name = f"{run_prefix}-throughput-{uuid.uuid4().hex[:8]}"
    nbytes = window * OHLCV_COLUMNS * np.dtype(DTYPE).itemsize
    shm = shared_memory.SharedMemory(name=name, create=True, size=nbytes)
    arr = np.ndarray((window, OHLCV_COLUMNS), dtype=DTYPE, buffer=shm.buf)
    arr[:] = 0.0
    t0 = time.perf_counter()
    for i in range(iterations):
        arr[:-1] = arr[1:]
        arr[-1] = _bar_from_index(i)
        _ = arr.mean(axis=0)
    elapsed2 = time.perf_counter() - t0
    shm.close()
    shm.unlink()
    results["shared_memory_elapsed_sec"] = elapsed2
    results["shared_memory_items_per_sec"] = (iterations / elapsed2) if elapsed2 > 0 else None
    return results


# --------------------------------------------------------------------------
# (f) cgroup accounting, best effort
# --------------------------------------------------------------------------

def measure_cgroup_accounting(args, work_dir, run_prefix):
    try:
        mem_path = _resolve_cgroup_memory_current_path()
    except Exception as e:
        return {"error": f"could not read /proc/self/cgroup: {type(e).__name__}: {e}"}
    if not mem_path or not os.path.exists(mem_path):
        return {"error": f"resolved memory.current path does not exist: {mem_path}"}

    result = {"memory_current_path": mem_path}
    result["before_kb"] = _read_memory_current_kb(mem_path)

    mfile = os.path.join(work_dir, "cgroup_memmap.bin")
    mm = np.memmap(mfile, dtype=DTYPE, mode="w+", shape=(args.ram_elements,))
    mm[:] = 1.0
    _ = float(np.asarray(mm).sum())
    result["after_memmap_touch_kb"] = _read_memory_current_kb(mem_path)
    del mm
    gc.collect()
    os.remove(mfile)
    result["after_memmap_drop_kb"] = _read_memory_current_kb(mem_path)

    name = f"{run_prefix}-cgroup-{uuid.uuid4().hex[:8]}"
    nbytes = args.ram_elements * np.dtype(DTYPE).itemsize
    shm = shared_memory.SharedMemory(name=name, create=True, size=nbytes)
    arr = np.ndarray((args.ram_elements,), dtype=DTYPE, buffer=shm.buf)
    arr[:] = 1.0
    _ = float(arr.sum())
    result["after_shm_touch_kb"] = _read_memory_current_kb(mem_path)
    shm.close()
    shm.unlink()
    gc.collect()
    result["after_shm_drop_kb"] = _read_memory_current_kb(mem_path)
    return result


# --------------------------------------------------------------------------
# harness plumbing
# --------------------------------------------------------------------------

def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()[-3000:]}


def _sweep_stray_shm(run_prefix):
    """Best-effort final sweep of any /dev/shm segment this run created and
    did not already clean up. Reports what it found before removing it --
    the whole point of (d) is that some of these are expected to survive
    briefly, and that survival is itself the measurement."""
    found = []
    for path in glob.glob(f"/dev/shm/{run_prefix}*"):
        try:
            size = os.path.getsize(path)
            os.remove(path)
            found.append({"path": path, "size_bytes": size, "removed": True})
        except OSError as e:
            found.append({"path": path, "removed": False, "error": str(e)})
    return found


def build_arg_parser():
    p = argparse.ArgumentParser(
        description="Measure numpy.memmap / multiprocessing.shared_memory durability, "
                     "leak behaviour, RAM return and throughput across real SIGKILL off/on cycles."
    )
    p.add_argument("--shape", type=str, default="256,6",
                    help="Comma-separated shape of the durability/leak-test array "
                         "(default 256,6 -- e.g. a 256-bar OHLCV+1 rolling window).")
    p.add_argument("--seed", type=int, default=1337,
                    help="Seed for the deterministic write pattern (default 1337, arbitrary "
                         "fixed value chosen for reproducibility -- override to vary it).")
    p.add_argument("--cycles", type=int, default=200,
                    help="Number of off/on cycles for the fd/mapping leak measurement (default 200, "
                         "chosen to be high enough that a slow per-cycle leak becomes visible as a trend).")
    p.add_argument("--ram-elements", type=int, default=5_000_000,
                    help="float64 element count for the RAM-return and cgroup measurements "
                         "(default 5,000,000 = 40MB, large enough to move MemAvailable measurably).")
    p.add_argument("--d-elements", type=int, default=1_000_000,
                    help="float64 element count for the shared_memory-after-SIGKILL measurement "
                         "(default 1,000,000 = 8MB; kept smaller since (d) runs an extra subprocess).")
    p.add_argument("--window-size", type=int, default=500,
                    help="Rolling-window row count for the throughput measurement (default 500).")
    p.add_argument("--throughput-iterations", type=int, default=2000,
                    help="Number of rolling-window update iterations to time (default 2000).")
    p.add_argument("--poll-interval-seconds", type=float, default=0.02,
                    help="Sleep granularity for a child's wait-to-be-killed loop (default 0.02s).")
    p.add_argument("--child-ready-timeout-seconds", type=float, default=10.0,
                    help="Max seconds to wait for a child's ready signal before proceeding anyway (default 10.0).")
    p.add_argument("--join-timeout-seconds", type=float, default=5.0,
                    help="Max seconds to wait for a SIGKILLed child to be reaped (default 5.0).")
    p.add_argument("--tracker-settle-seconds", type=float, default=0.5,
                    help="Wait applied before re-checking /dev/shm, to let multiprocessing's "
                         "resource_tracker finish any async cleanup pass it started on driver exit (default 0.5).")
    p.add_argument("--mp-context", type=str, default="forkserver", choices=["fork", "forkserver", "spawn"],
                    help="multiprocessing start method (default forkserver, matching this project's "
                         "measured part-launch mechanism).")
    p.add_argument("--work-parent-dir", type=str, default=None,
                    help="Directory under which a temp work dir is created (default: this script's directory).")
    p.add_argument("--json", action="store_true",
                    help="Emit compact single-line JSON to stdout instead of indented JSON.")
    return p


def main():
    args = build_arg_parser().parse_args()
    args.shape = tuple(int(x) for x in args.shape.split(","))

    parent_dir = args.work_parent_dir or os.path.dirname(os.path.abspath(__file__))
    os.makedirs(parent_dir, exist_ok=True)
    work_dir = tempfile.mkdtemp(prefix="measure_numeric_state_", dir=parent_dir)
    run_prefix = f"segbot-{os.getpid()}-{uuid.uuid4().hex[:6]}"

    ctx = multiprocessing.get_context(args.mp_context)

    result = {
        "meta": {
            "python": sys.version,
            "executable": sys.executable,
            "numpy_version": np.__version__,
            "args": {k: (list(v) if isinstance(v, tuple) else v) for k, v in vars(args).items()},
            "work_dir": work_dir,
            "run_prefix": run_prefix,
            "measured_at_unix": time.time(),
        }
    }

    print(f"[measure] work_dir={work_dir} mp_context={args.mp_context}", file=sys.stderr)

    try:
        print("[measure] (a) SIGKILL durability ...", file=sys.stderr)
        result["a_sigkill_durability"] = _safe(measure_sigkill_durability, args, work_dir, ctx)

        print("[measure] (b) fd/mapping leak across cycles ...", file=sys.stderr)
        result["b_leak_across_cycles"] = _safe(measure_leak_across_cycles, args, work_dir, ctx)

        print("[measure] (c) RAM return: memmap ...", file=sys.stderr)
        result["c_ram_return_memmap"] = _safe(measure_memmap_ram_return, args, work_dir, ctx)
        print("[measure] (c) RAM return: shared_memory ...", file=sys.stderr)
        result["c_ram_return_shared_memory"] = _safe(measure_shared_memory_ram_return, args, work_dir, ctx, run_prefix)

        print("[measure] (d) shared_memory after SIGKILL ...", file=sys.stderr)
        result["d_shared_memory_after_sigkill"] = _safe(measure_shared_memory_sigkill_specifics, args, work_dir, run_prefix)

        print("[measure] (e) write throughput ...", file=sys.stderr)
        result["e_write_throughput"] = _safe(measure_write_throughput, args, work_dir, run_prefix)

        print("[measure] (f) cgroup accounting ...", file=sys.stderr)
        result["f_cgroup_accounting"] = _safe(measure_cgroup_accounting, args, work_dir, run_prefix)
    finally:
        stray = _sweep_stray_shm(run_prefix)
        if stray:
            result["stray_shm_swept_at_exit"] = stray
            print(f"[measure] swept {len(stray)} stray /dev/shm segment(s) at exit", file=sys.stderr)
        shutil.rmtree(work_dir, ignore_errors=True)

    if args.json:
        print(json.dumps(result))
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
