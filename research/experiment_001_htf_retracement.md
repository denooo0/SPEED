# Experiment 001 — HTF Retracement Short (deterministic baseline)

> First real backtest. Logged as a scientific result, not spun.

## Setup

- **Strategy:** `HtfRetracementShort` — sell every retracement into a declining
  MA in a local downtrend, stop above the retracement high, 2R target.
  Deterministic, self-contained (no LLM, no lens confluence, no context filter).
- **Data:** XAUUSD H1, 57,600 bars, **2012-05-17 → 2022-03-04** (ejtraderLabs
  public historical-data repo, fetched via raw.githubusercontent.com).
- **Costs:** realistic — spread + slippage + funding + next-bar-open fills.
- **Engine:** certified clean by the poisoning audit (no lookahead).

## Result: FAIL

| Metric | Value | Gate |
|---|---|---|
| Sharpe (in-sample) | **−3.76** | — |
| Win rate | **32.2%** | — |
| Trades | 1,132 | ≥ 200 ✓ |
| DSR | ~0 | > 0.95 ✗ |
| PBO | 0.00 | < 0.20 ✓ |
| Walk-forward pass ratio | 0.00 | ≥ 0.70 ✗ |

The naive mechanical pattern has **negative expectancy**: 32% win rate at a 2R
target = `0.32×2 − 0.68×1 = −0.04R` per trade *before* costs, worse after. It
loses money.

## Honest interpretation (two layers)

**Layer 1 — the raw pattern is not a standalone edge.** A 30-line mechanical
"sell any declining-MA retracement" rule does not print money on a decade of
gold. This is the correct, healthy result. If it *had* passed, that would
signal a bug or overfit. The engine is honest — it manufactured no edge.

**Layer 2 — the test is biased against the strategy, three ways:**

1. **Short-only over a net-UP decade.** This data ran 2012→2022, gold
   **+26.8%** net ($1553 → $1970). A short-only strategy fights the secular
   tide in aggregate. The operator's actual trades were shorts in a *specific
   strong 2026 downtrend* — the opposite regime.
2. **No trend-strength filter.** The rule fires on *every* declining-MA
   pullback, including weak local dips inside a secular bull (bear traps). 1,132
   trades in 10 years = far too many, mostly low quality. The operator took a
   handful of high-conviction setups, not 113/year.
3. **No confluence / context / session filter.** The operator's edge used HTF
   bias discretion, CHoCH-exhaustion timing, and session awareness. The
   mechanical version has none of that — it is the raw skeleton, not the trade.

## What this does and does not prove

- **Proves:** the raw mechanical pattern, unfiltered, is negative-expectancy on
  2012–2022 gold. Do not trade it as-is.
- **Does NOT prove:** the operator's discretionary edge is fake. It proves the
  edge (if real) lives in the *selection* — which setups to take — not in the
  mechanical entry rule alone. That selection is exactly what the four lenses
  and the LLM filter are meant to supply.

This is the central thesis of the whole architecture, now with its first data
point: **the deterministic pattern is the substrate; the edge (if any) is in
the filtering.** The next experiments test whether filtering can turn this
negative-expectancy base into a positive one.

## Experiment queue (next)

1. **Regime-matched data.** Backtest on 2024–2026 gold (the operator's actual
   trading era, with real strong downtrends). Their recent MT5 export is the
   right dataset. Short-only makes sense in a downtrend regime.
2. **Trend-strength filter.** Only short when the HTF downtrend is strong
   (e.g., MA slope steepness percentile, price well below MA). Cut the 1,132
   trades down to the high-conviction subset.
3. **Symmetric long+short.** Let the strategy also buy retracements into a
   *rising* MA, so it isn't fighting secular direction.
4. **LLM-filter A/B.** Run `ABBacktestHarness`: baseline (this) vs the same
   entries filtered by the brain's SITUATION REPORT. Does selection turn
   negative expectancy positive? This is the Path-A thesis test.
5. **Session filter.** Restrict to London/NY killzones (PO3 manipulation
   windows) — see `research/po3_mechanism.md`.

Each is logged as a trial (feeding the honest N into DSR).

## Follow-up: confluence mining + exit tuning (same session)

After the FAIL, we mined the 1,131 trades (`scripts/analyze_trades.py`) to find
what separates winners from losers, then built a confluence-filtered strategy
(`src/strategies/htf_retracement_short_filtered.py`) and swept exits.

**What the mining found (winner vs loser, empirical):**
- `below_ma200` (secular bearish regime): 35.8% win vs 32% baseline — the best
  single robust filter, keeps 685 trades (no overcut).
- `strong recent down` (ret20 < −100bp): 55% win, +0.66R — standout, but only
  38 trades (small sample).
- Deep retrace above the MA (dist > +20bp): **0% win** — a hard exclusion.
- NY-London overlap: 25.8% win (worst) — reversals fire in the "killzone".
- Winners entered ~22bp below the MA (trend intact); losers ~9bp (weakening).

**What killed the edge anyway — the decisive lesson:**
Win-rate math (32% vs 33.3% breakeven) was *misleadingly optimistic*. With real
next-bar-open fills, winners capture only **+1.22R of the 2R target** (the fill
gap compresses R; the target is too far for the pattern's follow-through). Real
breakeven needs **45%** win rate; the filtered strategy hit 35%.

**Exit sweep (below_ma200-filtered), mean R by target:**

| Target | Trades | Win% | Mean R |
|---|---|---|---|
| 0.50R | 995 | 44.7% | −0.30 |
| 0.75R | 854 | 53.9% | −0.27 |
| 1.00R | 758 | 50.8% | −0.26 |
| 1.50R | 629 | 42.0% | −0.23 |
| 2.00R | 565 | 35.4% | −0.22 |
| 3.00R | 475 | 26.9% | −0.20 |

**Mean R is negative at EVERY target.** No exit structure rescues the pattern
on this data. Since ~20 filter/target combinations were tried and all failed,
the negative conclusion is robust (no multiple-testing/overfit concern — you
can't overfit to a result that never turns positive).

## The real learnings (carry-forward)

1. **Regime dominates.** This is a net-UP decade (gold +26.8%); a short-only
   pattern is fighting the tide. The single most important next test is
   **regime-matched data** — the operator's 2024-2026 strong-downtrend era.
2. **The win-rate heuristic lies; use realized R.** Winners rarely reach a far
   target. Design targets/exits around actual follow-through, or use trailing
   stops. The clean-RR napkin math overstates edge every time.
3. **Momentum continuation is where signal lives** (ret20<−100 → 55% win). Sell
   strength-into-weakness continuation, not drift. Worth a dedicated momentum
   hypothesis on regime-matched data.
4. **A mechanical clone of a discretionary edge often fails** because the
   discretion (regime read, conviction, context) IS the edge. This is the
   Path-A thesis: the LLM filter must supply selection — but even it needs the
   right regime to select within.
5. **The methodology works.** We rigorously killed a pattern in an afternoon
   instead of with real money over months. That is the entire point.

## Provenance

- Data: https://github.com/ejtraderLabs/historical-data (XAUUSD H1)
- Strategy: `src/strategies/htf_retracement_short.py`
- Runner: `scripts/run_backtest.py --setup htf-retracement-continuation --timeframe 1h`
- Engine integrity: `research/engine_integrity.md`
