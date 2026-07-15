"""LLM-as-filter Strategy wrapper (Path A).

Wraps any deterministic ``Strategy`` and only lets its ``Order`` reach the
simulator when the AtlasBrain agrees the setup is TAKE-worthy AND
``validate_take`` (the mandate chokepoint) approves.

Design intent:
    * Brain calls are expensive → cache aggressively. The cache key is
      (bar_close_hash, position_state_hash); on a hit we reuse the previous
      decision without a new API call. TTL is bounded in bars so that stale
      market regimes eventually expire.
    * `brain=None` means "pass-through" — the wrapper behaves identically to
      the base strategy. This makes the same class usable for the A/B baseline
      arm without having to swap types.
    * We never mutate the base strategy's state; on rejection we swallow the
      order and return None. The base strategy still saw the bar and updated
      its own state (so any rate-limits it maintains internally stay coherent).
    * The brain talks to a rich FeaturePack, not the shallow feature_pack the
      simulator passes to on_bar. If a FeatureReplayer is provided, we call
      ``pack_at(bar.timestamp)`` to fetch the same 4-lens snapshot the live
      brain would see; otherwise we fall back to the simulator's dict.

The chokepoint import (``validate_take``) is reused directly — we do not
reimplement doctrine here.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional

from src.backtest.cost_telemetry import CostTracker, _get_usage_field
from src.backtest.strategy import Bar, Order, PositionState, Strategy
from src.signals.llm_signal_generator import validate_take

logger = logging.getLogger(__name__)


DecisionAction = Literal["TAKE", "SKIP", "NO_TRADE"]


# ---------------------------------------------------------------------------
# LLMDecision — single-bar decision record. Kept simple so it survives being
# JSON-dumped into an experiment log by Track F.
# ---------------------------------------------------------------------------
@dataclass
class LLMDecision:
    action: DecisionAction  # "TAKE", "SKIP" (brain rejected), "NO_TRADE" (base skipped)
    reason: str
    confidence: float
    tokens_used: Dict[str, int] = field(default_factory=dict)
    cached: bool = False   # True if this decision was served from the cache


@dataclass
class _CacheEntry:
    decision: LLMDecision
    inserted_bar: int


class LLMFilterStrategy:
    """Wrap a Strategy so brain approval is required to actually place an order.

    Parameters
    ----------
    base_strategy:
        The underlying deterministic Strategy. Must implement ``on_bar``.
    brain:
        AtlasBrain (or a Fake exposing the same ``analyze`` signature). If
        ``None`` the wrapper is a pass-through — useful for the A/B baseline.
    feature_replayer:
        Optional FeatureReplayer. When set, the brain gets the rich 4-lens
        FeaturePack for the current bar; otherwise it receives the same
        ``feature_pack`` dict the simulator hands to the base strategy.
    memory_digest:
        Static memory digest string. Held constant across a backtest run —
        real live trading rebuilds this per bar; backtests approximate by
        holding it fixed so the CACHED prefix on the mandate stays warm.
    cost_tracker:
        Optional CostTracker to accumulate token usage. Created lazily if None
        so callers who don't care about telemetry don't have to pass one.
    cache_ttl_bars:
        Bars an entry stays valid for. 100 by default (~8 hours at 5-min bars).
        Setting to 0 disables the cache.
    """

    def __init__(
        self,
        base_strategy: Strategy,
        brain: Optional[Any],
        feature_replayer: Optional[Any] = None,
        memory_digest: str = "",
        cost_tracker: Optional[CostTracker] = None,
        cache_ttl_bars: int = 100,
    ) -> None:
        self.base_strategy = base_strategy
        self.brain = brain
        self.feature_replayer = feature_replayer
        self.memory_digest = memory_digest
        self.cost_tracker = cost_tracker if cost_tracker is not None else CostTracker()
        self.cache_ttl_bars = int(cache_ttl_bars)

        # ---- runtime state ----
        self._bar_counter: int = 0
        self._cache: Dict[str, _CacheEntry] = {}
        self._cache_hits: int = 0
        self._cache_misses: int = 0
        self.decisions: list = []             # every bar the base proposed an order
        self.filtered_out_count: int = 0      # base said trade, brain said no
        self.approved_count: int = 0
        self._last_feature_pack: Dict[str, Any] = {}

    # ---------------------------------------------------------------- Strategy protocol

    def on_bar(
        self,
        bar: Bar,
        feature_pack: Dict[str, Any],
        position_state: PositionState,
    ) -> Optional[Order]:
        """Simulator adapter. Delegates to :py:meth:`decide` but preserves the
        simulator's expected signature ``(bar, feature_pack, position_state)``.
        """
        # Cache the shallow feature_pack for decide() so a caller invoking the
        # (bar, position_state) signature (per the plan's contract) still works.
        self._last_feature_pack = feature_pack or {}
        return self._decide_impl(bar, feature_pack, position_state)

    def decide(self, bar: Bar, position_state: PositionState) -> Optional[Order]:
        """Simplified 2-arg entry (matches the interface spec in the plan)."""
        return self._decide_impl(bar, self._last_feature_pack, position_state)

    # ---------------------------------------------------------------- core

    def _decide_impl(
        self,
        bar: Bar,
        feature_pack: Dict[str, Any],
        position_state: PositionState,
    ) -> Optional[Order]:
        self._bar_counter += 1

        # 1) Ask the base strategy — always. Brain never sees a bar the base
        #    wasn't going to trade on. That keeps the LLM's job small.
        base_order = self.base_strategy.on_bar(bar, feature_pack, position_state)
        if base_order is None:
            return None

        # 2) Pass-through mode: no brain configured → base decisions land as-is.
        if self.brain is None:
            self.approved_count += 1
            self.decisions.append(
                LLMDecision(
                    action="TAKE",
                    reason="pass-through (no brain)",
                    confidence=0.0,
                    tokens_used={},
                    cached=False,
                )
            )
            return base_order

        # 3) Cache lookup.
        cache_key = self._cache_key(bar, position_state)
        cached_decision = self._cache_lookup(cache_key)
        if cached_decision is not None:
            self._cache_hits += 1
            decision = LLMDecision(
                action=cached_decision.action,
                reason=cached_decision.reason,
                confidence=cached_decision.confidence,
                tokens_used=dict(cached_decision.tokens_used),
                cached=True,
            )
        else:
            self._cache_misses += 1
            decision = self._call_brain(bar, feature_pack, position_state)
            # Insert into cache — even SKIP/NO_TRADE decisions are worth caching
            # because a repeated flat regime shouldn't burn tokens.
            if self.cache_ttl_bars > 0:
                self._cache[cache_key] = _CacheEntry(
                    decision=decision, inserted_bar=self._bar_counter
                )
                self._cache_evict_expired()

        self.decisions.append(decision)

        if decision.action == "TAKE":
            self.approved_count += 1
            return base_order

        # SKIP or NO_TRADE → filtered out.
        self.filtered_out_count += 1
        logger.debug(
            "LLM filter rejected order at %s: %s (%s)",
            bar.timestamp,
            decision.action,
            decision.reason,
        )
        return None

    # ---------------------------------------------------------------- brain call

    def _call_brain(
        self,
        bar: Bar,
        feature_pack: Dict[str, Any],
        position_state: PositionState,
    ) -> LLMDecision:
        """Call brain.analyze and translate its SR into a filter decision.

        On any exception we conservatively SKIP (don't burn a trade we don't
        understand) and log; this matches the live-trading policy.
        """
        # Build the rich FeaturePack payload if a replayer is wired up.
        payload = self._build_brain_payload(bar, feature_pack)
        try:
            sr = self.brain.analyze(payload, self.memory_digest, None)
        except Exception as exc:  # noqa: BLE001 — surface via log
            logger.warning("brain.analyze failed at %s: %s", bar.timestamp, exc)
            return LLMDecision(
                action="SKIP",
                reason=f"brain error: {exc}",
                confidence=0.0,
                tokens_used={},
                cached=False,
            )

        # Charge tokens even if we later reject: the API call happened.
        tokens_used = self._record_usage(sr)

        # Brain returned a SituationReport.
        proposal_decision = getattr(sr.trade_proposal, "decision", "NO_TRADE")
        confidence = float(getattr(sr, "confidence", 0.0))

        if proposal_decision != "TAKE":
            return LLMDecision(
                action=proposal_decision,
                reason=f"brain decided {proposal_decision}",
                confidence=confidence,
                tokens_used=tokens_used,
                cached=False,
            )

        # Brain said TAKE — run the doctrine chokepoint.
        rejection = validate_take(sr, has_open_position=position_state.is_open)
        if rejection is not None:
            return LLMDecision(
                action="SKIP",
                reason=f"validate_take: {rejection}",
                confidence=confidence,
                tokens_used=tokens_used,
                cached=False,
            )

        return LLMDecision(
            action="TAKE",
            reason="approved",
            confidence=confidence,
            tokens_used=tokens_used,
            cached=False,
        )

    # ---------------------------------------------------------------- helpers

    def _build_brain_payload(
        self, bar: Bar, feature_pack: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Assemble the dict the brain gets. Rich replayer output preferred."""
        if self.feature_replayer is not None:
            try:
                pack = self.feature_replayer.pack_at(bar.timestamp)
                if hasattr(pack, "to_dict"):
                    return pack.to_dict()
                return dict(pack)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "feature_replayer.pack_at(%s) failed: %s — falling back",
                    bar.timestamp,
                    exc,
                )
        # Fallback: pass the simulator's shallow dict + a minimal bar summary.
        base = dict(feature_pack) if feature_pack else {}
        base.setdefault("bar", {
            "timestamp": str(bar.timestamp),
            "close": float(bar.close),
            "high": float(bar.high),
            "low": float(bar.low),
            "volume": float(bar.volume),
        })
        return base

    def _record_usage(self, response: Any) -> Dict[str, int]:
        """Fold usage into the shared CostTracker and return a small dict.

        The dict is what we stash on the LLMDecision — deliberately kept small
        so it round-trips through JSON logs without ballooning experiment size.
        """
        model = getattr(self.brain, "config", None)
        model_name = getattr(model, "model", "claude-opus-4-7") if model else "claude-opus-4-7"

        # Charge the tracker.
        self.cost_tracker.record(response, model=model_name)

        # Pull the raw counters out for the decision record.
        usage = getattr(response, "usage", response)
        return {
            "input_tokens": _get_usage_field(usage, "input_tokens"),
            "output_tokens": _get_usage_field(usage, "output_tokens"),
            "cache_read_input_tokens": _get_usage_field(usage, "cache_read_input_tokens"),
            "cache_creation_input_tokens": _get_usage_field(
                usage, "cache_creation_input_tokens"
            ),
        }

    # ---------------------------------------------------------------- cache

    def _cache_key(self, bar: Bar, position_state: PositionState) -> str:
        """Content-address bar + position state so identical setups collide.

        We hash the bar's OHLCV + timestamp modulo a granularity, plus the
        binary "is_open / direction" position state. The hash keeps the key
        length constant and safely handles float precision drift.
        """
        bar_str = f"{bar.timestamp}|{bar.open:.5f}|{bar.high:.5f}|{bar.low:.5f}|{bar.close:.5f}|{bar.volume:.5f}"
        pos_str = f"{int(position_state.is_open)}|{position_state.direction}|{position_state.bars_held}"
        h = hashlib.blake2b(f"{bar_str}||{pos_str}".encode(), digest_size=16)
        return h.hexdigest()

    def _cache_lookup(self, key: str) -> Optional[LLMDecision]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if self._bar_counter - entry.inserted_bar > self.cache_ttl_bars:
            del self._cache[key]
            return None
        return entry.decision

    def _cache_evict_expired(self) -> None:
        """Cheap sweep. Cache is small (proposal-only bars) so O(N) is fine."""
        if not self._cache:
            return
        cutoff = self._bar_counter - self.cache_ttl_bars
        stale = [k for k, e in self._cache.items() if e.inserted_bar < cutoff]
        for k in stale:
            del self._cache[k]

    # ---------------------------------------------------------------- stats

    def cache_hit_rate(self) -> float:
        total = self._cache_hits + self._cache_misses
        if total == 0:
            return 0.0
        return self._cache_hits / total

    def stats(self) -> Dict[str, Any]:
        return {
            "n_decisions": len(self.decisions),
            "approved": self.approved_count,
            "filtered_out": self.filtered_out_count,
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "cache_hit_rate": self.cache_hit_rate(),
            "brain_calls": self.cost_tracker.total_calls,
            "estimated_usd": self.cost_tracker.estimated_usd(),
        }
