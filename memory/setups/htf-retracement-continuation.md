# Setup: HTF Retracement Continuation (short bias)

> Operator-originated. Distilled from two live XAUUSD shorts (2026-07):
> the 4-lot @ 4128 (+€34k) and the 1-lot @ 4058 (+€2.6k floating).
> Status: candidate — awaiting backtest validation on real history.

## Hypothesis

In a higher-timeframe **downtrend**, a counter-trend push (retracement or
LTF CHoCH-up) that stalls at the **declining moving average** is more often
**exhaustion** (continuation down) than a genuine reversal. Sell the
rejection, in alignment with HTF bias.

Mirror image applies for uptrends (buy the retracement into a rising MA) —
but the operator's demonstrated edge is currently short-side, so we validate
that first.

## Lenses

- **Structure (primary):** HTF makes lower highs / lower lows; price below a
  declining MA. The counter-trend push forms a lower high.
- **Context:** HTF bias is the gate — only take continuations *with* the trend.
- **Flow:** ideally the push into the MA shows waning momentum / negative CVD
  divergence at the rejection.
- **Intent:** funding / COT alignment is a bonus, not required.

## Trapped party

Late longs who bought the retracement / CHoCH-up expecting a reversal. They
are caught when price rejects the MA and resumes the downtrend.

## Entry

- HTF (H1/H4) confirmed bearish: lower highs, price under a declining MA.
- Price retraces UP into the declining MA (or forms an LTF CHoCH-up).
- Sell the rejection — a bearish LTF candle closing back below the MA, or a
  failure to hold above the prior lower high.

## Stop / kill thesis

**Kill thesis:** if price closes back **above** the retracement high (the
lower high that was sold) on the analysis timeframe, the continuation thesis
is dead — the "exhaustion" was actually a reversal. Exit at market.

Stop placement: above the swept retracement high + buffer.

## Targets

- First target: the prior swing low (the low the retracement bounced from).
- Extended: next HTF liquidity pool / measured move.
- The 4-lot trade ran ~98 points; the operator trailed it rather than fixed-TP.

## Sizing

Conviction-scaled. The operator used 4 lots on the primary (high-conviction,
clean HTF structure) and 1 lot on the follow-up (lower-conviction second
bite). Formalize as fractional Kelly once edge is quantified — bigger size on
higher expected R, smaller on marginal continuations.

## Backtest plan

Run as hypothesis `H-CHOCH-EXHAUSTION` / `H-RETRACE-CONT` through
`BacktestRunner.evaluate_hypothesis` on ≥ 3 years of XAUUSD H1/M30 with
realistic costs. Measure:
- Hit rate of "fade the counter-trend push in a downtrend"
- R-distribution (the 4-lot trade was a fat right-tail winner — is that
  repeatable or was it a war-driven outlier?)
- Whether the CHoCH-up-as-exhaustion read beats treating CHoCH-up as reversal

Promotion gate as usual: PBO < 0.20, DSR > 0.95, WFE ≥ 0.5 on ≥ 70% folds,
≥ 200 OOS trades.

## Relationship to PO3

This setup is the **distribution leg** of Power of 3 seen from the trade
level: the retracement into the MA is (often) the tail end of a manipulation
leg, and the rejection is the start of distribution back in the trend
direction. See `research/po3_mechanism.md`. If the backtest confirms both,
they may be the same edge described at two scales.

## Tags

`#short` `#htf-downtrend` `#retracement` `#choch-exhaustion` `#continuation`
`#operator-originated` `#candidate`

## Provenance

- `memory/live_trades/2026-07-XAUUSD-short-4128.md`
- `memory/live_trades/2026-07-XAUUSD-short-4058.md`
