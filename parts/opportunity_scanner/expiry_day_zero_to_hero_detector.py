"""expiry-day-zero-to-hero-detector: a deep-OTM index option cheap enough to
spike sharply if the underlying reaches its strike before today's close.

Index options only (NIFTY, BANK NIFTY, SENSEX -- the three with liquid
weekly expiries; stock options mostly don't have this dynamic). The
retail-known "zero to hero" trade: gamma explodes as expiry approaches, so
an option trading near zero can multiply many times over on a late move.

**Named risk, not an asserted edge.** The overwhelming majority of these
options expire worthless -- that is why they are cheap. This detector
produces a candidate; sizing and whether it's worth trading at all go
through the same risk-capital-allocation gate and calibrated confidence
every other detector's candidates do.

No live Indian option-chain data has been captured yet (spec section 4,
docs/superpowers/specs/2026-09-01-options-segment-bots-design.md), so the
thresholds below are constructor arguments with no measured backing --
same discipline every other detector in this project uses (a threshold is
never a literal inside the class), but the *deployed* setting values in
start_part are explicitly marked not-yet-measured rather than presented as
justified.
"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass, field

from runtime.market_signal import LONG, SHORT, SignalCalibrator, make_candidate, settle_claims_from
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "expiry-day-zero-to-hero-detector"

PART_DECLARATION = PartDeclaration(
    part_id="expiry-day-zero-to-hero-detector",
    consumes=(
        "broker-subscribed-instrument-listing", "broker-market-data", "broker-option-greeks",
        "broker-price-frame", "training-label",
    ),
    produces=("entry-candidate", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

FIRED = "fired"
NOT_EXPIRY_DAY = "not-expiry-day"
PREMIUM_TOO_HIGH = "premium-too-high"
NOT_FAR_ENOUGH_OTM = "not-far-enough-otm"
NO_LISTING = "no-listing-known"
NOT_AN_OPTION = "not-an-option-instrument"
NO_PREMIUM = "no-premium-observed"
NO_DELTA = "no-delta-observed"

_CALL = "CE"
_PUT = "PE"


@dataclass
class DetectorStanding:
    listings_seen: int = 0
    # What the sweep costs, and what it would have cost. The catalogue carries
    # every tracked instrument -- 102,940 of them on 2026-09-04 -- and all but a
    # handful can be ruled out by their expiry date alone, which does not change
    # between ticks. Both numbers are on health so a day with no expiry reads as
    # "nothing expires today" rather than as a detector that stopped (Rule 8).
    instruments_known: int = 0
    instruments_expiring_today: int = 0
    instruments_that_are_not_options: int = 0
    ltps_seen: int = 0
    greeks_seen: int = 0
    detections_run: int = 0
    fired: int = 0
    not_expiry_day: int = 0
    premium_too_high: int = 0
    not_far_enough_otm: int = 0
    no_listing: int = 0
    not_an_option: int = 0
    no_premium: int = 0
    no_delta: int = 0


class ZeroToHeroDetector:
    """Holds the latest listing/premium/delta per instrument; judges each on demand."""

    def __init__(
        self,
        maximum_premium: float,
        maximum_abs_delta: float,
        horizon_seconds: float,
        calibrator: SignalCalibrator,
        now_ns=time.time_ns,
    ) -> None:
        if not maximum_premium > 0:
            raise ValueError(
                f"maximum_premium is how cheap counts as 'zero', in rupees, and must be "
                f"positive; got {maximum_premium!r}"
            )
        if not 0.0 < maximum_abs_delta < 1.0:
            raise ValueError(
                f"maximum_abs_delta is a delta magnitude, which is bounded in (0, 1) by "
                f"definition; got {maximum_abs_delta!r}"
            )
        self._maximum_premium = maximum_premium
        self._maximum_abs_delta = maximum_abs_delta
        self._horizon = horizon_seconds
        self._calibrator = calibrator
        self._now_ns = now_ns
        self._listings: dict[str, object] = {}
        self._premiums: dict[str, float] = {}
        self._deltas: dict[str, float] = {}
        # Instrument keys grouped by the date their contract expires, so a tick
        # asks "what expires today" with one date conversion instead of one per
        # instrument. See `expiring_on` for what this replaced.
        self._keys_by_expiry_date: dict[datetime.date, set[str]] = {}
        self._expiry_date_of_key: dict[str, datetime.date] = {}
        self.standing = DetectorStanding()

    def observe_listing(self, listing) -> None:
        """Record the listing, and file it under the date its contract expires.

        Filing here rather than judging at detection time is the whole cost of
        this part. An expiry date is a property of the contract and cannot change
        between ticks, but `_is_expiry_today` converted two timestamps to dates
        for every instrument on every tick -- about 200,000 conversions a tick
        against the 102,940 instruments the catalogue carries, which measured
        0.957 of a core on 2026-09-04, 28% of the whole spine, for a detector
        that had fired nothing.
        """
        key = listing.instrument_key
        # Whether this key has ever been seen, read before the store is written:
        # a non-option filed for the first time and one restated for the hundredth
        # both have no expiry date, and telling them apart is what makes
        # `instruments_that_are_not_options` a count of instruments rather than of
        # messages.
        first_sight = key not in self._listings
        self._listings[key] = listing
        self.standing.listings_seen += 1

        # An INDEX listing carries no expiry: it is not an option contract, and it
        # can never be a candidate. Counted once here rather than refused on every
        # tick forever.
        expiry_date = None
        if listing.expiry_ms is not None:
            expiry_date = datetime.datetime.fromtimestamp(
                listing.expiry_ms / 1000, tz=IST
            ).date()

        previous = self._expiry_date_of_key.get(key)
        if previous == expiry_date and not first_sight:
            return
        if previous is not None:
            bucket = self._keys_by_expiry_date.get(previous)
            if bucket is not None:
                bucket.discard(key)
                if not bucket:
                    del self._keys_by_expiry_date[previous]
        if expiry_date is None:
            self._expiry_date_of_key.pop(key, None)
            if first_sight:
                self.standing.instruments_that_are_not_options += 1
            return
        self._expiry_date_of_key[key] = expiry_date
        self._keys_by_expiry_date.setdefault(expiry_date, set()).add(key)

    def observe_ltp(self, update) -> None:
        self._premiums[update.instrument_key] = float(update.last_traded_price)
        self.standing.ltps_seen += 1

    def observe_greeks(self, greeks) -> None:
        self._deltas[greeks.instrument_key] = float(greeks.delta)
        self.standing.greeks_seen += 1

    def _is_expiry_today(self, expiry_ms: int, now_ns: int) -> bool:
        expiry_date = datetime.datetime.fromtimestamp(expiry_ms / 1000, tz=IST).date()
        today_date = datetime.datetime.fromtimestamp(now_ns / 1e9, tz=IST).date()
        return expiry_date == today_date

    def expiring_on(self, now_ns: int | None = None) -> tuple[str, ...]:
        """The instruments whose contracts expire on the given day, sorted.

        The only instruments this part can ever fire on. Everything else is
        refused by `detect` for a reason that cannot change between ticks -- it is
        not an option, or it expires on some other date -- so asking the question
        once per listing beats asking it once per instrument per tick.

        Dates already past are dropped as they are passed over: the index is keyed
        by date and would otherwise keep one bucket per expiry ever seen, which is
        the unbounded-structure shape this project keeps paying for.
        """
        at = self._now_ns() if now_ns is None else now_ns
        today = datetime.datetime.fromtimestamp(at / 1e9, tz=IST).date()
        for expired in [day for day in self._keys_by_expiry_date if day < today]:
            for key in self._keys_by_expiry_date.pop(expired):
                self._expiry_date_of_key.pop(key, None)
        self.standing.instruments_known = len(self._listings)
        today_keys = self._keys_by_expiry_date.get(today, ())
        self.standing.instruments_expiring_today = len(today_keys)
        return tuple(sorted(today_keys))

    def detect(self, instrument_key: str, now_ns: int | None = None):
        """Judge one instrument. Returns (candidate, reason) -- candidate is
        None on refusal, and the reason names exactly which check failed."""
        at = self._now_ns() if now_ns is None else now_ns
        self.standing.detections_run += 1

        listing = self._listings.get(instrument_key)
        if listing is None:
            self.standing.no_listing += 1
            return None, NO_LISTING
        # broker-instrument-listing carries every tracked instrument, the
        # underlying index/stock included -- an INDEX listing's expiry_ms is
        # None (it is not an option contract), which crashed the live spine
        # 2026-09-02 the moment a real NIFTY 50 index listing reached here.
        if listing.expiry_ms is None:
            self.standing.not_an_option += 1
            return None, NOT_AN_OPTION
        if not self._is_expiry_today(listing.expiry_ms, at):
            self.standing.not_expiry_day += 1
            return None, NOT_EXPIRY_DAY

        premium = self._premiums.get(instrument_key)
        if premium is None:
            self.standing.no_premium += 1
            return None, NO_PREMIUM
        if premium > self._maximum_premium:
            self.standing.premium_too_high += 1
            return None, PREMIUM_TOO_HIGH

        delta = self._deltas.get(instrument_key)
        if delta is None:
            self.standing.no_delta += 1
            return None, NO_DELTA
        if abs(delta) > self._maximum_abs_delta:
            self.standing.not_far_enough_otm += 1
            return None, NOT_FAR_ENOUGH_OTM

        direction = LONG if listing.instrument_type == _CALL else SHORT
        # Cheaper and further OTM both read as "more room to multiply" --
        # blended as the average of how far under each cutoff this
        # candidate sits, each expressed as a fraction of its own ceiling.
        premium_headroom = 1.0 - (premium / self._maximum_premium)
        delta_headroom = 1.0 - (abs(delta) / self._maximum_abs_delta)
        signal_strength = (premium_headroom + delta_headroom) / 2.0

        calibration_key = listing.underlying_key
        confidence = self._calibrator.confidence(PART_ID, calibration_key)
        self.standing.fired += 1
        return (
            make_candidate(
                detector=PART_ID,
                venue_id="upstox",
                symbol=instrument_key,
                direction=direction,
                expectation="continuation",
                signal_strength=signal_strength,
                confidence=confidence,
                calibration_key=calibration_key,
                horizon_seconds=self._horizon,
                evidence={
                    "premium": premium,
                    "delta": delta,
                    "strike_price": listing.strike_price,
                    "expiry_ms": listing.expiry_ms,
                    "underlying_key": listing.underlying_key,
                },
                reason=(
                    f"{instrument_key} expires today at premium {premium:.2f} (cutoff "
                    f"{self._maximum_premium:.2f}) and delta {delta:+.3f} (cutoff "
                    f"{self._maximum_abs_delta:.2f}) -- {confidence.value:.0%} of this "
                    f"underlying's past calls have held within the horizon "
                    f"({'measured' if confidence.is_fitted else 'the prior'})"
                ),
                now_ns=self._now_ns,
            ),
            FIRED,
        )

    def observe_outcome(self, calibration_key: str, was_right: bool) -> None:
        self._calibrator.observe_outcome(PART_ID, calibration_key, was_right)


def describe_detector(detector: ZeroToHeroDetector) -> dict:
    s = detector.standing
    return {
        "part_id": PART_ID,
        # What the sweep actually costs. `instruments_known` is the catalogue --
        # 102,940 on 2026-09-04 -- and `instruments_expiring_today` is what this
        # part now looks at; the gap between them is the fix. Both are here so a
        # quiet day reads as "nothing expires today" and not as a stopped part.
        "instruments_known": s.instruments_known,
        "instruments_expiring_today": s.instruments_expiring_today,
        "instruments_that_are_not_options": s.instruments_that_are_not_options,
        "listings_seen": s.listings_seen,
        "ltps_seen": s.ltps_seen,
        "greeks_seen": s.greeks_seen,
        "detections_run": s.detections_run,
        "fired": s.fired,
        "not_expiry_day": s.not_expiry_day,
        "premium_too_high": s.premium_too_high,
        "not_far_enough_otm": s.not_far_enough_otm,
        "no_listing": s.no_listing,
        "not_an_option": s.not_an_option,
        "no_premium": s.no_premium,
        "no_delta": s.no_delta,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    `zero_to_hero_maximum_premium`/`zero_to_hero_maximum_abs_delta` are
    explicitly marked NOT YET MEASURED in the settings file -- no live
    Indian option-chain data has been captured to justify a real cutoff,
    the same honest gap the spec names rather than papers over.
    """
    from runtime.brokers.broker_adapter import (
        BrokerOptionGreeks, InstrumentListing, LtpUpdate,
    )
    from runtime.input_assembly import Batch

    listings = Batch(read=context.bus.reader("broker-subscribed-instrument-listing"))
    ltps = Batch(read=context.bus.reader("broker-market-data"))
    greeks = Batch(read=context.bus.reader("broker-option-greeks"))
    labels = Batch(read=context.bus.reader("training-label"))
    publish_candidates = context.bus.publisher_for("entry-candidate")

    detector = ZeroToHeroDetector(
        maximum_premium=context.number("zero_to_hero_maximum_premium"),
        maximum_abs_delta=context.number("zero_to_hero_maximum_abs_delta"),
        horizon_seconds=context.number("zero_to_hero_horizon_seconds"),
        calibrator=SignalCalibrator(
            prior_hit_rate=context.number("signal_prior_hit_rate"),
            prior_weight=context.number("signal_prior_weight"),
            half_life_observations=context.number("signal_half_life_observations"),
            minimum_observations=int(context.number("signal_minimum_observations")),
        ),
    )
    def tick() -> None:
        for payload in listings.payloads():
            for listing in payload if isinstance(payload, tuple) else (payload,):
                if isinstance(listing, InstrumentListing):
                    detector.observe_listing(listing)
        for ltp in ltps.payloads():
            if isinstance(ltp, LtpUpdate):
                detector.observe_ltp(ltp)
        for greek in greeks.payloads():
            if isinstance(greek, BrokerOptionGreeks):
                detector.observe_greeks(greek)
        settle_claims_from(labels.payloads(), detector, PART_ID)

        # Only the contracts that expire today can fire, and which those are is
        # decided when a listing arrives rather than for every instrument on every
        # tick. Sweeping all of them cost 0.957 of a core on 2026-09-04 -- 28% of
        # the spine, and the largest single cost on it -- because
        # `_is_expiry_today` converted two timestamps to dates for each of the
        # 102,940 instruments the catalogue carries, every tick, to reach the same
        # answer it had reached the tick before.
        candidates = tuple(
            candidate
            for instrument_key in detector.expiring_on()
            for candidate, _ in (detector.detect(instrument_key),)
            if candidate is not None
        )
        if candidates:
            publish_candidates(candidates)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_detector(detector),
    )


__all__ = [
    "FIRED",
    "NOT_EXPIRY_DAY",
    "NOT_FAR_ENOUGH_OTM",
    "NO_DELTA",
    "NO_LISTING",
    "NO_PREMIUM",
    "PART_DECLARATION",
    "PART_ID",
    "PREMIUM_TOO_HIGH",
    "DetectorStanding",
    "ZeroToHeroDetector",
    "describe_detector",
    "start_part",
]
