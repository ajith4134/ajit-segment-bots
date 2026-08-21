"""A part's memory limit counts the page cache it dirties, and there is no swap.

Section 5: writing 500 MB into a 200 MB cgroup is an OOM kill if the writer never
forces writeback, survives at the ceiling if it only fsyncs, and holds at 16 MB if
it also hands the written range back with FADV_DONTNEED. These tests hold that.

The cgroup tests run a real process under a real systemd transient scope, so they
are marked and they are slow. They are also the only honest way to check this:
a process's own RSS does not predict the kill.
"""

import os
import pathlib
import subprocess
import sys

import pytest

from runtime.page_cache_discipline import CacheReleasingWriter

WRITER_UNDER_LIMIT = """
import pathlib, sys
sys.path.insert(0, {repository!r})
from runtime.page_cache_discipline import CacheReleasingWriter, read_own_cgroup_memory_peak_bytes

path, total_bytes, interval = pathlib.Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
block = b"\\x5a" * 65536
with CacheReleasingWriter(path, writeback_interval_bytes=interval) as writer:
    while writer.bytes_written < total_bytes:
        writer.append(block)
print("PEAK", read_own_cgroup_memory_peak_bytes())
"""


def _run_under_memory_limit(script: str, arguments: list[str], megabytes: int, unit: str):
    return subprocess.run(
        [
            "systemd-run", "--user", "--scope", "--quiet",
            f"-p", f"MemoryMax={megabytes}M", "-p", "MemorySwapMax=0",
            f"--unit={unit}", sys.executable, "-c", script, *arguments,
        ],
        capture_output=True, text=True,
    )


def test_appends_are_readable_and_the_byte_count_is_what_was_written(durable_tmp_path):
    path = durable_tmp_path / "tape.bin"
    with CacheReleasingWriter(path, writeback_interval_bytes=4096) as writer:
        writer.append(b"one")
        writer.append(b"two")
        assert writer.bytes_written == 6
    assert path.read_bytes() == b"onetwo"


def test_refuses_a_path_whose_pages_are_memory(tmp_path):
    # pytest's own tmp_path is under /tmp, which is tmpfs on this box -- exactly
    # the trap section 5 records. The writer must not accept it.
    from runtime.storage_facts import VolatileStorageRefused

    with pytest.raises(VolatileStorageRefused):
        CacheReleasingWriter(tmp_path / "tape.bin", writeback_interval_bytes=4096)


@pytest.mark.cgroup
@pytest.mark.slow
def test_a_writer_that_drops_its_cache_stays_far_below_its_memory_limit(durable_tmp_path):
    repository = str(pathlib.Path(__file__).resolve().parents[2])
    script = WRITER_UNDER_LIMIT.format(repository=repository)
    total = 500 * 1024 * 1024
    interval = 8 * 1024 * 1024

    completed = _run_under_memory_limit(
        script, [str(durable_tmp_path / "tape.bin"), str(total), str(interval)],
        megabytes=200, unit=f"pagecache-drop-{os.getpid()}",
    )

    assert completed.returncode == 0, f"OOM-killed or failed: {completed.stderr[-400:]}"
    peak = int(completed.stdout.split("PEAK")[1].strip())
    assert peak < 64 * 1024 * 1024, f"peaked at {peak} bytes; dropping the cache should hold it near 16 MB"


@pytest.mark.cgroup
@pytest.mark.slow
def test_a_writer_that_never_forces_writeback_is_killed_by_the_kernel(durable_tmp_path):
    # The control. Without this the test above proves only that something ran.
    naive = """
import pathlib, sys
path, total = pathlib.Path(sys.argv[1]), int(sys.argv[2])
block = b"\\x5a" * 65536
written = 0
with open(path, "wb") as handle:
    while written < total:
        handle.write(block); written += len(block)
print("SURVIVED")
"""
    completed = _run_under_memory_limit(
        naive, [str(durable_tmp_path / "naive.bin"), str(500 * 1024 * 1024)],
        megabytes=200, unit=f"pagecache-naive-{os.getpid()}",
    )
    assert completed.returncode != 0, "a 500 MB unflushed write survived a 200 MB limit; re-check the limit was applied"
    assert "SURVIVED" not in completed.stdout
