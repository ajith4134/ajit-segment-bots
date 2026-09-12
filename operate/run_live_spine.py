"""Run the live spine: the parts that must be on for the system to learn and trade.

    .venv/bin/python operate/run_live_spine.py --list
    .venv/bin/python operate/run_live_spine.py

**This is an operations entry point, not a part** (RL-069 is about the substrate;
this is about the operator). It stands in for the resource governor deciding what
runs, and it says so rather than pretending otherwise: `switching-planner` will
choose the on-set from measured pressure, and when it does this script is deleted.

What it owns until then:

- the launcher, and therefore every part's control socket;
- the switch endpoint, so `gate-actuator` can switch parts even though nothing is
  planning switches yet;
- restarting a part that exits, and **writing down that it did** -- a part that
  died and was quietly restarted is a hole in the record of what the system was
  doing when it made a decision;
- refusing to start when `operate/start_trade_capture.py` is already running,
  because `venue-trade-stream-reader` writes the same tape files and two writers
  on one tape make a duplicate indistinguishable from a real second print.

**Live prices, never a replay (RL-071).** The feed parts here open sockets to the
venues. The tape is written as it always was -- by the part now, instead of by the
capture script -- and nothing in this path reads it back.

The clock this starts is the model's. `signal-outcome-labeller` scores each
detector's claim against the prices that arrive next, and the conviction model
becomes measured only after `bull_minimum_training_observations` of them. Those
accrue in real time and cannot be caught up on later, which is the same reason the
tape was started the day it became possible.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

PROJECT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

# Before anything imports numpy, which the tape does.
from runtime.forkserver_launcher import apply_blas_thread_caps  # noqa: E402

apply_blas_thread_caps()

from runtime.part_launcher import PartLauncher  # noqa: E402
from runtime.scope_placer import ScopeLimits  # noqa: E402
from runtime.secrets_reader import read_upstox_env  # noqa: E402
from runtime.settings_reader import load_settings_document, settings_directory  # noqa: E402
from runtime.switch_service import ACTION_TURN_OFF, OUTCOME_FLIPPED  # noqa: E402
from runtime.wiring_plan import derive_wiring  # noqa: E402

STATE_DIRECTORY = pathlib.Path.home() / ".local/share/ajit-segment-bots"
SUPERVISOR_LOG = STATE_DIRECTORY / "live-spine.jsonl"
LOCK_FILE = STATE_DIRECTORY / "live-spine.lock"

# The capture script this replaces. Both write the same tape files.
CAPTURE_SCRIPT_NAME = "operate/start_trade_capture.py"

# How often the supervisor looks at what it started. Not a decision about the
# system -- it is how long a dead part may go unnoticed, and every part reports its
# own health far more often than this.
SUPERVISION_INTERVAL_SECONDS = 2.0

# How long to wait before restarting a part that exited, and how far that grows.
# A part that dies on startup would otherwise be restarted as fast as the loop can
# fork, which turns one broken part into a busy machine.
RESTART_BACKOFF_FLOOR_SECONDS = 1.0
RESTART_BACKOFF_CEILING_SECONDS = 60.0

SWITCH_ENDPOINT_BACKLOG = 16

# The spine, in the order it is started: a part is started after the parts whose
# output it reads, so its first tick has something in its inbox rather than nothing.
# Comments say what each one is here for, because a list of part ids is a list of
# decisions and the decisions are the point.
LIVE_SPINE = (
    # First, so the earliest health reports have an inbox to land in: it consumes
    # part-health from every other part here, and a board reads the table it
    # writes. Until it was on the spine (2026-08-23) nothing consumed the
    # staleness or input loss every part had been reporting.
    "heartbeat-collector",
    # The governor's meters, complete since 2026-08-24 (RL-068: the governor
    # spine before the futures vertical). These measure the machine and the parts
    # on it and feed the planner; none of them reads the market, so they start
    # before the feed. The deciding half -- duty-cycle-planner, switching-planner,
    # off-state-verifier and gate-actuator -- starts after the feed, below.
    "hardware-scanner",
    "part-priority-reader",
    "part-appetite-meter",
    "hog-detector",
    "io-pressure-meter",
    "memory-pressure-forecaster",
    "failing-part-detector",
    "part-restart-budgeter",
    "switch-oscillation-damper",
    "resource-reservation-ledger",
    "accelerator-scheduler",
    # The feed, cut over from crypto to Indian 2026-09-02 (goal.md: crypto is
    # retired, not a parallel track). The old three -- symbol-catalogue-reader,
    # stream-budget-planner, venue-trade-stream-reader -- and the rest of the
    # Binance/Bybit venue-adapter cluster (ban-signal-detector, venue-quote/
    # premium-stream-reader, ccxt-venue-reader, cross-venue-price-consolidator,
    # venue-pool-rotator, ccxt-order-router, venue-rate-budgeter, venue-order-
    # status-translator, venue-balance-reader, venue-position-reader, venue-
    # outage-rider, order-book-reader, api-key-pool-rotator, order-state-poller,
    # order-reject-classifier, order-not-found-debouncer, order-resubmitter,
    # clock-skew-monitor) are off this spine now, not deleted -- still declared
    # in docs/features.json, same standing as leverage-selector, for the crypto
    # goal in git history rather than a live one. Their crypto-margin-only
    # downstream (funding-rate-forecaster, liquidation-cluster-mapper, paper-
    # liquidation-simulator, funding-settlement-recorder, leverage-selector,
    # liquidation-price-tracker, margin-liquidation-watch) is off too, for the
    # same reason the CLOSED findings in the crypto-retirement audit gave: a
    # bought option has no leverage dial and cannot be liquidated.
    #
    # broker-token-refresh-scheduler auto-refreshes today's Upstox token
    # (upstox-totp, no human in the loop); broker-instrument-catalogue-reader
    # needs no token (Upstox's instrument files are public). Both start with
    # nothing else running, same as the crypto readers they replace.
    "broker-token-refresh-scheduler",
    "broker-instrument-catalogue-reader",
    # The live feed itself, needing the token and the catalogue above. Writes
    # broker-market-data, broker-candle, broker-order-book-snapshot, broker-
    # open-interest and broker-option-greeks -- the tape writer and every
    # bridge below read from this one part, the same shape as venue-trade-
    # stream-reader writing market-data for every crypto reader that followed.
    "broker-market-feed-reader",
    # The master, narrowed to what that feed actually subscribed. Everything
    # below joins a listing to an LTP, a greek, an open-interest reading or a
    # depth snapshot, and none of those exist for an instrument nobody
    # subscribed -- so ten parts were draining all 102,940 rows to use 2,000 of
    # them, and 13.6% of the catalogue's deliveries were being dropped for want
    # of inbox room (measured 2026-09-05). After the feed reader, because the
    # subscription is the feed reader's own statement about itself.
    "subscribed-instrument-listing-filter",
    "broker-market-tape-writer",
    "broker-price-level-sampler",
    "broker-account-funds-reader",
    # The bridges: republish the broker's own types under the crypto-era
    # names ~40 detector/execution/prediction parts already read, so nothing
    # downstream had to be rewritten to trade a real Indian instrument.
    # broker-market-data-bridge -> market-data (paper-fill-simulator and
    # ~30 others), broker-order-book-bridge -> order-book-snapshot (book-walk
    # pricing, tick-size-resolver, the feature builders), broker-underlying-
    # price-frame-bridge -> symbol-price-frame (regime-classifier, mean-
    # reversion-detector, cointegration-pair-finder and the rest, unblocked
    # with zero code changes to any of them 2026-09-01), broker-candle-bridge
    # -> candle (kline-window-builder and the real conviction-model chain --
    # kronos-forecaster, bull/bear-conviction-model -- built 2026-09-02
    # specifically so this cutover would not take them to zero input).
    # broker-quote-bridge -> market-quote (2026-09-06): its only producer in
    # the blueprint was the crypto venue-quote-stream-reader, so
    # quote-level-sampler had received nothing ever and symbol-quote-frame had
    # never been produced -- which took instrument-selector's *fallback* away.
    # A quote is what that part prices a symbol from when the last trade is too
    # old to believe, and an Indian option that has not printed for minutes
    # while carrying a live bid and ask is the ordinary case, not the corner.
    "broker-market-data-bridge",
    "broker-order-book-bridge",
    "broker-underlying-price-frame-bridge",
    "broker-candle-bridge",
    "broker-quote-bridge",
    # The fifth bridge, and the one whose absence nothing reported: since the
    # cutover took symbol-catalogue-reader off this spine, NOTHING produced
    # symbol-universe. Eleven parts read it; seven of them were running and
    # starved, and universal-symbol-sweeper had swept an empty list 3,114 times
    # with every skip counter reading 0 -- so no entry-candidate, no
    # bull-side-candidate, no feature vector, no conviction, no intent, no
    # order. It is placed after broker-underlying-price-frame-bridge only for
    # reading order; it takes its underlyings' prices from broker-price-frame,
    # which broker-price-level-sampler above already publishes. Its equity
    # branch is held back until cash-equity-shortlist-ranker above has spoken
    # at least once (2026-09-05) -- publishing all 2,444 eligible shares while
    # waiting would be exactly the uncapped universe the shortlist exists to
    # replace.
    "broker-symbol-universe-bridge",
    # What the broker will lend against each instrument in that universe, after
    # the bridge that publishes it and after the feed that prices it. Bot 3's
    # leverage has no other source: `intraday_margin` is absent from every one
    # of the 102,940 rows of Upstox's instrument master, so the only place that
    # knows is the margin endpoint, and it has to be asked.
    "broker-margin-quoter",
    # Whether the market is open, before the one part whose whole behaviour
    # turns on that answer. It sat forty lines further down until 2026-09-05,
    # so broker-history-reader started with no market-session-state at all and
    # decided whether to fetch history against a level nobody had published
    # yet -- the ordering test had been failing on exactly this since the
    # 2026-09-02 cutover, and a permanently red ordering test hides the next
    # real inversion it exists to catch.
    "market-session-calendar",
    # Prices for the hours the market is shut (2026-09-02). It fetches only
    # while market-session-state says the session is not open, so it never
    # stands in for a market it could be reading; it publishes `candle` and
    # `market-data` and deliberately not `broker-candle`, so the tape stays a
    # record of live capture only.
    "broker-history-reader",
    # Cash-equity's real top-50 shortlist (2026-09-05): the operator asked for
    # the day's actual opportunities -- momentum, volume, 52-week range, gap,
    # VWAP deviation, ATR-normalised move -- instead of the bridge above
    # publishing all 2,444 eligible ordinary shares unranked and uncapped.
    # equity-opportunity-profiler fetches each candidate's own 52-week/ATR/
    # volume history from Upstox (needs the token and the catalogue above);
    # cash-equity-shortlist-ranker blends that with what liquidity-grader,
    # broker-price-level-sampler and broker-candle-bridge already publish.
    # Placed after broker-history-reader specifically: both read `candle`,
    # which broker-candle-bridge and broker-history-reader both produce, and
    # the two bridges/feed/ranker/bridge already sit in one cyclic component
    # (broker-market-feed-reader <-> subscribed-instrument-listing-filter and
    # the bridges beside it) where the ordering rule does not apply between
    # members -- broker-history-reader is the one `candle` producer outside
    # that cycle, so this is the position that satisfies it.
    "equity-opportunity-profiler",
    "cash-equity-shortlist-ranker",
    # The hard channel of stock-market-news-data (2026-09-02): the facts a part
    # refuses on rather than weighs, read from NSE's own public files. The two
    # readers come before the parts that hold their levels, so the first level
    # is published from a real fetch rather than from nothing.
    #
    # Each closes a failure that had no other guard: an order on an F&O-banned
    # name is rejected by the exchange and order-resubmitter would retry a
    # rejection that is not transient; paper-fill-simulator filled overnight
    # orders at the 15:29 price and journalled them as trades; and an
    # unadjusted 1:1 bonus reaches kline-window-builder as a -50% candle that
    # every detector fires on.
    "trading-restriction-reader",
    "instrument-restriction-state",
    "corporate-action-reader",
    "corporate-action-adjuster",
    # The head of the news feature, started 2026-09-12. Until that day
    # `stock-market-news-data` had 29 parts declared, 5 running, and **nothing
    # produced `raw-news-item` at all** -- so the fourteen parts below a source
    # (the deduplicator, the structurer, the symbol resolver, the sentiment
    # model, the impact forecaster) were starved at the top of the chain and
    # building any of them first would have measured nothing.
    #
    # After broker-instrument-catalogue-reader, whose listings say what there is
    # to ask news about, and after broker-token-refresh-scheduler, because
    # Upstox's News API is authenticated. Read-only: it fetches and publishes
    # what the broker said, and reads nothing it fetches.
    "broker-news-reader",
    # The chain below that source, started 2026-09-12. Until this day
    # `raw-news-item` had a producer and **no consumer at all** -- measured on
    # the live spine, 7,580 items published to nobody. Order matters: the
    # deduplicator is what stops a rolling backlog (167.1 hours of it, measured)
    # being restated as breaking news every sixteen minutes, so it comes first;
    # the tape writer is next because a story is not re-fetchable and an hour of
    # news not captured is gone; the meter and the monitor are measurement and
    # can follow.
    "news-item-deduplicator",
    "news-tape-writer",
    "news-latency-meter",
    "news-source-health-monitor",
    # The governor's deciding half, acting since 2026-08-24. duty-cycle-planner
    # counts market activity per UTC hour, so it starts after the reader; the
    # switching-planner weighs all fourteen inputs into a switch-plan; and
    # gate-actuator -- the one part ever handed the switch endpoint -- carries it
    # out, started last so the first plan it acts on was built with every meter
    # already reporting. What made turning the actuator on safe is
    # part-priority.toml: the feed reader, the sampler, the recorders and the
    # collector hold the first ten ranks, resource-reservation-ledger gives each a
    # guaranteed floor, and the planner never switches a reserved part off for
    # memory, hog or io reasons -- so the plan that could have cost an hour of
    # tape is a plan the policy cannot produce. Every governor part reports its
    # standing, so a flip that happened is on the board, and one that did not is
    # too.
    "duty-cycle-planner",
    "switching-planner",
    "off-state-verifier",
    "gate-actuator",
    # Every symbol's latest price, published four times a second as one frame per
    # venue. Thirty-seven parts read this instead of every trade, which is what
    # stops fan-out scaling with trading volume -- 12,707 deliveries a second
    # measured on 2026-08-23, against 148 for the same parts on frames. Without it
    # running, every one of those parts has an input nobody produces and sits
    # there looking perfectly healthy, which is the failure this whole phase is
    # about (docs/proposals/sampled-price-levels-and-a-governor-that-acts.md).
    "price-level-sampler",
    # The same argument on a louder feed: quotes arrive five times faster than
    # trades, so publishing per update would re-create the fan-out that sampling
    # the trade feed removed. Started beside the price sampler and on the same
    # cadence -- a reader comparing a price against a quote must not be handed one
    # of them sampled more often than the other.
    "quote-level-sampler",
    # Three parts moved up here 2026-09-02, cutting the crypto venue-adapter
    # cluster: each needs only market-data/order-book-snapshot/symbol-price-
    # frame, satisfied as of the bridges above, and each was declared far
    # later in the file with a consumer sitting earlier than it -- a real,
    # pre-existing ordering gap the crypto cluster's spurious cycle (an
    # accidental cycle through unrelated crypto-only types) had been masking,
    # not something this cutover introduced. liquidity-grader feeds
    # instrument-selector; correlation-cluster-mapper feeds exposure-limiter
    # and trade-cluster-detector; ground-truth-snapshot-builder feeds
    # decision-quality-critic, devils-advocate, premortem-writer, intent-
    # explainer, setup-second-opinion-reasoner and market-thesis-reasoner.
    "liquidity-grader",
    "correlation-cluster-mapper",
    "ground-truth-snapshot-builder",
    # ---- phase 6a: what the forecasts are built from, 2026-08-25 -----------
    # Placed here, with the samplers, because everything downstream reads them:
    # bull-feature-builder wants the funding forecast, instrument-selector wants
    # the implied-vol surface, and luck-skill-separator cannot tell luck from
    # skill without knowing how much the market was moving.
    #
    # Volatility is the one quantity in trading that is genuinely forecastable --
    # returns are close to unpredictable and their magnitude is not, because
    # volatility clusters. That is why this is a regression and not a
    # classification: sizing needs a number, not a direction.
    # The candle feed. Without it every part below has an empty inbox, because
    # kline-window-builder filters market-data for candles and the trade reader
    # publishes trades -- so the whole prediction chain sat running and idle on
    # 2026-08-25 with nothing to build a window from.
    #
    # Candles now come from broker-candle-bridge above, cut over 2026-09-02 --
    # ccxt-venue-reader (and the crypto book/venue-pool/api-key parts beside
    # it) are off, not deleted, same standing as the rest of the crypto
    # venue-adapter cluster. broker-order-book-bridge (also above) plays
    # order-book-reader's old role.
    "feed-gap-detector",
    "feed-jump-detector",
    "feed-coverage-auditor",
    "kline-window-builder",
    "implied-vol-reader",
    "order-flow-state-encoder",
    "flow-entropy-meter",
    "volatility-feature-builder",
    "realised-vol-regressor",
    "entropy-magnitude-forecaster",
    # liquidation-cluster-mapper is off too: liquidation-map is a crypto-
    # margin concept (a pool of forced-exit orders at a leverage level), and
    # stop-target-placer already degrades gracefully without one -- verified
    # 2026-09-01, same CLOSED shape as leverage-selector below.
    # Noticing. The only path in the blueprint from market-data to an
    # entry-candidate without a playbook-rule, which the learning loop cannot build
    # until trades have happened.
    "regime-classifier",
    "cointegration-pair-finder",
    "spread-reversion-detector",
    # Learning. This is the part that makes everything after it possible: it turns
    # a detector's claim plus the prices that follow into a training-label, with no
    # trade required (docs/proposals/signal-outcome-labelling.md).
    "signal-outcome-labeller",
    # What the labeller measured on the way, turned into the two profiles an exit
    # plan cannot be built without. They exist because the closed-trade profilers
    # cannot run until a trade has closed, and a trade cannot be opened without a
    # stop (docs/proposals/live-excursion-and-horizon-profiling.md). They read the
    # labeller's own labels rather than the feed: tracking claims is the labeller's
    # hot loop and doing it three times would be three times the cost for the same
    # numbers.
    "signal-excursion-profiler",
    "signal-horizon-profiler",
    # The bull bot. Forms no opinion at all until the conviction model is trained,
    # which is correct and is why the labeller runs beside it.
    "bull-setup-filter",
    "bull-feature-builder",
    "bull-outlier-rejector",
    "bull-conviction-model",
    "bull-conviction-calibrator",
    "bull-entry-timer",
    "bull-exit-plan-proposer",
    "bull-opinion-composer",
    # The bear half, started 2026-08-25 (phase 9). The same shape as the bull
    # bot and the opposite side, and the first time opinion-arbiter has two
    # opinions to resolve rather than one to apply its sole-opinion penalty to.
    # Ordered as the bull bot is: the weight learner and the setup filter first,
    # because a candidate no filter has weighted is a candidate nothing has an
    # opinion about, then features, then conviction, then the plan.
    "bear-setup-weight-learner",
    "bear-setup-filter",
    "bear-feature-builder",
    "bear-outlier-rejector",
    "bear-conviction-model",
    "bear-conviction-calibrator",
    "bear-entry-timer",
    "bear-exit-plan-proposer",
    "bear-opinion-composer",
    # Watches an open position for the thesis that opened it breaking. It reads
    # positions rather than the bot's own state, so it is started with the bot
    # rather than with the trading half.
    "bear-position-invalidation-watcher",
    # The third bot, started 2026-08-25 (phase 10): it follows a move already
    # under way instead of predicting one. Three sources of a follow candidate --
    # this system's own winners, a qualified mover off the scanner, and a copied
    # external position -- and one detector whose whole job is to refuse the ones
    # everybody is already in.
    "tail-setup-weight-learner",
    "tail-mover-qualifier",
    "tail-winner-selector",
    "leaderboard-reader",
    "onchain-position-reader",
    "copy-latency-estimator",
    "tail-copy-selector",
    "tail-move-remaining-estimator",
    "tail-crowding-detector",
    "tail-follow-conviction-model",
    "tail-trailing-exit-planner",
    "tail-opinion-composer",
    # The decision. One bot means one opinion, and the arbiter's sole-opinion
    # penalty is what says so in the intent rather than the intent pretending
    # three bots agreed.
    "opinion-arbiter",
    # How large this intent should be relative to a normal one, from the bots'
    # own calibrated conviction and how many of them agreed. Added to the spine
    # 2026-08-25: position-sizer has declared this input since the trading half
    # was wired and nothing has ever produced one, so every trade so far was
    # sized at the full risk budget whatever the conviction behind it.
    "size-hint-writer",
    # The settings the money comes from, read before anything is sized against
    # them. The validator is what the bounds gate refuses without: a bound checked
    # against settings nobody verified is a bound with no authority behind it.
    "main-account-settings-reader",
    "capital-allotment-reader",
    "capital-settings-validator",
    # The journal of what changed, started with the readers rather than with the
    # desk: it writes journal-entry, and the decoders read journal entries. RL-055
    # makes this journal the only source for the board's "when did this last
    # change", so it must be up before anything reads one.
    "capital-settings-change-recorder",
    # Before inr-pnl-accountant, which states every result in USDT (RL-028)
    # and needs the rate to convert a non-USDT quote at all.
    "paper-currency-converter",
    "money-mode-reader",
    # Which instrument carries the intent, and at what price and increment. The
    # selector prices the venue's own funding against the intent's horizon, which
    # is why symbol-catalogue-reader above has to be running.
    "instrument-selector",
    "tick-size-resolver",
    "exposure-limiter",
    # Where the exits go, decided before the entry is ever sent. Risk's own stop,
    # capped and moved clear of liquidation pools, and the target that closes the
    # trade in profit. It runs before the sizer because the sizer sizes the trade
    # against the distance to that stop -- a position sized without one is a
    # position whose risk nobody computed.
    "stop-target-placer",
    # The size, and the two bounds it must survive: what one trade may risk and
    # what one trade may commit.
    "paper-account-keeper",
    "position-sizer",
    "trade-capital-bounds-gate",
    # The order. Stamped with the id it keeps forever, addressed by the money
    # mode, and filled by the paper book -- which is the only destination this
    # phase may reach.
    "order-idempotency-stamper",
    "order-destination-router",
    # The three parts that make a paper fill resemble a live one, started
    # 2026-08-25: a fill priced by walking the real book rather than at the touch,
    # an order held for the latency a venue actually costs, and a position that
    # can be liquidated. The value of this whole block is that paper results
    # predict live ones, and each of these is one way that prediction breaks.
    "book-walk-fill-pricer",
    "order-latency-simulator",
    # paper-liquidation-simulator is off: it only ever fires on a
    # liquidation-price, which liquidation-price-tracker never produces
    # without a leverage-choice -- verified 2026-09-01, a bought option's
    # max loss is the premium paid, it cannot be liquidated the way a
    # leveraged futures position can.
    "paper-fill-simulator",
    # Closing the position. A fill becomes a held position, the exits are chained
    # to it the instant it fills, and both rest in the paper book until a live
    # price reaches one of them (RL-071). Whichever fills, the other is withdrawn.
    "fill-reconciler",
    "cost-basis-tracker",
    "peak-excursion-tracker",
    "exit-order-chainer",
    "stop-order-manager",
    "position-close-detector",
    # funding-settlement-recorder is off: funding rate is a crypto perpetual
    # concept and an option has none to book. inr-pnl-accountant's own
    # funding_inr term simply stays at its default without one.
    "inr-pnl-accountant",
    # The record. Without it a fill happened and nothing can say what decided it,
    # and a position closed with nothing to say what it was worth.
    "trade-lifecycle-recorder",
    "position-recorder",
    # ---- phase 5: closed-trade decoding, 2026-08-25 -------------------------
    # 115 round trips had closed and nothing had scored one. Every part here
    # reads `closed-trade` and `peak-excursion`, both already produced and
    # journalled, which is what makes this the block whose whole input was
    # already live.
    #
    # They are switched on together on purpose. Most of what they need, they
    # produce for each other -- `trade-episode` alone has eight readers -- so
    # starting them one at a time would be starting each into an empty inbox.
    # The ones that still refuse name their phase: volatility-forecast is 6,
    # regime-break-alert and correlation-cluster are 12, symbol-profile is 13.
    # A part that refuses for a stated reason is the finding, not the failure.
    #
    # Every one of them refuses a trade recorded before
    # closed_trades_trustworthy_after_ns: ten of the trades already on this
    # machine carry an entry price the market never printed, and a decoder
    # trained on those learns stop placement from prices that never existed.
    "near-miss-recorder",
    "entry-quality-scorer",
    "stop-placement-auditor",
    "trade-replay-verifier",
    "pnl-attributor",
    "regime-transition-tagger",
    "trade-cluster-detector",
    "luck-skill-separator",
    "shortfall-decomposer",
    "exit-counterfactual-replayer",
    # The keystone, started after its producers and before its readers: it
    # consumes six things the parts above make, and eight parts read the
    # `trade-episode` it produces.
    "trade-episode-encoder",
    "sequence-pattern-miner",
    "exploration-pair-decoder",
    "excursion-profiler",
    "exit-quality-scorer",
    "holding-horizon-profiler",
    "loss-cause-classifier",
    "winner-pattern-miner",
    "trade-narrative-writer",
    "lesson-extractor",
    # What the system concluded, as opposed to what it did. Added 2026-08-25 with
    # pnl-attribution: until then every conclusion this system drew lived on the
    # bus and died with the process that drew it, so a board had nothing on disk
    # to show and a part started tomorrow could learn nothing from today
    # (docs/proposals/a-conclusion-nobody-records-is-a-conclusion-nobody-has.md).
    # ---- phase 6b: the model, and the loop that keeps it honest ------------
    # This is a cycle by construction and is exempted as one: the model is
    # finetuned, it forecasts, forecast-scorer scores what it said against what
    # the market did, model-drift-monitor raises an alert when that accuracy
    # decays, and the alert is what triggers the next finetune. kronos-size-selector
    # closes a second loop, choosing which model size to run from the accuracy the
    # sizes themselves produced.
    #
    # Kronos-large (499.2M) is not open-source, which is why choosing a size is a
    # part at all rather than a setting.
    "kronos-size-selector",
    "kronos-finetuner",
    "kronos-forecaster",
    "forecast-distribution-gate",
    "forecast-ensembler",
    "forecast-scorer",
    "model-drift-monitor",
    # ---- phase 8: risk, capital, and the operator's own numbers ------------
    # The block that decides how much money a decision may use, and the desk that
    # holds the numbers the operator sets. Every capital ruling lands here.
    #
    # leverage-selector is ON since 2026-09-05, and it was the gap the temporary
    # goal named. It was off because Phase A was buy-only options -- no leverage
    # dial, and position-sizer defaults to 1.0x without a leverage-choice, which
    # was correct then and is a wrong answer now: cash-equity-intraday says
    # leverage_ceiling 5.0 and the bot would have been sized unlevered while its
    # own settings said five, with nothing reporting it. It answers once per
    # segment that trades the underlying, so the options segments still get their
    # 1.0x and only the segment that borrows borrows.
    "leverage-selector",
    # liquidation-price-tracker, margin-liquidation-watch and
    # paper-liquidation-simulator stay off, and this is a finding rather than an
    # omission: an intraday equity position on Indian broker margin is not
    # liquidated at a price the way a perpetual is. The broker squares it off
    # from around 15:15 IST, or calls for margin -- and the square-off is
    # modelled, by `intraday-square-off-placer`. A liquidation price computed
    # from a maintenance-margin rate would be a number this market does not
    # quote. Kept declared; they need a margin-shortfall model, which is real
    # unbuilt work and not a switch.
    "fund-lock-ledger",
    "drawdown-episode-tracker",
    # The brakes. Each emits its own risk-limit and the sizer takes the smallest,
    # so a brake that is off does not weaken the others -- it simply stops being
    # one of the votes.
    "drawdown-breaker",
    "stop-frequency-breaker",
    "profit-lock",
    # The desk. capital-settings-change-recorder matters beyond its own block:
    # RL-055 makes its journal the ONLY place the board's "when did this last
    # change" may come from -- never the file's mtime, never git log.
    "capital-utilisation-meter",
    "allocation-conservation-checker",
    "live-balance-divergence-watch",
    # ---- phase 7: the rest of the learning loop, 2026-08-25 ----------------
    # Six of these can act on what is already running; the rest name their own
    # phase and refuse. forecast-trust-learner is what forecast-ensembler has
    # been refusing for want of a trusted member, and sample-weight-assigner is
    # one of the two things kronos-finetuner needs before it can load a model at
    # all -- the other, retrain-request, waits on intelligence (12).
    #
    # A learned part that never sees an outcome is a part that never learns, and
    # every one of these closes a loop from what the system did back to what it
    # will do next.
    "label-builder",
    "reward-shaper",
    "sample-weight-assigner",
    "slippage-learner",
    "exit-timing-learner",
    "forecast-trust-learner",
    "instruction-performance-tracker",
    "regret-tracker",
    "bot-scorekeeper",
    # This order is a chain and not a preference: the scheduler asks for a
    # retrain, the registry keeps the version that produced, the tracker
    # attributes to that version, and the scorer scores what the tracker
    # attributed.
    "retrain-scheduler",
    "model-registry",
    "feature-attribution-tracker",
    "feature-reliability-scorer",
    "champion-challenger-gate",
    "edge-graduation-gate",
    # Reads the scorecard the keeper above builds: allocation moves toward the
    # segments that earned it, from realised USDT rather than from a forecast.
    "allocation-rebalance-proposer",
    # Last, after every part whose conclusions it writes down.
    "ablation-harness",
    "learning-recorder",
    # The ledger's other half: what the operator and the governor did, and the
    # check that the chain nobody can rewrite has not been rewritten.
    "control-recorder",
    "journal-integrity-checker",
    # Observability, started 2026-08-25. Nothing here decides anything: the probe
    # runner keeps each measurement beside the command that produced it, the
    # alert raiser deduplicates what a person should look at, and the board parts
    # build and check the page rather than publish it -- publishing is a URL
    # outside this repository and stays a person's action.
    "probe-runner",
    "human-override-reader",
    # Moved ahead of trading-halt-decider 2026-09-02, same reason as
    # ground-truth-snapshot-builder above: the crypto cutover removed a
    # spurious cycle that had been masking this real ordering gap.
    "venue-outage-rider",
    "market-anomaly-detector",
    "trading-halt-decider",
    "alert-raiser",
    # ccxt-order-router stays off: real Upstox order placement is a separate,
    # not-yet-built body of work (a broker-order-router part does not exist yet
    # -- runtime/brokers/upstox.py's own build_order_request_payload/
    # read_order_result are proven but deliberately not wired to a part, per
    # that commit's own reasoning).
    #
    # clock-skew-monitor comes back on (2026-09-06). It was off with that group
    # because its only input was `raw-venue-order-status`, which no live part
    # produces. It now also reads `broker-market-data`, whose `broker_time_ns`
    # is Upstox's own stamp on every price, so the offset between this machine's
    # clock and the broker's is measured continuously rather than never. Two
    # timestamp traps were found by hand on the day this was rewired and nothing
    # running would have caught either.
    #
    # Moved below broker-order-router on 2026-09-12: it also reads
    # `raw-venue-order-status`, and until that day nothing on the spine produced
    # one -- ccxt-order-router is off -- so its ordering against a producer was
    # vacuously satisfied. Adding a real producer made the requirement real, and
    # the spine's own dependency test is what noticed.
    "fund-conservation-auditor",
    "self-model-reporter",
    "decision-cost-accountant",
    "prompt-evaluator",
    "board-snapshot-builder",
    "board-publisher",
    "stale-board-watch",
    # Online research (phase 14). Nothing here fetches on its own: each reader
    # takes its fetcher by injection and answers FETCH_FAILED by name until one is
    # installed, so starting them makes the path exist without opening this box to
    # the open web by default.
    "exchange-announcement-reader",
    "options-flow-reader",
    "arxiv-feed-reader",
    "github-strategy-miner",
    "edge-comparator",
    "strategy-decoder",
    "trader-record-verifier",
    # copy-latency-estimator moved beside onchain-position-reader 2026-09-02
    # -- it needs external-position, which only that part produces, and
    # sat far later than tail-copy-selector (its consumer). Same shape as
    # this cutover's other unmasked ordering gaps.
    "copy-worthiness-scorer",
    # The procedural tier of memory, before the detectors that read its rules:
    # a rule applied by rule is the one thing a detector must not have to wait
    # for, and this part sat after every one of them until the spine's own
    # ordering test said so.
    "procedural-playbook",
    # The scanner's detectors, started 2026-08-25 (the rest of phase 3's block).
    # Each is one way a symbol becomes interesting, and every one of them has
    # been written and tested and never once run against the live feed.
    # liquidity-grader moved up beside the samplers 2026-09-02 -- see that
    # block's own comment.
    "momentum-burst-detector",
    "mean-reversion-detector",
    "volatility-gap-detector",
    # Index options only, added 2026-09-01 (options-scanner-first-slice.md):
    # a deep out-of-the-money option cheap enough for a late move toward its
    # strike to multiply its price before the session closes. The last of
    # the four crypto-only detectors this slice retired had no honest Indian
    # equivalent; this is what replaced them, not what was renamed from them.
    "expiry-day-zero-to-hero-detector",
    "universal-symbol-sweeper",
    "watch-condition-compiler",
    # The brain's remaining parts. opinion-conflict-resolver is the one that has
    # been waiting for phase 9: with a bear bot running there are finally two
    # opinions to resolve rather than one to pass through.
    "opinion-conflict-resolver",
    "devils-advocate",
    "premortem-writer",
    "intent-explainer",
    "intent-timing-gate",
    "bot-weight-sampler",
    "forecast-bias-weigher",
    "exploration-pair-opener",
    "brain-self-reflector",
    # docs/proposals/llm-reasoning-gets-a-vote.md step 1, 2026-08-30: the LLM
    # foundation's first part that reasons about the system rather than only
    # answering another part's question. Advisory only -- opinion-arbiter folds
    # it in as a per-bot trust discount, never a vote.
    "strategy-review-reasoner",
    # Step 2 of the same proposal, 2026-08-30: a real vote, but only on a
    # candidate another bot already has acting this tick.
    "setup-second-opinion-reasoner",
    # Step 3 of the same proposal, 2026-08-30, built last: the only one of the
    # three that originates its own vote rather than reviewing another bot's.
    "market-thesis-reasoner",
    # The bull bot's own two stragglers, which its bear twin has had since it
    # started: the weight learner and the watcher on an open position's thesis.
    "bull-setup-weight-learner",
    "bull-position-invalidation-watcher",
    # The execution path, started 2026-08-25 (phase 11). **Nothing here places a
    # live order.** money-mode says paper, so order-destination-router sends every
    # order to the paper book. venue-rate-budgeter, order-state-poller, venue-
    # order-status-translator, order-reject-classifier, order-not-found-
    # debouncer, order-resubmitter, venue-balance-reader and venue-position-
    # reader are all off with ccxt-order-router above -- the crypto live-order
    # path, not this segment's yet.
    # The parts that decide what an order should be, before the router that sends
    # it: a cancel and a reprice are things the router reads.
    "resting-order-cancel-policy",
    "limit-price-walker",
    # The live half of the order fork, started 2026-09-12 at the operator's
    # instruction. `order-destination-router` addresses every order by its
    # segment's money mode: paper orders go to paper-fill-simulator, live ones to
    # this. Both segments state money_mode "paper" today, so this refuses every
    # order it sees, and `refused_not_live_destination` climbing beside a
    # `placed` of zero is the measurement of that -- the honest picture of a live
    # path that is wired and deliberately shut, rather than one nobody built.
    #
    # **Placed here, after the two parts above, because it READS what they
    # decide** -- the comment above already says so -- and after
    # money-mode-reader, broker-token-refresh-scheduler and
    # broker-symbol-universe-bridge, which produce the three levels its gates
    # read. A router started before its gates have input refuses everything for
    # the wrong reason, and the counters look identical to refusing correctly.
    # The spine's own dependency test caught exactly that when this was first
    # put beside paper-fill-simulator.
    "broker-order-router",
    # Reads `raw-venue-order-status` from the router above, as well as
    # `broker-market-data`. See its own note higher up for why it came back on.
    "clock-skew-monitor",
    # The last risk part: it turns a bounded order into an execution schedule, a
    # plan rather than orders, so it adds nothing to the order path it reads.
    "participation-capped-order-splitter",
    # The guard on real money. It publishes the full limit while the segment is on
    # paper and judges nothing; live, it is what refuses until the bots have
    # graduated. Started now rather than with the live switch, because a guard
    # first started at the moment it must refuse is a guard nobody has watched.
    "live-switch-guard",
    # Intelligence, started 2026-08-25 (phase 12's first half). Nothing here
    # trades: they read what happened and say what it means -- the refutation
    # battery an edge has to survive, and the critics that judge decisions
    # after the fact. correlation-cluster-mapper (the clusters exposure-
    # limiter needs) moved up beside the samplers 2026-09-02 -- exposure-
    # limiter reads it and sits far earlier than this, a real pre-existing
    # ordering gap the crypto cutover's spurious-cycle removal unmasked, not
    # something this cutover introduced.
    "cross-segment-exposure-watch",
    "cross-segment-signal-bridge",
    "cross-segment-lesson-bridge",
    # What the calendar says is coming, before the detector that reads a scheduled
    # event as one reason a regime broke.
    "market-event-reader",
    "regime-break-detector",
    "turbulence-index-gauge",
    "open-web-reader",
    "edge-decay-tracker",
    "trial-count-accountant",
    "counterfactual-replayer",
    "causal-refutation-battery",
    "decision-quality-critic",
    "abstention-coverage-auditor",
    "forgetting-auditor",
    "idea-generator",
    # What a person said, before every part that obeys it. It is the highest
    # precedence halt there is, and a halt enforcer started before the part that
    # reads the override would enforce every other reason first.
    # The last two limiters, added 2026-08-25 to finish the risk block. Both
    # publish risk_allowed_fraction_when_clear -- one -- while they have nothing
    # to act on, so neither narrows anything until something produces a halt or a
    # venue announcement. Running them now is what makes the wire exist before
    # the first halt rather than after it.
    "halt-enforcer",
    # The other half of `close-positions`, added 2026-08-27. halt-enforcer stops
    # the bot opening anything new on that instruction and closes nothing; this
    # is what actually places the exits. Never shed -- part-priority ranks it
    # inside never_switched_off_priority_ceiling -- because a part that carries
    # out a human's instruction must not be switched off by the machine the
    # human is instructing.
    "position-flattener",
    # The exit nobody was placing: a contract that expires today, closed before
    # the session ends rather than held to settlement. Beside the flattener
    # because they share the placing (runtime/position_exit_placer.py) and
    # differ only in why a position must go. It matters most for Phase A's
    # second segment bot -- Indian single-stock options are physically settled,
    # so a bought call still open at expiry becomes a delivery obligation for
    # strike x lot size rather than a premium that expires worthless.
    "pre-expiry-position-closer",
    # The third of the three exits this system places on its own initiative, and
    # the one that does nothing at all on the segment running today: it closes
    # everything only when the segment's own settings say it may not hold
    # overnight, which cash-equity-intraday says and both options segments do
    # not. On an options spine it reports is_intraday 0 and holds still, which
    # is the correct board for it rather than a part that failed to run.
    "intraday-square-off-placer",
    "event-risk-limiter",
    # Knowledge, phase 13's unblocked half: the three tiers of memory. None of
    # these needs a provider key -- the parts that do are llm-foundation's, and
    # they stay off until the operator has one.
    "symbol-profile-store",
    # The embedder before the store that reads its embeddings: an episode is
    # keyed on the market condition that was observed, and the key is what the
    # store files it under.
    "episode-embedder",
    "episodic-trade-store",
    # Before the fact store, which reads the confidence it schedules a recheck
    # against: the store was started first and had nothing to age its facts by.
    "forgetting-curve-scheduler",
    "semantic-fact-store",
    "contradiction-detector",
    "community-chat-reader",
    "fact-provenance-tracker",
    "regime-memory-store",
    "knowledge-graph-linker",
    "knowledge-pruner",
    "knowledge-snapshot-versioner",
    "instruction-archive",
    # Backtesting, phase 15's first half. RL-071 is absolute and unaffected: a
    # replay proves an instruction before the scanner is told to watch for it,
    # and no trading decision and no live learning is ever made from one.
    "historical-bar-store",
    "walk-forward-splitter",
    "lookahead-auditor",
    "intra-bar-fill-sequencer",
    "fill-volume-capper",
    "execution-cost-model",
    "instruction-replayer",
    "backtest-scorer",
    "live-vs-replay-reconciler",
    "instruction-promotion-gate",
    # The LLM foundation, started 2026-08-25 (phase 13). None of it
    # needs a provider key: these are the parts that assemble context, render a
    # prompt from a versioned template, enforce the shape of a reply, keep the
    # golden cases and gate a promotion. The two parts that would spend money are
    # llm-services', and they refuse by name while no key exists.
    "prompt-registry",
    "prompt-template-author",
    "prompt-renderer",
    "structured-output-enforcer",
    "context-assembler",
    "retrieval-index",
    "retrieval-querier",
    "retrieval-quality-scorer",
    "knowledge-embedder",
    "golden-case-keeper",
    "prompt-drift-monitor",
    "prompt-promotion-gate",
    # The entrance to the promotion ring, added 2026-09-12. Until this part
    # existed, NO LLM call had ever been made by any of the sixteen parts that
    # publish `llm-request`: prompt-renderer had refused 906 of 906 requests for
    # `no-active-prompt-version-for-this-purpose`, because an active version
    # needs a promotion, a promotion needs a score, a score needs golden cases
    # and validated output, and validated output needs an active version.
    #
    # After prompt-promotion-gate and never in place of it: this part promotes a
    # purpose's FIRST version, once, on no evidence and saying so, and every
    # version after that goes through the gate on measurement.
    # docs/proposals/the-first-prompt-for-a-purpose-cannot-be-scored.md
    "seed-prompt-promoter",
    "part-token-budgeter",
    # The LLM services. metered-api-caller and subscription-session-caller
    # are the two parts in this system that could spend money on a provider, and
    # docs/secrets.md records no provider: they run and refuse, which is the state
    # they were built to report, and paid-spend-ledger stays at zero for a reason
    # it can name.
    "llm-backpressure-gauge",
    "llm-model-picker",
    "llm-request-router",
    "llm-response-cache",
    "local-model-caller",
    "metered-api-caller",
    "subscription-session-caller",
    "subscription-quota-watch",
    "paid-spend-ledger",
    # Skills, phase 13's third quarter: the procedural tier of memory that is
    # applied by rule and never searched. The distillers and readers need a source
    # to distil, and no fetcher is installed on this box, so they idle honestly.
    "skill-loader",
    "skill-version-keeper",
    "skill-provenance-stamper",
    "skill-composer",
    "skill-conflict-detector",
    # The readers that produce a source document, before the distiller that reads
    # one: a skill is distilled from something, and a distiller with nothing to
    # distil is the same shape as one whose sources have not started yet.
    "source-ingester",
    "book-and-paper-fetcher",
    "video-lecture-reader",
    "skill-distiller",
    "skill-gap-finder",
    "skill-scorer",
    "skill-tester",
    "skill-refresher",
    # The index last of the skill parts: it reads a skill, a conflict, a version,
    # a provenance stamp and a backtest, and every one of those is produced by a
    # part above it.
    "skill-index",
    # Hypothesis, the other half of phase 12: where the system proposes its own
    # edges. loss-inverter turns a losing trade into the hypothesis for the
    # opposite one, and nothing reaches the scanner without surviving the battery.
    "symbolic-hypothesis-miner",
    "loss-inverter",
    "hypothesis-deduplicator",
    "hypothesis-regime-tagger",
    "hypothesis-ranker",
    "power-estimator",
    "hypothesis-falsifier",
    "hypothesis-mutator",
    "expectancy-decomposer",
    "instruction-writer",
    "instruction-retirer",
    # Autonomous, started last on purpose 2026-08-25 (phase 15's second half).
    # Nothing should be able to propose a part for this system until every other
    # block has been observed running, and part-admission-gate is the part that
    # admits one. It needs a proposed part to admit, part-author needs a validated
    # LLM output to write one, and no provider is configured -- so the gate exists
    # and admits nothing, which is the honest state rather than an empty one.
    #
    # trading-halt-decider halts on any one cause and an unmeasured runway is not
    # one since today: the runway it reads is a provider budget, this box has no
    # provider, and reading that as NO_RUNWAY would have stopped paper trading
    # permanently the minute this block started.
    "folded-circuit-view",
    "no-progress-detector",
    "unattended-run-warden",
    "survival-tier-monitor",
    "conservation-planner",
    "autonomy-boundary",
    "autonomy-policy-engine",
    "capability-gap-finder",
    "upstream-improvement-watch",
    "part-author",
    "part-admission-gate",
    "part-replacement-planner",
    "self-modification-journal",
)

# The only money mode this spine may run in, whatever segment runtime.toml's
# own segment_id names. Checked before a part is started rather than trusted:
# `order-destination-router` refuses to address a live order and
# `paper-fill-simulator` refuses to simulate one, and this is the third check,
# at the one moment where refusing costs nothing. A run that reached a live
# venue is the failure this phase cannot recover from (RL-005).
PAPER = "paper"
MONEY_MODE_SETTING = "money_mode"
SEGMENT_ID_SETTING = "segment_id"
BUILT_SEGMENTS_SETTING = "built_segments"


def read_runtime_settings():
    return load_settings_document(settings_directory() / "runtime.toml", "runtime")


def refuse_unless_the_segment_is_on_paper(settings) -> tuple[str, str]:
    """Every segment this spine trades, and the money mode it may run in, both
    read from the operator's own files -- never a name fixed in this script.

    Checks all of `built_segments` since 2026-09-05, not only `segment_id`.
    Three segment bots run on this spine, and checking one of them would give
    exactly the reassurance this refusal is for while two others could be live.

    Fixed as `TRADED_SEGMENT = "futures"` until 2026-09-02: runtime.toml's own
    segment_id had already been changed to "index-options" for the crypto-to-
    Indian cutover, so every part correctly read the new segment's settings
    while this check kept validating the old one -- the exact "operator sets
    this segment live and this spine refuses" guarantee the docstring below
    promises, silently pointed at a file nobody was trading under any more.
    Caught by reading the live spine's own startup log, not by a test: this
    script's segment string had drifted from every part's, and nothing here
    could have noticed on its own.

    Read here rather than assumed, and read before any part is forked. The parts
    that place and fill orders each refuse a live order on their own, and this is
    the third check, at the one moment where refusing costs nothing. An operator
    who set this segment live and then started this spine gets a refusal instead
    of fourteen processes discovering it one at a time.
    """
    named = settings.entries.get(BUILT_SEGMENTS_SETTING)
    listed = named.value if named is not None else None
    segments = (
        [str(segment) for segment in listed]
        if isinstance(listed, (list, tuple)) and listed
        else [str(settings.read_value(SEGMENT_ID_SETTING))]
    )
    for segment in segments:
        document = load_settings_document(
            settings_directory() / "segments" / f"{segment}.toml", segment
        )
        mode = str(document.read_value(MONEY_MODE_SETTING))
        if mode != PAPER:
            raise SystemExit(
                f"{segment} says {MONEY_MODE_SETTING} = {mode!r}, and this spine starts the "
                f"parts that place orders. Only {PAPER!r} may run here: RL-005 is paper first, with "
                f"full experimentation and no restriction, and live only for what paper proved."
            )
    return ", ".join(segments), PAPER


def is_capture_script_running() -> list[str]:
    """The capture processes that would fight this one for the tape."""
    found = subprocess.run(
        ["pgrep", "-af", CAPTURE_SCRIPT_NAME], capture_output=True, text=True
    )
    return [line for line in found.stdout.splitlines() if CAPTURE_SCRIPT_NAME in line]


def record(event: dict) -> None:
    """Append one observation, flushed. A supervisor that is killed is normal."""
    STATE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    line = {"observed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "pid": os.getpid(), **event}
    with open(SUPERVISOR_LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
        handle.flush()
    print(json.dumps(line), flush=True)


def take_the_lock():
    """One supervisor at a time. Two would start two of every part."""
    STATE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_FILE, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SystemExit(
            f"another live spine already holds {LOCK_FILE}. Two supervisors would start two "
            f"of every part, and two copies of venue-trade-stream-reader would write every "
            f"message to the tape twice."
        )
    return handle


class RestartPolicy:
    """When a part that exited may be started again, and how often it has been.

    Backoff per part rather than globally: one part crashing on startup must not
    delay the restart of a different part that died for an unrelated reason.
    """

    def __init__(self, floor_seconds: float, ceiling_seconds: float) -> None:
        self._floor = floor_seconds
        self._ceiling = ceiling_seconds
        self._next_attempt_at: dict[str, float] = {}
        self._wait: dict[str, float] = {}
        self.restarts: dict[str, int] = {}

    def may_start(self, part_id: str, now: float) -> bool:
        return now >= self._next_attempt_at.get(part_id, 0.0)

    def record_restart(self, part_id: str, now: float) -> float:
        wait = min(self._wait.get(part_id, self._floor) * 2, self._ceiling)
        self._wait[part_id] = wait
        self._next_attempt_at[part_id] = now + wait
        self.restarts[part_id] = self.restarts.get(part_id, 0) + 1
        return wait

    def record_healthy(self, part_id: str) -> None:
        """A part that has stayed up starts its next backoff from the floor."""
        self._wait.pop(part_id, None)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print the spine and exit")
    parser.add_argument(
        "--without-feed",
        action="store_true",
        help="run everything except the three feed parts, leaving the capture script to write the tape",
    )
    arguments = parser.parse_args(argv)

    spine = LIVE_SPINE
    if arguments.without_feed:
        spine = tuple(
            part_id
            for part_id in LIVE_SPINE
            if part_id not in ("symbol-catalogue-reader", "stream-budget-planner", "venue-trade-stream-reader")
        )

    if arguments.list:
        for part_id in spine:
            print(part_id)
        return 0

    # Decrypted here, once, into this process's own environment -- child
    # processes (every part, forked below) inherit it, which is what lets
    # broker-token-refresh-scheduler's upstox-totp call auto-load UPSTOX_*
    # with no further wiring. setdefault so an operator's own explicit env
    # var is never overwritten. Missing/placeholder credentials leave the
    # environment untouched -- broker-token-refresh-scheduler already
    # tolerates that indefinitely (its own docstring: "never raises").
    for name, value in read_upstox_env().items():
        os.environ.setdefault(name, value)

    settings = read_runtime_settings()
    segment, money_mode = refuse_unless_the_segment_is_on_paper(settings)
    wiring = derive_wiring()
    unknown = [part_id for part_id in spine if part_id not in wiring]
    if unknown:
        raise SystemExit(f"not in the blueprint: {unknown}")

    if not arguments.without_feed:
        fighting = is_capture_script_running()
        if fighting:
            raise SystemExit(
                "the capture script is still running:\n  "
                + "\n  ".join(fighting)
                + f"\n\nvenue-trade-stream-reader writes the same tape files, and two writers make "
                f"a duplicate record indistinguishable from a real second print. Stop the capture "
                f"first (operate/README.md), or run with --without-feed to leave it in charge of "
                f"the tape."
            )

    lock = take_the_lock()

    # A previous run that died without its stop path -- a supervisor killed at
    # the timeout, a machine that lost power mid-stop -- leaves its parts'
    # scopes behind, and systemd refuses a second unit by the same name: every
    # placement would fail and all 57 parts would run unbounded while looking
    # started. The lock above proves no other spine is alive, so a scope named
    # for one of this spine's parts is a leftover, and stopping it kills any
    # orphaned process still inside -- which is the recovery, not a hazard: an
    # orphan holds inbox sockets the new run needs. The slice is found from this
    # process's own cgroup, the same way part-appetite-meter finds the scopes.
    from runtime.hardware_facts import read_own_cgroup_directory

    for leftover in sorted(read_own_cgroup_directory().parent.glob("*.scope")):
        part_id = leftover.name.removesuffix(".scope")
        if part_id in spine:
            subprocess.run(
                ["systemctl", "--user", "stop", leftover.name],
                capture_output=True, text=True,
            )
            record({"event": "leftover-scope-stopped", "part_id": part_id})

    # Every part in its own transient scope, with the same bounds for all --
    # limits are per-part only when a part with a genuinely larger working set
    # earns them (the settings' notes carry the measurements). The scopes are
    # what part-appetite-meter reads, so without them the metering is absent,
    # the planner refuses every plan, and the governor cannot act at all: this
    # line is what turned the governor from refusing to governing on 2026-08-24.
    # A placement that fails does not kill the part -- it runs unbounded and is
    # reported as such, which is the status quo before this existed.
    scope_limits = ScopeLimits(
        memory_max_bytes=int(settings.read_value("part_scope_memory_max_bytes")),
        cpu_weight=int(settings.read_value("part_scope_cpu_weight")),
        pids_max=int(settings.read_value("part_scope_pids_max")),
    )
    launcher = PartLauncher(
        place_in_scope=True,
        limits_for=lambda part_id: scope_limits,
        thread_ceiling=int(settings.read_value("fork_thread_ceiling")),
        placement_confirmation_deadline_seconds=float(
            settings.read_value("placement_confirmation_deadline")
        ),
        placement_confirmation_poll_interval_seconds=float(
            settings.read_value("placement_confirmation_poll_interval")
        ),
    )
    stop_deadline = float(settings.read_value("part_stop_deadline"))
    switch_service = launcher.open_switch_service(stop_deadline, SWITCH_ENDPOINT_BACKLOG)
    policy = RestartPolicy(RESTART_BACKOFF_FLOOR_SECONDS, RESTART_BACKOFF_CEILING_SECONDS)
    stopping = {"asked": False}

    def ask_to_stop(signal_number, _frame) -> None:
        stopping["asked"] = True
        record({"event": "stop-requested", "signal": signal_number})

    signal.signal(signal.SIGTERM, ask_to_stop)
    signal.signal(signal.SIGINT, ask_to_stop)

    record(
        {
            "event": "spine-starting",
            "parts": list(spine),
            "switch_endpoint": switch_service.address,
            "without_feed": arguments.without_feed,
            # Written into the record of the run, not only checked: what the
            # orders this spine places were addressed at is the first thing anyone
            # reading the journal afterwards needs to know.
            "segment": segment,
            "money_mode": money_mode,
            # True since 2026-08-24: every part is placed in its own scope with
            # the bounds the settings state, and a part a placement failed for
            # runs unbounded and is counted in the launcher's standing.
            "placed_in_scopes": True,
        }
    )

    for part_id in spine:
        try:
            launched = launcher.start(part_id)
            record({"event": "part-started", "part_id": part_id, "pid": launched.process.pid})
        except Exception as refusal:
            record(
                {
                    "event": "part-refused",
                    "part_id": part_id,
                    "reason": f"{type(refusal).__name__}: {refusal}",
                }
            )

    # Parts the governor has switched off, which the restart loop below must not
    # switch back on. Without this the supervisor and gate-actuator fight: the
    # actuator stops a part, the loop sees a part not running and restarts it,
    # switch-oscillation-damper reports the flapping, and the flapping is the
    # supervision's own. The governor's off ends when the governor says on.
    governor_switched_off: set[str] = set()

    try:
        while not stopping["asked"]:
            for outcome in switch_service.serve_pending():
                record(
                    {
                        "event": "governor-switch",
                        "part_id": outcome.part_id,
                        "action": outcome.action,
                        "outcome": outcome.outcome,
                        "detail": outcome.detail,
                    }
                )
                if outcome.outcome == OUTCOME_FLIPPED:
                    if outcome.action == ACTION_TURN_OFF:
                        governor_switched_off.add(outcome.part_id)
                    else:
                        governor_switched_off.discard(outcome.part_id)
            now = time.monotonic()
            for part_id in spine:
                if part_id in governor_switched_off:
                    continue
                if launcher.is_running(part_id):
                    policy.record_healthy(part_id)
                    continue
                if not policy.may_start(part_id, now):
                    continue
                wait = policy.record_restart(part_id, now)
                # Read before the restart replaces it. Without this the log said a
                # part restarted and never how the last one ended, so a part
                # crash-looping thirteen times a minute was indistinguishable from
                # one being cycled on purpose -- and undiagnosable either way.
                last_exit_code = launcher.read_exit_code(part_id)
                try:
                    launched = launcher.start(part_id)
                    record(
                        {
                            "event": "part-restarted",
                            "last_exit_code": last_exit_code,
                            "part_id": part_id,
                            "pid": launched.process.pid,
                            "restarts": policy.restarts[part_id],
                            "next_backoff_seconds": wait,
                        }
                    )
                except Exception as refusal:
                    record(
                        {
                            "event": "restart-refused",
                            "part_id": part_id,
                            "reason": f"{type(refusal).__name__}: {refusal}",
                            "next_backoff_seconds": wait,
                        }
                    )
            time.sleep(SUPERVISION_INTERVAL_SECONDS)
    finally:
        record({"event": "spine-stopping", "restarts": policy.restarts})
        outcomes = launcher.stop_all(stop_deadline)
        launcher.close()
        lock.close()
        record({"event": "spine-stopped", "exit_codes": {k: v for k, v in outcomes.items()}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
