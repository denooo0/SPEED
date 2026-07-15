# Power of 3 (PO3) — Research & Mechanism Extraction

> Source: ICT (Inner Circle Trader) framework, distilled from the public
> body of work. Hype stripped. Only the falsifiable mechanism retained.
> Status: candidate for backtesting against real XAUUSD history.

---

## The core claim (in one sentence, nouns only)

Within a dealing period, price delivers in three phases — **Accumulation →
Manipulation → Distribution (AMD)** — where the manipulation leg sweeps
liquidity in the *opposite* direction of the period's true move before the
distribution leg expands in the intended direction.

This is also called the **Market Maker Model (MMXM)** and, at the daily
scale, the "daily candle theory."

---

## The mechanism, phase by phase

### Phase 1 — Accumulation
- Price consolidates in a range near the period open.
- Both sides build positions. Volume is unremarkable.
- On the daily frame: often the Asian session (roughly 20:00–00:00 EST) —
  a tight range that becomes the reference liquidity pool.

### Phase 2 — Manipulation (the "Judas swing")
- A false move OUT of the accumulation range, in the *wrong* direction.
- Its job: run stops. Sweep buy-side liquidity (equal highs, breakout-buyer
  stops) if the true move is DOWN; sweep sell-side liquidity (equal lows,
  breakout-seller stops) if the true move is UP.
- On the daily frame: often the London session (roughly 02:00–05:00 EST).
- The trapped party: breakout traders who entered on the false move. This is
  literally the mandate's "name the trapped party" made concrete.

### Phase 3 — Distribution
- The real expansion, in the intended direction, away from the manipulation.
- On the daily frame: often the NY session (roughly 07:00–10:00 EST, the "NY
  killzone"), running toward the opposing liquidity / the daily close.

---

## Key reference points ICT attaches to PO3

- **Midnight NY open (00:00 EST)** — the "true day open." Price relative to
  this line is claimed to bias the day's direction.
- **London killzone (02:00–05:00 EST)** — highest probability window for the
  manipulation leg.
- **NY killzone (07:00–10:00 EST)** — highest probability window for the
  distribution leg.
- **Judas swing** — the manipulation leg specifically, named for betrayal
  (fakes out traders before the real move).

---

## How PO3 maps onto ATLAS's four lenses

| PO3 element | ATLAS lens | Existing code that touches it |
|---|---|---|
| Manipulation sweep | **Flow** (Lens 1) | `WSBuffer` sweep detection; `order_flow.py` |
| Accumulation range | **Structure** (Lens 2) | `structure.py` dealing range, swing points |
| Killzone timing | **Context** (Lens 3) | `context.py` session classification |
| Trapped breakout traders | The mandate's core question | `validate_take` requires a named trapped party |

PO3 is not a competing framework. It is a **sharper vocabulary for the
manipulation → distribution transition** ATLAS already hunts. It adds
*timing precision* (killzones) our first version lacked.

---

## Testable hypotheses (each backtestable with the Phase 0 harness)

### H-PO3-1: Killzone manipulation → distribution
**Claim:** When the London session (02:00–05:00 EST) sweeps the Asian-range
high or low, the NY session (07:00–10:00 EST) tends to move in the opposite
direction of the sweep.
**Falsifier:** NY session continues in the sweep's direction ≥ 50% of the time.
**Features:** Asian range high/low, London sweep boolean + direction, NY
session return sign.
**Data needed:** XAUUSD M5/M15 with EST session tagging, ≥ 3 years.

### H-PO3-2: Midnight-open directional bias
**Claim:** XAUUSD's position relative to the 00:00 EST open at the start of
the NY killzone predicts the sign of the day's close-vs-open.
**Falsifier:** No better than 50/50 predictive power out-of-sample.
**Features:** 00:00 EST open price, price at 07:00 EST, daily close.

### H-PO3-3: Judas-swing fade
**Claim:** Fading the London-session extreme (entering opposite the sweep,
after a CHoCH back into the range) produces positive expectancy with the
stop beyond the swept extreme.
**Falsifier:** Expectancy ≤ 0 after realistic costs (11bp round-trip).
**Features:** London extreme, CHoCH confirmation, entry on reclaim, stop
beyond extreme, target = opposing session liquidity.
**This is the closest PO3 setup to the operator's own demonstrated trades.**

---

## Honest caveats (the anti-hype section)

1. **ICT concepts are widely known → crowded.** If PO3 were a clean printing
   press, the edge would be arbitraged. What survives is likely *conditional*
   edge — specific sessions, specific regimes, specific liquidity conditions.
   The backtest's job is to find WHERE it holds, not whether it "works."

2. **Session-time definitions are contested.** "London killzone" boundaries
   vary by source. We fix ONE definition in code, log it, and don't fish
   across variants (that's PBO-inflating over-fitting).

3. **Survivorship in the teaching.** ICT examples are hand-picked winners.
   The backtest with realistic costs is the only honest judge. We expect the
   real hit rate to be well below the tutorials' implied rate.

4. **XAUUSD ≠ the instruments ICT usually demos (indices, FX majors).** Gold
   has its own session personality, war/macro premium, and CME-vs-spot
   structure. PO3 must be re-validated on gold specifically, not assumed.

5. **The manipulation leg is only obvious in hindsight.** Detecting it in
   real time (was that a Judas swing or the real move?) is the hard part —
   and exactly what the Flow + Structure lenses must resolve. If they can't
   distinguish the two live, the edge is untradeable regardless of backtest.

---

## Next actions

1. Promote H-PO3-3 (Judas-swing fade) to a hypothesis YAML and run it through
   `BacktestRunner.evaluate_hypothesis` once historical data is downloaded.
2. Add EST-session tagging to `context.py` (currently uses UTC session
   classification; PO3 needs killzone boundaries in EST).
3. Compare PO3 hit rate to the operator's manual "sell the retracement into
   the declining MA" trades — see `memory/setups/htf-retracement-continuation.md`.
   They may be the same edge described two ways.

---

## Source note

This document is distilled from the public ICT body of work (freely taught
across years of recorded material). It is a mechanism extraction, not an
endorsement. The operator supplied a video playlist for deeper extraction;
those transcripts could not be fetched from this remote environment (network
policy blocks YouTube — see `research/video_extraction_status.md`). This
document stands on the well-documented public framework and is sufficient to
begin backtesting.
