# THE COLOSSEUM — Gold Microstructure Signal Engine

A self-improving XAU/USD microstructure signal engine. Stdlib-only core (no
numpy, no pandas) so it runs anywhere and every line is inspectable.

**Verified:** `python3 -m colosseum.cli selftest` → **124/124 checks pass.**

```bash
python -m colosseum.cli selftest              # prove it works
python -m colosseum.cli simulate --ticks 60000 # full pipeline on synthetic tape
python -m colosseum.cli doctor                # validate a deployment
```

See **OPERATIONS.md** for going live, cold-start bootstrap, and the 3am runbook.

---

## The two design laws everything else follows from

**1 · Determinism.** Time is an *input*, never a clock read. Randomness is
seeded. Records are content-addressed. Because of this, the engine can replay
its own history bit-exactly — which is what makes self-healing provable rather
than hopeful.

**2 · No repaint.** A swing pivot carries both `t_ns` (when it happened) and
`t_confirmed_ns` (when it became knowable), and every downstream decision keys
off the latter. This single rule is worth more than any model improvement: it's
the difference between an honest engine and a beautiful backtest that loses
money live.

---

## Layout

```
colosseum/
├── core/          clock (DST-safe), types (immutable), ring buffers, content IDs
├── ingest/        tick normalizer (border guard), multi-TF bar aggregator
├── features/      VWAP+σ bands, ADL/OBV/CMF/delta, RSI/ATR, pivots,
│                  structure (BOS/CHoCH), divergence, sealed FeatureFrames
├── ledger/        CRC-framed crash-safe WAL, hash-chained indexed store
├── journal/       six-stratum memory, taxonomy, FTS index + backlinks, retros
├── guardian/      health state machine, detectors, checkpoints, repair ladder
├── learn/         calibration, Elo/relegation, path-match, reward, LinUCB
│                  bandit, FDR-controlled challenger promotion
├── arena/         pre-filters (3 lenses), arbiter, outcome tracker
├── llm/           master prompt + provider-agnostic validated strategist
├── feeds/         feed interface, failover, tick archiver, CSV/JSONL replay
├── delivery/      Telegram publisher with Taken/Skipped feedback
├── engine.py      wiring          runner.py   24/7 supervised process
├── replay.py      bootstrap + state verification
├── config.py      env-overridable config, secrets never journaled
├── cli.py         run · bootstrap · verify-state · retro · search · doctor
└── tests/         deterministic tape generator + 124-check verification suite
```

## Quick start

```python
from colosseum.engine import Engine, EngineConfig
from colosseum.tests.harness import MockStrategist, synthetic_ticks

eng = Engine(EngineConfig(root="./data"), strategists={
    "A": MockStrategist("A", "Wyckoff Accountant"),
    "B": MockStrategist("B", "Divergence Hunter"),
    "C": MockStrategist("C", "VWAP Mean-Reverter"),
})
for tick in synthetic_ticks(start_ns, 40_000):
    eng.on_tick(tick)          # feed live ticks here in production
eng.close_session("2026-08-03")
```

To go live, replace two things and nothing else:
1. `synthetic_ticks` → your broker's tick stream (emit `core.types.Tick`).
2. `MockStrategist` → an LLM client running the Part I master prompt. The
   `Strategist.reason()` interface is deliberately narrow so the model can be
   swapped, mocked, or replayed without touching the rest of the engine.

---

## What each guarantee costs you if you skip it

| Block | Guarantee | What breaks without it |
|---|---|---|
| `core/clock` | DST-correct session anchors | VWAP resets at the wrong hour twice a year, silently |
| `features/pivots` | k-bar confirmation | look-ahead bias; backtest lies |
| `ledger/wal` | CRC framing + torn-tail truncation | a power cut leaves half a record that parses as valid |
| `ledger/store` | hash chain | you cannot prove your own history is untampered |
| `guardian/recovery` | replay verification | "probably fine" silently becomes "resumed" |
| `learn/reward` | mechanism penalty | lucky wins out-rank understanding; engine gets dumber while P&L looks fine |
| `arena/arbiter` | cost gate | the learner trains on trades that lose money after spread |
| `journal/taxonomy` | controlled vocabulary | one strong lesson fragments into three weak ones |

---

## Notable behaviours (verified, not aspirational)

- **A lucky win scores below a reasoned loss.** `compute_reward` gives
  win+false-mechanism `−0.96` vs loss+confirmed-mechanism `−0.69`. This is the
  anti-laziness clause with teeth.
- **Seats get relegated on calibration, not win rate.** A seat claiming 0.88
  conviction while winning 45% loses shelf space even if it's profitable.
- **Stand-downs are compressed, not dropped.** Logging one per seat per second
  is 259k appends/day — a real bottleneck found by running it. Now they're
  written on reason-change or near-miss, with exact counts preserved.
- **Conservative tie-breaks.** If a bar spans both stop and target, the stop is
  assumed first. Optimistic resolution is how backtests inflate everything.
- **Uncertainty tightens risk, never loosens it.** Degraded health publishes at
  0.6× conviction; it never widens a stop to "give it room."

## Bugs the verification suite caught (and the fixes)

These were all found by *running* the thing, not by reading it — which is the
whole argument for the suite:

- **Reward ordering was decorative.** A lucky win on a false mechanism scored
  *above* a reasoned loss, so a seat could farm rating with unexplained wins.
  Raised `mechanism_penalty` until the intended ordering actually holds.
- **Stand-down journaling was a throughput bottleneck.** One entry per seat per
  second is ~259k appends/day. Now compressed on reason-change or near-miss,
  with exact counts preserved so no base rate is lost.
- **Repair storms.** A persistent fault re-climbed the repair ladder every health
  cycle — 230 incidents in a 2-session simulation, burying the one that mattered.
  Fixed with a per-fault cooldown, an attempt cap, and a stuck-in-recovery
  watchdog that escalates to SAFE instead of looping.
- **Detectors accusing on thin evidence.** `calibration_collapse` fired on 11
  samples. Now gated behind `min_calibration_n`.
- **Index write-back gap.** Outcomes updated the journal files but not the search
  index, so `scoreboard` and every outcome-filtered query silently returned empty.

## Known limitations (stated, not hidden)

- Spot gold volume is **tick-volume**, a proxy for activity, not contract
  volume. Stamped on the data. Fuse COMEX GC for ground truth if needed.
- Divergence samples indicator values at pivot *confirmation* time, not pivot
  time. Sound and leak-free, but slightly less precise than a replay-based
  sampler.
- The bandit tunes multipliers on structure-derived levels; it never invents a
  price. Deliberate — a bandit allowed to place stops directly eventually puts
  one in mid-air with a great justification.

---

## Upgrades beyond the baseline (all verified)

| Module | What it adds | Why it moves the needle |
|---|---|---|
| `arena/invalidation.py` | monitors and **acts on** each signal's stated early tell | The engine wrote down "here's what proves me wrong before the stop" and then ignored it. Exiting on the tell converts full −1R losses into partial ones. Fires on a **close**, never a wick (a wick through is a sweep — often the moment the thesis starts working). |
| `features/microstructure.py` | Amihud, Kyle's λ, VPIN, Corwin-Schultz, Roll, **volume profile / VPOC / value area** | All from OHLCV only. VWAP gives one number; the profile gives the shape. Fading a 2σ stretch into a low-volume node is a completely different trade from fading into a high-volume node — v1 couldn't tell them apart. |
| `ingest/infobars.py` | volume / dollar / imbalance bars | Time bars sample by the clock; markets deliver information by volume. Dollar bars measured **Jarque-Bera 1.3 vs 8.0** for 5-minute bars on the same tape — far closer to IID normal, which makes every downstream statistic more trustworthy. |
| `learn/metalabel.py` | secondary "should I take this?" model, purged+embargoed CV, uniqueness weighting | Lets the primary lenses stay aggressive while a filter kills their bad calls. Trains on the Ledger, which is already a fully-labelled dataset. **Fails open** — abstains until it proves out-of-sample lift. |
| `learn/sizing.py` | fractional-Kelly sizing on calibration-adjusted probability | The payoff for all the calibration work. An overconfident seat at p=0.75 sizes **smaller** than an honest seat at p=0.60. |

### Bugs found in the audit pass

- **Arbiter never released exposure on resolution** — slots stayed locked for the full correlation window (1h) even when a trade closed in 5 minutes. Four quick scalps locked the engine out for the rest of the hour, looking exactly like "the market was thin." Directly throttled the ≥5/day target.
- **`signals_per_day` was cumulative** — divided all-time signals by "days elapsed," so day 30 reported a month of signals as if they happened today.
- **Calibrator decayed aggregates but not bins** — `brier` and `decomposition()["brier"]` disagreed (0.64 vs 0.34), and the reliability curve was an all-time average that could never show a seat going bad. A calibration tracker that can't see a regime change reports false reassurance.
- **Invalidation monitor silently disabled** — with no timeframe view, `rvol` defaulted to 1.0 and failed the expansion test, so every expansion-qualified invalidation watched a clean break and did nothing.
- **Sizing capped before throttling** — a size scaled down by a deep drawdown was capped back *up* to the ceiling, so the throttle did nothing at exactly the moment it mattered.
- **Sizing saturated at the cap** — raw Kelly exceeds any sane risk cap for almost any p above breakeven, so every signal returned the same 1.00%, throwing away the calibrated probability it was built to use.
