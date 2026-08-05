# WHERE ALPHA ACTUALLY COMES FROM

Strategy notes for the Colosseum. Read this before adding features — it is the
argument for *why* the system is shaped the way it is.

---

## 1 · The correction: diversity is about ERRORS, not INPUTS

I previously told you the Colosseum's diversity problem was input collinearity —
three lenses reading transformations of the same price/volume series — and that
the fix was a fourth, more orthogonal data source. **That was wrong**, or at
least it identified the wrong binding constraint.

Ensemble theory is clear: what matters is **error decorrelation**, not input
decorrelation. Two models sharing every input can have completely independent
errors if they answer different questions. Two models with disjoint inputs can be
near-identical if they answer the same one.

The three original lenses correlate because all three ask: *"is there a trade
right now?"* — same question, same horizon, same objective. That is fixable
without a single new byte of data.

**The real diversity axes are `horizon × objective × regime`.** A 5-minute
reversal specialist and a 4-hour continuation specialist have structurally
decorrelated errors on identical tape. Encoded in `foundry/grammar.py`: every
lens must declare a `Horizon`, an `Objective`, and a `RegimeFilter`. Two lenses
that never fire at the same time cannot have correlated errors — that is the
cheapest diversity available and it costs nothing.

## 2 · Orthogonality as an admission requirement, not a monitored symptom

v1 had a `consensus_collapse` detector watching inter-seat correlation. That is a
smoke alarm — it tells you the house is already burning.

`foundry/behavior.py` inverts it. Every candidate lens is embedded as its
**activation vector** — what it did, bar by bar, in `{-1, 0, +1}` — and must be
provably distant from every incumbent on three metrics (cosine, Jaccard on firing
times, conditional agreement when both fire) before it can take a seat.

**You cannot collapse into a clone if a clone is structurally inadmissible.**

Verified behaviour: a 97% clone is rejected. A *perfectly inverted* lens is also
rejected — anti-correlation is still redundancy. A lens with disjoint firing
times is admitted even when built from identical features.

Why this matters more than it looks: the arbiter treats two seats agreeing as
*independent corroboration* and raises conviction. If those seats are secretly
the same lens, the engine systematically over-bets exactly where it is least
diversified. **False confluence is how an ensemble kills you.**

And it turns diversity from a constraint into a *search direction* —
`coverage_gaps()` finds the windows no incumbent covers, and the generator aims
there instead of firing blind.

## 3 · The real defense: deflation

Automated lens generation without multiple-testing correction is a machine for
manufacturing confident garbage. The numbers are brutal:

| Strategies searched | Best Sharpe from PURE NOISE |
|---|---|
| 10 | 1.57 |
| 100 | 2.53 |
| 1,000 | 3.26 |
| 10,000 | 3.86 |

The maximum of N draws is an **order statistic**, not an estimate. This is why
most marketed strategies fail live — nobody deflates for the search.

`learn/deflated.py` implements the Bailey/López de Prado defenses: Deflated
Sharpe (corrected for trials, skew, kurtosis), Probability of Backtest
Overfitting via CSCV (which tests the *selection process*, not any one
strategy), and Minimum Backtest Length. Verified: the luckiest of 1,000 noise
strategies is rejected; PBO cleanly separates noise pools from genuinely-spread
ones.

**`Foundry.trials_evaluated` is sacred.** It only ever increases. Resetting it,
or counting only survivors, turns this entire apparatus into theatre.

In the demo run: **1 lens admitted out of 452 trials.** That selectivity *is* the
product.

## 4 · Where edge actually comes from

Honest taxonomy, and what is reachable for a solo operator on gold with OHLCV:

| Source | Reachable? | Notes |
|---|---|---|
| **Information** (news, flow, alt data) | ✗ | excluded by your constraint |
| **Speed** (latency arb) | ✗ | not accessible |
| **Structure / forced flow** | ✓✓ | participants who MUST trade for non-price reasons leave footprints |
| **Risk premium** (liquidity provision) | ✓ | underexploited; the mean-reverter is a crude version |
| **Behavioral / positioning** | ✓✓ | trapped traders, stop clusters, herding |
| **Modeling** | ✓ | better inference from identical public data |

The three reachable ones, concretely:

**Forced flow (#3)** — the London AM/PM gold fixes, the Asia→London handover,
the London/NY overlap, COMEX settlement. Nobody needs to know *why* someone must
trade at 15:00 London to observe that they reliably do. This is why
`features/seasonality.py` exists, and why I think **intraday seasonality is the
single most underexploited real edge inside your constraint.** The volatility
profile alone is directly actionable: a stop sized for the NY overlap is far too
tight for Asia.

That module refuses to hand you "hour 13 is bullish" without passing both
Benjamini-Hochberg correction across all 24 buckets *and* a stability test across
non-overlapping halves. A pattern strong in one half and absent in the other is
reported as **UNSTABLE**, not as an edge.

**Positioning (#5)** — liquidity sweeps. Equal highs/lows are magnets; the sweep
and reversal is the moment a cohort gets trapped. Already in `structure.py`, and
`sweep_clustering` shows *when* it concentrates.

**Modeling (#6)** — meta-labeling, volume profile, dollar bars, calibration.

## 5 · The Foundry loop

```
GENERATE → BEHAVIOR GATE → STATISTICAL GATE → PARETO ARCHIVE → SEAT
```

Order matters. The behavior gate is O(archive) and cheap; the statistical gate is
O(bars) and expensive. Running behavior first means the expensive test only ever
sees candidates that would actually add something — and a clone never even costs
a trial.

Generation is three-way: seeded random (constrained by objective→feature priors,
because unconstrained search burns its whole budget on incoherent hypotheses),
evolutionary (mutation/crossover aimed at coverage gaps), and **LLM-proposed**.

The LLM division of labour is the interesting part: **the model proposes, the
statistics dispose.** An LLM is genuinely good at "here is an uncovered market
condition, what mechanism might exist there" and genuinely bad at knowing whether
it is real. Deflated Sharpe does not care how eloquent the hypothesis was.

The Pareto front over (edge, novelty, robustness, coverage) is deliberate. A
single leaderboard keeps four variations of the best idea. The front keeps the
mediocre lens that is the *only* thing covering low-vol Asia — which is exactly
the member that makes the ensemble worth having.

**Demonstrated:** planted `dist_sigma > 1.5 AND rvol < 0.9 → SHORT`. The Foundry
independently discovered `rvol < 1.005 AND rsi crosses_above 69.12 → SHORT` — the
same mechanism through a different feature (since `rsi = 50 + dist_sigma×12`) —
at 69% win rate, surviving deflation against 452 trials.

---

# MIRROR — reverse-engineering signal channels

A separate project, and a legitimately good idea. Framed correctly it is a
**behavioral cloning / inverse reinforcement learning** problem: observe an
expert's actions, infer the policy that generated them.

## Do this in order. Do not skip step 1.

**1 · Measure whether it is actually profitable.** Before any cloning effort,
replay every posted signal against *your own* tick data with *your* spread and
slippage. Published records are unreliable for structural reasons that need not
involve dishonesty: survivorship (losers deleted), selective reporting, break-even
laundering (an SL moved to entry after the fact stops being a loss), no cost
model, floating losers held for weeks as "still running", and martingale grids
that look brilliant until once.

`mirror/evaluate.py` does this conservatively — stop assumed before target when a
bar spans both, unfilled signals recorded rather than discarded, costs charged
both ways. It reports expectancy per signal net of cost and flags all of the above.

**If the answer is "not profitable," you have saved the entire project.** That is
a successful outcome, not a failed one.

**2 · Recover the level rulebook.** `mirror/provenance.py`. For each signal,
compute every candidate anchor from the tape at post time — swing highs, VWAP
bands, session extremes, prior day levels, VPOC, ATR multiples, round numbers,
Fibonacci levels — then find which anchor best explains the level they posted.

The key methodological point, learned by getting it wrong first: **rank by offset
CONSISTENCY, not proximity.** A tight tolerance can only discover rules with zero
offset ("stop sits *on* the swing high"). Real desks place stops *beyond*
structure. The true anchor is the one whose offset barely varies across hundreds
of signals. Proximity finds coincidences; stability finds rules.

Verified: given a planted `stop = swing_high + 0.30 ATR`, it recovers exactly
that, ranked first, with offset sd 0.00 — and separately identifies `tp1` as a
fixed 1.49R rule.

**3 · Clone the trigger.** Once levels are explained, train a classifier: given
market state, will they post in the next N minutes and in which direction? Decent
AUC means you have cloned the entry logic. This is where the tick archive earns
its keep.

**4 · Use it as a foil.** Their signals become a benchmark and a feature. Agreement
with your engine is confluence. If the cloned policy independently survives the
Foundry's deflation gates, it earns a seat like anything else — no special
treatment for being someone else's idea.

## Notes specific to your situation

Gold on weekdays, BTC on weekends is genuinely useful structure: BTC weekend
sessions are thin and mean-reverting in a way weekday gold is not, so **evaluate
the two regimes separately.** A channel that is profitable overall may be carried
entirely by one of them, and pooling would hide that.

High post frequency is a double-edged finding — it gives you sample size fast
(good for statistics) but also means correlated clustered exposure. Run
`detect_martingale()` early: per-signal statistics badly understate risk when the
real position is a whole cluster held at once.

Capture messages **in real time**. Deleted and edited messages are invisible
retroactively, and that is precisely the survivorship channel that makes records
look better than the trading.

---

## What I would do next, in priority order

1. **Bootstrap on real archived gold ticks.** Everything above is verified against
   synthetic tape and planted edges. Nothing is validated against real gold yet.
2. **Run the seasonality engine over 2+ years.** Cheapest real edge available, and
   the volatility profile immediately improves stop and target placement.
3. **Start MIRROR at step 1 only.** Measure the channels honestly. That single
   number decides whether steps 2–4 are worth any effort.
4. **Let the Foundry run on real data** with a large trial budget, and expect it
   to admit *almost nothing*. That is correct behaviour. One genuine lens per
   thousand trials is a good outcome.
5. **Only then** worry about whether the ensemble is big enough.

The failure mode I would watch for is not technical. It is deciding that the gates
are too strict because they keep rejecting things. They are calibrated to reject
things. That is what they are for.
