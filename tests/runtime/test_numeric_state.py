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


def test_reopening_with_a_different_dtype_of_the_same_total_size_refuses(durable_tmp_path):
    # The case a byte-size check alone cannot catch: float64[10] and int64[10]
    # both occupy 80 bytes, so a size-only guard would let this through and every
    # value the part reads back would be silent garbage -- worse than the
    # truncation case, because zeros at least look wrong. The declaration
    # sidecar written at creation is what closes this.
    from runtime.numeric_state import NumericStateShapeMismatch

    written_spec = NumericStateSpec(name="rolling-window", shape=(10,), dtype="float64")
    with open_numeric_state(durable_tmp_path, written_spec) as state:
        state.array[:] = 1.0

    same_size_different_dtype_spec = NumericStateSpec(
        name="rolling-window", shape=(10,), dtype="int64"
    )
    with pytest.raises(NumericStateShapeMismatch) as failure:
        open_numeric_state(durable_tmp_path, same_size_different_dtype_spec)
    message = str(failure.value)
    assert written_spec.dtype in message
    assert same_size_different_dtype_spec.dtype in message


def test_a_data_file_with_no_sidecar_reopens_under_a_matching_size(durable_tmp_path):
    # State written before this guard existed has no sidecar. That must not be
    # treated as a refusal -- it falls back to the size check alone, and a
    # matching size still opens cleanly.
    written_spec = NumericStateSpec(name="rolling-window", shape=(10,), dtype="float64")
    with open_numeric_state(durable_tmp_path, written_spec) as state:
        state.array[:] = 2.5
        state.record_sequence_stamp(3)

    declaration_path = durable_tmp_path / f"{written_spec.name}.declared.json"
    declaration_path.unlink()

    with open_numeric_state(durable_tmp_path, written_spec) as reopened:
        assert bool((reopened.array == 2.5).all())
        assert reopened.read_sequence_stamp() == 3


def test_a_data_file_with_no_sidecar_still_refuses_a_mismatched_size(durable_tmp_path):
    # The fallback is size-only, not no-guard-at-all: with the sidecar gone, a
    # genuine size mismatch must still refuse.
    from runtime.numeric_state import NumericStateShapeMismatch

    written_spec = NumericStateSpec(name="rolling-window", shape=(10,), dtype="float64")
    with open_numeric_state(durable_tmp_path, written_spec) as state:
        state.array[:] = 1.0

    declaration_path = durable_tmp_path / f"{written_spec.name}.declared.json"
    declaration_path.unlink()

    larger_spec = NumericStateSpec(name="rolling-window", shape=(20,), dtype="float64")
    with pytest.raises(NumericStateShapeMismatch):
        open_numeric_state(durable_tmp_path, larger_spec)


def test_the_sidecar_is_created_on_first_use_with_what_was_declared(durable_tmp_path):
    import json

    spec = NumericStateSpec(name="rolling-window", shape=(10,), dtype="float64")
    declaration_path = durable_tmp_path / f"{spec.name}.declared.json"
    assert not declaration_path.exists()

    with open_numeric_state(durable_tmp_path, spec):
        pass

    assert declaration_path.exists()
    declared = json.loads(declaration_path.read_text())
    assert tuple(declared["shape"]) == spec.shape
    assert declared["dtype"] == spec.dtype


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


def test_a_data_file_with_no_sidecar_gets_one_healed_and_is_then_guarded(durable_tmp_path):
    # Window 1: a sidecar written only after the data file leaves a kill window
    # where data exists with no sidecar, and every later open used to take the
    # "missing sidecar -> size-only" path forever. Healing on reopen closes it:
    # the very next open must both create the sidecar and be guarded by it.
    from runtime.numeric_state import NumericStateShapeMismatch

    written_spec = NumericStateSpec(name="rolling-window", shape=(10,), dtype="float64")
    with open_numeric_state(durable_tmp_path, written_spec) as state:
        state.array[:] = 1.0

    declaration_path = durable_tmp_path / f"{written_spec.name}.declared.json"
    declaration_path.unlink()
    assert not declaration_path.exists()

    with open_numeric_state(durable_tmp_path, written_spec):
        pass
    assert declaration_path.exists(), "the missing sidecar was not healed on reopen"

    same_size_different_dtype_spec = NumericStateSpec(
        name="rolling-window", shape=(10,), dtype="int64"
    )
    with pytest.raises(NumericStateShapeMismatch):
        open_numeric_state(durable_tmp_path, same_size_different_dtype_spec)


def test_a_zero_byte_sidecar_refuses_with_the_named_exception_not_a_decoder_error(
    durable_tmp_path,
):
    # Window 2: Path.write_text truncates before writing, so a kill mid-write
    # used to leave a 0-byte sidecar that exists but cannot be parsed -- and
    # json.loads("") raises json.JSONDecodeError, which a caller catching
    # NumericStateShapeMismatch does not catch. The atomic write should make
    # this unreachable; this test is what would catch it if it were not.
    from runtime.numeric_state import NumericStateShapeMismatch

    spec = NumericStateSpec(name="rolling-window", shape=(10,), dtype="float64")
    with open_numeric_state(durable_tmp_path, spec) as state:
        state.array[:] = 1.0

    declaration_path = durable_tmp_path / f"{spec.name}.declared.json"
    declaration_path.write_text("")

    with pytest.raises(NumericStateShapeMismatch):
        open_numeric_state(durable_tmp_path, spec)


def test_a_sidecar_holding_text_that_is_not_json_refuses_with_the_named_exception(
    durable_tmp_path,
):
    from runtime.numeric_state import NumericStateShapeMismatch

    spec = NumericStateSpec(name="rolling-window", shape=(10,), dtype="float64")
    with open_numeric_state(durable_tmp_path, spec) as state:
        state.array[:] = 1.0

    declaration_path = durable_tmp_path / f"{spec.name}.declared.json"
    declaration_path.write_text("not json at all {")

    with pytest.raises(NumericStateShapeMismatch):
        open_numeric_state(durable_tmp_path, spec)


def test_a_sidecar_with_no_data_file_opens_cleanly_when_the_spec_agrees(durable_tmp_path):
    # The surviving state from writing the sidecar before the data file: a kill
    # in that window leaves a sidecar with no data. The next open under an
    # agreeing spec must proceed and create the data file.
    import json

    spec = NumericStateSpec(name="rolling-window", shape=(10,), dtype="float64")
    declaration_path = durable_tmp_path / f"{spec.name}.declared.json"
    declaration_path.write_text(json.dumps({"shape": list(spec.shape), "dtype": spec.dtype}))
    data_path = durable_tmp_path / f"{spec.name}.f64"
    assert not data_path.exists()

    with open_numeric_state(durable_tmp_path, spec) as state:
        state.array[:] = 4.0
    assert data_path.exists()

    with open_numeric_state(durable_tmp_path, spec) as reopened:
        assert bool((reopened.array == 4.0).all())


def test_a_sidecar_with_no_data_file_refuses_when_the_spec_disagrees(durable_tmp_path):
    import json

    from runtime.numeric_state import NumericStateShapeMismatch

    written_spec = NumericStateSpec(name="rolling-window", shape=(10,), dtype="float64")
    declaration_path = durable_tmp_path / f"{written_spec.name}.declared.json"
    declaration_path.write_text(
        json.dumps({"shape": list(written_spec.shape), "dtype": written_spec.dtype})
    )
    data_path = durable_tmp_path / f"{written_spec.name}.f64"

    different_spec = NumericStateSpec(name="rolling-window", shape=(10,), dtype="int64")
    with pytest.raises(NumericStateShapeMismatch):
        open_numeric_state(durable_tmp_path, different_spec)
    assert not data_path.exists(), "the data file must not be created after a refusal"


def test_no_temporary_declaration_file_is_left_behind(durable_tmp_path):
    spec = NumericStateSpec(name="rolling-window", shape=(10,), dtype="float64")
    with open_numeric_state(durable_tmp_path, spec) as state:  # fresh creation
        state.array[:] = 1.0
    with open_numeric_state(durable_tmp_path, spec):  # ordinary reopen, verified
        pass

    declaration_path = durable_tmp_path / f"{spec.name}.declared.json"
    declaration_path.unlink()
    with open_numeric_state(durable_tmp_path, spec):  # healed reopen
        pass

    leftover_temporary_files = [
        entry.name for entry in durable_tmp_path.iterdir() if entry.name.endswith(".tmp")
    ]
    assert leftover_temporary_files == []
