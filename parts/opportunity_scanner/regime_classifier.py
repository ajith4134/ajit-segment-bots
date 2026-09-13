"""regime-classifier: which regime the market is in, by a Hurst proxy.

Every detector downstream is right in one regime and wrong in another. A mean
reverter is a machine for losing money in a trend; a momentum detector gives back
everything it makes in a chop. So the regime is not decoration -- it is the thing
that decides which detectors should be believed at all.

Hurst rather than a moving-average cross, because a cross tells you what the
price did and Hurst tells you what *kind* of series it is. A series can be rising
and mean-reverting at once, and a cross cannot express that.

Three states, and the third is the important one:

- **Trending** -- moves persist, so continuation setups have an edge.
- **Reverting** -- moves are given back, so reversion setups have an edge.
- **Random** -- neither. Both kinds of detector will lose slowly, and saying so
  is more useful than picking the nearer of the two.

**Unclassified is its own state.** Below enough observations the estimator swings
wildly on the same data, and a regime that flips between trending and reverting
every tick is worse than no regime at all.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.lot_book_checkpoint import book_key_of, book_key_text
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.price_frames import levels_in
from runtime.rolling_statistics import RollingWindow, hurst_exponent

PART_ID = "regime-classifier"
CHECKPOINT_COMPONENT = "series"

PART_DECLARATION = PartDeclaration(
    part_id="regime-classifier",
    consumes=("symbol-price-frame",),
    produces=("market-regime", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

TRENDING = "trending"
REVERTING = "reverting"
RANDOM = "random-walk"
UNCLASSIFIED = "unclassified"

# The Hurst value of a pure random walk. Everything here is a distance from it.
RANDOM_WALK_HURST = 0.5


@dataclass(frozen=True)
class MarketRegime:
    """What kind of series this symbol is right now, and how sure that is."""

    venue_id: str
    symbol: str
    regime: str
    hurst: float | None
    distance_from_random: float | None
    observations: int
    volatility: float | None
    reason: str
    classified_at_ns: int

    @property
    def is_classified(self) -> bool:
        return self.regime != UNCLASSIFIED

    def favours(self, expectation: str) -> bool:
        """Whether this regime supports a detector expecting reversion or continuation."""
        from runtime.market_signal import CONTINUATION, REVERSION

        if self.regime == TRENDING:
            return expectation == CONTINUATION
        if self.regime == REVERTING:
            return expectation == REVERSION
        return False


@dataclass
class ClassifierStanding:
    observations: int = 0
    classifications: int = 0
    symbols_tracked: int = 0
    by_regime: dict = field(default_factory=dict)
    unclassified: int = 0
    # Not counters: what happened to the checkpoint at start. A part that came
    # back holding nothing and one whose checkpoint could not be read are
    # different facts, and only the second is a fault (Rule 8).
    restored_symbols: int = 0
    checkpoint_verdict: str = ""
    # A symbol whose series went silent past its own gap bound is no longer one of
    # this part's subjects. Counted rather than dropped quietly: a number that
    # climbs steadily is a feed losing symbols, which is a finding, and one that
    # jumps once at start is a checkpoint outliving the universe it was written for.
    symbols_forgotten_silent: int = 0


class RegimeClassifier:
    """Estimates the Hurst exponent per symbol and names the regime it implies."""

    def __init__(
        self,
        window_length: int,
        minimum_observations: int,
        trending_above: float,
        reverting_below: float,
        maximum_gap_seconds: float | None = None,
        gap_patience_multiple: float | None = None,
        gap_warmup_gaps: int | None = None,
        now_ns=time.time_ns,
    ) -> None:
        if not reverting_below < RANDOM_WALK_HURST < trending_above:
            raise ValueError(
                "the reverting and trending thresholds must sit either side of the "
                f"random-walk value of {RANDOM_WALK_HURST}"
            )
        self._window_length = window_length
        self._minimum = minimum_observations
        self._trending_above = trending_above
        self._reverting_below = reverting_below
        self._now_ns = now_ns
        # How long a symbol may be silent before its window is judged to have a
        # hole in it rather than a series. None means the caller stated no bound,
        # and this part does not invent one (RL-061).
        self._maximum_gap_seconds = maximum_gap_seconds
        self._gap_patience_multiple = gap_patience_multiple
        self._gap_warmup_gaps = gap_warmup_gaps
        self._prices: dict[tuple[str, str], RollingWindow] = {}
        # When this part last *received* anything for a symbol, on its own clock.
        # Not the venue's print time, which is what the window keeps: with the
        # market shut every print carries a stamp hours old, so judging "is this
        # still one of my subjects" by the venue's clock forgets every symbol the
        # moment the session closes and re-adds it on the next poll. Measured
        # 2026-09-04 on the first run of the sweep below: 12,903 forgettings in
        # ten minutes from a universe of 3,209.
        self._last_seen_at_ns: dict[tuple[str, str], int] = {}
        self.standing = ClassifierStanding()

    def observe_price(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        """One print, with the venue's own time for it.

        A regime is a statement about a continuous stretch of market. Computed
        across a hole in the feed it describes two stretches with the reconnect
        between them read as a move, which is how a quiet market and a dead socket
        come to be classified as a breakout.
        """
        self.standing.observations += 1
        self._last_seen_at_ns[(venue_id, symbol)] = self._now_ns()
        self._window_for((venue_id, symbol)).observe(price, at_ns)
        self.standing.symbols_tracked = len(self._prices)

    def classify(self, venue_id: str, symbol: str) -> MarketRegime:
        window = self._prices.get((venue_id, symbol))
        if window is None:
            return self._regime(
                venue_id, symbol, UNCLASSIFIED, None, 0, None,
                "no prices have been seen for this symbol",
            )

        series = list(window.values)
        hurst = hurst_exponent(series, self._minimum)
        volatility = window.standard_deviation(self._minimum)
        self.standing.classifications += 1

        if hurst is None:
            self.standing.unclassified += 1
            return self._regime(
                venue_id, symbol, UNCLASSIFIED, None, len(series), volatility,
                f"{len(series)} observations of the {self._minimum} needed; below that the "
                f"estimator swings on the same data and a regime that flips every tick is "
                f"worse than none",
            )

        if hurst >= self._trending_above:
            regime = TRENDING
            reason = f"Hurst {hurst:.3f} above {self._trending_above:.3f}: moves persist"
        elif hurst <= self._reverting_below:
            regime = REVERTING
            reason = f"Hurst {hurst:.3f} below {self._reverting_below:.3f}: moves are given back"
        else:
            regime = RANDOM
            reason = (
                f"Hurst {hurst:.3f} is inside [{self._reverting_below:.3f}, "
                f"{self._trending_above:.3f}]: neither kind of detector has an edge here"
            )

        self.standing.by_regime[regime] = self.standing.by_regime.get(regime, 0) + 1
        return self._regime(venue_id, symbol, regime, hurst, len(series), volatility, reason)

    def classify_all(self) -> tuple[MarketRegime, ...]:
        """Every symbol this part is still watching.

        Callers wanting the dropped keys as well -- to forget them somewhere else
        too -- call `forget_silent_symbols` themselves first; this is the shorthand
        for the ones that do not. On 2026-09-04 the difference between the two sets
        was 3,208 symbols restored from a checkpoint written in the crypto era: this
        returned 3,209 regimes once per health interval, 99.2% of them classifying
        nothing, for a part that had received 1,190 prices.
        """
        self.forget_silent_symbols()
        return tuple(self.classify(venue, symbol) for venue, symbol in sorted(self._prices))

    def forget_silent_symbols(self, now_ns: int | None = None) -> tuple:
        """Drop every series whose next print would clear it anyway. Returns which.

        The keys, not a count, because whoever holds a per-symbol structure beside
        this one must drop the same symbols: a level publisher keyed by symbol would
        otherwise remember what was last said about a symbol that no longer exists,
        which is the unbounded-structure shape all over again one layer along.

        The window decides whether the series is over: `has_gone_silent_past_its_bound`
        is the same comparison `observe` makes on an arriving gap, so nothing is
        discarded here that the symbol's own next observation would not discard. A
        window with no gap bound is never dropped -- it was given no rule for what a
        hole is.

        **But silence is measured on this part's clock as well as the venue's.** With
        the market shut every print carries a stamp from before the close, so by the
        window's measure alone every symbol is silent the moment the session ends --
        and the next poll re-adds it, to be dropped again. Measured on the first run
        of this sweep, live: 12,903 forgettings in ten minutes from a universe of
        3,209, with `symbols_tracked` reading 3.

        So both conditions are required: the series must be over *and* nothing must
        have arrived about it. A symbol is one of this part's subjects for as long as
        messages about it keep coming, however old the stamps they carry. The rule
        itself is `runtime.rolling_statistics.subjects_gone_quiet`, because
        `correlation-cluster-mapper` needs the same answer about the same kind of
        window and a second statement of it is a second thing to keep in step.
        """
        from runtime.rolling_statistics import subjects_gone_quiet

        at = self._now_ns() if now_ns is None else now_ns
        gone = subjects_gone_quiet(self._prices, self._last_seen_at_ns, at)
        for key in gone:
            del self._prices[key]
            self._last_seen_at_ns.pop(key, None)
        self.standing.symbols_forgotten_silent += len(gone)
        return tuple(gone)

    def _window_for(self, key) -> RollingWindow:
        window = self._prices.get(key)
        if window is None:
            window = RollingWindow(
                length=self._window_length,
                maximum_gap_seconds=self._maximum_gap_seconds,
                gap_patience_multiple=self._gap_patience_multiple,
                gap_warmup_gaps=self._gap_warmup_gaps,
            )
            self._prices[key] = window
        return window

    # -- what survives a restart ----------------------------------------------
    #
    # Held in memory alone until 2026-08-29: `regime_minimum_observations` is set
    # equal to the 1024-trade window on purpose (below 512 the Hurst estimate
    # swings wider than the distance from a random walk to either regime), and
    # every restart discarded whatever a symbol had accumulated toward it.
    # Measured live at the time this was written: 48,192 classification
    # attempts, 48,192 unclassified -- not one symbol had ever reached the
    # floor, across a spine that had been restarted several times in the
    # session. `RollingWindow` already carries `as_document`/`restore_document`
    # for exactly this (built for cointegration-pair-finder's identical shape
    # of problem); this wires the same mechanism here.

    def read_checkpoint_state(self) -> dict:
        """Every symbol's price series, so a restart does not start blind.

        Each window carries its own last observation time, so the gap across a
        restart is measured rather than assumed continuous -- without it the
        first price after an outage sits beside the last one before it and
        reads as an instant move, which is exactly the shape a regime change
        would misread as a trend.
        """
        return {
            "prices": {
                book_key_text(key): window.as_document()
                for key, window in self._prices.items()
            },
        }

    def restore_from_checkpoint(self, state: dict) -> int:
        """Refill every series. Returns how many symbols came back."""
        for text, document in (state.get("prices") or {}).items():
            window = self._window_for(book_key_of(text))
            window.restore_document(document)
        self.standing.symbols_tracked = len(self._prices)
        self.standing.restored_symbols = len(self._prices)
        return len(self._prices)

    def _regime(self, venue_id, symbol, regime, hurst, observations, volatility, reason) -> MarketRegime:
        return MarketRegime(
            venue_id=venue_id,
            symbol=symbol,
            regime=regime,
            hurst=hurst,
            distance_from_random=None if hurst is None else hurst - RANDOM_WALK_HURST,
            observations=observations,
            volatility=volatility,
            reason=reason,
            classified_at_ns=self._now_ns(),
        )


def describe_regimes(classifier: RegimeClassifier, levels=None) -> dict:
    level_standing = {} if levels is None else {
        "regimes_published": levels.standing.publishes,
        "unchanged_regimes_skipped": levels.standing.unchanged_publishes_skipped,
        "regime_refreshes": levels.standing.refreshes,
        "regime_changes": levels.standing.changes,
        "symbols_held_as_levels": levels.keys_held,
    }
    return {
        "part_id": PART_ID,
        "observations": classifier.standing.observations,
        "symbols_tracked": classifier.standing.symbols_tracked,
        "classifications": classifier.standing.classifications,
        "unclassified": classifier.standing.unclassified,
        "by_regime": dict(classifier.standing.by_regime),
        "restored_symbols": classifier.standing.restored_symbols,
        "checkpoint_verdict": classifier.standing.checkpoint_verdict,
        "symbols_forgotten_silent": classifier.standing.symbols_forgotten_silent,
        **level_standing,
    }


def run_regime_classifier(
    classifier: RegimeClassifier, control_socket, read_prices, publish_regimes,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
    write_checkpoint=None,
    levels=None,
    forget_level=lambda key: None,
) -> int:
    """`publish_regimes(key, regimes)` -- keyed, because the level is per symbol.

    `forget_level` drops a symbol from whatever remembers what was last said about
    it, so the two structures shed the same symbols on the same sweep.
    """
    import time as _time

    last_full_publish = [float("-inf")]

    def tick() -> None:
        # Only the symbols whose price moved are reclassified. The part now
        # wakes on every arriving burst (2026-08-23), and classifying all sixty
        # symbols -- a Hurst exponent over each window -- on each wake made its
        # tick slower than the feed, so it lost market-data at about nine
        # messages a second while using a fifth of a core. A regime for a symbol
        # with no new price is the regime already published. The full set is still
        # swept once per health interval, so a consumer started later holds every
        # symbol within a second.
        #
        # What is computed and what is sent are two decisions since 2026-09-04.
        # The sweep above paces the work; `levels` paces the sending, and a regime
        # that has not changed does not go on the bus again until its own refresh
        # is due. Measured that day, with neither in place: 33,535,257
        # `market-regime` messages published from 1,190 prices received, 69% of
        # all traffic on the spine, with the market shut.
        touched = set()
        for venue_id, symbol, price, at_ns in read_prices():
            classifier.observe_price(venue_id, symbol, price, at_ns)
            touched.add((venue_id, symbol))
        now = _time.monotonic()
        if now - last_full_publish[0] >= health_interval_seconds:
            for key in classifier.forget_silent_symbols():
                forget_level(key)
            regimes = classifier.classify_all()
            last_full_publish[0] = now
        else:
            regimes = tuple(classifier.classify(v, s) for v, s in sorted(touched))
        for regime in regimes:
            publish_regimes((regime.venue_id, regime.symbol), (regime,))
        if touched and write_checkpoint is not None:
            write_checkpoint(classifier.standing.observations)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_regimes(classifier, levels),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Trades arrive as an event stream and prices are observed one by one; the regime
    is a level, republished every tick for every symbol seen so far. That asymmetry
    is deliberate. A consumer that joined late must still learn what regime a symbol
    is in, and a classifier that only spoke when the regime *changed* would leave it
    with nothing and no way to know it was missing something.

    Woken by its data rather than by its clock: the whole point of the regime is to
    be current when a detector asks, and the tick floor keeps a busy symbol from
    spinning this part at the rate of the tape.

    Every symbol's series survives a restart, since 2026-08-29: beside
    cointegration-pair-finder's own price series under `position_state_root`,
    because it is the same class of fact -- a stretch of market this part has
    been watching -- read off the same wire.
    """
    import pathlib

    from runtime.durable_state import CheckpointSchedule, DurableStateStore, restore_and_arm_checkpoint
    from runtime.input_assembly import Batch
    from runtime.level_publishing import LevelPublisherByKey, without_observation_time

    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    publish_to_bus = context.bus.publisher_for("market-regime")
    # One level per symbol, so one symbol's new price does not restate the other
    # 599. Each key keeps its own refresh clock, so the refreshes spread across the
    # interval instead of arriving as the one-per-second burst that `classify_all`
    # used to send whole.
    #
    # Its own refresh interval, not the shared one, because the keepalive is **per
    # key** and this level has more keys than any other. Measured 2026-09-04 after
    # the change check went in: 600 symbols held, 14 parts consuming market-regime,
    # so one publish is 14 datagrams and the refresh alone floors this part at
    # 8,400 messages a second whatever the change check does. The skip count was
    # 55,212 and the bus rate had not fallen -- the check was working and was not
    # the lever.
    levels = LevelPublisherByKey(
        publish=publish_to_bus,
        refresh_interval_seconds=context.number("regime_refresh_interval_seconds"),
        identity_of=without_observation_time,
    )
    classifier = RegimeClassifier(
        window_length=int(context.number("regime_window_length")),
        minimum_observations=int(context.number("regime_minimum_observations")),
        trending_above=context.number("regime_trending_hurst_above"),
        reverting_below=context.number("regime_reverting_hurst_below"),
        maximum_gap_seconds=context.number("price_series_maximum_gap_seconds"),
        gap_patience_multiple=context.number("price_gap_patience_multiple"),
        gap_warmup_gaps=int(context.number("price_gap_warmup_gaps")),
    )
    store = DurableStateStore(
        pathlib.Path(str(context.setting("position_state_root").value)).expanduser()
    )
    # The settings that give the stored series their meaning. A window judged
    # against a different length or gap bound describes a different stretch of
    # market -- restoring across such a change would test one span while
    # reporting another, the same reasoning cointegration-pair-finder's own
    # checkpoint uses.
    series_settings = {
        "regime_window_length": float(context.number("regime_window_length")),
        "price_series_maximum_gap_seconds": float(
            context.number("price_series_maximum_gap_seconds")
        ),
        "price_gap_patience_multiple": float(context.number("price_gap_patience_multiple")),
    }
    write_checkpoint = restore_and_arm_checkpoint(
        store,
        CheckpointSchedule(int(context.number("regime_state_checkpoint_interval"))),
        PART_ID,
        CHECKPOINT_COMPONENT,
        classifier,
        series_settings,
    )

    def read_prices():
        return tuple(
            (level.venue_id, level.symbol, level.price, level.observed_at_ns)
            for level in levels_in(trades.payloads())
        )

    return run_regime_classifier(
        classifier=classifier,
        control_socket=context.control_socket,
        read_prices=read_prices,
        publish_regimes=levels.publish_level,
        levels=levels,
        forget_level=levels.forget,
        health_interval_seconds=context.health_interval_seconds,
        emit_health=context.emit_health,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        write_checkpoint=write_checkpoint,
    )
