#!/usr/bin/env python3
"""Trade a captured session through the real parts, and report what closed.

Phase A asks for "paper trading on historic data first" (docs/goal.md), and
until 2026-09-04 there was no way to do it: the only options open-and-close
proof this project had was
`tests/integration/test_an_options_position_opens_and_closes.py`, whose own
docstring says its premium walk is "a stated, documented sequence rather than a
captured print series ... This test should move to a captured run the moment one
exists." One exists now -- 6,644 Upstox streams for 2026-09-04, carrying trades,
option greeks, the book and candles per contract.

**This is not the live spine and must never be mistaken for it (RL-071).** A
tape replay is never what a live run's decisions or learning are made from, so
this writes to its own state root, reads nothing the spine wrote, and every
trade it produces is stamped `replay` with the day it replayed. The dashboard
renders those separately for the same reason the board stopped showing the
crypto era's closed trades as this segment's: a board that answers a question
about live trading with replayed rows is worse than an empty one.

What is real here, and what is not:

- **Real**: every price is a captured Upstox print, in the order and at the
  times it printed. The fills, the fees (Upstox's own charge stack on both
  legs), the lot book, the excursion tracking and the close detection are the
  same parts the live spine runs, not reimplementations.
- **Stated**: where the entry sits and how far the stop and target sit from it.
  The live path gets those from `stop-target-placer`, which needs a volatility
  forecast or excursion history that a cold replay does not have. They are
  derived here from the contract's own captured move over the session and the
  provenance is printed with the results, so nothing reads as measured that was
  not.

**Since 2026-09-06 it runs the learning half too.** Until then this script
imported seven parts and stopped at `position-close-detector`: the 2026-09-04
replay closed 252 trades and not one of them reached `pnl-attributor`, so
everything downstream of `closed-trade` had never run on a real trade at all.
That is why `edge-graduation-gate` reads `judgements 0` on the live spine and
`bot-maturity` has never been produced for any of its five consumers. A closed
trade now walks on through attribution, entry quality, significance, the trade
episode and the bot scorecard -- see `LearningReplay` for what of that is
measured and what is stated, and for the one part it deliberately does not run.

Doing that exposed a defect the trading half could never have shown.
`paper-fill-simulator` stamps a fill `filled_at_ns=self._now_ns()` and
`position-close-detector` stamps `closed_at_ns=self._now_ns()`; live that is
right, and in a replay it was the wall clock of the machine running it, so a
forty-minute round trip was recorded as held for the few milliseconds the replay
took to walk its prints. Net PnL does not depend on the clock, so nothing
noticed. Anything scaled by a horizon does: `luck-skill-separator` reported
outcomes of **-1,958 and -5,689 standard deviations** where the honest figures
are -6.9 and -9.2. `TapeClock` gives those parts the captured session's own time
instead.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import math
import pathlib
import types
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from parts.closed_trade_decoding.entry_quality_scorer import EntryQualityScorer
from parts.closed_trade_decoding.luck_skill_separator import LuckSkillSeparator
from parts.closed_trade_decoding.pnl_attributor import PnlAttributor
from parts.closed_trade_decoding.trade_episode_encoder import TradeEpisodeEncoder
from parts.learning_loop.bot_scorekeeper import BotScorekeeper
from parts.broker_adapter.broker_order_router import BrokerOrderRouter
from parts.paper_live_trading.paper_fill_simulator import FILLED, PaperFillSimulator
from parts.paper_live_trading.stop_order_manager import (
    PLACE_NEW, PLACE_TARGET, StopOrderManager, as_order_request,
)
from parts.portfolio_state.cost_basis_tracker import CostBasisTracker
from parts.portfolio_state.fill_reconciler import FillReconciler
from parts.learning_loop.label_builder import (
    ClosedTradeRecord, ExcursionRecord, LabelBuilder,
)
from parts.portfolio_state.peak_excursion_tracker import PeakExcursionTracker
from parts.portfolio_state.position_close_detector import PositionCloseDetector
from parts.risk_capital_allocation.exit_order_chainer import ExitOrderChainer
from runtime.market_conditions import MarketSessionState, SessionKind
from runtime.settings_reader import load_settings_document, settings_directory
from runtime.tape import read_tape_index
from runtime.brokers.upstox import UpstoxAdapter
from runtime.trading_types import BUY, LONG, MARKET, OPTION, PAPER_BOOK, SELL, SPOT

# Where a replay's own results live. Deliberately not under the live spine's
# state root: nothing here may be read by a part, and nothing a part wrote may
# be read by this.
REPLAY_ROOT = pathlib.Path.home() / ".local/share/ajit-segment-bots/replay"
TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
VENUE = "upstox"

# The operator's own settings, which is where every segment states what it
# trades. Read rather than restated, for the same reason SettingsContext exists.
SETTINGS_ROOT = settings_directory()

# Prints in a full NSE session, used to scale a contract's own per-print
# volatility up to the daily figure luck-skill-separator compares against. The
# session is 09:15-15:30 IST; the count is the contract's own, so a thin
# contract is not flattered by a busy one's sampling rate.
SECONDS_IN_AN_NSE_SESSION = 6.25 * 60 * 60

# What the replay states rather than measures on the scorecard half, named here
# so it is one list rather than a value buried at each call site. See
# `LearningReplay`'s docstring.
THE_REPLAY_HAS_NO_DETECTOR = "replay-opened-at-the-first-print"
THE_REPLAY_HAS_NO_REGIME = "unclassified-in-replay"


def money_mode_of(segment: str):
    """One segment's real money mode, from its own settings file.

    The same source `money-mode-reader` reads live, rather than a hardcoded
    "paper": if an operator moves a segment to live, this replay should show the
    router refusing for the NEXT reason instead of going on claiming the first
    one. A segment with no file at all reads as paper, which is what
    `money-mode-reader` itself does in every ambiguous case.
    """
    import types

    if not segment:
        return types.SimpleNamespace(segment=segment, mode="paper")
    try:
        document = load_settings_document(
            settings_directory() / "segments" / f"{segment}.toml", f"segment:{segment}"
        )
        return types.SimpleNamespace(
            segment=segment, mode=str(document.read_value("money_mode"))
        )
    except (OSError, KeyError, ValueError):
        return types.SimpleNamespace(segment=segment, mode="paper")


def number(document, name: str) -> float:
    """One setting's value, by the same name the live part reads it under.

    Read from the operator's own settings rather than restated here, so a replay
    charges exactly what the live path charges. `read_value` refuses a name
    nobody declared, which is what stops a replay inventing a fee.
    """
    return float(document.read_value(name))


def prints_of(directory: pathlib.Path, day: str) -> list[tuple[int, float]]:
    """Every captured trade print for one instrument, in the order it printed."""
    index_path = directory / f"{day}.index"
    if not index_path.exists():
        return []
    records = read_tape_index(index_path)
    prints: list[tuple[int, float]] = []
    with open(directory / f"{day}.blob", "rb") as blob:
        for record in records:
            blob.seek(int(record["blob_offset"]))
            try:
                payload = json.loads(blob.read(int(record["blob_length"])))
            except ValueError:
                continue
            price = payload.get("price") or payload.get("last_traded_price")
            if price:
                prints.append((int(record["venue_time_ns"] or record["received_at_ns"]),
                               float(price)))
    return prints


# The only instrument master on this machine is the slice captured for the
# universe-bridge tests. It covers NIFTY, so most replayed contracts resolve to
# the name they trade under and the rest keep the venue's instrument key. A key
# shown as though it were a trading symbol would be a board naming something
# that does not exist.
CAPTURED_MASTER = (
    pathlib.Path(__file__).resolve().parents[1]
    / "tests/captured/upstox/2026-09-04-nse-instrument-master-nifty-slice.json"
)


def trading_symbols() -> dict[str, str]:
    """instrument_key -> the name it trades under, for what the master covers."""
    try:
        rows = json.loads(CAPTURED_MASTER.read_text())
    except (OSError, ValueError):
        return {}
    return {
        row["instrument_key"]: row["trading_symbol"]
        for row in rows
        if row.get("instrument_key") and row.get("trading_symbol")
    }


# The real instrument master, fetched from Upstox's own published asset rather
# than the NIFTY-shaped test slice beside the tests. The slice covers 196 rows
# and no `lot_size` at all, so it can name a NIFTY contract and cannot size one,
# and it knows nothing of the stock-options or cash-equity universes -- which is
# to say it cannot answer the question this replay is being asked.
#
# Kept out of the repository (Rule 9: large re-fetchable media stays out) and
# refreshed by hand, because an instrument master is a fact about a trading day
# and a stale one silently prices the wrong contract.
REAL_MASTER = (
    pathlib.Path.home()
    / ".local/share/ajit-segment-bots/instrument-master/complete.json.gz"
)

# Upstox's own instrument_type spellings, mapped to the vocabulary the segment
# files are written in. Not a guess: `segment_instrument_types` holds "option"
# and "spot", and `segment_that_trades` compares against those.
KIND_OF_INSTRUMENT_TYPE = {"CE": OPTION, "PE": OPTION, "EQ": SPOT}


def instruments_by_key() -> dict:
    """Every instrument the master knows, keyed by the key the tape files use."""
    import gzip

    if not REAL_MASTER.exists():
        raise SystemExit(
            f"no instrument master at {REAL_MASTER}. Fetch it first:\n"
            f"  mkdir -p {REAL_MASTER.parent} && curl -sSL -o {REAL_MASTER} "
            f"https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz\n"
            f"Without it a replay cannot say which segment owns a contract, and "
            f"guessing from the tape's own key format would be this project "
            f"deciding what the broker already publishes."
        )
    rows = json.load(gzip.open(REAL_MASTER))
    return {row["instrument_key"]: row for row in rows if row.get("instrument_key")}


class SettingsContext:
    """The `.setting(name)` shape `runtime/segment_settings` reads machine scope
    through, over the operator's real runtime.toml.

    A replay must ask the *same* functions the live spine asks -- which segment
    claims an instrument is decided by `segment_that_trades` in both, so a
    replay that reimplemented the claim could agree with itself and disagree
    with the bot.
    """

    def __init__(self) -> None:
        self._document = load_settings_document(
            settings_directory() / "runtime.toml", "runtime"
        )

    def setting(self, name: str):
        entry = self._document.entries.get(name)
        if entry is None:
            raise KeyError(name)
        return entry


def cash_equity_eligible_symbols(master: dict) -> frozenset[str]:
    """Every ordinary NSE share no derivative in the real master is written on.

    The same exclusion `broker-symbol-universe-bridge` applies live
    (`EquityWithoutADerivative`, reused rather than restated -- T-6), replayed
    against this day's real instrument master. `segment_that_trades` asks this
    for cash-equity-intraday's membership because that segment's
    `segment_underlying_trading_symbols` is a 14-name legacy fallback,
    disconnected from the derived universe the bridge actually publishes
    (2026-09-05) -- passing the static list here would reproduce the same gap
    a replay exists to catch, not verify past it.
    """
    import types

    from parts.market_data_feed.broker_symbol_universe_bridge import EquityWithoutADerivative

    admits = EquityWithoutADerivative().admits
    derivative_underlying_keys = {
        row["underlying_key"] for row in master.values() if row.get("underlying_key")
    }
    return frozenset(
        row["trading_symbol"]
        for key, row in master.items()
        if key not in derivative_underlying_keys and admits(types.SimpleNamespace(**row))
    )


def option_underlying_symbols(master: dict, rule) -> frozenset[str]:
    """Every underlying in the real master that `rule` admits AND an option names.

    The second half is what makes it a universe rather than a catalogue: 2,655
    ordinary shares are listed and 210 carry options; 216 index listings exist
    and 10 do. `IndexWithAnOption` and `StockWithAnOption` are reused from
    `broker-symbol-universe-bridge` rather than restated (T-6), so a replay
    cannot disagree with the live spine about who owns an instrument.

    Needed from 2026-09-12, when both option segments stopped stating their
    underlyings and started deriving them. `segment_that_trades` answers None
    for a derived segment it is given no membership for -- honestly, since it
    has no master -- so without this the replay found that no segment owned
    anything and reported "0 of 2 segments traded", which is the silent-wrong-
    answer shape this project keeps finding.
    """
    import types

    derivative_underlying_keys = {
        row["underlying_key"] for row in master.values() if row.get("underlying_key")
    }
    return frozenset(
        row["trading_symbol"]
        for key, row in master.items()
        if key in derivative_underlying_keys and rule.admits(types.SimpleNamespace(**row))
    )


def derived_membership_for(master: dict) -> dict:
    """Which underlyings each derived-universe segment owns, this day's master.

    One entry per segment whose `segment_universe_selection` is a rule rather
    than a list. A segment stating its own symbols is absent and needs no entry:
    `segment_that_trades` reads those straight from its settings file.
    """
    from parts.market_data_feed.broker_symbol_universe_bridge import (
        EquityWithoutADerivative, IndexWithAnOption, StockWithAnOption,
    )

    return {
        "cash-equity-intraday": cash_equity_eligible_symbols(master),
        "index-options": option_underlying_symbols(master, IndexWithAnOption()),
        "stock-options": option_underlying_symbols(master, StockWithAnOption()),
    }


def the_market_is_open_now(
    live_answer=None, fetch_holidays=None, now=None,
) -> bool | None:
    """Whether NSE is in a trading session right now, asked of the calendar part.

    `market-session-calendar` is the live spine's own answer to this, so a replay
    that reimplemented the hours could disagree with the bot about whether the
    market was open -- which is the difference between "replay history" and
    "trade live".

    **Until 2026-09-13 this could never answer True.** It built a
    `MarketSessionCalendar` and never gave it a holiday list, and `session_at`
    answers CLOSED ("no holiday list has been read yet") whenever none has been
    read -- so every run, market open or not, replayed history. Asked in order now:

    1. the running calendar part's own answer, read from its standing in the
       heartbeat table (`runtime/market_session_answer.py`, which the capture
       board's freshness tile reads too);
    2. with no spine running, a calendar built the way the part builds it,
       holiday list fetched from NSE the way the part fetches it;
    3. None when neither can answer -- a different answer from "closed", treated
       as closed by the caller, because an unmeasured session is not an open one
       (Rule 8).

    `live_answer`, `fetch_holidays` and `now` are seams for tests; left as None
    they are the real reader, the real NSE fetch and the real clock.
    """
    import zoneinfo

    from parts.stock_market_news_data.market_session_calendar import (
        EXCHANGE_TIMEZONE, HOLIDAY_URL, MarketSessionCalendar, read_clock_time,
    )
    from runtime.market_session_answer import IN_SESSION, read_the_calendars_live_answer

    answer, _proof = (live_answer or read_the_calendars_live_answer)()
    if answer is not None:
        return answer == IN_SESSION

    context = SettingsContext()
    try:
        timezone = zoneinfo.ZoneInfo(EXCHANGE_TIMEZONE)
        calendar = MarketSessionCalendar(
            segment=str(context.setting("market_session_segment").value),
            opens_at=read_clock_time(str(context.setting("market_session_opens_at_ist").value)),
            closes_at=read_clock_time(str(context.setting("market_session_closes_at_ist").value)),
            timezone=timezone,
        )
        if fetch_holidays is None:
            from runtime.nse_public_data import NsePublicData, open_browser_session

            nse = NsePublicData(
                session=open_browser_session(),
                timeout_seconds=float(context.setting("nse_public_data_timeout_seconds").value),
            )
            fetch_holidays = lambda: nse.read_json(HOLIDAY_URL)  # noqa: E731
        calendar.observe_holidays(fetch_holidays())
        moment = now or datetime.datetime.now(timezone)
        return calendar.session_at(moment).is_tradeable
    except Exception:
        return None


def the_most_recent_weekday_before_today() -> str:
    """The last day NSE could have traded, as a date.

    Could, not did: a holiday is still returned, and the history fetch then finds
    no prints for it and says so. Guessing which holidays exist here would be a
    second calendar to disagree with the real one.
    """
    day = datetime.date.today() - datetime.timedelta(days=1)
    while day.weekday() >= 5:  # Saturday, Sunday
        day -= datetime.timedelta(days=1)
    return day.isoformat()


def contracts_from_history(from_date: str, to_date: str, per_segment: int,
                           minimum_prints: int) -> tuple[dict, collections.Counter, dict]:
    """Each built segment's own instruments, priced from the broker's history.

    The tape is three days deep and holds only what the feed was subscribed to.
    Upstox serves one-minute bars from January 2022 for equities, indices and
    every currently listed option, so this is what lets a replay run on a session
    the tape never covered -- the operator's own suggestion, 2026-09-06.

    **What each segment trades is asked of the segment, never chosen here.**
    Every segment declares `segment_underlying_trading_symbols`, and for the two
    options segments the contracts are that underlying's nearest *unexpired*
    expiry, at the strikes closest to where the underlying actually closed on the
    day being replayed -- fetched, not assumed. A strike far from the money has
    no history because nobody traded it, and replaying one would be replaying an
    empty series.
    """
    from runtime.segment_settings import built_segments, read_segment_symbols

    from operate.historical_prints import prints_for_instrument
    from operate.nse_intraday_option_prices import open_browser_session as open_nse_session
    from operate.yahoo_finance_prints import open_browser_session as open_yahoo_session

    # One warmed session per host, reused for every instrument. Both are free and
    # neither needs an account; Upstox is reached only when neither can serve,
    # because it is the one source with a quota to spend.
    yahoo_session = open_yahoo_session()
    try:
        nse_session = open_nse_session()
    except Exception:
        nse_session = None
    sources_used: collections.Counter = collections.Counter()

    def prints_of_instrument(row):
        prints, source = prints_for_instrument(
            row, to_date, session=yahoo_session, nse_session=nse_session,
        )
        sources_used[source] += 1
        return prints

    context = SettingsContext()
    master = instruments_by_key()
    by_symbol = {}
    for row in master.values():
        symbol = row.get("trading_symbol")
        if symbol and row.get("instrument_type") in ("EQ", "INDEX"):
            by_symbol.setdefault(symbol, row)

    day_ms = int(datetime.date.fromisoformat(to_date).strftime("%s")) * 1000
    wanted: dict[str, list] = {}
    skipped: collections.Counter = collections.Counter()

    for segment in built_segments(context):
        try:
            underlyings = read_segment_symbols(
                segment, "segment_underlying_trading_symbols", SETTINGS_ROOT,
            )
        except Exception:
            skipped[f"{segment} declares no underlyings"] += 1
            continue
        chosen: list = []
        for underlying in underlyings:
            if len(chosen) >= per_segment:
                break
            row = by_symbol.get(underlying)
            if row is None:
                skipped[f"no master row for {underlying}"] += 1
                continue
            spot = prints_of_instrument(row)
            if len(spot) < minimum_prints:
                skipped[f"{underlying} has no history for {to_date}"] += 1
                continue
            if segment_trades_the_underlying_itself(segment):
                chosen.append((row["instrument_key"], row, spot))
                continue
            # An options segment: the nearest unexpired expiry, at the strikes
            # closest to where the underlying actually closed that day.
            close = spot[-1][1]
            for contract in contracts_nearest_the_money(master, underlying, close, day_ms):
                if len(chosen) >= per_segment:
                    break
                prints = prints_of_instrument(contract)
                if len(prints) < minimum_prints:
                    skipped[f"{contract.get('trading_symbol')} never traded that day"] += 1
                    continue
                chosen.append((contract["instrument_key"], contract, prints))
        if chosen:
            wanted[segment] = chosen
    # Which source actually served, reported separately from what was skipped: a
    # count of prices is not a reason an instrument was dropped, and printing it
    # under that heading is the kind of mislabelled number this project keeps
    # finding. A replay whose prices came from somewhere other than it thinks is
    # a replay whose result means something else, so this is carried out with
    # the result rather than left in a local.
    return wanted, skipped, dict(sources_used)


def segment_trades_the_underlying_itself(segment: str) -> bool:
    """Whether this segment trades the share itself rather than an option on it.

    Asked of the segment's own declared instrument types rather than of its name,
    so a segment renamed or added does not need this function edited. Those types
    are this project's own vocabulary -- "option", "spot", "dated-future" -- not
    the broker's CE/PE codes, which is the distinction that made the first
    version of this hand an options segment its own underlying to replay.
    """
    from runtime.segment_settings import instrument_types_this_segment_trades
    from runtime.trading_types import OPTION

    try:
        types = instrument_types_this_segment_trades(segment, SETTINGS_ROOT)
    except Exception:
        # A segment that will not say what it trades gets its underlying, which
        # is the reading that replays something real rather than nothing.
        return True
    return OPTION not in types


def contracts_nearest_the_money(master: dict, underlying: str, close: float,
                                day_ms: int) -> list:
    """That underlying's nearest unexpired chain, strikes closest to `close` first.

    Nearest expiry because that is where the volume is, and unexpired *as of the
    day being replayed* rather than as of today: replaying 2025 with today's
    nearest expiry would ask for a contract that did not exist yet.
    """
    chain = [
        row for row in master.values()
        if row.get("underlying_symbol") == underlying
        and row.get("instrument_type") in ("CE", "PE")
        and row.get("expiry") and row["expiry"] > day_ms
        and row.get("strike_price")
    ]
    if not chain:
        return []
    nearest = min(row["expiry"] for row in chain)
    at_that_expiry = [row for row in chain if row["expiry"] == nearest]
    at_that_expiry.sort(key=lambda row: abs(row["strike_price"] - close))
    return at_that_expiry


def contracts_for_each_segment(day: str, per_segment: int, minimum_prints: int) -> dict:
    """The busiest instruments each built segment actually owns, that day.

    Every tape directory is resolved through the real master to (kind,
    underlying), and the segment is then asked of `segment_that_trades` -- the
    live spine's own classifier, not a second copy of the rule. An instrument no
    segment claims is counted and dropped, which is the honest outcome for a
    commodity or currency contract the feed captured and no bot here trades.
    """
    from runtime.segment_settings import built_segments, segment_that_trades

    context = SettingsContext()
    master = instruments_by_key()
    derived_membership = derived_membership_for(master)
    wanted = {segment: [] for segment in built_segments(context)}
    skipped = collections.Counter()

    for directory in TAPE.iterdir():
        if not (directory / f"{day}.index").exists():
            continue
        row = master.get(directory.name)
        if row is None:
            skipped["not in the instrument master"] += 1
            continue
        kind = KIND_OF_INSTRUMENT_TYPE.get(row.get("instrument_type"))
        if kind is None:
            skipped[f"instrument_type {row.get('instrument_type')}"] += 1
            continue
        underlying = row.get("underlying_symbol") or row.get("trading_symbol")
        try:
            segment = segment_that_trades(
                kind, underlying, context, derived_membership=derived_membership
            )
        except Exception as refusal:
            skipped[f"claim refused: {refusal}"] += 1
            continue
        if segment is None:
            skipped[f"no built segment trades {kind} on {underlying}"] += 1
            continue
        wanted[segment].append((directory, row))

    chosen = {}
    for segment, candidates in wanted.items():
        scored = []
        for directory, row in candidates:
            prints = prints_of(directory, day)
            if len(prints) < minimum_prints:
                continue
            scored.append((directory.name, row, prints))
        scored.sort(key=lambda entry: -len(entry[2]))
        chosen[segment] = scored[:per_segment]
    return chosen, skipped


def size_for(
    segment_settings, price: float, lot_size: float, freeze_quantity: float = 0.0,
) -> tuple[float, str]:
    """How many units one trade takes, under this segment's own capital bounds.

    Whole lots, because a venue does not fill a third of one. The bounds are the
    segment's own `minimum_capital_per_trade` and `maximum_capital_per_trade` --
    the same two numbers `trade-capital-bounds-gate` refuses against live, read
    from the same files, so a replay cannot size a trade the live path would
    have refused.

    **And no more units than the exchange takes in one order** (2026-09-12).
    This is a second sizing path beside the live one, and it reproduced the live
    path's own defect independently: it snapped to whole lots and read no
    `freeze_quantity`, so the replay of 2026-09-08 sized 17,355 units of a NIFTY
    contract NSE caps at 1,755 and 38,350 of an HDFCBANK one. Every rupee of
    profit and loss a replay reports at such a size is priced at a fill no venue
    would have given, which makes the whole result a fiction of exactly the kind
    RL-063 exists to prevent -- and a replay that cannot be trusted is worse than
    none, because it is convincing.

    Zero means the master stated no freeze quantity, which is not the same as no
    limit; it is left unbound in that case and said so, rather than guessed.
    """
    minimum = float(segment_settings.read_value("minimum_capital_per_trade"))
    maximum = float(segment_settings.read_value("maximum_capital_per_trade"))
    cost_of_one_lot = price * lot_size
    if cost_of_one_lot > maximum:
        return 0.0, (
            f"one lot costs {cost_of_one_lot:,.2f}, above this segment's "
            f"maximum_capital_per_trade of {maximum:,.2f}"
        )
    lots = int(maximum // cost_of_one_lot)
    if freeze_quantity > 0:
        lots_the_exchange_takes = int(freeze_quantity // lot_size)
        if lots_the_exchange_takes <= 0:
            return 0.0, (
                f"one {lot_size:g}-unit lot is already above the {freeze_quantity:g} this "
                f"exchange accepts in a single order"
            )
        lots = min(lots, lots_the_exchange_takes)
    committed = lots * cost_of_one_lot
    if committed < minimum:
        return 0.0, (
            f"{lots} whole lot(s) commit {committed:,.2f}, below this segment's "
            f"minimum_capital_per_trade of {minimum:,.2f}"
            + (
                f" -- capped at the {freeze_quantity:g} units this exchange takes in one order"
                if freeze_quantity > 0 and lots * lot_size >= freeze_quantity - lot_size
                else ""
            )
        )
    return lots * lot_size, ""


def busiest_option_contracts(day: str, wanted: int) -> list[tuple[str, list]]:
    """The contracts that actually traded, most prints first.

    Chosen by how much they printed rather than by name: a contract picked by
    hand is a contract picked because it worked.
    """
    scored = []
    for directory in sorted(TAPE.glob("NSE_FO|*")):
        prints = prints_of(directory, day)
        if len(prints) < 200:
            continue
        scored.append((directory.name, prints))
    scored.sort(key=lambda entry: -len(entry[1]))
    return scored[:wanted]


class TapeClock:
    """The captured session's own time, for the parts that stamp `now`.

    `paper-fill-simulator` stamps a fill `filled_at_ns=self._now_ns()` and
    `position-close-detector` stamps `closed_at_ns=self._now_ns()`. Live that is
    exactly right. In a replay it is the wall clock of the machine running the
    replay, so a round trip that took forty minutes of real market time was
    recorded as having been held for the few **milliseconds** the replay took to
    walk its prints.

    Nothing noticed while the replay stopped at `position-close-detector`,
    because net PnL does not depend on the clock. The learning half does:
    `luck-skill-separator` scales a symbol's volatility to the horizon actually
    held, so a millisecond hold made the expected noise vanish and every trade
    read as thousands of standard deviations from it. Measured 2026-09-06,
    before this existed: standardised outcomes of -1,958 and -5,689 where the
    honest figures are single digits.

    So the replay hands those parts the tape's clock instead. It advances to
    each print as that print is walked, which is what "now" means to a part
    replaying a captured session.
    """

    def __init__(self, at_ns: int = 0) -> None:
        self._at_ns = at_ns

    def advance_to(self, at_ns: int) -> None:
        # Never backwards: the tape is walked in order, and a clock that went
        # back would make a holding period negative, which reads as a trade that
        # closed before it opened.
        self._at_ns = max(self._at_ns, at_ns)

    def __call__(self) -> int:
        return self._at_ns


class ReplayChain:
    """The six parts that turn a fill into a closed trade, wired as the live
    spine wires them. Mirrors the integration test's own chain deliberately --
    this is a proof about captured data, not a second implementation."""

    def __init__(self, settings, quantity_increment: float, replayed_day: str,
                 segment: str = "", kind: str = OPTION, clock: "TapeClock | None" = None) -> None:
        # The captured session's clock, not this machine's. See TapeClock.
        self.clock = clock or TapeClock()
        # Which of Upstox's two charge stacks this segment's fills pay. Passed
        # the same way the live runner passes it, so a replayed cash-equity
        # trade is charged 0.025% STT on turnover rather than the options
        # stack's 0.1% on premium -- the defect this replay would otherwise
        # have reported as a real net.
        self.segment = segment
        self.book = PaperFillSimulator(
            taker_fee_rate=number(settings, "taker_fee_rate"),
            maker_fee_rate=number(settings, "maker_fee_rate"),
            options_flat_brokerage=number(settings, "options_flat_brokerage"),
            options_stt_sell_rate=number(settings, "options_stt_sell_rate"),
            options_exchange_transaction_charge_rate=number(
                settings, "options_exchange_transaction_charge_rate"),
            options_ipft_charge_rate=number(settings, "options_ipft_charge_rate"),
            options_stamp_duty_buy_rate=number(settings, "options_stamp_duty_buy_rate"),
            options_gst_rate=number(settings, "options_gst_rate"),
            equity_intraday_rates={
                "flat_brokerage": number(settings, "equity_intraday_flat_brokerage"),
                "brokerage_rate": number(settings, "equity_intraday_brokerage_rate"),
                "stt_sell_rate": number(settings, "equity_intraday_stt_sell_rate"),
                "exchange_transaction_charge_rate": number(
                    settings, "equity_intraday_exchange_transaction_charge_rate"),
                "ipft_charge_rate": number(settings, "equity_intraday_ipft_charge_rate"),
                "stamp_duty_buy_rate": number(
                    settings, "equity_intraday_stamp_duty_buy_rate"),
                "sebi_charge_rate": number(settings, "equity_intraday_sebi_charge_rate"),
                "gst_rate": number(settings, "equity_intraday_gst_rate"),
            },
            kinds_by_segment={segment: kind} if segment else None,
            now_ns=self.clock,
        )
        # The live half of the order fork, added 2026-09-12. Every order this
        # replay makes is a PAPER order, so the router refuses each one at its
        # first gate and nothing reaches a broker -- which is the point of
        # running it here. A replay that showed the paper book filling while the
        # live router stayed silent would not prove the fork works; it would
        # only prove the paper half does.
        #
        # **Its three transports raise if they are ever called.** A replay runs
        # unattended against real captured data and must not be one bad
        # conditional away from placing an order at Upstox. If a gate ever fails
        # open, this replay stops with a traceback naming the endpoint rather
        # than quietly reaching it (RL-071: a replay is never what a real
        # decision is made from).
        def a_replay_must_never_reach_the_broker(*arguments):
            raise AssertionError(
                "broker-order-router tried to call Upstox from a REPLAY. Every "
                "replayed order is a paper order and must be refused at the "
                "router's first gate; reaching this line means a gate failed open."
            )

        self.router = BrokerOrderRouter(
            adapter=UpstoxAdapter(),
            place=a_replay_must_never_reach_the_broker,
            cancel=a_replay_must_never_reach_the_broker,
            modify=a_replay_must_never_reach_the_broker,
            # The segment's real money mode, read from its own settings file --
            # the same source money-mode-reader uses live. Not hardcoded to
            # paper: if an operator sets a segment live, this replay should show
            # the router refusing for the NEXT reason instead, not keep claiming
            # the first one.
            read_money_mode=lambda name: money_mode_of(name),
            read_instrument_key=lambda symbol: None,
            read_token=lambda: None,
            product=str(settings.read_value("broker_order_product")),
            validity=str(settings.read_value("broker_order_validity")),
            now_ns=self.clock,
        )
        # Every status the router returned this replay. Collected rather than
        # counted, so the summary can say WHICH refusal fired rather than only
        # how many -- a router refusing for the wrong reason produces the same
        # count as one refusing correctly.
        self.refusals: list = []
        self.book.observe_session(
            MarketSessionState(
                segment="NSE_EQ" if kind == SPOT else "NSE_FO",
                kind=SessionKind.OPEN,
                as_of_date=datetime.date.fromisoformat(replayed_day),
                # The session is stated as open because the captured prints are
                # themselves the evidence it was: a print exists only because the
                # market traded. Same reasoning paper-fill-simulator already
                # applies to a historical bar close, and the date it is stated
                # for is the day being replayed, never today.
                reason=f"replaying the captured session of {replayed_day}",
                observed_at_ns=0,
            )
        )
        self.reconciler = FillReconciler(quantity_tolerance=quantity_increment)
        self.cost_basis = CostBasisTracker(quantity_increment)
        self.excursions = PeakExcursionTracker()
        self.chainer = ExitOrderChainer()
        self.stops = StopOrderManager()
        self.closes = PositionCloseDetector(quantity_increment, now_ns=self.clock)
        self.closed_trades = []
        self.exit_orders_sent = []
        self.positions_held: dict = {}
        # Every fill this replay actually produced, in order. The learning half
        # attributes a closed trade across its own fills, and until 2026-09-06
        # nothing here kept them: the replay stopped at position-close-detector.
        self.fills = []

    def apply_fill(self, fill) -> None:
        self.fills.append(fill)
        position = self.reconciler.observe_fill(fill)
        self.cost_basis.observe_fill(fill)
        self.excursions.observe_position(position)
        key = (fill.venue_id, fill.symbol)
        was_held = self.positions_held.get(key, 0.0)
        self.positions_held[key] = position.quantity

        trade = self.closes.observe_fill(fill)
        if trade is not None:
            self.closed_trades.append(trade)

        exits = self.chainer.observe_entry_fill(
            fill_id=fill.fill_id,
            entry_order_id=fill.order_id or fill.fill_id,
            venue_id=fill.venue_id,
            symbol=fill.symbol,
            entry_side=fill.side,
            filled_quantity=fill.quantity,
        )
        if exits is not None and exits.should_be_sent:
            self.send_exits(exits)

        if was_held and position.is_flat:
            for cancel in self.stops.observe_position_closed(*key):
                self.exit_orders_sent.append(cancel)
                self.book.simulate(segment=self.segment,
                                   **as_paper_order(as_order_request(cancel)))

    def send_exits(self, exits) -> None:
        # The closing leg pays the same stack as the opening one. Without the
        # segment on these orders the exit would fall through to the options
        # stack and a cash-equity round trip would be charged two different
        # fee models -- which is worse than being charged one wrong one,
        # because the error would not be a constant.
        direction = LONG if exits.exit_side == SELL else "short"
        for action in (
            self.stops.apply_adjustment(
                venue_id=exits.venue_id, symbol=exits.symbol, direction=direction,
                quantity=exits.quantity, stop_price=exits.stop_price, money_mode=PaperMode(),
            ),
            self.stops.place_target(
                venue_id=exits.venue_id, symbol=exits.symbol, direction=direction,
                quantity=exits.quantity, target_price=exits.target_price, money_mode=PaperMode(),
            ),
        ):
            if not action.is_actionable:
                continue
            self.exit_orders_sent.append(action)
            self.book.simulate(segment=self.segment, **as_paper_order(as_order_request(action)))

    def observe_price(self, symbol: str, price: float, at_ns: int) -> None:
        # Before anything else: a fill this print causes is stamped with this
        # print's own time, which is what makes a replayed holding period real.
        self.clock.advance_to(at_ns)
        self.excursions.observe_price(VENUE, symbol, price, observed_at_ns=at_ns)
        for result in self.book.evaluate_resting({(VENUE, symbol): price}):
            if result.did_fill:
                excursion = self.excursions.read(VENUE, symbol)
                if excursion is not None:
                    self.closes.observe_excursion(
                        VENUE, symbol, excursion.best_unrealised, excursion.worst_unrealised,
                    )
                self.apply_fill(result.fill)


def daily_volatility_of(prints: list[tuple[int, float]]) -> float | None:
    """The contract's own realised volatility for the day, from its own prints.

    `luck-skill-separator` compares a trade's return against the noise the
    symbol produces on its own, and it wants that as a **daily fraction** --
    `expected_noise` scales it to the holding period by the square root of time,
    and `assess` divides `realised_pnl / notional` by the result. So this is
    return-space, never rupees.

    Measured, not stated: the standard deviation of the contract's own
    print-to-print returns, scaled up by the square root of how many of those
    intervals fit a session. A contract with fewer than two returns has no
    dispersion to measure and gets None, which the separator reports as
    "this symbol's volatility has never been measured" rather than as a zero.
    """
    returns = [
        (later / earlier) - 1.0
        for (_, earlier), (_, later) in zip(prints, prints[1:])
        if earlier > 0
    ]
    if len(returns) < 2:
        return None
    per_print = statistics.stdev(returns)
    first_ns, last_ns = prints[0][0], prints[-1][0]
    covered_seconds = (last_ns - first_ns) / 1e9
    if covered_seconds <= 0:
        return None
    seconds_per_print = covered_seconds / len(returns)
    if seconds_per_print <= 0:
        return None
    return per_print * math.sqrt(SECONDS_IN_AN_NSE_SESSION / seconds_per_print)


class LearningReplay:
    """The closed-trade chain, run on a replayed trade -- the half the replay never had.

    Until 2026-09-06 this script imported seven parts and stopped at
    `position-close-detector`. The 2026-09-04 replay closed 252 trades and not
    one of them reached `pnl-attributor`, so everything downstream of
    `closed-trade` had never run on a real trade at all -- which is why
    `edge-graduation-gate` reads `judgements 0` on the live spine and
    `bot-maturity` has never been produced for any of its five consumers.

    What is real here, on the same terms the module docstring already sets:

    - **Real**: the fills are this replay's own, priced by Upstox's real charge
      stack. The attribution is computed across them. The entry-quality window
      is the captured prints in the `entry_quality_window` seconds after the
      entry -- the window the decision could actually have acted in, which is
      what the scorer means by it. The significance is the trade's own return
      over the contract's own measured daily volatility.
    - **Stated**: the detector, the regime and the opinion's stated probability
      on the scorecard half. The replay opens at the first print by
      construction, so no detector fired and no bot stated a conviction. Those
      are named `replay-opened-at-the-first-print` and `unclassified-in-replay`
      so nothing reads as measured that was not, and the two that ARE real --
      whether the trade made money, and how much -- are what the scorecard is
      actually built from.

    **`edge-graduation-gate` is deliberately not run.** It needs a decision
    quality, a refutation verdict, a trial verdict and a coverage report, and a
    replay can produce none of the four. Driving it would mean inventing all
    four and calling the result a graduation, which is exactly the fabrication
    the encoder's own refusal exists to prevent. What it would need is printed
    instead.
    """

    def __init__(self, settings) -> None:
        self.attributor = PnlAttributor(
            reconciliation_tolerance=number(settings, "pnl_reconciliation_tolerance"),
        )
        self.entry_scorer = EntryQualityScorer(
            window_seconds=number(settings, "entry_quality_window"),
            minimum_prices=int(number(settings, "entry_quality_minimum_prices")),
            chasing_in_typical_movements=number(settings, "entry_quality_chasing_movements"),
        )
        self.separator = LuckSkillSeparator(
            significance_threshold=number(settings, "luck_significance_threshold"),
            minimum_comparable_outcomes=int(
                number(settings, "luck_minimum_comparable_outcomes")),
        )
        self.encoder = TradeEpisodeEncoder()
        # The replay states no conviction, so the "stated probability" it
        # records is the scorekeeper's own prior -- the number that means "no
        # information", rather than one invented here.
        self.stated_probability = number(settings, "learning_prior_hit_rate")
        self.scorekeeper = BotScorekeeper(
            prior_hit_rate=number(settings, "learning_prior_hit_rate"),
            prior_weight=number(settings, "learning_prior_weight"),
            half_life_observations=number(settings, "learning_half_life_observations"),
            minimum_observations=int(number(settings, "learning_minimum_observations")),
        )
        self.episodes = 0
        self.refused = collections.Counter()
        # label-builder, added 2026-09-12. It is the part that turns a closed
        # trade into what a model learns from, and it had never seen one: it
        # joined the live spine at 09:56 on 2026-09-08, after the last trade of
        # that session closed, so its standing has read `trades_seen 0` ever
        # since. `training-label` is consumed by both conviction models, both
        # setup-weight learners, three detectors and the retrain scheduler -- so
        # the wire from a realised trade back to every model exists and has
        # simply never carried anything.
        #
        # A replay can drive it honestly, and is the only thing that can while
        # the market is shut. All three of its inputs are real here: the
        # excursion is the best and worst print while the position was actually
        # open, the cost is this trade's own Upstox charge stack over its own
        # notional, and the stop distance is the one the replay really rested --
        # `stop_multiple` times the contract's own typical move. That last one
        # matters most: without a stop distance `THE_SIZE_WAS_RIGHT` is not
        # judgeable at all.
        self.labeller = LabelBuilder(
            favourable_threshold=number(settings, "label_favourable_threshold"),
            adverse_entry_threshold=number(settings, "label_adverse_entry_threshold"),
            exit_capture_threshold=number(settings, "label_exit_capture_threshold"),
            size_survival_multiple=number(settings, "label_size_survival_multiple"),
        )
        self.label_horizon_seconds = (
            number(settings, "spread_reversion_horizon")
            * number(settings, "bull_exit_conviction_horizon_multiple")
        )
        self.labels_built = 0
        self.labels_refused = collections.Counter()
        self.label_components = collections.Counter()

    def label(self, trade_id: str, closed_trade, held_prints, stop_price) -> dict | None:
        """`label-builder` on one finished round trip, from this replay's own facts.

        Returns what the label said per component, or None with the reason it
        could not be built -- which is itself a real answer and is counted.
        """
        entry = closed_trade.entry_price
        if not entry or not held_prints:
            self.labels_refused["no entry price or no print while it was held"] += 1
            return None

        prices = [price for _at_ns, price in held_prints]
        excursion = ExcursionRecord(
            peak_favourable_fraction=max(0.0, (max(prices) - entry) / entry),
            peak_adverse_fraction=max(0.0, (entry - min(prices)) / entry),
            # The replay walks prints in order and stops at the first exit, so
            # it knows the sequence but not which print was the peak in time.
            # Left at zero rather than guessed: nothing in the label reads them.
            seconds_to_peak_favourable=0.0,
            seconds_to_peak_adverse=0.0,
            observations=len(prices),
        )
        self.labeller.observe_excursion(
            VENUE, closed_trade.symbol, closed_trade.opened_at_ns, excursion,
        )
        # This trade's own round trip, from the charge stack that really priced
        # its fills -- not a rate from settings. A setup that is right only
        # before fees is not right.
        notional = abs(closed_trade.quantity) * entry
        if notional <= 0:
            self.labels_refused["the trade had no notional to charge costs against"] += 1
            return None
        self.labeller.observe_cost_estimate(
            VENUE, closed_trade.symbol, closed_trade.fees_paid / notional,
        )

        record = ClosedTradeRecord(
            venue_id=VENUE, symbol=closed_trade.symbol,
            detector=THE_REPLAY_HAS_NO_DETECTOR, regime=THE_REPLAY_HAS_NO_REGIME,
            side=closed_trade.direction,
            entry_price=entry, exit_price=closed_trade.exit_price,
            quantity=closed_trade.quantity,
            opened_at_ns=closed_trade.opened_at_ns,
            closed_at_ns=closed_trade.closed_at_ns,
            horizon_seconds=self.label_horizon_seconds,
            features={},
            # The stop this replay really rested, as a fraction of the entry.
            stop_distance_fraction=(
                abs(entry - stop_price) / entry if stop_price and entry else 0.0
            ),
        )
        built, reason = self.labeller.build(record)
        if built is None:
            self.labels_refused[reason] += 1
            return None
        self.labels_built += 1
        for component, value in built.labels.items():
            self.label_components[f"{component}:{'true' if value else 'false'}"] += 1
        return dict(built.labels)

    def decode(self, trade_id: str, closed_trade, fills, prints, entry_at_ns) -> dict:
        """One closed trade, all the way to an episode and a scorecard entry."""
        for fill in fills:
            self.attributor.observe_fill(trade_id, fill)
        self.attributor.observe_decision_price(trade_id, closed_trade.entry_price)
        attribution = self.attributor.attribute(trade_id, closed_trade)

        # The signal existed when the replay decided to open, and the window is
        # the prints after it -- "the window the decision could have acted in".
        self.entry_scorer.observe_signal_time(trade_id, entry_at_ns)
        for at_ns, price in prints:
            self.entry_scorer.observe_price(VENUE, closed_trade.symbol, price, at_ns)
        typical = typical_movement_of(prints)
        if typical is not None:
            self.entry_scorer.observe_typical_movement(VENUE, closed_trade.symbol, typical)
        entry_quality = self.entry_scorer.score(trade_id, closed_trade)

        volatility = daily_volatility_of(prints)
        if volatility is not None:
            self.separator.observe_daily_volatility(VENUE, closed_trade.symbol, volatility)
        significance = self.separator.assess(trade_id, closed_trade)

        # Each part publishes the inner value, never its own wrapper, so this is
        # what the encoder would receive on the bus.
        encoded = self.encoder.encode(
            trade_id, closed_trade,
            detector=THE_REPLAY_HAS_NO_DETECTOR,
            action="bought",
            outcome="target" if closed_trade.realised_pnl > 0 else "stop",
            attribution=attribution.attribution if attribution.is_usable else None,
            entry_quality=entry_quality.quality if entry_quality.is_usable else None,
            significance=significance.significance if significance.is_usable else None,
        )
        if encoded.episode is not None:
            self.episodes += 1
        for missing in encoded.missing:
            self.refused[missing] += 1

        won = closed_trade.realised_pnl > 0
        self.scorekeeper.record_opinion_outcome(
            bot="bull",
            detector=THE_REPLAY_HAS_NO_DETECTOR,
            regime=THE_REPLAY_HAS_NO_REGIME,
            # Stated, and deliberately the prior: the replay states no conviction,
            # and any other number would be one invented here.
            stated_probability=self.stated_probability,
            the_opinion_was_right=won,
            realised=closed_trade.realised_pnl,
        )
        return {
            # The captured session's own holding period, which only became a
            # real number when the replay stopped stamping fills with the wall
            # clock (see TapeClock). Everything scaled by a horizon depends on
            # it, so it is carried out with the result rather than trusted.
            "holding_seconds": closed_trade.holding_seconds,
            "attribution": attribution.state,
            "entry_quality": entry_quality.state,
            "significance": significance.state,
            "episode": encoded.state,
            "episode_missing": list(encoded.missing),
            "daily_volatility_measured": volatility,
            "standardised_outcome": significance.significance.standardised,
        }


def typical_movement_of(prints: list[tuple[int, float]]) -> float | None:
    """How far this contract usually moves between prints, in price.

    What `entry-quality-scorer` measures chasing against. Measured from the
    contract's own captured prints -- the median absolute move -- so a thin
    contract is judged against itself rather than against a busy one.
    """
    moves = [abs(later - earlier) for (_, earlier), (_, later) in zip(prints, prints[1:])]
    moves = [move for move in moves if move > 0]
    return statistics.median(moves) if moves else None


class PaperMode:
    mode = "paper"


def as_paper_order(request) -> dict:
    return {
        "client_order_id": request.client_order_id,
        "venue_id": request.venue_id,
        "symbol": request.symbol,
        "side": request.side,
        "quantity": request.quantity,
        "order_type": request.order_type,
        "limit_price": request.limit_price or None,
        "money_mode": "paper",
        "is_in_flight": False,
        "fill_price_estimate": None,
        "market_price": None,
        "stop_price": request.trigger_price,
        "cancels_client_order_id": request.cancels_client_order_id,
    }


def stop_and_target_for(prints: list[tuple[int, float]], entry: float,
                        stop_multiple: float, target_multiple: float) -> tuple[float, float]:
    """Where the exits sit, as multiples of this contract's own typical move.

    The live path takes these from `stop-target-placer`, which needs a
    volatility forecast or excursion history that a cold replay does not have.
    Derived instead from the contract's own captured session so the distance is
    a property of the instrument being traded rather than a number chosen to
    make the replay close -- an option whose premium swings 8% a minute and one
    that barely moves must not be given the same stop.

    The typical move is the median absolute change between consecutive prints,
    not the mean: a single fat print would drag a mean and quietly widen every
    exit that followed it.
    """
    moves = [abs(later - earlier) for (_, earlier), (_, later) in zip(prints, prints[1:])]
    moves = [move for move in moves if move > 0]
    if not moves:
        raise ValueError("this contract never changed price; there is nothing to trade against")
    typical = statistics.median(moves)
    return entry - stop_multiple * typical, entry + target_multiple * typical


def replay_one_contract(name: str, prints: list, settings, lot_size: float,
                        stop_multiple: float, target_multiple: float, day: str,
                        segment: str = "", kind: str = OPTION,
                        quantity: float | None = None,
                        learning: "LearningReplay | None" = None) -> dict:
    """Open at the first print, rest the exits, then walk every later print."""
    entry_at_ns, entry_price = prints[0]
    # Started at the entry print: the opening fill is stamped before any later
    # print is walked, and a fill stamped 0 would make the holding period the
    # whole epoch.
    chain = ReplayChain(settings, quantity_increment=lot_size, replayed_day=day,
                        segment=segment, kind=kind, clock=TapeClock(entry_at_ns))
    traded_quantity = lot_size if quantity is None else quantity
    stop_price, target_price = stop_and_target_for(
        prints, entry_price, stop_multiple, target_multiple)

    chain.chainer.register_plan(VENUE, name, BUY, stop_price, target_price)

    # The same order offered to the LIVE router first, exactly as
    # order-destination-router offers it live. It is addressed to the paper book
    # because this segment's money mode says paper, so the router refuses it at
    # its first gate and the book below fills it -- which is the fork working.
    # The refusal is collected rather than discarded: a live path that is wired
    # and shut has to be visible as that, not as silence.
    chain.refusals.append(chain.router.route(types.SimpleNamespace(
        venue_id=VENUE, symbol=name, side=BUY, quantity=traded_quantity,
        destination=PAPER_BOOK, segment=segment, order_type="market",
        limit_price=0.0, intent_id=f"replay-{name}",
    )))

    opened = chain.book.simulate(
        client_order_id=f"replay-{name}", venue_id=VENUE, symbol=name, side=BUY,
        quantity=traded_quantity, order_type=MARKET, limit_price=None, money_mode="paper",
        is_in_flight=False, fill_price_estimate=None, market_price=entry_price,
        stop_price=None, segment=segment,
    )
    if opened.outcome != FILLED:
        return {
            "symbol": name, "opened": False, "why": opened.reason,
            "router_statuses": chain.refusals,
        }
    chain.apply_fill(opened.fill)

    walked = 0
    for at_ns, price in prints[1:]:
        chain.observe_price(name, price, at_ns)
        walked += 1
        if chain.closed_trades:
            break

    if not chain.closed_trades:
        return {
            "symbol": name, "opened": True, "closed": False, "prints_walked": walked,
            "entry_price": entry_price, "stop_price": stop_price,
            "target_price": target_price,
            "why": "neither the stop nor the target was reached in the captured session",
            "router_statuses": chain.refusals,
        }

    closed = chain.closed_trades[0]
    # The learning half, on this replay's own fills and prints. Optional so the
    # trading half can still be replayed alone, and never able to change what
    # the trading half decided -- it reads a finished round trip.
    decoded = None
    if learning is not None:
        decoded = learning.decode(
            trade_id=f"{day}:{name}", closed_trade=closed, fills=chain.fills,
            prints=prints, entry_at_ns=entry_at_ns,
        )
        # Only the prints the position was actually open across. The whole
        # captured series would include prices after the exit, and an excursion
        # measured over those is not one this trade ever lived through.
        decoded["label"] = learning.label(
            trade_id=f"{day}:{name}", closed_trade=closed,
            held_prints=prints[1:walked + 1], stop_price=stop_price,
        )
    # Which of Upstox's two charge stacks actually priced these fills, carried
    # out with the result rather than trusted. A cash-equity trade priced by the
    # options stack still produces a plausible net -- twice the real cost on a
    # MARUTI round trip, measured 2026-09-05 -- so "the fees are right" is a
    # claim that has to be shown, not assumed from the kind being passed in.
    standing = chain.book.standing
    return {
        "priced_as_options": standing.fills_priced_as_options,
        "priced_as_equity": standing.fills_priced_as_equity,
        "priced_by_the_fallback_stack": standing.fills_priced_by_the_fallback_stack,
        "symbol": name,
        "opened": True,
        "closed": True,
        "prints_walked": walked,
        "quantity": closed.quantity,
        "entry_price": closed.entry_price,
        "exit_price": closed.exit_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "fees_paid": closed.fees_paid,
        "realised_pnl": closed.realised_pnl,
        "net_pnl": closed.realised_pnl - closed.fees_paid,
        "direction": closed.direction,
        "opened_at_ns": entry_at_ns,
        "decoded": decoded,
        "router_statuses": chain.refusals,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", default=None,
                        help="the captured day to replay, YYYY-MM-DD")
    parser.add_argument("--per-segment", type=int, default=8,
                        help="how many of each segment's busiest instruments to replay")
    parser.add_argument("--minimum-prints", type=int, default=200,
                        help="how many prints an instrument needs before it is worth replaying")
    parser.add_argument("--stop-multiple", type=float, default=8.0,
                        help="stop distance, in multiples of the instrument's own typical move")
    parser.add_argument("--target-multiple", type=float, default=12.0,
                        help="target distance, in the same units")
    parser.add_argument(
        "--from-history", metavar="YYYY-MM-DD", default=None,
        help=(
            "replay a past session from Upstox's own one-minute history instead of "
            "from the captured tape. The tape is three days deep and holds only "
            "what the feed was subscribed to; Upstox serves minute bars from "
            "January 2022. A bar close is one price a minute, not every print, so "
            "the tape stays the finer evidence where it exists"
        ),
    )
    arguments = parser.parse_args()
    # Whether the operator asked for a tape day, as opposed to the default that
    # is filled in below. Without this, "no flags" and "--day today" would be
    # indistinguishable and the market-closed rule could never fire.
    arguments.day_was_given = arguments.day is not None
    if arguments.day is None:
        arguments.day = time.strftime("%Y-%m-%d")

    settings = load_settings_document(settings_directory() / "runtime.toml", "runtime")

    # **History is the default whenever the market is shut** (operator,
    # 2026-09-06). The tape holds only the days this machine happened to be
    # capturing, and outside market hours there is nothing live to replay
    # either -- so a run started on a weekend or an evening would otherwise
    # replay a snapshot of a closed market and prove nothing. Asked of
    # `market-session-calendar`, the live spine's own answer, and an unmeasured
    # session counts as shut (Rule 8).
    from_history = arguments.from_history
    if from_history is None and not arguments.day_was_given:
        if the_market_is_open_now() is not True:
            from_history = the_most_recent_weekday_before_today()
            print(
                "The market is not open, so this replays real history rather than a "
                f"captured snapshot of a shut market: {from_history}.\n"
                "Pass --day to replay the tape instead.\n"
            )

    if from_history:
        replayed_day = from_history
        by_segment, skipped, priced_by = contracts_from_history(
            replayed_day, replayed_day, arguments.per_segment, arguments.minimum_prints)
        print(f"Replaying {replayed_day} from Upstox's own one-minute history, "
              "one bot per segment.")
        print("What each segment trades is asked of the segment's own "
              "segment_underlying_trading_symbols; the option contracts are its "
              "underlying's nearest unexpired expiry at the strikes closest to "
              "where that underlying actually closed.")
        print("A bar close is one price a minute, not every print -- so a stop and a "
              "target inside one minute's range both look reachable and only the "
              "close decides. The tape is the finer evidence where it exists.\n")
    else:
        replayed_day = arguments.day
        by_segment, skipped = contracts_for_each_segment(
            replayed_day, arguments.per_segment, arguments.minimum_prints)
        priced_by = {"the captured tape": sum(len(rows) for rows in by_segment.values())}
        print(f"Replaying the captured tape for {replayed_day}, one bot per segment.")
        print("Which segment owns an instrument is asked of segment_that_trades -- the "
              "live spine's own classifier, not a copy of the rule.\n")

    # One learning chain across every segment, because a scorecard is about a
    # bot and the bot is the same one whichever segment's instrument it traded.
    learning = LearningReplay(settings)

    results: dict[str, list] = {}
    for segment, instruments in by_segment.items():
        segment_settings = load_settings_document(
            settings_directory() / "segments" / f"{segment}.toml", segment)
        kind = KIND_OF_INSTRUMENT_TYPE.get(
            (instruments[0][1].get("instrument_type") if instruments else None), OPTION)
        rows = []
        for name, row, prints in instruments:
            lot_size = float(row.get("lot_size") or 1)
            # The exchange's single-order limit for this contract, from the same
            # master row the lot came from. Zero where it stated none.
            freeze_quantity = float(row.get("freeze_quantity") or 0)
            entry_price = prints[0][1]
            quantity, refused = size_for(
                segment_settings, entry_price, lot_size, freeze_quantity,
            )
            if quantity <= 0:
                rows.append({"symbol": name, "trading_symbol": row.get("trading_symbol"),
                             "opened": False, "why": refused})
                continue
            try:
                replayed = replay_one_contract(
                    name, prints, settings, lot_size,
                    arguments.stop_multiple, arguments.target_multiple, replayed_day,
                    segment=segment,
                    kind=KIND_OF_INSTRUMENT_TYPE.get(row.get("instrument_type"), OPTION),
                    quantity=quantity, learning=learning)
            except ValueError as refusal:
                replayed = {"symbol": name, "opened": False, "why": str(refusal)}
            replayed["instrument_key"] = name
            replayed["trading_symbol"] = row.get("trading_symbol")
            replayed["lot_size"] = lot_size
            replayed["quantity_traded"] = quantity
            replayed["capital_committed"] = quantity * entry_price
            rows.append(replayed)
        results[segment] = rows

    header = (f"{'instrument':<30}{'qty':>8}{'entry':>10}{'exit':>10}"
              f"{'capital':>12}{'fees':>9}{'net':>11}")
    for segment, rows in results.items():
        opened = [r for r in rows if r.get("opened")]
        closed = [r for r in rows if r.get("closed")]
        print(f"=== {segment}   {len(rows)} instrument(s) tried, "
              f"{len(opened)} opened, {len(closed)} closed")
        if not rows:
            print("    nothing on the tape this segment owns printed enough to replay\n")
            continue
        if closed:
            print("    " + header)
            print("    " + "-" * len(header))
            for trade in closed:
                shown = (trade.get("trading_symbol") or trade["symbol"])[:29]
                print(f"    {shown:<30}{trade['quantity_traded']:>8.0f}"
                      f"{trade['entry_price']:>10.2f}{trade['exit_price']:>10.2f}"
                      f"{trade['capital_committed']:>12.0f}{trade['fees_paid']:>9.2f}"
                      f"{trade['net_pnl']:>11.2f}")
            won = sum(1 for t in closed if t["net_pnl"] > 0)
            net = sum(t["net_pnl"] for t in closed)
            print("    " + "-" * len(header))
            print(f"    {won} of {len(closed)} closed in profit; net {net:,.2f} "
                  f"after Upstox's real charge stack for this segment")
        for trade in rows:
            if not trade.get("closed"):
                shown = (trade.get("trading_symbol") or trade["symbol"])[:29]
                print(f"    {shown:<30} did not close: {trade.get('why')}")
        print()

    router_refusals = [
        status
        for rows in results.values()
        for row in rows
        for status in (row.get("router_statuses") or ())
    ]
    closed_everywhere = [r for rows in results.values() for r in rows if r.get("closed")]
    decoded = [r["decoded"] for r in closed_everywhere if r.get("decoded")]
    if router_refusals:
        print("=== the live order path, offered the same orders")
        outcomes = collections.Counter(status.outcome for status in router_refusals)
        for outcome, count in outcomes.most_common():
            print(f"    {count:>4}  {outcome}")
        placed = sum(1 for status in router_refusals if status.outcome == "placed")
        print(f"    {placed} of {len(router_refusals)} reached a broker.")
        print("    Every replayed order is addressed to the paper book, because every")
        print("    built segment's money_mode says paper -- so broker-order-router")
        print("    refuses each one at its first gate and paper-fill-simulator fills")
        print("    it. That is the fork working. The router's transports raise if")
        print("    they are ever called, so a replay that somehow got past a gate")
        print("    would stop with a traceback rather than reach Upstox.\n")

    print("=== the learning half, on the same trades")
    print(f"    {len(decoded)} closed trade(s) decoded, "
          f"{learning.episodes} became a trade-episode")
    for piece, state in (
        ("attribution", "attribution"), ("entry quality", "entry_quality"),
        ("significance", "significance"),
    ):
        states = collections.Counter(row[state] for row in decoded)
        rendered = ", ".join(f"{count} {name}" for name, count in states.most_common())
        print(f"    {piece:<14} {rendered}")
    if learning.refused:
        print("    an episode was refused for a missing piece:")
        for missing, count in learning.refused.most_common():
            print(f"       {count:>4}  {missing} had not landed")
    # label-builder: the chain from a realised trade back to every model that
    # trains on `training-label` -- both conviction models, both setup-weight
    # learners, three detectors and the retrain scheduler. The wire has existed
    # all along and had never carried a single label (2026-09-12).
    print(f"    training-label {learning.labels_built} built of {len(decoded)} closed trade(s)")
    for component, count in sorted(learning.label_components.items()):
        print(f"       {count:>4}  {component}")
    if learning.labels_refused:
        for reason, count in learning.labels_refused.most_common():
            print(f"       {count:>4}  NOT labelled: {reason}")
    if learning.labeller.standing.size_not_judgeable:
        print(f"       {learning.labeller.standing.size_not_judgeable:>4}  "
              f"the-size-was-right could not be judged (no stop distance)")

    card = learning.scorekeeper.scorecard_for("bull").describe()
    for regime, record in card["by_regime"].items():
        print(f"    bot-scorecard  bull/{regime}: "
              f"{record['trades']} trade(s), {record['wins']} win(s)")
    print("    REAL here: the fills, the attribution across them, the entry-quality")
    print("      window of captured prints, the contract's own measured volatility,")
    print("      and all three of the label's inputs -- the excursion is the best and")
    print("      worst print while the position was open, the cost is this trade's own")
    print("      charge stack over its own notional, and the stop distance is the one")
    print("      the replay really rested.")
    print("    STATED: the detector, the regime and the opinion's probability -- the")
    print(f"      replay opens at the first print, so no detector fired ({THE_REPLAY_HAS_NO_DETECTOR}).")
    print("    NOT RUN: edge-graduation-gate. It needs a decision quality, a refutation")
    print("      verdict, a trial verdict and a coverage report; a replay produces none")
    print("      of the four, and inventing them would call a fabrication a graduation.\n")

    if priced_by:
        print("=== where the prices came from")
        for source, count in sorted(priced_by.items()):
            print(f"    {count:>4} instrument(s) priced by {source}")
        print("    Upstox is asked last on purpose: it is the only source with a daily")
        print("    quota, and the only one that can serve an arbitrary past option")
        print("    session. Spending it on a price a free source would have given is")
        print("    spending the thing that cannot be replaced.\n")

    traded = [s for s, rows in results.items() if any(r.get("closed") for r in rows)]
    print(f"Segments that opened AND closed a trade: {len(traded)} of {len(results)}"
          f"  {traded}")
    if skipped:
        print("\nInstruments on the tape that no built segment owns (the five "
              "largest reasons):")
        for reason, count in skipped.most_common(5):
            print(f"   {count:>6}  {reason}")

    REPLAY_ROOT.mkdir(parents=True, exist_ok=True)
    written = REPLAY_ROOT / f"{replayed_day}.by-segment.json"
    written.write_text(json.dumps({
        "replayed_day": replayed_day,
        "is_a_replay": True,
        "source": "captured Upstox tape, instruments resolved against Upstox's real master",
        "stop_multiple_of_typical_move": arguments.stop_multiple,
        "target_multiple_of_typical_move": arguments.target_multiple,
        "replayed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "segments_that_opened_and_closed": traded,
        "by_segment": results,
        "priced_by": priced_by,
        "learning": {
            "closed_trades_decoded": len(decoded),
            "trade_episodes_encoded": learning.episodes,
            "episodes_refused_for_a_missing_piece": dict(learning.refused),
            "bot_scorecard_bull": learning.scorekeeper.scorecard_for("bull").describe(),
            "stated_not_measured": {
                "detector": THE_REPLAY_HAS_NO_DETECTOR,
                "regime": THE_REPLAY_HAS_NO_REGIME,
                "stated_probability": learning.stated_probability,
            },
            "edge_graduation_gate_not_run_because": (
                "it needs a decision quality, a refutation verdict, a trial verdict and "
                "a coverage report, and a replay produces none of the four"
            ),
        },
        "instruments_no_segment_owns": dict(skipped.most_common(20)),
    }, indent=2) + "\n")
    print(f"\nwrote {written}")
    print("Stamped `is_a_replay` and kept out of the spine's state root: these are not "
          "live trades and no board may show them as though they were (RL-071, Rule 8).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
