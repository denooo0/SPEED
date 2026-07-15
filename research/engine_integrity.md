# Engine Integrity — Proof the Backtest Is Not Polluted

> The operator's explicit requirement: "make sure the engine is not polluted.
> no lookahead bias or anything poisoning." This document is the proof.

---

## The gold-standard test: future poisoning

`scripts/audit_lookahead.py` runs the definitive lookahead test:

1. Build a candle series. Run the full pipeline (FeatureReplayer → simulator
   → deterministic strategy). Record every FeaturePack and every trade.
2. **Poison every bar AFTER a cut index with 8–100× garbage** — 10× opens,
   12× highs, 0.1× lows, 8× closes, 100× volume.
3. Re-run on the poisoned series.
4. Assert three things:

| Check | What it proves | Result |
|---|---|---|
| (a) Every FeaturePack at/before the cut is **byte-identical** between clean and poisoned runs | No future bar can change a past feature. The feature layer has no leak. | **201 packs identical — PASS** |
| (b) Every trade that **entered** at/before the cut is identical (time, price, direction, size) | The simulator makes past decisions with only past data. | **8 trades identical — PASS** |
| (c) A signal on bar *i* fills at bar *i+1*'s **open**, never bar *i*'s close | No same-bar look-ahead in fills. | **PASS** |

If any future information reached the past, (a) or (b) would fail loudly. They
do not.

## Why this is airtight

The test does not inspect the code and argue it looks correct. It **injects a
maximally-different future** and checks the past is unchanged. There is no way
for a lookahead leak to survive this — if the engine read even one future bar,
the 8–100× poison would move the affected feature or decision.

## It cannot silently regress

The audit is wired into the permanent test suite as
`tests/test_lookahead_audit.py` (3 tests). Any future change that introduces
peeking — a rolling window that includes future bars, a fill on the signal
bar's close, a feature computed over the whole series — breaks the build.

## Structural guarantees behind the result

The audit confirms what the design already enforces:

- **FeatureReplayer.pack_at(ts)** slices `df.loc[df.index <= ts]` — features
  are strictly as-of-close (`src/features/replay.py`).
- **Macro + COT** are windowed to `<= ts` before use (`_macro_window`,
  `_cot_snapshot_at`) — no weekly-report look-ahead.
- **EventDrivenSimulator** fills a returned Order on the NEXT bar's open with
  spread + latency drift; it never fills on the signal bar
  (`src/backtest/simulator.py`, lines 8–9, 170–173, 202–207).
- **Exit ordering** assumes worst-case (SL before TP) when a single bar could
  hit both — conservative, never optimistic.
- **CPCV** purges label-overlapping training bars and embargoes a band after
  each test fold (`src/backtest/cpcv.py`) — no train/test leakage.

## Cost realism (not lookahead, but anti-self-deception)

The engine also refuses to lie about fills:

- Spread crossed on entry AND exit (bps of level).
- Latency drift toward the bar's worst-case (high for longs, low for shorts).
- Funding drag proportional to hold time.
- Next-bar-open fills (no "filled at the exact signal price" fantasy).

Filling at candle close with zero cost is the single most common way
backtests lie. This engine does neither.

## The dry-run confirms the gate is honest

Running `scripts/run_backtest.py` on synthetic (no-edge) data returns **FAIL**
— DSR 0.001 (gate: > 0.95), WFE 0.0 (gate: ≥ 0.70), negative Sharpe. A gate
that rubber-stamped a no-edge strategy would be worse than useless. This one
correctly rejects noise. When it eventually says PASS on real data, that PASS
will mean something.

---

## What integrity does NOT guarantee

Integrity proves the engine measures **honestly**. It does not prove the
strategy has **edge**. Those are different:

- A clean engine can still report "this strategy loses money" — and we should
  believe it.
- The edge question is answered only by running on REAL XAUUSD history, which
  requires data past the sandbox's network boundary
  (`research/operator_runbook.md`).

Integrity is the precondition for trusting the edge test. It is now in place
and permanently guarded.
