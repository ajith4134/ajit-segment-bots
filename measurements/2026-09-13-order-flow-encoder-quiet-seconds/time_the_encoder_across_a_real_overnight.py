"""How much work order-flow-state-encoder did on quiet seconds, before and after 2026-09-13.

Replays real Upstox prints (through broker-market-data-bridge, as the live spine does) for
the 60 busiest NSE_FO contracts of two consecutive trading days on the tape, so the gap
between the sessions is the real overnight quiet stretch, through both the encoder that
encoded every quiet second and the one that encodes only the retained history. Settings
are the live ones (order_flow_volume_window 300, order_flow_minimum_volume_observations 30).

Run:  .venv/bin/python measurements/2026-09-13-order-flow-encoder-quiet-seconds/time_the_encoder_across_a_real_overnight.py
"""
import pathlib, sys, time
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from parts.prediction.order_flow_state_encoder import OrderFlowStateEncoder
from tests.conftest import busiest_upstox_instruments, upstox_trades_for

class EveryQuietSecond(OrderFlowStateEncoder):
    def _encode_quiet_seconds(self, key, first_second, end_second):
        for empty in range(first_second, end_second):
            self._encode_empty(key, empty)

OLDER, NEWER = "2026-09-07", "2026-09-08"
keys = [k for k in busiest_upstox_instruments(NEWER, 200) if k in set(busiest_upstox_instruments(OLDER, 2000))][:60]
trades = [t for t in upstox_trades_for(OLDER, keys, 10**9) + upstox_trades_for(NEWER, keys, 10**9) if t.quantity is not None]
print(f"{len(keys)} contracts, {len(trades):,} sized prints over {OLDER} and {NEWER}")
for label, cls in (("every quiet second (before)", EveryQuietSecond), ("retained history only (after)", OrderFlowStateEncoder)):
    encoder = cls(volume_window_seconds=300, minimum_volume_observations=30)
    started = time.perf_counter()
    for t in trades:
        encoder.observe_trade(t.venue_id, t.symbol, t.price, t.quantity, t.venue_time_ns)
    seconds = time.perf_counter() - started
    s = encoder.standing
    print(f"  {label:32} {seconds:8.2f}s  seconds encoded {s.seconds_encoded:>10,}  "
          f"quiet encoded {s.empty_seconds_encoded:>10,}  beyond the history {s.quiet_seconds_beyond_the_history:>10,}  "
          f"per print {seconds / len(trades) * 1e6:8.1f} us")
