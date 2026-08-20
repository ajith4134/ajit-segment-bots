# Two Instagram carousels: open-source trading repos (read 2026-08-20)

Sources, downloaded with instaloader, every slide read as an image:

- `Db37hHEGCLh/` — @evolving.qc, "10 GitHub repos every trader should know about", 12 slides
- `DcJGPhDiZJx/` — @syntaix.ai, "13 open-source repos for building trading bots", 17 slides

Both are listicles. Neither carries a mechanism, formula or architecture diagram beyond
the Lumibot slide (one code path for backtest / paper / live). Every number on the
syntaix slides is a mockup dashboard (+214% etc.) and is not evidence of anything.

## Every repo named, verified with `gh repo view` on 2026-08-20

| slide says | real repo | stars | last push | licence | note |
|---|---|---|---|---|---|
| TradingAgents | TauricResearch/TradingAgents | 99,024 | 2026-07-18 | Apache-2.0 | multi-agent LLM desk: analysts debate, then a trader acts |
| Backtrader | mementum/backtrader | 22,901 | **2024-08-19** | GPL-3.0 | two years unmaintained; slide says "Active" |
| NautilusTrader | nautechsystems/nautilus_trader | 26,643 | 2026-08-20 | LGPL-3.0 | already an architecture reference here |
| Freqtrade | freqtrade/freqtrade | 53,458 | 2026-08-20 | GPL-3.0 | already a candidate dependency here |
| CCXT | ccxt/ccxt | 43,669 | 2026-08-20 | MIT | already a candidate dependency here |
| VectorBT | polakowo/vectorbt | 8,733 | 2026-08-02 | "Fair Code" (not OSI) | community edition of a paid PRO |
| Polymarket API | Polymarket/py-sdk (`pip install polymarket-client`) | — | 2026-08-17 | — | slide names no URL; `py-clob-client` is archived |
| Lumibot | Lumiwealth/lumibot | 1,947 | 2026-08-20 | GPL-3.0 | one strategy code path for backtest, paper, live |
| Hummingbot | hummingbot/hummingbot | 19,518 | 2026-08-20 | Apache-2.0 | market making + arbitrage, multi-venue |
| FinRL | AI4Finance-Foundation/FinRL | 16,048 | 2026-07-13 | MIT | deep RL trading environments |
| Jesse | jesse-ai/jesse | 8,348 | 2026-08-19 | MIT | crypto research + backtest framework |
| "Trading-Bot" `jesse-ai/trading-bot` | **does not exist** | — | — | — | slide 6 of syntaix is fabricated |
| LEAN | QuantConnect/Lean | 21,272 | 2026-08-19 | Apache-2.0 | multi-asset engine, C#/Python |
| Qlib | microsoft/qlib | 47,778 | 2026-07-23 | MIT | AI quant platform, factor research |
| TensorTrade | tensortrade-org/tensortrade | 6,979 | 2026-02-19 | Apache-2.0 | listed twice by syntaix with two different star counts |
| OctoBot | Drakkar-Software/OctoBot | 6,441 | 2026-08-17 | GPL-3.0 | visual strategy editor, multi-exchange |
| FreqAI `freqtrade/freqai` | **does not exist** as a repo | — | — | — | FreqAI is a module inside freqtrade |

Star counts printed on the syntaix slides are wrong for every repo checked (Backtrader
15.2k vs 22.9k real; FinRL 9.6k vs 16.0k; Qlib 11.2k vs 47.8k). Treat that account as
a generated template, per the instagram-content skill's standing warning.

Next step taken the same day: each real repo's source read by an agent and mapped onto
the 22 foundation blocks — see `docs/research/repo-harvest/`.
