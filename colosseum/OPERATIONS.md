# OPERATIONS RUNBOOK

Everything you need to take this from a repository to a running engine, plus what
to do when it misbehaves at 3am.

---

## 1 · First run (5 minutes, no broker or API key needed)

```bash
python -m colosseum.cli selftest              # 65 checks, all should pass
python -m colosseum.cli simulate --ticks 60000
python -m colosseum.cli search "divergence momentum"
python -m colosseum.cli scoreboard
python -m colosseum.cli retro --weekly --show
```

`simulate` runs the entire pipeline on deterministic synthetic tape and leaves a
full journal tree behind. Read a signal narrative before you write any
integration code — it shows you exactly what the engine will produce.

---

## 2 · Going live: exactly three integration points

**(a) The feed.** Subclass `feeds.base.WebsocketFeedTemplate`, implement
`_connect()` and `_parse()`. Two rules: emit *broker* event time as `t_ns` and
local receipt as `t_ingest_ns` (the gap is how clock skew is measured), and never
synthesize a tick to fill silence — silence is data the Guardian needs to see.

**(b) The strategists.** Set `llm_provider` and export the API key. `cli.py`
wires three seats automatically. Nothing else changes; `LLMStrategist` already
runs the master prompt and validates every response.

**(c) Telegram.** Create a bot via BotFather, set `TELEGRAM_TOKEN` and
`COLOSSEUM_TELEGRAM_CHAT_ID`, flip `telegram_enabled`.

```bash
export ANTHROPIC_API_KEY=sk-...
export TELEGRAM_TOKEN=123:abc
export COLOSSEUM_TELEGRAM_CHAT_ID=-100...
python -m colosseum.cli doctor        # validates everything before you start
python -m colosseum.cli run
```

`doctor` is the gate. It refuses to say READY unless config validates and the
ledger hash chain is intact.

---

## 3 · Cold start — do NOT skip this

A brand-new engine has no rankings, no calibration, and no lessons. Publishing
live signals from it is publishing noise with a confident tone.

```bash
python -m colosseum.cli bootstrap --archive ./ticks --days 60
```

This replays archived ticks through the **identical** pipeline in REPLAY health
(delivery hard-gated to zero). Because the pipeline is point-in-time sealed and
no component reads a clock, the resulting ledger is legitimate training data
rather than a backtest fantasy. Run it before your first live session, and
review the resulting weekly retro before trusting a single signal.

If you have no archive: run live in observe-only mode (`llm_provider=none`) for
a week to build one. The tick archiver runs regardless of whether seats reason.

---

## 4 · The daily rhythm

| When | What | Where |
|---|---|---|
| Continuous | signals + stand-downs | `journal/sessions/YYYY/MM/DD/` |
| Session close (17:00 NY) | diary, league table, calibration | `SESSION.md` |
| Weekly | is it improving? what to fix? | `journal/retros/weekly/` |
| Monthly | regime-level review | `journal/retros/monthly/` |
| On any fault | incident + repair trail | `journal/incidents/` |

Read the **weekly retro** religiously. It is the only artifact that answers "is
this thing actually getting better," and it is written to report regressions as
prominently as improvements.

---

## 5 · Reading the health state

| State | Publishing | What it means | Your move |
|---|---|---|---|
| `HEALTHY` | full | all detectors green | nothing |
| `DEGRADED` | 0.6× conviction | a non-critical detector tripped | read the verdict; usually self-clears after 5 clean checks |
| `RECOVERING` | **none** | critical fault; climbing the repair ladder | watch the incident journal |
| `SAFE` | **none** | fatal, or self-repair exhausted | **human required** — `guardian.clear_safe()` after diagnosis |
| `REPLAY` | none | bootstrap/verification in progress | expected |

Two escalation paths exist so the engine can never fail silently: repair attempts
are capped per fault (`max_repairs_per_fault`, default 3) and time in RECOVERING
is capped (`max_recovering_checks`, default 20). Either cap breaching sends it to
SAFE with an escalated incident. **Looping is denial, not resilience.**

---

## 6 · Troubleshooting

**"Fewer than 5 signals/day."** Read `throughput.diagnosis` in `SESSION.json` —
it names the dominant rejection reason and prescribes the fix. The *only*
sanctioned responses are: raise pre-filter *sensitivity*, add a regime-appropriate
lens, or accept a genuinely thin market. Never lower the quality bar; forcing
signals is a punished failure mode by design.

**"Everything is `cost_gate` rejected."** Your `spread` is too wide relative to
the targets your lenses find. Renegotiate spread or seek wider setups — do not
reduce `min_edge_multiple` below ~2.0, or the learner starts training on trades
that lose money after costs.

**"Mechanism rot warning in the retro."** More than 40% of outcomes had a false
stated mechanism: the engine is being right for the wrong reasons. P&L may look
fine while understanding decays. Audit the top seat's theses and raise
`RewardWeights.mechanism_penalty` before trusting the next promotion.

**"Calibration collapse."** Fit the isotonic layer, roll parameters back. This is
the *leading* indicator of a regime break — act before returns confirm it.

**"Ledger chain BROKEN."** The engine enters SAFE automatically. The log is
truth: run `rebuild_index()`, then `verify-state --archive` to re-derive from
ticks. Never "fix" the ledger by editing it.

**"State divergence."**
```bash
python -m colosseum.cli verify-state --archive ./ticks
```
A mismatch means live memory disagrees with what its own inputs imply. Trust the
log, discard the memory, rebuild. Never the reverse.

**"Repair loop / incident flood."** Should be impossible now (cooldown + attempt
cap + stuck-in-recovery watchdog), but if you see it: check
`guardian.suppressed_repairs` and raise `repair_cooldown_s`.

---

## 7 · Tuning knobs that actually matter

| Setting | Default | Raise it when | Lower it when |
|---|---|---|---|
| `min_edge_multiple` | 2.5 | spread is volatile | never below ~2.0 |
| `exploration_rate` | 0.07 | learner has plateaued | never below 0.02 |
| `llm_deadline_ms` | 3000 | stale-proposal rate is high | inference is fast and cheap |
| `challenger_fdr` | 0.10 | promotions keep souring | you need faster evolution |
| `max_concurrent` | 4 | — | drawdown is climbing |
| `daily_cost_budget_usd` | 25 | — | costs bite (engine gets pickier, not dark) |

---

## 8 · Backup

Back up **`ledger/` and `journal/` and `ticks/`**. Everything else —
`checkpoints/`, both SQLite indexes — is derived and rebuildable:

```python
ledger.rebuild_index()          # from the WAL
JournalIndex(root).rebuild()    # from journal files
```

Journals are never deleted by design. Ticks can roll to cold storage after 90
days, but keep them: they are the substrate that makes replay-rebuild — and
therefore self-healing — possible at all.

---

## 9 · What this system will not do for you

It will not tell you it is profitable. It will tell you, honestly and in writing,
whether it is **calibrated** and whether it **understood why price moved**. Those
are the things that compound. If you find yourself reading only the P&L line and
ignoring the calibration curve and the mechanism verdict, you have turned a
learning engine back into a slot machine.
