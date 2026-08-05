"""Advanced microstructure estimators — from OHLCV alone.

You constrained the engine to candles, volume, accumulation/distribution,
divergence and VWAP. That constraint is sound. But there is far more signal
recoverable from those inputs than ADL + RSI + VWAP bands, and this module
extracts it. Everything here is computed from OHLCV only — no order book, no
news, no external data. Nothing violates the constraint; it just mines it harder.

What this adds and why each one matters:

  AMIHUD ILLIQUIDITY   |return| / volume. Price impact per unit of volume. The
                       single best OHLCV-only proxy for "how hard is it to move
                       this market right now." High Amihud = thin book = your
                       stop is more likely to be swept by noise.

  KYLE'S LAMBDA        Regression of price change on SIGNED volume. Literally
                       the market's price-impact coefficient. Rising lambda
                       means informed flow is present — the single most useful
                       "is someone who knows something trading" signal available
                       without an order book.

  VPIN                 Volume-synchronized probability of informed trading.
                       Computed on VOLUME buckets, not time. Elevated VPIN
                       preceded the 2010 flash crash; it is a genuine toxicity
                       and regime-stress gauge.

  CORWIN-SCHULTZ       Effective spread estimated from high/low ranges alone.
                       Lets the engine detect spread widening even when the
                       broker feed only gives you trades — and cost modelling
                       that adapts to real conditions beats a fixed constant.

  ROLL SPREAD          Spread from serial covariance of returns. A second,
                       independent estimator; disagreement between Roll and
                       Corwin-Schultz is itself informative about regime.

  VOLUME PROFILE       VPOC, value area, high/low volume nodes. THE biggest
                       feature gap in v1: VWAP tells you the average price, the
                       profile tells you WHERE VOLUME ACTUALLY TRANSACTED. Price
                       returns to high-volume nodes and travels fast through low
                       volume ones. This is the map the auction actually leaves.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..core.ringbuf import Ring
from ..core.types import Bar


# ---------------------------------------------------------------- liquidity

class AmihudIlliquidity:
    """ILLIQ = mean(|return| / dollar volume). Higher = thinner market."""

    __slots__ = ("_r", "_prev_close")

    def __init__(self, window: int = 50):
        self._r: Ring[float] = Ring(window)
        self._prev_close: Optional[float] = None

    def update(self, b: Bar) -> float:
        if self._prev_close and self._prev_close > 0 and b.volume > 0:
            ret = abs(b.c - self._prev_close) / self._prev_close
            dollar_vol = b.volume * b.typical
            if dollar_vol > 0:
                self._r.push(ret / dollar_vol * 1e6)   # scaled for readability
        self._prev_close = b.c
        return self.value

    @property
    def value(self) -> float:
        return sum(self._r) / len(self._r) if len(self._r) else 0.0


class KyleLambda:
    """Price impact coefficient: Δp = λ · signed_volume.

    Estimated by rolling OLS through the origin, which is the correct form --
    zero net order flow should imply zero expected price change.
    """

    __slots__ = ("_num", "_den", "_win", "_prev_close")

    def __init__(self, window: int = 50):
        self._win: Ring[Tuple[float, float]] = Ring(window)
        self._prev_close: Optional[float] = None
        self._num = 0.0
        self._den = 0.0

    def update(self, b: Bar) -> float:
        if self._prev_close is not None:
            dp = b.c - self._prev_close
            signed_vol = b.delta if b.delta != 0 else (
                b.volume if b.c >= b.o else -b.volume)
            self._win.push((signed_vol, dp))
        self._prev_close = b.c
        num = sum(x * y for x, y in self._win)
        den = sum(x * x for x, _ in self._win)
        self._num, self._den = num, den
        return self.value

    @property
    def value(self) -> float:
        return (self._num / self._den) if self._den > 1e-12 else 0.0


class VPIN:
    """Volume-synchronized probability of informed trading.

    Sampled on equal-VOLUME buckets rather than time, which is the whole point:
    information arrives with volume, not with the clock. VPIN is the fraction of
    volume imbalance across a rolling set of buckets, in [0, 1]. Sustained high
    readings mean flow is one-sided and toxic.
    """

    __slots__ = ("bucket_size", "_buf_buy", "_buf_sell", "_buckets", "n_buckets")

    def __init__(self, bucket_size: float = 500.0, n_buckets: int = 50):
        self.bucket_size = bucket_size
        self.n_buckets = n_buckets
        self._buf_buy = 0.0
        self._buf_sell = 0.0
        self._buckets: Ring[float] = Ring(n_buckets)

    def update(self, b: Bar) -> float:
        buy = b.buy_vol if (b.buy_vol or b.sell_vol) else (
            b.volume if b.c >= b.o else 0.0)
        sell = b.sell_vol if (b.buy_vol or b.sell_vol) else (
            b.volume if b.c < b.o else 0.0)
        self._buf_buy += buy
        self._buf_sell += sell
        while self._buf_buy + self._buf_sell >= self.bucket_size:
            total = self._buf_buy + self._buf_sell
            scale = self.bucket_size / total
            fb, fs = self._buf_buy * scale, self._buf_sell * scale
            self._buckets.push(abs(fb - fs) / self.bucket_size)
            self._buf_buy -= fb
            self._buf_sell -= fs
        return self.value

    @property
    def value(self) -> float:
        return sum(self._buckets) / len(self._buckets) if len(self._buckets) else 0.0

    @property
    def ready(self) -> bool:
        return self._buckets.full


# ---------------------------------------------------------------- spread

class CorwinSchultz:
    """Effective spread from consecutive two-bar high/low ranges.

    Corwin & Schultz (2012). Negative estimates are set to zero, which is the
    standard treatment -- the estimator is noisy bar to bar and only meaningful
    smoothed.
    """

    __slots__ = ("_prev", "_r")

    def __init__(self, window: int = 20):
        self._prev: Optional[Bar] = None
        self._r: Ring[float] = Ring(window)

    def update(self, b: Bar) -> float:
        if self._prev is not None:
            h1, l1 = self._prev.h, self._prev.l
            h2, l2 = b.h, b.l
            if l1 > 0 and l2 > 0 and h1 > 0 and h2 > 0:
                beta = math.log(h1 / l1) ** 2 + math.log(h2 / l2) ** 2
                hi, lo = max(h1, h2), min(l1, l2)
                gamma = math.log(hi / lo) ** 2 if lo > 0 else 0.0
                k = 3 - 2 * math.sqrt(2)
                alpha = (math.sqrt(2 * beta) - math.sqrt(beta)) / k \
                    - math.sqrt(gamma / k) if beta >= 0 and gamma >= 0 else 0.0
                s = 2 * (math.exp(alpha) - 1) / (1 + math.exp(alpha))
                self._r.push(max(0.0, s) * b.c)   # convert to price units
        self._prev = b
        return self.value

    @property
    def value(self) -> float:
        return sum(self._r) / len(self._r) if len(self._r) else 0.0


class RollSpread:
    """Roll (1984): spread = 2·sqrt(-cov(Δp_t, Δp_{t-1})).

    Only defined when serial covariance is negative (bid-ask bounce). Positive
    covariance means trend dominates microstructure noise -- itself a useful
    read, so it is reported rather than hidden.
    """

    __slots__ = ("_d", "_prev_close")

    def __init__(self, window: int = 50):
        self._d: Ring[float] = Ring(window)
        self._prev_close: Optional[float] = None

    def update(self, b: Bar) -> float:
        if self._prev_close is not None:
            self._d.push(b.c - self._prev_close)
        self._prev_close = b.c
        return self.value

    @property
    def covariance(self) -> float:
        n = len(self._d)
        if n < 3:
            return 0.0
        m = sum(self._d) / n
        return sum((self._d[i] - m) * (self._d[i - 1] - m)
                   for i in range(1, n)) / (n - 1)

    @property
    def value(self) -> float:
        cov = self.covariance
        return 2.0 * math.sqrt(-cov) if cov < 0 else 0.0

    @property
    def trending(self) -> bool:
        """Positive serial covariance = momentum regime, not mean reversion."""
        return self.covariance > 0


# ---------------------------------------------------------------- profile

@dataclass
class ProfileState:
    vpoc: float                  # price with the most volume
    value_area_high: float
    value_area_low: float
    hvns: Tuple[float, ...]      # high-volume nodes: magnets
    lvns: Tuple[float, ...]      # low-volume nodes: price travels fast here
    dist_to_vpoc: float
    in_value_area: bool
    total_volume: float

    def to_dict(self) -> Dict[str, object]:
        return {"vpoc": round(self.vpoc, 2),
                "vah": round(self.value_area_high, 2),
                "val": round(self.value_area_low, 2),
                "dist_to_vpoc": round(self.dist_to_vpoc, 3),
                "in_value_area": self.in_value_area,
                "hvns": [round(x, 2) for x in self.hvns[:3]],
                "lvns": [round(x, 2) for x in self.lvns[:3]]}


class VolumeProfile:
    """Where volume ACTUALLY transacted — the auction's real map.

    VWAP gives you one number: the average. The profile gives you the shape.
    Price is drawn to high-volume nodes (unfinished business, fair value) and
    moves fast through low-volume nodes (nobody wants to trade there). Fading a
    2-sigma VWAP stretch INTO an LVN is a very different trade from fading it
    into an HVN, and v1 could not tell those apart at all.

    Bucketed by tick size; O(1) per bar update.
    """

    __slots__ = ("tick", "_hist", "_session_anchor", "value_area_pct")

    def __init__(self, tick: float = 0.10, value_area_pct: float = 0.70):
        self.tick = tick
        self.value_area_pct = value_area_pct
        self._hist: Dict[int, float] = {}
        self._session_anchor = 0

    def _bucket(self, price: float) -> int:
        return int(round(price / self.tick))

    def update(self, b: Bar, session_anchor_ns: int) -> Optional[ProfileState]:
        if session_anchor_ns != self._session_anchor:
            self._hist.clear()
            self._session_anchor = session_anchor_ns

        # Spread the bar's volume across its range. Without an intrabar
        # distribution this uniform split is the honest approximation; weighting
        # toward the close would smuggle in an assumption we cannot verify.
        lo, hi = self._bucket(b.l), self._bucket(b.h)
        n = max(1, hi - lo + 1)
        share = b.volume / n
        for k in range(lo, hi + 1):
            self._hist[k] = self._hist.get(k, 0.0) + share

        return self.state(b.c)

    def state(self, price: float) -> Optional[ProfileState]:
        if not self._hist:
            return None
        total = sum(self._hist.values())
        if total <= 0:
            return None

        vpoc_k = max(self._hist, key=self._hist.get)
        vpoc = vpoc_k * self.tick

        # Value area: expand outward from VPOC until value_area_pct is captured.
        target = total * self.value_area_pct
        acc = self._hist[vpoc_k]
        lo = hi = vpoc_k
        keys = self._hist
        while acc < target:
            below = keys.get(lo - 1, 0.0)
            above = keys.get(hi + 1, 0.0)
            if below == 0.0 and above == 0.0:
                break
            if above >= below:
                hi += 1
                acc += above
            else:
                lo -= 1
                acc += below

        vals = sorted(self._hist.values())
        if len(vals) >= 5:
            hi_thresh = vals[int(len(vals) * 0.85)]
            lo_thresh = vals[int(len(vals) * 0.15)]
        else:
            hi_thresh = lo_thresh = 0.0
        hvns = tuple(sorted((k * self.tick for k, v in self._hist.items()
                             if v >= hi_thresh > 0),
                            key=lambda x: abs(x - price))[:5])
        lvns = tuple(sorted((k * self.tick for k, v in self._hist.items()
                             if 0 < v <= lo_thresh),
                            key=lambda x: abs(x - price))[:5])

        return ProfileState(
            vpoc=vpoc, value_area_high=hi * self.tick,
            value_area_low=lo * self.tick, hvns=hvns, lvns=lvns,
            dist_to_vpoc=price - vpoc,
            in_value_area=(lo * self.tick) <= price <= (hi * self.tick),
            total_volume=total)


# ---------------------------------------------------------------- bundle

@dataclass
class MicroState:
    amihud: float
    kyle_lambda: float
    vpin: float
    corwin_schultz: float
    roll_spread: float
    roll_trending: bool
    profile: Optional[ProfileState]

    def to_dict(self) -> Dict[str, object]:
        return {"amihud": round(self.amihud, 6),
                "kyle_lambda": round(self.kyle_lambda, 8),
                "vpin": round(self.vpin, 4),
                "spread_cs": round(self.corwin_schultz, 4),
                "spread_roll": round(self.roll_spread, 4),
                "momentum_regime": self.roll_trending,
                "profile": self.profile.to_dict() if self.profile else None}

    def interpretation(self) -> List[str]:
        """Plain-English reads for the strategist prompt. Numbers the model
        cannot interpret are numbers the model will ignore."""
        out = []
        if self.vpin > 0.35:
            out.append(f"VPIN {self.vpin:.2f} — flow is one-sided and toxic; "
                       f"informed participants are likely active")
        if self.kyle_lambda > 0:
            out.append(f"Kyle lambda {self.kyle_lambda:.2e} — price impact per "
                       f"unit of signed volume is "
                       f"{'elevated: thin book, stops sweep easily' if self.kyle_lambda > 1e-3 else 'normal'}")
        if self.roll_trending:
            out.append("Serial covariance positive — MOMENTUM regime; fades are "
                       "structurally disadvantaged right now")
        else:
            out.append("Serial covariance negative — mean-reverting "
                       "microstructure; fades are structurally favoured")
        if self.profile:
            p = self.profile
            out.append(f"VPOC {p.vpoc:.2f} ({p.dist_to_vpoc:+.2f} away); price is "
                       f"{'INSIDE' if p.in_value_area else 'OUTSIDE'} the value "
                       f"area [{p.value_area_low:.2f}, {p.value_area_high:.2f}]")
            if p.lvns:
                out.append(f"Nearest low-volume node {p.lvns[0]:.2f} — price "
                           f"travels fast through it, poor place for a target")
            if p.hvns:
                out.append(f"Nearest high-volume node {p.hvns[0]:.2f} — magnet, "
                           f"good target, poor place for a stop")
        return out


class MicrostructureEngine:
    """All estimators, one update per closed bar, all O(1) or O(window)."""

    def __init__(self, tick: float = 0.10, vpin_bucket: float = 500.0):
        self.amihud = AmihudIlliquidity()
        self.kyle = KyleLambda()
        self.vpin = VPIN(bucket_size=vpin_bucket)
        self.cs = CorwinSchultz()
        self.roll = RollSpread()
        self.profile = VolumeProfile(tick=tick)
        self.last: Optional[MicroState] = None

    def update(self, b: Bar, session_anchor_ns: int) -> MicroState:
        st = MicroState(
            amihud=self.amihud.update(b),
            kyle_lambda=self.kyle.update(b),
            vpin=self.vpin.update(b),
            corwin_schultz=self.cs.update(b),
            roll_spread=self.roll.update(b),
            roll_trending=self.roll.trending,
            profile=self.profile.update(b, session_anchor_ns),
        )
        self.last = st
        return st
