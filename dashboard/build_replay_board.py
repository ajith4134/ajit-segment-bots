#!/usr/bin/env python3
"""Render what a replayed session actually traded, and say plainly that it is one.

`operate/replay_a_captured_session.py` walks captured Upstox prints through the
real fill, lot-book and close-detection parts. This renders its result.

**Every row here is a replay and the page says so in its own heading, its
banner and every table caption.** The board that shows live trades is
`trade-board.html`; on 2026-09-04 that board was found serving the retired
crypto era's closed trades as though they were this segment's, which is the
exact failure Rule 8 exists to prevent. A replay presented as live is the same
mistake with a different source, so the two are separate pages and this one
cannot be confused for the other.

Nothing here is hand-written. If no replay has been run, the page says
NOTHING REPLAYED rather than rendering an empty table that looks like a
session in which nothing happened to trade.
"""

from __future__ import annotations

import datetime
import html
import json
import pathlib
import sys

REPLAY_ROOT = pathlib.Path.home() / ".local/share/ajit-segment-bots/replay"
OUTPUT = pathlib.Path(__file__).resolve().parent / "replay-board.html"

STYLE = """
:root {
  --ground:#eceff4; --surface:#ffffff; --ink:#161a21; --muted:#5c6675;
  --line:#d3d9e2; --line-strong:#b6bfcd; --accent:#0c7c74; --warn:#9c6100;
  --fail:#a32b22; --replay:#5b4bbd;
  --shadow:0 1px 2px rgba(22,26,33,.06), 0 8px 24px -16px rgba(22,26,33,.28);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground:#0f1319; --surface:#171c24; --ink:#e4e8ee; --muted:#8a93a2;
    --line:#262d38; --line-strong:#3a4453; --accent:#35c9bc; --warn:#e0a22b;
    --fail:#f0776b; --replay:#a99cf5;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -16px rgba(0,0,0,.8);
  }
}
:root[data-theme="dark"] {
  --ground:#0f1319; --surface:#171c24; --ink:#e4e8ee; --muted:#8a93a2;
  --line:#262d38; --line-strong:#3a4453; --accent:#35c9bc; --warn:#e0a22b;
  --fail:#f0776b; --replay:#a99cf5;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -16px rgba(0,0,0,.8);
}
* { box-sizing:border-box; }
body { margin:0; background:var(--ground); color:var(--ink); line-height:1.55;
  font-family:"IBM Plex Sans",system-ui,-apple-system,sans-serif;
  -webkit-font-smoothing:antialiased; }
.page { max-width:1180px; margin:0 auto; display:flex; flex-direction:column;
  gap:2.2rem; padding:clamp(1.5rem,4vw,3.5rem) clamp(1rem,4vw,2rem) 5rem; }
.eyebrow { font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:.72rem;
  letter-spacing:.14em; text-transform:uppercase; color:var(--muted); }
h1 { font-family:Archivo,system-ui,sans-serif; font-weight:700; margin:0;
  font-size:clamp(1.9rem,5vw,2.9rem); letter-spacing:-.025em; line-height:1.05; }
.banner { border:1px solid var(--replay); border-left:4px solid var(--replay);
  border-radius:3px; padding:.9rem 1.1rem; color:var(--ink); background:var(--surface);
  box-shadow:var(--shadow); }
.banner strong { color:var(--replay); font-family:"IBM Plex Mono",ui-monospace,monospace;
  letter-spacing:.08em; }
.stamp { display:flex; flex-wrap:wrap; gap:.5rem 1.25rem; padding-top:.9rem;
  border-top:1px solid var(--line); font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-size:.82rem; color:var(--muted); }
.stamp strong { color:var(--ink); font-weight:500; font-variant-numeric:tabular-nums; }
h2 { font-family:Archivo,system-ui,sans-serif; font-weight:600; font-size:.82rem;
  letter-spacing:.12em; text-transform:uppercase; color:var(--muted); margin:0 0 .8rem; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:.85rem; }
.tile { background:var(--surface); border:1px solid var(--line);
  border-left:3px solid var(--line-strong); border-radius:3px; padding:.9rem 1rem;
  display:flex; flex-direction:column; gap:.3rem; box-shadow:var(--shadow); }
.tile .k { font-size:.74rem; letter-spacing:.06em; text-transform:uppercase; color:var(--muted);
  font-family:"IBM Plex Mono",ui-monospace,monospace; }
.tile .v { font-size:1.35rem; font-variant-numeric:tabular-nums; }
.tile.win { border-left-color:var(--accent); } .tile.win .v { color:var(--accent); }
.tile.lose { border-left-color:var(--fail); } .tile.lose .v { color:var(--fail); }
.table-wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:.88rem; min-width:820px; }
th { text-align:right; font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:.68rem;
  letter-spacing:.1em; text-transform:uppercase; color:var(--muted); font-weight:500;
  padding:.5rem .7rem; border-bottom:1px solid var(--line-strong); }
th:first-child, td:first-child { text-align:left; }
td { padding:.55rem .7rem; border-bottom:1px solid var(--line); color:var(--muted);
  text-align:right; font-variant-numeric:tabular-nums; }
td:first-child { color:var(--ink); white-space:nowrap;
  font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:.8rem; }
td.win { color:var(--accent); } td.lose { color:var(--fail); }
.flag { font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:.62rem;
  letter-spacing:.08em; text-transform:uppercase; color:var(--warn);
  border:1px solid currentColor; border-radius:2px; padding:.05rem .3rem; margin-left:.4rem; }
.empty { border:1px dashed var(--line-strong); border-radius:3px; text-align:center;
  padding:clamp(2rem,6vw,3.5rem) 1.5rem; color:var(--muted); }
.empty .headline { font-family:Archivo,system-ui,sans-serif; font-weight:600;
  font-size:1.05rem; color:var(--ink); }
footer { border-top:1px solid var(--line); padding-top:1.1rem; color:var(--muted);
  font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:.74rem; line-height:1.8; }
"""


def latest_replay() -> tuple[pathlib.Path, dict] | tuple[None, None]:
    """The most recently replayed day, or nothing if none has been run."""
    if not REPLAY_ROOT.is_dir():
        return None, None
    files = sorted(REPLAY_ROOT.glob("*.closed-trades.json"))
    if not files:
        return None, None
    newest = files[-1]
    try:
        return newest, json.loads(newest.read_text())
    except (OSError, ValueError):
        return newest, None


def money(value: float) -> str:
    return f"{value:,.2f}"


def render(document: dict, source: pathlib.Path) -> str:
    trades = document.get("trades", [])
    closed = [t for t in trades if t.get("closed")]
    opened = [t for t in trades if t.get("opened")]
    won = [t for t in closed if t.get("net_pnl", 0) > 0]
    fees = sum(t.get("fees_paid", 0.0) for t in closed)
    net = sum(t.get("net_pnl", 0.0) for t in closed)

    rows = []
    for trade in sorted(closed, key=lambda t: -t.get("net_pnl", 0.0)):
        name = trade.get("trading_symbol") or trade.get("instrument_key") or trade.get("symbol")
        flag = ('<span class="flag">venue key</span>'
                if trade.get("name_is_the_venues_key") else "")
        outcome = "win" if trade.get("net_pnl", 0) > 0 else "lose"
        rows.append(
            f"<tr><td>{html.escape(str(name))}{flag}</td>"
            f"<td>{trade['entry_price']:.2f}</td>"
            f"<td>{trade['exit_price']:.2f}</td>"
            f"<td>{trade['stop_price']:.2f}</td>"
            f"<td>{trade['target_price']:.2f}</td>"
            f"<td>{trade['quantity']:.0f}</td>"
            f"<td>{money(trade['fees_paid'])}</td>"
            f"<td class='{outcome}'>{money(trade['net_pnl'])}</td>"
            f"<td>{trade.get('prints_walked', 0):,}</td></tr>"
        )

    unclosed = [t for t in trades if t.get("opened") and not t.get("closed")]
    refused = [t for t in trades if not t.get("opened")]

    table = (
        '<div class="table-wrap"><table><thead><tr>'
        "<th>contract</th><th>entry</th><th>exit</th><th>stop</th><th>target</th>"
        "<th>qty</th><th>fees</th><th>net</th><th>prints walked</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
        if rows else
        '<div class="empty"><div class="headline">NOTHING CLOSED</div>'
        "<p>The replay opened positions and none of them reached a stop or a target "
        "inside the captured session.</p></div>"
    )

    net_class = "win" if net > 0 else "lose"
    return f"""<title>Replay Board — {html.escape(document.get('replayed_day',''))}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{STYLE}</style>
<div class="page">
  <header>
    <div class="eyebrow">ajit-segment-bots · index-options</div>
    <h1>Replay Board</h1>
    <div class="banner" style="margin-top:1rem">
      <strong>THIS IS A REPLAY — NOT LIVE TRADING.</strong>
      Every row below was produced by walking captured Upstox prints from
      {html.escape(document.get('replayed_day',''))} through the same fill, lot-book and
      close-detection parts the live spine runs. No live money, no live decision, and
      nothing here reached the spine's own state (RL-071). Live trades are on the
      trade board, which never shows these.
    </div>
    <div class="stamp">
      <span>replayed day <strong>{html.escape(document.get('replayed_day',''))}</strong></span>
      <span>run at <strong>{html.escape(document.get('replayed_at',''))}</strong></span>
      <span>rendered <strong>{datetime.datetime.now().astimezone():%Y-%m-%d %H:%M %Z}</strong></span>
      <span>source <strong>{html.escape(document.get('source',''))}</strong></span>
    </div>
  </header>

  <section>
    <h2>What the replay did</h2>
    <div class="grid">
      <div class="tile"><div class="k">contracts replayed</div><div class="v">{len(trades)}</div></div>
      <div class="tile"><div class="k">positions opened</div><div class="v">{len(opened)}</div></div>
      <div class="tile"><div class="k">trades closed</div><div class="v">{len(closed)}</div></div>
      <div class="tile"><div class="k">closed in profit</div><div class="v">{len(won)} of {len(closed)}</div></div>
      <div class="tile"><div class="k">fees paid (INR)</div><div class="v">{money(fees)}</div></div>
      <div class="tile {net_class}"><div class="k">net after fees (INR)</div><div class="v">{money(net)}</div></div>
    </div>
  </section>

  <section>
    <h2>Closed trades — replayed, not live</h2>
    {table}
  </section>

  <footer>
    Generated by dashboard/build_replay_board.py from {html.escape(str(source))}<br>
    Real: every price is a captured Upstox print, in the order and at the time it printed.
    The fills, fees (Upstox's own charge stack on both legs), lot book, excursion tracking
    and close detection are the live parts, not reimplementations.<br>
    Stated: the entry is the session's first print for that contract, and the exits sit
    {document.get('stop_multiple_of_typical_move','?')}x (stop) and
    {document.get('target_multiple_of_typical_move','?')}x (target) the contract's own median
    move between prints. The live path takes these from stop-target-placer, which needs a
    volatility forecast a cold replay does not have.<br>
    Quantity is {document.get('lot_size','?')} per trade.
    {len(unclosed)} opened without closing; {len(refused)} never opened.<br>
    A contract tagged <span class="flag">venue key</span> is shown by its Upstox instrument
    key because the captured instrument master does not name it.
  </footer>
</div>
"""


def main() -> int:
    source, document = latest_replay()
    if document is None:
        OUTPUT.write_text(f"""<title>Replay Board — nothing replayed</title>
<style>{STYLE}</style>
<div class="page"><header><div class="eyebrow">ajit-segment-bots</div>
<h1>Replay Board</h1></header>
<div class="empty"><div class="headline">NOTHING REPLAYED</div>
<p>No replay has been run, so there is nothing to show. This is the absence of a
run, not a run in which nothing traded — the two are different facts and this page
will not render one as the other.</p>
<p>Run: <code>python3 operate/replay_a_captured_session.py</code></p></div></div>
""")
        print(f"wrote {OUTPUT} (NOTHING REPLAYED)")
        return 0
    OUTPUT.write_text(render(document, source))
    closed = [t for t in document.get("trades", []) if t.get("closed")]
    print(f"wrote {OUTPUT}  ({len(closed)} closed trades from {document.get('replayed_day')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
