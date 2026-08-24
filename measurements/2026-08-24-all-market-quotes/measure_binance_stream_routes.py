import asyncio, json, time, collections, websockets

HOST = "wss://fstream.binance.com"
ROUTES = ["/ws", "/market/ws", "/public/ws"]
STREAMS = ["!miniTicker@arr", "!ticker@arr", "!bookTicker"]

async def sample(url, seconds):
    seen = collections.Counter(); msgs = 0; start = time.time()
    try:
        async with websockets.connect(url, ping_interval=20, max_size=None, open_timeout=15) as ws:
            while time.time() - start < seconds:
                try: raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, seconds-(time.time()-start)))
                except asyncio.TimeoutError: break
                msgs += 1
                d = json.loads(raw)
                for it in (d if isinstance(d, list) else [d]):
                    if isinstance(it, dict) and it.get("s"): seen[it["s"]] += 1
    except Exception as e:
        return f"FAILED {type(e).__name__}"
    return f"{len(seen):4d} symbols  {msgs:6d} msgs"

async def main():
    for stream in STREAMS:
        for route in ROUTES:
            r = await sample(f"{HOST}{route}/{stream}", 10)
            print(f"{stream:18s} {route:12s} -> {r}", flush=True)
asyncio.run(main())
