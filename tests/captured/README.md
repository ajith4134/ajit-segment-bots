# Captured venue payloads

**Every file here was sent by a real venue.** Nothing in this directory was
written by hand, edited afterwards, or reconstructed from documentation.

RL-063: tests run on real captured crypto data, never invented fixtures. The
reason is narrower than it sounds. A hand-written message tests what its author
believed the venue sends — which is precisely the belief the test existed to
check — and the fields worth testing are the ones nobody would have thought to
invent. Three from the Binance capture alone:

- the partial-depth stream carries `U`/`u`/`pu` update ids, which the docs
  describe only for the *diff* depth stream. Sequence continuity is checkable on
  a stream we assumed was stateless snapshots.
- an `aggTrade`'s `T` (when the trade happened) and `E` (when the venue emitted
  the event) genuinely differ, so which one an index is built on is a real
  choice rather than a stylistic one.
- the subscribe acknowledgement `{"result":null,"id":N}` has no `e` field at
  all, which is how a control frame is told from a data frame — a distinction no
  documentation states in those terms.

## How to recapture

    .venv/bin/python tests/captured/capture_venue_payloads.py binance-usdm --day 2026-08-22

`capture_venue_payloads.py` is the generator; everything beside it is output.
The day is passed in rather than read from the clock, so a re-run names the file
the operator meant rather than the file today happens to be.

**The capture builds its URLs, topics and subscribe frames from the adapter
itself.** A fixture captured with a hand-built frame would be evidence about that
frame instead of about the code that will run — and the first thing it would stop
catching is a wrong routed path, which on Binance produces a connection that is
open, silent, and indistinguishable from a quiet market.

## What is in each file

`*.jsonl` — one captured message per line, as
`{"received_at_ns": …, "payload": "<exactly what arrived>"}`. The payload is
JSON-encoded as a string rather than written raw so that a message containing a
newline cannot become two records; today's two venues send single-line JSON, and
a format that silently depended on that would break on the venue that does not.

`*-exchange-info-subset.json` — a REST catalogue response, **subset but not
edited**. The full response is over a megabyte and would be permanent weight in
every future clone. The subset rule is recorded in the manifest and is the point:
the first few symbols of each `(contractType, status)` pair the venue returned,
so the file contains a `TRADIFI_PERPETUAL` and a `SETTLING` contract — the two
cases the capturable-symbol rule turns on. A subset of the alphabetically first
ten symbols would very likely contain neither.

`capture-manifest.json` — for every file: which venue, which URL, what was
subscribed, on what day, and how it was subset. It also records the live counts
at capture time (872 contracts listed, 570 `PERPETUAL/TRADING`, 170
`TRADIFI_PERPETUAL/TRADING`, 127 `SETTLING`) and the response headers of note
(`x-mbx-used-weight-1m`), which is where the used-weight reader is tested against
a header a venue actually sent.

A fixture with no manifest entry is a file of unknown origin, which is not
evidence of anything.

## What these are not

They are not the tape. The tape is what phase 1 captures continuously and it
lives outside the repository, at `tape_root`. These are a few dozen messages
each, kept small enough to be permanent weight in a clone and specific enough to
pin the venue oddities the adapters exist for. Volume belongs on the tape.
