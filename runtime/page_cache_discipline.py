"""Write a stream to disk without accumulating page cache the cgroup will kill you for.

A part's memory.max bounds its heap plus the page cache it dirties, and this box
has no swap (section 5). Measured on ext4 under MemoryMax=200M, writing 500 MB:

    never fsync                      OOM-killed, 3 of 3
    fsync every 8 MB                 survives, pinned at the 200 MB ceiling
    fsync + FADV_DONTNEED            survives, 16 MB peak

Dirty pages are not reclaimable, so a writer that outruns writeback dies. fsync
alone is enough to live, but it leaves the part sitting on its whole limit in clean
cache, so part-appetite-meter would size every writer at its cap. Handing the range
back after writing it costs one syscall and twelve times less reserved memory.
"""

from __future__ import annotations

import os
import pathlib

from runtime.storage_facts import require_durable_directory


def read_own_cgroup_memory_peak_bytes() -> int | None:
    """The high-water memory this process's cgroup reached, or None if unreadable.

    memory.peak is the honest number here: a process's own RSS does not predict an
    OOM kill, because the page cache it dirtied is charged to the cgroup and not to it.
    """
    try:
        relative = open("/proc/self/cgroup").read().strip().split("::")[1]
    except (OSError, IndexError):
        return None
    for filename in ("memory.peak", "memory.current"):
        try:
            return int(pathlib.Path("/sys/fs/cgroup" + relative, filename).read_text().strip())
        except (OSError, ValueError):
            continue
    return None


class CacheReleasingWriter:
    """Append-only writer that forces writeback and releases the written range.

    The interval is a named setting with provenance (runtime.toml
    'writeback_interval'), never a literal here -- RL-061.
    """

    def __init__(
        self,
        path: pathlib.Path,
        writeback_interval_bytes: int,
        append: bool = False,
    ) -> None:
        """Open a stream for writing, releasing its page cache as it goes.

        append=False truncates, which is right for a file written once. append=True
        continues an existing file, which is what a tape needs: a part is SIGKILLed
        as the ordinary way of switching it off (section 4), so a tape writer that
        truncated on open would erase the day's capture every time its part
        restarted. That is not a hypothetical -- restart is the normal path.

        When appending, the byte counters start at the file's existing size, because
        posix_fadvise takes absolute file offsets: counting from zero on a resumed
        file would hand the kernel the wrong range and release pages belonging to
        data this writer never wrote.
        """
        path = pathlib.Path(path)
        require_durable_directory(path.parent)
        if writeback_interval_bytes <= 0:
            raise ValueError(
                "writeback_interval_bytes must be positive; a writer that never forces "
                "writeback is OOM-killed under a cgroup memory limit (section 5)"
            )
        self._path = path
        self._interval = writeback_interval_bytes
        already_on_disk = path.stat().st_size if append and path.exists() else 0
        self._handle = open(path, "ab" if append else "wb")
        self._descriptor = self._handle.fileno()
        self._bytes_written = already_on_disk
        self._released_to = already_on_disk

    @property
    def path(self) -> pathlib.Path:
        return self._path

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    def append(self, block: bytes) -> int:
        """Append a block, forcing writeback and releasing cache once per interval."""
        self._handle.write(block)
        self._bytes_written += len(block)
        if self._bytes_written - self._released_to >= self._interval:
            self.force_writeback()
        return self._bytes_written

    def force_writeback(self) -> None:
        """Flush to the kernel, fsync to disk, then hand the written range back.

        POSIX_FADV_DONTNEED only drops *clean* pages, so the fsync is not optional
        decoration -- without it there is nothing clean to drop.
        """
        self._handle.flush()
        os.fsync(self._descriptor)
        length = self._bytes_written - self._released_to
        if length > 0:
            os.posix_fadvise(
                self._descriptor, self._released_to, length, os.POSIX_FADV_DONTNEED
            )
            self._released_to = self._bytes_written

    def close(self) -> None:
        if self._handle.closed:
            return
        self.force_writeback()
        self._handle.close()

    def __enter__(self) -> "CacheReleasingWriter":
        return self

    def __exit__(self, *exception) -> None:
        self.close()
