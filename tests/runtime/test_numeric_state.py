"""Durable numeric state, and the stamp that says whether a part died mid-update.

Section 15.4: numpy.memmap has no API to close the underlying mmap, and it does not
need one. Dirty MAP_SHARED pages belong to the inode's page cache, not the process,
so they survive a SIGKILL that runs no userspace cleanup at all. Measured 6 of 6.
"""

import os
import signal
import subprocess
import sys

import numpy
import pytest

from runtime.numeric_state import (
    NumericStateSpec,
    has_state_gap,
    open_numeric_state,
)

SPEC = NumericStateSpec(name="rolling-window", shape=(2000, 6), dtype="float64")

CHILD = """
import sys
sys.path.insert(0, {repository!r})
from runtime.numeric_state import NumericStateSpec, open_numeric_state
import numpy, pathlib, time
spec = NumericStateSpec(name="rolling-window", shape=(2000, 6), dtype="float64")
state = open_numeric_state(pathlib.Path({directory!r}), spec)
pattern = numpy.arange(2000 * 6, dtype="float64").reshape(2000, 6) * 3.0 + {seed}
state.array[:] = pattern
state.record_sequence_stamp({stamp})
{maybe_flush}
print("WRITTEN", flush=True)
time.sleep(30)
"""


def _repository() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _write_then_kill(directory, seed: int, stamp: int, flush: bool) -> None:
    script = CHILD.format(
        repository=_repository(), directory=str(directory), seed=seed, stamp=stamp,
        maybe_flush="state.force_writeback()" if flush else "",
    )
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "WRITTEN"
        os.kill(child.pid, signal.SIGKILL)
        child.wait()
    finally:
        child.stdout.close()


def test_state_round_trips_within_one_process(durable_tmp_path):
    with open_numeric_state(durable_tmp_path, SPEC) as state:
        state.array[0, 0] = 42.5
        state.record_sequence_stamp(7)
    with open_numeric_state(durable_tmp_path, SPEC) as reopened:
        assert reopened.array[0, 0] == 42.5
        assert reopened.read_sequence_stamp() == 7


@pytest.mark.slow
@pytest.mark.parametrize("flush", [False, True])
def test_unflushed_state_survives_sigkill_intact(durable_tmp_path, flush):
    seed, stamp = 11, 99
    _write_then_kill(durable_tmp_path, seed=seed, stamp=stamp, flush=flush)

    expected = numpy.arange(2000 * 6, dtype="float64").reshape(2000, 6) * 3.0 + seed
    with open_numeric_state(durable_tmp_path, SPEC) as reopened:
        survived = int((reopened.array == expected).sum())
        assert survived == expected.size, f"{survived} of {expected.size} elements survived"
        assert reopened.read_sequence_stamp() == stamp


def test_a_stamp_behind_what_was_published_is_a_gap(durable_tmp_path):
    # The part updated memory, published stamp 5 to the store, then died before
    # its next update landed. On switch-on that divergence is a fault, not a
    # reason to silently reseed.
    with open_numeric_state(durable_tmp_path, SPEC) as state:
        state.record_sequence_stamp(4)
        assert has_state_gap(state, published_stamp=5) is True
        assert has_state_gap(state, published_stamp=4) is False


@pytest.mark.slow
def test_repeated_off_and_on_cycles_do_not_leak_descriptors_or_mappings(durable_tmp_path):
    # The real cost of the missing close(): fds and VMAs in a process that opens
    # many mappings without exiting. The governor must never do this, and this test
    # is what would catch it if something did.
    def counts() -> tuple[int, int]:
        with open("/proc/self/maps") as maps_file:
            mapping_count = len(maps_file.read().splitlines())
        return (
            len(os.listdir("/proc/self/fd")),
            mapping_count,
        )

    for _ in range(20):
        with open_numeric_state(durable_tmp_path, SPEC) as state:
            state.array[0, 0] += 1.0
    settled_descriptors, settled_mappings = counts()

    for _ in range(200):
        with open_numeric_state(durable_tmp_path, SPEC) as state:
            state.array[0, 0] += 1.0
    final_descriptors, final_mappings = counts()

    assert final_descriptors <= settled_descriptors + 2, "file descriptors are accumulating"
    assert final_mappings <= settled_mappings + 8, "mappings are accumulating"


def test_it_refuses_a_directory_whose_pages_are_memory(tmp_path):
    from runtime.storage_facts import VolatileStorageRefused

    with pytest.raises(VolatileStorageRefused):
        open_numeric_state(tmp_path, SPEC)


def test_reopening_with_a_larger_shape_refuses(durable_tmp_path):
    # numpy's own r+ mode would silently extend the file with zero bytes here --
    # exactly the quiet corruption has_state_gap exists to catch on the other
    # side of a crash. Reopening under a bigger spec must fail closed instead.
    import math

    from runtime.numeric_state import NumericStateShapeMismatch

    written_spec = NumericStateSpec(name="rolling-window", shape=(10,), dtype="float64")
    with open_numeric_state(durable_tmp_path, written_spec) as state:
        state.array[:] = 1.0

    larger_spec = NumericStateSpec(name="rolling-window", shape=(20,), dtype="float64")
    with pytest.raises(NumericStateShapeMismatch) as failure:
        open_numeric_state(durable_tmp_path, larger_spec)

    actual_bytes = numpy.dtype(written_spec.dtype).itemsize * math.prod(written_spec.shape)
    expected_bytes = numpy.dtype(larger_spec.dtype).itemsize * math.prod(larger_spec.shape)
    message = str(failure.value)
    assert str(actual_bytes) in message
    assert str(expected_bytes) in message


def test_reopening_with_a_smaller_shape_refuses(durable_tmp_path):
    # numpy's own r+ mode would silently map only a truncated subset here. The
    # rest of what was written would still be on disk but invisible -- the same
    # class of quiet corruption as the larger-shape case, from the other side.
    import math

    from runtime.numeric_state import NumericStateShapeMismatch

    written_spec = NumericStateSpec(name="rolling-window", shape=(20,), dtype="float64")
    with open_numeric_state(durable_tmp_path, written_spec) as state:
        state.array[:] = 1.0

    smaller_spec = NumericStateSpec(name="rolling-window", shape=(10,), dtype="float64")
    with pytest.raises(NumericStateShapeMismatch) as failure:
        open_numeric_state(durable_tmp_path, smaller_spec)

    actual_bytes = numpy.dtype(written_spec.dtype).itemsize * math.prod(written_spec.shape)
    expected_bytes = numpy.dtype(smaller_spec.dtype).itemsize * math.prod(smaller_spec.shape)
    message = str(failure.value)
    assert str(actual_bytes) in message
    assert str(expected_bytes) in message


def test_reopening_with_a_different_dtype_of_the_same_total_size_is_not_caught_by_size_alone(
    durable_tmp_path,
):
    # Documented limitation, not a guarantee: the guard compares total byte
    # counts, and float64[10] and int64[10] both occupy 80 bytes. This test
    # records that a dtype swap at the same total size passes through the guard
    # rather than leaving the gap silently untested -- see the task-10 report.
    written_spec = NumericStateSpec(name="rolling-window", shape=(10,), dtype="float64")
    with open_numeric_state(durable_tmp_path, written_spec) as state:
        state.array[:] = 1.0

    same_size_different_dtype_spec = NumericStateSpec(
        name="rolling-window", shape=(10,), dtype="int64"
    )
    with open_numeric_state(durable_tmp_path, same_size_different_dtype_spec) as reopened:
        assert reopened.array.dtype == numpy.dtype("int64")


def test_reopening_with_the_matching_spec_still_works_and_data_is_intact(durable_tmp_path):
    # The guard must not break the ordinary path: reopening under the same shape
    # and dtype the state was written under has to succeed exactly as before.
    written_spec = NumericStateSpec(name="rolling-window", shape=(10,), dtype="float64")
    with open_numeric_state(durable_tmp_path, written_spec) as state:
        state.array[:] = 3.5
        state.record_sequence_stamp(2)

    matching_spec = NumericStateSpec(name="rolling-window", shape=(10,), dtype="float64")
    with open_numeric_state(durable_tmp_path, matching_spec) as reopened:
        assert bool((reopened.array == 3.5).all())
        assert reopened.read_sequence_stamp() == 2
