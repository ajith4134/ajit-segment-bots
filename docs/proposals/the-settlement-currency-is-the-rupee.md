# The settlement currency is the rupee, not the tether

**2026-09-12.** Goal 2, item 2 — every part still shaped like crypto is
converted to its Indian-market equivalent rather than deleted.

## What was wrong

`usdt-pnl-accountant` produces `usdt-pnl-statement`, consumed by
`board-snapshot-builder`, `reward-shaper`, `allocation-rebalance-proposer` and
`decision-cost-accountant`. USDT is a dollar stablecoin used to settle crypto
perpetuals. **No NSE trade has ever settled in it, and none ever will.**

This is not only a naming problem, which is why it is worth the blueprint edit
rather than a comment. `reward-shaper` refuses a trade with
`no-usdt-denominated-result-for-this-trade`, and its own reason says why that
refusal exists:

> rewarding whatever it settled in would teach the bot to prefer whichever
> currency happened to move

That reasoning is exactly right and survives the rename. What does not survive is
the currency: every segment on this spine states `quote_currency = "INR"`, so the
denomination the reward must be measured in is the rupee. A part refusing every
Indian trade for not being denominated in a crypto stablecoin is a part that
refuses everything, silently, with a sensible-looking reason.

## What it is now

| before | after |
|---|---|
| `usdt-pnl-accountant` (part) | `inr-pnl-accountant` |
| `usdt-pnl-statement` (data type) | `inr-pnl-statement` |
| `parts/portfolio_state/usdt_pnl_accountant.py` | `parts/portfolio_state/inr_pnl_accountant.py` |
| `net_pnl_usdt`, `capital_used_usdt`, `realised_usdt` | `_inr` throughout |
| `NO_USDT_STATEMENT` | `NO_INR_STATEMENT` |

Nothing about the mechanism changes. The same part consumes the same inputs,
produces the same shape, and the four consumers read the same fields under their
new names. This is a rename with the currency corrected, not a redesign — and it
is deliberately *not* generalised to a currency-neutral
`settled-pnl-statement`.

**Why not currency-neutral.** Rule 7 says use the domain's vocabulary, and the
domain here is one market settling in one currency. A neutral name would push the
currency into a field that every consumer then has to check, and a consumer that
forgot would do exactly what `reward-shaper`'s refusal exists to prevent. When
this project trades a second currency, the honest change is a currency-keyed
level, not a name that quietly hopes nobody mixes them.

## What this does not fix

The Indian charge stack is already real and measured elsewhere
(`runtime/indian_options_fee_model.py`, `runtime/indian_equity_fee_model.py`), so
the numbers flowing through this part were never dollar-denominated in practice —
only the names and the refusal message were. That makes this a low-risk rename
and also means **it does not on its own move any of the 85 settings still fitted
to crypto**, which are the sharper half of the same goal.

## Verification

- `python3 dashboard/check_contracts.py` — the registry holds after the edit,
  with the producer and all four consumers renamed together. A partial rename
  fails it, which is the point of doing the blueprint first.
- The three payload/call/input checkers, which between them catch a consumer
  reading a field the renamed producer no longer carries.
- `python3 dashboard/measure_objectives.py` section 3 — the drift count falls.
