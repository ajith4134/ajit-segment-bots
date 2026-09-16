"""The realised-vol regressor's weights stay bounded on a real expiry day.

Found 2026-09-16 by the detector-edge measurement: on NIFTY's 16 SEP 25 expiry an
expiring put's one-minute window carried a 100% gap, a standardised feature of 20.7,
and plain gradient descent at a fixed rate is stable only while rate x (sum of
squared inputs) < 2. Here that was 0.01 x ~800. The weights reached the hundreds
within 3,808 updates and the next forecast overflowed `math.exp`.

Real data only (RL-063): the ten index underlyings the index segment derives, and
their contracts, for 2025-09-16 -- Upstox history, cached after the first run.
"""

from __future__ import annotations

import math

import pytest

from operate.historical_prints import NoBrokerToken, upstox_access_token

DAY = "2025-09-16"
INDICES = ("BANKEX", "BANKNIFTY", "FINNIFTY", "FOCIT", "MIDCPNIFTY", "NIFTY",
           "NIFTYFPI", "NIFTYNXT50", "SENSEX", "SENSEX50")


def test_an_expiry_day_does_not_blow_the_weights_up():
    try:
        upstox_access_token()
    except NoBrokerToken as missing:
        pytest.skip(str(missing))
    from operate.measure_detector_edge import DetectorEdgeRun
    from operate.past_session_prints import past_session_instruments

    run = DetectorEdgeRun()
    run.run_session(DAY, past_session_instruments(DAY, INDICES, 8, 1))
    regressor = run.chain.volatility_regressor
    assert regressor.standing.observations > 3_808
    weights = regressor.coefficients
    assert all(math.isfinite(value) for value in weights.values())
    # Standardised inputs: a weight far past the target's own log-scale means the
    # descent has left the problem rather than solved it.
    assert max(abs(value) for value in weights.values()) < abs(math.log(1e-12))
