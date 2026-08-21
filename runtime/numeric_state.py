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
import os
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

# Written to first, then renamed over the real sidecar with os.replace, which is
# atomic on the same filesystem. A reader never sees a torn or empty declaration
# this way -- Path.write_text truncates the file it opens, so writing straight to
# DECLARATION_SUFFIX would leave a real window where a kill mid-write leaves a
# 0-byte file that exists but cannot be parsed.
TEMPORARY_DECLARATION_SUFFIX = ".tmp"


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

    Also raised, rather than a bare decoder error, when the declaration sidecar
    exists but cannot be read as one -- the atomic write below should make that
    unreachable, but a reader fails closed with the right type regardless.
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


def _expected_data_byte_count(spec: NumericStateSpec) -> int:
    return numpy.dtype(spec.dtype).itemsize * math.prod(spec.shape)


def _write_declaration_atomically(declaration_path: pathlib.Path, spec: NumericStateSpec) -> None:
    """Write the sidecar so a reader never sees it torn or empty.

    Written to a temporary file in the same directory first, then moved into
    place with os.replace, which is atomic on the same filesystem: the final
    name either shows the complete write or is untouched, never a partial one.
    """
    temporary_path = declaration_path.parent / f"{declaration_path.name}{TEMPORARY_DECLARATION_SUFFIX}"
    temporary_path.write_text(json.dumps({"shape": list(spec.shape), "dtype": spec.dtype}))
    os.replace(temporary_path, declaration_path)


def _verify_declaration_matches(declaration_path: pathlib.Path, spec: NumericStateSpec) -> None:
    """Refuse, with this module's own exception, rather than let a reader see
    a decoder error for a sidecar the atomic write should make unreachable.
    """
    try:
        declared = json.loads(declaration_path.read_text())
        declared_shape = tuple(declared["shape"])
        declared_dtype = declared["dtype"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as unreadable:
        raise NumericStateShapeMismatch(
            f"{declaration_path} could not be read as a declaration ({unreadable}). "
            f"The sidecar beside a part's numeric state must hold its declared shape "
            f"and dtype; one that cannot be read is refused rather than silently "
            f"reinterpreted or replaced."
        ) from unreadable
    if declared_shape != spec.shape or declared_dtype != spec.dtype:
        raise NumericStateShapeMismatch(
            f"{declaration_path} declares shape={declared_shape} dtype={declared_dtype}, "
            f"but spec {spec.name} asks for shape={spec.shape} dtype={spec.dtype}. "
            f"Resizing or retyping a part's numeric state is a deliberate migration, not "
            f"something that happens by opening it under a different spec."
        )


def open_numeric_state(directory: pathlib.Path, spec: NumericStateSpec) -> NumericState:
    """Map a part's numeric state, creating it on first use.

    Turning a part on is ordinary startup: remap, and carry on. There is no restore
    path because startup already is one (section 4).

    The declaration sidecar is written before the data file, not after: the state
    that survives a kill mid-creation is then a sidecar with no data, which the
    next open verifies and creates the data file under -- never data with no
    sidecar, which is the unguarded case. A data file that already exists with no
    sidecar is instead healed once the size check on it has passed: this blesses
    the first spec a pre-guard file happens to be reopened under, which is no
    weaker than the size-only fallback it replaces, and every open after that one
    is guarded. That trade is deliberate.
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
        expected_bytes = _expected_data_byte_count(spec)
        actual_bytes = data_path.stat().st_size
        if actual_bytes != expected_bytes:
            raise NumericStateShapeMismatch(
                f"{data_path} holds {actual_bytes} bytes, but spec {spec.name} "
                f"(shape={spec.shape}, dtype={spec.dtype}) implies {expected_bytes} bytes. "
                f"Resizing a part's numeric state is a deliberate migration, not something "
                f"that happens by opening it under a different spec."
            )

    if declaration_path.exists():
        _verify_declaration_matches(declaration_path, spec)
    else:
        # Neither missing state (data_exists is False, ordinary fresh creation)
        # nor a stranded one (data_exists is True, the size check above already
        # passed) is a refusal here -- both are the sanctioned paths the two
        # window fixes above exist to route through this single write.
        _write_declaration_atomically(declaration_path, spec)

    mode = "r+" if data_exists else "w+"
    array = numpy.memmap(data_path, dtype=spec.dtype, mode=mode, shape=spec.shape)
    stamp_mode = "r+" if stamp_path.exists() else "w+"
    stamp = numpy.memmap(stamp_path, dtype=STAMP_DTYPE, mode=stamp_mode, shape=(1,))
    if stamp_mode == "w+":
        stamp[0] = NO_STAMP_RECORDED
    return NumericState(array=array, stamp=stamp)


def has_state_gap(state: NumericState, published_stamp: int) -> bool:
    """Did this part die between updating its state and publishing how far it got?

    On switch-on the part compares its own stamp against the last one the store
    recorded. A divergence is a fault to report, never a reason to silently reseed --
    a rolling window that quietly restarts is section 6's silently-biased indicator
    arriving by another route.
    """
    return state.read_sequence_stamp() < published_stamp
