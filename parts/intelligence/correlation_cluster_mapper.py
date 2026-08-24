"""correlation-cluster-mapper: which symbols are really one symbol.

Diversification is a claim about correlation, and in crypto the claim is usually
false: a book across twenty alts is one position in the market factor plus noise.
This part measures what actually moves together, so the exposure watch and the
risk gate are working from a measurement rather than from a list of tickers.

The measurement decisions that matter:

- **Correlation of returns, not of prices.** Two prices that both trend upward
  correlate at 0.99 and share no risk; the returns tell you whether they move
  together *today*, which is the question.
- **Rolling, and decayed.** Correlation is a property of a period. A pair that
  correlated at 0.9 through a rally and 0.2 through the chop after it has neither
  number as its correlation, and a mapper reporting the average would describe a
  market that no longer exists.
- **Clusters are transitive but reported with their weakest link.** A and B at
  0.9 and B and C at 0.9 puts all three in one cluster, and A and C may correlate
  at 0.5. The cluster is still the right unit for risk -- they move together
  enough to fail together -- and the weakest pair is reported so the strength of
  the claim is visible.
- **A pair with too little shared history is unmeasured, never zero.** Treating
  it as uncorrelated is how a book becomes concentrated in exactly the symbols
  nobody has data on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.price_frames import levels_in
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow, correlation

PART_ID = "correlation-cluster-mapper"

PART_DECLARATION = PartDeclaration(
    part_id="correlation-cluster-mapper",
    consumes=("symbol-price-frame",),
    produces=("correlation-cluster", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)


@dataclass(frozen=True)
class CorrelationCluster:
    """Symbols that move together closely enough to be one exposure."""

    name: str
    symbols: tuple
    average_correlation: float
    weakest_pair: tuple
    weakest_correlation: float
    observations: int
    all_pairs_measured: bool
    reason: str
    mapped_at_ns: int

    @property
    def is_a_single_exposure(self) -> bool:
        return len(self.symbols) > 1


@dataclass
class MapperStanding:
    observations: int = 0
    mappings: int = 0
    clusters_formed: int = 0
    unmeasured_pairs: int = 0
    symbols_tracked: int = 0
    largest_cluster_seen: int = 0
    strongest_correlation_seen: float | None = None


class CorrelationClusterMapper:
    """Measures what moves together, on returns, over a window that forgets."""

    def __init__(
        self,
        window_length: int,
        minimum_shared_observations: int,
        cluster_threshold: float,
        maximum_gap_seconds: float | None = None,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < cluster_threshold <= 1.0:
            raise ValueError(
                "the threshold is a correlation; at zero every symbol joins every cluster"
            )
        if minimum_shared_observations < 3:
            raise ValueError(
                "a correlation from two points is one, whatever the two points are"
            )
        self._window = window_length
        self._minimum = minimum_shared_observations
        self._threshold = cluster_threshold
        self._now_ns = now_ns
        # How long a symbol may be silent before its window is judged to have a
        # hole in it rather than a series. None means the caller stated no bound,
        # and this part does not invent one (RL-061).
        self._maximum_gap_seconds = maximum_gap_seconds
        self._prices: dict[str, RollingWindow] = {}
        self.standing = MapperStanding()

    def observe_price(self, symbol: str, price: float, at_ns: int) -> None:
        self.standing.observations += 1
        window = self._prices.get(symbol)
        if window is None:
            window = RollingWindow(
                length=self._window,
                maximum_gap_seconds=self._maximum_gap_seconds,
            )
            self._prices[symbol] = window
        window.observe(price, at_ns)
        self.standing.symbols_tracked = len(self._prices)

    def correlation_between(self, left: str, right: str) -> tuple[float | None, int]:
        """Correlation of returns, not of prices.

        Two prices that both trend up correlate at 0.99 and share no risk; the
        returns answer whether they move together today.
        """
        left_window = self._prices.get(left)
        right_window = self._prices.get(right)
        if left_window is None or right_window is None:
            return None, 0
        left_returns = left_window.returns()
        right_returns = right_window.returns()
        length = min(len(left_returns), len(right_returns))
        if length < self._minimum:
            return None, length
        return correlation(left_returns[-length:], right_returns[-length:]), length

    def map(self) -> tuple[CorrelationCluster, ...]:
        self.standing.mappings += 1
        symbols = sorted(self._prices)
        if len(symbols) < 2:
            return ()

        parent = {symbol: symbol for symbol in symbols}
        pair_correlations: dict[tuple[str, str], float] = {}
        unmeasured = 0

        def find(symbol):
            while parent[symbol] != symbol:
                parent[symbol] = parent[parent[symbol]]
                symbol = parent[symbol]
            return symbol

        for index, left in enumerate(symbols):
            for right in symbols[index + 1 :]:
                value, observations = self.correlation_between(left, right)
                if value is None:
                    # Unmeasured, never zero: treating it as uncorrelated is how
                    # a book concentrates in the symbols nobody has data on.
                    unmeasured += 1
                    continue
                pair_correlations[(left, right)] = value
                if abs(value) >= self._threshold:
                    parent[find(left)] = find(right)
                    if (
                        self.standing.strongest_correlation_seen is None
                        or abs(value) > self.standing.strongest_correlation_seen
                    ):
                        self.standing.strongest_correlation_seen = abs(value)

        self.standing.unmeasured_pairs = unmeasured

        grouped: dict[str, list] = {}
        for symbol in symbols:
            grouped.setdefault(find(symbol), []).append(symbol)

        clusters = []
        for root, members in sorted(grouped.items()):
            members = sorted(members)
            pairs = [
                (pair, value)
                for pair, value in pair_correlations.items()
                if pair[0] in members and pair[1] in members
            ]
            if pairs:
                average = sum(abs(value) for _, value in pairs) / len(pairs)
                weakest_pair, weakest = min(pairs, key=lambda entry: abs(entry[1]))
                weakest = abs(weakest)
            else:
                average = 1.0
                weakest_pair, weakest = (root, root), 1.0

            expected_pairs = len(members) * (len(members) - 1) // 2
            clusters.append(
                CorrelationCluster(
                    name=root,
                    symbols=tuple(members),
                    average_correlation=average,
                    weakest_pair=weakest_pair,
                    weakest_correlation=weakest,
                    observations=min(
                        (self.correlation_between(*pair)[1] for pair, _ in pairs),
                        default=0,
                    ),
                    all_pairs_measured=len(pairs) == expected_pairs,
                    reason=(
                        f"{len(members)} symbol(s) moving together at {average:.2f} average "
                        f"correlation"
                        + (
                            f"; the weakest link is {weakest_pair[0]}/{weakest_pair[1]} at "
                            f"{weakest:.2f}, and the cluster is still the right unit for risk "
                            f"because they move together enough to fail together"
                            if len(members) > 2
                            else ""
                        )
                        + (
                            ""
                            if len(pairs) == expected_pairs
                            else f"; {expected_pairs - len(pairs)} pair(s) in this cluster have "
                            f"too little shared history to measure"
                        )
                    ),
                    mapped_at_ns=self._now_ns(),
                )
            )

        self.standing.clusters_formed += len(clusters)
        self.standing.largest_cluster_seen = max(
            self.standing.largest_cluster_seen,
            max((len(cluster.symbols) for cluster in clusters), default=0),
        )
        return tuple(clusters)

    def release(self, symbol: str) -> None:
        """T-3."""
        self._prices.pop(symbol, None)
        self.standing.symbols_tracked = len(self._prices)


def describe_correlation_clusters(mapper: CorrelationClusterMapper) -> dict:
    clusters = mapper.map()
    return {
        "part_id": PART_ID,
        "observations": mapper.standing.observations,
        "symbols_tracked": mapper.standing.symbols_tracked,
        "mappings": mapper.standing.mappings,
        "clusters_now": len(clusters),
        "largest_cluster": max((len(cluster.symbols) for cluster in clusters), default=0),
        "unmeasured_pairs": mapper.standing.unmeasured_pairs,
        "strongest_correlation_seen": mapper.standing.strongest_correlation_seen,
        "clusters": [
            {"symbols": list(cluster.symbols), "average": cluster.average_correlation}
            for cluster in clusters
            if cluster.is_a_single_exposure
        ],
    }


def run_correlation_cluster_mapper(
    mapper: CorrelationClusterMapper, control_socket, read_prices, publish_clusters,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_prices(mapper)
        publish_clusters(mapper.map())

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

    One price per symbol per tick -- the latest print -- so every symbol's
    series is sampled on the same clock and a correlation is between
    simultaneous observations, not between one symbol's every print and
    another's.
    """
    from runtime.input_assembly import Batch

    # A frame already carries the latest price per symbol, so a keyed level shape
    # on top of it would be keeping the latest of the latest. Read as a batch and
    # flattened to its levels.
    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    publish_clusters = context.bus.publisher_for("correlation-cluster")
    mapper = CorrelationClusterMapper(
        window_length=int(context.number("correlation_window_length")),
        minimum_shared_observations=int(context.number("correlation_minimum_shared_observations")),
        cluster_threshold=context.number("correlation_cluster_threshold"),
            maximum_gap_seconds=context.number("price_series_maximum_gap_seconds"),
    )

    def read_prices(_mapper) -> None:
        latest_by_symbol: dict[str, tuple[float, int]] = {}
        for level in levels_in(trades.payloads()):
            latest_by_symbol[level.symbol] = (level.price, level.observed_at_ns)
        for symbol, (price, at_ns) in latest_by_symbol.items():
            mapper.observe_price(symbol, price, at_ns)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_clusters(kept)

    return run_correlation_cluster_mapper(
        mapper=mapper,
        control_socket=context.control_socket,
        read_prices=read_prices,
        publish_clusters=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
