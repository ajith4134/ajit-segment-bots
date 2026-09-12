"""Re-fit settings whose numbers were measured on crypto to the Indian market.

Goal 2, item 3 -- the acting half. A file that mentions Binance is a naming
problem; a **number** fitted to Binance is a decision being made every tick on a
market this project does not trade. `mean_reversion_minimum_volatility_fraction`
was five basis points because that was "just under the round trip at the venues'
taker fees"; on NSE it refused 92.2% of every in-session NIFTY observation and
the detector raised zero candidates.

Every value below is derived from real Indian data and carries where it came
from, per RL-061. The measurement is in
`measurements/2026-09-12-indian-order-sizes/`, taken from this project's own
captured tape for 2026-09-08 -- 601 instruments, 7,822 share prints and 68,791
option prints:

    kind     median      p75       p95       p99       max
    share     2,224    8,572    52,866   183,990   1,093,495
    option   13,722   44,026   201,000   464,100     969,000

**Idempotent.** A setting already carrying the Indian value is left alone and
reported as such. Re-running changes nothing.

**Writes the operator's live file**, so it backs it up first and refuses while
it cannot. Settings are the operator's (RL-055); this exists so the change is
reviewable as a script with its reasoning attached, not so it happens quietly.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sys
import time

SETTINGS = pathlib.Path.home() / ".config/ajit-segment-bots/settings/runtime.toml"

CONVERTED_MARKER = "REFITTED 2026-09-12"
INERT_MARKER = "INERT 2026-09-12"
MECHANISM_MARKER = "NEEDS AN INDIAN MECHANISM 2026-09-12"
INDEPENDENT_MARKER = "MARKET-INDEPENDENT 2026-09-12"
INCONCLUSIVE_MARKER = "MEASURED BUT INCONCLUSIVE 2026-09-12"
MEASURED = "measurements/2026-09-12-indian-order-sizes/"
TAPE = "this project's own captured tape for 2026-09-08"

# What this desk actually trades, from the segment files: a trade commits between
# 100,000 and 200,000 rupees. A reference order size exists to measure book
# impact *for the orders this bot places*, so the size it trades is the honest
# basis -- not a multiple of the median print, which is what the crypto figure
# used because a crypto bot's order was near the median print.
DESK_ORDER_SIZE = 150_000.0

# (value, provenance) or (value, provenance, unit) where the unit changes too.
REFITS: dict[str, tuple] = {
    # (new value as it should appear after `value = `, the provenance sentence)
    "captured_venues": (
        "[]",
        "Claude, 2026-09-12: the venues captured through a VENUE ADAPTER "
        '(runtime/venues/*.py). Was ["binance-usdm", "bybit-linear"], markets '
        "this project has not traded since the pivot of 2026-09-01. EMPTY, not "
        '["upstox"], and the difference is the point: Upstox is reached through '
        "a BROKER adapter (runtime/brokers/upstox.py), which is a different "
        "interface, so naming it here made `load_venue_adapter` look for "
        "runtime/venues/upstox.py and crash-loop feed-gap-detector -- measured "
        "on this spine the moment it was tried. This spine captures no "
        "venue-adapter venue at all, and empty is how that is said. The broker "
        "feed is watched through `broker_feed_venue_id`, which feed-gap-detector "
        "already adds separately (2026-09-06, added because every Upstox print "
        "was otherwise skipped and the part watched two streams that had already "
        "stopped). symbol-catalogue-reader and stream-budget-planner refuse an "
        "empty list by design -- correctly, since they are the crypto capture "
        "path and neither is on this spine; broker-symbol-universe-bridge "
        "produces `symbol-universe` in their place.",
    ),
    "market_thesis_bellwether_symbols": (
        '["NIFTY", "BANKNIFTY", "SENSEX"]',
        'Claude, 2026-09-12: the symbols a market thesis is formed against. Was '
        '["BTCUSDT", "ETHUSDT", "SOLUSDT"]. A bellwether has to be something '
        "this market actually watches, and these are measurably the three "
        f"busiest indices on {TAPE}: NIFTY 30,269 prints, BANKNIFTY 24,187, "
        "SENSEX 5,831, with no other index reaching a thousand. NIFTY is the "
        "market, BANKNIFTY its largest sector, SENSEX the other exchange's "
        "benchmark. Ranked by captured activity rather than by index weight, "
        "which this project does not hold constituent weights for -- so it is a "
        "liquidity ranking and says so.",
    ),
    "bull_reference_order_size_quote": (
        str(DESK_ORDER_SIZE),
        "Claude, 2026-09-12: the order size the bull bot measures book impact "
        "against. Was 1000.0 USDT, chosen as above the 166 USDT median trade on "
        "binance-usdm. The rupee figure is derived differently and deliberately: "
        "this desk's own trade commits between minimum_capital_per_trade "
        "(100,000) and maximum_capital_per_trade (200,000), so 150,000 is the "
        "size it really places, and impact should be measured at the size that "
        f"will be traded. Measured against {TAPE}, that is about 11x the median "
        "option print (13,722) and above the 95th percentile (201,000 is p95), "
        "which correctly says this desk is a large participant per order rather "
        f"than a typical one. Distribution in {MEASURED}.",
    ),
    "bear_reference_order_size_quote": (
        str(DESK_ORDER_SIZE),
        "Claude, 2026-09-12: mirrored for the short side from "
        "bull_reference_order_size_quote, same derivation and same measurement. "
        "Was 1000.0 USDT.",
    ),
    "liquidity_reference_order_size": (
        str(DESK_ORDER_SIZE),
        "Claude, 2026-09-12: the order size liquidity-grader walks the book at. "
        "Was 1000.0 USDT. Set to the same 150,000 rupees the two bots measure "
        "impact against, because a grade taken at a different size than the "
        "order that will be placed is a grade for an order nobody sends -- which "
        "is the mismatch runtime/symbol_round_trip_cost.py exists to close.",
    ),
    "slippage_size_bands": (
        '"10000,50000,200000"',
        "Claude, 2026-09-12: the notional bands slippage is learned per. Was "
        '"1000,10000,100000" USDT. Set to the real shape of Indian option '
        f"prints on {TAPE}: median 13,722, p75 44,026, p95 201,000. The bands "
        "bracket that distribution so each holds a real population rather than "
        "one holding everything -- the crypto bands in rupees would have put "
        "almost every print in the top band and learned one number.",
    ),
    "anomaly_minimum_quote_volume_for_a_move": (
        "10000.0",
        "Claude, 2026-09-12: the traded value a price move needs before it "
        "counts as a move rather than a print. Was 1000.0 USDT. Ten thousand "
        f"rupees sits just below the median option print on {TAPE} (13,722) and "
        "well above the median share print (2,224), so a move carrying less than "
        "this really is a small print -- which is the question the setting asks.",
    ),
    "whale_minimum_quote_value": (
        "1000000.0",
        "Claude, 2026-09-12: the traded value above which one print is somebody "
        "large rather than ordinary flow. The NUMBER is unchanged from the "
        "crypto era and the MEANING is not: 1,000,000 USDT was roughly eight and "
        "a half crore rupees, which no single NSE print reaches. Measured on "
        f"{TAPE}, the largest single print seen at all was 1,093,495 rupees on a "
        "share and 969,000 on an option, so one million rupees is the top of the "
        "observed distribution and is the right place for this line. Kept at the "
        "same digits by coincidence of unit, which is exactly why the provenance "
        "has to say so rather than leaving it looking untouched.",
    ),
    "autonomy_notional_ceilings": (
        "[0.0, 0.0, 100000.0, 200000.0]",
        "Claude, 2026-09-12: the most notional each autonomy tier may commit. "
        "Was [0.0, 0.0, 1000.0, 5000.0] USDT. Re-expressed against this desk's "
        "own bounds rather than converted at a rate: the two open tiers are now "
        "minimum_capital_per_trade (100,000) and maximum_capital_per_trade "
        "(200,000), so an autonomous tier can never commit more than the "
        "operator already allows a single trade. The two zero tiers stay zero -- "
        "they are the tiers that may not trade at all, and that is not a "
        "currency question.",
    ),
    "risk_maximum_stop_fraction": (
        "0.33",
        "Claude, 2026-09-12: the furthest a stop may sit from entry, whatever "
        "produced the distance. Was 0.05, and its own note gives the crypto "
        "basis: 'against a measured BTCUSDT range of 77415.1-77518.1 over the 28 "
        "seconds captured' -- a range of 0.13%. An NSE OPTION is a different "
        f"instrument entirely. Measured on {TAPE} (measurements/2026-09-12-indian-return-distribution/), 298 contracts: the "
        "median session high-low range is 12.89% of the opening price and the "
        "95th percentile is 33.12%. A stop capped at 5% of entry sits INSIDE "
        "ordinary intraday movement on essentially every option, so it is hit by "
        "noise rather than by the thesis being wrong -- and a stop that cannot "
        "be placed outside the noise makes every exit a coin flip. 0.33 is the "
        "measured p95 session range: a stop may now be placed beyond what an "
        "ordinary session does. It is a SANITY CAP and not the risk control: "
        "what actually bounds loss is risk_maximum_per_position_fraction, one "
        "percent of the allotment, which sizes the position so that even a stop "
        "this wide risks only that one percent.",
    ),
    "bull_outlier_deviation_threshold": (
        "4.0",
        "Claude, 2026-09-12: how far from its own history a feature must sit "
        "before the vector is flagged out of distribution. RE-DERIVED AND "
        "UNCHANGED. Its note chose four over the conventional three because "
        "'crypto features are fat-tailed', and the question is whether Indian "
        f"ones are too. Measured on {TAPE} (measurements/2026-09-12-indian-return-distribution/): the ratio of the 99.9th "
        "percentile print-to-print move to the median is 33x for NSE options, "
        "53x for shares and 16x for the index -- fat by any reading, and fat in "
        "the same direction the note reasoned in. Three standard deviations "
        "would flag ordinary Indian movement as anomalous. The number stands "
        "and now stands on Indian evidence.",
    ),
    "bear_outlier_deviation_threshold": (
        "4.0",
        "Claude, 2026-09-12: mirrored for the short side from "
        "bull_outlier_deviation_threshold -- re-derived against Indian tails and "
        "unchanged, for the reason stated there.",
    ),
    # Derived from Indian print cadence, measured on the same tape:
    #   kind    median gap   p95      p99      prints/min
    #   index      1.00s    28.0s    42.0s        11.5
    #   share      6.99s   110.0s  1055.8s         0.7
    #   option     8.27s   147.8s   858.2s         0.7
    # BTCUSDT printed about four times a SECOND. An Indian option prints 0.7
    # times a MINUTE -- 340 times slower -- and every number the crypto build
    # derived from print rate is wrong by about that factor.
    "feed_gap_threshold": (
        "150.0",
        "Claude, 2026-09-12: how long a stream may be silent before the silence "
        "is a feed-gap. Was 60.0, written against a Binance connection. Measured "
        f"on {TAPE} (measurements/2026-09-12-indian-feed-cadence/): ordinary silence between two prints of the same "
        "NSE option reaches 147.8 seconds at the 95th percentile and 858 at the "
        "99th, so a 60-second floor calls ordinary Indian silence a feed gap. "
        "150 is that p95, which is the least silence that is not normal. This is "
        "a FLOOR under `feed_gap_patience_multiple x the stream's own p99`, so a "
        "stream that has learned its own habit is judged by that instead; the "
        "floor only decides for a stream nothing has measured yet, which is "
        "exactly when a false gap is most likely.",
    ),
    "price_series_maximum_gap_seconds": (
        "150.0",
        "Claude, 2026-09-12: how long a symbol may be silent before the series a "
        "part is judging is treated as having a hole and the window is cleared. "
        "Was 120.0. Same measurement and same floor role as feed_gap_threshold "
        f"above (measurements/2026-09-12-indian-feed-cadence/): at 120 seconds an ordinary quiet NSE option clears a "
        "window that had nothing wrong with it, and a window cleared for no "
        "reason is a detector that never accumulates enough history to fire.",
    ),
    "bull_cold_start_price_window": (
        "4000",
        "Claude, 2026-09-12: how many recent prints per symbol the exit-plan "
        "proposer keeps so a range can be measured. RE-DERIVED AND UNCHANGED, "
        "which is a result rather than an omission. Its crypto note said four "
        "thousand 'covers a 60s horizon on the busiest symbols' -- true at "
        f"BTCUSDT's four prints a second. Measured on {TAPE} (measurements/2026-09-12-indian-feed-cadence/) the "
        "busiest Indian instrument is the NIFTY index at 11.5 prints a minute, "
        "so 60 seconds buys 12 prints and a window sized for a horizon would "
        "hold almost nothing. The Indian basis is a full session instead: 11.5 "
        "a minute across a 375-minute NSE day is 4,312 prints, so four thousand "
        "covers very nearly one session of the busiest thing this project "
        "watches. Same number, completely different reason, and the reason is "
        "what the next session needs.",
    ),
    "bear_cold_start_price_window": (
        "4000",
        "Claude, 2026-09-12: mirrored for the short side from "
        "bull_cold_start_price_window -- re-derived against Indian print rates "
        "and unchanged, for the reason stated there.",
    ),
    # Anchored to taker_fee_rate by their own notes, so correcting that fee
    # without these would leave three numbers pointing at a rate that no longer
    # exists -- the shape of drift where one fix makes a neighbour wrong.
    "bull_entry_prior_entry_cost_fraction": (
        "0.004266",
        "Claude, 2026-09-12: what the entry timer assumes a fill costs before it "
        "has measured its own. Was 0.0005, and its own note says why: 'anchored "
        "to the taker fee it will also pay'. That fee was bybit-linear's 0.00055 "
        "and is now Upstox's real per-side options cost, 0.004266, so the anchor "
        "moved and this follows it. Left un-updated it would assume an entry "
        "costs an eighth of what the same file says it costs -- and the timer "
        "would wait for an edge it had already decided was affordable.",
    ),
    "bear_entry_prior_entry_cost_fraction": (
        "0.004266",
        "Claude, 2026-09-12: mirrored for the short side from "
        "bull_entry_prior_entry_cost_fraction, same anchor and same reason.",
    ),
    "liquidity_deep_cost_fraction": (
        "0.004266",
        "Claude, 2026-09-12: the walk cost at the reference order size below "
        "which liquidity-grader calls a symbol deep. Was 0.001 -- 'ten basis "
        "points, the cost of a single taker fee' in its own note, against a "
        "crypto fee of 5.5 basis points. On NSE one side really costs 0.4266%, "
        "so a symbol whose walk is under ten basis points is not merely deep, it "
        "is cheaper than the charge stack allows any Indian option to be. At the "
        "old figure this grade was unreachable and every symbol read thinner "
        "than it is.",
    ),
    # The seven settings whose UNIT still said USDT. A rupee amount labelled in a
    # crypto stablecoin is half-converted, and the label is what a reader trusts.
    "whale_minimum_quote_value": (
        "1000000.0",
        "Claude, 2026-09-12: see the sentence above -- the digits are unchanged "
        "and the meaning is not. The unit is what was actually wrong.",
        "INR",
    ),
    "options_minimum_premium": (
        "10000.0",
        "Claude, 2026-09-12: the premium below which an options trade is not flow "
        "worth recording. The figure survives the change of market and the unit "
        "does not: ten thousand USDT is roughly 8.5 lakh, which would discard "
        "almost every option print on NSE, while ten thousand rupees sits just "
        "below the median Indian option print of 13,722 measured on this "
        "project's own tape for 2026-09-08 -- so it keeps the ordinary flow and "
        "drops the noise, which is what the operator asked it for.",
        "INR",
    ),
    "fund_conservation_tolerance": (
        "0.01",
        "Claude, 2026-09-12: how far the books may disagree before it is a fault. "
        "The number is right for rupees by luck of scale and the unit was wrong: "
        "one paisa is the smallest amount an Indian account can differ by, since "
        "rupee amounts settle to two decimals, so this is exactly one tick of "
        "the currency rather than an arbitrary tolerance.",
        "INR",
    ),
    "pnl_reconciliation_tolerance": (
        "0.01",
        "Claude, 2026-09-12: same as fund_conservation_tolerance -- one paisa, "
        "the smallest difference an Indian account can hold, not a rounded USDT.",
        "INR",
    ),
    "replay_fee_tolerance": (
        "1e-6",
        "Claude, 2026-09-12: how far a replayed fee may differ from the real one "
        "before the replay is not reproducing the run. A millionth of a rupee is "
        "far below one paisa, which is the point: this is a floating-point "
        "tolerance rather than a money tolerance, and it is unchanged because "
        "arithmetic error does not depend on the currency. Only the unit was "
        "wrong.",
        "INR",
    ),
    "llm_metered_per_call_ceiling": (
        "0.05",
        "Claude, 2026-09-12: the most one metered LLM call may cost. NOT "
        "converted to rupees, and that is the finding: every provider this "
        "project can call bills in US DOLLARS, so the amount was always right "
        "and the unit was always wrong -- USDT is a crypto stablecoin, USD is "
        "what Anthropic and OpenAI actually charge. Relabelled, not re-derived.",
        "USD",
    ),
    "llm_spend_ceiling": (
        "1.0",
        "Claude, 2026-09-12: the most the whole system may spend on metered LLM "
        "calls. Same as llm_metered_per_call_ceiling -- billed in US dollars, so "
        "the number stands and the unit was the error. A settlement currency and "
        "a supplier's billing currency are different questions, and this file "
        "had them as one.",
        "USD",
    ),
    "taker_fee_rate": (
        "0.004266",
        "Claude, 2026-09-12: what crossing the spread costs per side. Was "
        "0.00055 -- bybit-linear's published taker fee, the higher of the two "
        "crypto venues. NSE charges nothing resembling it: the real per-side "
        "cost of an Upstox options round trip is 0.4266%, already derived and "
        "already in this file as per_side_trading_cost_fraction, from Upstox's "
        "own six-line charge stack plus half the measured spread "
        "(measurements/2026-09-07-indian-price-staleness/). The crypto figure "
        "understated it 7.8-fold, and three parts that run every tick used it "
        "unconditionally: position-sizer charges it as the fee on every size, "
        "execution-cost-model makes it the fee component of every estimate "
        "(628,233 of them so far), and tail-mover-qualifier doubles it into the "
        "round trip a mover has to clear -- so the tailgater was qualifying "
        "movers that cannot pay for themselves. paper-fill-simulator is "
        "unaffected either way: it charges Upstox's real stack for an Upstox "
        "fill and only falls back to this rate for a venue this project no "
        "longer has.",
    ),
    "policy_earned_size_per_competence": (
        "100000.0",
        "Claude, 2026-09-12: how much notional one unit of demonstrated "
        "competence earns. Was 1000.0 USDT. Set to minimum_capital_per_trade, "
        "so the first competence a bot earns buys exactly one tradeable trade on "
        "this desk rather than a fraction of one -- at the old figure, converted "
        "or not, an earned unit bought a size trade-capital-bounds-gate would "
        "refuse as below the minimum.",
    ),
}


# Settings already re-derived for the Indian market in earlier sessions, whose
# notes say so in prose but carry no marker a probe can read. Recorded here
# rather than re-derived: each is proved by an artifact that exists in the tree,
# or its value is literally an Indian identity that needs no measurement.
#
# This is NOT a way to make the count fall. Nothing here changes a value, every
# entry names its proof, and the test alongside asserts each named artifact is
# really on disk -- an entry whose evidence vanishes fails rather than passing
# quietly.
ALREADY_INDIAN: dict[str, str] = {
    # Proved by a measurement or an Indian model that exists in the tree.
    "signal_label_move_fraction": "measurements/2026-09-07-indian-price-staleness/",
    "options_flat_brokerage": "runtime/indian_options_fee_model.py",
    "instrument_maximum_cost_fraction": "measurements/2026-09-07-indian-price-staleness/",
    "bull_cold_start_minimum_prints": "measurements/2026-09-07-exit-plan-starvation/",
    "bear_cold_start_minimum_prints": "measurements/2026-09-07-exit-plan-starvation/",
    "maximum_decision_price_drift": "measurements/2026-09-05-why-upstox-never-fills/",
    "mean_reversion_minimum_volatility_fraction": "runtime/indian_options_fee_model.py",
    "per_side_trading_cost_fraction": "measurements/2026-09-07-indian-price-staleness/",
    "reference_price_materiality_fraction": "runtime/price_staleness.py",
    "reference_price_prior_one_second_move": (
        "measurements/2026-08-24-reference-price-staleness/measure_price_drift_by_age.py"
    ),
    # Proved by pointing at another setting that is itself proved.
    "limit_walk_maximum_total_fraction": "measurements/2026-09-05-why-upstox-never-fills/",
    "intent_timing_maximum_price_drift": "measurements/2026-09-05-why-upstox-never-fills/",
    "tail_crowding_order_flow_deviation_threshold": "runtime/underlying_open_interest.py",
    # Indian identities. The value itself is the market, so there is nothing to
    # measure -- "INR" is not a number fitted to anything.
    "segment_id": "the value names an Indian segment",
    "settlement_currency": "the value is INR",
    "segment_trading_venues": "the value names Upstox",
    "broker_feed_venue_id": "the value names Upstox",
    "upstox_candle_interval": "the value is an Upstox interval code",
}


# Settings whose only readers are parts that are off this spine, or that no code
# reads at all. Their values are INERT: nothing acts on them, so re-deriving a
# number for them would be inventing a figure for a decision nobody makes.
#
# A third state, deliberately not folded into "converted". A converted setting is
# right for this market; an inert one is simply not being asked. Calling the
# second the first would be the drift guard lying in the other direction -- and
# if one of these parts ever comes back on the spine, its setting is crypto again
# that day, which is what the note has to say.
INERT_WITH_THE_CRYPTO_PATH: dict[str, str] = {
    "cash_equity_shortlist_liquidity_pool_size": (
        "cash-equity-shortlist-ranker, whose segment cash-equity-intraday was "
        "retired on 2026-09-12. The part is still built and still on the spine, "
        "and with no cash-equity segment in built_segments it ranks nothing"
    ),
    "captured_symbol_count": "symbol-catalogue-reader, off the spine since the 2026-09-02 cutover",
    "symbol_selection_metric": "symbol-catalogue-reader, off the spine",
    "symbol_catalogue_refresh_interval": "symbol-catalogue-reader, off the spine",
    "stream_drain_interval": "ccxt-venue-reader and order-book-reader, both off the spine",
    "consolidated_price_maximum_quote_age": "cross-venue-price-consolidator, off the spine",
    "liquidation_fee_rate": "paper-liquidation-simulator, off the spine -- a bought option cannot be liquidated",
    "book_symbols_when_thinnable": "stream-budget-planner, off the spine",
    "liquidation_cascade_minimum_cluster_notional": "no code reads it",
    "funding_premium_clamp": "no code reads it",
    # whale_minimum_quote_value is read by nothing either, but it was already
    # given an Indian derivation earlier today (REFITS), and converted beats
    # inert: a setting that is right for this market stays right if its reader
    # comes back. Listing it in both is the conflict the overlap test catches.
}


def note_insertion_point(block: str) -> int | None:
    """Where a sentence may be appended inside this setting's note, or None.

    Handles both TOML string forms. A note written with a TRIPLE-quoted string
    is the trap: a regex for a single-quoted note matches the FIRST quote of the
    triple
    and reports the insertion point inside the delimiter, which produces a file
    tomllib refuses. `book_symbols_when_thinnable` is written that way, and it
    is what the parse-before-write guard caught on 2026-09-12 -- the guard
    working, and the reason it exists.
    """
    triple = re.search(r'note\s*=\s*"""', block)
    if triple is not None:
        closing = block.find('"""', triple.end())
        return closing if closing >= 0 else None
    single = re.search(r'note\s*=\s*"(.*)"', block, re.S)
    return single.end(1) if single is not None else None


# Crypto concepts whose part is on this spine but whose Indian answer is a
# different MECHANISM rather than a different number. Re-scaling these would be
# the worst outcome available: a plausible figure for a cost that does not exist,
# which looks derived and is fiction.
#
# Recorded, not changed, and deliberately NOT counted as converted -- each is a
# design question with a named answer, and the note says what that answer is so
# the next session does not have to rediscover it.
NEEDS_AN_INDIAN_MECHANISM: dict[str, str] = {
    "anomaly_disagreement_threshold": (
        "market-anomaly-detector calls the VENUES in disagreement when one venue's "
        "print sits this far from the consolidated price. It needs two venues "
        "pricing one instrument, and this project now has one: captured_venues is "
        "empty and every price comes from Upstox. There is no second opinion to "
        "disagree with, so the threshold has nothing to gate. The Indian analogue "
        "of 'two prices for one thing' is the option's own premium against the "
        "premium implied by its underlying and the surface -- implied-vol-reader "
        "and broker-option-greeks both already carry the pieces -- which is a "
        "different comparison needing its own measurement, not this fraction"
    ),
    "leverage_carry_tolerance_per_day": (
        "the most leverage-selector will pay per day to borrow before reducing "
        "leverage. Both built segments state leverage_ceiling 1.0 and "
        "cash-equity-intraday, the only segment that ever used leverage, was "
        "retired on 2026-09-12 -- so nothing borrows and nothing pays carry. Its "
        "own note already says it was 'carried over unchanged' from the crypto "
        "funding tolerance. A leveraged Indian segment's answer is Upstox's real "
        "MTF interest rate, which is a published schedule rather than a tolerance "
        "this project picks"
    ),
    "intraday_borrowing_daily_interest_rate": (
        "what the broker charges per day on the borrowed part of an intraday "
        "position. Zero is CORRECT for Upstox MIS -- interest is what the margin "
        "trading facility charges to carry overnight, a product this project "
        "deliberately does not use -- and its own note says it is NOT VERIFIED "
        "against Upstox's schedule. It is moot either way now: the segment that "
        "borrowed is retired. Verify against the schedule before any leveraged "
        "segment trades, rather than treating a zero nobody checked as measured"
    ),
    "stop_cluster_clearance_fraction": (
        "stop-cluster-clearance moves a stop clear of a LIQUIDATION cluster -- a "
        "crowd of leveraged positions that will be force-closed at one price. NSE "
        "has no such cluster for a bought option, because there is nothing to "
        "force-close. The Indian analogue of 'a price where a crowd is waiting' is "
        "open interest concentrated at a strike, which broker-open-interest already "
        "carries per contract; that is a different measurement on a different axis, "
        "not this fraction rescaled"
    ),
    "signal_bridge_deviation_threshold": (
        "its own note says it gates 'how far from its own normal a FUNDING RATE or "
        "transfer must sit'. Neither exists on NSE. cross-segment-signal-bridge was "
        "already pointed at open interest and order flow for the Indian build "
        "(2026-09-01), so the threshold belongs to whichever of those it now reads "
        "and has to be measured against that series rather than inherited from a "
        "funding rate's distribution"
    ),
    "bear_settlements_per_day": (
        "bear-setup-filter projects a carry cost over its horizon by multiplying a "
        "funding rate by settlements per day. A bought NSE option settles no funding "
        "at all; what it pays for being held is THETA, the decay of its own premium, "
        "which is a function of time to expiry and volatility rather than a rate per "
        "day. The Indian answer is to project theta from the option's own greeks -- "
        "broker-option-greeks already carries it -- not to pick a number of "
        "settlements. Left at 3.0 because the constructor refuses zero and a smaller "
        "positive number would be a quieter version of the same fiction"
    ),
    "maintenance_margin_rate": (
        "leverage-selector sizes against a liquidation distance. A bought option "
        "cannot be liquidated -- its worst case is the premium, already paid -- and "
        "both built segments state leverage_ceiling 1.0, so nothing on this spine "
        "asks for leverage at all since cash-equity-intraday was retired on "
        "2026-09-12. The Indian answer for a leveraged segment is the broker's own "
        "per-order margin (broker-margin-quoter, already built and carrying real "
        "Upstox quotes), never a flat rate"
    ),
    "leverage_target_liquidation_distance": (
        "the other half of the same question as maintenance_margin_rate, and the "
        "same answer: there is no liquidation price for a bought option, and no "
        "built segment uses leverage"
    ),
}


# Settings whose quantity genuinely does not depend on which market is traded.
# A bus ceiling is a bus ceiling; a count of a model's features is a count; the
# fraction of an account one trade may risk is the operator's policy and means
# the same in rupees as in dollars.
#
# These name crypto only because they were written during the crypto build and
# the note cites what was in front of the author at the time. Recorded rather
# than re-derived, because there is nothing to re-derive -- and recorded rather
# than ignored, because "nothing to do" has to be a stated answer or it is
# indistinguishable from "nobody looked" (Rule 8).
MARKET_INDEPENDENT: dict[str, str] = {
    "broker_reconnect_backoff_floor": (
        "the first wait after a dropped feed connection before doubling. A "
        "reconnection policy is about being a well-behaved client of whatever "
        "server dropped you, not about what that server prices. Its note already "
        "says it matches the crypto reader's starting point because no Upstox "
        "measurement exists -- and none is needed: one second before a first "
        "retry is polite on any socket, and the doubling is what adapts"
    ),
    "broker_account_funds_freshness": (
        "how old a funds reading may be before it is read again. A staleness "
        "budget for a REST poll, chosen against how fast an account's available "
        "margin can move and how hard the endpoint may be hit -- both properties "
        "of this project's own polling rather than of the instruments"
    ),
    "broker_price_frame_cadence_seconds": (
        "how often the price-level sampler republishes every instrument's latest "
        "price. A publish rate on this project's own bus. Note the direction it "
        "errs in is now generous rather than tight: four frames a second against "
        "an Indian option that prints 0.7 times a MINUTE "
        f"(measurements/2026-09-12-indian-feed-cadence/) means most frames restate an unchanged price, which is the "
        "cheap failure, and level publishing already skips an unchanged level"
    ),
    "price_gap_patience_multiple": (
        "how far past its own habit a symbol must be silent before the gap is "
        "real, as a multiple of that symbol's OWN measured p99. The multiple is "
        "the judgement; the p99 it multiplies is measured live and per symbol, "
        "so the market enters through the measurement rather than through this "
        "number. That is the whole design of an adaptive bound, and it is why "
        "this one did not need re-deriving when the floor beneath it did"
    ),
    "anomaly_feed_silence_patience_multiple": "the same adaptive-bound argument, for silence that reads as an anomaly",
    "feed_gap_patience_multiple": "the same adaptive-bound argument, for silence that reads as a feed gap",
    "spread_reversion_z_threshold": (
        "its own note says it is 'judgement anchored to a measurement rather than "
        "measured directly: under a normal distribution two standard deviations is "
        "the outer 5%'. That is a property of the normal distribution, which does "
        "not change market. The note's second half -- that fat tails make it fire "
        "more often than 5% -- is if anything MORE true of Indian options than of "
        "crypto perpetuals, and in the same direction, so the choice stands"
    ),
    "risk_maximum_drawdown_fraction": (
        "how far realised equity may fall from its high-water mark before the "
        "drawdown breaker zeroes the risk limit. Ten percent of an allotment is "
        "ten percent whatever the allotment is denominated in; its note names USDT "
        "only because it worked the fraction through against the old 10,000 USDT "
        "paper balance. The segments now hold 75 lakh each and a tenth of that is "
        "7.5 lakh, computed from the same fraction"
    ),
    "closed_trades_trustworthy_after_ns": (
        "a boundary in time, marking which recorded trades came from a run whose "
        "prices the market never printed. When that moment was has nothing to do "
        "with which market printed afterwards"
    ),
    "bull_opinion_maximum_missing_features": (
        "a count of features, set from the model's own feature list rather than "
        "from judgement -- its note says so. It changes when the model's features "
        "change, not when the market does"
    ),
    "bear_opinion_maximum_missing_features": "the short side's mirror of the same count",
    "risk_maximum_per_position_fraction": (
        "the fraction of an allotment one position may risk. The operator's policy, "
        "and one percent means the same thing in rupees as in dollars"
    ),
    "risk_maximum_total_fraction": (
        "five times the single-position limit by construction, so it follows that "
        "policy rather than any market"
    ),
    "miner_perfect_fit_threshold": (
        "the fitted hit rate above which a readable formula is refused as too good "
        "to be real. A statement about overfitting, which is a property of fitting "
        "rather than of the instrument fitted"
    ),
    "reference_price_move_anchor_seconds": (
        "two prints a millisecond apart differ by the tick rather than by "
        "volatility, so a move needs an anchor at least this old. A fact about "
        "measuring a series, true of any series"
    ),
    "reference_price_minimum_age_seconds": (
        "the floor on a learned bound, set at the anchor spacing: below it the "
        "estimate is extrapolating inside its own resolution. Again a property of "
        "the estimator, not the market"
    ),
    "price_frame_maximum_symbols": (
        "how many symbols fit in one frame before the 131,072-byte bus ceiling is "
        "reached, measured by serialising real frames at 50.1 bytes per symbol. A "
        "capacity of this project's own bus"
    ),
    "broker_price_frame_maximum_symbols": "the same bus ceiling, for the broker feed's own frame",
    "broker_stream_drain_interval": (
        "the longest a part may block on one socket read before yielding to its "
        "tick loop -- T-2 tick discipline, which is about this runtime rather than "
        "about any venue"
    ),
    "anomaly_basis_window_observations": (
        "how many of a symbol's own prints a normal basis is learned from. A "
        "sample size for an estimator, chosen for statistical stability"
    ),
}


# Settings a measurement was attempted for, where the evidence came back too
# thin to re-derive from. **These stay counted as outstanding**: the note records
# what was measured and what would settle it, and no marker is written that the
# guard skips. Recording the attempt is worth doing -- the next session should
# not repeat a measurement that did not work -- but it is not progress and must
# not read as any.
MEASURED_BUT_INCONCLUSIVE: dict[str, str] = {
    "cointegration_minimum_correlation": (
        "measured on 30 NSE share series from 2026-09-08, 435 pairs at the 256 "
        "window: median |correlation| 0.367, q75 0.575, q90 0.701. The crypto "
        "figure of 0.5 was chosen as 'about the median pair on bybit-linear (q50 "
        "0.56)', and on this Indian sample 0.5 sits nearer the 70th percentile "
        "than the median -- so keeping it is a TIGHTER filter here than it was "
        "there, not a looser one. Not changed on this evidence: one session and "
        "thirty symbols is too thin to move a threshold that decides what the "
        "pair finder spends its budget on, and loosening it toward the Indian "
        "median would widen a sweep that already grows with the SQUARE of a "
        "1,980-symbol universe. What would settle it: the same measurement "
        "across several sessions and the full F&O underlying list"
    ),
    "cointegration_minimum_reversion_strength": (
        "measured alongside the correlation above and the result was NOT USABLE: "
        "median reversion per step came out 0.0000 at every window tried. Print-"
        "to-print is the wrong granularity for it on this market -- an NSE share "
        "barely moves between consecutive prints (median step 0.037%), so the "
        "deviation ratio the estimator forms is dominated by rounding rather than "
        "by any pull back toward the mean. What would settle it: a spread half-"
        "life fitted on time-spaced bars rather than on prints, across several "
        "sessions"
    ),
    "cointegration_window_length": (
        "the window the two thresholds above are measured AT, so it cannot be "
        "re-derived before they are. The crypto figure of 256 was chosen because "
        "reversion was three to five times stronger there than at 1024; the "
        "Indian reversion measurement did not work (see "
        "cointegration_minimum_reversion_strength), so the comparison that chose "
        "256 cannot yet be repeated here. Correlation alone rose slightly with "
        "window on this sample -- median 0.301 at 128, 0.367 at 256, 0.322 at "
        "512 -- which is not a strong enough signal to move it on"
    ),
}


def record_measured_but_inconclusive(text: str, name: str, what: str) -> tuple[str, str]:
    """Record a measurement that did not settle the value. Stays outstanding."""
    start = text.find(f"[{name}]")
    if start < 0:
        return text, "ABSENT"
    end = text.find("\n[", start + 1)
    block = text[start:end if end > 0 else len(text)]
    if INCONCLUSIVE_MARKER in block:
        return text, "already recorded"
    at = note_insertion_point(block)
    if at is None:
        return text, "NO NOTE"
    addition = (
        f" {INCONCLUSIVE_MARKER}: an Indian measurement was attempted and did not "
        f"settle this -- {what}. The value is unchanged and this setting REMAINS "
        f"outstanding; the record exists so the next attempt starts from what was "
        f"already tried rather than repeating it."
    )
    block = block[:at] + addition.replace('"', "'") + block[at:]
    return text[:start] + block + text[(end if end > 0 else len(text)):], "recorded"


def record_market_independent(text: str, name: str, why: str) -> tuple[str, str]:
    """Record that a setting's quantity does not depend on the market."""
    start = text.find(f"[{name}]")
    if start < 0:
        return text, "ABSENT"
    end = text.find("\n[", start + 1)
    block = text[start:end if end > 0 else len(text)]
    if INDEPENDENT_MARKER in block:
        return text, "already recorded"
    at = note_insertion_point(block)
    if at is None:
        return text, "NO NOTE"
    addition = (
        f" {INDEPENDENT_MARKER}: this quantity does not depend on which market is "
        f"traded -- {why}. It names crypto only because it was written during the "
        f"crypto build and its note cites what was in front of the author then. "
        f"Nothing to re-derive; recorded so that is a stated answer rather than a "
        f"gap nobody looked at."
    )
    block = block[:at] + addition.replace('"', "'") + block[at:]
    return text[:start] + block + text[(end if end > 0 else len(text)):], "recorded"


def record_needs_a_mechanism(text: str, name: str, why: str) -> tuple[str, str]:
    """Record that a crypto concept needs an Indian mechanism, not a new number."""
    start = text.find(f"[{name}]")
    if start < 0:
        return text, "ABSENT"
    end = text.find("\n[", start + 1)
    block = text[start:end if end > 0 else len(text)]
    if MECHANISM_MARKER in block:
        return text, "already recorded"
    at = note_insertion_point(block)
    if at is None:
        return text, "NO NOTE"
    addition = (
        f" {MECHANISM_MARKER}: this is a crypto concept and its Indian answer is a "
        f"different mechanism, not a different number -- {why}. The value is left "
        f"alone on purpose: re-scaling it would produce a plausible figure for a "
        f"cost that does not exist here, which looks derived and is fiction."
    )
    block = block[:at] + addition.replace('"', "'") + block[at:]
    return text[:start] + block + text[(end if end > 0 else len(text)):], "recorded"


def record_inert(text: str, name: str, reader: str) -> tuple[str, str]:
    """Record that nothing on this spine reads a setting. No value changes."""
    start = text.find(f"[{name}]")
    if start < 0:
        return text, "ABSENT"
    end = text.find("\n[", start + 1)
    block = text[start:end if end > 0 else len(text)]
    if INERT_MARKER in block:
        return text, "already recorded"
    at = note_insertion_point(block)
    if at is None:
        return text, "NO NOTE"
    addition = (
        f" {INERT_MARKER}: nothing on this spine reads this -- {reader}. The value is "
        f"still the crypto one and is left alone deliberately: re-deriving a number for "
        f"a decision nobody makes would be inventing a figure. If that part is ever put "
        f"back on the spine this is crypto again that day, and has to be derived then."
    )
    block = block[:at] + addition.replace('"', "'") + block[at:]
    return text[:start] + block + text[(end if end > 0 else len(text)):], "recorded"


def record_already_indian(text: str, name: str, proof: str) -> tuple[str, str]:
    """Append the marker to a setting already derived for India. No value changes."""
    start = text.find(f"[{name}]")
    if start < 0:
        return text, "ABSENT"
    end = text.find("\n[", start + 1)
    block = text[start:end if end > 0 else len(text)]
    if CONVERTED_MARKER in block:
        return text, "already recorded"
    at = note_insertion_point(block)
    if at is None:
        return text, "NO NOTE"
    addition = (
        f" {CONVERTED_MARKER}: already derived for the Indian market in an earlier "
        f"session; this records it so a probe can tell a converted setting from one "
        f"still fitted to crypto. Proof: {proof}. No value was changed."
    )
    block = block[:at] + addition.replace('"', "'") + block[at:]
    return text[:start] + block + text[(end if end > 0 else len(text)):], "recorded"


def back_up(path: pathlib.Path) -> pathlib.Path:
    destination = path.with_name(
        f"{path.name}.before-indian-refit-{time.strftime('%Y-%m-%dT%H%M%S')}"
    )
    shutil.copy2(path, destination)
    return destination


def refit(
    text: str, name: str, new_value: str, note: str, new_unit: str | None = None,
) -> tuple[str, str]:
    """Replace one setting's value and append its provenance. Returns (text, what happened)."""
    start = text.find(f"[{name}]")
    if start < 0:
        return text, "ABSENT"
    end = text.find("\n[", start + 1)
    block = text[start:end if end > 0 else len(text)]

    if CONVERTED_MARKER in block:
        return text, "already recorded"
    current = re.search(r"^value\s*=\s*(.+)$", block, re.M)
    if current is None:
        return text, "NO VALUE LINE"

    # **A value that already matches still has to be recorded.** Returning
    # "already Indian" on an equal value was a real bug (2026-09-12):
    # whale_minimum_quote_value's whole point is that the DIGITS are unchanged
    # and the meaning is not -- 1,000,000 USDT is about eight and a half crore,
    # 1,000,000 rupees is the top of the observed print distribution -- and it
    # was the one setting that silently kept its crypto provenance because of
    # it. The marker is what a probe reads, so skipping it leaves the setting
    # counted as drift forever.
    was = current.group(1).strip()
    updated = block if was == new_value.strip() else (
        block[:current.start(1)] + new_value + block[current.end(1):]
    )

    if new_unit is not None:
        unit_line = re.search(r'^unit\s*=\s*"(.*)"$', updated, re.M)
        if unit_line is not None and unit_line.group(1) != new_unit:
            was = f"{was} {unit_line.group(1)}"
            updated = updated[:unit_line.start(1)] + new_unit + updated[unit_line.end(1):]

    at = note_insertion_point(updated)
    if at is not None:
        addition = f" {CONVERTED_MARKER}, was {was}. {note}"
        updated = updated[:at] + addition.replace('"', "'") + updated[at:]
    return text[:start] + updated + text[(end if end > 0 else len(text)):], f"{was} -> {new_value}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write. Without it, report only.")
    arguments = parser.parse_args()

    if not SETTINGS.exists():
        print(f"no settings at {SETTINGS}", file=sys.stderr)
        return 2
    text = SETTINGS.read_text()
    if arguments.apply:
        print(f"backed up to {back_up(SETTINGS)}")
    else:
        print("DRY RUN -- nothing will be written. Pass --apply to do it.")

    changed = 0
    for name, what in MEASURED_BUT_INCONCLUSIVE.items():
        text, done = record_measured_but_inconclusive(text, name, what)
        print(f"  {name:46} inconclusive: {done}")
        if done == "recorded":
            changed += 1
    for name, why in MARKET_INDEPENDENT.items():
        text, what = record_market_independent(text, name, why)
        print(f"  {name:46} independent: {what}")
        if what == "recorded":
            changed += 1
    for name, why in NEEDS_AN_INDIAN_MECHANISM.items():
        text, what = record_needs_a_mechanism(text, name, why)
        print(f"  {name:46} mechanism: {what}")
        if what == "recorded":
            changed += 1
    for name, reader in INERT_WITH_THE_CRYPTO_PATH.items():
        text, what = record_inert(text, name, reader)
        print(f"  {name:46} inert: {what}")
        if what == "recorded":
            changed += 1
    for name, proof in ALREADY_INDIAN.items():
        text, what = record_already_indian(text, name, proof)
        print(f"  {name:46} {what}")
        if what == "recorded":
            changed += 1
    for name, entry in REFITS.items():
        value, note = entry[0], entry[1]
        new_unit = entry[2] if len(entry) > 2 else None
        text, what = refit(text, name, value, note, new_unit)
        print(f"  {name:46} {what}")
        if "->" in what:
            changed += 1
    if arguments.apply and changed:
        # **Parse before writing.** A provenance note is prose going into a TOML
        # string, and prose contains quotes and backslashes. An ad-hoc edit to
        # one of these notes on 2026-09-12 put `\'` into a basic string, TOML
        # refused the whole file, and the spine could not start at all -- every
        # part down, because of a comment. The settings file is the one input
        # every part reads, so a writer that cannot prove its output parses has
        # no business writing it.
        import tomllib

        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as failure:
            print(
                f"REFUSED: the result would not be valid TOML ({failure}). "
                f"Nothing written; the settings file is untouched.",
                file=sys.stderr,
            )
            return 2
        SETTINGS.write_text(text)
    print(f"\n{changed} setting(s) {'refitted' if arguments.apply else 'would be refitted'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
