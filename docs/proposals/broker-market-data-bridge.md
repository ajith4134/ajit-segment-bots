# broker-market-data-bridge

Given by the user 2026-09-01, continuing the order-execution path after
correcting the `position-sizer` framing (it needed no changes at all; the
real blocker was one level up -- no real `fill` for options existed
anywhere in the pipeline).

Researched first: does Upstox's sandbox simulate options order placement?
Genuinely unclear from their own docs -- every sandbox code sample uses
equity instruments, the docs never state whether `NSE_FO` tokens are
accepted by the sandbox specifically, and confirming empirically would need
real sandbox credentials this project doesn't have yet. Not blocking on an
unverifiable external behaviour: building a local, self-contained
simulation (the same approach the crypto build itself proved before any
live venue was connected) is the unblocked path.

Republishes Upstox's `broker-market-data` (LtpUpdate) as `market-data`
(`NormalisedTrade`), the type `paper-fill-simulator` and ~30 other detector/
execution-layer parts already consume. `NormalisedTrade.side` widened from
`str` to `str | None` -- verified by an exhaustive search that zero real
consumers read `.side`/`.signed_quantity` on this type, so `None` (never a
fabricated aggressor) is honest and safe. `sequence` uses the project's own
existing `NOT_SENT` sentinel, the same one `binance_usdm.py` already uses
for a venue that sends no sequence number.

**Not done here:** the `order-book-snapshot` bridge (`broker-order-book-
snapshot` -> `OrderBookSnapshot`), which `paper-fill-simulator`'s book-walk
pricing also needs. Separate slice, same pattern, structurally confirmed
compatible earlier this session (no crypto-specific mandatory field).
