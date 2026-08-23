"""opinion-conflict-resolver: what to do when the bots contradict each other.

Three bots look at the same symbol and can reach opposite conclusions. That is
not a malfunction -- it is the point of running three, and a system that averaged
them would produce a fourth opinion nobody holds and nobody can defend.

So disagreement is ruled on rather than blended, and the rulings are few and
explicit:

- **Stand aside.** The default, and the most common correct answer. Two bots
  pointing opposite ways in a regime where neither has an edge is the market
  saying it is unreadable, and taking the louder one is a coin flip with costs.
- **Favour the bot whose regime this is.** A reverting regime is the mean
  reverter's; a trend is not. This only applies where the regime is classified
  and the favoured bot's record *in that regime* supports it -- not its record
  overall, which is where this kind of ruling usually goes wrong.
- **Favour maturity.** A bot with a long record in this regime beats one still
  being sampled, and the gap has to be large: a bot with thirty trades is not
  meaningfully more proven than one with twenty-five.

**A ruling is recorded even when it is "do nothing".** A trade not taken because
two bots contradicted each other is a decision, and a system that records only
what it did cannot learn from what it declined.

**Regime memory is what stops the same ruling being re-derived every tick** --
and what makes it visible when a ruling that was right for months stops working.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import LONG, SHORT
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trade_intent import ConflictRuling

PART_ID = "opinion-conflict-resolver"

PART_DECLARATION = PartDeclaration(
    part_id="opinion-conflict-resolver",
    consumes=("directional-opinion", "market-regime", "bot-maturity", "regime-memory"),
    produces=("conflict-ruling", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

NO_CONFLICT = "no-conflict-to-rule-on"
STAND_ASIDE = "stand-aside"
FAVOUR_THE_REGIME = "favour-the-bot-whose-regime-this-is"
FAVOUR_MATURITY = "favour-the-bot-with-the-longer-record-here"
FOLLOW_REGIME_MEMORY = "follow-what-this-regime-has-taught-before"

# Which bot each classified regime belongs to. Not a preference: it is the
# statement of what each bot is for, and a ruling that ignored it would be
# favouring a bot in the market it was built to lose in.
BOT_OF_REGIME = {
    "trending": ("bull-bot", "profit-tailgating-bot"),
    "reverting": ("bull-bot", "bear-bot"),
}


@dataclass(frozen=True)
class BotMaturity:
    """How far a bot is from being ranked rather than sampled, in this regime."""

    bot: str
    regime: str
    trades_here: int
    is_mature: bool


@dataclass
class ResolverStanding:
    conflicts_seen: int = 0
    ruled: int = 0
    stood_aside: int = 0
    by_ruling: dict = field(default_factory=dict)
    memory_hits: int = 0
    memory_contradicted: int = 0


class OpinionConflictResolver:
    """Rules on disagreement between bots, and records the ruling either way."""

    def __init__(
        self,
        maturity_gap_trades: int,
        minimum_regime_hit_rate: float,
        remember_rulings: bool,
        now_ns=time.time_ns,
    ) -> None:
        if maturity_gap_trades < 1:
            raise ValueError(
                "a gap of zero would let thirty trades outrank twenty-five, which is not a "
                "difference in evidence"
            )
        self._maturity_gap = maturity_gap_trades
        self._minimum_regime_hit_rate = minimum_regime_hit_rate
        self._remember = remember_rulings
        self._now_ns = now_ns
        self._maturity: dict[tuple[str, str], BotMaturity] = {}
        self._regime_hit_rate: dict[tuple[str, str], float] = {}
        self._memory: dict[tuple[str, str], ConflictRuling] = {}
        self.standing = ResolverStanding()

    def observe_bot_maturity(self, maturity: BotMaturity) -> None:
        self._maturity[(maturity.bot, maturity.regime)] = maturity

    def observe_regime_hit_rate(self, bot: str, regime: str, hit_rate: float) -> None:
        """A bot's record *in this regime*, which is what a regime ruling turns on."""
        self._regime_hit_rate[(bot, regime)] = hit_rate

    def remembered_ruling(self, symbol: str, regime: str) -> ConflictRuling | None:
        return self._memory.get((symbol, regime))

    def resolve(self, opinions, regime) -> ConflictRuling:
        """One symbol's opinions, ruled on. `opinions` are calls to act, not stand-downs."""
        acting = [opinion for opinion in opinions if opinion.is_a_call_to_act]
        symbol = opinions[0].symbol if opinions else ""
        venue_id = opinions[0].venue_id if opinions else ""

        sides = {opinion.side for opinion in acting}
        if len(sides) < 2:
            return self._ruling(
                venue_id, symbol, NO_CONFLICT, None, (), regime,
                f"{len(acting)} bot(s) want to act and they point the same way; there is "
                f"nothing to rule on",
            )

        self.standing.conflicts_seen += 1
        longs = tuple(sorted(o.bot for o in acting if o.side == LONG))
        shorts = tuple(sorted(o.bot for o in acting if o.side == SHORT))

        remembered = self._memory.get((symbol, regime.regime))
        if remembered is not None and remembered.favoured_bot is not None:
            still_wants = any(
                opinion.bot == remembered.favoured_bot for opinion in acting
            )
            if still_wants:
                self.standing.memory_hits += 1
                opposed = tuple(
                    opinion.bot for opinion in acting if opinion.bot != remembered.favoured_bot
                )
                return self._ruling(
                    venue_id, symbol, FOLLOW_REGIME_MEMORY, remembered.favoured_bot, opposed,
                    regime.regime,
                    f"this conflict has been ruled on before in {regime.regime} and "
                    f"{remembered.favoured_bot} was favoured: {remembered.grounds}",
                )
            self.standing.memory_contradicted += 1

        if regime.is_classified:
            favoured = self._bot_the_regime_belongs_to(acting, regime.regime)
            if favoured is not None:
                opposed = tuple(o.bot for o in acting if o.bot != favoured)
                return self._ruling(
                    venue_id, symbol, FAVOUR_THE_REGIME, favoured, opposed, regime.regime,
                    f"{regime.regime} is the regime {favoured} is built for, and its record "
                    f"*in this regime* is "
                    f"{self._regime_hit_rate.get((favoured, regime.regime), 0.0):.0%}, above "
                    f"the {self._minimum_regime_hit_rate:.0%} a regime ruling needs; the "
                    f"overall record is deliberately not what this turns on",
                )

        mature = self._clearly_more_mature(acting, regime.regime)
        if mature is not None:
            opposed = tuple(o.bot for o in acting if o.bot != mature)
            return self._ruling(
                venue_id, symbol, FAVOUR_MATURITY, mature, opposed, regime.regime,
                f"{mature} has at least {self._maturity_gap} more closed trades in "
                f"{regime.regime} than every bot opposing it; a smaller gap is not a "
                f"difference in evidence",
            )

        return self._ruling(
            venue_id, symbol, STAND_ASIDE, None, longs + shorts, regime.regime,
            f"{'/'.join(longs) or 'nobody'} want long and {'/'.join(shorts) or 'nobody'} want "
            f"short in {regime.regime}, and nothing separates them; two bots pointing opposite "
            f"ways where neither has an edge is the market being unreadable, and taking the "
            f"louder one is a coin flip with costs",
        )

    def _bot_the_regime_belongs_to(self, acting, regime: str) -> str | None:
        """The one acting bot this regime is for, if its record here supports it."""
        candidates = [
            opinion.bot
            for opinion in acting
            if opinion.bot in BOT_OF_REGIME.get(regime, ())
            and self._regime_hit_rate.get((opinion.bot, regime), 0.0)
            >= self._minimum_regime_hit_rate
        ]
        # Exactly one, or the regime does not separate them either.
        return candidates[0] if len(candidates) == 1 else None

    def _clearly_more_mature(self, acting, regime: str) -> str | None:
        matured = [
            (self._maturity.get((opinion.bot, regime)), opinion.bot) for opinion in acting
        ]
        known = [(maturity, bot) for maturity, bot in matured if maturity is not None]
        if len(known) < len(matured) or len(known) < 2:
            return None
        known.sort(key=lambda entry: entry[0].trades_here, reverse=True)
        best, runner_up = known[0], known[1]
        if not best[0].is_mature:
            return None
        if best[0].trades_here - runner_up[0].trades_here < self._maturity_gap:
            return None
        return best[1]

    def _ruling(self, venue_id, symbol, ruling, favoured, opposed, regime, grounds) -> ConflictRuling:
        if ruling != NO_CONFLICT:
            self.standing.by_ruling[ruling] = self.standing.by_ruling.get(ruling, 0) + 1
            if favoured is None:
                self.standing.stood_aside += 1
            else:
                self.standing.ruled += 1
        result = ConflictRuling(
            venue_id=venue_id,
            symbol=symbol,
            ruling=ruling,
            favoured_bot=favoured,
            opposed_bots=tuple(opposed),
            grounds=grounds,
            regime=regime,
            ruled_at_ns=self._now_ns(),
        )
        if self._remember and ruling in (FAVOUR_THE_REGIME, FAVOUR_MATURITY):
            self._memory[(symbol, regime)] = result
        return result

    def forget_regime(self, regime: str) -> int:
        """Drop what this regime taught, when the regime itself has broken.

        A ruling that was right for months and stops working is the failure this
        part is most exposed to, so the memory is dropped by whoever observes
        the break rather than decaying quietly on its own.
        """
        stale = [key for key in self._memory if key[1] == regime]
        for key in stale:
            del self._memory[key]
        return len(stale)


def describe_conflict_resolution(resolver: OpinionConflictResolver) -> dict:
    return {
        "part_id": PART_ID,
        "conflicts_seen": resolver.standing.conflicts_seen,
        "ruled_for_a_bot": resolver.standing.ruled,
        "stood_aside": resolver.standing.stood_aside,
        "by_ruling": dict(sorted(resolver.standing.by_ruling.items())),
        "rulings_recalled_from_memory": resolver.standing.memory_hits,
        "remembered_rulings_no_longer_applicable": resolver.standing.memory_contradicted,
        "rulings_held_in_memory": len(resolver._memory),
    }


def run_opinion_conflict_resolver(
    resolver: OpinionConflictResolver, control_socket, read_opinions_and_regime,
    publish_rulings, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_rulings(
            tuple(
                resolver.resolve(opinions, regime)
                for opinions, regime in read_opinions_and_regime(resolver)
            )
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Opinions are gathered per symbol; a ruling is made for each symbol that
    received an opinion this wake, in that symbol's current regime. A bot's
    hit rate in a regime is read off the maturity the graduation gate
    publishes, as its conditions record it, when that record carries one.
    """
    from runtime.input_assembly import Batch, LatestByKey

    opinions = Batch(read=context.bus.reader("directional-opinion"))
    regimes = LatestByKey(read=context.bus.reader("market-regime"), key_of=lambda r: (r.venue_id, r.symbol))
    maturities = Batch(read=context.bus.reader("bot-maturity"))
    memories = Batch(read=context.bus.reader("regime-memory"))
    publish_rulings = context.bus.publisher_for("conflict-ruling")
    resolver = OpinionConflictResolver(
        maturity_gap_trades=int(context.number("exploration_maturity_gap_trades")),
        minimum_regime_hit_rate=context.number("conflict_minimum_regime_hit_rate"),
        remember_rulings=bool(context.setting("conflict_remember_rulings").value),
    )
    held: dict[tuple[str, str], dict] = {}

    def read_opinions_and_regime(_resolver):
        for maturity in maturities.payloads():
            resolver.observe_bot_maturity(
                BotMaturity(
                    bot=maturity.bot, regime=maturity.regime,
                    trades_here=maturity.trades_here, is_mature=maturity.is_mature,
                )
            )
            hit_rate = (maturity.conditions_met or {}).get("hit_rate") if isinstance(maturity.conditions_met, dict) else None
            if isinstance(hit_rate, (int, float)):
                resolver.observe_regime_hit_rate(maturity.bot, maturity.regime, float(hit_rate))
        for memory in memories.payloads():
            if getattr(memory, "state", "") == "ended":
                resolver.forget_regime(memory.regime)
        touched = set()
        for opinion in opinions.payloads():
            key = (opinion.venue_id, opinion.symbol)
            held.setdefault(key, {})[opinion.bot] = opinion
            touched.add(key)
        regime_by_symbol = regimes.mapping()
        return tuple(
            (tuple(held[key].values()), regime_by_symbol[key])
            for key in sorted(touched) if key in regime_by_symbol and len(held[key]) > 1
        )

    def publish(rulings) -> None:
        if rulings:
            publish_rulings(rulings)

    return run_opinion_conflict_resolver(
        resolver=resolver,
        control_socket=context.control_socket,
        read_opinions_and_regime=read_opinions_and_regime,
        publish_rulings=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
