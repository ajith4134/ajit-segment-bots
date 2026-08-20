"""Symmetric memory-pressure test: SQLite WAL vs LMDB writing the same volume
inside the same cgroup memory.max. Run INSIDE a systemd transient scope."""
import argparse, os, sqlite3, sys, time

def cg_path():
    cg = open("/proc/self/cgroup").read().strip().split("::")[1]
    return "/sys/fs/cgroup" + cg

def peak():
    try: return int(open(cg_path()+"/memory.peak").read().strip())
    except OSError:
        try: return int(open(cg_path()+"/memory.current").read().strip())
        except OSError: return -1

def run_sqlite(path, total_mb, rec_bytes, commit_mb, synchronous):
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(f"PRAGMA synchronous={synchronous}")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("CREATE TABLE IF NOT EXISTS journal("
                "id INTEGER PRIMARY KEY, part TEXT, ts_ns INTEGER, kind TEXT, payload BLOB)")
    blob = b"\x5a" * rec_bytes
    n = (total_mb << 20) // rec_bytes
    per_commit = max(1, (commit_mb << 20) // rec_bytes)
    con.execute("BEGIN IMMEDIATE")
    for i in range(n):
        con.execute("INSERT INTO journal(part,ts_ns,kind,payload) VALUES(?,?,?,?)",
                    ("market-data-feed", time.monotonic_ns(), "switch-record", blob))
        if (i + 1) % per_commit == 0:
            con.execute("COMMIT"); con.execute("BEGIN IMMEDIATE")
    con.execute("COMMIT")
    rows = con.execute("SELECT count(*) FROM journal").fetchone()[0]
    con.close()
    return n, rows

def run_lmdb(path, total_mb, rec_bytes, commit_mb, map_gib):
    import lmdb
    env = lmdb.open(path, map_size=map_gib << 30, writemap=False, sync=True, subdir=True)
    blob = b"\x5a" * rec_bytes
    n = (total_mb << 20) // rec_bytes
    per_commit = max(1, (commit_mb << 20) // rec_bytes)
    written = 0
    while written < n:
        with env.begin(write=True) as txn:
            for _ in range(min(per_commit, n - written)):
                txn.put(b"k%012d" % written, blob)
                written += 1
    with env.begin() as txn:
        rows = txn.stat()["entries"]
    env.close()
    return n, rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", choices=["sqlite", "lmdb"], required=True)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--total-mb", type=int, default=500, help="bytes of payload written")
    ap.add_argument("--record-bytes", type=int, default=4096)
    ap.add_argument("--commit-mb", type=int, default=8, help="payload per transaction")
    ap.add_argument("--synchronous", default="FULL", help="sqlite only")
    ap.add_argument("--map-gib", type=int, default=2, help="lmdb only")
    a = ap.parse_args()
    os.makedirs(a.dir, exist_ok=True)
    t0 = time.perf_counter()
    if a.store == "sqlite":
        n, rows = run_sqlite(os.path.join(a.dir, "journal.db"), a.total_mb,
                             a.record_bytes, a.commit_mb, a.synchronous)
    else:
        n, rows = run_lmdb(os.path.join(a.dir, "lmdbenv"), a.total_mb,
                           a.record_bytes, a.commit_mb, a.map_gib)
    dt = time.perf_counter() - t0
    print(f"RESULT store={a.store} intended={n} stored={rows} seconds={dt:.2f} "
          f"records_per_sec={n/dt:.0f} cgroup_peak_bytes={peak()}")

if __name__ == "__main__":
    main()
