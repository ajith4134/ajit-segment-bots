"""near-miss-recorder: the trades that were not taken, and what happened next.

Without this part the record contains only trades that were taken, which is
survivorship applied to this system's own decisions. A filter that rejects every good
setup looks flawless in that record, because nothing it rejected appears anywhere.
The rejections are where the most correctable information lives, and they are free --
no capital was risked to produce them.

What makes a near miss usable rather than noise:

- **The reason it was not taken is recorded at the time.** Reconstructed afterwards
  it becomes whatever now seems plausible, and the whole point is to find out which
  reasons are systematically wrong.
- **It resolves on a horizon fixed in advance.** Otherwise the resolution is chosen
  after seeing the outcome -- pick a long enough window and almost every skipped long
  eventually looks right.
- **Costs are subtracted from the counterfactual.** A skipped trade that would have
  made less than the fees was correctly skipped, and a gross comparison declares
  hundreds of correct refusals to be mistakes.
- **A near miss is not a signal.** Nothing here re-enters anything. It is recorded so
  the filters can be scored later, and acting on it directly would make the recorder
  a second, unreviewed trading path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.price_frames import levels_in
from runtime.trade_decoding_types import NearMissEpisode
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "near-miss-recorder"

PART_DECLARATION = PartDeclaration(
    part_id="near-miss-recorder",
    consumes=("entry-candidate", "trade-intent", "directional-opinion", "symbol-price-frame"),
    produces=("near-miss-episode", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

RECORDED = "recorded"
RESOLVED = "resolved"
NOT_YET_RESOLVED = "the-horizon-has-not-elapsed"
NO_PRICE = "no-price-arrived-to-resolve-it-against"
ALREADY_RECORDED = "already-recorded"


@dataclass(frozen=True)
class NearMissOutcome:
    episode_id: str
    state: str
    episode: NearMissEpisode | None
    reason: str
    at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.episode is not None


@dataclass
class RecorderStanding:
    near_misses_recorded: int = 0
    resolved: int = 0
    unresolved: int = 0
    skips_that_were_right: int = 0
    skips_that_were_mistakes: int = 0
    duplicates: int = 0
    never_resolved_no_price: int = 0
    by_reason: dict = field(default_factory=dict)


class NearMissRecorder:
    """Records refusals with their stated reason and resolves them on a fixed horizon."""

    def __init__(
        self,
        horizon_seconds: float,
        round_trip_cost_fraction: float,
        now_ns=time.time_ns,
    ) -> None:
        if horizon_seconds <= 0:
            raise ValueError(
                "the horizon is fixed in advance: chosen afterwards, almost every "
                "skipped long eventually looks right"
            )
        if round_trip_cost_fraction < 0:
            raise ValueError(
                "a gross comparison declares hundreds of correct refusals to be mistakes, "
                "so the round-trip cost is subtracted"
            )
        self._horizon_seconds = horizon_seconds
        self._cost_fraction = round_trip_cost_fraction
        self._now_ns = now_ns
        self._episodes: dict[str, NearMissEpisode] = {}
        self._prices: dict[tuple, list] = {}
        self.standing = RecorderStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        self._prices.setdefault((venue_id, symbol), []).append((at_ns, price))

    def record(
        self, episode_id: str, venue_id: str, symbol: str, side: str,
        reference_price: float, why_not_taken: str, considered_at_ns: int,
    ) -> NearMissOutcome:
        if episode_id in self._episodes:
            self.standing.duplicates += 1
            return self._outcome(
                episode_id, ALREADY_RECORDED, self._episodes[episode_id],
                "already recorded",
            )

        episode = NearMissEpisode(
            episode_id=episode_id, venue_id=venue_id, symbol=symbol, side=side,
            reference_price=reference_price, why_not_taken=why_not_taken,
            would_have_realised=None, is_resolved=False,
            considered_at_ns=considered_at_ns, resolved_at_ns=None,
        )
        self._episodes[episode_id] = episode
        self.standing.near_misses_recorded += 1
        self.standing.unresolved += 1
        self.standing.by_reason[why_not_taken] = (
            self.standing.by_reason.get(why_not_taken, 0) + 1
        )
        return self._outcome(
            episode_id, RECORDED, episode,
            f"{side} on {symbol} not taken: {why_not_taken}. The reason is recorded now, "
            f"because reconstructed later it becomes whatever seems plausible -- and which "
            f"reasons are systematically wrong is the question",
        )

    def resolve(self, episode_id: str) -> NearMissOutcome:
        episode = self._episodes.get(episode_id)
        if episode is None or episode.is_resolved:
            return self._outcome(
                episode_id, ALREADY_RECORDED, episode, "nothing to resolve",
            )

        resolve_at = episode.considered_at_ns + int(self._horizon_seconds * 1e9)
        if self._now_ns() < resolve_at:
            return self._outcome(
                episode_id, NOT_YET_RESOLVED, episode,
                f"the {self._horizon_seconds:.0f}s horizon has not elapsed",
            )

        prices = [
            price
            for at_ns, price in self._prices.get((episode.venue_id, episode.symbol), [])
            if at_ns >= resolve_at
        ]
        if not prices:
            self.standing.never_resolved_no_price += 1
            return self._outcome(
                episode_id, NO_PRICE, episode,
                "no price arrived at or after the horizon, so this cannot be resolved",
            )

        exit_price = prices[0]
        sign = 1.0 if episode.side == "long" else -1.0
        gross = sign * (exit_price - episode.reference_price) / episode.reference_price
        # A skipped trade that would have made less than the costs was skipped correctly.
        net = gross - self._cost_fraction

        resolved = NearMissEpisode(
            episode_id=episode.episode_id, venue_id=episode.venue_id,
            symbol=episode.symbol, side=episode.side,
            reference_price=episode.reference_price,
            why_not_taken=episode.why_not_taken, would_have_realised=net,
            is_resolved=True, considered_at_ns=episode.considered_at_ns,
            resolved_at_ns=self._now_ns(),
        )
        self._episodes[episode_id] = resolved
        self.standing.resolved += 1
        self.standing.unresolved -= 1
        if resolved.was_a_mistake_to_skip:
            self.standing.skips_that_were_mistakes += 1
        else:
            self.standing.skips_that_were_right += 1

        return self._outcome(
            episode_id, RESOLVED, resolved,
            f"would have made {net:+.2%} net over {self._horizon_seconds:.0f}s "
            f"({gross:+.2%} gross, {self._cost_fraction:.2%} of costs subtracted). "
            + (
                "Skipping it was a mistake"
                if resolved.was_a_mistake_to_skip
                else "Skipping it was right"
            )
            + ". Nothing re-enters from here: this is recorded so the filters can be "
              "scored, not so a second trading path can act on it",
        )

    def mistakes_by_reason(self) -> dict:
        """Which stated reasons systematically rejected trades that would have paid."""
        counts: dict = {}
        for episode in self._episodes.values():
            if not episode.is_resolved:
                continue
            entry = counts.setdefault(episode.why_not_taken, [0, 0])
            entry[1] += 1
            if episode.was_a_mistake_to_skip:
                entry[0] += 1
        return {
            reason: {"mistakes": mistakes, "total": total}
            for reason, (mistakes, total) in sorted(counts.items())
        }

    def _outcome(self, episode_id, state, episode, reason) -> NearMissOutcome:
        return NearMissOutcome(
            episode_id=episode_id, state=state, episode=episode, reason=reason,
            at_ns=self._now_ns(),
        )


def describe_near_misses(recorder: NearMissRecorder) -> dict:
    return {
        "part_id": PART_ID,
        "near_misses_recorded": recorder.standing.near_misses_recorded,
        "resolved": recorder.standing.resolved,
        "unresolved": recorder.standing.unresolved,
        "skips_that_were_right": recorder.standing.skips_that_were_right,
        "skips_that_were_mistakes": recorder.standing.skips_that_were_mistakes,
        "never_resolved_for_want_of_a_price": (
            recorder.standing.never_resolved_no_price
        ),
        "by_reason": dict(recorder.standing.by_reason),
        "mistakes_by_reason": recorder.mistakes_by_reason(),
        "compares_gross": False,
        "re_enters_anything": False,
    }


def run_near_miss_recorder(
    recorder: NearMissRecorder, control_socket, read_refusals, publish_episodes,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for job in read_refusals():
            outcome = recorder.record(**job)
            if outcome.is_usable:
                publish_episodes(outcome.episode)
        for episode_id in list(recorder._episodes):
            outcome = recorder.resolve(episode_id)
            if outcome.state == RESOLVED:
                publish_episodes(outcome.episode)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_near_misses(recorder),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A candidate the brain stood aside on is a near miss: the intent's reason
    says why, the latest print is the reference, and the recorder follows
    the price for its horizon to say what was missed.
    """
    from runtime.input_assembly import Batch

    candidates = Batch(read=context.bus.reader("entry-candidate"))
    intents = Batch(read=context.bus.reader("trade-intent"))
    opinions = Batch(read=context.bus.reader("directional-opinion"))
    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    publish_episodes = context.bus.publisher_for("near-miss-episode")
    recorder = NearMissRecorder(
        horizon_seconds=context.number("near_miss_horizon"),
        round_trip_cost_fraction=context.number("reference_price_materiality_fraction"),
    )
    prices: dict[tuple[str, str], float] = {}
    latest_candidate: dict[tuple[str, str], object] = {}

    def read_refusals():
        for trade in levels_in(trades.payloads()):
                prices[(trade.venue_id, trade.symbol)] = trade.price
                recorder.observe_price(trade.venue_id, trade.symbol, trade.price, trade.observed_at_ns)
        for candidate in candidates.payloads():
            latest_candidate[(candidate.venue_id, candidate.symbol)] = candidate
        opinions.payloads()
        jobs = []
        for intent in intents.payloads():
            if intent.is_actionable:
                continue
            key = (intent.venue_id, intent.symbol)
            candidate = latest_candidate.get(key)
            price = prices.get(key)
            if candidate is None or price is None:
                continue
            jobs.append({
                "episode_id": f"{key[0]}:{key[1]}:{intent.formed_at_ns}",
                "venue_id": key[0], "symbol": key[1], "side": candidate.direction,
                "reference_price": price, "why_not_taken": intent.reason,
                "considered_at_ns": intent.formed_at_ns,
            })
        return tuple(jobs)

    return run_near_miss_recorder(
        recorder=recorder,
        control_socket=context.control_socket,
        read_refusals=read_refusals,
        publish_episodes=lambda episode: publish_episodes((episode,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
