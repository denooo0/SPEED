# Operational Policies — Time, Drawdown, Idea Lifecycle, Variance

> All policies enforceable in code. References real ATLAS module paths.

---

## Weekly time budget

### Part-time (20h/wk) — bot runs 24/7, operator feeds and reviews

| Activity | Hrs | When | Why |
|---|---|---|---|
| Live observation | 3 | NY open Tue/Wed/Thu 8:30–9:30 ET | Highest info-density windows for XAU. You only need to *see* what autopsies later describe in detail. |
| Notion idea-intake | 2 | 30min/day Mon–Thu | Capture is cheap, must be daily-fresh or it decays. |
| Autopsy review | 4 | Sat 2h + Wed 2h | Sat = weekly digest reread; Wed = mid-week course correct. |
| Code / maintenance | 5 | Sun 3h + Wed 2h | Batch — context-switch is the killer for solo ops. |
| Backtests | 2 | Sun afternoon | Triggered by Sat digest's flagged hypotheses. |
| Rest (hard floor) | 4 | Fri night + Sun morning | Enforced by `src/meta/audit.py`: no `propose()` calls 22:00 Fri → 12:00 Sun. |
| **Total** | **20** | | |

### Full-time (50h/wk) — extra hours buy infrastructure, NOT screen-watching

| Activity | Hrs | Delta vs PT | Why |
|---|---|---|---|
| Live observation | 6 | +3 | Add Asia open Sun/Mon. |
| Notion intake | 4 | +2 | 1h/day — includes COT / calendar deep-reads. |
| Autopsy review | 8 | +4 | Daily 1h + weekly 3h. |
| Code / maintenance | 16 | +11 | Where the leverage is — backtest harness, meta-loop, dual-purpose plumbing. |
| Backtests | 8 | +6 | Run hypotheses to N ≥ 30 (see variance rule). |
| Rest (floor) | 8 | +4 | Full day off — non-negotiable; enforced by hook. |
| **Total** | **50** | | |

**Anti-pattern:** spending the extra hours staring at price. Past ~6h/week of screen time, expected value is negative — fatigue + overfitting to recent observations.

---

## Drawdown response matrix

Equity-curve peak tracked in `db_manager` (new `equity_peak` row). New function `position_manager.check_drawdown_gate(balance: float) -> DrawdownState` called from `main.TradingBot._cycle` **before** `open_from_signal`.

| DD% | Automatic action (module:function) | Operator action | Recovery condition | Override |
|---|---|---|---|---|
| **−5%** | `position_manager.check_drawdown_gate` → halves `RiskCalculator.risk_per_trade` (e.g. 2% → 1%) | Read last 10 autopsies; log Notion note | DD heals to −2% over 5 closed trades | Operator, logged via `meta/audit.py` |
| **−10%** | Same hook → flips `CycleGate.should_invoke` to TAKE-ONLY-A-grade (gate threshold +0.2); LLM calls suspended for non-A setups | Trigger `DigestBuilder.rebuild(last_n=30)`; write `plan/dd10_review.md` | DD heals to −5% AND 3 winning trades in last 5 | Operator + explicit Notion ticket |
| **−15%** | `Executor.execute_entry` short-circuits to paper-only regardless of config; meta-loop `MetaProposer.propose` auto-fires | 48h cool-off; **no code changes during cool-off** | DD heals to −8% AND meta-loop proposes ≥ 1 oracle-passed change | Operator only after 48h timer; audit-logged |
| **−20%** | Full halt: `main.TradingBot.run` sets `self._halted=True`; `position_manager.open_from_signal` raises `HaltedError`; open positions managed to exit only | Hard stop. Week off. Re-derive thesis from scratch in Notion before resuming. | Manual unhalt + signed Notion postmortem + `BacktestOracle.validate` green on ≥ 3 changes | **Nobody auto** — operator must run `scripts/unhalt.py --postmortem path/to/md` |

Hooks land in `position_manager` (size scaling), `gate` (quality bar), `executor` (paper-only force), `main` (halt). Single source of truth: `position_manager.check_drawdown_gate` returns a state enum consumed by all four.

---

## Idea lifecycle (Notion → live)

```
NOTION DB: "Hypotheses"
  capture → triage → backtest → deploy | shelf | archive
   daily    Sun/Wed  on-demand   gated    auto    auto
```

- **Capture:** any time, target 3–7 ideas/week. Notion fields: `hypothesis`, `mechanism`, `expected_edge`, `kill_condition`, `status=NEW`.
- **Triage:** Sundays 14:00 UTC. `scripts/notion_triage.py` (NEW) pulls `status=NEW`, calls `MetaProposer.propose` to convert to `ProposedChange`, marks `status=QUEUED` or `status=REJECTED_TRIAGE` with reason.
- **Backtest:** `status=QUEUED` ideas run through `BacktestOracle.validate` on Sun + Wed cron. Pass → `status=SHADOW` (paper-trading). Fail → `status=SHELVED`.
- **Deploy:** `SHADOW` runs ≥ 30 paper trades minimum (see variance rule). If real-time stats match backtest within CI → `HumanApprovalDeployer` flips to `LIVE`.
- **Stale prune:** `status=NEW` for > 14 days → auto-archived `STALE`. Operator must re-touch to revive.
- **Failed-hypothesis archive:** `SHELVED` ideas live in Notion's `Shelf` view forever with backtest artifact path under `memory/shelf/<id>.md`. Re-test allowed only when (a) new feature added or (b) > 90 days elapsed AND new regime tag. Both gates enforced in `MetaProposer.propose` (rejects re-submissions inside the window).

---

## Variance vs signal — when does "the system needs adjustment" beat "this is just noise"?

**Single trade loss = always variance.** N=1 has zero discriminative power; no action permitted.

**Rules codified in new `src/meta/variance_gate.py`:**

1. **Wilson 95% CI on win rate.** After N closed trades for a rule, compute Wilson lower bound on win-rate. Required N scales with expected edge: `N_min = ceil(4 / edge²)`, floor 30, cap 200.

2. **SPRT for online drift.** For *live* rules, run Wald sequential probability ratio test with H₀ = baseline-backtest WR, H₁ = baseline − 5pp. Boundaries A = ln(0.95/0.05) ≈ 2.94, B = ln(0.05/0.95) ≈ −2.94. Cross lower → auto-shelf. Cross upper → keep. In between → keep observing.

3. **Backtest single failure:** require 3 independent failures across non-overlapping windows (`BacktestOracle.validate` with `n_splits=3`) before `MetaProposer` is permitted to retire the rule. One bad window = variance.

4. **Hook:** `position_manager._fire_close_hook` appends to per-rule rolling stats table; `main._cycle` calls `variance_gate.evaluate_all_rules()` once per day; rules failing SPRT lower flip to `SHADOW` (not deleted — observable).

**The asymmetry: cheap to demote (paper-mode), expensive to delete.** Default action on ambiguity = collect more data, NOT change code.

---

## New / touched files

- `/home/user/SPEED/src/risk/position_manager.py` — add `check_drawdown_gate`
- `/home/user/SPEED/src/signals/gate.py` — DD-aware threshold
- `/home/user/SPEED/src/execution/executor.py` — paper-only force at −15%
- `/home/user/SPEED/main.py` — halt flag, daily variance evaluation
- `/home/user/SPEED/src/meta/variance_gate.py` — **NEW module**
- `/home/user/SPEED/scripts/notion_triage.py` — **NEW script**
- `/home/user/SPEED/scripts/unhalt.py` — **NEW script**
- `/home/user/SPEED/src/meta/audit.py` — rest-window enforcement, override logging
