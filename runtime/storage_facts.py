"""Which filesystem a path is on, and whether it is real disk.

This exists because /tmp on this box is tmpfs. A part that writes its state there
would lose it on reboot while appearing to work, and a memory measurement taken
there is measuring RAM against RAM. Section 5 of the runtime spec records the
round of measurement lost to that. require_durable_directory is the refusal.
"""

from __future__ import annotations

import pathlib

MOUNTINFO_PATH = pathlib.Path("/proc/self/mountinfo")

# Filesystems whose pages are RAM. tmpfs and ramfs hold no disk behind them, so
# nothing written to them survives a reboot and everything written to them is
# charged to the writing cgroup as unreclaimable memory.
MEMORY_BACKED_FILESYSTEMS = frozenset({"tmpfs", "ramfs", "devtmpfs"})


class VolatileStorageRefused(RuntimeError):
    """A durable directory was required and a memory-backed one was offered."""


def _read_mount_table() -> list[tuple[str, str]]:
    """Return (mount point, filesystem type) for every mount, longest path last."""
    mounts: list[tuple[str, str]] = []
    for line in MOUNTINFO_PATH.read_text().splitlines():
        # mountinfo: id parent major:minor root mount-point options... - fstype source
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        remainder = after.split()
        if len(fields) < 5 or not remainder:
            continue
        mounts.append((fields[4], remainder[0]))
    mounts.sort(key=lambda entry: len(entry[0]))
    return mounts


def _nearest_existing_ancestor(path: pathlib.Path) -> pathlib.Path:
    candidate = path.absolute()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def read_filesystem_type(path: pathlib.Path) -> str:
    """Name the filesystem holding this path, resolving it if it does not exist yet."""
    resolved = _nearest_existing_ancestor(pathlib.Path(path)).resolve()
    winner = "unknown"
    for mount_point, filesystem_type in _read_mount_table():
        mount = pathlib.Path(mount_point)
        if resolved == mount or mount in resolved.parents:
            winner = filesystem_type
    return winner


def is_memory_backed_filesystem(path: pathlib.Path) -> bool:
    """Is this path's storage RAM rather than disk?"""
    return read_filesystem_type(path) in MEMORY_BACKED_FILESYSTEMS


def require_durable_directory(path: pathlib.Path) -> pathlib.Path:
    """Return the path, or refuse it because what is written there would not survive.

    Every store and every durability or memory measurement passes its directory
    through here first.
    """
    filesystem_type = read_filesystem_type(path)
    if filesystem_type in MEMORY_BACKED_FILESYSTEMS:
        raise VolatileStorageRefused(
            f"{path} is on {filesystem_type}, which is RAM. State written there does "
            f"not survive a reboot, and memory measured there is measuring RAM against "
            f"RAM. Choose a directory on real disk -- see section 5 of the runtime spec."
        )
    return pathlib.Path(path)
