"""cross-segment-exposure-watch: what the whole book is actually exposed to.

Every segment sizes its own positions correctly and the book can still be one
bet. Three segments long three different symbols that all move together is not a
diversified book -- it is a leveraged position in whatever they have in common,
and no part inside a segment can see it, because each sees only its own.

This is the part that sees all three at once, and it measures three things a
per-segment view cannot:

- **Net and gross exposure.** Net says what the book is directionally worth;
  gross says how much is at risk to a volatility event that moves everything
  together. A book that is net flat and grossly enormous is the classic shape of
  a blow-up, and the two numbers together are what shows it.
- **Concentration by underlying, not by symbol.** BTCUSDT on one venue and
  BTCUSDC on another are the same exposure wearing two names, and a limit
  measured per symbol would let a position be doubled by spelling it differently.
- **Correlated exposure.** Positions in different symbols that move together are
  a single position, and this part reports the group's combined size rather than
  each member's.

**It advises and never blocks.** Every trade decision belongs to a segment
(RL-048); this part produces the view the risk gate acts on. A watcher that could
refuse a trade directly would be a second risk gate with none of the first one's
record.

**A correlation nobody has measured is not a correlation of zero.** Unmeasured
pairs are named, because the group most likely to move together is the one nobody
has enough history on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.underlying_of_a_trading_symbol import underlying_of_a_trading_symbol

PART_ID = "cross-segment-exposure-watch"

PART_DECLARATION = PartDeclaration(
    part_id="cross-segment-exposure-watch",
    consumes=("position",),
    produces=("exposure-view", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

LONG = "long"
SHORT = "short"


@dataclass(frozen=True)
class ExposureGroup:
    """Positions that are really one position, and what they add up to."""

    name: str
    members: tuple
    net_notional: float
    gross_notional: float
    segments: tuple
    correlation_is_measured: bool
    reason: str

    @property
    def is_one_sided(self) -> bool:
        return abs(self.net_notional) > 0.9 * self.gross_notional


@dataclass(frozen=True)
class ExposureView:
    """What the whole book is exposed to, across every segment at once."""

    net_notional: float
    gross_notional: float
    by_segment: dict
    by_underlying: dict
    groups: tuple
    largest_group: ExposureGroup | None
    unmeasured_pairs: tuple
    positions_counted: int
    reason: str
    viewed_at_ns: int

    @property
    def net_to_gross(self) -> float | None:
        """Near zero is a hedged book; near one is a directional one."""
        if self.gross_notional <= 0:
            return None
        return abs(self.net_notional) / self.gross_notional

    def concentration_of(self, underlying: str) -> float | None:
        if self.gross_notional <= 0:
            return None
        return self.by_underlying.get(underlying, 0.0) / self.gross_notional


@dataclass
class WatchStanding:
    views: int = 0
    positions_seen: int = 0
    groups_formed: int = 0
    unmeasured_pairs_reported: int = 0
    largest_concentration_seen: float | None = None
    highest_net_to_gross_seen: float | None = None


class CrossSegmentExposureWatch:
    """Sees every segment's positions at once and reports what they really are."""

    def __init__(
        self,
        correlation_threshold: float,
        minimum_correlation_observations: int,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < correlation_threshold <= 1.0:
            raise ValueError(
                "the threshold is a correlation and must be inside (0, 1]; at zero every "
                "position joins every group"
            )
        self._threshold = correlation_threshold
        self._minimum = minimum_correlation_observations
        self._now_ns = now_ns
        self._positions: dict[tuple[str, str, str], object] = {}
        self._correlations: dict[tuple[str, str], tuple] = {}
        self._underlying_of: dict[str, str] = {}
        self.standing = WatchStanding()

    def observe_position(self, segment: str, position) -> None:
        """One segment's position. Replacing rather than accumulating: it is a state."""
        key = (segment, position.venue_id, position.symbol)
        if position.quantity == 0:
            self._positions.pop(key, None)
        else:
            self._positions[key] = position
        self.standing.positions_seen = len(self._positions)

    def observe_correlation(self, left: str, right: str, correlation: float, observations: int) -> None:
        self._correlations[tuple(sorted((left, right)))] = (correlation, observations)

    def set_underlying(self, symbol: str, underlying: str) -> None:
        """BTCUSDT and BTCUSDC are the same exposure wearing two names."""
        self._underlying_of[symbol] = underlying

    def underlying_of(self, symbol: str) -> str:
        return self._underlying_of.get(symbol, symbol)

    def correlation_between(self, left: str, right: str) -> tuple[float | None, bool]:
        record = self._correlations.get(tuple(sorted((left, right))))
        if record is None:
            return None, False
        correlation, observations = record
        return correlation, observations >= self._minimum

    def view(self) -> ExposureView:
        self.standing.views += 1
        positions = list(self._positions.items())

        net = 0.0
        gross = 0.0
        by_segment: dict[str, float] = {}
        by_underlying: dict[str, float] = {}

        for (segment, _, symbol), position in positions:
            # **At the price each position was opened at, because `position` is
            # this part's only input and a `Position` carries no mark.** Reading
            # `mark_price` off one crash-looped this part every tick a position
            # was open -- it is a field the type has never had, and the exposure
            # it was meant to measure was never measured once.
            #
            # Cost-basis notional is the honest measure available here, and it is
            # the right one for what this part asks: concentration and net-to-gross
            # are ratios between positions, and marking them all to a price this
            # part cannot see would not change which underlying dominates. Where a
            # mark is wanted, it arrives as a declared input (R-01), not a getattr.
            notional = position.quantity * position.average_entry_price
            net += notional
            gross += abs(notional)
            by_segment[segment] = by_segment.get(segment, 0.0) + notional
            underlying = self.underlying_of(symbol)
            by_underlying[underlying] = by_underlying.get(underlying, 0.0) + abs(notional)

        groups, unmeasured = self._group_correlated(positions)
        self.standing.groups_formed += len(groups)
        self.standing.unmeasured_pairs_reported += len(unmeasured)

        largest = max(groups, key=lambda group: group.gross_notional, default=None)
        if gross > 0:
            concentration = max(by_underlying.values()) / gross
            if (
                self.standing.largest_concentration_seen is None
                or concentration > self.standing.largest_concentration_seen
            ):
                self.standing.largest_concentration_seen = concentration
            ratio = abs(net) / gross
            if (
                self.standing.highest_net_to_gross_seen is None
                or ratio > self.standing.highest_net_to_gross_seen
            ):
                self.standing.highest_net_to_gross_seen = ratio

        return ExposureView(
            net_notional=net,
            gross_notional=gross,
            by_segment=by_segment,
            by_underlying=by_underlying,
            groups=tuple(groups),
            largest_group=largest,
            unmeasured_pairs=tuple(unmeasured),
            positions_counted=len(positions),
            reason=(
                f"{len(positions)} position(s) across {len(by_segment)} segment(s): net "
                f"{net:,.0f} against gross {gross:,.0f}, measured at what each position "
                f"was opened at -- no mark reaches this part"
                + (
                    f" ({abs(net) / gross:.0%} directional -- a book that is net flat and "
                    f"grossly enormous is the shape of a blow-up, and the two numbers "
                    f"together are what show it)"
                    if gross > 0
                    else ""
                )
                + (
                    f"; the largest correlated group holds {largest.gross_notional:,.0f} "
                    f"across {len(largest.members)} position(s) in "
                    f"{len(largest.segments)} segment(s)"
                    if largest is not None
                    else ""
                )
                + (
                    f"; {len(unmeasured)} pair(s) have no measured correlation, and the group "
                    f"most likely to move together is the one nobody has history on"
                    if unmeasured
                    else ""
                )
            ),
            viewed_at_ns=self._now_ns(),
        )

    def _group_correlated(self, positions) -> tuple[list, list]:
        """Positions that move together are one position, however many symbols they use."""
        symbols = sorted({symbol for (_, _, symbol), _ in positions})
        parent = {symbol: symbol for symbol in symbols}
        unmeasured = []

        def find(symbol):
            while parent[symbol] != symbol:
                parent[symbol] = parent[parent[symbol]]
                symbol = parent[symbol]
            return symbol

        for index, left in enumerate(symbols):
            for right in symbols[index + 1 :]:
                if self.underlying_of(left) == self.underlying_of(right):
                    parent[find(left)] = find(right)
                    continue
                correlation, measured = self.correlation_between(left, right)
                if correlation is None or not measured:
                    unmeasured.append(f"{left}/{right}")
                    continue
                if abs(correlation) >= self._threshold:
                    parent[find(left)] = find(right)

        grouped: dict[str, list] = {}
        for (segment, venue_id, symbol), position in positions:
            grouped.setdefault(find(symbol), []).append((segment, venue_id, symbol, position))

        groups = []
        for root, members in grouped.items():
            # The same basis as `view`: what each position was opened at, which
            # is the only price a part consuming `position` alone can have.
            net = sum(
                position.quantity * position.average_entry_price for _, _, _, position in members
            )
            gross = sum(
                abs(position.quantity * position.average_entry_price)
                for _, _, _, position in members
            )
            segments = tuple(sorted({segment for segment, _, _, _ in members}))
            measured = all(
                self.correlation_between(left[2], right[2])[1]
                for index, left in enumerate(members)
                for right in members[index + 1 :]
                if self.underlying_of(left[2]) != self.underlying_of(right[2])
            )
            groups.append(
                ExposureGroup(
                    name=root,
                    members=tuple(f"{segment}:{venue}:{symbol}" for segment, venue, symbol, _ in members),
                    net_notional=net,
                    gross_notional=gross,
                    segments=segments,
                    correlation_is_measured=measured,
                    reason=(
                        f"{len(members)} position(s) across {len(segments)} segment(s) that "
                        f"move together: net {net:,.0f}, gross {gross:,.0f}"
                        + ("" if measured else "; not every pair's correlation is measured")
                    ),
                )
            )
        return groups, sorted(set(unmeasured))


def describe_exposure(watch: CrossSegmentExposureWatch) -> dict:
    view = watch.view()
    return {
        "part_id": PART_ID,
        "views": watch.standing.views,
        "positions_held": watch.standing.positions_seen,
        "net_notional": view.net_notional,
        "gross_notional": view.gross_notional,
        "net_to_gross": view.net_to_gross,
        "by_segment": dict(sorted(view.by_segment.items())),
        "by_underlying": dict(sorted(view.by_underlying.items())),
        "correlated_groups": len(view.groups),
        "unmeasured_pairs": list(view.unmeasured_pairs),
        "largest_concentration_seen": watch.standing.largest_concentration_seen,
        "highest_net_to_gross_seen": watch.standing.highest_net_to_gross_seen,
        "blocks_trades": False,
    }


def run_cross_segment_exposure_watch(
    watch: CrossSegmentExposureWatch, control_socket, read_positions, publish_view,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_positions(watch)
        publish_view(watch.view())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_exposure(watch),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    **A position's segment is the position's own, not `segment_id`'s.** Until
    2026-09-07 every position was stamped with the segment the settings name,
    which on a spine trading three segments made a cross-segment watch that could
    only ever see one: measured on the live spine that day, two stock-options
    positions (HINDUNILVR 1980 PE, KOTAKBANK 425 PE) were both filed under
    `by_segment.index-options`, so the one number this part exists to produce --
    what the whole book is exposed to across segments -- was a single segment's
    exposure wearing three segments' name. `Position.segment` has carried the
    answer since 2026-09-05; `segment_id` stays only as the fallback for a
    position restored from a checkpoint that predates it.

    A symbol's underlying is the share or index the contract is a claim on: the
    first token of the venue's trading symbol, so "HINDUNILVR 1980 PE 29 SEP 26"
    groups with "HINDUNILVR 2000 CE 29 SEP 26" and with the share itself. Was the
    symbol with the settlement currency taken off its end, which is BTCUSDT's
    rule: on an NSE options book it stripped nothing, so every strike of every
    expiry was its own underlying and two positions on one share read as two
    unrelated bets -- exactly the concentration this part is here to see.

    No correlation reaches this part -- `position` is its only input -- so
    every cross-underlying pair is reported as unmeasured, which is the true
    state, not a guess at one.
    """
    from runtime.input_assembly import Batch

    positions = Batch(read=context.bus.reader("position"))
    publish_view = context.bus.publisher_for("exposure-view")
    segment = str(context.setting("segment_id").value)
    settlement = str(context.setting("settlement_currency").value)
    watch = CrossSegmentExposureWatch(
        correlation_threshold=context.number("correlation_cluster_threshold"),
        minimum_correlation_observations=int(context.number("correlation_minimum_shared_observations")),
    )

    def underlying_of(symbol: str) -> str:
        # This part had the only correct copy of the rule; it now lives in
        # runtime/ so the other three cannot drift from it again (2026-09-12).
        return underlying_of_a_trading_symbol(symbol, settlement)

    def read_positions(_watch) -> None:
        for position in positions.payloads():
            watch.set_underlying(position.symbol, underlying_of(position.symbol))
            # The position's own segment, falling back to this spine's only where
            # the position names none -- a checkpoint written before positions
            # carried one.
            watch.observe_position(getattr(position, "segment", "") or segment, position)

    def publish(view) -> None:
        if view is not None:
            publish_view((view,))

    return run_cross_segment_exposure_watch(
        watch=watch,
        control_socket=context.control_socket,
        read_positions=read_positions,
        publish_view=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
