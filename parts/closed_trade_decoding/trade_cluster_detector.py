"""trade-cluster-detector: which trades were really one bet.

Ten longs opened within a minute across ten correlated alts is not ten bets. It is
one bet on the same move, with ten sets of fees, and every statistic computed over
those trades as independent samples is overconfident -- roughly by the square root of
the cluster size. That error compounds through everything downstream: win rates,
significance tests, and the risk sizing that reads them.

Clustering here is on three things together, because any one alone is wrong:

- **Time.** Trades opened far apart are separate decisions even in the same symbol.
- **Direction.** A long and a short on correlated instruments at the same moment are
  a hedge, not a doubled bet, and lumping them together is worse than not clustering.
- **Correlation.** The instruments have to actually move together. Two unrelated
  symbols traded at the same instant are two bets that happened to be simultaneous.

The output is an **effective number of bets**, not just a group: with perfect
correlation ten trades are one bet, with none they are ten, and the useful cases are
in between. Downstream statistics divide by that number rather than by the trade
count, which is the whole reason this part exists.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.trade_decoding_types import TradeCluster
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "trade-cluster-detector"

PART_DECLARATION = PartDeclaration(
    part_id="trade-cluster-detector",
    consumes=("closed-trade", "correlation-cluster"),
    produces=("trade-cluster", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

CLUSTERED = "clustered"
INDEPENDENT = "these-trades-were-separate-bets"
NOTHING_TO_CLUSTER = "fewer-than-two-trades-to-consider"


@dataclass(frozen=True)
class ClusterOutcome:
    state: str
    clusters: tuple
    independent_trades: tuple
    reason: str
    detected_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == CLUSTERED and bool(self.clusters)


@dataclass
class DetectorStanding:
    trades_examined: int = 0
    clusters_found: int = 0
    trades_in_clusters: int = 0
    independent_trades: int = 0
    largest_cluster: int = 0
    smallest_effective_bets: float | None = None
    hedges_not_clustered: int = 0


class TradeClusterDetector:
    """Groups trades that were one bet, and says how many bets they really were."""

    def __init__(
        self,
        window_seconds: float,
        minimum_correlation: float,
        now_ns=time.time_ns,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError(
                "trades opened far apart are separate decisions, so clustering needs a "
                "window"
            )
        if not 0.0 < minimum_correlation <= 1.0:
            raise ValueError(
                "two unrelated symbols traded at the same instant are two bets that "
                "happened to be simultaneous, so correlation is required"
            )
        self._window_seconds = window_seconds
        self._minimum_correlation = minimum_correlation
        self._now_ns = now_ns
        self._groups: dict[str, str] = {}
        self._correlations: dict[tuple, float] = {}
        self._sequence = 0
        self.standing = DetectorStanding()

    def observe_correlation_group(self, symbol: str, group: str) -> None:
        self._groups[symbol] = group

    def observe_correlation(self, left: str, right: str, correlation: float) -> None:
        self._correlations[tuple(sorted((left, right)))] = correlation

    def correlation_between(self, left: str, right: str) -> float:
        if left == right:
            return 1.0
        pair = tuple(sorted((left, right)))
        if pair in self._correlations:
            return self._correlations[pair]
        # Same declared group means correlated enough to matter, absent a number.
        if self._groups.get(left) and self._groups.get(left) == self._groups.get(right):
            return self._minimum_correlation
        return 0.0

    def effective_bets(self, symbols) -> float:
        """N trades with average pairwise correlation r behave like N / (1 + (N-1)r)."""
        count = len(symbols)
        if count <= 1:
            return float(count)
        pairs = [
            self.correlation_between(symbols[i], symbols[j])
            for i in range(count)
            for j in range(i + 1, count)
        ]
        average = sum(pairs) / len(pairs) if pairs else 0.0
        return count / (1.0 + (count - 1) * max(average, 0.0))

    def detect(self, trades) -> ClusterOutcome:
        """`trades` is a sequence of (trade_id, closed_trade)."""
        trades = list(trades)
        self.standing.trades_examined += len(trades)
        if len(trades) < 2:
            return self._outcome(
                NOTHING_TO_CLUSTER, (), tuple(trade_id for trade_id, _ in trades),
                "fewer than two trades to consider",
            )

        ordered = sorted(trades, key=lambda pair: pair[1].opened_at_ns)
        used: set = set()
        clusters = []

        for index, (trade_id, trade) in enumerate(ordered):
            if trade_id in used:
                continue
            members = [(trade_id, trade)]
            for other_id, other in ordered[index + 1 :]:
                if other_id in used:
                    continue
                within = (
                    abs(other.opened_at_ns - trade.opened_at_ns) / 1e9
                    <= self._window_seconds
                )
                # A long against a correlated short is a hedge, not a doubled bet.
                same_direction = other.direction == trade.direction
                correlated = (
                    self.correlation_between(trade.symbol, other.symbol)
                    >= self._minimum_correlation
                )
                if within and correlated and not same_direction:
                    self.standing.hedges_not_clustered += 1
                if within and same_direction and correlated:
                    members.append((other_id, other))

            if len(members) < 2:
                continue

            for member_id, _ in members:
                used.add(member_id)
            symbols = [member.symbol for _, member in members]
            effective = self.effective_bets(symbols)
            self._sequence += 1
            span = (
                max(member.opened_at_ns for _, member in members)
                - min(member.opened_at_ns for _, member in members)
            ) / 1e9
            cluster = TradeCluster(
                cluster_id=f"cluster-{self._sequence}",
                trade_ids=tuple(member_id for member_id, _ in members),
                correlation_group=self._groups.get(trade.symbol),
                direction=trade.direction,
                opened_within_seconds=span,
                effective_bets=effective,
                reason=(
                    f"{len(members)} {trade.direction} trade(s) opened within "
                    f"{span:.0f}s across {len(set(symbols))} correlated symbol(s). These "
                    f"are {effective:.2f} bet(s), not {len(members)} -- any statistic "
                    f"treating them as independent is overconfident by roughly "
                    f"{math.sqrt(len(members) / effective):.2f}x"
                ),
                detected_at_ns=self._now_ns(),
            )
            clusters.append(cluster)
            self.standing.clusters_found += 1
            self.standing.trades_in_clusters += len(members)
            self.standing.largest_cluster = max(
                self.standing.largest_cluster, len(members)
            )
            if (
                self.standing.smallest_effective_bets is None
                or effective < self.standing.smallest_effective_bets
            ):
                self.standing.smallest_effective_bets = effective

        independent = tuple(
            trade_id for trade_id, _ in ordered if trade_id not in used
        )
        self.standing.independent_trades += len(independent)

        if not clusters:
            return self._outcome(
                INDEPENDENT, (), independent,
                "no two trades were close enough in time, direction and correlation to "
                "have been one bet",
            )

        return self._outcome(
            CLUSTERED, tuple(clusters), independent,
            f"{len(clusters)} cluster(s) covering "
            f"{sum(len(cluster.trade_ids) for cluster in clusters)} trade(s), "
            f"{len(independent)} independent",
        )

    def _outcome(self, state, clusters, independent, reason) -> ClusterOutcome:
        return ClusterOutcome(
            state=state, clusters=clusters, independent_trades=independent,
            reason=reason, detected_at_ns=self._now_ns(),
        )


def describe_clustering(detector: TradeClusterDetector) -> dict:
    return {
        "part_id": PART_ID,
        "trades_examined": detector.standing.trades_examined,
        "clusters_found": detector.standing.clusters_found,
        "trades_in_clusters": detector.standing.trades_in_clusters,
        "independent_trades": detector.standing.independent_trades,
        "largest_cluster": detector.standing.largest_cluster,
        "smallest_effective_bets": detector.standing.smallest_effective_bets,
        "hedges_correctly_not_clustered": detector.standing.hedges_not_clustered,
        "clusters_on_time_alone": False,
        "clusters_a_hedge_as_a_doubled_bet": False,
    }


def run_trade_cluster_detector(
    detector: TradeClusterDetector, control_socket, read_trades, publish_clusters,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        outcome = detector.detect(read_trades())
        for cluster in outcome.clusters:
            publish_clusters(cluster)

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

    Closed trades inside the window are clustered together; the correlation
    mapper's groups say which symbols move together. The window of recent
    trades is kept here, bounded to the decoding window.
    """
    from collections import deque

    from runtime.input_assembly import Batch
    from runtime.trade_identity import closed_trade_id

    closed = Batch(read=context.bus.reader("closed-trade"))
    clusters = Batch(read=context.bus.reader("correlation-cluster"))
    publish_clusters = context.bus.publisher_for("trade-cluster")
    detector = TradeClusterDetector(
        window_seconds=context.number("trade_cluster_window"),
        minimum_correlation=context.number("trade_cluster_minimum_correlation"),
    )
    recent = deque(maxlen=int(context.number("decoding_window")))

    def read_trades():
        for cluster in clusters.payloads():
            members = getattr(cluster, "members", None) or getattr(cluster, "symbols", ())
            group = getattr(cluster, "cluster_id", None) or getattr(cluster, "group", "")
            for symbol in members:
                detector.observe_correlation_group(symbol, str(group))
            pairs = getattr(cluster, "correlations", None)
            if isinstance(pairs, dict):
                for (left, right), value in pairs.items():
                    detector.observe_correlation(left, right, float(value))
        for trade in closed.payloads():
            recent.append((closed_trade_id(trade), trade))
        return tuple(recent)

    return run_trade_cluster_detector(
        detector=detector,
        control_socket=context.control_socket,
        read_trades=read_trades,
        publish_clusters=lambda cluster: publish_clusters((cluster,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
