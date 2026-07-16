# Path 3 — Discretionary Trade Copilot

> The pivot. After Experiments 001–002 showed no simple mechanical pattern has
> edge on gold, we stop trying to REPLACE the operator's discretion and start
> AMPLIFYING it. The operator's live trading works (€34k, €2.6k shorts). The
> bot's job: discipline, sizing, journaling, and learning from real trades so
> it gets better the longer it runs.

## Why this, not signal generation

- Two patterns, both directions, two decades, ~30+ configs, all FAIL on real
  gold with realistic costs (see `research/experiment_001/002`).
- The operator's edge is discretionary — regime read, conviction, context,
  macro. Not mechanizable cheaply, and Alpha Arena shows LLM-as-trader loses.
- What IS reliable and valuable: enforcing discipline, sizing from measured
  edge, and building an evidence base on the operator's OWN setups.

## What it does (built, tested — 20 copilot tests, 419 total)

The operator proposes a trade; the copilot:

1. **Discipline gate** (`src/copilot/discipline_gate.py`) — enforces the ATLAS
   doctrine on human decisions:
   - BLOCK: R:R < 2, vague/short kill thesis, wrong-side stop, 2nd position,
     daily-loss-cap breach, drawdown halt (−20%), inside a news window.
   - WARN: thin thesis; **a setup that is a net loser over the operator's own
     history** ("this is 3-8 for you — are you sure?").
2. **Position sizer** (`src/copilot/position_sizer.py`) — fractional Kelly
   (0.25×, capped 2%) computed from the operator's per-setup stats. Cold-start
   conservative (0.5%) until ≥ 20 closed trades; auto de-sizes losing setups to
   the floor; conviction nudge ±.
3. **Journal + learning** (`src/copilot/journal.py`) — every trade logged to
   JSONL; per-setup SetupStats accumulate (win rate, expectancy, avg win/loss
   R). This is the "gets better over time" substrate.
4. **Orchestrator** (`src/copilot/copilot.py`) — review → coach → (commit) →
   close → learn.
5. **CLI** (`scripts/copilot.py`) — `review` / `close` / `profile` / `open`.

## The learning loop (demonstrated)

```
operator proposes trade
        │
   discipline gate  ──BLOCK──> refuse + reasons
        │ pass
   size from OWN per-setup Kelly (cold-start conservative)
        │
   commit → journal (OPEN)
        │
   close → R-multiple → per-setup stats update
        │
   next proposal of same setup:
     - gate warns if the setup is a net loser for you
     - sizer risks MORE on your proven edges, LESS on your weak ones
```

Demonstrated: a setup sized 0.52% cold-start was sized 2.00% after 23 logged
trades once it measured 65% win / +3.06R — Kelly on the operator's real data.

## Usage

```bash
# Review + log a trade (dry-run without --commit)
python scripts/copilot.py review \
  --instrument XAUUSD --direction short \
  --entry 4128.21 --stop 4140 --target 4030 \
  --setup htf-retracement-short \
  --thesis "H1 downtrend, sold retracement into declining MA; late longs trapped" \
  --kill "H1 closes back above 4140 with momentum for two bars" \
  --conviction 4 --session ny --equity 100000 --commit

# Close it
python scripts/copilot.py close --id <trade_id> --exit 4030 --reason TP

# See what you're actually good at (the learning)
python scripts/copilot.py profile
```

## How it "only gets better"

- **More trades → better sizing.** Kelly fractions sharpen as each setup's
  sample grows; conservative until earned.
- **More trades → honest self-knowledge.** `profile` ranks your setups by real
  expectancy. Your winners get capital; your pet-theory losers get flagged.
- **Reuses the autopsy/digest infra.** Closed trades already flow into the
  markdown memory + digest; the LLM enricher can later turn each into a
  post-mortem, and the meta-loop can propose gate/sizing refinements — all with
  the human-approval + backtest-oracle guardrails already built.

## Next enrichments (optional, incremental)

1. **LLM coaching layer** — the brain (Opus) reads the proposal + the
   operator's history and adds qualitative coaching to the deterministic gate.
   Cheap (one call per trade the operator initiates), and it's advisory, not
   a decision-maker (avoids the Alpha Arena failure mode).
2. **Telegram front-end** — propose/close trades from the phone; pre-trade
   checklist as a chat flow.
3. **Screenshot ingestion** — attach the chart; the LLM extracts levels/context
   into the journal automatically.
4. **Calendar wiring** — auto-set `--news` from the existing calendar feed.
5. **Weekly review** — auto-generated performance digest (win rate by setup /
   session / day, discipline adherence, biggest leaks).

## The honest framing

This will not manufacture an edge the operator doesn't have. It makes a
*working discretionary trader* more disciplined, better-sized, and
self-aware — and it compounds that advantage as the journal grows. That is a
real, defensible product, and it is honest about what it is.
