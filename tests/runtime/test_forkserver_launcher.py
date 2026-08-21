"""The BLAS thread caps are load-bearing for fork safety, not a scheduling preference.

Section 1 rule 3, as corrected by measurement: with the caps unset, import numpy
alone puts 12 kernel threads in the process -- before any array maths -- while
threading.active_count() still reports 1. That is why the fork site reads field 20
of /proc/self/stat and not Python's own count.
"""

import multiprocessing
import os
import subprocess
import sys

import pytest

from runtime.forkserver_launcher import (
    BLAS_THREAD_CAP_VARIABLES,
    ForkRefused,
    apply_blas_thread_caps,
    is_safe_to_fork,
    read_kernel_thread_count,
    spawn_part,
    start_forkserver,
)

CAPPED = "; ".join(f"os.environ['{name}'] = '1'" for name in [
    "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS",
])

THREAD_COUNT_AFTER_NUMPY = """
import os, sys
{caps}
import numpy
import threading
sys.path.insert(0, {repository!r})
from runtime.forkserver_launcher import read_kernel_thread_count
print(read_kernel_thread_count(), threading.active_count())
"""


def _thread_count_after_importing_numpy(caps: str) -> tuple[int, int]:
    repository = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    script = THREAD_COUNT_AFTER_NUMPY.format(caps=caps, repository=repository)
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                               env={k: v for k, v in os.environ.items()
                                    if k not in BLAS_THREAD_CAP_VARIABLES})
    assert completed.returncode == 0, completed.stderr[-400:]
    kernel, python = completed.stdout.split()
    return int(kernel), int(python)


def test_reads_the_kernel_thread_count_not_pythons():
    assert read_kernel_thread_count() >= 1


def test_uncapped_numpy_import_spawns_threads_python_cannot_see():
    kernel, python = _thread_count_after_importing_numpy(caps="pass")
    assert kernel > 1, "OpenBLAS did not spawn at import; re-check the caps really were unset"
    assert python == 1, "this is the whole point: threading.active_count() cannot see them"


def test_capped_numpy_import_leaves_the_process_forkable():
    kernel, python = _thread_count_after_importing_numpy(caps=CAPPED)
    assert kernel == 1
    assert python == 1


def test_apply_blas_thread_caps_sets_every_variable_and_reports_what_it_set():
    environment: dict[str, str] = {}
    applied = apply_blas_thread_caps(environment)
    assert set(applied) == set(BLAS_THREAD_CAP_VARIABLES)
    assert all(environment[name] == "1" for name in BLAS_THREAD_CAP_VARIABLES)


def test_apply_blas_thread_caps_does_not_overwrite_a_deliberate_setting():
    environment = {"OPENBLAS_NUM_THREADS": "2"}
    apply_blas_thread_caps(environment)
    assert environment["OPENBLAS_NUM_THREADS"] == "2"


def test_is_safe_to_fork_is_true_for_a_single_threaded_process():
    assert is_safe_to_fork(thread_ceiling=1) is True


def test_spawn_part_refuses_when_the_process_holds_more_threads_than_the_ceiling():
    context = start_forkserver(preload_modules=("numpy",))
    with pytest.raises(ForkRefused) as refusal:
        spawn_part(context, entry_point=_report_thread_count, arguments=(), thread_ceiling=0)
    assert "/proc/self/stat" in str(refusal.value)


def _report_thread_count() -> None:
    """Module-level on purpose: forkserver pickles the target by qualified name."""
    import numpy
    assert "numpy" in sys.modules
    print(read_kernel_thread_count())


def test_a_part_forked_from_the_forkserver_inherits_the_preloaded_numpy():
    context = start_forkserver(preload_modules=("numpy",))
    queue = context.Queue()
    process = spawn_part(context, entry_point=_report_preload_state, arguments=(queue,), thread_ceiling=1)
    inherited, kernel_threads = queue.get(timeout=30)
    process.join(timeout=30)
    assert inherited is True, "the child re-imported numpy instead of inheriting it"
    assert kernel_threads == 1, "the child came up with BLAS threads; the caps did not reach it"


def _report_preload_state(queue) -> None:
    """Module-level on purpose (section 1 rule 2)."""
    import sys as child_sys
    queue.put(("numpy" in child_sys.modules, read_kernel_thread_count()))
