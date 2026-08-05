"""Deterministic synthetic gold tape + a mock strategist.

The generator is seeded and produces a market with real structure -- trends,
mean reversion, volatility clustering, volume that expands into moves and dries
up at exhaustion -- because a random walk cannot exercise the pattern logic and
would make every test vacuously pass.

The mock strategist stands in for the LLM running the Part I master prompt. It
constructs proposals with genuine reasoning fields so the whole pipeline
(journal narrative, path matching, mechanism grading) is exercised end to end.
"""
from __future__ import annotations

import math
from typing import Dict, Iterator, List, Optional

from ..arena.seat import Candidate, Strategist
from ..core.types import FeatureFrame, Proposal, Side, Target, Tick


class Xorshift:
    """Deterministic across machines and Python versions -- random.random() is
    not guaranteed stable, and replay verification depends on exactness."""

    def __init__(self, seed: int = 2026):
        self.s = seed & 0xFFFFFFFF or 1

    def u32(self) -> int:
        x = self.s
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        self.s = x & 0xFFFFFFFF
        return self.s

    def rand(self) -> float:
        return self.u32() / 0xFFFFFFFF

    def gauss(self) -> float:
        u1 = max(self.rand(), 1e-12)
        u2 = self.rand()
        return math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)


def synthetic_ticks(start_ns: int, n: int, seed: int = 2026,
                    px0: float = 2400.0) -> Iterator[Tick]:
    """Regime-switching tape with volatility clustering and structured volume."""
    r = Xorshift(seed)
    px = px0
    vol = 0.05
    trend = 0.0
    regime_left = 0
    for i in range(n):
        if regime_left <= 0:
            regime_left = 600 + int(r.rand() * 2400)
            k = r.rand()
            trend = 0.0015 if k > 0.66 else (-0.0015 if k < 0.33 else 0.0)
            vol = 0.03 + r.rand() * 0.09
        regime_left -= 1

        shock = r.gauss() * vol
        px += trend + shock
        # mean reversion to keep the series bounded and create fade setups
        px += (px0 - px) * 0.00004

        # volume expands with |move| and spikes occasionally (news-like bursts)
        base = 8.0 + abs(shock) / max(vol, 1e-9) * 6.0
        if r.rand() < 0.004:
            base *= 4.0 + r.rand() * 6.0
        volume = max(0.5, base + r.gauss() * 2.0)

        spread = 0.12 + (0.25 if r.rand() < 0.02 else 0.0)
        t_ns = start_ns + i * 1_000_000_000
        yield Tick(t_ns=t_ns, bid=px - spread / 2, ask=px + spread / 2,
                   volume=volume, t_ingest_ns=t_ns + 2_000_000)


class MockStrategist(Strategist):
    """Stands in for the LLM. Builds structurally-anchored levels tuned by the
    bandit's chosen arm, with reasoning text that matches the lens."""

    def __init__(self, seat_id: str, lens: str):
        self.seat_id = seat_id
        self.lens = lens

    def reason(self, f: FeatureFrame, c: Candidate,
               params: Dict[str, float]) -> Optional[Proposal]:
        atr = max(float(params.get("atr", 1.0)), 0.05)
        entry = f.mid
        buf = atr * float(params["stop_atr_mult"])
        long_ = c.bias is Side.LONG

        stop = (c.anchor_level - buf) if long_ else (c.anchor_level + buf)

        # Geometry sanity. If the structural anchor has ended up on the wrong
        # side of current price (price already ran past the level), there is no
        # valid trade here -- STAND DOWN rather than manufacture one. Forcing a
        # stop to the "correct" side would be inventing a level in mid-air,
        # which is exactly the failure the structure-anchored design forbids.
        if long_ and stop >= entry:
            return None
        if (not long_) and stop <= entry:
            return None
        risk = abs(entry - stop)
        if risk < atr * 0.15:                     # too tight to survive noise
            return None
        tp1 = entry + (1 if long_ else -1) * risk * float(params["tp1_r"])
        tp2 = entry + (1 if long_ else -1) * risk * float(params["tp2_r"])

        if c.trigger == "absorption":
            path = ("sweep the highs then reject on absorption, lose VWAP and "
                    "expand to target") if not long_ else \
                   ("sweep the lows then reject on absorption, reclaim VWAP and "
                    "expand to target")
            mech = (f"Effort/result {c.evidence.get('effort_result', 0):.2f} with "
                    f"RVOL {c.evidence.get('rvol', 0):.2f}: heavy volume, no "
                    f"progress. That is absorption; the aggressive side is "
                    f"exhausted and trapped.")
        elif c.trigger.endswith("_bearish") or c.trigger.endswith("_bullish"):
            path = ("reject at the divergent pivot, lose VWAP then trend to "
                    "target") if not long_ else \
                   ("reject at the divergent pivot, reclaim VWAP then trend to "
                    "target")
            mech = (f"{c.trigger} of strength {c.evidence.get('strength', 0):.3f} "
                    f"stacked across {int(c.evidence.get('stack', 1))} timeframe(s): "
                    f"momentum has stopped confirming price.")
        else:
            path = ("reject at the band then mean revert back to test VWAP")
            mech = (f"Price {c.evidence.get('dist_sigma', 0):+.2f} sigma from "
                    f"VWAP with RVOL {c.evidence.get('rvol', 0):.2f} -- stretched "
                    f"and no longer supported by participation.")

        return Proposal(
            seat_id=self.seat_id, lens=self.lens, direction=c.bias,
            conviction=min(0.95, 0.45 + c.score * 0.4),
            entry=round(entry, 2), stop=round(stop, 2),
            stop_reason=(f"beyond the {c.anchor_kind} at {c.anchor_level:.2f} "
                         f"plus {params['stop_atr_mult']}x ATR buffer; a close "
                         f"past it kills the thesis"),
            targets=(Target(round(tp1, 2), "TP1",
                            f"{params['tp1_r']}R -- first structural objective"),
                     Target(round(tp2, 2), "TP2",
                            f"{params['tp2_r']}R -- measured move completion")),
            predicted_path=path, mechanism=mech,
            invalidation=(f"a close back beyond {c.anchor_level:.2f} on expanding "
                          f"delta means the trapped side was not trapped; thesis "
                          f"void before the stop"),
            thesis=(f"{self.lens}: {c.trigger} at {c.anchor_level:.2f}. "
                    f"Fade the exhausted side back toward fair value."),
            counter_case=("If this is a strong-trend day the signal gets run "
                          "over, which is why conviction is capped here."),
            horizon_min=max(10, int(45 * float(params.get("horizon_mult", 1.0)))),
            frame_hash=f.frame_hash, t_ns=f.t_event_ns,
            prefilter_score=c.score)
