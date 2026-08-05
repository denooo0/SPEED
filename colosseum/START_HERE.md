# START HERE — the first steps, in order

Written so you can execute without thinking about sequencing. Do these in order.
Each step has a **stop condition** — a specific thing to check before moving on.

---

## STEP 0 · Prove the code runs (5 minutes)

```bash
pip install telethon                  # the only external dep, and only for MIRROR
python -m colosseum.cli selftest      # should print 113+/113+ all green
```

**Stop condition:** all checks green. If not, stop and send me the output.

---

## STEP 1 · Start Telegram capture TODAY (30 minutes, then it runs forever)

This is first because **every day you wait is a day of deleted messages you can
never recover.** Backfill gets you history; only live capture protects you from
survivorship.

### 1a · Credentials (10 min, once)

Go to **https://my.telegram.org** → log in with your phone → **API development
tools** → create an app (any name). Copy `api_id` and `api_hash`.

```bash
export TG_API_ID=1234567
export TG_API_HASH=abcdef0123456789abcdef0123456789
export TG_CHANNELS="@channel_one,@channel_two,@channel_three"
```

Put these in a `.env` you never commit. The session file that gets created is a
credential too — treat it like a password.

### 1b · Backfill 6 months

```bash
python -m colosseum.mirror.telegram_ingest --backfill --months 6
```

First run sends you a login code. After that the session persists. Expect this
to take anywhere from minutes to a couple of hours depending on channel volume —
it sleeps between batches on purpose and respects rate limits.

### 1c · Check what you got

```bash
python -m colosseum.mirror.telegram_ingest --summary
```

**Stop condition:** you see a sane `messages_per_day` and a `span_days` near 180.
If `with_text` is near zero and `photos` is high, that channel posts images —
re-run with `--download-images` and expect OCR to be the weak link.

### 1d · Start live capture and leave it running

```bash
nohup python -m colosseum.mirror.telegram_ingest --live &
```

This also logs **deletions** to `deletions.jsonl`. That file is evidence: it is
the only place survivorship becomes visible.

---

## STEP 2 · Get gold tick/bar data (the other half of MIRROR)

You cannot evaluate a signal without knowing what price actually did. Options,
roughly in order of preference:

- your broker's historical API (best — matches your real execution)
- a data vendor (Dukascopy, Polygon FX, Tiingo, Twelve Data)
- COMEX GC futures data if you want true contract volume

1-minute bars are enough for MIRROR. Tick data is better and is required if you
later want to bootstrap the main engine.

**Stop condition:** you can load 6 months of XAUUSD bars covering the same period
as your Telegram archive.

---

## STEP 3 · Answer the one question that decides everything

**Are the channels actually profitable?**

This is the whole reason MIRROR exists, and if the answer is no, you have saved
yourself months. Replay every parsed signal against your own bars with your own
spread:

```python
from colosseum.mirror.telegram_ingest import read_archive
from colosseum.mirror.parse import MessageParser
from colosseum.mirror.evaluate import replay_signal, evaluate_channel, CostModel, detect_martingale

parser = MessageParser()
signals, updates = [], []
for rec in read_archive("mirror_data/raw/channel_one.jsonl"):
    s, u = parser.parse(str(rec["msg_id"]), rec["t_ns"], rec["text"])
    if s: signals.append(s)
    if u: updates.append(u)

print(f"parsed {len(signals)} signals from the archive")
print(parser.stats)                       # check the parse rate before trusting anything

cost = CostModel(spread=0.30, slippage_entry=0.10, slippage_exit=0.15)
outcomes = [replay_signal(s, bars_after(s.t_ns), cost, updates) for s in signals]
report = evaluate_channel("channel_one", outcomes, cost)
print(report.verdict)
for w in report.warnings: print("⚠", w)
print(detect_martingale(signals)["assessment"])
```

**Read the verdict honestly.** Three outcomes:

- **PROFITABLE** → proceed to Step 4. You have something worth reverse-engineering.
- **MARGINAL** → collect more data before investing effort. Do not talk yourself into it.
- **NOT PROFITABLE** → **stop.** Study it as a negative example and move on. This
  is a successful result, not a failed one.

Check the parse rate first. If the parser only understood 40% of messages, the
verdict is about that 40%, not about the channel.

---

## STEP 4 · Recover the rulebook (only if Step 3 said PROFITABLE)

Feed each signal its market context at post time and mine the level provenance:

```python
from colosseum.mirror.provenance import build_context, ProvenanceMiner

miner = ProvenanceMiner()
for s in signals:
    ctx = build_context(s.t_ns, mid_at(s.t_ns), atr_at(s.t_ns),
                        swing_high=..., swing_low=..., vwap=...,
                        session_high=..., session_low=..., vpoc=...)
    if s.stop:  miner.observe("stop", s.stop, ctx)
    if s.entry: miner.observe("entry", s.entry, ctx)
    for i, tp in enumerate(s.targets, 1):
        miner.observe(f"tp{i}", tp, ctx)
        if s.risk: miner.observe_r_multiple(f"tp{i}", abs(tp - s.entry) / s.risk)

rep = miner.report()
for role, rules in rep["rules_by_role"].items():
    print(role, "->", rules[0]["rule"] if rules else "no rule found")
for r in rep["r_multiple_rules"]: print(r["interpretation"])
```

**Stop condition:** you can state their rules in one sentence each, e.g. *"stop
sits 0.3 ATR beyond the prior 15m swing 71% of the time; TP1 is a fixed 1.5R."*

Read `ambiguous_attributions` — where two anchors explain the levels equally
well, do not pick one and call it the rule.

---

## STEP 5 · Forced flow (run this in parallel — it needs no Telegram)

This is independent of MIRROR and is the highest-confidence real edge available
inside your constraint.

```python
from colosseum.features.forcedflow import ForcedFlowEngine
ff = ForcedFlowEngine()
for bar in your_15m_bars:                      # 1-2 years minimum
    ff.update(bar, swept=sweep_flag(bar))
rep = ff.report()
for w in rep["windows"]:
    if w["tradeable"]:
        print(w["window"], w["vol_ratio"], w["findings"])
print(rep["volatility_multipliers"])
```

**The volatility multipliers are the immediate payoff.** Feed them into stop and
target sizing right away — a stop sized for the London/NY overlap is far too
tight for Asia, and that single correction usually improves results more than any
new signal logic.

Treat directional findings with suspicion even when they pass. The module holds
them to FDR correction **and** a sign-stability test across halves precisely
because that is where seasonal folklore comes from.

---

## What NOT to do

- **Don't take screenshots.** MTProto gives you structured data with exact
  timestamps and edit history. Screenshots throw all of that away.
- **Don't use a bot.** Bots cannot read history before they joined, which makes
  your 4–6 month backfill impossible.
- **Don't skip Step 3.** Cloning a channel that isn't profitable is months of
  work to reproduce a loss.
- **Don't trust a claimed win rate.** Measure it yourself against your own tape.
- **Don't loosen the statistical gates when they reject things.** They are
  calibrated to reject things. That is the job.

---

## Where you'll be after this

Telegram capture running permanently, six months of history parsed, an honest
verdict on every channel, a recovered rulebook for any that pass, and a
volatility profile you can use immediately regardless of how MIRROR turns out.

Then the Foundry gets pointed at real gold data — and you find out what actually
survives deflation.
