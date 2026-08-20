"""Fair glue comparison: setup excluded from the timed region for BOTH threads and
processes, via a barrier. Each worker times only its own aggregation."""
import argparse, os, sys, sysconfig, time, threading
import multiprocessing as mp
from glue_workload import make_stream, aggregate

def timed_worker(stream, window, reps, barrier, sink, idx):
    barrier.wait()
    t0 = time.perf_counter()
    for _ in range(reps): aggregate(stream, window)
    sink[idx] = time.perf_counter() - t0

def proc_timed(items, window, reps, seed, barrier, q, idx):
    stream = make_stream(items, seed)          # setup, before the barrier
    barrier.wait()
    t0 = time.perf_counter()
    for _ in range(reps): aggregate(stream, window)
    q.put((idx, time.perf_counter() - t0))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", type=int, default=150_000)
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    ft = sysconfig.get_config_var("Py_GIL_DISABLED") == 1
    unit = a.items * a.reps
    print(f"build={sys.version.split()[0]} free_threaded={bool(ft)} workers={a.workers} "
          f"work_per_worker={unit} tuples")

    streams = [make_stream(a.items, 1000 + i) for i in range(a.workers)]

    t0 = time.perf_counter()
    for _ in range(a.reps): aggregate(streams[0], a.window)
    serial = time.perf_counter() - t0
    print(f"  serial 1 worker : {serial:7.3f}s  {unit/serial/1e6:6.3f} M tuples/s   [baseline]")

    b = threading.Barrier(a.workers); sink = [0.0]*a.workers
    ths = [threading.Thread(target=timed_worker, args=(streams[i], a.window, a.reps, b, sink, i))
           for i in range(a.workers)]
    t0 = time.perf_counter()
    for t in ths: t.start()
    for t in ths: t.join()
    wall_t = time.perf_counter() - t0
    agg_t = unit*a.workers/max(sink)
    print(f"  {a.workers} threads      : slowest worker {max(sink):.3f}s  aggregate {agg_t/1e6:6.3f} M tuples/s"
          f"   speedup vs serial {agg_t/(unit/serial):.2f}x")

    ctx = mp.get_context("forkserver")
    pb = ctx.Barrier(a.workers); q = ctx.Queue()
    ps = [ctx.Process(target=proc_timed, args=(a.items, a.window, a.reps, 1000+i, pb, q, i))
          for i in range(a.workers)]
    for p in ps: p.start()
    times = [q.get()[1] for _ in ps]
    for p in ps: p.join()
    agg_p = unit*a.workers/max(times)
    print(f"  {a.workers} processes    : slowest worker {max(times):.3f}s  aggregate {agg_p/1e6:6.3f} M tuples/s"
          f"   speedup vs serial {agg_p/(unit/serial):.2f}x")

if __name__ == "__main__":
    for v in ("OPENBLAS_NUM_THREADS","OMP_NUM_THREADS","MKL_NUM_THREADS",
              "VECLIB_MAXIMUM_THREADS","NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(v, "1")
    main()
