"""liquidation-cluster-mapper: where forced selling waits, estimated from open interest.

Liquidation levels are not published. What is published is open interest, the mark
price, and the venue's maintenance margin schedule -- and those three are enough to
say where positions opened at various leverages would be liquidated, and how much
notional sits at each level.

The estimate is genuinely an estimate and this part says so at every step:

- **Positions are attributed to leverage bands, not to individual traders.** Who
  is at 20x is unknowable; what fraction of open interest historically sits in
  each band is measurable from how much liquidation actually occurred at each
  distance, and that is what this part learns.
- **The entry price of the open interest is unknown**, so clusters are computed
  from the price *distribution* the position was likely opened across -- the
  traded volume profile -- rather than from a single assumed entry.
- **Clusters are reported with the confidence the evidence supports.** A cluster
  inferred from a thin volume profile in a symbol with no liquidation history is
  a guess, and the parts that trade cascades need to know which kind they have.

**Nothing here is a signal by itself.** A liquidation cluster is a place where
supply or demand may appear suddenly; whether that is worth trading is the
cascade detector's judgement, and this part deliberately produces no direction.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "liquidation-cluster-mapper"

PART_DECLARATION = PartDeclaration(
    part_id="liquidation-cluster-mapper",
    consumes=("market-data", "order-book-snapshot"),
    produces=("liquidation-map", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

MAPPED = "mapped"
NO_OPEN_INTEREST = "no-open-interest-reported-for-this-symbol"
NO_MARGIN_SCHEDULE = "this-venue's-maintenance-margin-schedule-is-not-known"
NO_VOLUME_PROFILE = "no-traded-volume-profile-to-attribute-positions-across"

LONG = "long"
SHORT = "short"


@dataclass(frozen=True)
class MarginTier:
    """One tier of a venue's maintenance margin schedule, as published."""

    notional_floor: float
    maintenance_margin_rate: float
    maximum_leverage: float


@dataclass(frozen=True)
class LiquidationCluster:
    """Notional that would be force-closed near one price, and how sure that is."""

    venue_id: str
    symbol: str
    price: float
    notional: float
    side: str
    leverage_band: float
    distance_fraction: float
    confidence: Estimate

    @property
    def is_measured(self) -> bool:
        return self.confidence.is_fitted


@dataclass(frozen=True)
class LiquidationMap:
    """Every estimated cluster for one symbol, above and below the mark."""

    venue_id: str
    symbol: str
    state: str
    mark_price: float
    clusters: tuple
    open_interest_notional: float
    largest_cluster: LiquidationCluster | None
    reason: str
    mapped_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == MAPPED

    def within(self, fraction: float, side: str) -> tuple:
        return tuple(
            cluster
            for cluster in self.clusters
            if cluster.side == side and cluster.distance_fraction <= fraction
        )


@dataclass
class MapperStanding:
    maps_made: int = 0
    maps_published: int = 0
    refused_no_open_interest: int = 0
    refused_no_schedule: int = 0
    refused_no_profile: int = 0
    clusters_produced: int = 0
    liquidations_observed: int = 0
    by_leverage_band: dict = field(default_factory=dict)


class LiquidationClusterMapper:
    """Estimates where forced closes sit, and learns the leverage mix from what happens."""

    def __init__(
        self,
        leverage_bands: tuple,
        prior_band_share: float,
        prior_weight: float,
        half_life_observations: float,
        minimum_observations: int,
        volume_profile_buckets: int,
        now_ns=time.time_ns,
    ) -> None:
        if not leverage_bands:
            raise ValueError("a mapper with no leverage bands has nowhere to place a position")
        if any(band <= 1.0 for band in leverage_bands):
            raise ValueError("a position at or below 1x leverage is not liquidatable")
        self._bands = tuple(sorted(leverage_bands))
        self._minimum = minimum_observations
        self._buckets = volume_profile_buckets
        self._now_ns = now_ns
        self._band_share = {
            band: RateEstimator(
                prior=prior_band_share, prior_weight=prior_weight,
                half_life_observations=half_life_observations,
            )
            for band in self._bands
        }
        self._schedules: dict[str, tuple] = {}
        self._open_interest: dict[tuple[str, str], float] = {}
        self._volume_profile: dict[tuple[str, str], dict] = {}
        self.standing = MapperStanding()

    def observe_margin_schedule(self, venue_id: str, tiers) -> None:
        """The venue's published maintenance margin tiers. Never assumed."""
        self._schedules[venue_id] = tuple(sorted(tiers, key=lambda tier: tier.notional_floor))

    def observe_open_interest(self, venue_id: str, symbol: str, notional: float) -> None:
        self._open_interest[(venue_id, symbol)] = notional

    def observe_traded_volume(self, venue_id: str, symbol: str, price: float, quote_volume: float) -> None:
        """Where volume actually traded, which is where positions were likely opened.

        A single assumed entry price would put every cluster in one place; the
        volume profile is the closest observable thing to where the open interest
        was actually built.
        """
        profile = self._volume_profile.setdefault((venue_id, symbol), {})
        profile[price] = profile.get(price, 0.0) + quote_volume

    def observe_liquidation(self, venue_id: str, symbol: str, price: float, mark_at_open: float) -> None:
        """A liquidation that actually happened, which teaches the leverage mix.

        Which band it belonged to is inferred from how far price had to move,
        because that distance is a function of the leverage and the maintenance
        margin and nothing else.
        """
        self.standing.liquidations_observed += 1
        if mark_at_open <= 0:
            return
        distance = abs(price - mark_at_open) / mark_at_open
        band = self._band_from_distance(venue_id, distance)
        for candidate in self._bands:
            self._band_share[candidate].observe(candidate == band)
        self.standing.by_leverage_band[band] = self.standing.by_leverage_band.get(band, 0) + 1

    def liquidation_price(self, venue_id: str, entry: float, leverage: float, side: str) -> float | None:
        """Where a position at this leverage is force-closed, from the venue's schedule.

        The formula rather than a rule of thumb: at 20x the liquidation is not at
        5% away, it is at 5% minus the maintenance margin rate, and the
        difference is exactly the region a cascade trades through.
        """
        tiers = self._schedules.get(venue_id)
        if not tiers or leverage <= 1.0 or entry <= 0:
            return None
        rate = self._maintenance_rate(tiers, entry, leverage)
        move = (1.0 / leverage) - rate
        if move <= 0:
            return None
        return entry * (1.0 - move) if side == LONG else entry * (1.0 + move)

    def map(self, venue_id: str, symbol: str, mark_price: float) -> LiquidationMap:
        self.standing.maps_made += 1
        key = (venue_id, symbol)

        if venue_id not in self._schedules:
            self.standing.refused_no_schedule += 1
            return self._map(
                venue_id, symbol, NO_MARGIN_SCHEDULE, mark_price, (), 0.0,
                f"{venue_id}'s maintenance margin schedule is not known here, and without it "
                f"a liquidation price is a rule of thumb rather than a calculation",
            )

        open_interest = self._open_interest.get(key)
        if open_interest is None or open_interest <= 0:
            self.standing.refused_no_open_interest += 1
            return self._map(
                venue_id, symbol, NO_OPEN_INTEREST, mark_price, (), 0.0,
                "no open interest is reported for this symbol, so there is no notional to place",
            )

        profile = self._volume_profile.get(key)
        if not profile:
            self.standing.refused_no_profile += 1
            return self._map(
                venue_id, symbol, NO_VOLUME_PROFILE, mark_price, (), open_interest,
                "no traded volume profile, so there is nothing to attribute the open interest "
                "across; a single assumed entry price would put every cluster in one place",
            )

        clusters = []
        total_volume = sum(profile.values())
        for band in self._bands:
            share = self._band_share[band].estimate(self._minimum)
            band_notional = open_interest * share.value
            for entry_price, volume in profile.items():
                weight = volume / total_volume
                for side in (LONG, SHORT):
                    price = self.liquidation_price(venue_id, entry_price, band, side)
                    if price is None or price <= 0:
                        continue
                    distance = abs(price - mark_price) / mark_price if mark_price else 0.0
                    clusters.append(
                        LiquidationCluster(
                            venue_id=venue_id,
                            symbol=symbol,
                            price=price,
                            notional=band_notional * weight / 2,
                            side=side,
                            leverage_band=band,
                            distance_fraction=distance,
                            confidence=share,
                        )
                    )

        clusters = self._merge_nearby(clusters, mark_price)
        self.standing.clusters_produced += len(clusters)
        self.standing.maps_published += 1
        largest = max(clusters, key=lambda cluster: cluster.notional, default=None)

        return self._map(
            venue_id, symbol, MAPPED, mark_price, tuple(clusters), open_interest,
            f"{len(clusters)} cluster(s) from {open_interest:,.0f} of open interest across "
            f"{len(self._bands)} leverage band(s) and {len(profile)} traded price level(s)"
            + (
                f"; the largest holds {largest.notional:,.0f} at {largest.price:.8g}, "
                f"{largest.distance_fraction:.2%} away"
                if largest is not None
                else ""
            )
            + f". The band mix is learned from {self.standing.liquidations_observed} observed "
            f"liquidation(s), so these are estimates and each carries its own confidence",
        )

    def _merge_nearby(self, clusters, mark_price: float) -> list:
        """Bucket clusters by price, because a cascade does not respect the grid."""
        if not clusters or mark_price <= 0:
            return clusters
        width = mark_price / self._buckets
        merged: dict[tuple[int, str], list] = {}
        for cluster in clusters:
            merged.setdefault((int(cluster.price / width), cluster.side), []).append(cluster)

        result = []
        for group in merged.values():
            notional = sum(cluster.notional for cluster in group)
            weighted_price = (
                sum(cluster.price * cluster.notional for cluster in group) / notional
                if notional
                else group[0].price
            )
            representative = max(group, key=lambda cluster: cluster.notional)
            result.append(
                LiquidationCluster(
                    venue_id=representative.venue_id,
                    symbol=representative.symbol,
                    price=weighted_price,
                    notional=notional,
                    side=representative.side,
                    leverage_band=representative.leverage_band,
                    distance_fraction=abs(weighted_price - mark_price) / mark_price,
                    confidence=representative.confidence,
                )
            )
        return sorted(result, key=lambda cluster: cluster.distance_fraction)

    def _maintenance_rate(self, tiers, entry: float, leverage: float) -> float:
        notional = entry * leverage
        rate = tiers[0].maintenance_margin_rate
        for tier in tiers:
            if notional >= tier.notional_floor:
                rate = tier.maintenance_margin_rate
        return rate

    def _band_from_distance(self, venue_id: str, distance: float) -> float:
        """Which leverage band a liquidation at this distance implies."""
        tiers = self._schedules.get(venue_id)
        rate = tiers[0].maintenance_margin_rate if tiers else 0.0
        implied = 1.0 / (distance + rate) if distance + rate > 0 else self._bands[-1]
        return min(self._bands, key=lambda band: abs(band - implied))

    def _map(self, venue_id, symbol, state, mark, clusters, open_interest, reason) -> LiquidationMap:
        return LiquidationMap(
            venue_id=venue_id,
            symbol=symbol,
            state=state,
            mark_price=mark,
            clusters=clusters,
            open_interest_notional=open_interest,
            largest_cluster=max(clusters, key=lambda c: c.notional, default=None),
            reason=reason,
            mapped_at_ns=self._now_ns(),
        )


def describe_liquidation_mapping(mapper: LiquidationClusterMapper) -> dict:
    return {
        "part_id": PART_ID,
        "leverage_bands": list(mapper._bands),
        "venues_with_a_margin_schedule": sorted(mapper._schedules),
        "maps_made": mapper.standing.maps_made,
        "maps_published": mapper.standing.maps_published,
        "refused_no_margin_schedule": mapper.standing.refused_no_schedule,
        "refused_no_open_interest": mapper.standing.refused_no_open_interest,
        "refused_no_volume_profile": mapper.standing.refused_no_profile,
        "clusters_produced": mapper.standing.clusters_produced,
        "liquidations_observed": mapper.standing.liquidations_observed,
        "observed_by_leverage_band": dict(sorted(mapper.standing.by_leverage_band.items())),
        "produces_a_direction": False,
    }


def run_liquidation_cluster_mapper(
    mapper: LiquidationClusterMapper, control_socket, read_market, publish_maps,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        symbols = read_market(mapper)
        publish_maps(
            tuple(mapper.map(venue_id, symbol, mark) for venue_id, symbol, mark in symbols)
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_liquidation_mapping(mapper),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Traded volume per price comes from the trade stream; the mark is the
    latest print. Open interest and the margin schedule are not on any input
    this part declares, so the map is built from the volume profile and the
    prior band shares, and says so in its reason.
    """
    import time as _time

    from runtime.input_assembly import Batch
    from runtime.venues.venue_adapter import NormalisedTrade

    trades = Batch(read=context.bus.reader("market-data"))
    books = Batch(read=context.bus.reader("order-book-snapshot"))
    publish_maps = context.bus.publisher_for("liquidation-map")
    mapper = LiquidationClusterMapper(
        leverage_bands=tuple(float(b) for b in context.setting("liquidation_leverage_bands").value),
        prior_band_share=context.number("liquidation_prior_band_share"),
        prior_weight=context.number("learning_prior_weight"),
        half_life_observations=context.number("learning_half_life_observations"),
        minimum_observations=int(context.number("learning_minimum_observations")),
        volume_profile_buckets=int(context.number("liquidation_volume_profile_buckets")),
    )
    marks: dict[tuple[str, str], float] = {}
    last_map = [float("-inf")]

    def read_market(_mapper):
        books.payloads()
        for trade in trades.payloads():
            if isinstance(trade, NormalisedTrade):
                mapper.observe_traded_volume(trade.venue_id, trade.symbol, trade.price, trade.quote_volume)
                marks[(trade.venue_id, trade.symbol)] = trade.price
        now = _time.monotonic()
        if now - last_map[0] < context.health_interval_seconds:
            return ()
        last_map[0] = now
        return tuple((key[0], key[1], mark) for key, mark in sorted(marks.items()))

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_maps(kept)

    return run_liquidation_cluster_mapper(
        mapper=mapper,
        control_socket=context.control_socket,
        read_market=read_market,
        publish_maps=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
