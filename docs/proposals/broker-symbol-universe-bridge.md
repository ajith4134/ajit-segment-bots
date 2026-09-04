# broker-symbol-universe-bridge

**Proposed 2026-09-04.** Republishes a broker's own instrument master as
`symbol-universe` -- the crypto-era type eleven parts already consume and which,
since the 2026-09-02 cutover, **no running part produces**.

## The gap

`symbol-catalogue-reader` is the only producer of `symbol-universe` in the
blueprint. It is built on the Binance/Bybit `VenueAdapter` and was deliberately
taken off the live spine when the feed was cut over to Indian data
(`operate/run_live_spine.py`, "the old three -- symbol-catalogue-reader,
stream-budget-planner, venue-trade-stream-reader ... are off this spine now, not
deleted"). Four bridges were built to close the equivalent gap for other types:

    broker-market-data-bridge            -> market-data
    broker-order-book-bridge             -> order-book-snapshot
    broker-underlying-price-frame-bridge -> symbol-price-frame
    broker-candle-bridge                 -> candle

**`symbol-universe` is the missing fifth**, and nothing reported it. Measured on
the live spine 2026-09-04, eleven parts consume the type and seven of them are
running and starved:

    universal-symbol-sweeper   3,114 sweeps over an EMPTY list -- every skip
                               counter reads 0, which is the signature of
                               iterating nothing rather than rejecting things
    bull-feature-builder       vectors_built 0
    bear-feature-builder       -- same shape
    feed-coverage-auditor      cannot audit coverage of an unknown universe
    options-flow-reader
    instrument-selector        listings_registered 0
    tick-size-resolver

The consequence is the whole scanning chain: no `symbol-universe` means the
sweeper sweeps nothing, which means zero `entry-candidate`, zero
`bull-side-candidate`, zero feature vectors, zero convictions, zero intents and
therefore no order. **No paper trade has ever opened on this segment**, and this
is one of the two reasons why.

## What it does

Consumes `broker-instrument-listing` (Upstox's instrument master, 102,940
listings already flowing) and `broker-price-frame`, and publishes
`symbol-universe` as a level.

Per R-01 this is the explicit, named crossing between the broker's own types and
the crypto-era type. `InstrumentListing`'s own docstring records why the two were
never merged: "reusing that type id would wire this reader into every existing
crypto consumer of symbol-universe". A bridge is the deliberate crossing that
design left room for -- the same reasoning
`broker-underlying-price-frame-bridge` was built on.

**`symbol` is the `trading_symbol`, not the `instrument_key`.** Every other
bridge already resolves it that way -- `broker-market-data-bridge` republishes
LTP under `listing.trading_symbol` -- so a universe keyed on anything else could
never be matched to a price by `universal-symbol-sweeper`, whose universe is
keyed `(venue_id, symbol)` and whose `skipped_unmeasurable` counts exactly that
failure. The instrument_key format is Upstox's own implementation detail (T-4).

## What goes in it, and why it is bounded

Two kinds of entry:

1. **The tracked index underlyings** (`symbol_universe_index_trading_symbols`:
   NIFTY, BANKNIFTY, SENSEX). These are the only symbols with prices dense
   enough to fill a detector window -- measured 2026-09-04, 3 of 3 index
   instruments reached the 256-observation floor and their p99 gap between
   prints is 0.6s. `instrument_kind` is None for them: `trading_types` has no
   INDEX kind, and None already means "unknown kind, treat as no kind" rather
   than being read as any particular one.

2. **The nearest expiry's option contracts for those underlyings**, ranked by
   how close their strike sits to the underlying's own last traded price and
   capped at `symbol_universe_option_contracts_per_underlying`.

**The cap is not a convenience, it is a standing ruling.** The operator's note
on `captured_symbol_count` (2026-08-25) reads: "hold at 50 and stop walking. The
count is not raised again until the project is 100% built". The reason is
recorded in the same note and is structural -- `cointegration-pair-finder` and
`spread-reversion-detector` fail as the *square* of the universe: at 205 symbols
the pair finder had tested 3,277,888 pairs and the spread detector had dropped
332,858 inputs and was still climbing. An option chain's nearest expiry across
three indices is well over a thousand contracts, so publishing it whole would
reproduce that failure exactly, on a segment that has never completed a trade.

Ranking by distance from the money is what makes a small cap the *right* small
cap rather than an arbitrary slice: measured the same day, only 76 of 891 traded
NSE_FO instruments (8.5%) had traded enough times to fill a detector window,
and the ones that had not are the far-from-the-money contracts. The bridge
therefore selects the contracts that actually trade rather than the first ones
the instrument master happens to list.

An underlying whose price has not arrived yet contributes **no** option
contracts, and the count of them is reported. Ranking by distance from a price
nobody has read would be ranking by nothing; a bridge that silently fell back to
"the first N strikes" would look identical on the board while selecting a
different universe (Rule 8).

## What this does NOT fix

`instrument-selector.observe_listed_symbol` registers only `PERPETUAL_FUTURE`
and returns early for options -- deliberately, from the crypto era: "Spot and
options are not registered either ... those segments are honestly empty (RL-050,
RL-062)." So this bridge unblocks the **scanning** half (sweeper, feature
builders, detectors) and does not by itself let an intent become an order. The
options half of `instrument-selector` is separate work, now the next blocker on
the execution path rather than a hidden one.

## Contract

    consumes  broker-instrument-listing, broker-price-frame
    produces  symbol-universe, part-health

    resource_class      bandwidth-bound
    rate_risk           changes-the-answer
    skipped_tick_effect delays

`symbol-universe` is a level, restated on `symbol_universe_restatement_interval`
(30s, already set) through `LevelPublisher` -- the same cadence and the same
reasoning `symbol-catalogue-reader` uses: a level published only when the source
is re-read is an event to every consumer that was not listening at that moment.

## Settings

| | |
|---|---|
| `symbol_universe_index_trading_symbols` | the index underlyings whose chains are published |
| `symbol_universe_option_contracts_per_underlying` | how many nearest-the-money contracts per underlying, per expiry |
| `symbol_universe_restatement_interval` | already exists; reused unchanged |
