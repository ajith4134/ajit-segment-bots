"""Start the forkserver every part is forked from, and refuse an unsafe fork.

Why forkserver and not a hand-rolled zygote (section 1): a warm parent that forks
is four times cheaper per idle child -- 0.76 MB PSS against 3.07 -- and it is still
the wrong choice, because fork's safety condition cannot be verified once and
trusted. POSIX: a mutex another thread holds at the instant of fork is copied
locked, and the thread that would release it does not exist in the child. That is
a hang, not a crash, and it has to hold at every fork for the life of the process,
against every library any dependency pulls in. forkserver moves that invariant into
a maintained component whose only job is to hold it.

Why the caps come first (section 1 rule 3, corrected by measurement): with them
unset, import numpy alone puts 12 kernel threads in the process, before any array
maths, while threading.active_count() still reports 1. So the caps are what make
the forkserver forkable, and reading field 20 of /proc/self/stat is what turns a
missing environment variable into an immediate logged refusal.
"""

from __future__ import annotations

import multiprocessing
import os
import pathlib
from collections.abc import Callable, MutableMapping

# Every library that might start its own thread pool on import. OPENBLAS_NUM_THREADS
# is the authoritative one for our wheel: PyPI OpenBLAS wheels are built with
# pthreads, not OpenMP, so OMP_NUM_THREADS is a fallback rather than the control.
BLAS_THREAD_CAP_VARIABLES = (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)

SINGLE_THREAD = "1"

# Field 20 of /proc/<pid>/stat, 1-indexed as the man page counts it, is num_threads.
# It has to be parsed after the last ')' because field 2 is the executable name and
# may itself contain spaces and parentheses.
_NUM_THREADS_INDEX_AFTER_COMM = 17


class ForkRefused(RuntimeError):
    """A fork was refused because the forking process held more than one thread."""


def apply_blas_thread_caps(
    environment: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Pin every BLAS thread pool to one thread, returning what was set.

    Must run before numpy is imported anywhere in the process. A deliberate value
    already in the environment is left alone -- the governor owns parallelism, and
    if it decided a part gets two BLAS threads, this is not the place to overrule it.
    """
    target = os.environ if environment is None else environment
    applied: dict[str, str] = {}
    for name in BLAS_THREAD_CAP_VARIABLES:
        if name not in target:
            target[name] = SINGLE_THREAD
        applied[name] = target[name]
    return applied


def read_kernel_thread_count(pid: int | str = "self") -> int:
    """How many threads the kernel says this process has.

    Deliberately not threading.active_count(): that counts Python's own Thread
    objects and cannot see a thread OpenBLAS started in C. Measured, the two
    disagree 12 to 1 immediately after an uncapped import of numpy.
    """
    raw = pathlib.Path(f"/proc/{pid}/stat").read_text()
    after_comm = raw.rsplit(")", 1)[1].split()
    return int(after_comm[_NUM_THREADS_INDEX_AFTER_COMM])


def is_safe_to_fork(thread_ceiling: int, pid: int | str = "self") -> bool:
    """Is this process single-threaded enough to fork without risking a locked mutex?"""
    return read_kernel_thread_count(pid) <= thread_ceiling


def start_forkserver(preload_modules: tuple[str, ...]) -> multiprocessing.context.BaseContext:
    """Return a forkserver context with the given modules already imported.

    The preload imports numpy but must never execute a BLAS call: a warm-up matmul
    would be a computation in the forking process, and the caps are the only reason
    that process is single-threaded at all.
    """
    apply_blas_thread_caps()
    context = multiprocessing.get_context("forkserver")
    context.set_forkserver_preload(list(preload_modules))
    return context


def spawn_part(
    context: multiprocessing.context.BaseContext,
    entry_point: Callable[..., None],
    arguments: tuple,
    thread_ceiling: int,
) -> multiprocessing.Process:
    """Fork one part, refusing loudly rather than risking a child that hangs.

    entry_point must be module-level: forkserver pickles the target by qualified
    name, and a nested function fails with
    AttributeError: module '__mp_main__' has no attribute '...' (section 1 rule 2).
    """
    observed = read_kernel_thread_count()
    if observed > thread_ceiling:
        raise ForkRefused(
            f"refusing to fork: /proc/self/stat reports {observed} kernel threads, ceiling is "
            f"{thread_ceiling}. A mutex held by another thread at the instant of fork is copied "
            f"locked into the child and never released. The usual cause is numpy imported before "
            f"the BLAS thread caps were applied -- see apply_blas_thread_caps."
        )
    process = context.Process(target=entry_point, args=arguments)
    process.start()
    return process
