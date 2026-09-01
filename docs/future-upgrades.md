# Future upgrades

Scope deliberately deferred out of the current build, recorded here so it
isn't lost and isn't silently implied into a build that hasn't earned it yet.
Each entry moves out of this file only when the user says to build it, at
which point it becomes a blueprint edit (`dashboard/blueprint_edits/`) like
any other design change.

## Full NSE cash-market universe (~2000+ symbols)

**Deferred 2026-09-01.** The intraday equity segment starts on the
F&O-eligible subset (~180-200 symbols) — the same universe already needed for
stock futures and stock options, so one universe feeds three segments instead
of maintaining two. Full NSE cash coverage (~2000+ listed stocks) is the
named next upgrade for that segment once the F&O-eligible build is proven.

Why deferred rather than built alongside: the crypto build hit governor
contention (`switching-planner` shedding parts under load) at just 30 symbols
across 2 venues. A ~2000-symbol universe is an order of magnitude larger
before any of the six-broker data-redundancy layer is even accounted for —
sizing this against real measured load, the way
`measurements/2026-08-26-what-fits-on-this-box/` did for the crypto build, is
part of doing this upgrade, not a footnote to it.
