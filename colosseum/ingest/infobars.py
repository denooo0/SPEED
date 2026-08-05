"""Information-driven bars: volume, dollar, and imbalance bars.

This is a structural upgrade to the entire feature layer, and it is fully inside
your stated constraint — it is still just candles and volume, sampled smarter.

THE PROBLEM WITH TIME BARS. A 5-minute bar during the London open and a 5-minute
bar at 3am Asia contain wildly different amounts of information but are treated
as equal observations. The consequences are not cosmetic:

  * returns are far from normal (fat tails, heteroskedastic)
  * serial correlation is inflated
  * volatility estimates are biased
  * any learner trained on them over-weights dead periods and under-samples the
    moments that actually matter

THE FIX (López de Prado, *Advances in Financial Machine Learning*, ch. 2).
Sample by INFORMATION ARRIVAL instead of by clock:

  VOLUME BARS    close a bar every N contracts/units traded
  DOLLAR BARS    close a bar every $N of notional — the best of the three,
                 because it is robust to price level drifting over time (a
                 fixed volume threshold means something different at $1,800
                 gold than at $3,000 gold)
  IMBALANCE BARS close when signed order flow exceeds its own expectation —
                 these sample *precisely* at the moments informed flow arrives

Empirically these produce returns much closer to IID normal, which improves
every downstream estimator, every statistical test, and every model. Running the
existing lenses on dollar bars instead of time bars is one of the highest
leverage-per-line changes available in this whole system.

Time bars are kept — humans think in minutes and the Telegram card has to make
sense. But the LEARNER should see dollar bars.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from ..core.ringbuf import Ring
from ..core.types import Bar, Quality, Tick


class _InfoBarBuilder:
    """Shared accumulation logic; subclasses define when to close a bar."""

    __slots__ = ("history", "_open", "_o", "_h", "_l", "_c", "_v", "_n",
                 "_bv", "_sv", "_t_open", "_last_sign", "_acc", "label")

    def __init__(self, history: int, label: str):
        self.history: Ring[Bar] = Ring(history)
        self.label = label
        self._open = False
        self._o = self._h = self._l = self._c = 0.0
        self._v = self._acc = 0.0
        self._n = 0
        self._bv = self._sv = 0.0
        self._t_open = 0
        self._last_sign = 1

    def _threshold(self) -> float:
        raise NotImplementedError

    def _increment(self, t: Tick) -> float:
        raise NotImplementedError

    def update(self, t: Tick) -> Optional[Bar]:
        p = t.mid
        if not self._open:
            self._open = True
            self._t_open = t.t_ns
            self._o = self._h = self._l = self._c = p
            self._v = t.volume
            self._n = 1
            self._acc = self._increment(t)
            self._bv = t.volume if self._last_sign > 0 else 0.0
            self._sv = t.volume if self._last_sign < 0 else 0.0
            return None

        if p > self._h:
            self._h = p
        if p < self._l:
            self._l = p
        if p > self._c:
            self._last_sign = 1
        elif p < self._c:
            self._last_sign = -1
        self._c = p
        self._v += t.volume
        self._n += 1
        self._acc += self._increment(t)
        if self._last_sign > 0:
            self._bv += t.volume
        else:
            self._sv += t.volume

        if self._acc >= self._threshold():
            bar = Bar(
                t_open_ns=self._t_open, t_close_ns=t.t_ns,
                interval_s=0,                 # 0 marks a non-time bar
                o=self._o, h=self._h, l=self._l, c=self._c,
                volume=self._v, ticks=self._n,
                buy_vol=self._bv, sell_vol=self._sv,
                closed=True, quality=t.quality)
            self.history.push(bar)
            self._open = False
            self._acc = 0.0
            return bar
        return None

    def live(self) -> Optional[Bar]:
        if not self._open:
            return None
        return Bar(t_open_ns=self._t_open, t_close_ns=0, interval_s=0,
                   o=self._o, h=self._h, l=self._l, c=self._c,
                   volume=self._v, ticks=self._n, buy_vol=self._bv,
                   sell_vol=self._sv, closed=False)

    @property
    def progress(self) -> float:
        th = self._threshold()
        return min(1.0, self._acc / th) if th > 0 else 0.0


class VolumeBars(_InfoBarBuilder):
    __slots__ = ("threshold",)

    def __init__(self, threshold: float = 5000.0, history: int = 512):
        super().__init__(history, "volume")
        self.threshold = threshold

    def _threshold(self) -> float:
        return self.threshold

    def _increment(self, t: Tick) -> float:
        return t.volume


class DollarBars(_InfoBarBuilder):
    """The recommended default. Robust to price level drift over months/years."""
    __slots__ = ("threshold",)

    def __init__(self, threshold: float = 12_000_000.0, history: int = 512):
        super().__init__(history, "dollar")
        self.threshold = threshold

    def _threshold(self) -> float:
        return self.threshold

    def _increment(self, t: Tick) -> float:
        return t.volume * t.mid


class ImbalanceBars(_InfoBarBuilder):
    """Close when cumulative signed flow exceeds its rolling expectation.

    These sample exactly when order flow becomes one-sided — i.e. precisely at
    the moments the Wyckoff and divergence lenses care about. The threshold
    ADAPTS to recent conditions, so the bar rate stays roughly stable across
    regimes instead of collapsing in quiet markets.
    """
    __slots__ = ("_ewma_theta", "alpha", "_signed", "min_threshold")

    def __init__(self, initial_threshold: float = 2000.0, alpha: float = 0.05,
                 history: int = 512):
        super().__init__(history, "imbalance")
        self._ewma_theta = initial_threshold
        self.min_threshold = initial_threshold * 0.2
        self.alpha = alpha
        self._signed = 0.0

    def _threshold(self) -> float:
        return max(self._ewma_theta, self.min_threshold)

    def _increment(self, t: Tick) -> float:
        self._signed += t.volume * (1 if self._last_sign > 0 else -1)
        return abs(self._signed)

    def update(self, t: Tick) -> Optional[Bar]:
        bar = super().update(t)
        if bar is not None:
            realized = abs(self._signed)
            self._ewma_theta = ((1 - self.alpha) * self._ewma_theta
                                + self.alpha * realized)
            self._signed = 0.0
        return bar


@dataclass
class BarStats:
    """Diagnostics that let you TUNE the thresholds instead of guessing.

    Target roughly 50-100 bars per session: enough resolution to react, few
    enough that each carries real information.
    """
    count: int = 0
    total_seconds: float = 0.0
    mean_duration_s: float = 0.0
    min_duration_s: float = 0.0
    max_duration_s: float = 0.0

    @staticmethod
    def of(bars: List[Bar]) -> "BarStats":
        if not bars:
            return BarStats()
        durs = [(b.t_close_ns - b.t_open_ns) / 1e9 for b in bars
                if b.t_close_ns > b.t_open_ns]
        if not durs:
            return BarStats(count=len(bars))
        return BarStats(count=len(bars), total_seconds=sum(durs),
                        mean_duration_s=sum(durs) / len(durs),
                        min_duration_s=min(durs), max_duration_s=max(durs))


def normality_score(bars: List[Bar]) -> Dict[str, float]:
    """How close are this bar type's returns to IID normal?

    This is the empirical justification for the whole module: run it on time
    bars and on dollar bars over the same tape and compare. Lower |skew| and
    excess kurtosis nearer zero means better-behaved inputs, which means every
    downstream statistical claim is more trustworthy.
    """
    closes = [b.c for b in bars if b.c > 0]
    if len(closes) < 30:
        return {"n": len(closes), "skew": 0.0, "excess_kurtosis": 0.0,
                "jarque_bera": 0.0}
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1] > 0]
    n = len(rets)
    m = sum(rets) / n
    var = sum((r - m) ** 2 for r in rets) / n
    if var <= 0:
        return {"n": n, "skew": 0.0, "excess_kurtosis": 0.0, "jarque_bera": 0.0}
    sd = var ** 0.5
    skew = sum(((r - m) / sd) ** 3 for r in rets) / n
    kurt = sum(((r - m) / sd) ** 4 for r in rets) / n - 3.0
    jb = n / 6.0 * (skew ** 2 + kurt ** 2 / 4.0)
    return {"n": n, "skew": round(skew, 4),
            "excess_kurtosis": round(kurt, 4), "jarque_bera": round(jb, 2)}


class InfoBarEngine:
    """Runs all three bar types alongside the existing time bars."""

    def __init__(self, dollar_threshold: float = 12_000_000.0,
                 volume_threshold: float = 5000.0,
                 imbalance_threshold: float = 2000.0):
        self.dollar = DollarBars(dollar_threshold)
        self.volume = VolumeBars(volume_threshold)
        self.imbalance = ImbalanceBars(imbalance_threshold)

    def update(self, t: Tick) -> Dict[str, Optional[Bar]]:
        return {"dollar": self.dollar.update(t),
                "volume": self.volume.update(t),
                "imbalance": self.imbalance.update(t)}

    def diagnostics(self) -> Dict[str, object]:
        out: Dict[str, object] = {}
        for name, b in (("dollar", self.dollar), ("volume", self.volume),
                        ("imbalance", self.imbalance)):
            bars = b.history.to_list()
            out[name] = {"stats": BarStats.of(bars).__dict__,
                         "normality": normality_score(bars)}
        return out
