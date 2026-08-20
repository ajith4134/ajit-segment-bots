"""SQLite WAL writer that checkpoints and drops cache as it goes."""
import argparse, os, sqlite3, time
ap=argparse.ArgumentParser()
ap.add_argument("--dir", required=True); ap.add_argument("--total-mb", type=int, default=500)
ap.add_argument("--record-bytes", type=int, default=4096)
ap.add_argument("--commit-mb", type=int, default=8)
ap.add_argument("--checkpoint-every-mb", type=int, default=0, help="0 = rely on autocheckpoint")
ap.add_argument("--drop-cache", action="store_true")
ap.add_argument("--synchronous", default="FULL")
a=ap.parse_args(); os.makedirs(a.dir, exist_ok=True)
def peak():
    cg=open("/proc/self/cgroup").read().strip().split("::")[1]
    for f in ("memory.peak","memory.current"):
        try: return int(open("/sys/fs/cgroup"+cg+"/"+f).read().strip())
        except OSError: pass
    return -1
path=os.path.join(a.dir,"journal.db")
con=sqlite3.connect(path, isolation_level=None)
con.execute("PRAGMA journal_mode=WAL"); con.execute(f"PRAGMA synchronous={a.synchronous}")
con.execute("PRAGMA busy_timeout=5000")
con.execute("CREATE TABLE IF NOT EXISTS journal(id INTEGER PRIMARY KEY, part TEXT, ts_ns INTEGER, kind TEXT, payload BLOB)")
blob=b"\x5a"*a.record_bytes; n=(a.total_mb<<20)//a.record_bytes
per=max(1,(a.commit_mb<<20)//a.record_bytes)
ckpt=(a.checkpoint_every_mb<<20)//a.record_bytes if a.checkpoint_every_mb else 0
t0=time.perf_counter(); con.execute("BEGIN IMMEDIATE")
for i in range(n):
    con.execute("INSERT INTO journal(part,ts_ns,kind,payload) VALUES(?,?,?,?)",
                ("market-data-feed", time.monotonic_ns(), "switch-record", blob))
    if (i+1)%per==0:
        con.execute("COMMIT")
        if ckpt and (i+1)%ckpt==0:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if a.drop_cache:
                for suffix in ("", "-wal"):
                    try:
                        fd=os.open(path+suffix, os.O_RDONLY)
                        os.posix_fadvise(fd,0,0,os.POSIX_FADV_DONTNEED); os.close(fd)
                    except OSError: pass
        con.execute("BEGIN IMMEDIATE")
con.execute("COMMIT")
rows=con.execute("SELECT count(*) FROM journal").fetchone()[0]; con.close()
print(f"RESULT sqlite ckpt_mb={a.checkpoint_every_mb} drop={a.drop_cache} intended={n} stored={rows} "
      f"seconds={time.perf_counter()-t0:.2f} cgroup_peak_bytes={peak()}")
