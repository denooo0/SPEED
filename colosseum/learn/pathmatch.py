"""Path matching: grading HOW price got there, not just where it ended.

v1 hand-waved `path_match`. This makes it a real number.

A predicted path is free text ("sweep 2419, reject, reclaim VWAP, then trend").
The realized tape is a sequence of detected micro-events. Both are parsed into
the SAME canonical vocabulary, then compared by normalized weighted edit
distance.

Why this matters more than it looks: a seat that predicts the right direction by
the wrong route does not understand the market -- it got lucky on a coin flip
with a story attached. Scoring the route is what separates understanding from
narration, and it's the signal that lets the meta-learner promote genuine insight
over noise that happened to profit.
"""
from __future__ import annotations

import re
from typing import Dict, List, Sequence, Tuple

# Canonical micro-event vocabulary
EVENTS = [
    "sweep_high", "sweep_low", "reject", "absorb", "reclaim_vwap",
    "lose_vwap", "test_vwap", "bos_up", "bos_down", "choch_up", "choch_down",
    "expand", "compress", "retest", "trend", "target", "stop", "chop",
]

# Phrase -> canonical event. Ordered: longer/more specific patterns first.
PATTERNS: List[Tuple[str, str]] = [
    (r"sweep\w*\s+(?:the\s+)?high|take\s+(?:the\s+)?high|stop\s*hunt\s+high|"
     r"raid\s+(?:the\s+)?high|marginal\s+new\s+high", "sweep_high"),
    (r"sweep\w*\s+(?:the\s+)?low|take\s+(?:the\s+)?low|stop\s*hunt\s+low|"
     r"raid\s+(?:the\s+)?low|marginal\s+new\s+low", "sweep_low"),
    (r"reclaim\w*\s+vwap|back\s+above\s+vwap|recover\w*\s+vwap", "reclaim_vwap"),
    (r"los\w+\s+vwap|below\s+vwap|break\w*\s+vwap\s+down|fail\w*\s+vwap", "lose_vwap"),
    (r"test\w*\s+vwap|tag\w*\s+vwap|back\s+to\s+vwap|mean\s+revert", "test_vwap"),
    (r"reject\w*|rejection|wick\w*\s+off|fade\w*", "reject"),
    (r"absorb\w*|absorption|effort\s+without\s+result|stall\w*", "absorb"),
    (r"break\w*\s+of\s+structure\s+up|bos\s+up|higher\s+high", "bos_up"),
    (r"break\w*\s+of\s+structure\s+down|bos\s+down|lower\s+low", "bos_down"),
    (r"change\s+of\s+character\s+up|choch\s+up", "choch_up"),
    (r"change\s+of\s+character\s+down|choch\s+down", "choch_down"),
    (r"expand\w*|expansion|impuls\w+|flush\w*|thrust", "expand"),
    (r"compress\w*|consolidat\w+|coil\w*|tighten\w*", "compress"),
    (r"retest\w*|pullback|throwback", "retest"),
    (r"trend\w*|run\w*\s+to|leg\s+(?:up|down)|drive", "trend"),
    (r"target|tp\d?|take\s+profit|reach\w*\s+\d", "target"),
    (r"stop\w*\s+out|stopped|invalidat\w+", "stop"),
    (r"chop\w*|range\w*|sideways|balance", "chop"),
]

# Events that are near-synonyms cost less to substitute than unrelated ones.
KINSHIP: Dict[Tuple[str, str], float] = {
    ("sweep_high", "reject"): 0.4, ("sweep_low", "reject"): 0.4,
    ("absorb", "reject"): 0.5, ("expand", "trend"): 0.3,
    ("test_vwap", "reclaim_vwap"): 0.5, ("test_vwap", "lose_vwap"): 0.5,
    ("bos_up", "trend"): 0.4, ("bos_down", "trend"): 0.4,
    ("retest", "compress"): 0.6, ("target", "trend"): 0.5,
}


def parse_path(text: str) -> List[str]:
    """Free text -> canonical event sequence, order preserved.

    Scans by position so 'sweep the highs then reject and lose VWAP' yields
    [sweep_high, reject, lose_vwap] in the order stated, not registry order.
    """
    if not text:
        return []
    t = text.lower()
    hits: List[Tuple[int, str]] = []
    claimed: List[Tuple[int, int]] = []
    for pat, ev in PATTERNS:
        for m in re.finditer(pat, t):
            s, e = m.span()
            if any(s < ce and cs < e for cs, ce in claimed):
                continue          # don't double-count overlapping phrases
            claimed.append((s, e))
            hits.append((s, ev))
    hits.sort(key=lambda x: x[0])
    out: List[str] = []
    for _, ev in hits:
        if not out or out[-1] != ev:      # collapse immediate repeats
            out.append(ev)
    return out


def _sub_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    return KINSHIP.get((a, b)) or KINSHIP.get((b, a)) or 1.0


def path_distance(pred: Sequence[str], real: Sequence[str]) -> float:
    """Weighted Levenshtein with kinship-aware substitution."""
    n, m = len(pred), len(real)
    if n == 0 and m == 0:
        return 0.0
    if n == 0 or m == 0:
        return float(max(n, m))
    prev = [float(j) for j in range(m + 1)]
    for i in range(1, n + 1):
        cur = [float(i)] + [0.0] * m
        for j in range(1, m + 1):
            cur[j] = min(
                prev[j] + 1.0,                              # deletion
                cur[j - 1] + 1.0,                           # insertion
                prev[j - 1] + _sub_cost(pred[i - 1], real[j - 1]))
        prev = cur
    return prev[m]


def path_match(predicted_text: str, realized: Sequence[str]) -> Dict[str, object]:
    """0..1 similarity plus the parse, so the journal can show its working.

    Returning the parsed sequences (not just the score) is deliberate: when a
    path scores badly you need to see whether the *prediction* was wrong or the
    *parser* was, and a bare number hides that.
    """
    pred = parse_path(predicted_text)
    real = [e for e in realized if e in EVENTS]
    if not pred and not real:
        return {"score": 0.0, "predicted": [], "realized": list(real),
                "note": "no parseable path -- prediction was not falsifiable"}
    dist = path_distance(pred, real)
    denom = max(len(pred), len(real)) or 1
    score = max(0.0, 1.0 - dist / denom)
    ordered = _lcs_len(pred, real)
    return {
        "score": round(score, 4),
        "predicted": pred,
        "realized": real,
        "ordered_hits": ordered,
        "coverage": round(ordered / len(pred), 4) if pred else 0.0,
        "note": _verdict_note(score),
    }


def _lcs_len(a: Sequence[str], b: Sequence[str]) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            cur[j] = prev[j - 1] + 1 if a[i - 1] == b[j - 1] else max(prev[j], cur[j - 1])
        prev = cur
    return prev[len(b)]


def _verdict_note(s: float) -> str:
    if s >= 0.8:
        return "route understood -- prediction described the actual mechanism"
    if s >= 0.5:
        return "partially right route -- direction may have been right for "\
               "partly wrong reasons"
    if s >= 0.25:
        return "route largely wrong -- treat any profit as luck, not skill"
    return "route wrong -- the stated mechanism did not occur"
