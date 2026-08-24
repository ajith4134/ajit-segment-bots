"""Does the all-market quote stream carry every update, or a sampled one?

Two sockets, same symbol, same window. If the per-symbol stream carries many more
updates for that symbol than the all-market stream does, the all-market stream is
throttled -- and a quote's whole value is its age.
"""
import asyncio, json, time, websockets

SYMBOL = "BTCUSDT"
SECONDS = 30

async def count(url, label, symbol):
    n, start = 0, time.time()
    async with websockets.connect(url, ping_interval=20, max_size=None, open_timeout=20) as ws:
        while time.time() - start < SECONDS:
            try: raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError: continue
            d = json.loads(raw)
            for it in (d if isinstance(d, list) else [d]):
                if isinstance(it, dict) and it.get("s") == symbol:
                    n += 1
    return label, n

async def main():
    results = await asyncio.gather(
        count(f"wss://fstream.binance.com/ws/{SYMBOL.lower()}@bookTicker", "per-symbol @bookTicker", SYMBOL),
        count("wss://fstream.binance.com/ws/!bookTicker", "all-market !bookTicker", SYMBOL),
    )
    for label, n in results:
        print(f"{label:26s} {n:6d} updates for {SYMBOL} in {SECONDS}s  ({n/SECONDS:6.1f}/s)")
asyncio.run(main())
