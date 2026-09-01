# exchange-announcement-reader: broker-instrument-listing replaces symbol-universe

Given by the user 2026-09-01 ("Continue doing next").

Unlike most of the crypto-only audit, this part is not really crypto-only in
concept -- corporate/exchange announcements (delistings, circuit halts,
tick-size changes) apply to NSE/BSE the same way they apply to a crypto
venue, and the announcement-matching mechanism (effective-time vs.
publication-time, kind classification, symbol matching against a declared
universe rather than substring) is already fully generic. The only crypto
coupling was its one input, `symbol-universe`.

**A real finding along the way:** this part's own `start_part` docstring
already said no announcement feed was ever connected on this box, for
crypto either -- `read_rows()` always returned `()`. So converting the
universe input to `broker-instrument-listing` (matching against
`trading_symbol`, resolved the same way the underlying-price bridge and
open-interest aggregator already do) makes this part Indian-ready in
exactly the same "path exists, no fetcher installed yet" state it was
already honestly in. Nothing regresses; nothing was fabricated.

`FUNDING_CHANGE` and `LEVERAGE_CHANGE` stay in `ANNOUNCEMENT_KINDS` --
harmless, crypto-perpetual-specific kinds that simply won't ever be
classified from Indian announcement data until Phase B's margin segment
exists. Not removed, not faked with an Indian substitute.
