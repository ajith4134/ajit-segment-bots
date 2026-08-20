"""Pure-Python 'glue' workload: bar aggregation over OHLCV tuples.
Free-threading's only plausible win for this architecture. Raw numbers, no verdicts."""
import argparse, os, sys, sysconfig, time, threading
import multiprocessing as mp

def make_stream(n, seed):
    # deterministic, no RNG import cost inside the timed loop
    s = seed
    out = []
    for i in range(n):
        s = (s * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
        px = 20000.0 + ((s >> 33) % 100000) / 100.0
        out.append((i, px, px * 1.001, px * 0.999, px, (s >> 20) % 1000))
    return out

def aggregate(stream, window):
    """The glue: dict building, list append, comparisons - no numpy, no C fast path."""
    bars, hi, lo, vol, n = [], None, None, 0.0, 0
    close = 0.0
    for t, o, h, l, c, v in stream:
        hi = h if hi is None or h > hi else hi
        lo = l if lo is None or l < lo else lo
        vol += v; close = c; n += 1
        if n == window:
            bars.append({"t": t, "open": o, "high": hi, "low": lo,
                         "close": close, "volume": vol, "count": n})
            hi = lo = None; vol = 0.0; n = 0
    return len(bars)

def worker(stream, window, reps):
    total = 0
    for _ in range(reps):
        total += aggregate(stream, window)
    return total

def proc_worker(items, window, reps, seed, q):
    q.put(worker(make_stream(items, seed), window, reps))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", type=int, default=200_000, help="OHLCV tuples per worker")
    ap.add_argument("--window", type=int, default=60, help="tuples per aggregated bar")
    ap.add_argument("--reps", type=int, default=3, help="passes over the stream per worker")
    ap.add_argument("--workers", type=int, default=6, help="K: matches 6 physical cores")
    a = ap.parse_args()

    ft = sysconfig.get_config_var("Py_GIL_DISABLED") == 1
    print(f"build={sys.version.split()[0]} free_threaded={bool(ft)} gil_enabled={sys._is_gil_enabled() if ft else True} "
          f"items={a.items} window={a.window} reps={a.reps} workers={a.workers}")

    streams = [make_stream(a.items, 1000 + i) for i in range(a.workers)]
    unit = a.items * a.reps

    t0 = time.perf_counter(); worker(streams[0], a.window, a.reps); t1 = time.perf_counter()
    print(f"  1 x serial     : {t1-t0:7.3f}s  {unit/(t1-t0)/1e6:6.3f} M tuples/s")

    ths = [threading.Thread(target=worker, args=(streams[i], a.window, a.reps)) for i in range(a.workers)]
    t0 = time.perf_counter()
    for t in ths: t.start()
    for t in ths: t.join()
    t1 = time.perf_counter()
    print(f"  {a.workers} x threads   : {t1-t0:7.3f}s  {unit*a.workers/(t1-t0)/1e6:6.3f} M tuples/s  "
          f"speedup {(unit/( (t1-t0)/a.workers ))/ (unit/(t1-t0)) if False else ''}")

    ctx = mp.get_context("forkserver")
    q = ctx.Queue()
    ps = [ctx.Process(target=proc_worker, args=(a.items, a.window, a.reps, 1000+i, q)) for i in range(a.workers)]
    t0 = time.perf_counter()
    for p in ps: p.start()
    for _ in ps: q.get()
    for p in ps: p.join()
    t1 = time.perf_counter()
    print(f"  {a.workers} x processes : {t1-t0:7.3f}s  {unit*a.workers/(t1-t0)/1e6:6.3f} M tuples/s  "
          f"(includes fork + stream build per process)")

if __name__ == "__main__":
    for v in ("OPENBLAS_NUM_THREADS","OMP_NUM_THREADS","MKL_NUM_THREADS",
              "VECLIB_MAXIMUM_THREADS","NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(v, "1")
    main()
