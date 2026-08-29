"""tail-trailing-exit-planner: a trail, never a target. That is the whole bot.

The bull and bear proposers name where a trade is finished. This one refuses to,
and the refusal is the design: a tailgater joins moves whose size it cannot know
-- that is why it joins them already running -- so a fixed target is a claim it
has no basis for. Every target it could name would either cut a move that was
still going or sit somewhere the move never reaches.

So the plan is a **trailing stop and nothing else**, and the questions are how
far behind and how it tightens:

- **How far behind** comes from the excursion profile: how much a winning move in
  this symbol normally retraces before continuing. A trail inside that is a
  machine for being taken out of moves that were still going, which is the same
  failure as a stop too tight and costs more here because the whole bot exists to
  hold winners.
- **How it tightens** comes from the exit counterfactual: the record of what
  *would* have been made by exiting at each point. It is the only measurement
  that can say whether this bot's trails have been too loose or too tight, and
  without it a trailing stop is a rule nobody is checking.

**A trail only ever moves in the trade's favour.** Not a rule of thumb: a trail
that could loosen would let a losing move argue its way into more room, which is
exactly the reasoning a person makes and a machine should not.

**A symbol with no excursion record gets no plan**, and this bot then holds
nothing in it (RL-062).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.price_frames import levels_in
from runtime.price_staleness import ObservedPrice
from runtime.bot_opinion import LONG, SHORT, ExitPlan, ExitTarget
from runtime.learned_estimator import Estimate, QuantileEstimator
from runtime.knowledge_types import TICK_SIZE
from runtime.trade_decoding_types import ExitCounterfactual
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "tail-trailing-exit-planner"
BOT = "profit-tailgating-bot"
CHECKPOINT_COMPONENT = "trailing-stops"

PART_DECLARATION = PartDeclaration(
    part_id="tail-trailing-exit-planner",
    consumes=(
        "follow-candidate", "symbol-price-frame", "symbol-profile",
        "exit-counterfactual", "excursion-profile", "move-remaining", "position",
    ),
    produces=("tail-exit-plan", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

NO_EXCURSION_PROFILE = "no-retracement-record-for-this-symbol"
NO_MOVE_REMAINING = "nothing-has-estimated-how-much-of-this-move-is-left"
NO_PRICE = "no-price-for-this-symbol"
TRAIL_WOULD_EXCEED_WHAT_IS_LEFT = "the-trail-is-wider-than-the-move-has-left"

# A trailing plan reaches its "target" only by being stopped out of a move that
# has turned. The plan still has to close the whole position, so the trail is
# recorded as the single exit that does it.
TRAIL_IS_THE_ONLY_EXIT = "the-trail-is-the-exit; this-bot-never-names-a-target"

# Matches the rule_name exit-counterfactual-replayer constructs for the one
# job it replays from this bot's own exit plan (see that part's read_jobs).
# Not shared via import -- the two parts agree on it only through the data on
# the wire, exactly as every other rule_name string in that block is a
# convention rather than an imported constant (T-4).
TAIL_TRAIL_RULE_NAME = "tail-exit-plan:trail"


def trail_key_text(key: tuple[str, str]) -> str:
    """One position's key as one string, for a JSON object that has only strings.

    The same separator stop-order-manager uses for its own resting-exit
    checkpoint, so a person reading both files by eye is reading the same shape
    for the same position.
    """
    return f"{key[0]}|{key[1]}"


def trail_key_of(text: str) -> tuple[str, str]:
    venue_id, _, symbol = text.partition("|")
    return venue_id, symbol


@dataclass(frozen=True)
class RetracementProfile:
    """How much a winning move in this symbol normally gives back before continuing."""

    venue_id: str
    symbol: str
    normal_retracement_fraction: float
    moves_observed: int
    is_fitted: bool


@dataclass
class PlannerStanding:
    plans_requested: int = 0
    plans_built: int = 0
    trails_tightened: int = 0
    trails_never_loosened: int = 0
    by_refusal: dict = field(default_factory=dict)
    widest_trail: float = 0.0
    counterfactuals_seen: int = 0
    # Not counters: what happened to the checkpoint at start. A part that came
    # back holding nothing and one whose checkpoint could not be read are
    # different facts, and only the second is a fault (Rule 8).
    restored_symbols: int = 0
    checkpoint_verdict: str = ""


class TailTrailingExitPlanner:
    """Plans a trail whose width is measured, and which can only ever tighten."""

    def __init__(
        self,
        trail_safety_multiple: float,
        minimum_trail_fraction: float,
        tighten_after_gain_fraction: float,
        tightened_trail_multiple: float,
        counterfactual_window: int,
        counterfactual_quantile: float,
        prior_trail_fraction: float,
        now_ns=time.time_ns,
    ) -> None:
        if trail_safety_multiple <= 1.0:
            raise ValueError(
                "a trail inside what a winning move normally retraces takes this bot out of "
                "the moves it exists to hold"
            )
        if not 0.0 < tightened_trail_multiple < 1.0:
            raise ValueError(
                "tightening multiplies the trail width, so it must be inside (0, 1); at or "
                "above one the trail would loosen"
            )
        self._trail_multiple = trail_safety_multiple
        self._minimum_trail = minimum_trail_fraction
        if tighten_after_gain_fraction <= 0:
            raise ValueError(
                "tightening happens once the follow is far enough ahead to be worth "
                "protecting; that distance must be positive"
            )
        self._tighten_after = tighten_after_gain_fraction
        self._tightened_multiple = tightened_trail_multiple
        self._counterfactual_quantile = counterfactual_quantile
        self._now_ns = now_ns
        self._prices: dict[tuple[str, str], float] = {}
        self._price_steps: dict[tuple[str, str], float] = {}
        self._profiles: dict[tuple[str, str], RetracementProfile] = {}
        self._counterfactual_trails: dict[tuple[str, str], QuantileEstimator] = {}
        self._counterfactual_window = counterfactual_window
        self._prior_trail = prior_trail_fraction
        self._standing_trails: dict[tuple[str, str], float] = {}
        self._entry_prices: dict[tuple[str, str], float] = {}
        # What a checkpoint is written against: incremented only when a trail is
        # set, moved, or released, so `CheckpointSchedule` can tell a tick that
        # changed something from one woken by an unrelated price print.
        self._mutations = 0
        self.standing = PlannerStanding()

    @property
    def checkpointable_mutations(self) -> int:
        return self._mutations

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        """One print, kept with the venue's own time for it.

        `at_ns` has no default. A price with no age cannot be told apart from a
        price that stopped arriving, which is how a symbol frozen for 56 minutes
        was traded on 2026-08-23.
        """
        self._prices[(venue_id, symbol)] = ObservedPrice(
            price=price, observed_at_ns=at_ns
        )

    def observe_symbol_profile(self, venue_id: str, symbol: str, price_step: float) -> None:
        self._price_steps[(venue_id, symbol)] = price_step

    def observe_retracement_profile(self, profile: RetracementProfile) -> None:
        self._profiles[(profile.venue_id, profile.symbol)] = profile

    def observe_exit_counterfactual(self, counterfactual: ExitCounterfactual) -> None:
        """What this bot's own trail width would have made, so it can be corrected.

        Only the trailing-stop replay of this bot's own plan is relevant --
        exit-counterfactual also carries fixed-target/fixed-stop replays of
        the bull/bear proposers' plans on the same wire, which say nothing
        about a trail width.
        """
        if counterfactual.rule_name != TAIL_TRAIL_RULE_NAME:
            return
        if counterfactual.trail_fraction is None or counterfactual.difference is None:
            return
        self.standing.counterfactuals_seen += 1
        key = (counterfactual.venue_id, counterfactual.symbol)
        estimator = self._counterfactual_trails.get(key)
        if estimator is None:
            estimator = QuantileEstimator(
                window=self._counterfactual_window, prior=self._prior_trail
            )
            self._counterfactual_trails[key] = estimator
        # difference > 0 means this trail width would have beaten the actual
        # exit -- the actual trail was too tight, so learn a wider one.
        if counterfactual.difference > 0:
            estimator.observe(counterfactual.trail_fraction * self._trail_multiple)
        else:
            estimator.observe(counterfactual.trail_fraction)

    def trail_width(self, venue_id: str, symbol: str) -> tuple[float | None, Estimate | None]:
        """How far behind price the trail sits, from the record rather than a rule."""
        key = (venue_id, symbol)
        profile = self._profiles.get(key)
        if profile is None or not profile.is_fitted:
            return None, None
        width = max(
            self._minimum_trail, profile.normal_retracement_fraction * self._trail_multiple
        )
        estimator = self._counterfactual_trails.get(key)
        counterfactual = (
            None
            if estimator is None
            else estimator.estimate(self._counterfactual_quantile, minimum_observations=1)
        )
        if counterfactual is not None and counterfactual.is_fitted:
            width = max(width, counterfactual.value)
        return width, counterfactual

    def plan(self, candidate, remaining) -> tuple[ExitPlan | None, str]:
        self.standing.plans_requested += 1
        key = (candidate.venue_id, candidate.symbol)

        observed = self._prices.get(key)
        price = None if observed is None else observed.price
        if price is None or price <= 0:
            return None, self._refuse(NO_PRICE)

        width, counterfactual = self.trail_width(*key)
        if width is None:
            return None, self._refuse(NO_EXCURSION_PROFILE)

        if remaining is None:
            # `tail-move-remaining-estimator` publishes this and until 2026-08-26
            # nothing consumed it: start_part handed every candidate a literal
            # None and `plan` dereferenced it, so the first follow-candidate the
            # qualifier ever produced crash-looped this part. Refused and named
            # rather than planned without the check -- a trail this bot cannot
            # compare against what the move has left is the one thing a tailgater
            # must not guess at, because joining a move already running is the
            # whole reason it has no target of its own.
            return None, self._refuse(NO_MOVE_REMAINING)

        left = remaining.remaining_fraction
        if left is not None and width > left:
            # A trail wider than what the move has left is stopped out by
            # ordinary noise before the move can pay for it.
            return None, self._refuse(TRAIL_WOULD_EXCEED_WHAT_IS_LEFT)

        if key in self._standing_trails:
            # Already tracking this position -- advance_trail owns the ratchet
            # from here; plan() must not reset it back toward the current
            # price on every candidate, which would loosen a trail that had
            # already tightened.
            stop_price = self._standing_trails[key]
        else:
            stop_price = self._trail_price(key, candidate.direction, price, width)
            self._standing_trails[key] = stop_price
            self._mutations += 1
        self._entry_prices.setdefault(key, price)
        self.standing.plans_built += 1
        self.standing.widest_trail = max(self.standing.widest_trail, width)

        # The plan's risk_fraction must describe stop_price's actual distance
        # from price, not the freshly-measured retracement width -- once the
        # ratchet has moved the stop, the two diverge, and
        # stop_target_placer.py inverts stop_price/(1 -+ risk_fraction) to
        # recover a reference price on the assumption that they agree.
        stop_distance_fraction = (
            (price - stop_price) / price
            if candidate.direction == LONG
            else (stop_price - price) / price
        )

        return (
            ExitPlan(
                bot=BOT,
                venue_id=candidate.venue_id,
                symbol=candidate.symbol,
                side=candidate.direction,
                stop_price=stop_price,
                targets=(
                    ExitTarget(
                        price=stop_price,
                        fraction=1.0,
                        reason=TRAIL_IS_THE_ONLY_EXIT,
                    ),
                ),
                invalidation_reason=(
                    f"the move turning by {stop_distance_fraction:.2%} is this bot's only exit; "
                    f"it joined a move whose size it cannot know, so naming a target would be a "
                    f"claim it has no basis for"
                ),
                horizon_seconds=0.0,
                risk_fraction=stop_distance_fraction,
                reward_to_risk=(
                    None if left is None or stop_distance_fraction <= 0
                    else left / stop_distance_fraction
                ),
                reason=(
                    f"trailing {width:.2%} behind {price:.8g}, which is "
                    f"{self._trail_multiple:.2g}x what a winning move in {candidate.symbol} "
                    f"normally retraces"
                    + (
                        f", widened to what the exit counterfactual says trails should have been "
                        f"({counterfactual.value:.2%})"
                        if counterfactual is not None and counterfactual.is_fitted
                        else ", with no exit counterfactual recorded yet to correct it"
                    )
                    + "; no target, because this bot cannot know how far a move it joined "
                    "already running will go"
                ),
                planned_at_ns=self._now_ns(),
            ),
            "planned",
        )

    def advance_trail(self, venue_id: str, symbol: str, direction: str, price: float) -> float | None:
        """Move the trail with the price, and never against the trade.

        A trail that could loosen would let a losing move argue its way into more
        room -- the reasoning a person makes and a machine should not.
        """
        key = (venue_id, symbol)
        standing = self._standing_trails.get(key)
        if standing is None:
            return None
        width, _ = self.trail_width(venue_id, symbol)
        if width is None:
            return standing

        # Tighten once the follow is far enough ahead that giving back a full
        # retracement would cost more than the trail protects. Before that the
        # wide trail is what keeps the bot in the move it exists to hold.
        candidate_width = width
        gain = self._gain_since_entry(key, direction, price)
        if gain is not None and gain >= self._tighten_after:
            candidate_width = width * self._tightened_multiple
            self.standing.trails_tightened += 1

        proposed = self._trail_price(key, direction, price, candidate_width)
        if direction == LONG:
            advanced = max(standing, proposed)
        else:
            advanced = min(standing, proposed)
        if advanced == standing and proposed != standing:
            self.standing.trails_never_loosened += 1
        if advanced != standing:
            self._mutations += 1
        self._standing_trails[key] = advanced
        return advanced

    def _gain_since_entry(self, key, direction: str, price: float) -> float | None:
        entry = self._entry_prices.get(key)
        if entry is None or entry <= 0:
            return None
        move = (price - entry) / entry
        return move if direction == LONG else -move

    def standing_trail(self, venue_id: str, symbol: str) -> float | None:
        return self._standing_trails.get((venue_id, symbol))

    def forget_position(self, venue_id: str, symbol: str) -> None:
        """A closed follow releases its trail. T-3: nothing accumulates for ever."""
        key = (venue_id, symbol)
        if self._standing_trails.pop(key, None) is not None:
            self._mutations += 1
        self._entry_prices.pop(key, None)

    # -- what survives a restart ----------------------------------------------
    #
    # Held in memory alone until 2026-08-29, the same defect stop-order-manager
    # had until 2026-08-26: every restart forgot every trail. The consequence is
    # not only a board that could never show one -- `advance_trail` ratchets from
    # whatever it remembers, so a restart that forgot a trail plans a fresh one
    # from wherever price now sits, which can only be looser than the trail it
    # replaces (a trail may never loosen, and a forgotten one is the same failure
    # by omission rather than by arithmetic).

    def read_checkpoint_state(self) -> dict:
        """The trailing stops this planner believes are resting, for the next process.

        Not the standing counters: those count what *this* process did.
        `positions_being_trailed` is recomputed from what came back, because
        that is a fact about the trails rather than about the process.
        """
        return {
            "trails": {
                trail_key_text(key): {
                    "stop_price": stop_price,
                    "entry_price": self._entry_prices.get(key),
                }
                for key, stop_price in self._standing_trails.items()
            },
        }

    def restore_from_checkpoint(self, state: dict) -> int:
        """Rebuild the trails that were resting. Returns how many came back."""
        trails = state.get("trails") or {}
        self._standing_trails = {}
        self._entry_prices = {}
        for text, held in trails.items():
            key = trail_key_of(text)
            self._standing_trails[key] = float(held["stop_price"])
            entry = held.get("entry_price")
            if entry is not None:
                self._entry_prices[key] = float(entry)
        self.standing.restored_symbols = len(self._standing_trails)
        return self.standing.restored_symbols

    def _trail_price(self, key, direction: str, price: float, width: float) -> float:
        if direction == LONG:
            return self._on_step(key, price * (1.0 - width), round_down=True)
        return self._on_step(key, price * (1.0 + width), round_down=False)

    def _on_step(self, key, price: float, round_down: bool) -> float:
        step = self._price_steps.get(key)
        if step is None or step <= 0:
            return price
        steps = price / step
        whole = int(steps)
        if round_down:
            return whole * step
        return (whole if steps == whole else whole + 1) * step

    def _refuse(self, reason: str) -> str:
        self.standing.by_refusal[reason] = self.standing.by_refusal.get(reason, 0) + 1
        return reason


def _apply_positions_and_prices(planner, positions, price_prints) -> None:
    """One tick's positions and price prints, applied to the planner.

    Extracted from read_candidates_and_market so forgetting a closed
    position and advancing a held one's trail are testable without going
    through start_part's Batch/LatestByKey wiring.
    """
    held_by_key = {
        (position.venue_id, position.symbol): position
        for position in positions if not position.is_flat
    }
    for position in positions:
        if position.is_flat:
            planner.forget_position(position.venue_id, position.symbol)
    for trade in price_prints:
        planner.observe_price(
            trade.venue_id, trade.symbol, trade.price, trade.observed_at_ns
        )
        held = held_by_key.get((trade.venue_id, trade.symbol))
        if held is not None:
            planner.advance_trail(trade.venue_id, trade.symbol, held.direction, trade.price)


def describe_trailing(planner: TailTrailingExitPlanner) -> dict:
    return {
        "part_id": PART_ID,
        "plans_requested": planner.standing.plans_requested,
        "plans_built": planner.standing.plans_built,
        "refused_by_reason": dict(planner.standing.by_refusal),
        "trails_tightened": planner.standing.trails_tightened,
        "times_a_trail_refused_to_loosen": planner.standing.trails_never_loosened,
        "widest_trail": planner.standing.widest_trail,
        "exit_counterfactuals_seen": planner.standing.counterfactuals_seen,
        "symbols_with_a_retracement_record": len(planner._profiles),
        "positions_being_trailed": len(planner._standing_trails),
        "restored_symbols": planner.standing.restored_symbols,
        "checkpoint_verdict": planner.standing.checkpoint_verdict,
    }


def run_tail_trailing_exit_planner(
    planner: TailTrailingExitPlanner, control_socket, read_candidates_and_market,
    publish_plans, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
    write_checkpoint=None,
) -> int:
    def tick() -> None:
        plans = []
        for candidate, remaining in read_candidates_and_market(planner):
            plan, _ = planner.plan(candidate, remaining)
            if plan is not None:
                plans.append(plan)
        publish_plans(tuple(plans))
        # After the plans go out, never before: a checkpoint written first would
        # record a trail this tick never actually planned against. Called every
        # tick; `write_checkpoint` itself skips the fsync when nothing moved
        # (CheckpointSchedule.is_due against `checkpointable_mutations`).
        if write_checkpoint is not None:
            write_checkpoint(planner.checkpointable_mutations)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_trailing(planner),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    import pathlib

    from runtime.durable_state import CheckpointSchedule, DurableStateStore, restore_and_arm_checkpoint
    from runtime.input_assembly import Batch, LatestByKey

    candidates = Batch(read=context.bus.reader("follow-candidate"))
    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    profiles = Batch(read=context.bus.reader("symbol-profile"))
    counterfactuals = Batch(read=context.bus.reader("exit-counterfactual"))
    excursions = Batch(read=context.bus.reader("excursion-profile"))
    positions = Batch(read=context.bus.reader("position"))
    # Bounded, like every level here: an estimate of how much of a move is left
    # is a statement about a move that is still running, and one held past the
    # move it described would trail a position against a number from a different
    # market. The estimator publishes per follow-candidate, so an ordinary
    # reading is far inside this and reaching it means the move is over.
    remaining = LatestByKey(
        read=context.bus.reader("move-remaining"),
        key_of=lambda estimate: (estimate.venue_id, estimate.symbol),
        maximum_age_seconds=context.number("tail_move_remaining_maximum_age_seconds"),
    )
    publish_plans = context.bus.publisher_for("tail-exit-plan")
    planner = TailTrailingExitPlanner(
        trail_safety_multiple=context.number("tail_trail_safety_multiple"),
        minimum_trail_fraction=context.number("tail_minimum_trail_fraction"),
        tighten_after_gain_fraction=context.number("tail_tighten_after_gain_fraction"),
        tightened_trail_multiple=context.number("tail_tightened_trail_multiple"),
        counterfactual_window=int(context.number("tail_counterfactual_window")),
        counterfactual_quantile=context.number("tail_counterfactual_quantile"),
        prior_trail_fraction=context.number("tail_prior_trail_fraction"),
    )
    # Beside the resting-exit checkpoint stop-order-manager keeps, under
    # position_state_root: it is the same fact about the same position, and a
    # board reading one should not have to look somewhere else for the other.
    store = DurableStateStore(
        pathlib.Path(str(context.setting("position_state_root").value)).expanduser()
    )
    store.root.mkdir(parents=True, exist_ok=True)
    write_checkpoint = restore_and_arm_checkpoint(
        store,
        # Every change, not every N: a trail changes when a follow opens, moves,
        # or closes -- tens of times an hour -- and losing one costs the ratchet
        # its memory, the same reasoning stop-order-manager uses for its own
        # resting exits.
        CheckpointSchedule(1),
        PART_ID,
        CHECKPOINT_COMPONENT,
        planner,
        {},
    )

    def read_candidates_and_market(_planner):
        _apply_positions_and_prices(
            planner, positions.payloads(), levels_in(trades.payloads())
        )
        for profile in profiles.payloads():
            # `tick-size`, from the closed key set a profile's fields are filed
            # under. It was read as "price_increment" -- a key no profile has ever
            # carried -- and the fields hold ProfileField objects rather than
            # floats, so this never observed a step and never said so.
            step = profile.value_of(TICK_SIZE)
            if step:
                planner.observe_symbol_profile(profile.venue_id, profile.symbol, float(step))
        for counterfactual in counterfactuals.payloads():
            planner.observe_exit_counterfactual(counterfactual)
        for profile in excursions.payloads():
            # An excursion-profile is not a retracement profile, and this part
            # handed one straight to a method that reads
            # `normal_retracement_fraction` -- a field ExcursionProfile has never
            # had. What it does carry is `adverse_excursion`: how far a call that
            # came right went against itself first, which is exactly how much a
            # winner gives back before continuing, measured from real trades.
            planner.observe_retracement_profile(
                RetracementProfile(
                    venue_id=profile.venue_id,
                    symbol=profile.symbol,
                    normal_retracement_fraction=profile.adverse_excursion,
                    moves_observed=profile.trades_observed,
                    is_fitted=profile.is_fitted,
                )
            )
        left_by_symbol = remaining.mapping()
        return tuple(
            (candidate, left_by_symbol.get((candidate.venue_id, candidate.symbol)))
            for candidate in candidates.payloads()
        )

    def publish(items) -> None:
        if items:
            publish_plans(items)

    return run_tail_trailing_exit_planner(
        planner=planner,
        control_socket=context.control_socket,
        read_candidates_and_market=read_candidates_and_market,
        publish_plans=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
        write_checkpoint=write_checkpoint,
    )
