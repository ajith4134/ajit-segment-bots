"""Can one Bybit public connection carry tickers for the whole linear universe?
Lists every symbol, measures the args length against the venue's 21,000-char cap,
subscribes, and counts coverage. Read-only."""
import asyncio, json, time, collections, urllib.request, websockets

REST = "https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000"
WS = "wss://stream.bybit.com/v5/public/linear"

def symbols():
    out, cursor = [], None
    while True:
        url = REST + (f"&cursor={cursor}" if cursor else "")
        d = json.load(urllib.request.urlopen(url, timeout=20))["result"]
        out += [i["symbol"] for i in d["list"] if i.get("status") == "Trading"]
        cursor = d.get("nextPageCursor")
        if not cursor: break
    return sorted(set(out))

async def main():
    syms = symbols()
    topics = [f"tickers.{s}" for s in syms]
    args_len = len(json.dumps(topics))
    print(f"{len(syms)} trading linear symbols; args array is {args_len} chars vs the 21,000 cap")
    if args_len > 21000:
        print("  does NOT fit one connection -- would need splitting")
    seen = collections.Counter(); last = {}; gaps = {}
    start = time.time()
    async with websockets.connect(WS, ping_interval=None, max_size=None, open_timeout=20) as ws:
        # Bybit expects the client to ping every 20s.
        async def pinger():
            while True:
                await asyncio.sleep(15)
                try: await ws.send(json.dumps({"op": "ping"}))
                except Exception: return
        ping_task = asyncio.create_task(pinger())
        await ws.send(json.dumps({"op": "subscribe", "args": topics}))
        while time.time() - start < 90:
            try: raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError: continue
            m = json.loads(raw)
            if m.get("op") == "subscribe":
                print(f"  subscribe ack: success={m.get('success')} {str(m.get('ret_msg'))[:80]}")
                continue
            d = m.get("data")
            if not isinstance(d, dict): continue
            s = d.get("symbol")
            if not s: continue
            now = time.time()
            seen[s] += 1
            if s in last: gaps.setdefault(s, []).append(now - last[s])
            last[s] = now
        ping_task.cancel()
    dur = time.time() - start
    print(f"window {dur:.0f}s: {len(seen)} of {len(syms)} symbols quoted, {sum(seen.values())} updates")
    if gaps:
        worst = sorted(((max(g), s) for s, g in gaps.items()), reverse=True)[:5]
        print("  worst per-symbol gaps:", [(s, round(g,1)) for g, s in worst])
    silent = [s for s in syms if s not in seen]
    print(f"  never quoted in the window: {len(silent)}  e.g. {silent[:6]}")
asyncio.run(main())
