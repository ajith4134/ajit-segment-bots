"""market-thesis-reasoner: what is this market doing right now, across symbols,
and why -- step 3 of docs/proposals/llm-reasoning-gets-a-vote.md (RL-010/013/026),
built last because it is the only one of the three with no existing candidate to
confirm: it originates its own vote, so it carries the most of this proposal's
own weight to get right.

**Direction is measured, never asserted by the model.** `market-regime` alone
carries no sign (it is a Hurst proxy: trending/reverting/random, never up/down)
and a single `verified-snapshot` is one instant with no history in it -- between
them the two declared inputs carry no directional fact for a model to be
checked against. So this part keeps its own short rolling window of a
bellwether symbol's `verified-snapshot` prices (the same `RollingWindow`
`regime-classifier` itself uses) and reads the *sign* of its own measured
recent price change. `market-regime.favours(...)` then says whether that
regime supports betting the measured direction continues or reverses --
exactly the method it exists to answer, per `regime_classifier.py`. The model
is asked only to phrase why, and -- like `setup-second-opinion-reasoner` --
its answer can downgrade a confirmation to a stand-down if nothing it wrote
survives being checked against the facts, never invent a direction of its own.

**`timing=None, exit_plan=None`, always**, for the same reason as
`setup-second-opinion-reasoner`: an LLM should not be inventing a stop price.
`opinion-arbiter`'s `_planned_or_acting` already refuses to let a planless
opinion source a trade-intent's stop_price/horizon.

**Bounded by a settings-named symbol list, not by discovery.** A settings list
of bellwether symbols, reviewed no more often than
`market_thesis_review_interval_seconds` per symbol -- an unbounded "reason
about everything" would be an unbounded token bill, which is exactly what
`part-token-budgeter` and this cadence both exist to prevent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from runtime.bot_opinion import ENTER_NOW, LONG, SHORT, DirectionalOpinion, stand_down
from runtime.claim_verification import make_request, verify_against_facts, written_without_a_model
from runtime.learned_estimator import RateEstimator
from runtime.market_signal import CONTINUATION, REVERSION
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow

PART_ID = "market-thesis-reasoner"
BOT = "market-thesis-reasoner"

PART_DECLARATION = PartDeclaration(
    part_id="market-thesis-reasoner",
    consumes=("market-regime", "verified-snapshot", "validated-llm-output"),
    produces=("directional-opinion", "llm-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PURPOSE = "review-the-thesis-for-a-bellwether-symbol"

INSTRUCTION = (
    "This is a bellwether symbol's measured recent price direction and the regime "
    "that supports it. Judge whether this thesis holds and say why, using only the "
    "numbers given -- every number you write must be one of them. Cite the numbers "
    "that support it, or write nothing if none do."
)

NOT_ENOUGH_PRICE_HISTORY = "not-enough-price-history-in-the-window"
NO_REGIME_EDGE = "the-regime-gives-neither-continuation-nor-reversion-an-edge"
MODEL_FOUND_NOTHING_TO_CONFIRM = "the-model-cited-nothing-that-survived-verification"


@dataclass
class ThesisStanding:
    reviews_written: int = 0
    confirmed: int = 0
    stood_aside_for_no_edge: int = 0
    refused_after_the_model_found_nothing: int = 0
    model_sentences_removed: int = 0


class MarketThesisReasoner:
    """Reads its own measured price direction, lets the regime say whether it
    has an edge, and asks a model only to phrase why."""

    def __init__(
        self,
        bellwether_symbols,
        window_length: int,
        minimum_window_observations: int,
        review_interval_seconds: float,
        prior_hit_rate: float,
        prior_weight: float,
        half_life_observations: float,
        minimum_observations: int,
        relative_tolerance: float,
        maximum_sentences: int,
        now_ns=time.time_ns,
    ) -> None:
        if review_interval_seconds <= 0:
            raise ValueError("a review interval of zero would ask a model the same question every tick")
        self._bellwether = frozenset(bellwether_symbols)
        self._window_length = window_length
        self._minimum_window = minimum_window_observations
        self._review_interval_seconds = review_interval_seconds
        self._prior_hit_rate = prior_hit_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._minimum = minimum_observations
        self._tolerance = relative_tolerance
        self._maximum_sentences = maximum_sentences
        self._now_ns = now_ns
        self._windows: dict[tuple[str, str], RollingWindow] = {}
        self._regimes: dict[tuple[str, str], object] = {}
        self._records: dict[str, RateEstimator] = {}
        self._last_reviewed_at_ns: dict[tuple[str, str], int] = {}
        self.standing = ThesisStanding()

    def is_bellwether(self, symbol: str) -> bool:
        return symbol in self._bellwether

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        key = (venue_id, symbol)
        window = self._windows.get(key)
        if window is None:
            window = RollingWindow(length=self._window_length)
            self._windows[key] = window
        window.observe(price, at_ns)

    def observe_regime(self, regime) -> None:
        self._regimes[(regime.venue_id, regime.symbol)] = regime

    def due_for_review(self, key: tuple[str, str], now_ns: int) -> bool:
        last = self._last_reviewed_at_ns.get(key)
        return last is None or (now_ns - last) / 1e9 >= self._review_interval_seconds

    def _record_for(self, regime_name: str) -> RateEstimator:
        record = self._records.get(regime_name)
        if record is None:
            record = RateEstimator(
                prior=self._prior_hit_rate, prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._records[regime_name] = record
        return record

    def measured_side(self, key: tuple[str, str]) -> str | None:
        """The sign of this window's own measured recent price change, or None
        below the minimum observations, or on a series that has not moved."""
        window = self._windows.get(key)
        if window is None or window.count < self._minimum_window:
            return None
        change = sum(window.returns())
        if change > 0:
            return LONG
        if change < 0:
            return SHORT
        return None

    def thesis_side(self, key: tuple[str, str]) -> tuple[str | None, str]:
        """What the regime says about the window's own measured direction --
        continuation, reversion, or no edge -- never a direction it invents."""
        regime = self._regimes.get(key)
        recent = self.measured_side(key)
        if recent is None:
            return None, NOT_ENOUGH_PRICE_HISTORY
        if regime is None or not regime.is_classified:
            return None, NO_REGIME_EDGE
        if regime.favours(CONTINUATION):
            return recent, f"{regime.regime}: betting the measured recent direction continues"
        if regime.favours(REVERSION):
            opposite = SHORT if recent == LONG else LONG
            return opposite, f"{regime.regime}: betting against the measured recent direction"
        return None, NO_REGIME_EDGE

    def facts_for(self, key: tuple[str, str]) -> dict:
        window = self._windows[key]
        regime = self._regimes.get(key)
        facts = {
            "recent_cumulative_return": round(sum(window.returns()), 6),
            "window_observations": window.count,
        }
        if regime is not None and regime.hurst is not None:
            facts["hurst"] = round(regime.hurst, 4)
        return facts

    def request(self, venue_id: str, symbol: str):
        return make_request(
            purpose=PURPOSE,
            venue_id=venue_id,
            symbol=symbol,
            instruction=INSTRUCTION,
            facts=self.facts_for((venue_id, symbol)),
            maximum_sentences=self._maximum_sentences,
            now_ns=self._now_ns,
        )

    def review(self, venue_id: str, symbol: str, model_output: str | None = None) -> DirectionalOpinion:
        key = (venue_id, symbol)
        self.standing.reviews_written += 1
        side, thesis_reason = self.thesis_side(key)

        if side is None:
            self.standing.stood_aside_for_no_edge += 1
            self._last_reviewed_at_ns[key] = self._now_ns()
            return stand_down(
                BOT, self.measured_side(key) or LONG, venue_id, symbol,
                refusal=thesis_reason, reason=thesis_reason, now_ns=self._now_ns,
            )

        regime = self._regimes[key]
        facts = self.facts_for(key)
        confidence = self._record_for(regime.regime).estimate(self._minimum)
        if model_output is None:
            verified = written_without_a_model(f"{side} {symbol}: {thesis_reason}", facts)
        else:
            # The same invariant setup-second-opinion-reasoner holds: a verdict
            # resting on whether the model cited a real number, never on its prose.
            verified = verify_against_facts(model_output, facts, self._tolerance, require_a_citation=True)
            self.standing.model_sentences_removed += len(verified.removed_sentences)
            if verified.is_empty:
                self.standing.refused_after_the_model_found_nothing += 1
                self._last_reviewed_at_ns[key] = self._now_ns()
                return stand_down(
                    BOT, side, venue_id, symbol,
                    refusal=MODEL_FOUND_NOTHING_TO_CONFIRM,
                    reason=f"nothing the model wrote about {symbol} survived verification",
                    now_ns=self._now_ns,
                )

        self.standing.confirmed += 1
        self._last_reviewed_at_ns[key] = self._now_ns()
        return DirectionalOpinion(
            bot=BOT,
            side=side,
            venue_id=venue_id,
            symbol=symbol,
            action=ENTER_NOW,
            conviction=confidence,
            timing=None,
            exit_plan=None,
            features_summary={},
            refusal=None,
            reason=verified.text or thesis_reason,
            formed_at_ns=self._now_ns(),
        )


def describe_thesis(reasoner: MarketThesisReasoner) -> dict:
    return {
        "part_id": PART_ID,
        "reviews_written": reasoner.standing.reviews_written,
        "confirmed": reasoner.standing.confirmed,
        "stood_aside_for_no_edge": reasoner.standing.stood_aside_for_no_edge,
        "refused_after_the_model_found_nothing": reasoner.standing.refused_after_the_model_found_nothing,
        "model_sentences_removed": reasoner.standing.model_sentences_removed,
        "bellwether_symbols_tracked": len(reasoner._windows),
    }


def run_market_thesis_reasoner(
    reasoner: MarketThesisReasoner, control_socket, read_reviews_and_output,
    publish_opinions, publish_requests, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        opinions = []
        requests = []
        for (venue_id, symbol), model_output in read_reviews_and_output(reasoner):
            # No point spending a token on a symbol that already has no measured
            # edge: the model's answer could not change that verdict either way.
            if model_output is None and reasoner.thesis_side((venue_id, symbol))[0] is not None:
                requests.append(reasoner.request(venue_id, symbol))
            opinions.append(reasoner.review(venue_id, symbol, model_output))
        publish_opinions(tuple(opinions))
        publish_requests(tuple(requests))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_thesis(reasoner),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A verified-snapshot for a bellwether symbol feeds its price window every
    time one arrives, cheaply; a review is only triggered, and only an
    llm-request sent, once due_for_review says the interval has passed.
    """
    from runtime.input_assembly import Batch

    snapshots = Batch(read=context.bus.reader("verified-snapshot"))
    regimes = Batch(read=context.bus.reader("market-regime"))
    outputs = Batch(read=context.bus.reader("validated-llm-output"))
    publish_opinions = context.bus.publisher_for("directional-opinion")
    publish_requests = context.bus.publisher_for("llm-request")

    reasoner = MarketThesisReasoner(
        bellwether_symbols=tuple(str(symbol) for symbol in context.setting("market_thesis_bellwether_symbols").value),
        window_length=int(context.number("market_thesis_window_length")),
        minimum_window_observations=int(context.number("market_thesis_minimum_window_observations")),
        review_interval_seconds=context.number("market_thesis_review_interval_seconds"),
        prior_hit_rate=context.number("brain_prior_hit_rate"),
        prior_weight=context.number("brain_prior_weight"),
        half_life_observations=context.number("brain_half_life_observations"),
        minimum_observations=int(context.number("brain_minimum_observations")),
        relative_tolerance=context.number("llm_claim_relative_tolerance"),
        maximum_sentences=int(context.number("llm_maximum_sentences")),
    )
    pending: set[tuple[str, str]] = set()

    def read_reviews_and_output(active_reasoner):
        for snapshot in snapshots.payloads():
            if not active_reasoner.is_bellwether(snapshot.symbol) or not snapshot.can_be_used_as_ground_truth:
                continue
            price = snapshot.facts.get("mid", snapshot.facts.get("last-trade"))
            if price is not None:
                active_reasoner.observe_price(snapshot.venue_id, snapshot.symbol, float(price), snapshot.measured_at_ns)

        for regime in regimes.payloads():
            if active_reasoner.is_bellwether(regime.symbol):
                active_reasoner.observe_regime(regime)

        judgements = []
        for output in outputs.payloads():
            if output.purpose != PURPOSE:
                continue
            value = output.value if isinstance(output.value, dict) else {}
            key = (str(value.get("venue_id", "")), str(value.get("symbol", "")))
            if key in pending:
                pending.discard(key)
                judgements.append((key, output.text))

        now_ns = time.time_ns()
        for key in sorted(active_reasoner._windows):
            if key in pending:
                continue
            if not active_reasoner.due_for_review(key, now_ns):
                continue
            # Only a key with a measured edge produces an outstanding request to
            # correlate later -- a no-edge stand-down resolves in this same tick
            # and due_for_review's own interval, not `pending`, governs when it
            # is looked at again.
            if active_reasoner.thesis_side(key)[0] is not None:
                pending.add(key)
            judgements.append((key, None))
        return judgements

    return run_market_thesis_reasoner(
        reasoner=reasoner,
        control_socket=context.control_socket,
        read_reviews_and_output=read_reviews_and_output,
        publish_opinions=publish_opinions,
        publish_requests=publish_requests,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
