"""instruction-replayer: an instruction run over history, one bar at a time forward.

This is where a backtest is either honest or not, and the property that decides it is
structural rather than careful: **the replayer hands the instruction a view of the
data that ends at the current bar, and cannot hand it more.** A decision made with a
window that physically excludes the future cannot use the future, however the
instruction is written.

That is the difference between checking for lookahead and preventing it. Auditing
afterwards catches the cases somebody thought to check; a bounded view catches all of
them, including the ones introduced later by an author who did not know the rule.

Everything else follows from the same principle -- the simulation is only allowed to
do what a live system could have done:

- **A decision made on bar N fills no earlier than bar N+1.** Filling at the close of
  the bar that produced the signal assumes an order placed and executed at the instant
  the bar ended, which is not a thing that happens.
- **Fills are capped by the volume that traded** and priced by the cost model, both
  supplied rather than assumed, so the run records which models produced it.
- **When a stop and a target sit in the same bar, the sequencer decides**, and its
  answer -- including "unknown, so adverse first" -- is carried into the trade.
- **The run records the period it covers and whether it was out of sample.** A result
  without that is not comparable to any other result.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.backtest_types import AT_THE_NEXT_OPEN, BacktestRun, BacktestTrade
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "instruction-replayer"

PART_DECLARATION = PartDeclaration(
    part_id="instruction-replayer",
    consumes=(
        "opportunity-instruction", "walk-forward-split", "cost-estimate",
        "fill-sequence", "fillable-size", "historical-window",
    ),
    produces=("backtest-run", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

REPLAYED = "replayed"
NO_BARS = "no-bar-falls-inside-this-split"
NO_COST_MODEL = "no-cost-model-was-supplied"
NO_TRADES = "the-instruction-never-fired-over-this-period"


@dataclass(frozen=True)
class BoundedView:
    """What a decision may see: everything up to and including the current bar.

    The whole point of this type is that it cannot reach forward. An instruction
    handed one of these cannot use the future because the future is not in it.
    """

    bars: tuple
    up_to_ns: int

    @property
    def latest(self):
        return self.bars[-1] if self.bars else None

    def __len__(self) -> int:
        return len(self.bars)


@dataclass(frozen=True)
class ReplayOutcome:
    instruction_id: str
    split_id: str
    state: str
    run: BacktestRun | None
    reason: str
    ran_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == REPLAYED and self.run is not None


@dataclass
class ReplayerStanding:
    runs: int = 0
    trades_simulated: int = 0
    signals_that_could_not_fill: int = 0
    fills_capped_by_volume: int = 0
    ambiguous_bars_resolved: int = 0
    refused_no_cost_model: int = 0
    refused_no_bars: int = 0
    decisions_offered_the_future: int = 0
    # Splits that arrived without the bars they cut (see start_part).
    splits_without_bars: int = 0


class InstructionReplayer:
    """Replays an instruction forward over bars, handing it a view that cannot cheat."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._cost_of = None
        self._cap = None
        self._sequence_of = None
        self._sequence = 0
        self.standing = ReplayerStanding()

    def install_cost_model(self, cost_of) -> None:
        """`cost_of(venue_id, symbol, notional) -> total cost`."""
        self._cost_of = cost_of

    def install_volume_cap(self, cap) -> None:
        """`cap(venue_id, symbol, at_ns, intended) -> fillable quantity`."""
        self._cap = cap

    def install_sequencer(self, sequence_of) -> None:
        """`sequence_of(bar, stop, target, is_long) -> 'stop' | 'target' | None`."""
        self._sequence_of = sequence_of

    def view_up_to(self, bars, index: int) -> BoundedView:
        """A view that physically excludes everything after the current bar."""
        return BoundedView(bars=tuple(bars[: index + 1]), up_to_ns=bars[index].at_ns)

    def replay(
        self, instruction_id: str, split, window, decide, quantity: float,
        is_out_of_sample: bool = True,
    ) -> ReplayOutcome:
        """`decide(view) -> None | {'side', 'stop', 'target'}`, given a bounded view."""
        if self._cost_of is None:
            self.standing.refused_no_cost_model += 1
            return self._outcome(
                instruction_id, split.split_id, NO_COST_MODEL, None,
                "no cost model was supplied. A run without costs is a run about a market "
                "that does not charge for anything",
            )

        bars = [
            bar for bar in window.bars
            if split.test_from_ns <= bar.at_ns <= split.test_to_ns
        ]
        if len(bars) < 2:
            self.standing.refused_no_bars += 1
            return self._outcome(
                instruction_id, split.split_id, NO_BARS, None,
                f"{len(bars)} bar(s) inside the split, and a decision needs a bar after "
                f"it to fill in",
            )

        trades = []
        open_trade = None

        for index in range(len(bars) - 1):
            bar = bars[index]
            next_bar = bars[index + 1]

            if open_trade is not None:
                resolved = self._resolve(open_trade, bar)
                if resolved is not None:
                    trades.append(self._close(open_trade, resolved, bar, window))
                    open_trade = None
                    continue

            if open_trade is not None:
                continue

            decision = decide(self.view_up_to(bars, index))
            if not decision:
                continue

            # A decision made on this bar fills no earlier than the next one: filling
            # at the close that produced the signal assumes an instant execution at
            # the moment the bar ended.
            intended = quantity
            fillable = intended
            if self._cap is not None:
                fillable = self._cap(
                    window.venue_id, window.symbol, next_bar.at_ns, intended
                )
                if fillable < intended:
                    self.standing.fills_capped_by_volume += 1
            if fillable <= 0:
                self.standing.signals_that_could_not_fill += 1
                continue

            open_trade = {
                "side": decision["side"],
                "stop": decision["stop"],
                "target": decision["target"],
                "entry_price": next_bar.open_price,
                "entry_at_ns": next_bar.at_ns,
                "quantity": fillable,
                "decided_with_data_up_to_ns": bar.at_ns,
            }

        if open_trade is not None:
            trades.append(self._close(open_trade, "period-end", bars[-1], window))

        self._sequence += 1
        run = BacktestRun(
            run_id=f"run-{self._sequence}",
            instruction_id=instruction_id,
            split_id=split.split_id,
            venue_id=window.venue_id,
            symbol=window.symbol,
            trades=tuple(trades),
            period_from_ns=bars[0].at_ns,
            period_to_ns=bars[-1].at_ns,
            fill_assumption=AT_THE_NEXT_OPEN,
            costs_applied=True,
            was_out_of_sample=is_out_of_sample,
            bars_seen=len(bars),
            ran_at_ns=self._now_ns(),
        )
        self.standing.runs += 1
        self.standing.trades_simulated += len(trades)

        if not trades:
            return self._outcome(
                instruction_id, split.split_id, NO_TRADES, run,
                f"the instruction never fired over {len(bars)} bar(s). That is a result: "
                f"a rule that does not trigger has no edge to measure",
            )

        return self._outcome(
            instruction_id, split.split_id, REPLAYED, run,
            f"{len(trades)} trade(s) over {len(bars)} bar(s), filled at the next open "
            f"with costs applied. Every decision saw a view that ends at its own bar, so "
            f"it could not have used the future however it was written",
        )

    def _resolve(self, trade, bar) -> str | None:
        is_long = trade["side"] == "long"
        stop_inside = bar.low_price <= trade["stop"] <= bar.high_price
        target_inside = bar.low_price <= trade["target"] <= bar.high_price
        if stop_inside and target_inside:
            self.standing.ambiguous_bars_resolved += 1
            if self._sequence_of is not None:
                return self._sequence_of(bar, trade["stop"], trade["target"], is_long)
            return "stop"
        if stop_inside:
            return "stop"
        if target_inside:
            return "target"
        return None

    def _close(self, trade, reason, bar, window) -> BacktestTrade:
        exit_price = {
            "stop": trade["stop"], "target": trade["target"],
        }.get(reason, bar.close_price)
        sign = 1.0 if trade["side"] == "long" else -1.0
        gross = sign * (exit_price - trade["entry_price"]) * trade["quantity"]
        notional = abs(trade["entry_price"] * trade["quantity"])
        costs = self._cost_of(window.venue_id, window.symbol, notional)
        return BacktestTrade(
            entry_at_ns=trade["entry_at_ns"],
            exit_at_ns=bar.at_ns,
            side=trade["side"],
            entry_price=trade["entry_price"],
            exit_price=exit_price,
            quantity=trade["quantity"],
            gross=gross,
            costs=costs,
            net=gross - costs,
            fill_assumption=AT_THE_NEXT_OPEN,
            decided_with_data_up_to_ns=trade["decided_with_data_up_to_ns"],
        )

    def _outcome(self, instruction_id, split_id, state, run, reason) -> ReplayOutcome:
        return ReplayOutcome(
            instruction_id=instruction_id, split_id=split_id, state=state, run=run,
            reason=reason, ran_at_ns=self._now_ns(),
        )


def describe_replaying(replayer: InstructionReplayer) -> dict:
    return {
        "part_id": PART_ID,
        "runs": replayer.standing.runs,
        "trades_simulated": replayer.standing.trades_simulated,
        "signals_that_could_not_fill": replayer.standing.signals_that_could_not_fill,
        "fills_capped_by_volume": replayer.standing.fills_capped_by_volume,
        "ambiguous_bars_resolved": replayer.standing.ambiguous_bars_resolved,
        "refused_no_cost_model": replayer.standing.refused_no_cost_model,
        "refused_no_bars": replayer.standing.refused_no_bars,
        "fill_assumption": AT_THE_NEXT_OPEN,
        "fills_at_the_signal_bars_close": False,
        "decisions_offered_the_future": replayer.standing.decisions_offered_the_future,
    }


def run_instruction_replayer(
    replayer: InstructionReplayer, control_socket, read_jobs, publish_runs,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for job in read_jobs():
            outcome = replayer.replay(**job)
            if outcome.run is not None:
                publish_runs(outcome.run)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_replaying(replayer),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    An instruction is a measurement, a comparison and a threshold; its
    decision over a bounded view is the measurement on the view's last
    bars, compared -- a return over the instruction's horizon in bars
    against the threshold -- and a side, stop and target from the bar
    range. Every instruction is replayed over every split of every window
    for its symbol, with the cost model, the volume cap and the fill
    sequence the other backtesting parts publish.

    A split names where it cuts and not the bars it cuts. The bars are on
    `historical-window`, which this part consumes since 2026-08-25: an outcome
    names the `window_id` it split, and that is the window's own id, so the two
    halves join on a number both sides already carry. A split whose window has
    not arrived is still counted rather than dropped -- the bars may not have been
    built yet, and replaying against bars that are missing is what this block
    exists to prevent.

    `walk-forward-split` carries a `SplitOutcome`: a window id, a state, and a
    **tuple** of splits. Reading each payload as one split -- which this part did
    until 2026-08-25 -- takes `split_id` and `test_from_ns` off the wrapper, and
    neither is there.
    """
    from runtime.input_assembly import Batch, LatestByKey

    instructions = LatestByKey(read=context.bus.reader("opportunity-instruction"), key_of=lambda i: i.instruction_id)
    splits = Batch(read=context.bus.reader("walk-forward-split"))
    estimates = LatestByKey(read=context.bus.reader("cost-estimate"), key_of=lambda e: (e.venue_id, e.symbol))
    sequences = LatestByKey(read=context.bus.reader("fill-sequence"), key_of=lambda s: (s.venue_id, s.symbol, s.at_ns))
    sizes = LatestByKey(read=context.bus.reader("fillable-size"), key_of=lambda s: (s.venue_id, s.symbol, s.at_ns))
    publish_runs = context.bus.publisher_for("backtest-run")
    replayer = InstructionReplayer()
    quantity = context.number("replay_quantity")
    windows = LatestByKey(
        read=context.bus.reader("historical-window"),
        key_of=lambda window: window.window_id,
    )

    def cost_of(venue_id, symbol, notional):
        estimate = estimates.mapping().get((venue_id, symbol))
        if estimate is None or estimate.notional <= 0:
            return None
        return (estimate.fee + estimate.half_spread + estimate.expected_impact) / estimate.notional * notional

    def cap(venue_id, symbol, at_ns, intended):
        size = sizes.mapping().get((venue_id, symbol, at_ns))
        return intended if size is None else min(intended, size.fillable)

    def sequence_of(venue_id, symbol, at_ns):
        return sequences.mapping().get((venue_id, symbol, at_ns))

    replayer.install_cost_model(cost_of)
    replayer.install_volume_cap(cap)
    replayer.install_sequencer(sequence_of)

    def decide_for(instruction):
        bars_back = max(1, int(instruction.horizon_seconds / context.number("backtest_bar_interval")))

        def decide(view):
            bars = view.bars
            if len(bars) <= bars_back:
                return None
            last, earlier = bars[-1], bars[-1 - bars_back]
            value = last.close_price / earlier.close_price - 1.0 if earlier.close_price else 0.0
            fired = value > instruction.threshold if instruction.comparison in ("above", "crosses-above") else value < instruction.threshold
            if not fired:
                return None
            span = max(last.high_price - last.low_price, 1e-12)
            if instruction.direction == "long":
                return {"side": "long", "stop": last.close_price - span, "target": last.close_price + span}
            return {"side": "short", "stop": last.close_price + span, "target": last.close_price - span}

        return decide

    def read_jobs():
        jobs = []
        window_by_id = windows.mapping()
        for outcome in splits.payloads():
            window = window_by_id.get(outcome.window_id)
            if window is None:
                # One count per split, not per outcome: the standing answers "how
                # many replays did not happen", and an outcome is several.
                replayer.standing.splits_without_bars += len(outcome.splits) or 1
                continue
            for split in outcome.splits:
                for instruction in instructions.mapping().values():
                    jobs.append({
                        "instruction_id": instruction.instruction_id,
                        "split": split,
                        "window": window,
                        "decide": decide_for(instruction),
                        "quantity": quantity,
                        "is_out_of_sample": True,
                    })
        return tuple(jobs)

    def publish(item) -> None:
        if item is not None:
            publish_runs((item,))

    return run_instruction_replayer(
        replayer=replayer,
        control_socket=context.control_socket,
        read_jobs=read_jobs,
        publish_runs=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
