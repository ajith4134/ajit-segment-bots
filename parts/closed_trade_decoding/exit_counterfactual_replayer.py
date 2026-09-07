"""exit-counterfactual-replayer: what other exit rules would have done, as hindsight.

Replaying alternative exits is the most seductive analysis in trading and the easiest
to fool yourself with. The output always contains a rule that would have done better
on the trades already seen, because a rule chosen after seeing the outcomes is fitted
to them. This part produces those numbers anyway -- they are genuinely useful across
many trades -- while making the hindsight impossible to forget.

Four things it does that a naive replay does not:

- **Every result is stamped `is_hindsight`.** A single trade's counterfactual is
  never evidence that a rule is better. It becomes evidence only when the same
  comparison holds across a sample, which is the horizon profiler's job.
- **Reachability is checked.** A rule exiting at a price the tape never printed, or
  at a size the book could not absorb, would not have been reachable, and reporting
  it as a result invents money. Unreachable replays are marked and excluded from any
  aggregate.
- **Costs are applied to the counterfactual too.** A rule that trades more often
  pays more fees, and a gross comparison always favours the busier rule.
- **The rules are fixed in advance, from the plans the bots actually produce.**
  Sweeping a continuum of parameters and reporting the best is curve fitting with
  extra steps; replaying the exit plans that were genuinely proposed is not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.trade_decoding_types import ExitCounterfactual
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "exit-counterfactual-replayer"

PART_DECLARATION = PartDeclaration(
    part_id="exit-counterfactual-replayer",
    consumes=("closed-trade", "market-data", "bull-exit-plan", "bear-exit-plan", "tail-exit-plan"),
    produces=("exit-counterfactual", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

REPLAYED = "replayed"
NEVER_TRIGGERED = "the-rule-would-never-have-fired"
UNREACHABLE = "the-price-it-assumes-was-never-printed"
NO_TAPE = "no-price-history-covers-this-trade"

# The rule shapes this part can replay. Each corresponds to a plan the bots
# actually produce, rather than a parameter sweep invented here.
FIXED_TARGET = "fixed-target"
FIXED_STOP = "fixed-stop"
TRAILING_STOP = "trailing-stop"
TIME_EXIT = "time-exit"

REPLAYABLE_RULES = (FIXED_TARGET, FIXED_STOP, TRAILING_STOP, TIME_EXIT)


def _jobs_for_plan(kind: str, plan, trade_id: str, trade) -> tuple:
    """Every counterfactual replay job one exit plan implies.

    Every plan gets its stop and its targets replayed as fixed rules. Only
    tail-exit-plan also gets a trailing-stop replay: it is the only bot whose
    plan is a trail rather than a fixed level, and REPLAYABLE_RULES has always
    supported TRAILING_STOP -- nothing fed it until now.
    """
    jobs = [
        (trade_id, trade, f"{kind}:stop", {"kind": FIXED_STOP, "price": plan.stop_price}),
    ]
    for index, target in enumerate(plan.targets):
        jobs.append(
            (trade_id, trade, f"{kind}:target-{index}", {"kind": FIXED_TARGET, "price": target.price})
        )
    if kind == "tail-exit-plan":
        jobs.append((
            trade_id, trade, "tail-exit-plan:trail",
            {"kind": TRAILING_STOP, "distance": plan.risk_fraction * trade.entry_price},
        ))
    return tuple(jobs)


@dataclass(frozen=True)
class ReplayOutcome:
    trade_id: str
    rule_name: str
    state: str
    counterfactual: ExitCounterfactual
    reason: str
    replayed_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == REPLAYED


@dataclass
class ReplayerStanding:
    replays: int = 0
    reachable_replays: int = 0
    never_triggered: int = 0
    unreachable: int = 0
    without_a_tape: int = 0
    rules_that_beat_the_actual_exit: int = 0
    rules_that_lost_to_the_actual_exit: int = 0


class ExitCounterfactualReplayer:
    """Replays fixed exit rules over the recorded tape, always labelled as hindsight."""

    def __init__(self, round_trip_cost_fraction: float, now_ns=time.time_ns) -> None:
        if round_trip_cost_fraction < 0:
            raise ValueError(
                "a gross comparison always favours the busier rule, so costs apply to "
                "the counterfactual too"
            )
        self._cost_fraction = round_trip_cost_fraction
        self._now_ns = now_ns
        self._tape: dict[tuple[str, str], list] = {}
        self.standing = ReplayerStanding()

    def observe_tape(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        """The prices that actually printed while the position was open.

        Keyed by instrument, not by trade id: the real trade id only exists
        once the trade closes (closed_trade_id needs closed_at_ns), so keying
        by it here meant the tape and the replay lookup could never meet.
        """
        self._tape.setdefault((venue_id, symbol), []).append((at_ns, price))

    def release(self, venue_id: str, symbol: str) -> None:
        """T-3: a closed trade's tape must not leak into the next trade's replay."""
        self._tape.pop((venue_id, symbol), None)

    def replay(self, trade_id: str, closed_trade, rule_name: str, rule: dict) -> ReplayOutcome:
        self.standing.replays += 1
        if rule.get("kind") not in REPLAYABLE_RULES:
            raise ValueError(
                f"{rule.get('kind')!r} is not a rule shape this part replays. Sweeping a "
                f"continuum of parameters and reporting the best is curve fitting with "
                f"extra steps"
            )

        tape = sorted(self._tape.get((closed_trade.venue_id, closed_trade.symbol), []))
        if not tape:
            self.standing.without_a_tape += 1
            return self._outcome(
                trade_id, rule_name, NO_TAPE, None, None, False,
                "no price history covers this trade, so nothing can be replayed against "
                "what actually printed",
                closed_trade.venue_id, closed_trade.symbol, None,
            )

        is_long = closed_trade.direction == "long"
        sign = 1.0 if is_long else -1.0
        entry = closed_trade.entry_price
        kind = rule["kind"]
        trail_fraction = (
            rule["distance"] / entry if kind == TRAILING_STOP and entry else None
        )
        exit_price = None
        best = entry

        for at_ns, price in tape:
            if kind == FIXED_TARGET:
                target = rule["price"]
                if (price >= target) if is_long else (price <= target):
                    exit_price = target
                    break
            elif kind == FIXED_STOP:
                stop = rule["price"]
                if (price <= stop) if is_long else (price >= stop):
                    exit_price = stop
                    break
            elif kind == TRAILING_STOP:
                best = max(best, price) if is_long else min(best, price)
                trail = best - rule["distance"] if is_long else best + rule["distance"]
                if (price <= trail) if is_long else (price >= trail):
                    exit_price = trail
                    break
            elif kind == TIME_EXIT:
                if at_ns - closed_trade.opened_at_ns >= rule["seconds"] * 1e9:
                    exit_price = price
                    break

        if exit_price is None:
            self.standing.never_triggered += 1
            return self._outcome(
                trade_id, rule_name, NEVER_TRIGGERED, None, None, False,
                f"the {kind} rule never fired over this trade's tape, so it would have "
                f"held to the actual exit",
                closed_trade.venue_id, closed_trade.symbol, trail_fraction,
            )

        # A rule exiting at a price the tape never printed did not have a fill
        # available, and reporting it as a result invents money.
        printed = [price for _, price in tape]
        reachable = min(printed) <= exit_price <= max(printed)
        if not reachable:
            self.standing.unreachable += 1
            return self._outcome(
                trade_id, rule_name, UNREACHABLE, exit_price, None, False,
                f"the rule assumes an exit at {exit_price:.6f}, which never printed "
                f"between {min(printed):.6f} and {max(printed):.6f}. Reporting it would "
                f"invent money",
                closed_trade.venue_id, closed_trade.symbol, trail_fraction,
            )

        gross = sign * (exit_price - entry) * closed_trade.quantity
        costs = self._cost_fraction * abs(entry * closed_trade.quantity)
        realised = gross - costs
        difference = realised - closed_trade.realised_pnl

        self.standing.reachable_replays += 1
        if difference > 0:
            self.standing.rules_that_beat_the_actual_exit += 1
        else:
            self.standing.rules_that_lost_to_the_actual_exit += 1

        return self._outcome(
            trade_id, rule_name, REPLAYED, exit_price, realised, True,
            f"{kind} would have exited at {exit_price:.6f} for {realised:+.4f} against the "
            f"actual {closed_trade.realised_pnl:+.4f}, {difference:+.4f} apart, costs "
            f"applied. This is hindsight: a rule chosen after seeing outcomes is fitted to "
            f"them, and one trade is never evidence that it is better",
            closed_trade.venue_id, closed_trade.symbol, trail_fraction, difference,
        )

    def _outcome(
        self, trade_id, rule_name, state, exit_price, realised, reachable, reason,
        venue_id, symbol, trail_fraction, difference=None,
    ) -> ReplayOutcome:
        return ReplayOutcome(
            trade_id=trade_id, rule_name=rule_name, state=state,
            counterfactual=ExitCounterfactual(
                trade_id=trade_id, rule_name=rule_name, exit_price=exit_price,
                realised_pnl=realised, difference=difference,
                would_have_been_reachable=reachable, is_hindsight=True, reason=reason,
                replayed_at_ns=self._now_ns(),
                venue_id=venue_id, symbol=symbol, trail_fraction=trail_fraction,
            ),
            reason=reason, replayed_at_ns=self._now_ns(),
        )


def describe_replaying(replayer: ExitCounterfactualReplayer) -> dict:
    return {
        "part_id": PART_ID,
        "replays": replayer.standing.replays,
        "reachable_replays": replayer.standing.reachable_replays,
        "rules_that_never_triggered": replayer.standing.never_triggered,
        "unreachable_replays": replayer.standing.unreachable,
        "trades_without_a_tape": replayer.standing.without_a_tape,
        "rules_that_beat_the_actual_exit": (
            replayer.standing.rules_that_beat_the_actual_exit
        ),
        "rules_that_lost_to_the_actual_exit": (
            replayer.standing.rules_that_lost_to_the_actual_exit
        ),
        "replayable_rules": list(REPLAYABLE_RULES),
        "sweeps_parameters": False,
        "compares_gross": False,
        "every_result_is_labelled_hindsight": True,
    }


def run_exit_counterfactual_replayer(
    replayer: ExitCounterfactualReplayer, control_socket, read_jobs,
    publish_counterfactuals, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for trade_id, closed_trade, rule_name, rule in read_jobs():
            outcome = replayer.replay(trade_id, closed_trade, rule_name, rule)
            publish_counterfactuals(outcome.counterfactual)

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

    The tape is every print while a position was open, kept per symbol; the
    rules replayed against a closed trade are the bots' own exit plans for
    that symbol -- the stop as a fixed stop, each target as a fixed target.
    """
    from runtime.input_assembly import Batch, LatestByKey
    from runtime.trade_identity import closed_trade_id
    from runtime.venues.venue_adapter import NormalisedTrade

    closed = Batch(read=context.bus.reader("closed-trade"))
    trades = Batch(read=context.bus.reader("market-data"))
    plans = {
        kind: LatestByKey(
            read=context.bus.reader(kind), key_of=lambda p: (p.venue_id, p.symbol),
            maximum_age_seconds=context.number("exit_counterfactual_plan_maximum_age_seconds"),
        )
        for kind in ("bull-exit-plan", "bear-exit-plan", "tail-exit-plan")
    }
    publish_counterfactuals = context.bus.publisher_for("exit-counterfactual")
    replayer = ExitCounterfactualReplayer(round_trip_cost_fraction=context.number("reference_price_materiality_fraction"))

    def read_jobs():
        for trade in trades.payloads():
            if isinstance(trade, NormalisedTrade):
                replayer.observe_tape(
                    trade.venue_id, trade.symbol, trade.price, trade.venue_time_ns
                )
        jobs = []
        for trade in closed.payloads():
            trade_id = closed_trade_id(trade)
            key = (trade.venue_id, trade.symbol)
            for kind, source in plans.items():
                plan = source.mapping().get(key)
                if plan is None:
                    continue
                jobs.extend(_jobs_for_plan(kind, plan, trade_id, trade))
            # The next trade on this symbol gets a fresh tape -- the one just
            # replayed against must not leak into it (T-3).
            replayer.release(*key)
        return tuple(jobs)

    def publish(counterfactual) -> None:
        if counterfactual is not None:
            publish_counterfactuals((counterfactual,))

    return run_exit_counterfactual_replayer(
        replayer=replayer,
        control_socket=context.control_socket,
        read_jobs=read_jobs,
        publish_counterfactuals=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
