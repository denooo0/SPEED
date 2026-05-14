# Dual-Purpose Feedback Architecture

> The system both **trades** (to extract intel from real markets)
> and **adjusts** itself based on intel extracted (continuous evolution).
> This is the closed feedback loop.

---

## Architecture diagram

```
                   LIVE TRADING PATH                                META-LOOP PATH
                   ----------------                                 --------------
candles + ws + cot
       │
       ▼
LLMSignalGenerator.build_feature_pack()
       │
       ▼
CycleGate.should_invoke()  --(news/cooldown/no-trigger)--> SKIP (log gate_decision)
       │  fires
       ▼
AtlasBrain.analyze() ─► SituationReport
       │
       ▼
validate_take(sr, has_open_position) ── rejection ──► db.signals(fired=0) + reject_reason
       │  None
       ▼
situation_report_to_trade_signal()
       │
       ▼
Executor.execute_entry()  ─► db.signals(fired=1)
       │  drift check (max_drift_pips)  ─► exec_intel.csv  (slippage, drift, latency)
       ▼
PositionManager.open_from_signal()  ─► db.positions + db.trades(ENTRY)
       │
       ▼
PositionManager.evaluate()  (each tick)
       │  ─► db.trades(SCALE_OUT_TPx | EXIT)
       │  per fill: exec_intel.csv (slippage, spread, fill_ms)
       │
       ▼  on full close
_fire_close_hook()  ─►  AutopsyWriter.from_closed_position()
       │
       ▼
memory/autopsies/<id>.md   (frontmatter + body)
       │
       ▼
AutopsyEnricher (Sonnet 4.6)  ─► fills "What actually happened" + "One-line lesson"
       │
       ▼
DigestBuilder.rebuild(last_n=30)  ─►  memory/digest.md  ───────►  read by AtlasBrain  (INTRA-LOOP)
                                                                       │
                                                                       ▼ (CROSS-LOOP)
   exec_intel.csv + digest.md + autopsy cluster + current config/addendum
                                                                       │
                                                                       ▼
                                                       MetaProposer.propose()  [weekly cron]
                                                                       │
                                                                       ▼
                                                       BacktestOracle.validate()  (shadow harness)
                                                                       │
                                                                       ▼
                                                       AdversarialCritic.critique()
                                                                       │
                                                                       ▼
                                                       HumanApprovalDeployer.present()
                                                                       │
                                                                       ▼
                                                              OPERATOR (Telegram)
                                                                       │
                                                                       ▼
                                                          git commit (rollback = git revert)
```

The autopsy / digest stream is the **intra-loop** — the brain self-corrects via prompt context next cycle. The exec_intel + autopsy cluster stream is the **cross-loop** — structural changes via meta.

---

## Intel extracted per trade (what backtests cannot see)

Captured at specific call sites in the live trading path:

| Signal | Source | Why backtest can't see it |
|---|---|---|
| `requested_price` vs `actual_fill_price` (slippage bps) | `Executor.execute_entry` after `connector.fetch_ticker` | Backtest uses candle close, no real LP |
| Decision-to-fill latency | timestamp delta between `sr.now_ms` and `db.log_trade_event ENTRY` | Backtest is instantaneous |
| Bid/ask spread at entry/exit | `ticker['bid']/['ask']` snapshot in `Executor` and `position_mgr.evaluate` | Historical L1 reconstruction is lossy |
| Spread under news | spread snapshot × `CalendarFeed.is_in_news_window` boolean | Sim doesn't model spread blowouts |
| TP1 sweep wick depth | `current_price` vs `tp1` at moment `(current_price − tp_price) × tp_sign ≥ 0` in `evaluate()` | Candle OHLC hides intra-bar wicks |
| Partial-fill ratio | `close_size` vs requested in `evaluate()` | Backtest assumes full fills |
| Gate-fired-but-rejected rate | `CycleGate.should_invoke` + `validate_take` outcomes | Backtest has different distribution |
| WS staleness at decision | `ws_snapshot.age_ms` inside `build_feature_pack` | Backtest data is always "fresh" |
| Real funding/OI tick at fill | `lens_intent` snapshot persisted alongside fill | Reconstructable but rarely matches live |
| `kill_thesis` invalidation lag | time between SR's `kill_thesis` condition becoming true and SL firing | Requires live tick stream |

**Implementation:** new `src/execution/exec_intel.py` writing one row per fill event to `db.exec_intel` (new table) and a mirror CSV at `memory/exec_intel.csv`. Call sites: `Executor.execute_entry` after fill, `PositionManager.evaluate` immediately after each `log_trade_event`.

---

## Feedback flow table

| Intel signal | Destination | Cadence | Triggers |
|---|---|---|---|
| ENTRY / EXIT events | `db.trades` (already wired) | per fill | `_fire_close_hook` → `AutopsyWriter` |
| Full close | `memory/autopsies/<id>.md` | on close | `AutopsyEnricher` (async) |
| Autopsies | `memory/digest.md` via `DigestBuilder.rebuild` | on close + daily cron | Read by `AtlasBrain.analyze` (INTRA-LOOP self-correction) |
| Slippage / spread / latency | `db.exec_intel` + `memory/exec_intel.csv` | per fill | weekly meta-loop cron |
| Gate rejections | `db.signals` + structured `gate_decisions` log | per cycle | weekly meta-loop cron |
| `validate_take` rejections | `db.signals(fired=0, reason=...)` | per SR | weekly cron — clusters of same rejection reason = candidate rule |
| Autopsy cluster (recurring losses by tag) | `DigestBuilder` "Patterns currently bleeding" | weekly | `MetaProposer.propose(digest_md, autopsy_cluster, current_config, current_addendum)` |
| `ProposedChange` | `meta/proposed/<change_id>.json` | from proposer | `BacktestOracle.validate` then `AdversarialCritic.critique` |
| `DeploymentPackage` | Telegram brief via `HumanApprovalDeployer.present` | post-critic | operator decision |
| Approval | single git commit | manual | live config swap on next process restart |

---

## Approval matrix

| Adjustment | Mode | Rationale |
|---|---|---|
| Autopsy write + enrichment | **automatic** | Pure observation, no behavior change |
| Digest rebuild | **automatic** | Influences brain only as context; brain still must obey `validate_take` |
| Confidence calibration table (Platt scaling) refit | **automatic, shadow-applied** | Reweighs `cb_sum` mapping; cannot loosen `validate_take` thresholds |
| `GateConfig` cooldown / trigger toggles (Tier A) | **approval-required** via deployer | Changes invocation distribution |
| New `setup_file` in `memory/setups/` (Tier A) | **approval-required** | New memory item the brain quotes |
| `ATLAS_ADDENDUM.md` clauses (Tier B) | **approval-required + 2-week shadow** | Layered on frozen mandate |
| `validate_take` rule additions (Tier B) | **approval-required + 2-week shadow** | Touches the chokepoint |
| `MIN_RR_MULTIPLE`, `MAX_CONFIDENCE`, `MIN_NONZERO_LENSES` (Tier C) | **manual-only** | Doctrine; meta cannot propose |
| `ATLAS_MANDATE.md` Laws 1–12 | **manual-only, frozen** | Tier D forbidden |
| Position sizing / `RiskCalculator` formula | **manual-only** | Sizing is sacred |
| `AdversarialCritic` / `MetaProposer` prompts | **manual-only, frozen** | Meta cannot modify itself |
| Kill-switch flips | **manual-only** | Operator authority |

---

## Variance vs signal triggers

All computed in `DigestBuilder` extension (new `stat_tests.py`):

1. **Minimum sample:** no proposal until `wins + losses ≥ 30` since last deploy.
2. **Per-tag bleed:** tag must have `n ≥ 8` AND `sum(R) ≤ -3.0R` AND one-sided binomial p < 0.05 vs 40% baseline win rate.
3. **Slippage drift:** rolling 20-trade mean slippage > backtested assumption by > 2σ (Welch's t-test on `exec_intel.slippage_bps`).
4. **Win-rate collapse:** rolling 20 vs prior 50 win-rates differ by > 15pp AND χ² p < 0.05.
5. **Expectancy halving:** rolling 20-trade avg R below `baseline_avg_r / 2` for two consecutive windows.
6. **Confidence calibration drift:** bucketed predicted-vs-realized win rate Brier score > 0.10 over 30 trades.

If none fire → variance. Digest still updates (intra-loop) but no `ProposedChange` is emitted.

---

## Anti-runaway firewalls

1. **Frozen chokepoint:** `validate_take` constants marked Tier C. `MetaProposer.propose` MUST raise if a proposed diff attempts to *loosen* any threshold. Enforced via unit test `tests/meta/test_proposer_cannot_loosen.py`.
2. **Proposer rate limit:** max 3 proposals per tier per rolling 7 days; enforced inside `MetaProposer.propose` against `meta/proposed/` directory timestamps. Hard `RuntimeError` if exceeded.
3. **Tier D prohibition:** `ChangeTarget` Literal in `proposer.py` does not include `proposer_prompt`, `critic_prompt`, `mandate`, `risk_calculator`, `validate_take_threshold` — type system blocks them.
4. **Mandatory shadow window:** every Tier B change must run in `shadow_mode=True` (decisions logged, not executed) for 14 calendar days. `HumanApprovalDeployer.deploy` refuses without a `shadow_2w` entry in the oracle's `regimes_tested`.
5. **Backtest gates:** `BacktestOracle` constants `SHARPE_IMPROVEMENT_MIN=0.10`, `PBO_MAX=0.20`, `DSR_MIN=0.95`, plus "ruin probability must not increase" — all required, no per-regime opt-outs.
6. **Adversarial veto:** `CritiqueResult.verdict == "blocking_objection"` blocks deploy regardless of oracle score.
7. **Human approval non-optional:** `HumanApprovalDeployer.deploy` returns `None` if operator decision != "approve". No bypass flag.
8. **One-line rollback:** each deploy = single git commit; `rollback_command = "git revert <sha>"` stored in `DeploymentPackage`. Operator can revert from Telegram.
9. **Equity kill-switch:** `Executor.execute_entry` refuses if rolling 5-day account drawdown ≥ 8% OR rolling 20-trade expectancy negative; flips bot to `READ_ONLY` (gate runs, signals logged, no orders).
10. **Daily loss cap:** hard halt new entries after 2 consecutive SL hits in one UTC day.
11. **Meta-loop kill-switch:** env `ATLAS_META_LOOP_ENABLED=0` disables `MetaProposer` entirely; trading continues with last approved config. Default off until oracle harness exists.
12. **Frozen-prompt hashes:** `proposer_prompt_hash` and `critic_prompt_hash` recorded on every `ProposedChange` / `CritiqueResult`; `HumanApprovalDeployer` refuses if either hash diverges from the version pinned in `meta/frozen_prompts.lock`.

---

## Files touched (real paths)

Existing: `src/risk/position_manager.py`, `src/signals/llm_signal_generator.py`, `src/signals/gate.py`, `src/execution/executor.py`, `src/memory/autopsy_writer.py`, `src/memory/digest_builder.py`, `src/meta/proposer.py`, `src/meta/oracle.py`, `src/meta/critic.py`, `src/meta/deployer.py`, `src/database/db_schema.py`.

New: `src/execution/exec_intel.py`, `src/memory/stat_tests.py`, `meta/frozen_prompts.lock`, `exec_intel` table in `db_schema.py`.
