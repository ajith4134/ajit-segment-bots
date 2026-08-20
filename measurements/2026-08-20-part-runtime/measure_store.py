#!/usr/bin/env python3
"""Measurement harness: SQLite vs LMDB for structured part state, on THIS box.

Models the real write shapes described in the part-runtime design:

  JOURNAL       many separate OS processes each appending one small structured
                record (part id, timestamp_ns, data type, payload blob,
                provenance string) -- switch-records, journal entries,
                provenance stamps.
  SETTINGS      a part reads a handful of key/value settings once at startup,
                then exits. This sits on the switch-on critical path.
  QUERY         "all records this part produced between t0 and t1", and
                "current value of setting X and when it last changed".

Every number below either came out of a clock or is a named --flag with a
documented default; there are no bare tuning constants in the measurement
logic itself.

Run (STD venv only -- lmdb has no cp314t wheel and cannot install on the
free-threaded interpreter):

    SCRATCH/vstd/bin/python SCRATCH/m/measure_store.py --json

Every measurement section is wrapped so one section's failure does not stop
the others; a failed section records {"error": "<traceback>"} in its place.
If `lmdb` cannot be imported at all, that fact is recorded once at the top
level and every LMDB-side measurement records its own "lmdb not importable"
error instead of raising.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import secrets
import shutil
import signal
import sqlite3
import statistics
import struct
import sys
import tempfile
import time
import traceback
import zlib
from pathlib import Path

try:
    import lmdb  # type: ignore

    LMDB_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - exercised only when lmdb is absent
    lmdb = None  # type: ignore
    LMDB_IMPORT_ERROR = repr(exc)


DATA_TYPE = "switch-record"  # constant label for the synthetic journal records


# ---------------------------------------------------------------------------
# small shared helpers -- used by both the parent process and every child
# ---------------------------------------------------------------------------

def _percentiles(values_ms):
    """Raw distribution summary. No interpretation, just sorted-order stats."""
    if not values_ms:
        return {"count": 0}
    s = sorted(values_ms)
    n = len(s)

    def pct(p):
        if n == 1:
            return s[0]
        idx = min(n - 1, max(0, round(p / 100 * (n - 1))))
        return s[idx]

    return {
        "count": n,
        "min_ms": s[0],
        "p50_ms": pct(50),
        "p95_ms": pct(95),
        "p99_ms": pct(99),
        "max_ms": s[-1],
        "mean_ms": statistics.fmean(s),
    }


def _crc32(payload: bytes) -> int:
    return zlib.crc32(payload) & 0xFFFFFFFF


def _make_payload(n_bytes: int) -> bytes:
    return os.urandom(n_bytes)


def _make_provenance(n_bytes: int) -> str:
    # token_hex(k) returns 2*k hex chars; ask for enough and trim to exact length.
    raw = secrets.token_hex((n_bytes + 1) // 2)
    return raw[:n_bytes]


def _write_json(path, obj) -> None:
    Path(path).write_text(json.dumps(obj))


def _read_json(path):
    return json.loads(Path(path).read_text())


def _dir_size_bytes(*paths) -> int:
    total = 0
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            total += p.stat().st_size
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
    return total


def _safe_call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception:
        return {"error": traceback.format_exc()}


# ---------------------------------------------------------------------------
# SQLite: schema, connection helper, journal/settings codecs
# ---------------------------------------------------------------------------

def _sqlite_connect(db_path, synchronous: str, busy_timeout_ms: int) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=busy_timeout_ms / 1000.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA synchronous={synchronous}")
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    return conn


def _sqlite_init_journal_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS journal (
            part_id TEXT NOT NULL,
            timestamp_ns INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            data_type TEXT NOT NULL,
            payload BLOB NOT NULL,
            provenance TEXT NOT NULL,
            checksum INTEGER NOT NULL,
            PRIMARY KEY (part_id, timestamp_ns, seq)
        )
        """
    )
    conn.commit()


def _sqlite_init_settings_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_ns INTEGER NOT NULL
        )
        """
    )
    conn.commit()


# ---------------------------------------------------------------------------
# LMDB: env helper, journal/settings key/value codecs
# ---------------------------------------------------------------------------

def _lmdb_open(db_dir, map_size_mb: int, writemap: bool, create: bool, readonly: bool = False):
    return lmdb.open(
        str(db_dir),
        map_size=map_size_mb * 1024 * 1024,
        max_dbs=1,
        subdir=True,
        writemap=writemap,
        create=create,
        readonly=readonly,
        lock=not readonly,
        sync=True,  # never MDB_NOSYNC -- durability parity with SQLite synchronous=FULL/NORMAL
    )


def _lmdb_journal_key(part_id: str, timestamp_ns: int, seq: int, part_id_width: int) -> bytes:
    pid = part_id.encode("utf-8")[:part_id_width].ljust(part_id_width, b"\x00")
    return pid + struct.pack(">Q", timestamp_ns) + struct.pack(">I", seq)


def _lmdb_journal_key_seq(key: bytes, part_id_width: int) -> int:
    return struct.unpack_from(">I", key, part_id_width + 8)[0]


def _lmdb_journal_value(data_type: str, provenance: str, checksum: int, payload: bytes) -> bytes:
    dt = data_type.encode("utf-8")
    pr = provenance.encode("utf-8")
    return struct.pack(">HHI", len(dt), len(pr), checksum) + dt + pr + payload


def _lmdb_journal_decode(value: bytes):
    dt_len, pr_len, checksum = struct.unpack_from(">HHI", value, 0)
    off = 8
    dt = value[off:off + dt_len].decode("utf-8")
    off += dt_len
    pr = value[off:off + pr_len].decode("utf-8")
    off += pr_len
    payload = value[off:]
    return dt, pr, checksum, payload


def _lmdb_settings_key(key: str) -> bytes:
    return key.encode("utf-8")


def _lmdb_settings_value(updated_ns: int, value_str: str) -> bytes:
    return struct.pack(">Q", updated_ns) + value_str.encode("utf-8")


def _lmdb_settings_decode(value: bytes):
    updated_ns = struct.unpack_from(">Q", value, 0)[0]
    return updated_ns, value[8:].decode("utf-8")


# ---------------------------------------------------------------------------
# (a) + (b) concurrent append throughput and writer-lock contention
#
# Every child times its own append as: t0 -> [acquire write lock] -> t_lock
# -> [insert + commit] -> t1. lock_wait_ms = t_lock - t0 is directly "how
# long this writer blocked acquiring the single write lock" for LMDB, and
# "how long BEGIN IMMEDIATE took" for SQLite (which, with busy_timeout set,
# blocks the same way rather than raising). total_ms = t1 - t0 is "the
# latency of a single append" as asked for in (a).
# ---------------------------------------------------------------------------

def sqlite_writer_proc(db_path, synchronous, busy_timeout_ms, sqlite_max_retries,
                        part_id, num_records, payload_bytes, provenance_bytes,
                        barrier, result_path):
    conn = _sqlite_connect(db_path, synchronous, busy_timeout_ms)
    lock_wait_ms = []
    total_ms = []
    busy_errors = 0
    retries_total = 0

    barrier.wait()
    t_writer_start = time.perf_counter()
    for i in range(num_records):
        payload = _make_payload(payload_bytes)
        provenance = _make_provenance(provenance_bytes)
        checksum = _crc32(payload)
        ts = time.time_ns()
        attempt = 0
        while True:
            t0 = time.perf_counter()
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                busy_errors += 1
                attempt += 1
                retries_total += 1
                if attempt > sqlite_max_retries:
                    raise
                continue
            t_lock = time.perf_counter()
            conn.execute(
                "INSERT INTO journal(part_id,timestamp_ns,seq,data_type,payload,provenance,checksum)"
                " VALUES (?,?,?,?,?,?,?)",
                (part_id, ts, i, DATA_TYPE, payload, provenance, checksum),
            )
            conn.commit()
            t1 = time.perf_counter()
            lock_wait_ms.append((t_lock - t0) * 1000)
            total_ms.append((t1 - t0) * 1000)
            break
    writer_wall_ms = (time.perf_counter() - t_writer_start) * 1000
    conn.close()
    _write_json(result_path, {
        "part_id": part_id,
        "records": num_records,
        "writer_wall_ms": writer_wall_ms,
        "lock_wait_ms": lock_wait_ms,
        "total_ms": total_ms,
        "busy_errors": busy_errors,
        "retries_total": retries_total,
    })


def lmdb_writer_proc(db_dir, writemap, map_size_mb, part_id_width, part_id,
                      num_records, payload_bytes, provenance_bytes,
                      barrier, result_path):
    env = _lmdb_open(db_dir, map_size_mb, writemap, create=False)
    lock_wait_ms = []
    total_ms = []

    barrier.wait()
    t_writer_start = time.perf_counter()
    for i in range(num_records):
        payload = _make_payload(payload_bytes)
        provenance = _make_provenance(provenance_bytes)
        checksum = _crc32(payload)
        ts = time.time_ns()
        key = _lmdb_journal_key(part_id, ts, i, part_id_width)
        value = _lmdb_journal_value(DATA_TYPE, provenance, checksum, payload)

        t0 = time.perf_counter()
        txn = env.begin(write=True)  # blocks here until the single writer lock is free
        t_lock = time.perf_counter()
        try:
            txn.put(key, value)
            txn.commit()
        except Exception:
            txn.abort()
            raise
        t1 = time.perf_counter()
        lock_wait_ms.append((t_lock - t0) * 1000)
        total_ms.append((t1 - t0) * 1000)
    writer_wall_ms = (time.perf_counter() - t_writer_start) * 1000
    env.close()
    _write_json(result_path, {
        "part_id": part_id,
        "records": num_records,
        "writer_wall_ms": writer_wall_ms,
        "lock_wait_ms": lock_wait_ms,
        "total_ms": total_ms,
    })


def run_sqlite_append_variant(args, label, synchronous):
    db_path = Path(args.dir) / f"append_sqlite_{label}.db"
    conn = _sqlite_connect(db_path, synchronous, args.busy_timeout_ms)
    _sqlite_init_journal_schema(conn)
    conn.close()

    ctx = multiprocessing.get_context(args.mp_start_method)
    barrier = ctx.Barrier(args.writers + 1)
    results_dir = Path(args.dir) / "results"
    procs, result_paths = [], []
    for w in range(args.writers):
        rp = results_dir / f"sqlite_{label}_{w}.json"
        result_paths.append(rp)
        p = ctx.Process(
            target=sqlite_writer_proc,
            args=(str(db_path), synchronous, args.busy_timeout_ms, args.sqlite_max_retries,
                  f"writer-{w}", args.records, args.payload_bytes, args.provenance_bytes,
                  barrier, str(rp)),
        )
        p.start()
        procs.append(p)

    barrier.wait()
    t_release = time.perf_counter()
    for p in procs:
        p.join()
    aggregate_wall_s = time.perf_counter() - t_release

    for p in procs:
        if p.exitcode != 0:
            raise RuntimeError(f"sqlite writer process exited with code {p.exitcode}")

    results = [_read_json(rp) for rp in result_paths]
    all_total_ms = [x for r in results for x in r["total_ms"]]
    all_lock_ms = [x for r in results for x in r["lock_wait_ms"]]
    total_records = sum(r["records"] for r in results)
    per_writer = [{
        "part_id": r["part_id"],
        "records": r["records"],
        "records_per_sec": (r["records"] / (r["writer_wall_ms"] / 1000)) if r["writer_wall_ms"] > 0 else None,
        "writer_wall_ms": r["writer_wall_ms"],
        "busy_errors": r["busy_errors"],
        "retries_total": r["retries_total"],
    } for r in results]

    return {
        "synchronous": synchronous,
        "writers": args.writers,
        "records_per_writer": args.records,
        "aggregate_records_per_sec": (total_records / aggregate_wall_s) if aggregate_wall_s > 0 else None,
        "aggregate_wall_s": aggregate_wall_s,
        "per_writer": per_writer,
        "append_latency_total_ms": _percentiles(all_total_ms),
        "append_latency_lock_wait_ms": _percentiles(all_lock_ms),
        "busy_errors_total": sum(r["busy_errors"] for r in results),
        "busy_retries_total": sum(r["retries_total"] for r in results),
        "longest_stall_ms": max(all_total_ms) if all_total_ms else None,
        "longest_lock_wait_ms": max(all_lock_ms) if all_lock_ms else None,
    }


def run_lmdb_append_variant(args, label, writemap):
    db_dir = Path(args.dir) / f"append_lmdb_{label}"
    env = _lmdb_open(db_dir, args.lmdb_map_size_mb, writemap, create=True)
    env.close()

    ctx = multiprocessing.get_context(args.mp_start_method)
    barrier = ctx.Barrier(args.writers + 1)
    results_dir = Path(args.dir) / "results"
    procs, result_paths = [], []
    for w in range(args.writers):
        rp = results_dir / f"lmdb_{label}_{w}.json"
        result_paths.append(rp)
        part_id = f"writer-{w}"
        p = ctx.Process(
            target=lmdb_writer_proc,
            args=(str(db_dir), writemap, args.lmdb_map_size_mb, args.part_id_width, part_id,
                  args.records, args.payload_bytes, args.provenance_bytes, barrier, str(rp)),
        )
        p.start()
        procs.append(p)

    barrier.wait()
    t_release = time.perf_counter()
    for p in procs:
        p.join()
    aggregate_wall_s = time.perf_counter() - t_release

    for p in procs:
        if p.exitcode != 0:
            raise RuntimeError(f"lmdb writer process exited with code {p.exitcode}")

    results = [_read_json(rp) for rp in result_paths]
    all_total_ms = [x for r in results for x in r["total_ms"]]
    all_lock_ms = [x for r in results for x in r["lock_wait_ms"]]
    total_records = sum(r["records"] for r in results)
    per_writer = [{
        "part_id": r["part_id"],
        "records": r["records"],
        "records_per_sec": (r["records"] / (r["writer_wall_ms"] / 1000)) if r["writer_wall_ms"] > 0 else None,
        "writer_wall_ms": r["writer_wall_ms"],
    } for r in results]

    return {
        "writemap": writemap,
        "writers": args.writers,
        "records_per_writer": args.records,
        "aggregate_records_per_sec": (total_records / aggregate_wall_s) if aggregate_wall_s > 0 else None,
        "aggregate_wall_s": aggregate_wall_s,
        "per_writer": per_writer,
        "append_latency_total_ms": _percentiles(all_total_ms),
        "append_latency_lock_wait_ms": _percentiles(all_lock_ms),
        "longest_stall_ms": max(all_total_ms) if all_total_ms else None,
        "longest_lock_wait_ms": max(all_lock_ms) if all_lock_ms else None,
    }


def measure_append_throughput(args):
    out = {"sqlite": {}, "lmdb": {}}
    out["sqlite"]["synchronous_full"] = _safe_call(run_sqlite_append_variant, args, "full", "FULL")
    out["sqlite"]["synchronous_normal"] = _safe_call(run_sqlite_append_variant, args, "normal", "NORMAL")
    if lmdb is not None:
        out["lmdb"]["default"] = _safe_call(run_lmdb_append_variant, args, "default", False)
        out["lmdb"]["writemap"] = _safe_call(run_lmdb_append_variant, args, "writemap", True)
    else:
        out["lmdb"]["error"] = f"lmdb not importable: {LMDB_IMPORT_ERROR}"
    return out


# ---------------------------------------------------------------------------
# (c) cold settings read -- the switch-on critical-path measurement
#
# Each repetition spawns a genuinely NEW process (per --mp-start-method,
# default 'spawn' so the child re-imports everything rather than inheriting
# state via fork's copy-on-write). The child times its own open+read+close
# ("in_process_read_ms"); the parent times the whole Process().start()..join()
# span ("process_wall_ms"), which additionally includes this interpreter's
# own bootstrap/import cost for that start method -- NOT the same thing as
# this project's real forkserver switch-on path (2.78ms), which preloads
# modules and is measured elsewhere. That distinction is called out in the
# output so it is not misread as a switch-on number.
# ---------------------------------------------------------------------------

def sqlite_settings_populate(db_path, num_keys, busy_timeout_ms):
    conn = _sqlite_connect(db_path, "FULL", busy_timeout_ms)
    _sqlite_init_settings_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    for i in range(num_keys):
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value,updated_ns) VALUES (?,?,?)",
            (f"setting-{i}", secrets.token_hex(8), time.time_ns()),
        )
    conn.commit()
    conn.close()


def sqlite_settings_cold_read_proc(db_path, busy_timeout_ms, num_keys, result_path):
    t0 = time.perf_counter()
    conn = sqlite3.connect(str(db_path), timeout=busy_timeout_ms / 1000.0)
    conn.execute("PRAGMA journal_mode=WAL")
    read_count = 0
    for i in range(num_keys):
        row = conn.execute(
            "SELECT value, updated_ns FROM settings WHERE key=?", (f"setting-{i}",)
        ).fetchone()
        if row is not None:
            read_count += 1
    conn.close()
    in_process_ms = (time.perf_counter() - t0) * 1000
    _write_json(result_path, {"in_process_read_ms": in_process_ms, "keys_read": read_count})


def lmdb_settings_populate(db_dir, num_keys, map_size_mb):
    env = _lmdb_open(db_dir, map_size_mb, writemap=False, create=True)
    with env.begin(write=True) as txn:
        for i in range(num_keys):
            key = _lmdb_settings_key(f"setting-{i}")
            txn.put(key, _lmdb_settings_value(time.time_ns(), secrets.token_hex(8)))
    env.close()


def lmdb_settings_cold_read_proc(db_dir, map_size_mb, num_keys, result_path):
    t0 = time.perf_counter()
    env = _lmdb_open(db_dir, map_size_mb, writemap=False, create=False, readonly=True)
    read_count = 0
    with env.begin() as txn:
        for i in range(num_keys):
            v = txn.get(_lmdb_settings_key(f"setting-{i}"))
            if v is not None:
                read_count += 1
    env.close()
    in_process_ms = (time.perf_counter() - t0) * 1000
    _write_json(result_path, {"in_process_read_ms": in_process_ms, "keys_read": read_count})


def _run_cold_read_reps(ctx, target, fixed_args, reps, results_dir, label):
    process_wall_ms, in_process_ms, errors = [], [], []
    for rep in range(reps):
        rp = results_dir / f"{label}_{rep}.json"
        t0 = time.perf_counter()
        p = ctx.Process(target=target, args=(*fixed_args, str(rp)))
        p.start()
        p.join()
        wall_ms = (time.perf_counter() - t0) * 1000
        if p.exitcode != 0:
            errors.append({"rep": rep, "exitcode": p.exitcode})
            continue
        inner = _read_json(rp)
        process_wall_ms.append(wall_ms)
        in_process_ms.append(inner["in_process_read_ms"])
    return process_wall_ms, in_process_ms, errors


def measure_cold_settings_read_sqlite(args, db_path):
    ctx = multiprocessing.get_context(args.mp_start_method)
    results_dir = Path(args.dir) / "results"
    process_wall_ms, in_process_ms, errors = _run_cold_read_reps(
        ctx, sqlite_settings_cold_read_proc,
        (str(db_path), args.busy_timeout_ms, args.settings_keys),
        args.settings_reps, results_dir, "sqlite_cold",
    )
    return {
        "settings_keys": args.settings_keys,
        "reps": args.settings_reps,
        "mp_start_method": args.mp_start_method,
        "process_wall_ms": _percentiles(process_wall_ms),
        "in_process_read_ms": _percentiles(in_process_ms),
        "errors": errors,
        "note": ("process_wall_ms includes this interpreter's own bootstrap cost for "
                 "--mp-start-method; it is NOT the project's real forkserver switch-on "
                 "path (measured separately at 2.78ms fork + 5.6ms cgroup placement). "
                 "in_process_read_ms isolates store-open + K-key-read + close."),
    }


def measure_cold_settings_read_lmdb(args, db_dir):
    ctx = multiprocessing.get_context(args.mp_start_method)
    results_dir = Path(args.dir) / "results"
    process_wall_ms, in_process_ms, errors = _run_cold_read_reps(
        ctx, lmdb_settings_cold_read_proc,
        (str(db_dir), args.lmdb_map_size_mb, args.settings_keys),
        args.settings_reps, results_dir, "lmdb_cold",
    )
    return {
        "settings_keys": args.settings_keys,
        "reps": args.settings_reps,
        "mp_start_method": args.mp_start_method,
        "process_wall_ms": _percentiles(process_wall_ms),
        "in_process_read_ms": _percentiles(in_process_ms),
        "errors": errors,
        "note": ("process_wall_ms includes this interpreter's own bootstrap cost for "
                 "--mp-start-method; it is NOT the project's real forkserver switch-on "
                 "path (measured separately at 2.78ms fork + 5.6ms cgroup placement). "
                 "in_process_read_ms isolates store-open + K-key-read + close."),
    }


def measure_cold_settings_read_all(args):
    out = {}
    sqlite_path = Path(args.dir) / "settings_sqlite.db"
    sqlite_settings_populate(str(sqlite_path), args.settings_keys, args.busy_timeout_ms)
    out["sqlite"] = _safe_call(measure_cold_settings_read_sqlite, args, sqlite_path)

    if lmdb is not None:
        lmdb_dir = Path(args.dir) / "settings_lmdb"
        lmdb_settings_populate(str(lmdb_dir), args.settings_keys, args.lmdb_map_size_mb)
        out["lmdb"] = _safe_call(measure_cold_settings_read_lmdb, args, lmdb_dir)
    else:
        out["lmdb"] = {"error": f"lmdb not importable: {LMDB_IMPORT_ERROR}"}
    return out


# ---------------------------------------------------------------------------
# (d) crash recovery -- the single most important measurement.
#
# A writer commits `crash_baseline_records` durably, one transaction, then
# opens a SECOND, separate transaction, writes `crash_hold_records` more
# records into it WITHOUT committing, signals the parent via an Event, and
# sleeps (a bounded safety net only -- the parent kills as soon as it sees
# the event, well before the sleep would elapse). The parent SIGKILLs it
# mid-transaction, then opens the store fresh in a brand-new process and
# checks: did it open, how long did that open take, are all baseline records
# present and byte-for-byte intact (checksum + length against what was
# recorded before the kill), and are ANY of the never-committed records
# visible (which would mean a torn/non-atomic write).
# ---------------------------------------------------------------------------

def sqlite_crash_writer_proc(db_path, synchronous, busy_timeout_ms, part_id,
                              baseline_records, hold_records, payload_bytes,
                              provenance_bytes, hold_sleep_s, event, checksum_path):
    conn = _sqlite_connect(db_path, synchronous, busy_timeout_ms)
    _sqlite_init_journal_schema(conn)

    baseline = {}
    conn.execute("BEGIN IMMEDIATE")
    for i in range(baseline_records):
        payload = _make_payload(payload_bytes)
        provenance = _make_provenance(provenance_bytes)
        checksum = _crc32(payload)
        ts = time.time_ns()
        conn.execute(
            "INSERT INTO journal(part_id,timestamp_ns,seq,data_type,payload,provenance,checksum)"
            " VALUES (?,?,?,?,?,?,?)",
            (part_id, ts, i, DATA_TYPE, payload, provenance, checksum),
        )
        baseline[i] = {"checksum": checksum, "payload_len": len(payload)}
    conn.commit()

    # A second, separate transaction, deliberately never committed.
    conn.execute("BEGIN IMMEDIATE")
    for i in range(hold_records):
        seq = baseline_records + i
        payload = _make_payload(payload_bytes)
        provenance = _make_provenance(provenance_bytes)
        checksum = _crc32(payload)
        ts = time.time_ns()
        conn.execute(
            "INSERT INTO journal(part_id,timestamp_ns,seq,data_type,payload,provenance,checksum)"
            " VALUES (?,?,?,?,?,?,?)",
            (part_id, ts, seq, DATA_TYPE, payload, provenance, checksum),
        )

    _write_json(checksum_path, {
        "part_id": part_id, "baseline_records": baseline_records,
        "hold_records": hold_records, "baseline": baseline,
    })
    event.set()
    time.sleep(hold_sleep_s)  # safety net only; parent kills long before this elapses
    conn.close()  # reached only if the harness's kill signal somehow failed


def sqlite_crash_reopen_proc(db_path, busy_timeout_ms, part_id, baseline_records,
                              hold_records, baseline_meta, result_path):
    result = {"opened": False, "open_error": None}
    try:
        t0 = time.perf_counter()
        conn = sqlite3.connect(str(db_path), timeout=busy_timeout_ms / 1000.0)
        conn.execute("PRAGMA journal_mode=WAL")
        total_present = conn.execute(
            "SELECT COUNT(*) FROM journal WHERE part_id=?", (part_id,)
        ).fetchone()[0]
        result["open_recover_ms"] = (time.perf_counter() - t0) * 1000
        result["opened"] = True
    except Exception as exc:
        result["open_error"] = repr(exc)
        _write_json(result_path, result)
        return

    baseline_present = 0
    baseline_intact = True
    torn_seqs = []
    for seq_str, meta in baseline_meta.items():
        seq = int(seq_str)
        row = conn.execute(
            "SELECT payload, checksum FROM journal WHERE part_id=? AND seq=?", (part_id, seq)
        ).fetchone()
        if row is None:
            continue
        baseline_present += 1
        payload, checksum = row
        ok = (len(payload) == meta["payload_len"] and checksum == meta["checksum"]
              and _crc32(payload) == meta["checksum"])
        if not ok:
            baseline_intact = False
            torn_seqs.append(seq)

    uncommitted_present = 0
    for i in range(hold_records):
        seq = baseline_records + i
        row = conn.execute(
            "SELECT 1 FROM journal WHERE part_id=? AND seq=?", (part_id, seq)
        ).fetchone()
        if row is not None:
            uncommitted_present += 1
    conn.close()

    result.update({
        "total_present_for_part": total_present,
        "baseline_target": baseline_records,
        "baseline_present": baseline_present,
        "baseline_all_intact": baseline_intact,
        "torn_seqs": torn_seqs,
        "uncommitted_attempted": hold_records,
        "uncommitted_present": uncommitted_present,
    })
    _write_json(result_path, result)


def measure_sqlite_crash_recovery(args):
    db_path = Path(args.dir) / "crash_sqlite.db"
    ctx = multiprocessing.get_context(args.mp_start_method)
    results_dir = Path(args.dir) / "results"
    event = ctx.Event()
    checksum_path = results_dir / "crash_sqlite_baseline.json"
    part_id = "crash-writer"

    p = ctx.Process(
        target=sqlite_crash_writer_proc,
        args=(str(db_path), args.crash_synchronous, args.busy_timeout_ms, part_id,
              args.crash_baseline_records, args.crash_hold_records, args.payload_bytes,
              args.provenance_bytes, args.crash_hold_ms / 1000.0, event, str(checksum_path)),
    )
    p.start()
    got_event = event.wait(timeout=args.crash_event_timeout_s)
    if not got_event:
        p.kill()
        p.join()
        return {"error": "writer never signalled hold-in-progress within --crash-event-timeout-s"}

    t_kill = time.perf_counter()
    os.kill(p.pid, signal.SIGKILL)
    p.join(timeout=args.crash_event_timeout_s)
    kill_to_join_ms = (time.perf_counter() - t_kill) * 1000
    baseline_meta = _read_json(checksum_path)

    reopen_result_path = results_dir / "crash_sqlite_reopen.json"
    p2 = ctx.Process(
        target=sqlite_crash_reopen_proc,
        args=(str(db_path), args.busy_timeout_ms, part_id, args.crash_baseline_records,
              args.crash_hold_records, baseline_meta["baseline"], str(reopen_result_path)),
    )
    t0 = time.perf_counter()
    p2.start()
    p2.join()
    reopen_process_wall_ms = (time.perf_counter() - t0) * 1000

    if reopen_result_path.exists():
        result = _read_json(reopen_result_path)
    else:
        result = {"opened": False, "open_error": f"reopen process exitcode={p2.exitcode}, no result file"}
    result["reopen_process_wall_ms"] = reopen_process_wall_ms
    result["kill_to_join_ms"] = kill_to_join_ms
    result["writer_exitcode_after_sigkill"] = p.exitcode
    result["synchronous"] = args.crash_synchronous
    return result


def lmdb_crash_writer_proc(db_dir, map_size_mb, part_id_width, part_id,
                            baseline_records, hold_records, payload_bytes,
                            provenance_bytes, hold_sleep_s, event, checksum_path):
    env = _lmdb_open(db_dir, map_size_mb, writemap=False, create=False)

    baseline = {}
    with env.begin(write=True) as txn:
        for i in range(baseline_records):
            payload = _make_payload(payload_bytes)
            provenance = _make_provenance(provenance_bytes)
            checksum = _crc32(payload)
            ts = time.time_ns()
            key = _lmdb_journal_key(part_id, ts, i, part_id_width)
            txn.put(key, _lmdb_journal_value(DATA_TYPE, provenance, checksum, payload))
            baseline[i] = {"checksum": checksum, "payload_len": len(payload)}
    # committed on context-manager exit above

    txn = env.begin(write=True)  # deliberately held open, never committed
    for i in range(hold_records):
        seq = baseline_records + i
        payload = _make_payload(payload_bytes)
        provenance = _make_provenance(provenance_bytes)
        checksum = _crc32(payload)
        ts = time.time_ns()
        key = _lmdb_journal_key(part_id, ts, seq, part_id_width)
        txn.put(key, _lmdb_journal_value(DATA_TYPE, provenance, checksum, payload))

    _write_json(checksum_path, {
        "part_id": part_id, "baseline_records": baseline_records,
        "hold_records": hold_records, "baseline": baseline,
    })
    event.set()
    time.sleep(hold_sleep_s)  # safety net only; parent kills long before this elapses
    txn.abort()
    env.close()  # reached only if the harness's kill signal somehow failed


def lmdb_crash_reopen_proc(db_dir, map_size_mb, part_id_width, part_id,
                            baseline_records, hold_records, baseline_meta, result_path):
    result = {"opened": False, "open_error": None}
    try:
        t0 = time.perf_counter()
        env = _lmdb_open(db_dir, map_size_mb, writemap=False, create=False)
        result["open_recover_ms"] = (time.perf_counter() - t0) * 1000
        result["opened"] = True
    except Exception as exc:
        result["open_error"] = repr(exc)
        _write_json(result_path, result)
        return

    prefix = part_id.encode("utf-8")[:part_id_width].ljust(part_id_width, b"\x00")
    present_by_seq = {}
    with env.begin() as txn:
        cur = txn.cursor()
        if cur.set_range(prefix):
            for key, value in cur:
                if not key.startswith(prefix):
                    break
                seq = _lmdb_journal_key_seq(key, part_id_width)
                _, _, checksum, payload = _lmdb_journal_decode(value)
                present_by_seq[seq] = (checksum, len(payload))
    env.close()

    baseline_present = 0
    baseline_intact = True
    torn_seqs = []
    for seq_str, meta in baseline_meta.items():
        seq = int(seq_str)
        if seq not in present_by_seq:
            continue
        baseline_present += 1
        checksum, plen = present_by_seq[seq]
        ok = (plen == meta["payload_len"] and checksum == meta["checksum"])
        if not ok:
            baseline_intact = False
            torn_seqs.append(seq)

    uncommitted_present = sum(
        1 for i in range(hold_records) if (baseline_records + i) in present_by_seq
    )

    result.update({
        "total_present_for_part": len(present_by_seq),
        "baseline_target": baseline_records,
        "baseline_present": baseline_present,
        "baseline_all_intact": baseline_intact,
        "torn_seqs": torn_seqs,
        "uncommitted_attempted": hold_records,
        "uncommitted_present": uncommitted_present,
    })
    _write_json(result_path, result)


def measure_lmdb_crash_recovery(args):
    db_dir = Path(args.dir) / "crash_lmdb"
    env = _lmdb_open(db_dir, args.lmdb_map_size_mb, writemap=False, create=True)
    env.close()

    ctx = multiprocessing.get_context(args.mp_start_method)
    results_dir = Path(args.dir) / "results"
    event = ctx.Event()
    checksum_path = results_dir / "crash_lmdb_baseline.json"
    part_id = "crash-writer"

    p = ctx.Process(
        target=lmdb_crash_writer_proc,
        args=(str(db_dir), args.lmdb_map_size_mb, args.part_id_width, part_id,
              args.crash_baseline_records, args.crash_hold_records, args.payload_bytes,
              args.provenance_bytes, args.crash_hold_ms / 1000.0, event, str(checksum_path)),
    )
    p.start()
    got_event = event.wait(timeout=args.crash_event_timeout_s)
    if not got_event:
        p.kill()
        p.join()
        return {"error": "writer never signalled hold-in-progress within --crash-event-timeout-s"}

    t_kill = time.perf_counter()
    os.kill(p.pid, signal.SIGKILL)
    p.join(timeout=args.crash_event_timeout_s)
    kill_to_join_ms = (time.perf_counter() - t_kill) * 1000
    baseline_meta = _read_json(checksum_path)

    reopen_result_path = results_dir / "crash_lmdb_reopen.json"
    p2 = ctx.Process(
        target=lmdb_crash_reopen_proc,
        args=(str(db_dir), args.lmdb_map_size_mb, args.part_id_width, part_id,
              args.crash_baseline_records, args.crash_hold_records, baseline_meta["baseline"],
              str(reopen_result_path)),
    )
    t0 = time.perf_counter()
    p2.start()
    p2.join()
    reopen_process_wall_ms = (time.perf_counter() - t0) * 1000

    if reopen_result_path.exists():
        result = _read_json(reopen_result_path)
    else:
        result = {"opened": False, "open_error": f"reopen process exitcode={p2.exitcode}, no result file"}
    result["reopen_process_wall_ms"] = reopen_process_wall_ms
    result["kill_to_join_ms"] = kill_to_join_ms
    result["writer_exitcode_after_sigkill"] = p.exitcode
    return result


def measure_crash_recovery_all(args):
    out = {}
    out["sqlite"] = _safe_call(measure_sqlite_crash_recovery, args)
    if lmdb is not None:
        out["lmdb"] = _safe_call(measure_lmdb_crash_recovery, args)
    else:
        out["lmdb"] = {"error": f"lmdb not importable: {LMDB_IMPORT_ERROR}"}
    return out


# ---------------------------------------------------------------------------
# (e) range query + "current value and when it last changed"
# ---------------------------------------------------------------------------

def populate_query_store_sqlite(db_path, num_records, num_parts, payload_bytes,
                                 provenance_bytes, busy_timeout_ms):
    conn = _sqlite_connect(db_path, "FULL", busy_timeout_ms)
    _sqlite_init_journal_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    base_ts = time.time_ns()
    seq_by_part = {}
    for i in range(num_records):
        part_id = f"query-part-{i % num_parts}"
        seq = seq_by_part.get(part_id, 0)
        seq_by_part[part_id] = seq + 1
        ts = base_ts + i  # 1ns apart: strictly increasing, globally unique
        payload = _make_payload(payload_bytes)
        provenance = _make_provenance(provenance_bytes)
        checksum = _crc32(payload)
        conn.execute(
            "INSERT INTO journal(part_id,timestamp_ns,seq,data_type,payload,provenance,checksum)"
            " VALUES (?,?,?,?,?,?,?)",
            (part_id, ts, seq, DATA_TYPE, payload, provenance, checksum),
        )
    conn.commit()
    conn.close()
    return base_ts


def measure_sqlite_range_query(db_path, busy_timeout_ms, part_id, t0_ns, t1_ns, reps):
    conn = sqlite3.connect(str(db_path), timeout=busy_timeout_ms / 1000.0)
    conn.execute("PRAGMA journal_mode=WAL")
    latencies = []
    row_count = None
    for _ in range(reps):
        t_start = time.perf_counter()
        rows = conn.execute(
            "SELECT * FROM journal WHERE part_id=? AND timestamp_ns BETWEEN ? AND ?"
            " ORDER BY timestamp_ns",
            (part_id, t0_ns, t1_ns),
        ).fetchall()
        latencies.append((time.perf_counter() - t_start) * 1000)
        row_count = len(rows)
    conn.close()
    return {"latency_ms": _percentiles(latencies), "rows_returned": row_count,
            "part_id": part_id, "t0_ns": t0_ns, "t1_ns": t1_ns}


def measure_sqlite_point_lookup(db_path, busy_timeout_ms, key, reps):
    conn = sqlite3.connect(str(db_path), timeout=busy_timeout_ms / 1000.0)
    conn.execute("PRAGMA journal_mode=WAL")
    latencies = []
    found = None
    for _ in range(reps):
        t_start = time.perf_counter()
        row = conn.execute("SELECT value, updated_ns FROM settings WHERE key=?", (key,)).fetchone()
        latencies.append((time.perf_counter() - t_start) * 1000)
        found = row
    conn.close()
    return {"latency_ms": _percentiles(latencies), "found": found is not None, "key": key}


def populate_query_store_lmdb(db_dir, num_records, num_parts, payload_bytes,
                               provenance_bytes, map_size_mb, part_id_width):
    env = _lmdb_open(db_dir, map_size_mb, writemap=False, create=True)
    base_ts = time.time_ns()
    seq_by_part = {}
    with env.begin(write=True) as txn:
        for i in range(num_records):
            part_id = f"query-part-{i % num_parts}"
            seq = seq_by_part.get(part_id, 0)
            seq_by_part[part_id] = seq + 1
            ts = base_ts + i
            payload = _make_payload(payload_bytes)
            provenance = _make_provenance(provenance_bytes)
            checksum = _crc32(payload)
            key = _lmdb_journal_key(part_id, ts, seq, part_id_width)
            txn.put(key, _lmdb_journal_value(DATA_TYPE, provenance, checksum, payload))
    env.close()
    return base_ts


def measure_lmdb_range_query(db_dir, map_size_mb, part_id_width, part_id, t0_ns, t1_ns, reps):
    env = _lmdb_open(db_dir, map_size_mb, writemap=False, create=False, readonly=True)
    prefix = part_id.encode("utf-8")[:part_id_width].ljust(part_id_width, b"\x00")
    start_key = prefix + struct.pack(">Q", t0_ns)
    end_key = prefix + struct.pack(">Q", t1_ns) + b"\xff" * 4
    latencies = []
    row_count = None
    for _ in range(reps):
        t_start = time.perf_counter()
        rows = []
        with env.begin() as txn:
            cur = txn.cursor()
            if cur.set_range(start_key):
                for key, value in cur:
                    if key > end_key or not key.startswith(prefix):
                        break
                    rows.append(_lmdb_journal_decode(value))
        latencies.append((time.perf_counter() - t_start) * 1000)
        row_count = len(rows)
    env.close()
    return {"latency_ms": _percentiles(latencies), "rows_returned": row_count,
            "part_id": part_id, "t0_ns": t0_ns, "t1_ns": t1_ns}


def measure_lmdb_point_lookup(db_dir, map_size_mb, key, reps):
    env = _lmdb_open(db_dir, map_size_mb, writemap=False, create=False, readonly=True)
    latencies = []
    found = None
    for _ in range(reps):
        t_start = time.perf_counter()
        with env.begin() as txn:
            v = txn.get(_lmdb_settings_key(key))
        latencies.append((time.perf_counter() - t_start) * 1000)
        found = v
    env.close()
    return {"latency_ms": _percentiles(latencies), "found": found is not None, "key": key}


def measure_query_all(args):
    out = {}
    total_span_ns = args.query_records
    window_ns = max(1, int(total_span_ns * args.query_window_fraction))
    target_part = "query-part-0"

    sqlite_q_path = Path(args.dir) / "query_sqlite.db"
    base_ts = populate_query_store_sqlite(
        str(sqlite_q_path), args.query_records, args.query_parts,
        args.payload_bytes, args.provenance_bytes, args.busy_timeout_ms,
    )
    out["sqlite_range_query"] = _safe_call(
        measure_sqlite_range_query, str(sqlite_q_path), args.busy_timeout_ms,
        target_part, base_ts, base_ts + window_ns, args.query_reps,
    )
    settings_sqlite_path = Path(args.dir) / "settings_sqlite.db"
    if settings_sqlite_path.exists():
        out["sqlite_point_lookup"] = _safe_call(
            measure_sqlite_point_lookup, str(settings_sqlite_path), args.busy_timeout_ms,
            "setting-0", args.query_reps,
        )
    else:
        out["sqlite_point_lookup"] = {"error": "settings_sqlite.db missing -- cold_settings_read section must run first"}

    if lmdb is not None:
        lmdb_q_dir = Path(args.dir) / "query_lmdb"
        base_ts_l = populate_query_store_lmdb(
            str(lmdb_q_dir), args.query_records, args.query_parts,
            args.payload_bytes, args.provenance_bytes, args.lmdb_map_size_mb, args.part_id_width,
        )
        out["lmdb_range_query"] = _safe_call(
            measure_lmdb_range_query, str(lmdb_q_dir), args.lmdb_map_size_mb, args.part_id_width,
            target_part, base_ts_l, base_ts_l + window_ns, args.query_reps,
        )
        settings_lmdb_dir = Path(args.dir) / "settings_lmdb"
        if settings_lmdb_dir.exists():
            out["lmdb_point_lookup"] = _safe_call(
                measure_lmdb_point_lookup, str(settings_lmdb_dir), args.lmdb_map_size_mb,
                "setting-0", args.query_reps,
            )
        else:
            out["lmdb_point_lookup"] = {"error": "settings_lmdb missing -- cold_settings_read section must run first"}
    else:
        out["lmdb_range_query"] = {"error": f"lmdb not importable: {LMDB_IMPORT_ERROR}"}
        out["lmdb_point_lookup"] = {"error": f"lmdb not importable: {LMDB_IMPORT_ERROR}"}
    return out


# ---------------------------------------------------------------------------
# (f) on-disk size, and LMDB's map_size vs actual usage
# ---------------------------------------------------------------------------

def measure_sizes(args, sqlite_paths, lmdb_dirs):
    sqlite_sizes = {}
    for label, path in sqlite_paths.items():
        path = Path(path)
        if not path.exists():
            continue
        sqlite_sizes[label] = {
            "bytes": _dir_size_bytes(path, str(path) + "-wal", str(path) + "-shm"),
        }

    lmdb_sizes = {}
    if lmdb is not None:
        for label, path in lmdb_dirs.items():
            path = Path(path)
            if not path.exists():
                continue
            try:
                env = lmdb.open(
                    str(path), map_size=args.lmdb_map_size_mb * 1024 * 1024,
                    max_dbs=1, subdir=True, create=False, readonly=True, lock=False,
                )
                info = env.info()
                stat = env.stat()
                used_bytes = info["last_pgno"] * stat["psize"]
                env.close()
                lmdb_sizes[label] = {
                    "map_size_bytes": info["map_size"],
                    "used_bytes_estimate": used_bytes,
                    "used_fraction": (used_bytes / info["map_size"]) if info["map_size"] else None,
                    "on_disk_bytes": _dir_size_bytes(path),
                }
            except Exception as exc:
                lmdb_sizes[label] = {"error": repr(exc)}
    else:
        lmdb_sizes["error"] = f"lmdb not importable: {LMDB_IMPORT_ERROR}"

    return {"sqlite": sqlite_sizes, "lmdb": lmdb_sizes,
            "lmdb_map_size_mb_configured": args.lmdb_map_size_mb}


def measure_sizes_all(args):
    sqlite_paths = {
        "append_sqlite_full": Path(args.dir) / "append_sqlite_full.db",
        "append_sqlite_normal": Path(args.dir) / "append_sqlite_normal.db",
        "settings_sqlite": Path(args.dir) / "settings_sqlite.db",
        "crash_sqlite": Path(args.dir) / "crash_sqlite.db",
        "query_sqlite": Path(args.dir) / "query_sqlite.db",
    }
    lmdb_dirs = {
        "append_lmdb_default": Path(args.dir) / "append_lmdb_default",
        "append_lmdb_writemap": Path(args.dir) / "append_lmdb_writemap",
        "settings_lmdb": Path(args.dir) / "settings_lmdb",
        "crash_lmdb": Path(args.dir) / "crash_lmdb",
        "query_lmdb": Path(args.dir) / "query_lmdb",
    }
    return _safe_call(measure_sizes, args, sqlite_paths, lmdb_dirs)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_argparser():
    p = argparse.ArgumentParser(
        description="Measure SQLite vs LMDB for structured part state on this box.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--dir", default=None,
                    help="Directory to hold all stores. Default: a fresh tempdir, removed at exit unless --keep-dir.")
    p.add_argument("--keep-dir", action="store_true",
                    help="Do not delete --dir when done.")
    p.add_argument("--json", action="store_true",
                    help="Print exactly one compact JSON object to stdout and nothing else. "
                         "Without this flag the JSON is pretty-printed for human reading.")
    p.add_argument("--writers", type=int, default=4,
                    help="Concurrent writer processes for the append-throughput test (a/b).")
    p.add_argument("--records", type=int, default=2000,
                    help="Records each writer appends in the throughput test (a/b).")
    p.add_argument("--payload-bytes", type=int, default=256,
                    help="Synthetic payload blob size in bytes.")
    p.add_argument("--provenance-bytes", type=int, default=64,
                    help="Synthetic provenance string length in bytes.")
    p.add_argument("--part-id-width", type=int, default=24,
                    help="Fixed-width bytes reserved for part_id inside LMDB composite keys.")
    p.add_argument("--busy-timeout-ms", type=int, default=5000,
                    help="SQLite busy_timeout pragma (ms); also the sqlite3.connect() timeout.")
    p.add_argument("--sqlite-max-retries", type=int, default=20,
                    help="Max BEGIN IMMEDIATE retries after busy_timeout itself expires, before giving up.")
    p.add_argument("--lmdb-map-size-mb", type=int, default=4096,
                    help="LMDB map_size in MiB, pre-declared per LMDB store. Generous default "
                         "chosen so none of the tests below hit MapFullError at their default scale.")
    p.add_argument("--settings-keys", type=int, default=20,
                    help="Settings keys for the cold-read/switch-on test (c) and the point-lookup test (e).")
    p.add_argument("--settings-reps", type=int, default=20,
                    help="Fresh-process repetitions for the cold settings read (c).")
    p.add_argument("--query-records", type=int, default=20000,
                    help="Records pre-populated for the range/point query test (e).")
    p.add_argument("--query-parts", type=int, default=20,
                    help="Distinct part_ids spread across the query-test population.")
    p.add_argument("--query-reps", type=int, default=20,
                    help="Repetitions of each timed query in (e).")
    p.add_argument("--query-window-fraction", type=float, default=0.05,
                    help="Fraction of the full record-index span used as the range-query window in (e).")
    p.add_argument("--crash-synchronous", default="FULL", choices=["FULL", "NORMAL"],
                    help="SQLite synchronous pragma used for the crash-recovery test (d).")
    p.add_argument("--crash-baseline-records", type=int, default=50,
                    help="Records committed durably before the crash-inducing transaction opens.")
    p.add_argument("--crash-hold-records", type=int, default=5,
                    help="Records written but never committed before SIGKILL.")
    p.add_argument("--crash-hold-ms", type=int, default=3000,
                    help="Safety-net sleep (ms) the crash writer holds its open transaction for if the "
                         "parent's kill is somehow delayed; the parent kills immediately on seeing the "
                         "writer's ready-to-be-killed event, well before this elapses.")
    p.add_argument("--crash-event-timeout-s", type=float, default=10.0,
                    help="How long the parent waits for the crash writer's ready event before giving up.")
    p.add_argument("--mp-start-method", default="spawn", choices=["spawn", "fork", "forkserver"],
                    help="multiprocessing start method for every child process this harness spawns. "
                         "'spawn' most honestly simulates a genuinely fresh OS process for the cold-open "
                         "measurement in (c); it does NOT reproduce this project's real forkserver "
                         "switch-on path (measured separately at 2.78ms), it only isolates store-open+read "
                         "cost from this interpreter's own import/bootstrap cost.")
    return p


def main():
    args = build_argparser().parse_args()

    store_dir = Path(args.dir) if args.dir else Path(tempfile.mkdtemp(prefix="store_measure_"))
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "results").mkdir(exist_ok=True)
    args.dir = str(store_dir)

    output = {
        "measured_at_unix_ns": time.time_ns(),
        "python_version": sys.version,
        "sqlite_version": sqlite3.sqlite_version,
        "lmdb_available": lmdb is not None,
        "lmdb_import_error": LMDB_IMPORT_ERROR,
        "lmdb_version": getattr(lmdb, "__version__", None) if lmdb is not None else None,
        "cpu_count": os.cpu_count(),
        "args": {k: v for k, v in vars(args).items()},
    }

    try:
        output["append_throughput"] = _safe_call(measure_append_throughput, args)
        output["cold_settings_read"] = _safe_call(measure_cold_settings_read_all, args)
        output["crash_recovery"] = _safe_call(measure_crash_recovery_all, args)
        output["query"] = _safe_call(measure_query_all, args)
        output["sizes_on_disk"] = _safe_call(measure_sizes_all, args)
    finally:
        if not args.keep_dir:
            shutil.rmtree(store_dir, ignore_errors=True)

    if args.json:
        print(json.dumps(output, default=str))
    else:
        print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
