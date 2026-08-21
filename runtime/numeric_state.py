"""Durable numeric part state: rolling windows and learned parameters.

File-backed numpy.memmap (section 15.4). numpy has no API to close the underlying
mmap and does not need one: dirty MAP_SHARED pages live in the page cache, which
belongs to the inode rather than the process, so they are written back whether or
not any userspace cleanup runs -- which under SIGKILL is none of it. Measured on
ext4, 6 of 6 trials, 100% of 2 000 000 float64 elements survived unflushed.

multiprocessing.shared_memory is deliberately not the fallback. It is /dev/shm,
which is RAM: not durable across an off/on cycle, and memory that stays resident
while a part is off is exactly what T-3 forbids. Its resource tracker also still
carries cpython#82300. Its only role here is hot live-to-live transport between two
running parts, with track=False, never as a store.

The one way to reintroduce numpy's fd wart is to map part state into a process that
never exits. The governor must not do that.
"""

from __future__ import annotations

import json
import math
import pathlib
from dataclasses import dataclass

import numpy

from runtime.storage_facts import require_durable_directory

# The stamp lives beside the data rather than inside it, so the array keeps the
# exact shape and dtype the part declared and nothing has to reserve a row.
STAMP_DTYPE = "int64"
STAMP_SUFFIX = ".stamp"
DATA_SUFFIX = ".f64"
NO_STAMP_RECORDED = 0

# What was declared when the state was created, recorded beside the data rather
# than smuggled into a memmap header. A byte-size check alone cannot tell
# float64[10] from int64[10] -- both are 80 bytes -- so the declared dtype is
# what closes that gap; the size check stays, and answers a different question
# (a file truncated or corrupted by something outside this module).
DECLARATION_SUFFIX = ".declared.json"


class NumericStateShapeMismatch(RuntimeError):
    """An existing file disagrees with what the spec now declares.

    Resizing a part's numeric state -- a rolling window growing, a learned
    parameter block changing shape or dtype -- is a deliberate migration, never
    something that happens by opening the file under a different spec.
    numpy.memmap would otherwise do the shape half of this silently: 'r+' mode
    zero-pads a file that is too small and happily maps only a truncated subset
    of one that is too large. A same-size dtype swap is silent in a worse way --
    no size disagreement at all, just every value read back as reinterpreted
    garbage. Either is the same quiet corruption has_state_gap exists to catch
    on the other side of a crash, arriving instead through a redeploy that
    changed a shape or a dtype.
    """


@dataclass(frozen=True)
class NumericStateSpec:
    """A fixed shape and dtype, which is what makes this memmappable at all.

    dtype=object arrays cannot be memory-mapped: they are arrays of pointers into
    the Python heap, and a pointer means nothing in another process.
    """

    name: str
    shape: tuple[int, ...]
    dtype: str


class NumericState:
    """One part's numeric state, and the sequence stamp that dates it."""

    def __init__(self, array: numpy.memmap, stamp: numpy.memmap) -> None:
        self.array = array
        self._stamp = stamp

    def record_sequence_stamp(self, stamp: int) -> None:
        """Record how far this state has been updated to."""
        self._stamp[0] = stamp

    def read_sequence_stamp(self) -> int:
        return int(self._stamp[0])

    def force_writeback(self) -> None:
        """Make writeback happen now rather than whenever the kernel gets to it.

        Not what makes the state durable -- that is unconditional. This is for
        ordering, when two files must be consistent with each other at a known moment.
        """
        self.array.flush()
        self._stamp.flush()

    def close(self) -> None:
        """Drop the references so CPython's refcounting unmaps them.

        There is no close() on numpy.memmap, and numpy#13510 has been open since 2019
        because there is no safe way to add one -- a memmap can be aliased by other
        ndarray views and closing under a live view segfaults the interpreter. Dropping
        the last reference is the supported route.
        """
        self.array = None
        self._stamp = None

    def __enter__(self) -> "NumericState":
        return self

    def __exit__(self, *exception) -> None:
        self.close()


def open_numeric_state(directory: pathlib.Path, spec: NumericStateSpec) -> NumericState:
    """Map a part's numeric state, creating it on first use.

    Turning a part on is ordinary startup: remap, and carry on. There is no restore
    path because startup already is one (section 4).
    """
    directory = require_durable_directory(pathlib.Path(directory))
    directory.mkdir(parents=True, exist_ok=True)
    data_path = directory / f"{spec.name}{DATA_SUFFIX}"
    stamp_path = directory / f"{spec.name}{STAMP_SUFFIX}"
    declaration_path = directory / f"{spec.name}{DECLARATION_SUFFIX}"

    if numpy.dtype(spec.dtype).hasobject:
        raise ValueError(
            f"{spec.name} declares dtype {spec.dtype}, which holds Python objects. "
            f"An array of pointers into one process's heap cannot be shared or made durable."
        )

    data_exists = data_path.exists()
    if data_exists:
        expected_bytes = numpy.dtype(spec.dtype).itemsize * math.prod(spec.shape)
        actual_bytes = data_path.stat().st_size
        if actual_bytes != expected_bytes:
            raise NumericStateShapeMismatch(
                f"{data_path} holds {actual_bytes} bytes, but spec {spec.name} "
                f"(shape={spec.shape}, dtype={spec.dtype}) implies {expected_bytes} bytes. "
                f"Resizing a part's numeric state is a deliberate migration, not something "
                f"that happens by opening it under a different spec."
            )

        # A missing sidecar is not a refusal -- it means this state was written
        # before the guard existed, and the size check above is all there is to
        # fall back on. That costs nothing on data the sidecar was there for.
        if declaration_path.exists():
            declared = json.loads(declaration_path.read_text())
            declared_shape = tuple(declared["shape"])
            declared_dtype = declared["dtype"]
            if declared_shape != spec.shape or declared_dtype != spec.dtype:
                raise NumericStateShapeMismatch(
                    f"{declaration_path} declares shape={declared_shape} "
                    f"dtype={declared_dtype}, but spec {spec.name} asks for "
                    f"shape={spec.shape} dtype={spec.dtype}. Resizing or retyping a "
                    f"part's numeric state is a deliberate migration, not something "
                    f"that happens by opening it under a different spec."
                )

    mode = "r+" if data_exists else "w+"
    array = numpy.memmap(data_path, dtype=spec.dtype, mode=mode, shape=spec.shape)
    stamp_mode = "r+" if stamp_path.exists() else "w+"
    stamp = numpy.memmap(stamp_path, dtype=STAMP_DTYPE, mode=stamp_mode, shape=(1,))
    if stamp_mode == "w+":
        stamp[0] = NO_STAMP_RECORDED
    if mode == "w+":
        declaration_path.write_text(json.dumps({"shape": list(spec.shape), "dtype": spec.dtype}))
    return NumericState(array=array, stamp=stamp)


def has_state_gap(state: NumericState, published_stamp: int) -> bool:
    """Did this part die between updating its state and publishing how far it got?

    On switch-on the part compares its own stamp against the last one the store
    recorded. A divergence is a fault to report, never a reason to silently reseed --
    a rolling window that quietly restarts is section 6's silently-biased indicator
    arriving by another route.
    """
    return state.read_sequence_stamp() < published_stamp
