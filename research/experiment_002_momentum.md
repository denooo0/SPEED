# Experiment 002 — Momentum Continuation (+ a metric bug caught)

## Setup

- **Data:** recent XAUUSD H1, 2004-06 → 2025-06 (122k bars, FeziweMelvin repo
  via raw.githubusercontent.com). Fetched by ATLAS directly — the operator's
  local Desktop file was unreachable, so ATLAS sourced regime-recent gold
  itself. Note: 2023→2025 gold **rose +84%**, 2004→2025 **+778%** — a secular
  bull; short-only fights the tide.
- **Strategy:** `MomentumContinuation` — trade WITH momentum: enter in the
  trend direction (price vs MA) on a breakout of the recent range when recent
  N-bar momentum is strong. Symmetric long/short. Built from Exp-001's
  "trade with momentum, not against; use reachable targets" learnings.

## Result: FAIL (robustly, once metrics are honest)

Every configuration — long/short/symmetric, targets 0.75R…12R, momentum
thresholds 100/200bp, full history and 2023+ slice — produced **negative
equity and negative Sharpe**:

| Config (long, full history) | Trades | Win% | Sharpe | Total return |
|---|---|---|---|---|
| rr=2 | 1633 | 35% | −2.72 | **−96%** |
| rr=5 | 1171 | 18% | −1.73 | −94% |
| rr=8 | 953 | 14% | −1.08 | −87% |
| rr=12 | 808 | 10% | −0.91 | −87% |
| **Buy & hold** | — | — | — | **+778%** |

Every active variant destroyed value versus simply holding gold.

## A metric bug caught (and fixed) mid-experiment

An intermediate run showed impossible numbers: mean R-multiple of **5.9
billion**. Investigation:

- Data was clean (no bad candles — worst intrabar range ~6% in real 2008/2011
  flash moves).
- Root cause was in the **simulator's R-multiple**: when a next-bar-open fill
  gaps to/through the intended stop, `risk_per_unit = max(net_entry − stop,
  1e-12)` collapsed to ~1e-12, so `R = move / risk` exploded to millions.
- The **equity curve was never affected** (it uses actual gross returns, not
  R), so the trustworthy verdict — deeply negative — was always correct. But
  the R-multiple, and any statistic derived from it, was polluted.

**Fix** (`src/backtest/simulator.py`): floor `risk_per_unit` at 5bp of the fill
price. R-multiples are now bounded and meaningful; equity math unchanged.
Added to the integrity guarantees alongside the lookahead audit.

**Lesson:** "engine not polluted" is not only about lookahead. Metric
pathologies (a clamp that turns a gap-fill into infinite R) manufacture false
positives just as dangerously. Always cross-check R-based numbers against
equity-based ones; trust equity.

## The cumulative, robust conclusion (Exp 001 + 002)

Across **two patterns** (retracement fade, momentum continuation), **both
directions**, **two decades / two regimes**, **many targets and thresholds**,
and after fixing two robustness bugs:

> **No simple mechanical technical pattern tested has any edge on gold H1 that
> survives realistic costs.** A friction floor (~0.15–0.22R from spread +
> slippage + next-bar-open fill gap) plus the difficulty of timing beats every
> raw rule. Buy-and-hold beats them all.

Because ~30+ configurations were tried and *all* failed, the negative is
robust — there is no multiple-testing/overfit concern in a conclusion that
never turned positive.

## What this does and does not mean

- **Does mean:** the operator's discretionary edge does not transfer to naive
  mechanical H1 rules. Confirmed twice.
- **Does NOT mean:** the operator can't trade profitably (their live €34k /
  €2.6k shorts are real). It means the *mechanization* of that discretion, via
  simple technical patterns on this timeframe/data, does not work.

## Genuinely-remaining options (honest)

1. **Higher-timeframe trend-following (daily).** Untested cleanly. Gold's
   secular trend is huge; a daily long-biased trend-follower with wide stops +
   proper sizing might capture a chunk. Caveat: even success here is mostly
   *capturing gold beta*, not alpha, and it's long-biased, not the operator's
   short edge.
2. **LLM-filter A/B (Path-A thesis).** Does LLM selection turn a negative
   baseline positive? Honest prior is poor — the baseline is −96%, so filtering
   must find a rare positive subset, and the Alpha Arena evidence is against
   LLM-as-trader.
3. **Reframe the product.** The operator's edge may be discretionary +
   macro/regime + execution discipline — not a mechanizable signal. A bot that
   assists (risk discipline, position management, alerting, journaling,
   autopsy) rather than generates signals may be the realistic, valuable
   product.

## What is genuinely won regardless

The infrastructure is honest and fast: it killed false hopes in an afternoon
for the cost of compute, caught its own two bugs, and would immediately
validate any real edge the moment one appears. That is exactly what a research
platform is for. The failures are the platform *working*, not the platform
failing.

## Provenance

- Data: https://github.com/FeziweMelvin/XAUUSD-Gold-Price (XAU_1h_data.csv)
- Strategy: `src/strategies/momentum_continuation.py`
- Metric fix: `src/backtest/simulator.py` (risk_per_unit floor)
