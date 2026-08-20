"""Same write, but the writer returns its own page cache to the kernel as it goes."""
import argparse, os, time
ap=argparse.ArgumentParser()
ap.add_argument("--dir", required=True); ap.add_argument("--total-mb", type=int, default=500)
ap.add_argument("--chunk-kb", type=int, default=64)
ap.add_argument("--writeback-every-mb", type=int, default=8)
ap.add_argument("--drop-cache", action="store_true", help="posix_fadvise DONTNEED after writeback")
a=ap.parse_args(); os.makedirs(a.dir, exist_ok=True)
def peak():
    cg=open("/proc/self/cgroup").read().strip().split("::")[1]
    for f in ("memory.peak","memory.current"):
        try: return int(open("/sys/fs/cgroup"+cg+"/"+f).read().strip())
        except OSError: pass
    return -1
buf=b"\x5a"*(a.chunk_kb<<10); written=0; dropped_to=0; t0=time.perf_counter()
step=a.writeback_every_mb<<20
with open(os.path.join(a.dir,"tape.bin"),"wb") as f:
    fd=f.fileno()
    while written < (a.total_mb<<20):
        f.write(buf); written += len(buf)
        if step and written % step == 0:
            f.flush(); os.fsync(fd)
            if a.drop_cache:
                os.posix_fadvise(fd, dropped_to, written-dropped_to, os.POSIX_FADV_DONTNEED)
                dropped_to = written
    f.flush(); os.fsync(fd)
print(f"RESULT plainfile2 mb={a.total_mb} drop_cache={a.drop_cache} "
      f"seconds={time.perf_counter()-t0:.2f} cgroup_peak_bytes={peak()}")
