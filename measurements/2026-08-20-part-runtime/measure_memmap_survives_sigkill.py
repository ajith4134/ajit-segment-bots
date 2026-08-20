"""Does a part's numeric state survive SIGKILL with no flush? The crash-only crux."""
import argparse, os, signal, subprocess, sys, tempfile, time
import numpy as np

CHILD = r'''
import sys, numpy as np, time, os
path, n, seed, do_flush = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]=="1"
a = np.memmap(path, dtype="float64", mode="r+", shape=(n,))
a[:] = np.arange(n, dtype="float64") * 3.0 + seed
if do_flush: a.flush()
sys.stdout.write("written\n"); sys.stdout.flush()
time.sleep(60)
'''

def run(path, n, seed, do_flush, kill_mode):
    a = np.memmap(path, dtype="float64", mode="w+", shape=(n,)); a[:] = -1.0; a.flush(); del a
    p = subprocess.Popen([sys.executable, "-c", CHILD, path, str(n), str(seed), "1" if do_flush else "0"],
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "written"
    os.kill(p.pid, signal.SIGKILL if kill_mode == "kill" else signal.SIGTERM)
    p.wait()
    b = np.memmap(path, dtype="float64", mode="r", shape=(n,))
    expect = np.arange(n, dtype="float64") * 3.0 + seed
    match = int((b == expect).sum()); del b
    return match

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--elements", type=int, default=2_000_000, help="float64 elements (16 MB at 2e6)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--trials", type=int, default=3)
    a = ap.parse_args()
    d = tempfile.mkdtemp(prefix="memmapkill-")
    print(f"elements={a.elements} bytes={a.elements*8} trials={a.trials}")
    for do_flush in (False, True):
        for t in range(a.trials):
            path = os.path.join(d, f"state-{int(do_flush)}-{t}.f64")
            m = run(path, a.elements, a.seed + t, do_flush, "kill")
            print(f"  flush={do_flush!s:5} SIGKILL trial {t}: {m}/{a.elements} elements survived "
                  f"({100.0*m/a.elements:.2f}%)")
            os.unlink(path)
    os.rmdir(d)

if __name__ == "__main__":
    main()
