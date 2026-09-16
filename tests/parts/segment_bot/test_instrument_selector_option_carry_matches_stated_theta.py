"""Does instrument-selector price an option's carry as the venue prices it?

`carry_over` is added to `round_trip_cost_fraction` and weighed against
`instrument_maximum_cost_fraction`, so it has to be in the same units the round
trip is: a fraction of the PREMIUM traded. Until 2026-09-16 the option branch
multiplied its decay by `premium_fraction` -- the option's price over the
underlying's -- restating it as a fraction of the underlying and dividing it by
about eighty.

The venue states the truth this test measures against: Upstox carries `theta` on
every greeks update, rupees of premium lost per day, which over a horizon is
`|theta| x horizon/86400 / premium` as a fraction of premium. Real captured
greeks and real captured prints only (RL-063) -- a fixture would agree with
whatever the code does.
"""

import bisect
import json
import math
import pathlib

import pytest

from parts.segment_bot.instrument_selector import (
    ListedInstrument,
    OPTION,
    InstrumentSelector,
)
from runtime.price_staleness import PriceStalenessEstimator
from runtime.tape import StreamKind, read_payload, read_tape_index

from tests.conftest import (
    UPSTOX_VENUE,
    upstox_days_newest_first,
    upstox_listings_by_key,
)

TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape" / UPSTOX_VENUE
HORIZON_SECONDS = 3600.0
NEAR_THE_MONEY = (0.4, 0.6)
# What the selector registers as ATM is what this compares, because that is the
# contract it actually chooses; a far-wing delta prices its theta differently.
SAMPLES_NEEDED = 200
CONTRACTS_READ = 400


def a_selector():
    return InstrumentSelector(
        built_segments=("index-options",),
        maximum_cost_fraction=0.5,
        round_trip_cost_fraction=0.008532,
        price_staleness=PriceStalenessEstimator(
            materiality_fraction=0.001, anchor_seconds=1.0, quantile=0.95,
            window=50, observations_needed=5, prior_one_second_move=0.0005,
            minimum_age_seconds=0.5, maximum_age_seconds=120.0,
        ),
        now_ns=lambda: 0,
    )


def a_listed_option(premium_fraction, seconds_to_expiry):
    return ListedInstrument(
        venue_id=UPSTOX_VENUE, symbol="NIFTY", instrument_kind=OPTION,
        contract_symbol="a captured contract",
        funding_rate_per_settlement=None, settlements_per_day=None,
        basis_fraction=None, premium_fraction=premium_fraction,
        seconds_to_expiry=seconds_to_expiry,
        supports_short=False, supports_long=True, supports_convexity=True,
        round_trip_cost_fraction=0.008532, absorbable_quote=None, seconds_to_fill=None,
    )


def last_price_at_or_before(times, prices, at_ns):
    position = bisect.bisect_right(times, at_ns) - 1
    return prices[position] if position >= 0 and prices[position] > 0 else None


def captured_prints(instrument_key, day):
    """Every distinct captured price for one instrument that day, oldest first."""
    directory = TAPE / instrument_key
    index_path = directory / f"{day}.index"
    blob_path = directory / f"{day}.blob"
    if not index_path.exists() or index_path.stat().st_size == 0:
        return [], []
    times, prices = [], []
    for record in read_tape_index(index_path):
        try:
            payload = json.loads(read_payload(blob_path, record))
        except Exception:
            continue
        # `last_traded_price` is the name Upstox's own feed uses and the tape
        # keeps; a reader of "price" finds nothing and reports no samples.
        price = payload.get("last_traded_price", payload.get("price"))
        if price is None or float(price) <= 0:
            continue
        if prices and float(price) == prices[-1]:
            continue
        times.append(int(record[0]))
        prices.append(float(price))
    return times, prices


def carry_and_stated_theta_samples():
    """(carry as the part prices it, carry as Upstox states it) per greeks update.

    Reads the newest captured day that carries enough near-the-money greeks, so
    the test measures this project's own tape rather than a day chosen by hand.
    """
    listings = upstox_listings_by_key()
    if not listings:
        return [], None
    for day in upstox_days_newest_first():
        samples = []
        spot_prints = {}
        greeks_directories = sorted(
            path for path in TAPE.glob("NSE_FO*")
            if (path / f"{day}.{StreamKind.OPTION_GREEKS.name.lower()}.index").exists()
        )
        for directory in greeks_directories[:CONTRACTS_READ]:
            listing = listings.get(directory.name)
            if listing is None or listing.expiry_ms is None or not listing.underlying_key:
                continue
            if listing.underlying_key not in spot_prints:
                spot_prints[listing.underlying_key] = captured_prints(listing.underlying_key, day)
            spot_times, spot_prices = spot_prints[listing.underlying_key]
            if not spot_times:
                continue
            contract_times, contract_prices = captured_prints(directory.name, day)
            if not contract_times:
                continue
            stream = StreamKind.OPTION_GREEKS.name.lower()
            blob_path = directory / f"{day}.{stream}.blob"
            for record in read_tape_index(directory / f"{day}.{stream}.index"):
                at_ns = int(record[0])
                try:
                    payload = json.loads(read_payload(blob_path, record))
                except Exception:
                    continue
                delta, theta = payload.get("delta"), payload.get("theta")
                if delta is None or theta is None:
                    continue
                if not NEAR_THE_MONEY[0] <= abs(float(delta)) <= NEAR_THE_MONEY[1]:
                    continue
                premium = last_price_at_or_before(contract_times, contract_prices, at_ns)
                spot = last_price_at_or_before(spot_times, spot_prices, at_ns)
                if premium is None or spot is None:
                    continue
                seconds_to_expiry = (listing.expiry_ms - at_ns / 1e6) / 1000.0
                if seconds_to_expiry <= HORIZON_SECONDS:
                    continue
                carry = a_selector().carry_over(
                    a_listed_option(premium / spot, seconds_to_expiry), HORIZON_SECONDS
                )
                stated = abs(float(theta)) * HORIZON_SECONDS / 86400.0 / premium
                if carry is None or stated <= 0:
                    continue
                samples.append((carry, stated, premium / spot))
        if len(samples) >= SAMPLES_NEEDED:
            return samples, day
    return [], None


def median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


@pytest.fixture(scope="module")
def captured_carry_samples():
    samples, day = carry_and_stated_theta_samples()
    if not samples:
        pytest.skip(
            "no captured day carries "
            f"{SAMPLES_NEEDED} near-the-money option-greeks updates with prints "
            "for both the contract and its underlying"
        )
    return samples, day


def test_the_carry_this_part_prices_agrees_with_the_theta_upstox_states(captured_carry_samples):
    samples, _day = captured_carry_samples
    ratios = [stated / carry for carry, stated, _ in samples]
    # The square-root decay is a model of theta, not a restatement of it, so this
    # asserts the two agree in magnitude -- measured 1.1x at the median on
    # 2026-09-07/08 -- not that they are equal. A units error cannot hide inside
    # this band: the one this replaces was 80.6x.
    assert 0.5 <= median(ratios) <= 2.0, (
        f"median stated/priced carry {median(ratios):.2f}x over {len(samples):,} samples"
    )


def test_pricing_the_carry_against_the_underlying_would_understate_it_by_an_order_of_magnitude(
    captured_carry_samples,
):
    """The defect this test exists for, measured rather than described.

    Multiplying the decay by `premium_fraction` is what the part did until
    2026-09-16. Recomputed here from the same samples so the regression is a
    number on this project's own tape, not a comment.
    """
    samples, _day = captured_carry_samples
    understated = [stated / (carry * premium_fraction) for carry, stated, premium_fraction in samples]
    assert median(understated) > 10.0, (
        "the pre-2026-09-16 formula would have been "
        f"{median(understated):.1f}x low, which this test no longer distinguishes"
    )


def test_a_carry_that_eats_the_round_trip_is_reachable_within_a_contract_s_life(
    captured_carry_samples,
):
    """A cost that no real contract can reach is a gate that never gates.

    `select` weighs `round_trip_cost_fraction + carry` against
    `instrument_maximum_cost_fraction`, so what matters is how close to expiry a
    contract has to be before its carry alone exceeds the round trip. Solved for
    that moment rather than counted on one day's sample, because which contracts
    a given captured day holds is an accident of when it was captured -- the
    2026-09-07/08 tape carried a weekly expiry and 7.8% of its near-the-money
    samples were over the round trip; a day whose chain is three weeks out
    carries none, and neither fact is about this formula.

    carry = 1 - sqrt(1 - horizon/T) exceeds `cost` when T < horizon / (1 - (1 -
    cost)^2) -- about 2.5 days at a one-hour horizon, which every weekly expiry
    passes through. Scaled by `premium_fraction` it takes until the last hour of
    the contract's life, by which point the horizon outlives the contract and the
    gate can never act.
    """
    samples, _day = captured_carry_samples
    round_trip = 0.008532

    def expiry_at_which_carry_reaches(cost):
        return HORIZON_SECONDS / (1.0 - (1.0 - cost) ** 2)

    priced_in_premium = expiry_at_which_carry_reaches(round_trip)
    assert priced_in_premium > 86400.0, (
        f"carry only reaches the round trip {priced_in_premium / 3600:.1f}h before expiry"
    )

    # The pre-2026-09-16 formula, against each sample's real premium/spot.
    scaled = []
    for _carry, _stated, premium_fraction in samples:
        needed = round_trip / premium_fraction
        if needed >= 1.0:
            # Unreachable at any time to expiry: the decay tops out at 1.0.
            scaled.append(float("inf"))
            continue
        scaled.append(expiry_at_which_carry_reaches(needed))
    reachable_from = median(scaled)
    assert reachable_from <= 2 * HORIZON_SECONDS or reachable_from == float("inf"), (
        f"the scaled carry would have reached the round trip {reachable_from / 3600:.1f}h "
        "before expiry, which this test no longer distinguishes from reachable"
    )
