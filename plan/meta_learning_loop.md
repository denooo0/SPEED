# META-LEARNING LOOP — ATLAS v8.5

> Status: design, not deployed. Skeleton only.
> Pre-requisite: keep v8 architecture running while the loop scaffolds.

---

## I. Honest framing of "exponentially rapid evolution"

The user asked for a meta-learning loop that accelerates evolution
exponentially rapidly. The honest read of what's possible:

**True exponential improvement requires compounded edge, not just iteration
speed.** The Alpha Arena evidence (Oct–Nov 2025, frontier LLMs lost
30–63% trading crypto perps) means an unconstrained meta-loop that lets
the LLM modify its own decision-making prompts will **exponentially
rapidly drift toward worse decisions**. Goodhart's law, hallucination,
recency bias, and recursive self-improvement instability all compound
against us.

What "exponentially rapid" CAN honestly mean:

1. **Hypothesis generation speed.** The LLM proposes prompt / threshold /
   rule modifications in seconds. We can generate dozens of hypotheses per
   day.
2. **Backtest-validated iteration.** Each hypothesis is evaluated against
   held-out data before deployment. The backtest is the bottleneck — but
   if we run a backtest in seconds, we can validate many hypotheses per
   day.
3. **Compounding only kicks in if the strategy has edge.** The whole
   architecture has zero growth multiplier without a real edge to compound.
   The backtest harness is therefore the most important component — it's
   what tells us whether anything is real.

**Hard prerequisites before the meta-loop earns the right to evolve
anything:**

- Backtest harness exists, with realistic execution model (next-bar-open
  fills, 11bp Bybit cost, 1.5s latency, funding drag)
- CPCV + DSR + PBO validation pipeline exists
- v8 has produced ≥ 200 real trades (paper or live) so the autopsy
  database has enough signal to cluster

Until those three are in place, the meta-loop has no oracle, no truth,
nothing to optimize against. It would just amplify noise.

---

## II. Architecture

```
                  ┌─ TRADES CLOSE (paper or live) ─┐
                  │                                  │
                  ▼                                  │
        AUTOPSIES (markdown w/ frontmatter)         │
                  │                                  │
                  ▼                                  │
        DIGEST + PATTERN CLUSTERING                 │
        (rolling window, runs every N closes)       │
                  │                                  │
                  ▼                                  │
        META-PROPOSER (Qwen3 Max / Qwen3.6 / Claude)│
        Reads:                                       │
          - digest                                   │
          - clustered autopsies                      │
          - current mandate/addendum                 │
          - current gate config                      │
          - current validate_take rules              │
        Emits: ProposedChange[] (JSON)               │
                  │                                  │
                  ▼                                  │
        BACKTEST ORACLE                              │
        Replays proposed changes against held-out    │
        historical data. Computes:                   │
          - Sharpe, win rate, R-distribution         │
          - DSR, PBO (vs baseline trial count)       │
          - Max DD, ruin probability                 │
                  │                                  │
                  ▼                                  │
        ADVERSARIAL CRITIC (separate LLM)           │
        Tries to break the proposed change:          │
          - "Under what regime does this fail?"      │
          - "What's the worst-case overfitting?"     │
          - "What invariant does this break?"        │
                  │                                  │
                  ▼                                  │
        HUMAN APPROVAL GATE                          │
        Operator sees:                               │
          - Proposed change diff                     │
          - Backtest result vs baseline              │
          - Critic's failure analysis                │
          - Reversibility (git diff)                 │
        Approves / rejects / modifies.               │
                  │                                  │
                  ▼                                  │
        DEPLOYMENT (versioned via git)               │
        Every change is a commit. Every revert is    │
        a single command. All meta-decisions logged  │
        with full context.                           │
                  │                                  │
                  ▼ (audit trail feeds back)         │
                  └──────────────────────────────────┘
```

**Frequency:** weekly batch run, or after every 50 closed trades —
whichever comes first. NOT after every trade (recency bias kills you).

**Cost ceiling:** meta-loop has a monthly LLM budget cap. If it's
spending more than 20% of the brain's monthly budget, something is
wrong.

---

## III. Meta-targets, by safety tier

### Tier A — deploy first (lowest blast radius)

**A1. Confidence calibration.** Track LLM-emitted confidence vs realized
outcomes. If 0.8-confidence trades win 50% and 0.4-confidence win 60%,
the brain is inverted. Fix: post-hoc Platt scaling. Apply a learned
sigmoid to confidence scores before they feed `validate_take`.
- Touches: a calibration table loaded by the converter.
- Reversibility: delete the table to revert.

**A2. Gate threshold tuning.** Currently `volume_spike` fires on >2x
20-bar average. Backtest 1.5x, 2x, 2.5x, 3x against historical data and
pick the threshold with best forward-period Sharpe.
- Touches: `config.yaml` GATE section, optional per-instrument override.
- Reversibility: restore prior config value.

**A3. Cooldown tuning.** Currently 300s. Test 60s, 300s, 900s, 1800s.
- Touches: `GateConfig.cooldown_seconds`.
- Reversibility: restore prior value.

### Tier B — deploy after Tier A proves the loop works

**B1. Lens weighting.** Track which lens's high-confidence contributions
correlate with winning trades. If `structure` predicts but `intent`
doesn't on XAUUSD, increase `validate_take`'s structure weight; cap
intent at lower contribution.
- Touches: validate_take's confidence math.
- Reversibility: revert to equal weights.

**B2. Setup taxonomy discovery.** Cluster autopsies (frontmatter tags +
embedding similarity). When ≥5 autopsies cluster on a feature signature
not covered by an existing `memory/setups/*.md` file, the meta-proposer
drafts a new setup file. Operator approves before it enters the brain's
relevant-setups context.
- Touches: new `memory/setups/<name>.md` files.
- Reversibility: delete the file.

### Tier C — deploy only after Tier B is stable for 60+ days

**C1. Validate_take rule mining.** Discover new doctrine clauses from
loss patterns. Example: "if session=ny-overlap AND last_bos=bear AND
volume_spike, reject" emerges from 8 of 10 such trades losing. Operator
approves the new rule.
- Touches: `validate_take()` in `llm_signal_generator.py` — additional
  rejection clauses.
- Reversibility: comment-out the clause; commits remain in git history.

**C2. Mandate addendum evolution.** The proposer suggests additions to
the addendum (new vocabulary, new regime descriptions, new kill-thesis
templates). Approved additions go into `prompts/ATLAS_ADDENDUM.md`.
- Touches: cached system prompt. Invalidates the prompt cache once.
- Reversibility: git revert.

### Tier D — FORBIDDEN

**D1. The meta-loop modifying its OWN system prompt.** Recursive self-
improvement is the fastest path to broken. The meta-proposer's system
prompt is frozen at deployment and only the operator can edit it
manually. This is the only structural firewall against runaway evolution.

**D2. The meta-loop modifying the backtest oracle's logic.** If the
oracle is what tells us truth, the oracle must not be mutable by the
thing it judges.

**D3. Automatic deployment without human approval.** Every change passes
the approval gate. No exceptions, ever.

---

## IV. Safety rails (in code, not in policy)

1. **Backtest-validation gate.** A `ProposedChange` cannot deploy without
   passing `BacktestOracle.validate(change)` against a held-out window.
   Pass criterion: change improves Sharpe by ≥ 0.1 AND PBO does not rise
   AND ruin probability does not rise.

2. **Adversarial critic gate.** A second LLM call argues against the
   proposed change. The critic's transcript is shown to the operator.
   If the critic identifies a structural objection the proposer can't
   answer, the change is blocked.

3. **Rate limit.** Max 3 deployed changes per week per tier. Max 1 Tier-C
   change per month. Slows the loop deliberately.

4. **Adversarial market regimes.** Before any change is approved for
   live, the backtest oracle replays the change against three regime
   archives: trending bull, trending bear, sideways/choppy. A change
   that fails in any regime is blocked.

5. **Reversibility-by-default.** Every deployed change is a single git
   commit. `git revert <sha>` is the rollback. The deployment script
   refuses to deploy a change that doesn't produce a clean diff.

6. **Frozen audit trail.** Every proposer call, every oracle result,
   every critic verdict, every operator decision is logged immutably
   to `meta/audit/<iso-timestamp>-<change-id>.json`.

7. **Cost circuit breaker.** Monthly meta-loop LLM spend cap. Tripped =
   loop halts; operator manually reviews and re-enables.

---

## V. Phased rollout (5 phases, gated)

### Phase M-0 — Prerequisites (must complete before any meta work)
- Backtest harness with realistic execution model
- CPCV + DSR + PBO validation pipeline
- ≥ 200 real autopsies in `memory/autopsies/`
- Confidence-vs-outcome data collected

**Gate to M-1:** all four prereqs ticked.

### Phase M-1 — Tier-A meta-loop only (confidence calibration + gate tuning)
- 30-day operation
- ≤ 3 changes deployed
- Track: did meta-loop's changes improve forward-Sharpe vs no-change control?

**Gate to M-2:** demonstrably improved forward-Sharpe with statistical
significance (or honestly conclude the loop adds no value and stop).

### Phase M-2 — Tier-B (lens weighting + setup taxonomy)
- 60-day operation
- ≤ 6 changes deployed
- Track: setup file additions actually used by the brain in real
  thesis production

**Gate to M-3:** Tier-B changes contributed net positive across
deployed instruments.

### Phase M-3 — Tier-C (rule mining + mandate evolution)
- 90-day operation
- ≤ 3 changes deployed
- Operator scrutiny: every Tier-C change reviewed twice (proposal and
  90-day post-deployment).

**Gate to M-4:** the system has demonstrably learned from its mistakes
in a way that out-of-sample data confirms.

### Phase M-4 — Sustained operation
- The loop is live. Changes deploy weekly under all safety rails.
- Once a quarter, operator runs a "meta-meta" review: is the loop itself
  adding value, or has it become noise?

---

## VI. Why this won't be "exponentially rapid" (the honest part)

A meta-loop is not a free multiplier. It's a way to systematize
hypothesis generation and testing. Real improvements happen at the
speed of:

- How fast can the backtest run? (seconds, if implemented well)
- How fast can the operator approve? (minutes, but they're a human and
  shouldn't be the bottleneck)
- How fast does new market data arrive? (one bar at a time — minutes to
  hours)
- How fast do trades resolve so we know if a change helped? (days to
  weeks for the autopsy database to update)

Real-world cadence: 1–3 deployed changes per week, each one a small
delta. Compounded over a year, that's 50–150 measured improvements.
That's not exponential — but it IS more than zero, which is what most
"set and forget" systems deliver.

**The exponential is the COMPOUNDED EDGE, not the iteration speed.** If
each change adds 5bp of edge and we deploy 50/year, that's 2.5% of
edge added in a year. If the strategy compounds at that rate above
baseline, you'd be looking at a 30%+ improvement in 5 years.

Without proven baseline edge, none of this matters.

---

## VII. What this turn delivers

- This plan file: `plan/meta_learning_loop.md`
- Skeleton modules in `src/meta/` (no full implementation — design only):
  - `proposer.py` — interface for the meta-proposer
  - `oracle.py` — interface to the backtest oracle (currently stub)
  - `critic.py` — adversarial critic interface
  - `deployer.py` — human-approval + git-commit deployment
  - `audit.py` — immutable audit log writer
- `plan/qwen_integration.md` — three integration paths (Qwen3 Max API,
  Qwen3.6 local via Ollama, Qwen3.6 via OpenRouter)
- `scripts/install_qwen.sh` — operator helper for the local install path

**Not in this turn:**
- Full implementations (no autopsy clustering, no backtest oracle, no
  live proposer LLM calls)
- The prerequisite v9 backtest harness (separate work)
- Any deployment automation

The meta-loop CANNOT do real work until the v9 backtest harness exists.
This plan locks the architecture so when the backtest is ready, the
loop slots in without surprises.

---

## VIII. Lessons from v8 baked into v9 + meta-loop

| v8 mistake | v9 / meta fix |
|---|---|
| No backtest before engineering | Prereq M-0; nothing meta runs without it |
| 60-trade promotion gate is theatre | DSR > 0.95 + PBO < 0.20 + N ≥ 200 |
| LLM-as-strategist unproven | Meta-loop only modifies, never replaces, the deterministic gate |
| Recursive self-improvement risk | Tier D forbidden — meta-LLM's own prompt is frozen |
| Position size = fixed 2% | Tier-A target: fractional Kelly calibration |
| Backtest at candle close | Already named in v9 — next-bar-open + 11bp cost + 1.5s latency |
| Win rate as headline metric | DSR + PBO + R-distribution replace it |
| No regime testing | Safety rail 4: changes evaluated across 3 regime archives |
| Single autopsy = signal | Cluster-of-5 minimum before any pattern triggers a proposal |
