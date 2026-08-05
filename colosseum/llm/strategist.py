"""LLM strategist: Stage 2 of a seat.

Provider-agnostic by design -- you inject a `complete(system, user) -> str`
callable, so Anthropic, OpenAI, a local model, or a recorded fixture all work
identically. That matters for two reasons beyond convenience: the engine can be
REPLAYED against recorded completions (determinism), and a provider outage
degrades one seat rather than killing the engine.

Everything the model returns is treated as untrusted input. It is schema-checked,
range-checked, geometry-checked, and cost-checked before it is allowed to become
a Proposal. A model that hallucinates a stop on the wrong side of entry gets a
rejected proposal and a logged reason -- never a live signal.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from ..arena.seat import Candidate, Strategist
from ..core.types import FeatureFrame, Proposal, Side, Target
from .prompt import render_frame, system_prompt

CompleteFn = Callable[[str, str], str]


@dataclass
class LLMStats:
    calls: int = 0
    signals: int = 0
    stand_downs: int = 0
    schema_failures: int = 0
    validation_failures: int = 0
    timeouts: int = 0
    retries: int = 0
    total_latency_ms: float = 0.0
    est_cost_usd: float = 0.0
    failure_reasons: Dict[str, int] = field(default_factory=dict)

    def note(self, reason: str) -> None:
        self.failure_reasons[reason] = self.failure_reasons.get(reason, 0) + 1

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.calls if self.calls else 0.0


def extract_json(text: str) -> Optional[dict]:
    """Models wrap JSON in prose or fences despite instructions. Recover it.

    Tries strict parse, then fenced block, then the first balanced brace span.
    Being forgiving here is worth it: a perfectly good signal should not be lost
    to a stray ```json.
    """
    if not text:
        return None
    t = text.strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    start = t.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except Exception:
                    return None
    return None


class LLMStrategist(Strategist):
    def __init__(self, seat_id: str, lens: str, complete: CompleteFn,
                 *, max_retries: int = 1, deadline_ms: int = 3000,
                 cost_per_call_usd: float = 0.006,
                 min_conviction: float = 0.45,
                 max_conviction: float = 0.95,
                 lessons_provider: Optional[Callable[[], List[str]]] = None,
                 clock_ms: Optional[Callable[[], float]] = None):
        self.seat_id = seat_id
        self.lens = lens
        self._complete = complete
        self.max_retries = max_retries
        self.deadline_ms = deadline_ms
        self.cost_per_call_usd = cost_per_call_usd
        self.min_conviction = min_conviction
        self.max_conviction = max_conviction
        self.lessons_provider = lessons_provider
        self.stats = LLMStats()
        self._system = system_prompt(seat_id)
        # Injected so tests/replay control it; production passes wall clock.
        self._clock_ms = clock_ms or (lambda: time.monotonic() * 1000.0)
        self.last_stand_down: str = ""

    # ---- main entry ------------------------------------------------------

    def reason(self, f: FeatureFrame, c: Candidate,
               params: Dict[str, float]) -> Optional[Proposal]:
        lessons = self.lessons_provider() if self.lessons_provider else None
        user = render_frame(f, c, params, lessons)

        raw = self._call_with_retry(user)
        if raw is None:
            return None

        obj = extract_json(raw)
        if obj is None:
            self.stats.schema_failures += 1
            self.stats.note("unparseable_json")
            self.last_stand_down = "model returned unparseable output"
            return None

        action = str(obj.get("action", "")).lower().strip()
        if action == "stand_down" or not action:
            self.stats.stand_downs += 1
            self.last_stand_down = str(
                obj.get("stand_down_reason", "model stood down without a reason"))
            return None

        prop, err = self._validate(obj, f, c, params)
        if prop is None:
            self.stats.validation_failures += 1
            self.stats.note(err or "unknown")
            self.last_stand_down = f"rejected by validator: {err}"
            return None

        self.stats.signals += 1
        return prop

    def _call_with_retry(self, user: str) -> Optional[str]:
        for attempt in range(self.max_retries + 1):
            t0 = self._clock_ms()
            try:
                raw = self._complete(self._system, user)
            except Exception as e:
                self.stats.note(f"provider_error:{type(e).__name__}")
                if attempt < self.max_retries:
                    self.stats.retries += 1
                    continue
                return None
            dt = self._clock_ms() - t0
            self.stats.calls += 1
            self.stats.total_latency_ms += dt
            self.stats.est_cost_usd += self.cost_per_call_usd

            if dt > self.deadline_ms:
                # A late answer is about tape that has already moved. Publishing
                # it is worse than silence, so it is dropped, not salvaged.
                self.stats.timeouts += 1
                self.stats.note("deadline_exceeded")
                self.last_stand_down = (f"reasoning took {dt:.0f}ms > "
                                        f"{self.deadline_ms}ms deadline; tape stale")
                return None
            return raw
        return None

    # ---- validation: everything the model says is untrusted ---------------

    def _validate(self, o: dict, f: FeatureFrame, c: Candidate,
                  params: Dict[str, float]) -> Tuple[Optional[Proposal], str]:
        try:
            d = str(o.get("direction", "")).lower()
            if d not in ("long", "short"):
                return None, "bad_direction"
            side = Side.LONG if d == "long" else Side.SHORT

            conv = float(o.get("conviction", 0))
            if not (0.0 <= conv <= 1.0):
                return None, "conviction_out_of_range"
            conv = min(max(conv, self.min_conviction), self.max_conviction)

            entry = float(o["entry"])
            stop = float(o["stop"])

            # Sanity vs the actual tape: a level far from current price is a
            # hallucination, not a limit order.
            atr = max(float(params.get("atr", 1.0)), 0.01)
            if abs(entry - f.mid) > atr * 12:
                return None, "entry_detached_from_price"
            if abs(stop - entry) > atr * 20:
                return None, "stop_absurdly_far"

            if side is Side.LONG and stop >= entry:
                return None, "long_stop_above_entry"
            if side is Side.SHORT and stop <= entry:
                return None, "short_stop_below_entry"
            risk = abs(entry - stop)
            if risk < atr * 0.10:
                return None, "stop_too_tight_to_survive_noise"

            raw_t = o.get("targets") or []
            targets: List[Target] = []
            for t in raw_t[:3]:
                px = float(t["price"])
                # Targets must be in the trade's direction. A "target" behind
                # entry is a sign the model lost the plot.
                if side is Side.LONG and px <= entry:
                    return None, "long_target_below_entry"
                if side is Side.SHORT and px >= entry:
                    return None, "short_target_above_entry"
                targets.append(Target(round(px, 2),
                                      str(t.get("label", f"TP{len(targets)+1}")),
                                      str(t.get("reason", ""))[:300]))
            if not targets:
                return None, "no_targets"

            for k in ("mechanism", "invalidation", "predicted_path", "thesis"):
                if not str(o.get(k, "")).strip():
                    return None, f"missing_{k}"

            # The falsifiability test, enforced in code: an invalidation that
            # just restates the stop is not an early tell.
            inv = str(o["invalidation"]).strip()
            if len(inv) < 25 or inv.lower() in ("stop is hit", "stop hit",
                                                "the stop is hit"):
                return None, "invalidation_not_falsifiable"

            return Proposal(
                seat_id=self.seat_id, lens=self.lens, direction=side,
                conviction=conv, entry=round(entry, 2), stop=round(stop, 2),
                stop_reason=str(o.get("stop_reason", ""))[:400],
                targets=tuple(targets),
                predicted_path=str(o["predicted_path"])[:600],
                mechanism=str(o["mechanism"])[:800],
                invalidation=inv[:500],
                thesis=str(o["thesis"])[:800],
                counter_case=str(o.get("counter_case", ""))[:600],
                horizon_min=max(5, min(int(float(o.get("horizon_min", 45))), 720)),
                frame_hash=f.frame_hash, t_ns=f.t_event_ns,
                prefilter_score=c.score,
                meta={"llm": True, "arm": params.get("arm_id", "")},
            ), ""
        except (KeyError, TypeError, ValueError) as e:
            return None, f"schema:{type(e).__name__}"


# ---- provider adapters ----------------------------------------------------

def anthropic_completer(client, model: str = "claude-sonnet-4-5",
                        max_tokens: int = 1200, temperature: float = 0.3):
    """Adapter for the Anthropic SDK. Temperature is low but non-zero: some
    diversity of expression helps, but this is analysis, not creative writing."""
    def _complete(system: str, user: str) -> str:
        r = client.messages.create(
            model=model, max_tokens=max_tokens, temperature=temperature,
            system=system, messages=[{"role": "user", "content": user}])
        return "".join(getattr(b, "text", "") for b in r.content)
    return _complete


def openai_completer(client, model: str = "gpt-4o",
                     max_tokens: int = 1200, temperature: float = 0.3):
    def _complete(system: str, user: str) -> str:
        r = client.chat.completions.create(
            model=model, max_tokens=max_tokens, temperature=temperature,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}])
        return r.choices[0].message.content or ""
    return _complete


def recorded_completer(responses: Dict[str, str], fallback: str = ""):
    """Replay adapter: keyed by frame hash. Makes LLM-in-the-loop runs
    deterministic, which is what lets recovery verification work end to end."""
    def _complete(system: str, user: str) -> str:
        for k, v in responses.items():
            if k in user:
                return v
        return fallback
    return _complete
