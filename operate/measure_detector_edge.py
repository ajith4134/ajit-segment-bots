#!/usr/bin/env python3
"""Which detectors have an edge after costs, on sessions they were not chosen on?

The fourth temporary goal (docs/goal.md, 2026-09-16) asked for profit first, proven
across many past sessions, starting with every detector. This drives the detectors'
own classes -- built by the same functions their parts use
(operate/detectors_for_measurement.py) -- over one-minute history, one session after
another, and scores every candidate as the trade the option segments would have made.

**What is real:**

- every price is an Upstox one-minute close, contracts from the expired-instruments
  route (operate/past_session_prints.py);
- the chain the volatility-gap detector hears its forecast through runs as live:
  candle windows, volatility features, the realised-vol regressor learning online;
- each detector's confidence learns from `signal-outcome-labeller` itself, as live;
- the trade is sized by the replay's `size_for` (the segment's own capital bounds and
  NSE's freeze quantity) and charged Upstox's options stack on both legs plus two
  half-spread crossings (`backtest_prior_half_spread_fraction`).

**What is approximated, and printed with every report:**

- **mean-reversion and momentum-burst see one close a minute here, several prints a
  second live.** Their windows span hours here and minutes live, so those rows measure
  the same rule on a slower clock. volatility-gap and zero-to-hero are the faithful rows.
- **A bar's close is acted on when the bar ends**, one interval after its stamp. Acting
  at the stamp would trade on a price a minute before it existed.
- **Implied volatility is inverted from the premium** (runtime/implied_volatility_from_premium.py,
  median 0.004-0.009 from Upstox's own), because history carries no greeks. The
  surface holds the at-the-money figure only; skew is left absent, which the feature
  builder names as missing rather than filling.
- **Delta for zero-to-hero is estimated** (runtime/option_delta.py) from that volatility.
- **`entropy-magnitude-forecaster` is not run.** It needs order-flow entropy that
  one-minute bars cannot give, so only one of the two live forecast producers is heard.
- **What is bought** follows `instrument-selector`: a long on a contract buys it; a
  short on a contract buys the at-the-money contract of the opposite type; a view on
  an underlying buys its at-the-money call or put.
- **The exit is the horizon.** The first close at or after entry plus the candidate's
  own horizon, or the session's last close if the horizon runs past it. No stop, no
  target: this measures the setup, not the exit plan.

**Walk-forward.** Sessions are ordered; the earlier `edge_walk_forward_train_fraction`
choose the buckets (detector, state or direction, regime) whose mean net return has a
lower confidence bound above zero with at least `edge_minimum_trades_per_bucket`
trades. Only the later sessions score them. A number from the sessions a bucket was
chosen on is never reported as its edge.

This is a measurement (RL-071): it publishes nothing and writes only under
`measurements/`.

    .venv/bin/python operate/measure_detector_edge.py --from 2025-09-16 --to 2026-09-15
"""

from __future__ import annotations

import argparse
import bisect
import collections
import dataclasses
import datetime
import json
import math
import pathlib
import statistics
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VENUE = "upstox"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
OUTPUT = ROOT / "measurements" / "2026-09-16-detector-edge-across-sessions"

NOT_MEASURED = {
    "spread-reversion-detector": (
        "needs cointegrated pairs over time-spaced bars, and the spread half-life those "
        "settings wait on is still unmeasured (CLAUDE.md, 2026-09-13)"
    ),
    "universal-symbol-sweeper": (
        "needs watch-conditions, liquidity grades and venue announcements, none of which "
        "one-minute history can rebuild"
    ),
}


@dataclasses.dataclass(frozen=True)
class ScoredCandidate:
    session: str
    detector: str
    bucket: str
    underlying: str
    named: str
    bought: str
    direction: str
    entered_at_ns: int
    entry_price_known_at_ns: int     # the end of the bar whose close is the entry
    entry_price: float
    exit_price: float
    quantity: float
    held_seconds: float
    fees: float
    spread_cost: float
    net_return: float


class SessionClock:
    """The session's own time, handed to every object that stamps one."""

    def __init__(self) -> None:
        self.now_ns = 0

    def __call__(self) -> int:
        return self.now_ns


# -- which underlyings the pilot covers ----------------------------------------------

def pilot_underlyings(before_day: str, stocks_wanted: int) -> tuple[str, ...]:
    """Every index the index segment derives, plus the stocks with the most option
    volume on NSE's own file for the last session *before* the pilot starts -- ranked
    before, so the choice cannot know what the pilot's sessions did."""
    from operate import nse_fo_bhavcopy as bhavcopy
    from operate.replay_a_captured_session import derived_membership_for, instruments_by_key

    membership = derived_membership_for(instruments_by_key())
    indices = sorted(membership.get("index-options", ()))
    stocks = membership.get("stock-options", frozenset())
    day = datetime.date.fromisoformat(before_day)
    session = bhavcopy.open_browser_session()
    for _ in range(10):
        day -= datetime.timedelta(days=1)
        if day.weekday() >= 5:
            continue
        try:
            contracts = bhavcopy.contracts_in(bhavcopy.fetch_bhavcopy(day.strftime("%Y%m%d"), session=session))
        except Exception:
            continue
        volume = collections.Counter()
        for contract in contracts:
            if contract.underlying in stocks and contract.option_type in ("CE", "PE"):
                volume[contract.underlying] += contract.volume
        ranked = [name for name, _ in volume.most_common(stocks_wanted)]
        return tuple(indices) + tuple(ranked)
    raise SystemExit(f"no NSE bhavcopy found in the ten days before {before_day}")


def weekdays(from_day: str, to_day: str) -> list[str]:
    day, end = datetime.date.fromisoformat(from_day), datetime.date.fromisoformat(to_day)
    days = []
    while day <= end:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += datetime.timedelta(days=1)
    return days


# -- the run -----------------------------------------------------------------------

class DetectorEdgeRun:
    """One chain, carried across sessions so windows and learning persist as live."""

    def __init__(self) -> None:
        from operate.detectors_for_measurement import build_detector_chain
        from parts.learning_loop.signal_outcome_labeller import (
            build_signal_outcome_labeller_from_settings,
        )
        from runtime.price_staleness import price_staleness_from

        self.clock = SessionClock()
        self.chain = build_detector_chain(self.clock)
        settings = self.chain.settings
        self.settings = settings
        self.staleness = price_staleness_from(
            settings, materiality_fraction=settings.number("signal_label_move_fraction"),
        )
        self.labeller = build_signal_outcome_labeller_from_settings(
            settings, price_staleness=self.staleness, now_ns=self.clock,
        )
        self.window_length = int(settings.number("kline_window_length"))
        self.forecast_horizon = settings.number("forecast_horizon")
        self.maximum_gap_ns = settings.number("price_series_maximum_gap_seconds") * 1e9
        self.half_spread = settings.number("backtest_prior_half_spread_fraction")
        self.iv_rate = settings.number("implied_volatility_annual_carry_rate")
        self.iv_tolerance = settings.number("implied_volatility_search_tolerance")
        self.iv_ceiling = settings.number("implied_volatility_search_ceiling")
        self.calendar_year = settings.number("option_delta_seconds_per_year")
        self.close_text = str(settings.setting("market_session_closes_at_ist").value)
        self.fee_rates = {
            "flat_brokerage": settings.number("options_flat_brokerage"),
            "stt_sell_rate": settings.number("options_stt_sell_rate"),
            "exchange_transaction_charge_rate": settings.number("options_exchange_transaction_charge_rate"),
            "ipft_charge_rate": settings.number("options_ipft_charge_rate"),
            "stamp_duty_buy_rate": settings.number("options_stamp_duty_buy_rate"),
            "gst_rate": settings.number("options_gst_rate"),
        }
        self.symbols_of_underlying: dict[str, set[str]] = collections.defaultdict(set)
        self.refused = collections.Counter()
        self.fired = collections.Counter()

    # -- small pieces ----------------------------------------------------------

    def _seconds_to_expiry_close(self, at_ns: int, expiry: str) -> float:
        hour, minute = (int(part) for part in self.close_text.split(":")[:2])
        close = datetime.datetime.combine(
            datetime.date.fromisoformat(expiry), datetime.time(hour, minute), IST,
        )
        return close.timestamp() - at_ns / 1e9

    def _round_trip_cost(self, entry_value: float, exit_value: float) -> tuple[float, float]:
        from runtime.indian_options_fee_model import upstox_options_order_cost

        fees = (
            upstox_options_order_cost(entry_value, "buy", **self.fee_rates).total
            + upstox_options_order_cost(exit_value, "sell", **self.fee_rates).total
        )
        spread = self.half_spread * (entry_value + exit_value)
        return fees, spread

    # -- one session -----------------------------------------------------------

    def run_session(self, day: str, instruments) -> list[ScoredCandidate]:
        from operate.replay_a_captured_session import size_for
        from parts.opportunity_scanner.expiry_day_zero_to_hero_detector import (
            PART_ID as ZERO_TO_HERO,
        )
        from parts.opportunity_scanner.volatility_gap_detector import PART_ID as VOLATILITY_GAP
        from parts.prediction.implied_vol_reader import ImpliedVolSurface
        from runtime.brokers.broker_adapter import BrokerOptionGreeks, InstrumentListing, LtpUpdate
        from runtime.forecast_types import Candle
        from runtime.implied_volatility_from_premium import implied_volatility
        from runtime.market_signal import settle_claims_from
        from runtime.option_delta import estimated_delta
        from runtime.segment_settings import segment_settings_path
        from runtime.settings_reader import load_settings_document
        from parts.learning_loop.signal_outcome_labeller import REGIME_NOT_KNOWN

        chain = self.chain
        by_symbol = {instrument.trading_symbol: instrument for instrument in instruments}
        segment_documents = {
            segment: load_settings_document(segment_settings_path(segment), f"segment:{segment}")
            for segment in {instrument.segment for instrument in instruments}
        }
        # Every bar becomes an event at its END: that is when its close is known.
        events = []
        for instrument in instruments:
            for candle in instrument.candles:
                start_ns = candle.bar_time_ms * 1_000_000
                events.append((start_ns + chain.kline_windows.interval_ns, instrument.trading_symbol, candle))
        events.sort(key=lambda event: (event[0], event[1]))
        series = {
            symbol: ([start + chain.kline_windows.interval_ns for start, _ in instrument.prints],
                     [price for _, price in instrument.prints])
            for symbol, instrument in by_symbol.items()
        }

        def price_known_at(symbol: str, at_ns: int):
            times, prices = series[symbol]
            position = bisect.bisect_right(times, at_ns) - 1
            if position < 0:
                return None, None
            return prices[position], times[position]

        spot_now: dict[str, float] = {}
        iv_now: dict[str, float] = {}
        for instrument in instruments:
            if instrument.option_type is None:
                continue
            fields = {field.name: None for field in dataclasses.fields(InstrumentListing)
                      if field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING}
            expiry_ms = int(datetime.datetime.combine(
                datetime.date.fromisoformat(instrument.expiry), datetime.time(15, 30), IST,
            ).timestamp() * 1000)
            fields.update(
                instrument_key=instrument.instrument_key, exchange="NSE", segment="NSE_FO",
                instrument_type=instrument.option_type, trading_symbol=instrument.trading_symbol,
                lot_size=int(instrument.lot_size), freeze_quantity=instrument.freeze_quantity,
                expiry_ms=expiry_ms, strike_price=instrument.strike,
                underlying_key=instrument.underlying,
            )
            chain.detectors[ZERO_TO_HERO].observe_listing(InstrumentListing(**fields))

        scored: list[ScoredCandidate] = []
        seen_minutes: set = set()
        last_resolved_ns = 0

        def at_the_money(underlying: str, option_type: str, at_ns: int):
            spot = spot_now.get(underlying)
            if spot is None:
                return None
            chain_now = [i for i in instruments
                         if i.underlying == underlying and i.option_type == option_type]
            if not chain_now:
                return None
            return min(chain_now, key=lambda i: abs(i.strike - spot))

        def trade(candidate, regime_name: str, at_ns: int) -> None:
            named = by_symbol.get(candidate.symbol)
            if named is None:
                self.refused["candidate names a symbol this session does not hold"] += 1
                return
            is_long = candidate.direction == "long"
            if named.option_type is None:
                bought = at_the_money(named.underlying, "CE" if is_long else "PE", at_ns)
            elif is_long:
                bought = named
            else:
                bought = at_the_money(named.underlying, "PE" if named.option_type == "CE" else "CE", at_ns)
            if bought is None:
                self.refused["no contract can express the view"] += 1
                return
            state = (candidate.evidence or {}).get("state") or candidate.direction
            bucket = f"{candidate.detector}|{state}|{regime_name}"
            minute = (bucket, named.underlying, at_ns // 60_000_000_000)
            if minute in seen_minutes:
                return
            seen_minutes.add(minute)
            entry, entry_at = price_known_at(bought.trading_symbol, at_ns)
            if entry is None or at_ns - entry_at > self.maximum_gap_ns:
                self.refused["the bought contract had no recent price"] += 1
                return
            quantity, why = size_for(segment_documents[bought.segment], entry,
                                     bought.lot_size, bought.freeze_quantity)
            if quantity <= 0:
                self.refused[f"not sizeable: {why.split(',')[0][:60]}"] += 1
                return
            times, prices = series[bought.trading_symbol]
            target = at_ns + candidate.horizon_seconds * 1e9
            position = bisect.bisect_left(times, target)
            if position >= len(times):
                position = len(times) - 1
            exit_price, exit_at = prices[position], times[position]
            if exit_at <= at_ns:
                self.refused["no price after entry this session"] += 1
                return
            fees, spread = self._round_trip_cost(entry * quantity, exit_price * quantity)
            net = (exit_price - entry) * quantity - fees - spread
            scored.append(ScoredCandidate(
                session=day, detector=candidate.detector, bucket=bucket,
                underlying=named.underlying, named=candidate.symbol,
                bought=bought.trading_symbol, direction=candidate.direction,
                entered_at_ns=at_ns, entry_price_known_at_ns=entry_at, entry_price=entry, exit_price=exit_price, quantity=quantity,
                held_seconds=(exit_at - at_ns) / 1e9, fees=fees, spread_cost=spread,
                net_return=net / (entry * quantity),
            ))

        def raise_candidate(candidate, regime_name: str, at_ns: int) -> None:
            if candidate is None:
                return
            self.fired[candidate.detector] += 1
            self.labeller.observe_candidate(candidate, regime_name=regime_name)
            trade(candidate, regime_name, at_ns)

        for at_ns, symbol, candle in events:
            self.clock.now_ns = at_ns
            instrument = by_symbol[symbol]
            price = float(candle.close)

            chain.regimes.observe_price(VENUE, symbol, price, at_ns)
            self.labeller.observe_price(VENUE, symbol, price, at_ns)
            self.staleness.observe_price(VENUE, symbol, price, at_ns)
            regime = chain.regimes.classify(VENUE, symbol)
            regime_name = regime.regime if regime.is_classified else REGIME_NOT_KNOWN

            for part_id in ("mean-reversion-detector", "momentum-burst-detector"):
                detector = chain.detectors[part_id]
                detector.observe_price(VENUE, symbol, price, at_ns)
                candidate, _ = detector.detect(VENUE, symbol, regime)
                raise_candidate(candidate, regime_name, at_ns)

            # The forecast chain, on the closed bar.
            chain.kline_windows.observe_candle(VENUE, symbol, Candle(
                open_time_ns=candle.bar_time_ms * 1_000_000, open=candle.open,
                high=candle.high, low=candle.low, close=candle.close,
                volume=candle.volume, quote_volume=candle.volume * candle.close,
                trades=0, is_closed=True,
            ))
            window = chain.kline_windows.build(VENUE, symbol, self.window_length)
            feature_set = chain.volatility_features.build(window, self.forecast_horizon)
            chain.volatility_training.observe(feature_set)
            forecast = chain.volatility_regressor.forecast(feature_set)
            gap_detector = chain.detectors[VOLATILITY_GAP]
            if forecast.expected_volatility is not None:
                gap_detector.observe_forecast(VENUE, symbol, forecast.expected_volatility,
                                              forecast.horizon_seconds)
                self.symbols_of_underlying[instrument.underlying].add(symbol)

            if instrument.option_type is None:
                spot_now[symbol] = price
            else:
                spot = spot_now.get(instrument.underlying)
                seconds = self._seconds_to_expiry_close(at_ns, instrument.expiry)
                iv = None if spot is None else implied_volatility(
                    price, spot, instrument.strike, instrument.option_type, seconds,
                    self.calendar_year, self.iv_rate, self.iv_tolerance, self.iv_ceiling,
                )
                if iv is not None:
                    iv_now[symbol] = iv
                    atm = [i for i in instruments if i.underlying == instrument.underlying
                           and i.option_type is not None and i.trading_symbol in iv_now]
                    nearest = min(atm, key=lambda i: abs(i.strike - spot))
                    atm_iv = statistics.fmean(
                        iv_now[i.trading_symbol] for i in atm if i.strike == nearest.strike
                    )
                    surface = ImpliedVolSurface(
                        venue_id=VENUE, underlying=instrument.underlying, state="readable",
                        by_expiry={}, at_the_money={seconds: atm_iv}, skew={},
                        quotes_used=len(atm), quotes_dropped={}, read_at_ns=at_ns,
                    )
                    chain.volatility_features.observe_surface(VENUE, instrument.underlying, surface)
                    for held in self.symbols_of_underlying.get(instrument.underlying, ()):
                        gap_detector.observe_implied(VENUE, held, atm_iv)
                    zero = chain.detectors[ZERO_TO_HERO]
                    zero.observe_ltp(LtpUpdate(
                        instrument_key=instrument.instrument_key, last_traded_price=price,
                        last_traded_quantity=None, last_traded_time_ms=at_ns // 1_000_000,
                        close_price=None, broker_time_ns=at_ns,
                    ))
                    delta = estimated_delta(spot, instrument.strike, instrument.option_type,
                                            iv, seconds, self.calendar_year)
                    if delta is not None:
                        zero.observe_greeks(BrokerOptionGreeks(
                            instrument_key=instrument.instrument_key, delta=delta, theta=0.0,
                            gamma=0.0, vega=0.0, rho=0.0, implied_volatility=iv,
                            broker_time_ns=at_ns,
                        ))
                        if instrument.instrument_key in zero.expiring_on(at_ns):
                            candidate, _ = zero.detect(instrument.instrument_key, at_ns)
                            raise_candidate(candidate, regime_name, at_ns)

            candidate, _ = gap_detector.detect(VENUE, symbol)
            raise_candidate(candidate, regime_name, at_ns)

            if at_ns - last_resolved_ns >= chain.kline_windows.interval_ns:
                last_resolved_ns = at_ns
                labels = self.labeller.resolve_settled_claims()
                if labels:
                    for part_id, detector in chain.detectors.items():
                        settle_claims_from(labels, detector, part_id)
        return scored


# -- walk forward and report --------------------------------------------------------

def bucket_statistics(rows) -> dict:
    returns = [row["net_return"] for row in rows]
    n = len(returns)
    mean = statistics.fmean(returns) if n else float("nan")
    spread = statistics.stdev(returns) if n > 1 else float("nan")
    return {
        "trades": n,
        "sessions": len({row["session"] for row in rows}),
        "mean_net": mean,
        "stdev": spread,
        "win_rate": sum(1 for r in returns if r > 0) / n if n else float("nan"),
        "median_net": statistics.median(returns) if n else float("nan"),
    }


def walk_forward(rows: list[dict], train_fraction: float, minimum_trades: int,
                 confidence_level: float) -> dict:
    sessions = sorted({row["session"] for row in rows})
    cut = int(len(sessions) * train_fraction)
    train_sessions, test_sessions = set(sessions[:cut]), set(sessions[cut:])
    assert not train_sessions & test_sessions
    z = statistics.NormalDist().inv_cdf(confidence_level)
    by_bucket_train = collections.defaultdict(list)
    by_bucket_test = collections.defaultdict(list)
    for row in rows:
        (by_bucket_train if row["session"] in train_sessions else by_bucket_test)[row["bucket"]].append(row)
    kept = {}
    for bucket, bucket_rows in by_bucket_train.items():
        stats = bucket_statistics(bucket_rows)
        if stats["trades"] < minimum_trades or math.isnan(stats["stdev"]):
            continue
        lower = stats["mean_net"] - z * stats["stdev"] / math.sqrt(stats["trades"])
        if lower > 0:
            kept[bucket] = {"train": stats, "train_lower_bound": lower,
                            "test": bucket_statistics(by_bucket_test.get(bucket, []))}
    all_test = {bucket: bucket_statistics(r) for bucket, r in by_bucket_test.items()}
    return {
        "train_sessions": sorted(train_sessions),
        "test_sessions": sorted(test_sessions),
        "kept": kept,
        "every_bucket_on_test": all_test,
        "buckets_considered": len(by_bucket_train),
    }


def write_report(rows, result, run: DetectorEdgeRun, underlyings, args, path: pathlib.Path) -> None:
    def pct(value):
        return "—" if value is None or (isinstance(value, float) and math.isnan(value)) else f"{value:+.3%}"

    lines = [
        "# Detector edge across sessions",
        "",
        f"Generated {datetime.datetime.now(IST):%Y-%m-%d %H:%M IST} by `operate/measure_detector_edge.py "
        f"--from {args.from_day} --to {args.to_day}`. Every figure is net of Upstox's options charges "
        f"on both legs and two half-spread crossings.",
        "",
        f"- sessions scored: **{len(set(r['session'] for r in rows))}** "
        f"(fitted on {len(result['train_sessions'])}, scored on {len(result['test_sessions'])})",
        f"- underlyings: {', '.join(underlyings)}",
        f"- trades: {len(rows):,}",
        "",
        "## Every detector, on the later sessions only",
        "",
        "| detector | trades | sessions | win rate | mean net | median net |",
        "|---|---|---|---|---|---|",
    ]
    by_detector = collections.defaultdict(list)
    for row in rows:
        if row["session"] in set(result["test_sessions"]):
            by_detector[row["detector"]].append(row)
    for detector in sorted(set(run.chain.detectors) | set(by_detector)):
        stats = bucket_statistics(by_detector.get(detector, []))
        if stats["trades"] == 0:
            lines.append(f"| {detector} | 0 | 0 | NOT MEASURED — fired {run.fired[detector]:,} times, no scored trade on the later sessions | | |")
            continue
        lines.append(f"| {detector} | {stats['trades']:,} | {stats['sessions']} | "
                     f"{stats['win_rate']:.1%} | {pct(stats['mean_net'])} | {pct(stats['median_net'])} |")
    for detector, reason in NOT_MEASURED.items():
        lines.append(f"| {detector} | NOT MEASURED | | {reason} | | |")
    lines += [
        "",
        "## Buckets chosen on the earlier sessions, scored on the later",
        "",
        f"A bucket is kept when its mean net return on the earlier sessions has a lower "
        f"{args.confidence_level:.0%} bound above zero over at least {args.minimum_trades} trades. "
        f"{len(result['kept'])} of {result['buckets_considered']} buckets were kept.",
        "",
        "| bucket | earlier: trades / mean / lower bound | later: trades / sessions / win rate / mean net |",
        "|---|---|---|",
    ]
    for bucket, entry in sorted(result["kept"].items(), key=lambda kv: -kv[1]["train_lower_bound"]):
        train, test = entry["train"], entry["test"]
        lines.append(
            f"| `{bucket}` | {train['trades']:,} / {pct(train['mean_net'])} / {pct(entry['train_lower_bound'])} | "
            + (f"{test['trades']:,} / {test['sessions']} / {test['win_rate']:.1%} / {pct(test['mean_net'])} |"
               if test["trades"] else "0 — never fired on the later sessions |")
        )
    lines += [
        "",
        "## Refused before a trade could be scored",
        "",
        *[f"- {reason}: {count:,}" for reason, count in run.refused.most_common()],
        "",
        "## Approximations this rests on",
        "",
        "- A bar's close is acted on when the bar ends, one interval after its stamp.",
        "- Implied volatility is inverted from the premium; the surface carries at-the-money only, no skew.",
        "- Zero-to-hero's delta is estimated from that volatility, not stated by Upstox.",
        "- `entropy-magnitude-forecaster` is not run: one-minute bars carry no order-flow entropy.",
        "- The exit is the candidate's own horizon, capped at the session's last close; no stop, no target.",
        "- Contracts are each segment's chain width nearest the session's open, on the nearest unexpired expiry; strikes further out are not held.",
        "- **mean-reversion and momentum-burst run live on every print, several a second; here on one close a minute.** "
        "Their observation windows therefore span hours here where they span minutes live, so their rows describe the "
        "same rule on a slower clock, not the live detector's edge. volatility-gap (fed by one-minute candle windows live "
        "too) and zero-to-hero (premium and delta thresholds) are the faithful rows.",
    ]
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="from_day", required=True)
    parser.add_argument("--to", dest="to_day", required=True)
    parser.add_argument("--underlyings", default="", help="comma list; default is the pilot rule")
    parser.add_argument("--output", default=str(OUTPUT))
    args = parser.parse_args()

    from operate.detectors_for_measurement import MeasurementSettings
    from operate.past_session_prints import past_session_instruments

    settings = MeasurementSettings()
    args.train_fraction = settings.number("edge_walk_forward_train_fraction")
    args.minimum_trades = int(settings.number("edge_minimum_trades_per_bucket"))
    args.confidence_level = settings.number("edge_confidence_level")
    stocks_wanted = int(settings.number("edge_pilot_stock_underlyings"))
    # The live feed subscribes to each segment's own chain width, whatever a strike's
    # volume, so a contract is held here if it traded at all that session.
    from runtime.segment_settings import read_segment_setting

    chain_width = {
        segment: int(read_segment_setting(segment, "segment_option_contracts_per_underlying").value)
        for segment in ("index-options", "stock-options")
    }
    minimum_prints = 1

    output = pathlib.Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    underlyings = (tuple(name.strip() for name in args.underlyings.split(",") if name.strip())
                   or pilot_underlyings(args.from_day, stocks_wanted))
    print(f"underlyings ({len(underlyings)}): {', '.join(underlyings)}", flush=True)
    from operate.replay_a_captured_session import derived_membership_for, instruments_by_key

    membership = derived_membership_for(instruments_by_key())
    segment_of = {
        name: next((segment for segment in chain_width if name in membership.get(segment, ())), None)
        for name in underlyings
    }

    # Trades are kept per session so a run stopped by Upstox's quota resumes where it
    # stopped. The chain's learned state is not: a resumed run starts its learning
    # cold, and the report says how many sessions were scored in one pass.
    scored_path = output / "scored-trades.jsonl"
    done = set()
    if scored_path.exists():
        for line in scored_path.read_text().splitlines():
            done.add(json.loads(line)["session"])
    run = DetectorEdgeRun()
    started = time.monotonic()
    with scored_path.open("a") as sink:
        for day in weekdays(args.from_day, args.to_day):
            if day in done:
                continue
            try:
                instruments = tuple(
                    instrument
                    for segment, width in chain_width.items()
                    for instrument in past_session_instruments(
                        day, tuple(u for u in underlyings if segment_of[u] == segment),
                        width, minimum_prints,
                    )
                )
            except RuntimeError as refusal:
                print(f"{day}: stopped -- {refusal}", flush=True)
                break
            if not any(i.option_type for i in instruments):
                print(f"{day}: no session (holiday, or nothing traded)", flush=True)
                continue
            rows = run.run_session(day, instruments)
            if not rows:
                rows_marker = {"session": day, "detector": "", "bucket": "", "net_return": None}
                sink.write(json.dumps(rows_marker) + "\n")
            for row in rows:
                sink.write(json.dumps(dataclasses.asdict(row)) + "\n")
            sink.flush()
            print(f"{day}: {len(instruments)} instruments, {len(rows)} trades, "
                  f"fired {dict(run.fired)}  [{time.monotonic() - started:.0f}s]", flush=True)

    rows = [json.loads(line) for line in scored_path.read_text().splitlines()]
    rows = [row for row in rows if row.get("net_return") is not None]
    if not rows:
        print("no trades scored")
        return 1
    result = walk_forward(rows, args.train_fraction, args.minimum_trades, args.confidence_level)
    (output / "buckets.json").write_text(json.dumps(result, indent=1, default=str))
    write_report(rows, result, run, underlyings, args, output / "report.md")
    print((output / "report.md").read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
