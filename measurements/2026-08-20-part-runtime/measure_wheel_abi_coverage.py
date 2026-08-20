import json, sys, urllib.request

PKGS = ["numpy","scipy","pandas","polars","pyarrow","duckdb","websockets","aiohttp",
        "orjson","msgpack","ujson","ccxt","cryptography","uvloop","zstandard","lmdb",
        "apsw","numba","threadpoolctl","watchdog","httpx","pydantic","psutil",
        "python-rapidjson","cffi","pyzmq","blosc2","numexpr","bottleneck","ta-lib",
        "scikit-learn","lightgbm","torch","tomli-w","cbor2"]

def tags(pkg):
    url = f"https://pypi.org/pypi/{pkg}/json"
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            d = json.load(r)
    except Exception as e:
        return None, None, f"{type(e).__name__}", None
    ver = d["info"]["version"]
    files = [f["filename"] for f in d["urls"]]
    linux = [f for f in files if f.endswith(".whl") and "manylinux" in f and "x86_64" in f]
    has314  = any("cp314-" in f for f in linux)
    has314t = any("cp314t-" in f for f in linux)
    pure    = any(f.endswith("-py3-none-any.whl") for f in files if f.endswith(".whl"))
    sdist_only = bool(files) and not any(f.endswith(".whl") for f in files)
    return ver, (has314, has314t, pure, sdist_only), None, len(linux)

print(f"{'package':22} {'version':12} {'cp314':6} {'cp314t':7} {'pure-py':8} note")
rows=[]
for p in PKGS:
    ver, t, err, n = tags(p)
    if err:
        print(f"{p:22} {'-':12} {'?':6} {'?':7} {'?':8} FETCH-FAIL {err}")
        rows.append((p,None)); continue
    has314, has314t, pure, sdist_only = t
    note = "pure python (ABI-independent)" if pure and not has314 else ("SDIST ONLY - needs compiler" if sdist_only else "")
    print(f"{p:22} {ver:12} {'YES' if has314 else 'no':6} {'YES' if has314t else 'NO':7} {'yes' if pure else 'no':8} {note}")
    rows.append((p,(has314,has314t,pure,sdist_only)))

ok=[r for r in rows if r[1]]
need_abi=[r for r in ok if not r[1][2]]
print()
print(f"packages checked ok: {len(ok)}/{len(PKGS)}")
print(f"of those, need a compiled ABI wheel (not pure python): {len(need_abi)}")
print(f"  have cp314  (standard):      {sum(1 for r in need_abi if r[1][0])}/{len(need_abi)}")
print(f"  have cp314t (free-threaded): {sum(1 for r in need_abi if r[1][1])}/{len(need_abi)}")
print("  cp314 but NOT cp314t (uninstallable on free-threaded, no compiler here):")
for p,t in need_abi:
    if t[0] and not t[1]:
        print("    -", p)
