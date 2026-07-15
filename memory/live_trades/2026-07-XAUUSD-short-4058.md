---
id: 2026-07-XAUUSD-short-4058
instrument: XAUUSD
direction: short
size_lots: 1
entry_price: 4058.38
exit_or_current: 4027.66
result: floating_win
pnl_eur: 2686
status: open
timeframe_of_analysis: M30
session: unknown
account_type: metatrader-large-account
tags: [short, htf-downtrend, retracement-into-ma, choch-exhaustion, smaller-lot, gold]
source: operator-live-screenshot
logged_by: operator
---

## The trade

Sold 1 lot XAUUSD at **4058.38**. Currently ~**4027.66**, about **+€2,686**
floating. Smaller lot than the 4-lot conviction trade — a lower-conviction
continuation entry after banking the big short.

## Chart context (M30)

- M30 downtrend, price below the declining MA.
- A CHoCH pushed price up against the MA; operator read it as **exhaustion**
  and sold in accordance with HTF bearish bias.
- Entry near a pullback high into the MA — same structural idea as the 4-lot
  trade, one timeframe lower.

## Mechanism (why the operator took it)

- **HTF bias:** still bearish.
- **Entry trigger:** LTF CHoCH up read as exhaustion of the counter-trend
  push; sold the rejection.
- **Trapped party:** longs who bought the CHoCH expecting a reversal; caught
  as price failed at the MA and resumed the downtrend.
- **Sizing:** deliberately smaller (1 lot vs 4) — lower conviction than the
  primary trade. Good discipline; the reduced size on a follow-up is exactly
  the risk-scaling the system should formalize.

## Honest notes

- This is a "second bite" continuation trade after the primary short. Second
  bites are lower expectancy than the primary move — the operator correctly
  sized down.
- CHoCH-as-exhaustion (vs CHoCH-as-reversal) is the hard live judgment. Here
  it resolved as exhaustion. The backtest must measure: when HTF is bearish
  and an LTF CHoCH-up occurs, does fading it (exhaustion read) beat trading
  it as a reversal? That is hypothesis H-CHOCH-EXHAUSTION below.

## Candidate hypothesis this generates

**H-CHOCH-EXHAUSTION:** In an HTF downtrend, an LTF (M15/M30) CHoCH to the
upside that stalls at a declining MA is more often exhaustion (continuation
down) than a genuine reversal. Fade it short with stop above the CHoCH high.
- Falsifier: reversal continuation up ≥ 50% of the time out-of-sample.
- This is directly testable and is the operator's own demonstrated read.
