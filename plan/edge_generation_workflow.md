# Edge Generation Workflow

> How operator-observed market patterns become live trading setups.
> Source: agent design pass, synthesized from doctrine + research findings.

---

## Workflow diagram

```
[Operator observes pattern]
        │
        ▼
[Notion: ATLAS XAUUSD — Idea Intake]  --(MCP poll)-->  scripts/notion_triage.py
        │
        ▼
[Hypothesis Forge] <-- src/research/hypothesis_forge.py  (NEW)
   Uses 4-lens schema + current setup taxonomy from src/memory/markdown_store.py
        │
        ▼
[Hypothesis YAML in research/hypotheses/<id>.yml]  (Pydantic-validated)
        │
        ▼
[Backtest Oracle] <-- src/backtest/runner.py  (NEW — v9 prerequisite)
   In-sample fit → walk-forward CPCV → PBO / DSR
        │
        ├── FAIL ──> research/retired/<id>.md  (autopsy w/ reason codes)
        │           Re-test allowed after 90 days OR new feature added.
        │
        └── PASS ──> Setup registered in src/memory/markdown_store.py
                    + becomes a candidate filter inside src/signals/llm_signal_generator.py
                            │
                            ▼
                    [Live Paper Trading]
                    validate_take chokepoint enforces doctrine
                            │
                            ▼
                    src/memory/autopsies/<id>.md + digest update
                            │
                            ▼
                    30+ paper trades → promotion review
                            │
                            ▼
                    LIVE (real-capital) or SHELVED with autopsy
```

The Notion intake is the upstream end. The autopsy + digest feed back to the brain on every cycle (intra-loop) and to the meta-proposer weekly (cross-loop).

---

## Data shapes at each step

### 1. Notion Observation (template, see `ATLAS XAUUSD — Idea Intake` page)

```
Title: <short pattern name>
Properties:
  status: NEW | FORGING | BACKTESTING | SHADOW | LIVE | SHELVED | STALE | RETIRED
  lens: [flow | structure | context | intent]   (multi-select)
  symbol: "XAUUSD"
  timeframe: "1m" | "5m" | "15m" | "1h" | "1d"
  session: "Asia" | "London" | "NY" | "London-NY overlap" | "off-hours"
  conviction: 1..5
  source: "screen" | "news" | "intuition" | "coincidence" | "other"
Body sections (free text):
  ## What I saw
  ## When (time/session/regime)
  ## Why I think it works (mechanism)
  ## Counterexamples I remember
  ## Suggested entry/exit feel
```

### 2. Hypothesis (LLM forge output, Pydantic-validated)

```json
{
  "id": "H-2026-0142",
  "parent_observation": "<notion_page_id>",
  "thesis": "string, one sentence",
  "mechanism": "string, why this should persist",
  "entry_rule": {"trigger": "...", "features_required": [...]},
  "exit_rule": {"stop": "...", "target": "...", "time_stop": "..."},
  "filters": {"session": [...], "regime": [...], "cot_state": "..."},
  "features_used": ["pdh_sweep", "london_open_window", ...],
  "expected_edge": {"direction": "mean_revert", "horizon_bars": 12},
  "falsifiers": ["if no reversal within N bars", "if volume < X"],
  "min_sample": 60
}
```

### 3. Backtest contract

`src/backtest/runner.py` accepts the hypothesis YAML directly and emits:

```json
{
  "hypothesis_id": "H-2026-0142",
  "in_sample": {"sharpe": ..., "pf": ..., "trades": ..., "hit_rate": ...},
  "walk_forward": {"wfe_per_step": [...], "wfe_pass_ratio": 0.78},
  "pbo": 0.14,
  "dsr": 0.97,
  "verdict": "PASS" | "FAIL",
  "fail_reasons": []
}
```

### 4. Promotion criteria (hardcoded in `runner.py`)

```
PASS requires ALL of:
  pbo < 0.20
  dsr > 0.95
  wfe_pass_ratio >= 0.70
  min wfe_per_step >= 0.5
  trades >= 60 (per fold) OR aggregated >= 200
```

### 5. Live registration

Passing hypothesis: appended to `memory/setups/` (gains a setup file with frontmatter + lessons), becomes a candidate filter inside `src/signals/llm_signal_generator.py`. Every fill writes `memory/autopsies/<id>.md`; digest tracks live-vs-backtest delta per setup.

---

## Worked example: London PDH sweep reversal

1. **Operator drops Notion card:** *"XAUUSD sweeps PDH in London hour-1 then reverses."* lens=structure+context, session=London, conviction=4.
2. **`notion_triage.py` (Sun 14:00 UTC)** picks up `status=NEW`, sends body + observation schema to LLM via `hypothesis_forge.py`. Prompt = ATLAS forge role + 4-lens definitions + setup taxonomy excerpt; user msg = observation body; output schema = Hypothesis JSON.
3. **LLM emits hypothesis:** trigger = `high[bar] > pdh AND time in [08:00, 09:00 UTC]`; entry = short on first 5m bar that closes back below `pdh` within 30min; stop = sweep high + 0.3 ATR; target = `pdh - 1R` or session VWAP; falsifier = no reclaim within 6 bars.
4. **`runner.py` walk-forwards 3y of Bybit 5m**, splits 12 folds. Result: `pbo=0.16, dsr=0.96, wfe_pass_ratio=0.75`. Verdict = PASS.
5. **Setup gets ID `LDN_PDH_SWEEP_REV`**, added to taxonomy. `llm_signal_generator.py` now includes it as a candidate. Paper trading begins; each fill writes an autopsy.
6. **Digest tracks live-vs-backtest delta.** If live Sharpe drifts > 40% below backtest over 30 trades, auto-flag for retirement review.

---

## Open decisions for the operator

1. **Walk-forward geometry:** anchored vs rolling, fold count. Suggested: 12 rolling, 6mo IS / 1mo OOS.
2. **Minimum trade sample before promotion-eligible:** suggested ≥ 60 trades aggregated across OOS folds.
3. **Cost model assumption:** Bybit taker fee + slippage. Suggested: 11bp round-trip given XAUUSD perp spreads (per research findings).
4. **Hypothesis lifecycle TTL:** how many live-paper trades before mandatory re-backtest. Suggested: 50 trades or 90 days, whichever first.
5. **Live-vs-backtest drift kill-switch:** Sharpe decay % threshold + cooldown before retirement. Suggested: 40% decay over 30 trades → flag; 60% over 30 trades → auto-retire.

---

## Integration points (real file paths)

| Step | Module / file |
|---|---|
| Notion poll | `scripts/notion_triage.py` (NEW) |
| Hypothesis forge | `src/research/hypothesis_forge.py` (NEW) |
| Hypothesis storage | `research/hypotheses/<id>.yml` (NEW dir) |
| Backtest runner | `src/backtest/runner.py` (NEW — v9 prerequisite) |
| Setup taxonomy | `src/memory/markdown_store.py` (existing) |
| Live filter integration | `src/signals/llm_signal_generator.py` (existing) |
| Autopsy on close | `src/memory/autopsy_writer.py` (existing) |
| Digest aggregation | `src/memory/digest_builder.py` (existing) |
| Retired archive | `research/retired/<id>.md` (NEW dir) |
