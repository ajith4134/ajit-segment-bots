# Measurements behind the part runtime spec

Every claim in `docs/superpowers/specs/2026-08-20-part-runtime-design.md` that carries a
number comes from a script in this directory. They are kept so an answer can be
**re-run rather than believed** — Rule 0. Each one prints raw numbers and no verdicts;
the reading of them is in the spec, where it can be argued with.

Run them with a standard-build interpreter (§15.1). They were run on 2026-08-20 with
Python 3.14.4, numpy 2.5.2, OpenBLAS 0.3.34, threadpoolctl 3.6.0.

| Script | What it establishes | Spec section |
|---|---|---|
| `measure_wheel_abi_coverage.py` | which packages publish `cp314` but not `cp314t` wheels — decisive on a box with no compiler | §15.1 |
| `measure_glue_scaling.py` (+ `glue_workload.py`) | pure-Python bar aggregation: 1 worker vs K threads vs K processes, setup excluded by a barrier | §15.1 |
| `measure_interpreter.py` | build identity, GIL state before/after every import, forkserver viability, per-part RSS/PSS, MemAvailable return | §15.1, §2 |
| `measure_blas_thread_spawn.py` | kernel thread count before/after `import numpy` and after a matmul, with and without the thread caps | §1 rule 3 |
| `measure_switch_on_reliability.py` | fork-then-place switch-on: success rate and latency across settle delays | §3 |
| `measure_memmap_survives_sigkill.py` | whether unflushed `numpy.memmap` state survives `SIGKILL` | §15.4 |
| `measure_numeric_state.py` | the same question in depth: fd/VMA/RSS trend across many off/on cycles, RAM return, `shared_memory` after `SIGKILL` | §15.4 |
| `measure_store.py` | SQLite vs LMDB: concurrent multi-process append, contention, cold settings read, crash recovery, range queries | §15.2 |
| `measure_store_under_limit.py`, `measure_sqlite_under_limit.py`, `measure_page_cache_under_limit.py` | what a writer's page cache costs against its cgroup `memory.max` | §5 |

## Read this before running any of them

**Never point one of these at `/tmp`.** On this box `/tmp` is **tmpfs** — RAM with a
filesystem interface. A memory measurement taken there is measuring RAM against RAM and
means nothing, and a durability measurement there proves nothing about writeback to disk.

This is not hypothetical: the first round of store testing ran under `/tmp` and produced
an OOM kill for LMDB, then for SQLite, then for a plain file write containing no database
at all. Re-run on ext4, none of them fail. The scripts that take a `--dir` still default
it to a temporary directory, which on this box lands on tmpfs — **pass `--dir` explicitly,
somewhere under `$HOME`.** Making that default safe is phase 0 work.

Check what you are measuring on before trusting a number:

    stat -f -c '%T' <dir>       # want ext2/ext3 (ext4 reports this), never tmpfs

## The other trap: a busy box

`measure_glue_scaling.py` gave a 2.1× spread and then contradicted itself when this
session's own research agents were running. E2 is a shared-core machine and its
throughput carries host variance nobody here can observe. Take five repetitions on a
quiet box and report the spread, not one run.
