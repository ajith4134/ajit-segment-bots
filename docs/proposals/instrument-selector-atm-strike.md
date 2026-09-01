# instrument-selector: ATM strike selection

Given by the user 2026-09-01 ("Yes start in the order execution path") —
the first piece of the order-execution path, and the hard blocker on every
part downstream of it (no order can be placed without knowing which
contract).

Implements `docs/superpowers/specs/2026-09-01-options-segment-bots-design.md`
§3 exactly: default policy is ATM (delta closest to 0.5), a settings-driven
delta target is the spec's own named future upgrade, not built here.

**Why a separate tracker, not folded into `select()`'s own cost
comparison:** `select()` picks the *cheapest* candidate among what's
registered. Cheapest and closest-to-0.5-delta are different things -- a
deep-OTM option is cheaper, not more ATM. `AtmStrikeTracker`
(`runtime/atm_strike_tracker.py`) filters to the one ATM strike *before*
registration, which is what makes "Default policy: ATM" literally true
rather than an emergent (and wrong) side effect of cost-minimization.

**A real gap found in the existing framework, fixed here:**
`ListedInstrument.supports_short` answers "can this instrument express a
bearish view" and already existed (spot: `False`, perpetual/dated-future:
`True`). Nothing symmetric existed for "can this express a bullish view,"
because no crypto instrument was ever bullish-only. A bought PUT is the
first one -- buying a put to express a bullish intent would have been
silently accepted with nothing to stop it. Added `supports_long: bool =
True` (default keeps every existing crypto registration valid, unchanged)
and the matching `_cannot_carry` check.

**A second real bug found and fixed:** `SEGMENT_OF[OPTION]` still mapped to
`"options"`, the retired crypto segment id. With `built_segments=
("index-options",)`, every real option candidate would have been refused
as living in an unbuilt segment -- the same class of bug already fixed in
`cross-segment-signal-bridge`/`cross-segment-lesson-bridge`.

**Not done here, named so it isn't lost:** `broker-market-data`'s option
LTP is not wired to `observe_option_price` from `start_part` -- it needs
`LtpUpdate.instrument_key` correlated against the option contract, real
additional wiring, not a one-line addition. `leverage-selector` staying
unwired for the options segment (confirmed safe, prior session), and the
rest of the order-execution chain (`position-sizer`'s `account-balance` ->
`broker-account-funds` redesign, the `market-data`/`order-book-snapshot`
bridges confirmed safe this session by an exhaustive `.side` search) are
each their own next slice.
