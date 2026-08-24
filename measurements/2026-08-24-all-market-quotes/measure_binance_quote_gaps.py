"""Does the quietest symbol in the whole universe still quote often enough to
size a position against? One !bookTicker socket, 90 seconds, per-symbol gaps."""
import asyncio, json, time, statistics, websockets

URL = "wss://fstream.binance.com/ws/!bookTicker"
SECONDS = 90

async def main():
    last = {}; gaps = {}; counts = {}
    start = time.time()
    async with websockets.connect(URL, ping_interval=20, max_size=None, open_timeout=15) as ws:
        while time.time() - start < SECONDS:
            try: raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError: continue
            d = json.loads(raw); s = d.get("s")
            if not s: continue
            now = time.time()
            counts[s] = counts.get(s, 0) + 1
            if s in last: gaps.setdefault(s, []).append(now - last[s])
            last[s] = now
    dur = time.time() - start
    print(f"window {dur:.0f}s, {len(counts)} symbols quoted, {sum(counts.values())} updates")
    worst = sorted(((max(g), s) for s, g in gaps.items()), reverse=True)[:8]
    print("worst per-symbol quote gap:")
    for g, s in worst: print(f"   {s:16s} {g:6.1f}s   ({counts[s]} updates)")
    allmax = [max(g) for g in gaps.values()]
    allmax.sort()
    print(f"median symbol's worst gap {statistics.median(allmax):.1f}s")
    print(f"p95 of worst gaps {allmax[int(len(allmax)*0.95)]:.1f}s")
    once = [s for s, c in counts.items() if c == 1]
    print(f"symbols that quoted exactly once in the window: {len(once)}")
asyncio.run(main())
