"""Switch-on reliability: fork a part via forkserver, migrate it into its own
transient scope, N times per settle delay. Reports success rate and latency.
No verdicts - raw numbers only."""
import argparse, os, signal, statistics, subprocess, sys, time
import multiprocessing as mp

def part_body(sock_hint):                      # module-level: forkserver pickles by name
    time.sleep(30)

def cgroup_of(pid):
    try: return open(f"/proc/{pid}/cgroup").read().strip().split("::")[1]
    except OSError: return None

def place(pid, name, mem_bytes, weight):
    t0 = time.perf_counter()
    r = subprocess.run(["busctl","--user","call","org.freedesktop.systemd1",
        "/org/freedesktop/systemd1","org.freedesktop.systemd1.Manager","StartTransientUnit",
        "ssa(sv)a(sa(sv))", name, "fail", "3",
        "PIDs","au","1",str(pid),
        "MemoryMax","t",str(mem_bytes),
        "CPUWeight","t",str(weight),"0"], capture_output=True, text=True)
    return (time.perf_counter()-t0)*1000, r.returncode, r.stderr.strip()[:100]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15, help="migrations per settle delay")
    ap.add_argument("--settles-ms", default="0,1,5,25,100", help="settle delays to test")
    ap.add_argument("--confirm-ms", type=int, default=400, help="wait before reading final cgroup")
    ap.add_argument("--mem-mb", type=int, default=256)
    ap.add_argument("--weight", type=int, default=100)
    a = ap.parse_args()

    mp.set_start_method("forkserver")
    mp.set_forkserver_preload(["numpy"])
    for delay_s in [float(x)/1000 for x in a.settles_ms.split(",")]:
        forks, places, ok, fails = [], [], 0, []
        for i in range(a.trials):
            t0 = time.perf_counter()
            p = mp.Process(target=part_body, args=(None,)); p.start()
            forks.append((time.perf_counter()-t0)*1000)
            if delay_s: time.sleep(delay_s)
            name = f"ratetest-{os.getpid()}-{int(delay_s*1000)}-{i}.scope"
            ms, rc, err = place(p.pid, name, a.mem_mb<<20, a.weight)
            places.append(ms)
            time.sleep(a.confirm_ms/1000)
            cg = cgroup_of(p.pid) or ""
            moved = name.rstrip(".scope").split("/")[-1] in cg
            if moved: ok += 1
            else: fails.append({"trial": i, "rc": rc, "err": err, "cgroup": cg})
            os.kill(p.pid, signal.SIGKILL); p.join()
        q = lambda v,k: round(statistics.quantiles(v, n=100)[k-1],2) if len(v)>2 else round(max(v),2)
        print(f"settle={int(delay_s*1000):4d}ms  moved={ok}/{a.trials}  "
              f"fork_med={statistics.median(forks):.2f}ms  "
              f"place_med={statistics.median(places):.2f}ms place_p95={q(places,95)}ms")
        for f in fails[:3]: print("     FAIL", f)

if __name__ == "__main__":
    for v in ("OPENBLAS_NUM_THREADS","OMP_NUM_THREADS","MKL_NUM_THREADS",
              "VECLIB_MAXIMUM_THREADS","NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(v, "1")
    main()
