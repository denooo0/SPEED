"""Controlled vocabulary for lessons.

Free-text tags do not aggregate. "absorption at highs", "absorbed the highs",
and "high absorption" become three lessons that each look weak, when they are
one lesson that is strong. A controlled vocabulary with alias resolution is the
difference between a searchable knowledge base and a pile of notes.

New concepts ARE allowed -- unknown tags are admitted to a quarantine namespace
(`unclassified/*`) and surfaced in the weekly retrospective for promotion into
the canon. The vocabulary grows deliberately rather than sprawling accidentally.
"""
from __future__ import annotations

from typing import Dict, List, Set

THEMES: Dict[str, List[str]] = {
    "absorption": [
        "absorption_at_highs", "absorption_at_lows",
        "effort_without_result", "volume_climax_no_progress",
    ],
    "divergence": [
        "regular_bearish_div", "regular_bullish_div",
        "hidden_bearish_div", "hidden_bullish_div",
        "divergence_failed_in_trend", "multi_tf_divergence_stack",
    ],
    "liquidity": [
        "sweep_high_reversal", "sweep_low_reversal",
        "stop_hunt_before_move", "liquidity_void_fast_fill",
        "equal_highs_magnet", "equal_lows_magnet",
    ],
    "vwap": [
        "vwap_reclaim_continuation", "vwap_rejection_fade",
        "two_sigma_exhaustion", "vwap_mean_magnet", "band_walk_trend",
    ],
    "structure": [
        "bos_continuation", "choch_reversal", "range_breakout_fail",
        "trend_day_no_pullback", "failed_breakout_trap",
    ],
    "flow": [
        "adl_price_conflict", "obv_confirms_trend",
        "cmf_regime_shift", "delta_divergence",
    ],
    "regime": [
        "asia_chop_avoid", "london_open_expansion", "ny_overlap_trend",
        "low_vol_mean_revert", "high_vol_momentum",
    ],
    "risk": [
        "stop_too_tight_for_atr", "target_beyond_realistic_range",
        "cost_ate_the_edge", "correlated_signal_cluster",
    ],
    "process": [
        "quota_forcing_avoided", "stood_down_correctly",
        "stale_frame_rejected", "exploration_trade",
    ],
    # Emitted by the outcome tracker itself. `mechanism_held` and
    # `right_for_wrong_reason` are the two most important tags in the whole
    # vocabulary: together they separate genuine understanding from luck.
    "mechanism": [
        "mechanism_held", "right_for_wrong_reason",
        "entry_offset_too_patient", "never_filled",
    ],
}

CANON: Set[str] = {t for tags in THEMES.values() for t in tags}
THEME_OF: Dict[str, str] = {t: th for th, tags in THEMES.items() for t in tags}

ALIASES: Dict[str, str] = {
    "absorbed_highs": "absorption_at_highs",
    "high_absorption": "absorption_at_highs",
    "no_result_high_effort": "effort_without_result",
    "bear_div": "regular_bearish_div",
    "bull_div": "regular_bullish_div",
    "stop_hunt": "stop_hunt_before_move",
    "vwap_fade": "vwap_rejection_fade",
    "2sigma_fade": "two_sigma_exhaustion",
    "choch": "choch_reversal",
    "bos": "bos_continuation",
}


def normalize(tag: str) -> str:
    """Resolve a raw tag to canonical form; quarantine the genuinely new."""
    t = "_".join(tag.strip().lower().split())
    t = "".join(c if (c.isalnum() or c == "_") else "_" for c in t)
    while "__" in t:
        t = t.replace("__", "_")
    t = t.strip("_")
    if t in CANON:
        return t
    if t in ALIASES:
        return ALIASES[t]
    return f"unclassified/{t}" if t else "unclassified/empty"


def theme_of(tag: str) -> str:
    return THEME_OF.get(tag, "unclassified")


def normalize_all(tags) -> List[str]:
    seen, out = set(), []
    for t in tags:
        n = normalize(t)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out
