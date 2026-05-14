# v9 — Generational Wealth Plan

> **Goal:** maximize probability of generational wealth — short term AND long term.
> **Not the goal:** speed-to-$50k. The 100x/30d target was reframed in this turn.
> **Locked decisions:** Path A (LLM-as-filter), free data only, paper-first, Notion idea intake, $500 starting capital, XAUUSD on Bybit perp.

---

## I. The honest probabilistic frame

What "generational wealth" actually requires:

- A real edge that compounds (not a fluke streak)
- Position sizing that survives variance (fractional Kelly, not all-in)
- Capital persistence through drawdowns (no ruin)
- Time horizon long enough for compounding to dominate noise (years, not weeks)
- Continuous learning that keeps the edge fresh as markets adapt

The probability multipliers we control:

| Lever | Effect |
|---|---|
| Prove edge in backtest before trading real money | 10× — without this, we're gambling |
| Fractional Kelly sizing | 5× — survives variance that ruins fixed-% sizing |
| Hard drawdown halt at -20% | 3× — capped downside per cycle |
| Continuous autopsy + digest learning | 2× — strategy adapts as markets do |
| Meta-loop hypothesis evolution | 1.5–3× — compounds discovered edges |
| Multi-instrument once XAUUSD proves out | 2–4× — diversifies regime exposure |

Stacking these gives realistic compound growth that *can* reach generational scale over a 5–15 year horizon, NOT 30 days.

---

## II. Phased plan — short term to long term

### Phase 0 — Edge Discovery (Weeks 1–8)

**Goal:** discover whether the four-lens spike-low-recovery setup actually has edge on XAUUSD, with realistic costs.

**No real money. No paper trading. Backtest only.**

Track A (parallel): build the data acquisition pipeline. Bybit 2015+ candles, CFTC COT archive, FRED macro, scraped sentiment.

Track B (parallel): build the backtest harness with realistic execution model (next-bar-open fills, 11bp Bybit cost, 1.5s latency, funding drag).

Track C (parallel): refactor four-lens feature extractors for backtest replay. Cache historical computations.

Track D (depends on B+C): wire the LLM brain into backtest as filter (Path A). A/B harness comparing with-LLM vs deterministic-only on identical setups.

Track E (depends on D): scaffold the meta-loop's `BacktestOracle.validate` — the oracle becomes the truth-teller.

Track F (parallel): observability — every backtest run is a logged scientific experiment with hypothesis registry.

**Promotion gate to Phase 1:**
- PBO < 0.20 on 12-fold CPCV
- DSR > 0.95
- Walk-forward efficiency ≥ 0.5 on ≥ 70% of folds
- Min trades aggregated across OOS ≥ 200
- Realistic-cost backtest still profitable after 11bp + slippage + funding

**If gate fails:** the strategy doesn't have edge. We do NOT proceed to Phase 1. We either pivot setups, pivot instruments, or stop. This is the brutal honesty step.

### Phase 1 — Paper Trading on Real Market (Weeks 9–16)

**Goal:** verify the backtest's edge survives in real-time.

- Live data feed (Bybit WebSocket already wired)
- LLM-as-filter pipeline running
- Notion idea intake operational; operator feeds new hypotheses weekly
- $0 real capital. Paper only.

**What we track:**
- Live trade distribution vs backtest distribution (Wilson 95% CI overlap)
- Real slippage, real spread, real fill latency — does our cost model match reality?
- LLM filter quality: does the LLM's TAKE/SKIP/NO_TRADE decision actually correlate with outcomes?
- Idea-intake throughput: does the operator generate 3–7 ideas/week?

**Promotion gate to Phase 2:**
- ≥ 50 paper trades closed (per setup, per regime)
- Live Sharpe within 30% of backtest Sharpe
- LLM filter contributes positive alpha vs rules-only baseline (statistically significant at p < 0.05)
- ≥ 2 setups graduated from Notion intake to live filter
- No `validate_take` chokepoint violations
- No anti-runaway firewall trips

**If gate fails:** back to Phase 0 for re-engineering, OR halt entirely if no path forward.

### Phase 2 — Live Trading on $500 (Weeks 17–24)

**Goal:** prove the system makes money with real (small) capital. Real fills, real fees, real drawdown psychology.

- Bybit live, $500 of go-broke money
- Fractional Kelly sizing (0.25 of full Kelly), capped at 2% per trade
- Hard drawdown halt at -20% per the operational policies
- Telegram alerts on every trade + daily P&L summary

**What success looks like:**
- 30+ live trades closed
- Net positive after 90 days
- Drawdown profile within modeled 1σ envelope
- No anti-runaway firewall trips
- At least one Notion-originated hypothesis promoted to LIVE

**What we accept:**
- Net positive but smaller than backtest implied (the gap teaches us about cost realism)
- Drawdowns up to -15% (well within tolerance)
- Some hypotheses shelved (good — proves the gate works)

### Phase 3 — Prop Firm Challenge (Months 7–12)

**Goal:** scale the system using prop-firm capital. Doctrine maps cleanly onto prop firm rules (FTMO daily-loss cap, trailing drawdown).

- Pass a $100k challenge (~$500 fee). The system was engineered to their constraints.
- Live the funded account. 80–90% profit split.
- Realistic monthly P&L target: $3–8k/month per $100k funded.
- Run multiple challenges in parallel once the first passes — diversification across firms.

### Phase 4 — Audited Track Record + Subscription / Education (Months 9–18)

**Goal:** monetize the proven system three ways, all gated on Phase 2/3 success.

- Public track on X — sanitized SITUATION REPORTS with the existing `scripts/post_to_x.py` helper.
- Subscription channel — capped at 100–300 subs to protect edge from front-running.
- Education cohort — sell the methodology (four-lens + autopsy doctrine), not the live signals.

This is monetization, not capital growth. The trading is the engine; this is the second engine.

### Phase 5 — External Allocation (Months 18–36)

**Goal:** real generational pace.

- Audited prop record + verified XAUUSD performance + public methodology + audience = legitimacy.
- Pitch family offices, small funds, HNW individuals as sub-advisor.
- 2/20 or 0/30 on $1–10M AUM.
- Multi-agent ATLAS becomes the durable moat — competitors can copy the prompts; they can't copy the autopsy memory across thousands of cycles.

This is the seven-figure outcome.

### Phase 6 — Path B Research Arm (Months 24+)

**Goal:** if Path A is producing cashflow, fund Path B as research. NOT before.

- Build the LLM-as-strategist that beats the Alpha Arena baseline.
- See `plan/qwen_integration.md` for Path B requirements (fine-tuning, RLHF on realized P&L, multi-model consensus, etc.).
- Funded by Path A profits, not external capital.

---

## III. The dual-purpose feedback loop (always-on)

While the phases progress, two loops run continuously:

**Intra-loop (every cycle):** brain → SITUATION REPORT → trade → autopsy → digest → brain (next cycle reads new digest).

**Cross-loop (weekly):** autopsy clusters + exec_intel + gate-decision distributions → MetaProposer → BacktestOracle → AdversarialCritic → operator approval → git commit.

See `plan/dual_purpose_architecture.md` for the full design.

---

## IV. Where the operator's "ideas noticed in markets" land

Notion page: **ATLAS XAUUSD — Idea Intake** (live, planted under "ATLAS Strategy Injection — Elite Strategies & Opus Integration Prompt").

- Operator captures observations using the template
- `scripts/notion_triage.py` pulls Sundays 14:00 UTC
- Each observation → hypothesis forge → backtest oracle → SHELVED or SHADOW
- Operator approves SHADOW → LIVE promotions

See `plan/edge_generation_workflow.md` for the full design.

---

## V. The operational discipline

See `plan/operational_policies.md` for:

- 20h/wk and 50h/wk time budgets
- Drawdown response matrix (5% / 10% / 15% / 20% triggers)
- Idea lifecycle cadence
- Variance vs signal thresholds (Wilson CI + SPRT)

---

## VI. Path B — documented for future

Locked decision: Path A primary, Path B parked. See `plan/qwen_integration.md` for what it would take to make LLM-as-strategist beat the Alpha Arena baseline.

When Phase 3 produces consistent cashflow, Path B research can begin — funded by Path A profits, not pre-investment.

---

## VII. Pre-split parallel execution graph for Phase 0

Each track is independently agent-executable. Total wall-clock with parallelism: ~7–10 days for Phase 0.

```
TRACK A — Data Acquisition                        [agent: data-eng]
  - Bybit M5/M15/M30/H1 from 2015 (paginated through API limits)
  - CFTC COT archive (weekly)
  - FRED macro indicators (DXY, US10Y, VIX, oil)
  - News + sentiment (RSS + LLM-scoring)
  - Storage: parquet, validated, idempotent
       │
       ▼
TRACK B — Backtest Harness                        [agent: quant-eng]
  - Event-driven simulator (next-bar-open fills)
  - Realistic cost model (11bp Bybit + spread + slippage + funding + 1.5s latency)
  - Walk-forward + CPCV runners
  - Metrics: Sharpe, DSR, PBO, max DD, ruin probability
       │
       ▼
TRACK C — Feature Pipeline (replay-mode)          [agent: features-eng]   ⟶ parallel to B
  - Refactor flow/structure/context/intent for backtest replay
  - Add: order flow stats, volatility regime, correlation snapshot
  - Cache historical feature computations
       │
       └───┬───────────────┐
           ▼               ▼
TRACK D — LLM-as-Filter Integration               [agent: integration]
  - Wire brain into backtest as filter (Path A)
  - A/B harness: with-LLM vs deterministic baseline
  - Cost telemetry per backtest run
       │
       ▼
TRACK E — Meta-Loop Backtest-Oracle Implementation [agent: meta-eng]
  - Fill BacktestOracle.validate stub (was NotImplementedError)
  - Wire to harness from Track B
  - Audit log writer for meta decisions
       │
       ▼
TRACK F — Observability + Experiment Logging      [agent: obs-eng]   ⟶ parallel to all
  - Every backtest run = logged scientific experiment
  - Hypothesis registry + parameter-sweep dashboard
  - Live-vs-backtest drift detector
```

**Critical path:** A → B → D → E. Tracks C and F run in parallel. ~7–10 days wall-clock.

---

## VIII. The honest summary

- **30 days:** finish Phase 0. Know whether edge exists.
- **8 weeks:** Phase 0 + Phase 1. Paper-trading live.
- **16 weeks:** Phase 2. Real $500 in play.
- **6 months:** First prop firm passed (if Phase 2 worked).
- **12 months:** Multiple funded accounts + subscription revenue.
- **18 months:** External allocation conversation.
- **36 months:** Generational pace if every phase compounded.

Every phase has an honest abort criterion. The path is engineered to maximize the *probability* of reaching the generational outcome, not to minimize time. The patient version is the only version that has a realistic shot.

---

## IX. What this turn delivered

- This file: master v9 plan.
- `plan/edge_generation_workflow.md` — Notion → hypothesis → backtest → live pipeline design.
- `plan/operational_policies.md` — time budget, drawdown response, idea lifecycle, variance gates.
- `plan/dual_purpose_architecture.md` — the intra-loop + cross-loop feedback architecture.
- Notion: `ATLAS XAUUSD — Idea Intake` page live with template + workflow.
- The pre-split parallel execution graph for Phase 0.

**Next concrete move:** kick off Tracks A + B + C in parallel for Phase 0 (data + backtest + features). That's what Phase 0 needs to begin, and all three are agent-executable in parallel.
