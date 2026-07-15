---
id: 2026-07-XAUUSD-short-4128
instrument: XAUUSD
direction: short
size_lots: 4
entry_price: 4128.21
exit_or_current: 4030.14
result: won
pnl_eur: 34440
status: closed
timeframe_of_analysis: H1
session: unknown
account_type: metatrader-large-account
tags: [short, htf-downtrend, retracement-into-ma, lower-high, gold, war-macro-context]
source: operator-live-screenshot
logged_by: operator
---

## The trade

Sold 4 lots XAUUSD at **4128.21**. Rode the H1 downtrend down ~98 points to
~**4030.14** for roughly **+€34,440** floating, then closed around +€30–34k.
(Operator: "I closed the trade around 30k.")

## Chart context (H1)

- Clear H1 downtrend from a ~4154 swing high, price making lower highs.
- Entry was a **lower high / retracement into the declining MA** — price
  pushed up against the falling moving average and was sold in alignment
  with the higher-timeframe bearish bias.
- A/D oscillator and volume were visible on the operator's chart; the entry
  coincided with a rejection of the MA.

## Mechanism (why it worked)

- **HTF bias:** bearish (lower highs, declining MA).
- **Entry trigger:** retracement/exhaustion into the declining MA — a
  counter-trend push read as exhaustion, sold in the direction of the trend.
- **Trapped party:** late longs who bought the retracement expecting
  continuation up; they were caught as price rejected the MA and resumed down.
- This is the same structural idea as the mandate's spike-recovery, mirrored
  for shorts: fade the counter-trend push, trade with HTF bias.

## Honest notes

- Operator flagged that the first (earlier) weekend trade's exact entry was
  "a bit luck." This 4-lot trade was purer chart analysis: HTF bias +
  retracement-into-MA.
- The 4-lot size on a strong-conviction setup is larger than the follow-up
  1-lot — conviction-scaled sizing (good instinct; formalize as fractional
  Kelly later).
- War / macro factors and CME weekend continuation were tailwinds but the
  entry itself was structural.

## What this feeds

This is a real example of the candidate setup
`memory/setups/htf-retracement-continuation.md`. It seeds the autopsy
database with a live, operator-voiced win. When the backtest harness runs
that setup on history, this trade is one of the pattern instances we want
it to reproduce.
