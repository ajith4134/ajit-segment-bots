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
- **Every pair is measured, but not every pair on every pass.** Pairs grow with
  the square of the universe and the answer does not. Measured on this box
  2026-09-05: one pair costs 128 microseconds, or 76 with each symbol's returns
  computed once per pass instead of once per pair. At the 2,444-share cash-equity
  universe that is 2,985,346 pairs and 228 seconds of CPU for one pass, against a
  15-second remap interval -- fifteen cores, continuously, for a statistic whose
  own window is an hour long.

  So a pass measures a **budget** of pairs and resumes where the last one
  stopped, and the budget is derived rather than chosen: enough that one full
  sweep of every pair completes inside one correlation window. The statistic
  cannot move faster than its own window, so a sweep that finishes inside one is
  not a slower answer -- it is the same answer, computed once instead of 240
  times.

  What that costs in honesty is stated on every cluster and counted on the
  standing: `all_pairs_measured` was already there for pairs with too little
  history, and a pair not yet reached in this sweep is counted apart from one
  that cannot be measured at all. The two are different facts and only one of
  them is about the market.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.price_frames import levels_in
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.pair_sweep import pairs_after
from runtime.rolling_statistics import RollingWindow, correlation

PART_ID = "correlation-cluster-mapper"

# This part's symbols are all one book's, so the sweep has one group. The shared
# walk takes groups because `cointegration-pair-finder` sweeps per venue: two
# symbols on different venues are not a pair, because their prices did not
# arrive from the same book.
ONE_GROUP = ""

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
    # Symbols dropped because their series ended and nothing is arriving about
    # them. Counted rather than dropped quietly: pairs grow with the square of
    # this part's universe, so a number that climbs steadily is a feed losing
    # symbols and one that jumps once at start is a universe that outlived its
    # venue.
    symbols_forgotten_silent: int = 0
    # The pair budget (2026-09-05). `pairs_total` is every pair the universe
    # implies; `pairs_measured_this_pass` is what this pass could afford;
    # `pairs_not_reached_this_pass` is the rest -- counted apart from
    # `unmeasured_pairs`, which is pairs that cannot be measured at all for want
    # of shared history. One is about the machine, the other about the market.
    pairs_total: int = 0
    pairs_measured_this_pass: int = 0
    pairs_not_reached_this_pass: int = 0
    pairs_measured_this_sweep: int = 0
    passes_per_sweep: int = 1
    sweeps_completed: int = 0
    strong_pairs_remembered: int = 0
    strong_pairs_evicted: int = 0


class CorrelationClusterMapper:
    """Measures what moves together, on returns, over a window that forgets."""

    def __init__(
        self,
        window_length: int,
        minimum_shared_observations: int,
        cluster_threshold: float,
        maximum_gap_seconds: float | None = None,
        gap_patience_multiple: float | None = None,
        now_ns=time.time_ns,
        maximum_pairs_per_pass: int | None = None,
        passes_per_sweep_ceiling: int | None = None,
        remembered_pairs_maximum: int | None = None,
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
        self._gap_patience_multiple = gap_patience_multiple
        # How many pair correlations one pass may compute. None means every pair,
        # every pass, which is what a universe small enough for that states -- and
        # what this part did until 2026-09-05.
        self._maximum_pairs_per_pass = maximum_pairs_per_pass
        # The most passes a full sweep of every pair may take. The budget is
        # raised above `maximum_pairs_per_pass` for nothing; this is the other
        # end, and it is what ties the budget to the statistic rather than to a
        # CPU wish: a sweep that finishes inside one correlation window measures
        # every pair at least once per window turnover.
        self._passes_per_sweep_ceiling = passes_per_sweep_ceiling
        # How many measured pairs may be carried between passes. Clusters are
        # built from the whole sweep, not from one pass's slice, so the pairs have
        # to be remembered -- and remembering all of them at 2,444 symbols is
        # three million entries. Only pairs at or above the cluster threshold are
        # kept, because those are the only ones union-find acts on.
        self._remembered_pairs_maximum = remembered_pairs_maximum
        # Where the next pass resumes, as the pair itself rather than an index:
        # the symbol list changes between passes and an index into it would point
        # somewhere else.
        self._resume_after: tuple[str, str] | None = None
        # How many pairs this sweep has measured so far. A sweep is not one pass:
        # it is however many passes it takes to reach every pair once, and it is
        # the unit the budget is derived from.
        self._measured_this_sweep = 0
        # Pairs measured at or above the threshold, carried across passes.
        self._strong_pairs: dict[tuple[str, str], float] = {}
        self._prices: dict[str, RollingWindow] = {}
        # When this part last received anything about a symbol, on its own clock
        # rather than the venue's -- see `runtime.rolling_statistics.subjects_gone_quiet`
        # for why both are needed.
        self._last_seen_at_ns: dict[str, int] = {}
        self.standing = MapperStanding()

    def observe_price(self, symbol: str, price: float, at_ns: int) -> None:
        self.standing.observations += 1
        self._last_seen_at_ns[symbol] = self._now_ns()
        window = self._prices.get(symbol)
        if window is None:
            window = RollingWindow(
                length=self._window,
                maximum_gap_seconds=self._maximum_gap_seconds,
                gap_patience_multiple=self._gap_patience_multiple,
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

    def forget_silent_symbols(self, now_ns: int | None = None) -> tuple:
        """Drop every symbol whose series is over and about which nothing arrives.

        Pairs grow with the square of the universe, so this is the only lever on
        this part's cost that is not a slower answer. Measured on the live spine
        2026-09-04: 467 symbols, 108,811 pairs, `unmeasured_pairs` 108,811 -- every
        pair of them -- for 0.408 of a core, because the universe was still the
        retired crypto one and no pair had enough shared observations to measure.

        The rule is `runtime.rolling_statistics.subjects_gone_quiet`, shared with
        `regime-classifier`, which needs the same answer about the same kind of
        window.
        """
        from runtime.rolling_statistics import subjects_gone_quiet

        at = self._now_ns() if now_ns is None else now_ns
        gone = subjects_gone_quiet(self._prices, self._last_seen_at_ns, at)
        for symbol in gone:
            del self._prices[symbol]
            self._last_seen_at_ns.pop(symbol, None)
        self.standing.symbols_forgotten_silent += len(gone)
        self.standing.symbols_tracked = len(self._prices)
        return gone

    def budget_for(self, pair_count: int) -> int | None:
        """How many pairs this pass may measure, or None for all of them.

        Derived, not chosen: enough that a full sweep finishes inside the ceiling
        the caller states, and never above the caller's own per-pass maximum. A
        caller stating neither gets every pair, which is what a universe small
        enough for that means.
        """
        budget = None
        if self._passes_per_sweep_ceiling:
            budget = -(-pair_count // self._passes_per_sweep_ceiling)
        if self._maximum_pairs_per_pass is not None:
            budget = (
                self._maximum_pairs_per_pass
                if budget is None
                else min(budget, self._maximum_pairs_per_pass)
            )
        return budget

    def _pairs_from_the_cursor(self, symbols: list):
        """Every pair, starting after the one the last pass stopped on.

        Lazily, and never as a list: the whole point of the budget is that the
        pair count is enormous, and building 2,985,346 tuples to look at 12,439
        of them costs more than the correlations it saves (measured 2026-09-05:
        252 MB and a quarter of a second). `runtime.pair_sweep` is the shared
        walk, because `cointegration-pair-finder` sweeps the same pairs for a
        different measurement and two copies of a cursor this subtle is how one
        of them ends up skipping a pair forever.
        """
        resume = None
        if self._resume_after is not None:
            resume = (ONE_GROUP, *self._resume_after)
        for _, left, right in pairs_after({ONE_GROUP: symbols}, resume):
            yield left, right

    def _remember(self, pair: tuple[str, str], value: float) -> None:
        """Keep a cluster-forming pair, evicting the weakest when full."""
        self._strong_pairs[pair] = value
        if (
            self._remembered_pairs_maximum is not None
            and len(self._strong_pairs) > self._remembered_pairs_maximum
        ):
            weakest = min(self._strong_pairs, key=lambda key: abs(self._strong_pairs[key]))
            del self._strong_pairs[weakest]
            self.standing.strong_pairs_evicted += 1

    def map(self) -> tuple[CorrelationCluster, ...]:
        self.standing.mappings += 1
        self.forget_silent_symbols()
        symbols = sorted(self._prices)
        if len(symbols) < 2:
            return ()

        parent = {symbol: symbol for symbol in symbols}
        unmeasured = 0

        def find(symbol):
            while parent[symbol] != symbol:
                parent[symbol] = parent[parent[symbol]]
                symbol = parent[symbol]
            return symbol

        pair_count = len(symbols) * (len(symbols) - 1) // 2
        budget = self.budget_for(pair_count)
        self.standing.pairs_total = pair_count
        self.standing.passes_per_sweep = (
            1 if budget is None else max(1, -(-pair_count // budget))
        )

        # Each symbol's returns once per pass rather than once per pair. Measured
        # 2026-09-05: 128 microseconds a pair recomputed, 76 with this -- the
        # returns are O(window) and were being rebuilt n-1 times per symbol.
        living = {symbol: self._prices[symbol].returns() for symbol in symbols}

        measured = 0
        last_pair = None
        for left, right in self._pairs_from_the_cursor(symbols):
            if budget is not None and measured >= budget:
                break
            last_pair = (left, right)
            left_returns = living[left]
            right_returns = living[right]
            length = min(len(left_returns), len(right_returns))
            if length < self._minimum:
                # Unmeasured, never zero: treating it as uncorrelated is how a
                # book concentrates in the symbols nobody has data on.
                unmeasured += 1
                measured += 1
                self._strong_pairs.pop((left, right), None)
                continue
            value = correlation(left_returns[-length:], right_returns[-length:])
            measured += 1
            if abs(value) >= self._threshold:
                self._remember((left, right), value)
                if (
                    self.standing.strongest_correlation_seen is None
                    or abs(value) > self.standing.strongest_correlation_seen
                ):
                    self.standing.strongest_correlation_seen = abs(value)
            else:
                # It was strong and is not any more. Forgetting it here is what
                # stops a cluster outliving the correlation that formed it.
                self._strong_pairs.pop((left, right), None)

        self._measured_this_sweep += measured
        if budget is None or self._measured_this_sweep >= pair_count:
            # Every pair has been reached once. The next pass starts a new sweep
            # from the top, so a correlation is never older than one sweep.
            self.standing.sweeps_completed += 1
            self._measured_this_sweep = 0
            self._resume_after = None
        else:
            self._resume_after = last_pair
        self.standing.pairs_measured_this_pass = measured
        self.standing.pairs_measured_this_sweep = self._measured_this_sweep
        self.standing.pairs_not_reached_this_pass = max(0, pair_count - measured)
        self.standing.strong_pairs_remembered = len(self._strong_pairs)

        # Clusters are formed from the whole sweep, not from this pass's slice: a
        # pair measured three passes ago is still the best answer there is about
        # it, and rebuilding union-find from one slice would break a cluster apart
        # every pass and put it back together the next.
        pair_correlations = {
            pair: value
            for pair, value in self._strong_pairs.items()
            if pair[0] in parent and pair[1] in parent
        }
        for (left, right) in pair_correlations:
            parent[find(left)] = find(right)

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
        "symbols_forgotten_silent": mapper.standing.symbols_forgotten_silent,
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
    mapping_is_due=lambda: True,
) -> int:
    """Read what moved, and remap when a remap is due.

    Which symbols move together is a level, and it changes on the timescale
    correlations change on -- not on the timescale prices arrive on. Measured on
    the live spine at 10:26 on 2026-08-26: 160 messages a second built from 3
    price frames a second, remapping and restating the same clusters.

    The mapping is paced, not just the publish, and that is the whole point here.
    `map()` correlates every pair: at 67 symbols that is 2,211 correlations over
    256-long windows, and at roughly eighteen ticks a second it cost 76% of a core
    on 2026-08-26 -- the second busiest process on the machine. Skipping only the
    send would have paid all of that and thrown the answer away.

    Observing prices stays on every tick. A window that missed the prices between
    two remaps would correlate a series with holes in it, which is a different
    series, not a cheaper one.
    """

    def tick() -> None:
        read_prices(mapper)
        if not mapping_is_due():
            return
        publish_clusters(mapper.map())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_correlation_clusters(mapper),
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
    from runtime.level_publishing import (
        LevelPublisher,
        PacedPublisher,
        without_observation_time,
    )

    # Paces the mapping, not just the send: correlating every pair is the
    # expensive half, and a publisher that only refused to send it would pay all
    # of that and discard the answer. Same shape as heartbeat-collector's.
    # Its own interval, not the shared one, because the cost here is quadratic in
    # the universe and the answer is not. `correlation_window_length` is 256
    # observations -- about an hour -- so remapping once a second recomputed an
    # hour-long statistic three and a half thousand times per window turnover.
    # Measured 2026-09-04: 548 symbols, 149,878 pairs, every one of them
    # unmeasured, for 0.423 of a core.
    remap_interval = context.number("correlation_remap_interval_seconds")
    remaps = PacedPublisher(
        publish=lambda _items: None,
        interval_seconds=remap_interval,
    )

    cluster_levels = LevelPublisher(
        publish=context.bus.publisher_for("correlation-cluster"),
        refresh_interval_seconds=remap_interval,
        identity_of=without_observation_time,
    )
    mapper = CorrelationClusterMapper(
        window_length=int(context.number("correlation_window_length")),
        minimum_shared_observations=int(context.number("correlation_minimum_shared_observations")),
        cluster_threshold=context.number("correlation_cluster_threshold"),
        maximum_gap_seconds=context.number("price_series_maximum_gap_seconds"),
        gap_patience_multiple=context.number("price_gap_patience_multiple"),
        # The pair budget (2026-09-05). The ceiling on passes is what ties it to
        # the statistic: a sweep of every pair finishes inside one correlation
        # window, so no correlation is ever older than the window it describes.
        # The per-pass maximum is the machine's side of the same bargain.
        passes_per_sweep_ceiling=int(
            context.number("correlation_passes_per_sweep_ceiling")
        ),
        maximum_pairs_per_pass=int(
            context.number("correlation_maximum_pairs_per_pass")
        ),
        remembered_pairs_maximum=int(
            context.number("correlation_remembered_pairs_maximum")
        ),
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
            cluster_levels.publish_level(kept)

    def mapping_is_due() -> bool:
        """Whether a remap is due, and record it as taken if so.

        The reading and the recording are one call because a caller that asked
        and then did not remap would push the next remap out by a full interval
        for nothing.
        """
        if not remaps.is_due():
            return False
        remaps.publish_snapshot(())
        return True

    return run_correlation_cluster_mapper(
        mapper=mapper,
        control_socket=context.control_socket,
        read_prices=read_prices,
        publish_clusters=publish,
        mapping_is_due=mapping_is_due,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
