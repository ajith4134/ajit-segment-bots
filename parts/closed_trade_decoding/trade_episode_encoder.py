"""trade-episode-encoder: one trade, assembled into the record everything learns from.

Eight separate analyses land on the same trade -- attribution, entry quality, loss
cause, significance, regime, clustering, near misses, timing -- and every learning
part downstream needs them together. This encoder is where they become one immutable
episode.

Its correctness rules are about completeness and permanence:

- **An episode is written once and never revised.** A memory that can be edited is
  one that will be, and the value of an episode is that it records what was known
  when the trade closed rather than what is believed now. A later, better analysis
  becomes a new episode that supersedes it, with both kept.
- **A partial episode says which pieces are missing.** Encoding with holes filled by
  defaults produces a record that looks complete, and every downstream statistic
  computed over it silently includes fabricated fields.
- **Clustered trades carry their cluster.** Without it, ten correlated trades enter
  the learning set as ten independent examples and every model trained on them is
  overconfident.
- **Insignificant outcomes are marked, not dropped.** They are legitimate evidence
  that a setup produces noise, and dropping them leaves a training set of nothing
  but decisive results -- which is the shape that teaches a system every trade should
  be decisive.

The conditions dictionary is the part a model actually reads, so it holds only
measured values with their provenance, never a summary sentence.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.knowledge_types import TradeEpisode
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "trade-episode-encoder"

PART_DECLARATION = PartDeclaration(
    part_id="trade-episode-encoder",
    consumes=(
        "closed-trade", "journal-entry", "pnl-attribution", "near-miss-episode",
        "regime-transition-flag", "entry-quality", "outcome-significance", "trade-cluster",
    ),
    produces=("trade-episode", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

ENCODED = "encoded"
INCOMPLETE = "some-analyses-have-not-landed-yet"
ALREADY_ENCODED = "an-episode-already-exists-for-this-trade"
SUPERSEDED = "superseded-by-a-later-encoding"

# The pieces an episode wants. Absent ones are named rather than defaulted.
REQUIRED_PIECES = ("attribution", "entry_quality", "significance")
OPTIONAL_PIECES = ("loss_cause", "regime_flag", "cluster", "exit_quality", "stop_audit")


@dataclass(frozen=True)
class EncodingOutcome:
    trade_id: str
    state: str
    episode: TradeEpisode | None
    missing: tuple
    supersedes: str | None
    reason: str
    encoded_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state in (ENCODED, SUPERSEDED) and self.episode is not None


@dataclass
class EncoderStanding:
    episodes_encoded: int = 0
    refused_incomplete: int = 0
    supersessions: int = 0
    edits_attempted: int = 0
    clustered_episodes: int = 0
    insignificant_episodes_kept: int = 0
    # Ticks on which a trade was already encoded from exactly these analyses.
    # Counted rather than silent: a large number here is the encoder being
    # asked the same question repeatedly, which is worth seeing, and a zero
    # once trades are closing would mean the check stopped working.
    re_encodes_skipped: int = 0
    pieces_missing: dict = field(default_factory=dict)


class TradeEpisodeEncoder:
    """Assembles the analyses into one immutable episode per trade."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._episodes: dict[str, TradeEpisode] = {}
        self._history: dict[str, list] = {}
        self._sequence = 0
        self.standing = EncoderStanding()

    def encode(
        self, trade_id: str, closed_trade, detector: str, action: str, outcome: str,
        attribution=None, entry_quality=None, significance=None, loss_cause=None,
        regime_flag=None, cluster=None, exit_quality=None, stop_audit=None,
        narrative: str = "",
    ) -> EncodingOutcome:
        pieces = {
            "attribution": attribution, "entry_quality": entry_quality,
            "significance": significance, "loss_cause": loss_cause,
            "regime_flag": regime_flag, "cluster": cluster,
            "exit_quality": exit_quality, "stop_audit": stop_audit,
        }
        missing = tuple(
            name for name in REQUIRED_PIECES if pieces[name] is None
        )
        if missing:
            self.standing.refused_incomplete += 1
            for name in missing:
                self.standing.pieces_missing[name] = (
                    self.standing.pieces_missing.get(name, 0) + 1
                )
            return self._outcome(
                trade_id, INCOMPLETE, None, missing, None,
                f"{', '.join(missing)} has not landed yet. Filling it with a default "
                f"produces a record that looks complete, and every statistic over it "
                f"would silently include a fabricated field",
            )

        # Only measured values go into conditions: this is what a model reads.
        conditions = {
            "realised_pnl": closed_trade.realised_pnl,
            "holding_seconds": closed_trade.holding_seconds,
            "entry_percentile": entry_quality.percentile,
            "was_chasing": entry_quality.was_chasing,
            "cost_share": attribution.cost_share,
            "attribution_residual": attribution.residual,
            "standardised_outcome": significance.standardised,
            "is_significant": significance.is_significant,
        }
        for name in OPTIONAL_PIECES:
            piece = pieces[name]
            if piece is None:
                continue
            if name == "loss_cause":
                conditions["loss_cause"] = piece.cause
                conditions["loss_was_avoidable"] = piece.was_avoidable
            elif name == "regime_flag":
                conditions["regime_changed"] = piece.changed
                conditions["fraction_in_the_new_regime"] = (
                    piece.fraction_of_the_trade_in_the_new_regime
                )
            elif name == "cluster":
                conditions["cluster_id"] = piece.cluster_id
                conditions["effective_bets"] = piece.effective_bets
                self.standing.clustered_episodes += 1
            elif name == "exit_quality":
                conditions["captured_fraction"] = piece.captured_fraction
            elif name == "stop_audit":
                conditions["stop_verdict"] = piece.verdict
                conditions["stop_in_typical_movements"] = (
                    piece.distance_in_typical_movements
                )

        if not significance.is_significant:
            # Kept: a training set of nothing but decisive results teaches a system
            # that every trade should be decisive.
            self.standing.insignificant_episodes_kept += 1

        self._sequence += 1
        existing = self._episodes.get(trade_id)
        episode = TradeEpisode(
            episode_id=f"episode-{trade_id}-{self._sequence}",
            venue_id=closed_trade.venue_id,
            symbol=closed_trade.symbol,
            detector=detector,
            regime=(
                regime_flag.regime_at_entry
                if regime_flag is not None and regime_flag.regime_at_entry
                else "unknown"
            ),
            conditions=conditions,
            action=action,
            outcome=outcome,
            realised=closed_trade.realised_pnl,
            opened_at_ns=closed_trade.opened_at_ns,
            closed_at_ns=closed_trade.closed_at_ns,
            narrative=narrative,
        )
        self._episodes[trade_id] = episode
        self._history.setdefault(trade_id, []).append(episode)
        self.standing.episodes_encoded += 1

        if existing is not None:
            self.standing.supersessions += 1
            return self._outcome(
                trade_id, SUPERSEDED, episode, (), existing.episode_id,
                f"supersedes {existing.episode_id}. Both are kept: an episode records "
                f"what was known when it was written, and editing it would destroy the "
                f"evidence of how understanding changed",
            )

        return self._outcome(
            trade_id, ENCODED, episode, (), None,
            f"{len([piece for piece in pieces.values() if piece is not None])} analysis "
            f"result(s) assembled into one immutable episode"
            + (
                ". The outcome is not distinguishable from noise, and it is kept as such"
                if not significance.is_significant
                else ""
            ),
        )

    def episode_for(self, trade_id: str) -> TradeEpisode | None:
        return self._episodes.get(trade_id)

    def history_for(self, trade_id: str) -> tuple:
        """Every encoding of this trade, oldest first. Nothing is discarded."""
        return tuple(self._history.get(trade_id, ()))

    def _outcome(
        self, trade_id, state, episode, missing, supersedes, reason,
    ) -> EncodingOutcome:
        return EncodingOutcome(
            trade_id=trade_id, state=state, episode=episode, missing=missing,
            supersedes=supersedes, reason=reason, encoded_at_ns=self._now_ns(),
        )


def describe_episode_encoding(encoder: TradeEpisodeEncoder) -> dict:
    return {
        "part_id": PART_ID,
        "episodes_encoded": encoder.standing.episodes_encoded,
        "refused_incomplete": encoder.standing.refused_incomplete,
        "supersessions": encoder.standing.supersessions,
        "clustered_episodes": encoder.standing.clustered_episodes,
        "insignificant_episodes_kept": encoder.standing.insignificant_episodes_kept,
        "re_encodes_skipped": encoder.standing.re_encodes_skipped,
        "pieces_missing": dict(encoder.standing.pieces_missing),
        "required_pieces": list(REQUIRED_PIECES),
        "can_edit_an_episode": False,
        "edits_attempted": encoder.standing.edits_attempted,
        "fills_missing_analyses_with_defaults": False,
        "drops_insignificant_outcomes": False,
    }


def run_trade_episode_encoder(
    encoder: TradeEpisodeEncoder, control_socket, read_trades, publish_episodes,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for job in read_trades():
            outcome = encoder.encode(**job)
            if outcome.is_usable:
                publish_episodes(outcome.episode)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_episode_encoding(encoder),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Each analysis of a trade arrives on its own type; the encoder keeps the
    latest per trade and re-encodes when a new one lands, superseding the
    earlier episode. The detector and action come from the journal's
    entry-candidate and trade-intent for the symbol; absent, they are named
    unknown.
    """
    from runtime.input_assembly import Batch, LatestByKey
    from runtime.trade_identity import closed_trade_id

    closed = Batch(read=context.bus.reader("closed-trade"))
    entries = Batch(read=context.bus.reader("journal-entry"))
    by_trade = {
        kind: LatestByKey(read=context.bus.reader(kind), key_of=lambda item: item.trade_id)
        for kind in ("pnl-attribution", "entry-quality", "outcome-significance", "regime-transition-flag")
    }
    near_misses = Batch(read=context.bus.reader("near-miss-episode"))
    clusters = Batch(read=context.bus.reader("trade-cluster"))
    publish_episodes = context.bus.publisher_for("trade-episode")
    encoder = TradeEpisodeEncoder()
    closed_by_id: dict[str, object] = {}
    detector_of: dict[tuple[str, str], str] = {}
    action_of: dict[tuple[str, str], str] = {}
    cluster_of: dict[str, object] = {}
    # What each trade was last encoded from. `LatestByKey.mapping()` returns every
    # key it still retains rather than the ones that just arrived, so without this
    # every trade ever closed is re-encoded on every tick, forever: one closed
    # trade had become 3,048 episodes and was still climbing when this was found,
    # and winner-pattern-miner had counted it 2.8 million times.
    encoded_from: dict[str, tuple] = {}

    def read_trades():
        for entry in entries.payloads():
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            key = (str(payload.get("venue_id", "")), str(payload.get("symbol", "")))
            if entry.kind == "entry-candidate":
                detector_of[key] = str(payload.get("detector", "unknown"))
            elif entry.kind == "trade-intent" and payload.get("action") not in (None, "stand-aside"):
                action_of[key] = str(payload.get("action"))
        for cluster in clusters.payloads():
            for trade_id in cluster.trade_ids:
                cluster_of[trade_id] = cluster
        near_misses.payloads()
        touched = set()
        for trade in closed.payloads():
            trade_id = closed_trade_id(trade)
            closed_by_id[trade_id] = trade
            touched.add(trade_id)
        for source in by_trade.values():
            for trade_id in source.mapping():
                if trade_id in closed_by_id:
                    touched.add(trade_id)
        jobs = []
        for trade_id in sorted(touched):
            trade = closed_by_id[trade_id]
            key = (trade.venue_id, trade.symbol)
            analyses = (
                by_trade["pnl-attribution"].mapping().get(trade_id),
                by_trade["entry-quality"].mapping().get(trade_id),
                by_trade["outcome-significance"].mapping().get(trade_id),
                by_trade["regime-transition-flag"].mapping().get(trade_id),
                cluster_of.get(trade_id),
                detector_of.get(key, "unknown"),
                action_of.get(key, trade.direction),
            )
            # Re-encode when an analysis actually lands, which is what supersession
            # is for -- not when the same analyses are simply still retained.
            if encoded_from.get(trade_id) == analyses:
                encoder.standing.re_encodes_skipped += 1
                continue
            encoded_from[trade_id] = analyses
            jobs.append({
                "trade_id": trade_id, "closed_trade": trade,
                "detector": detector_of.get(key, "unknown"), "action": action_of.get(key, trade.direction),
                "outcome": "profit" if trade.realised_pnl > 0 else "loss",
                "attribution": by_trade["pnl-attribution"].mapping().get(trade_id),
                "entry_quality": by_trade["entry-quality"].mapping().get(trade_id),
                "significance": by_trade["outcome-significance"].mapping().get(trade_id),
                "regime_flag": by_trade["regime-transition-flag"].mapping().get(trade_id),
                "cluster": cluster_of.get(trade_id),
            })
        return tuple(jobs)

    return run_trade_episode_encoder(
        encoder=encoder,
        control_socket=context.control_socket,
        read_trades=read_trades,
        publish_episodes=lambda episode: publish_episodes((episode,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
