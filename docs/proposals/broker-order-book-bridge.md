# broker-order-book-bridge

Given by the user 2026-09-01, continuing the order-execution path. Second
of two bridges `paper-fill-simulator` needs (the first, `broker-market-
data-bridge`, landed earlier this session).

Republishes Upstox's own depth updates (`broker-order-book-snapshot`) as
`order-book-snapshot`, the crypto-era type `book-walk-fill-pricer`,
`limit-price-walker` and others already read. `OrderBookSnapshot`'s fields
(`venue_id`, `symbol`, `bids`, `asks`, `sequence`, `venue_time_ns`,
`is_from_snapshot`) turned out to have no genuinely crypto-specific
mandatory field -- unlike `market-data`, no type widening was needed.

Bids and asks are explicitly sorted best-first rather than trusted from
Upstox's own level ordering, verified by a test that deliberately feeds
levels out of order -- `OrderBookSnapshot.best_bid`/`best_ask` read the
first element as the best, and a bridge that silently assumed input order
would misprice every fill walked against it if that assumption were ever
wrong.

With both bridges landed, `paper-fill-simulator` now has real inputs for
every crypto-shaped type it consumes except `consolidated-price` and
`cost-estimate`/`fill-price-estimate` (its own upstream feature-shaping
parts, not audited this session). Wiring `paper-fill-simulator` itself for
the options segment -- confirming its internal logic (which never read
`.side`, confirmed earlier) genuinely produces sensible fills against real
Indian option prices and depth -- is the next slice, not done here.
