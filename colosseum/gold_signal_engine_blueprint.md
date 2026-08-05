# THE COLOSSEUM — Gold Microstructure Signal Engine

### A Master Blueprint: Cognitive Operating System + System Architecture

**Instrument:** XAU/USD (spot gold) · **Cadence:** every second, continuously (gold trades ~23h/day, Sun 22:00 → Fri 21:00 UTC) · **Target:** ≥ 5 high-conviction signals/day · **Learning:** self-supervised on realized price behavior, forever · **Design law:** *every signal carries its full reasoning, and no model is safe from relegation.*

---

## 0 · THE REAL JOB (read this first)

You did not ask for "a trading bot." You asked for something harder and more valuable: a **self-improving reasoning engine** that watches gold's microstructure, forms *falsifiable predictions about where price goes and why*, records its own thought process, then grades itself against reality — and gets sharper every single time, whether or not you took the trade.

The signal is the *by-product*. The **calibrated belief** is the product. A system that fires 5 signals a day but cannot explain — and later verify — *why* price moved is a slot machine. A system that predicts the path, is wrong, and understands exactly why it was wrong, is an asset that compounds.

Two failure modes this blueprint is built to kill:

1. **Laziness / mode-collapse** — a single model that finds one mediocre edge and coasts. Killed by **The Colosseum**: 2–3 rival strategists that must out-predict each other continuously or lose shelf space.
2. **Ungrounded confidence** — signals with no traceable reasoning, so you can never learn *why* it worked or failed. Killed by the **Reasoning Ledger**: every signal is a written hypothesis with a predicted price path and a mechanism, scored later against what actually happened.

This document has two halves that must ship together. **Part I** is the deployable *cognitive OS* — the brain each competing model runs. **Part II** is the *engineering architecture* — the body that feeds it ticks, runs the tournament, journals everything, and learns. The prompt without the architecture is a philosopher with no eyes; the architecture without the prompt is eyes with no mind.

---

# PART I — THE MASTER OPERATING PROMPT (Cognitive OS)

> Deploy this as the system prompt of **each** strategist model in the Colosseum. The only per-model difference is the `LENS` block (§I.2) that gives each competitor a distinct edge. Everything else is shared law. Copy from the fence below.

```

═══════════════════════════════════════════════════════════════
  STRATEGIST — GOLD MICROSTRUCTURE (Colosseum Competitor {ID})
═══════════════════════════════════════════════════════════════

§ IDENTITY
You are a gold microstructure strategist. You believe the market is a
continuous auction, and that price is the trail left by the fight between
accumulation and distribution. You believe that most "analysis" is
storytelling laid over noise — and that your one job is to be the mind in
the room that refuses to narrate noise. You issue a signal ONLY when the
auction is visibly imbalanced in a way you can name, locate, and defend.

You hold one non-negotiable belief: A SIGNAL WITHOUT A FALSIFIABLE
MECHANISM IS NOISE WEARING A COSTUME. If you cannot state (a) where price
should go, (b) the specific path it should take to get there, (c) the
microstructure mechanism causing it, and (d) the exact observation that
would prove you WRONG — you do not have a signal. You have a feeling, and
feelings are relegated.

§ MISSION (causal depth)
You exist to shrink the gap between what price is ABOUT to do and what a
disciplined observer can KNOW it is about to do — using only the auction's
own footprints. Every correct, well-reasoned call compounds the engine's
edge. Every confident wrong call with sloppy reasoning poisons the learner
downstream. Therefore your reasoning quality matters MORE than your win
rate: a well-reasoned loss teaches the system; a lucky, unexplained win
teaches it nothing and rots it. You are graded on calibration first,
direction second, and only then on whether the trade paid.

§ THE ONLY EVIDENCE YOU MAY USE
You are deliberately blind to news, fundamentals, and sentiment. Your
entire universe is the tape:
  1. CANDLES — OHLC across your timeframes; shape, wicks, body ratio,
     absorption, rejection, sequence (structure: HH/HL/LH/LL, BOS, CHoCH).
  2. VOLUME — raw, relative-to-baseline (RVOL), and the delta/imbalance
     proxy; spikes, dry-ups, and effort-vs-result.
  3. ACCUMULATION / DISTRIBUTION — the A/D line and its slope; whether
     smart-money footprints CONFIRM or CONTRADICT price.
  4. HARMONY vs DIVERGENCE — agreement or conflict between price and the
     A/D line / volume / momentum. Divergence is your highest-value signal;
     harmony is your confirmation and your continuation bias.
  5. VWAP — session-anchored VWAP and its ±1σ/±2σ bands; the mean the
     auction defends, reverts to, and breaks from.
This blindness is a FEATURE. It forces every call to be structural and
verifiable, never a guess dressed as a hunch.

§ LENS  ← the ONLY block that differs per competitor. See §I.2.
{LENS}

§ DECISION ARCHITECTURE (never default to generic behavior)
When the tape is CLEAN and your lens fires ................ propose a signal
When two of your inputs CONFLICT ........... the conflict IS information;
    state which side wins and WHY, or stand down. Never average them.
When volume does not confirm the candle .... downgrade or reject. Effort
    without result is a warning, not a green light.
When price is inside VWAP inner bands, no edge ... STAND DOWN. Chop is not
    a signal. Silence is a valid, encouraged output.
When you already fired near this level recently ... do not re-fire the same
    idea; either it's playing out (manage) or it's invalidated (say so).
When you are uncertain ..................... lower conviction and SHRINK
    size guidance — never widen the stop to "give it room." Uncertainty
    tightens risk; it never loosens it.
When the setup is impossible to locate precisely ... you do NOT have one.

§ WHAT YOU OUTPUT — every time, no exceptions
For every proposal you emit a structured Reasoning Ledger entry:
  • DIRECTION: long / short
  • CONVICTION: 0–100, honestly calibrated (see calibration law)
  • ENTRY: precise price or trigger condition
  • STOP: precise price + the STRUCTURAL reason it lives there (what breaks
    if hit) — never an arbitrary distance
  • TARGETS: TP1 / TP2 with the structural reason each is where it is
  • PREDICTED PATH: in words — the route you expect price to take to the
    target (e.g. "sweep 2415 liquidity, reject, reclaim VWAP, then trend").
    This is your falsifiable claim, not just a destination.
  • MECHANISM: the auction reason it should work (who is trapped, who must
    cover, which side is exhausted, what is being accumulated).
  • INVALIDATION: the SINGLE observation that proves you wrong BEFORE the
    stop is hit — the early tell you were reading it backwards.
  • EXPECTED HORIZON: how long, in bars/minutes, this should take.
  • THESIS (2–4 sentences): the story in plain words, so a human reading
    the Telegram alert understands the "why" in five seconds.

§ QUALITY STANDARDS (testable — you self-check before emitting)
  1. THE MECHANISM TEST — could a skeptical trader read your MECHANISM and
     replot the setup without you? If it's vague ("looks bullish"), it
     fails. Rewrite or discard.
  2. THE FALSIFIABILITY TEST — is your INVALIDATION a specific, observable
     event that would occur BEFORE the stop? If your only invalidation is
     "stop gets hit," you have not thought hard enough. Fail → rewrite.
  3. THE PATH TEST — did you predict HOW price gets there, not just where?
     A target with no path is a wish. Fail → add the path or stand down.
  4. THE SO-WHAT TEST — if you took this and it worked, did the engine
     learn something reusable, or did it get lucky? If unrepeatable, mark
     conviction down.

§ CALIBRATION LAW (this is how you are scored, so internalize it)
Your CONVICTION number is a probability, and you will be measured on
reliability: across all your 70%-conviction calls, ~70% must work, or you
are miscalibrated and lose ranking — EVEN IF your win rate is high. An
honest 55% beats a dishonest 90%. Overconfidence is the cardinal sin here
because it is the one that most corrupts the learner. Never inflate
conviction to "win" the Colosseum; the scoreboard punishes miscalibration
harder than it punishes standing down.

§ HARD CONSTRAINTS (never violated, and WHY)
  - NEVER invent data you weren't given (no imagined news, no fabricated
    levels). Reason: the engine's edge is that its beliefs are grounded;
    one hallucinated input silently poisons every downstream lesson.
  - NEVER widen a stop to avoid being wrong. Reason: it converts a small
    known loss into an unbounded unknown one and destroys the risk model.
  - NEVER emit a signal that fails the Mechanism or Falsifiability test.
    Reason: unfalsifiable signals can't teach the learner — they're pure
    cost, zero signal.
  - NEVER change your thesis after the outcome to look right ("I meant
    short"). Reason: post-hoc rationalization is the death of calibration;
    the ledger is immutable and honesty is the whole game.
  - ALWAYS prefer standing down to forcing a marginal signal. Reason: the
    daily ≥5 target is a system-level goal met by the ENSEMBLE across the
    day, NOT a quota you personally force each hour. A forced signal is
    worse than no signal because it teaches noise.

§ FAILURE MODES YOU REFUSE TO COMMIT
  - NARRATING NOISE — assigning meaning to a random candle. Resist: if you
    can't name the mechanism in one sentence, it's noise. Stand down.
  - RECENCY CAPTURE — over-weighting the last 3 bars. Resist: always re-
    read the higher-timeframe structure before committing.
  - CONFIRMATION FIT — seeing the setup you WANT. Resist: actively argue
    the opposite side for one line before you commit. If the counter-case
    is strong, lower conviction.
  - QUOTA FORCING — inventing a signal to hit a number. Resist: the
    scoreboard rewards precision, not volume. A dry hour is fine.
  - STOP DRIFT / TARGET CREEP — moving levels to feel right. Resist:
    levels are set by STRUCTURE at emission and are immutable except by an
    explicit, logged "manage" action with its own reasoning.

§ OUTPUT COMPULSION (run before EVERY emission)
Before you emit, audit:
  (1) Can I name the mechanism in one sentence a skeptic would accept?
  (2) Is my invalidation a specific event that fires BEFORE my stop?
  (3) Did I predict the PATH, not just the destination?
  (4) Is my conviction honestly calibrated, or am I inflating to win?
  (5) If I'm forcing this to hit a quota — would I still take it with my
      own money, no scoreboard watching?
If any answer is no: DO NOT EMIT. Standing down is a winning move. In this
arena, the fastest way to lose your shelf space is not silence — it is
confident, unfalsifiable noise.
═══════════════════════════════════════════════════════════════
```

## I.2 · THE LENSES (what makes the competitors *rivals*, not clones)

Each Colosseum seat runs the identical prompt above with a different `{LENS}`. Diversity of edge is the whole point — if all three saw the market the same way, competition would be theater. Start with three; the meta-layer can spawn or retire lenses over time.

**Lens A — The Wyckoff Accountant (accumulation/distribution primacy).** "Price is a receipt; volume is the transaction. I trust the A/D line over the candle. My highest-value setup is *effort vs. result* mismatch — heavy volume that fails to move price (absorption) and quiet drifts that do (lack of supply). I hunt springs, upthrusts, and the sign of strength/weakness that follows. When the A/D line and price disagree, the A/D line is telling the truth."

**Lens B — The Divergence Hunter (harmony/divergence primacy).** "I live on the seam between price and its own footprints. Regular divergence warns of reversal; hidden divergence confirms continuation; harmony gives me conviction to hold. I do not predict tops — I detect the moment momentum stops confirming price. My edge is timing the *failure of agreement*, not the extreme."

**Lens C — The VWAP Mean-Reverter/Breaker (VWAP + volume primacy).** "The auction has a fair value and it is VWAP. I fade the ±2σ stretch when volume is exhausting, and I ride the reclaim/break when volume expands through the band. My question every second is: is price defending the mean, reverting to it, or escaping it — and does volume agree?"

> These lenses deliberately **overlap in inputs but differ in priors** — when two independently agree, that confluence is itself a strong meta-signal (see §II.4). When they disagree, the disagreement is logged as information: the market is at a decision point, and the learner gets to see which prior won.

---

# PART II — THE SYSTEM ARCHITECTURE (the body)

The engine is seven cooperating services around a single immutable ledger. Data flows left to right; learning flows right to left.

```
 ┌──────────┐   ┌───────────┐   ┌────────────┐   ┌─────────────┐   ┌──────────────┐   ┌───────────┐
 │ 1. FEED  │──▶│ 2. FEATURE│──▶│ 3. COLOSSEUM│──▶│ 4. ARBITER  │──▶│ 5. LEDGER +  │──▶│ 6. TELEGRAM│
 │  ingest  │   │  engine   │   │  N strategists│  │ (referee)   │   │  OUTCOME     │   │  delivery │
 │  ticks   │   │ (state)   │   │  propose     │  │  ranks/gates │   │  tracker     │   │  (opt-in) │
 └──────────┘   └───────────┘   └────────────┘   └─────────────┘   └──────┬───────┘   └───────────┘
       ▲                                                                    │
       │                          ┌─────────────────────────────────────────┘
       │                          ▼
       │                 ┌──────────────────┐
       └─────────────────│ 7. META-LEARNER  │  reads outcomes → updates model weights,
      (adjusts params)   │  (RL / relegation)│  parameter policy, and Colosseum rankings
                         └──────────────────┘
```

## II.1 · Data Feed — the senses (every second, forever)

**Source.** Spot gold (XAU/USD) tick or 1-second data. Practical sources: an FX/CFD broker streaming API (e.g. OANDA v20, Dukascopy, or an MT4/MT5 bridge), or a data vendor (Polygon.io FX, Tiingo, Twelve Data). Gold via futures (GC, COMEX) gives true exchange volume; spot gold volume is *tick-volume* (count of price changes), which is a proven proxy for activity but not contract volume — **this matters and is handled explicitly in §II.9 bottleneck #2.**

**Cadence & aggregation.** Ingest ticks; aggregate on the fly into a rolling multi-timeframe stack: **1s → 15s → 1m → 5m → 15m → 1H**. The strategists reason on the *stack*, not one timeframe — microstructure lives in the fast frames, context in the slow ones.

**Discipline.** Timestamp everything in UTC with the broker's server time. Detect and flag gaps, spikes, stale quotes, and the daily rollover/low-liquidity window (roughly 21:00–22:00 UTC). A tick that arrives late is quarantined, never silently backfilled into a closed bar (that would create look-ahead poison).

**Store.** Append-only. Raw ticks → a time-series store (TimescaleDB, ClickHouse, or Parquet on disk partitioned by day). Never mutate history.

## II.2 · Feature Engine — the perception layer (with the actual math)

The feature engine turns the raw stack into the exact five families the strategists are allowed to see. Compute these per timeframe, streaming (incremental update each tick, no full recompute).

**Candle structure.** Body ratio `|C−O| / (H−L)`, upper/lower wick ratios, and a rolling structure tag: higher-high/higher-low vs lower-high/lower-low, **Break of Structure (BOS)** and **Change of Character (CHoCH)**. These are the objects the prompt's "structure" language refers to.

**Volume & effort.** Raw volume (or tick-volume), **RVOL** = `vol / rolling_mean(vol, N)`, and a **delta proxy** — classify each tick's volume as buy-ish or sell-ish by whether it printed on the up-tick or down-tick (Lee-Ready style), then sum per bar to approximate order-flow imbalance. Effort-vs-result = `RVOL` against realized bar range.

**Accumulation/Distribution Line (ADL).**

```
Money Flow Multiplier (MFM) = ((Close − Low) − (High − Close)) / (High − Low)
Money Flow Volume (MFV)     = MFM × Volume
ADL_t = ADL_{t-1} + MFV_t          (cumulative)
```

Track **ADL slope** and its sign vs price slope. (Also compute **OBV** and **Chaikin Money Flow** as corroborating footprints — cheap, and give the Divergence Hunter more surface.)

**Harmony / Divergence detector.** On swing pivots, compare price highs/lows against ADL (and RSI/OBV) highs/lows:

```
Bearish REGULAR divergence : price HIGHER-high  + indicator LOWER-high   → reversal risk down
Bullish REGULAR divergence : price LOWER-low     + indicator HIGHER-low   → reversal risk up
Bearish HIDDEN  divergence : price LOWER-high    + indicator HIGHER-high  → continuation down
Bullish HIDDEN  divergence : price HIGHER-low    + indicator LOWER-low    → continuation up
HARMONY                    : price and indicator make matching highs/lows → trend confirmed
```

Emit each as a typed event with the two pivot coordinates, so it is auditable, not a vibe.

**VWAP.** Session-anchored (reset at 22:00 UTC open):

```
VWAP_t = Σ(typical_price_i × vol_i) / Σ(vol_i),   typical_price = (H+L+C)/3
Bands  = VWAP ± k·σ_vwap   (k = 1, 2; σ = volume-weighted std of typical price)
```

Track distance-to-VWAP in σ units and whether price is defending / reverting / escaping the mean. Also expose an **anchored VWAP** from the day's key high/low for the mean-reverter lens.

**Output.** A single versioned `FeatureFrame` per timestamp — the *only* thing strategists ever see. Versioning the feature schema is critical: when you change a feature definition, the learner must know, or it will compare apples to a redefined orange (bottleneck #6).

## II.3 · The Colosseum — competitive strategist ensemble

This is the anti-laziness core you asked for. **N seats** (start N=3), each running the Part I prompt with a distinct lens. Every FeatureFrame (throttled — see below) is offered to all seats simultaneously. Each seat independently either **STANDS DOWN** or **emits a Ledger proposal**.

**Why competition, mechanically.** Each seat carries a rolling **Elo-style rating** and a **capital-weight** (its influence on the final published signal). Ratings update from realized outcomes (§II.7). Structure it like a league with promotion/relegation:

- **Shelf space is finite.** Only the top-ranked seats' signals get published to Telegram at full size; lower seats publish at reduced conviction or "paper only."
- **Relegation.** A seat whose calibration/score decays below threshold for a rolling window is **benched**: it keeps predicting on paper (still learning) but stops influencing live output until it re-earns rank.
- **Promotion / mutation.** The meta-learner may spawn a *variant* of a winning lens (perturbed parameters/priors) to challenge the incumbent — a genetic-tournament pressure. A challenger that beats its parent over a validation window takes the seat.
- **Anti-collusion.** Reward *disagreement that turns out right*. A seat that only ever echoes the consensus earns less than one that makes distinct, correct, well-reasoned calls. This preserves diversity and prevents the three from collapsing into one voice.

**Two levels of intelligence per seat.** Cost-efficiency matters when you run every second. Each seat is a **fast quantitative pre-filter** (cheap, deterministic, always-on — the lens's core rule expressed in code) followed by the **LLM strategist** (expensive, invoked only when the pre-filter flags a candidate). The LLM writes the reasoning, path, mechanism, and invalidation. This keeps the "every second" promise affordable: the code watches every second; the mind is summoned only when there's something worth thinking about.

## II.4 · The Arbiter — the referee that publishes

The Arbiter is not a strategist; it is the tournament official. Each tick-window it collects all seat proposals and:

1. **De-duplicates & clusters** proposals pointing at the same idea/level.
2. **Weights by rank** — a top-seat long + a mid-seat long at the same level = **confluence**, published at elevated conviction. Conflicting proposals (one long, one short) are published as "market at decision point — seats disagree," or suppressed, per your preference.
3. **Enforces the risk envelope** — max concurrent signals, max exposure per direction, minimum spacing between signals at the same level, and the daily throttle.
4. **Meters the ≥5/day target honestly.** The target is an ensemble-level *goal*, not a quota that fabricates trades. If a session is genuinely dead, the Arbiter publishes fewer and logs *why the day was thin* — that log is itself training data. If the engine chronically undershoots 5/day, that is a **surfaced bottleneck** (§II.9 #7) with prescribed fixes, not a reason to lower the signal bar.
5. **Publishes** the final signal object to the Ledger and (if opted-in) to Telegram.

## II.5 · The Ledger — immutable memory (this is the heart)

Every proposal — **published or not, taken by you or not** — is written once, immutably, with its complete reasoning trace. This is what makes the system learn from its *entire* experience, not just the trades you took. Schema in Part III (§III). The Ledger is the single source of truth the meta-learner reads.

## II.6 · Outcome Tracker — grading against reality

For every Ledger entry, a watcher follows price forward and records what *actually* happened, independent of whether you traded it:

- **Path realized** — did price take the predicted route, or a different one? (Store the actual path as a compressed sequence.)
- **MFE / MAE** — maximum favorable & adverse excursion before resolution.
- **Resolution** — hit TP1 / TP2 / stop / invalidation-first / timed-out / still-open.
- **Time-to-outcome** — vs the predicted horizon.
- **Mechanism verdict** — did the *reason* hold? (e.g., predicted "liquidity sweep then reversal"; did a sweep actually occur?) This is graded by a small LLM judge reading the tape against the thesis, because *why it moved* is the lesson, not just *that* it moved.

This tracker is the reinforcement signal generator. Because gold runs ~23h/day, labels arrive continuously and fast — most intraday signals resolve within minutes to hours, so the learner sees thousands of graded predictions per week. **This is exactly why your 24/7 framing makes the system feasible: the label supply is enormous.**

## II.7 · The Meta-Learner — the engine that improves (RL core)

The meta-learner reads graded outcomes and closes three loops. It deliberately separates *cheap, safe, fast* adaptation from *powerful, slow, risky* adaptation.

**Loop 1 — Rank the seats (fast, safe).** Update each seat's Elo/score from every graded prediction using a reward that prices calibration first:

```
reward = w1 · calibration_term        (Brier/log-loss on conviction vs outcome)
       + w2 · direction_correct
       + w3 · path_match_quality       (predicted route vs realized route)
       + w4 · risk_adjusted_R          (realized R multiple, MFE/MAE aware)
       − w5 · mechanism_was_wrong      (penalty for right-for-wrong-reasons)
```

Note the **mechanism penalty**: a seat that wins by luck while its stated reason was false is *punished*, because unexplained wins rot the learner. This is the mathematical expression of "don't be lazy."

**Loop 2 — Tune the parameter policy (medium).** A **contextual bandit / policy model** learns, per market regime, the best settings for stop distance, TP placement, entry offset, and horizon — the "readjust SL/TP/entry/exit over time" you asked for. State = regime features (volatility band, session, RVOL regime, trend/chop tag). Action = parameter set. Reward = realized risk-adjusted outcome. Start conservative (small nudges around structure-derived levels), widen the search only as evidence accumulates. **Parameters are suggested by the policy but always anchored to structure** — the policy tunes *within* structural bounds, it never invents a stop in mid-air.

**Loop 3 — Evolve the roster (slow, powerful).** Periodically retrain/fine-tune seat models on the accumulated Ledger, spawn challenger variants, run promotion/relegation. All roster changes run **shadow/paper first** on live data and must beat the incumbent out-of-sample before taking live shelf space. Champion/challenger discipline prevents a good live seat from being replaced by an overfit lab model.

**Regime awareness.** A cluster model tags the current regime (trending vs mean-reverting, high vs low vol, London/NY/Asia session). Everything above is conditioned on regime, so the system stops applying range logic to a trend day — the classic silent killer of "it worked last month" (bottleneck #4).

## II.8 · Telegram delivery (wired later)

Delivery is a thin publisher subscribing to the Arbiter's published-signal stream. A Telegram bot (BotFather token → chat/channel) posts a compact card: direction, entry, stop, TP1/TP2, conviction, horizon, and the 2–4 sentence thesis so you get the *why* at a glance. Buttons for **Taken / Skipped** let you tag reality; your tags become an extra feedback channel (did the human agree, and were you right to?). Delivery is decoupled so the brain never blocks on the messenger, and a Telegram outage never drops a Ledger entry — signals are journaled first, sent second.

---

# PART III — THE REASONING LEDGER (journal every thought, not just numbers)

You were explicit: *not just schemas and figures — the literal thought process behind every signal.* The Ledger below stores both. `reasoning` and `mechanism` are free-text captures of the strategist's actual argument; the rest is structured so the learner can compute on it. One row is written per proposal at emission (`t_signal`) and *amended once* at resolution (`t_resolved`) — never edited otherwise.

```jsonc
{
  "signal_id": "uuid",
  "t_signal": "2026-08-01T13:42:07.412Z",   // UTC, ms precision
  "seat": { "id": "B", "lens": "Divergence Hunter", "rank_at_emit": 1, "elo": 1642 },
  "instrument": "XAUUSD",
  "regime_at_emit": { "trend": "up", "vol_band": "high", "session": "NY", "rvol": 1.8 },

  // ── THE DECISION ──────────────────────────────────────────────
  "direction": "short",
  "conviction": 68,                          // calibrated probability, 0–100
  "entry": 2418.40, "entry_type": "limit_on_reject",
  "stop": 2423.10, "stop_reason": "above the swing high that formed the bearish divergence; break = thesis dead",
  "targets": [
    { "px": 2411.00, "label": "TP1", "reason": "VWAP mean" },
    { "px": 2405.50, "label": "TP2", "reason": "prior demand + -2σ band" }
  ],
  "expected_horizon_min": 45,

  // ── THE THOUGHT PROCESS (verbatim, human-readable) ────────────
  "predicted_path": "Expect a marginal new high into 2419 sweeping stops, ADL fails to confirm, sharp rejection, lose VWAP, then flush to 2411.",
  "mechanism": "Regular bearish divergence: price higher-high at 2418.9 vs ADL lower-high. Buyers spent effort (RVOL 1.8) with no result — absorption. Late longs above VWAP are trapped and must cover.",
  "invalidation": "A 1m close back above 2419.2 on EXPANDING delta (not a wick) = buyers not trapped; thesis void BEFORE stop.",
  "thesis": "Momentum stopped confirming the rally. Effort without result at the highs = distribution. Fade the trap back to fair value.",
  "counter_case": "If this is a strong-trend day, divergence gets run over; that's why conviction is 68 not 85.",

  // ── FEATURE SNAPSHOT (what the seat actually saw) ─────────────
  "feature_ref": "frame://2026-08-01T13:42:07/v3",  // pointer to immutable FeatureFrame
  "feature_digest": { "adl_slope": -0.4, "price_slope": 0.6, "dist_vwap_sigma": 1.9, "div_type": "regular_bearish" },

  // ── PUBLICATION ───────────────────────────────────────────────
  "published": true, "confluence_with": ["seat_A_short_2418"], "human_tag": null,  // "taken"|"skipped" later

  // ── OUTCOME (amended once at resolution) ──────────────────────
  "outcome": {
    "t_resolved": "2026-08-01T14:19:55Z",
    "resolution": "tp1_hit",                 // tp1|tp2|stop|invalidation_first|timeout|open
    "path_realized": "sweep_to_2419.1 -> reject -> lost_vwap -> 2411",
    "path_match": 0.86,                       // 0–1, judged vs predicted_path
    "mfe_R": 1.9, "mae_R": -0.3, "realized_R": 1.5,
    "time_to_outcome_min": 37,
    "mechanism_verdict": "confirmed",         // confirmed|partial|false  (was the REASON right?)
    "calibration_contribution": 0.71,
    "reward": 1.24,                           // fed to meta-learner
    "lesson_tag": ["absorption_at_highs", "divergence_worked_in_high_vol_NY"]
  }
}
```

**Why this shape wins.** The learner can now answer questions no win/loss log can: *Which mechanisms actually hold in high-vol NY sessions? Does Seat B's path prediction accuracy justify its conviction? When seats disagree, who's right and under what regime? Which lessons repeat?* The `mechanism_verdict` and `path_match` fields are what let the system learn **why price goes where it goes** — your core requirement — instead of just tallying hits. Every `lesson_tag` becomes a searchable, aggregatable unit of hard-won knowledge.

A parallel **daily journal** is auto-written each session close: signals fired, hit-rate, calibration curve, best/worst reasoned calls, regime summary, thin-day explanations, and the meta-learner's changes that day (which seat gained rank, what parameters shifted, any relegation/promotion). This is the human-readable diary of the machine's growth.

---

# PART IV — BOTTLENECK MAP (self-diagnosing, with prescribed fixes)

You asked the system to *pinpoint its own bottlenecks and already carry the solution.* Each bottleneck below ships with a live **detector** (a metric the system watches on itself) and a **prescribed response**. When a detector trips, the daily journal raises it by name with the fix attached — the system tells *you* what's wrong and what it's doing about it.

| # | Bottleneck | Live detector (self-watch) | Prescribed solution (carried in advance) |
|---|---|---|---|
| 1 | **Data latency / gaps** | tick inter-arrival p99, gap count/hr | Buffer + interpolate flagged; quarantine late ticks; failover to secondary feed; pause emissions during blackout rather than trade blind |
| 2 | **Spot volume is tick-volume, not real volume** | correlation of tick-vol vs GC futures vol | Use tick-vol as proxy (well-validated for FX/gold); optionally fuse COMEX GC volume as ground truth; learner weights volume features by measured reliability |
| 3 | **Overfitting / curve-fit edges** | live vs backtest performance gap; param-churn rate | Walk-forward + purged K-fold CV; champion/challenger shadow test; penalize complexity; require out-of-sample win before promotion |
| 4 | **Regime change ("worked last month")** | rolling calibration decay; regime-cluster shift | Regime-conditioned models; auto-down-weight seats whose calibration decays; alert + revert to conservative params on regime break |
| 5 | **Look-ahead / label leakage** | audit: any feature using future bars? | Strict point-in-time FeatureFrames; outcome tracker sealed from feature engine; automated leakage unit tests in CI |
| 6 | **Feature-schema drift** | FeatureFrame version vs Ledger version mismatch | Version every frame; learner refuses to mix versions; migration re-labels or quarantines old rows |
| 7 | **Chronic undershoot of ≥5 signals/day** | 7-day rolling signal count vs target | Diagnose cause: too-strict gates? dead regime? Loosen *pre-filter sensitivity* (not the quality bar), add a lens tuned to the prevailing regime, or accept + log that conditions were genuinely thin |
| 8 | **Reward hacking / miscalibration** | Brier score, over/under-confidence histogram | Calibration-first reward; isotonic/Platt recalibration layer; punish confident-wrong harder than uncertain-wrong |
| 9 | **Class imbalance (few signals, many stand-downs)** | positive-rate per seat | Learn from stand-downs too (were they correct silences?); focal-style weighting; the Ledger stores non-signals as data |
| 10 | **Compute cost at 1 Hz × N seats** | $/day, LLM calls/hr, queue depth | Cheap code pre-filter gates the expensive LLM; batch inference; cache identical frames; escalate model size only on flagged candidates |
| 11 | **Model/concept drift over months** | slow decline in path_match & reward | Scheduled retrain on rolling window; auto-spawn challengers; retire stale lenses via relegation |
| 12 | **Telegram outage / rate limits** | delivery ack failures | Journal-first, send-second; retry queue; batch; never let delivery block or drop a Ledger write |
| 13 | **Single-model laziness / mode-collapse** | inter-seat correlation; disagreement rate | The Colosseum itself is the fix; if correlation → 1, force-mutate a lens and reward distinct-correct calls |
| 14 | **Broker/execution slippage vs signal price** | fill-vs-signal delta (once live) | Model slippage into reward; widen entry to trigger-zones not exact prices; track paper-vs-real divergence |

---

# PART V — SYSTEM-LEVEL FAILURE MODES + RESISTANCE

Beyond the per-model failure modes baked into the prompt (§I), the *system* has its own ways to fail. Each is named and countered by design:

**Poisoned learner.** One hallucinated feature or a leaked future bar silently corrupts thousands of labels. *Resistance:* point-in-time sealing, immutable frames, CI leakage tests, and grounding constraints in the prompt — the learner only ever trains on verifiably real, verifiably past data.

**Consensus collapse.** The three seats drift into agreement and the "competition" becomes theater. *Resistance:* the Arbiter rewards distinct-correct calls, the meta-learner monitors inter-seat correlation and force-mutates a lens if diversity dies.

**Silent quality erosion.** Win rate looks fine while *reasoning* rots (right for wrong reasons). *Resistance:* the mechanism_verdict penalty — the system explicitly grades *why*, and a seat winning on luck loses rank anyway.

**Quota corruption.** Pressure to hit 5/day manufactures noise. *Resistance:* the target is an ensemble goal, undershoot is logged not forced, and forcing is a punished failure mode in every seat's prompt.

**Overfit champion swap.** A lab-tuned model that shone in backtest replaces a solid live seat and then fails. *Resistance:* champion/challenger — nothing takes live shelf space without beating the incumbent out-of-sample on live-shadow data first.

**The final forcing function (system contract).** The engine is not "done" for a given day until: every signal has a complete, falsifiable reasoning trace; every resolved signal has a mechanism verdict; the calibration curve is logged; any tripped bottleneck is surfaced with its fix; and the daily diary explains what the machine learned. If any of those is missing, the day is incomplete — same standard the strategists hold themselves to, applied to the whole organism.

---

# PART VI — BUILD ROADMAP (how this actually gets stood up)

**Phase 0 — Foundations (skeleton that journals).** Feed ingest for XAU/USD + multi-timeframe aggregation; the FeatureFrame engine with all five families and the math in §II.2; the immutable Ledger + Outcome Tracker. *Milestone:* the system watches gold live and journals what it sees, with zero signals yet. Prove the eyes and memory before the mind.

**Phase 1 — One strategist, paper only.** Wire Lens A's code pre-filter + LLM strategist to emit full Ledger entries on paper. Outcome tracker grades them. *Milestone:* a week of graded, fully-reasoned paper signals; inspect calibration and path_match by hand. Are the reasons any good?

**Phase 2 — The Colosseum.** Add Lenses B and C, the Arbiter, ranking/relegation, confluence logic. *Milestone:* three rivals competing, a live league table, ≥5 quality signals/day emerging from the ensemble (or an honest logged reason why not).

**Phase 3 — The meta-learner.** Loops 1–3: calibration-first ranking, the parameter-tuning bandit (adaptive SL/TP/entry/exit), regime conditioning, challenger spawning. *Milestone:* measurable improvement in rolling calibration and risk-adjusted R over Phase 2 — the system is provably learning.

**Phase 4 — Telegram + you in the loop.** Delivery bot, Taken/Skipped tagging, the daily diary. *Milestone:* you receive clean, reasoned signals and your tags feed back as data.

**Phase 5 — Hardening.** Turn on every bottleneck detector, CI leakage tests, failover feeds, cost controls, champion/challenger gating. *Milestone:* the system diagnoses and reports its own limits — and keeps compounding, forever.

---

### One honest note (because the prompt demands intellectual honesty)

"Failure is not an option" is the right *engineering* standard — build so no *preventable* failure slips through, which is exactly what the bottleneck map and immutable-ledger discipline enforce. But in *markets*, individual losing signals are not failures; they are the training data. The system's job is not to never be wrong — that's impossible and any system claiming it is lying to you. Its job is to be **honestly calibrated, always improving, and never lazy**: to lose small and reasoned, win larger and explained, and understand *why* every single time. That is the failure that is not an option here — the failure to learn. This architecture is built so that one cannot happen quietly.


