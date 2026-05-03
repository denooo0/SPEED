"""SITUATION REPORT → sanitized X post.

Phase 2 monetization enabler. Produces a public-safe rendering of a SITUATION
REPORT for posting to X / Twitter / Discord. Sanitization rules:

  - Strip exact entry zones (replace with "around N" rounded to instrument-
    appropriate granularity)
  - Strip exact stop / TP levels (front-runners on a small audience can move
    the very levels we'd react to)
  - Keep regime, asymmetry summary, thesis, kill-thesis class (not the level)
  - Single post under 280 chars OR a clean two-post thread

ATLAS voice maintained: short sentences, no hype words, no emoji unless the
operator opts in via config.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from src.llm.schema import SituationReport

POST_CHAR_LIMIT = 280


@dataclass
class PostConfig:
    instrument_name: str = "XAUUSD"
    granularity: float = 5.0          # round levels to this for public display
    include_emoji: bool = False
    label_paper: bool = True          # tag posts as paper before promotion gate


def round_to(value: float, granularity: float) -> float:
    if granularity <= 0:
        return value
    return round(value / granularity) * granularity


def render_post(sr: SituationReport, config: Optional[PostConfig] = None) -> List[str]:
    """Return a list of post strings (1 or 2). Each ≤ POST_CHAR_LIMIT chars."""
    cfg = config or PostConfig()
    decision = sr.trade_proposal.decision
    if decision == "NO_TRADE":
        return _render_no_trade(sr, cfg)
    if decision == "SKIP":
        return _render_skip(sr, cfg)
    return _render_take(sr, cfg)


def _prefix(cfg: PostConfig) -> str:
    paper = " [PAPER]" if cfg.label_paper else ""
    return f"ATLAS / {cfg.instrument_name}{paper}"


def _render_take(sr: SituationReport, cfg: PostConfig) -> List[str]:
    tp = sr.trade_proposal
    # Sanitize entry: round midpoint to public granularity
    if tp.entry_zone is not None:
        mid = (tp.entry_zone.low + tp.entry_zone.high) / 2
        rounded_entry = round_to(mid, cfg.granularity)
        entry_str = f"around {rounded_entry:g}"
    else:
        entry_str = "structural reclaim"

    # Sanitize first target — relative R-multiple instead of absolute
    target_str = f"{tp.rr_minimum:g}R minimum"

    head = (
        f"{_prefix(cfg)} | {tp.direction.upper()}\n"
        f"Regime: {sr.regime}\n"
        f"Setup: {entry_str}\n"
        f"Target: {target_str}\n"
        f"Conf: {sr.confidence:.2f}"
    )

    thesis = sr.thesis.strip()
    # Rule: kill thesis class only, not specific level
    kill_class = _classify_kill_thesis(sr.kill_thesis)
    body = f"Why: {thesis}\nKill: {kill_class}"

    posts = [head]
    # If body fits in head, append; else thread
    combined = head + "\n\n" + body
    if len(combined) <= POST_CHAR_LIMIT:
        return [combined]
    posts.append(_truncate(body, POST_CHAR_LIMIT))
    return posts


def _render_skip(sr: SituationReport, cfg: PostConfig) -> List[str]:
    text = (
        f"{_prefix(cfg)} | SKIP\n"
        f"Regime: {sr.regime}\n"
        f"Why: {_truncate(sr.thesis, 180)}"
    )
    return [_truncate(text, POST_CHAR_LIMIT)]


def _render_no_trade(sr: SituationReport, cfg: PostConfig) -> List[str]:
    nz_lenses = sum(
        1 for v in (
            sr.confidence_breakdown.flow,
            sr.confidence_breakdown.structure,
            sr.confidence_breakdown.context,
            sr.confidence_breakdown.intent,
        ) if v > 0
    )
    text = (
        f"{_prefix(cfg)} | NO TRADE\n"
        f"Regime: {sr.regime} | Lenses active: {nz_lenses}/4\n"
        f"Default is NO. Sitting on hands."
    )
    return [_truncate(text, POST_CHAR_LIMIT)]


def _classify_kill_thesis(kill: str) -> str:
    """Map specific kill thesis to a public-safe class."""
    k = (kill or "").lower()
    if "close" in k and ("above" in k or "below" in k):
        return "structural reclaim"
    if "minute" in k or "time" in k:
        return "time decay"
    if "cvd" in k or "delta" in k:
        return "flow reversal"
    return "structural invalidation"


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
