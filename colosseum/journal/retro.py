"""Stratum 3: weekly and monthly retrospectives.

Session diaries answer "what happened today." Retrospectives answer the question
that actually determines whether this engine is worth running: **is it getting
better, and how do we know?**

Every retro is built to be falsifiable. It reports trend direction on calibration
and reward with explicit deltas, names what regressed as prominently as what
improved, and surfaces the unclassified lesson tags awaiting promotion into the
canon. A retro that only reports good news is marketing, not memory.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .taxonomy import theme_of
from .writer import Journal


def _iso_week(d: str) -> str:
    y, m, dd = (int(x) for x in d.split("-"))
    iso = date(y, m, dd).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _trend(series: Sequence[float]) -> Tuple[str, float]:
    """Split-half comparison. Robust on the small n a week of trading gives you,
    where a regression slope would be dominated by one outlier day."""
    if len(series) < 4:
        return "insufficient data", 0.0
    mid = len(series) // 2
    a = statistics.fmean(series[:mid])
    b = statistics.fmean(series[mid:])
    delta = b - a
    if abs(delta) < 1e-9:
        return "flat", 0.0
    return ("improving" if delta > 0 else "declining"), delta


class Retrospective:
    def __init__(self, journal: Journal):
        self.j = journal

    # ---- data collection -------------------------------------------------

    def _sessions(self) -> List[Dict[str, Any]]:
        out = []
        for p in sorted((self.j.root / "sessions").rglob("SESSION.json")):
            try:
                out.append(json.loads(p.read_text()))
            except Exception:
                continue
        return out

    def _signals(self, session_dates: Sequence[str]) -> List[Dict[str, Any]]:
        want = set(session_dates)
        out = []
        for p in (self.j.root / "sessions").rglob("signals/*.json"):
            try:
                r = json.loads(p.read_text())
            except Exception:
                continue
            if r.get("session_date") in want:
                out.append(r)
        return out

    def _lessons(self) -> List[Dict[str, Any]]:
        out = []
        for p in (self.j.root / "lessons").glob("*/meta.json"):
            try:
                out.append(json.loads(p.read_text()))
            except Exception:
                continue
        return out

    # ---- analysis --------------------------------------------------------

    def _analyse(self, sessions: List[Dict[str, Any]],
                 signals: List[Dict[str, Any]]) -> Dict[str, Any]:
        resolved = [s for s in signals if s.get("outcome")]
        wins = [s for s in resolved
                if str(s["outcome"].get("resolution", "")).startswith("tp")]
        rs = [float(s["outcome"].get("realized_r", 0)) for s in resolved]
        pm = [float(s["outcome"].get("path_match", 0)) for s in resolved]
        mech = [str(s["outcome"].get("mechanism_verdict", "")) for s in resolved]

        by_seat: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"n": 0, "wins": 0, "r": 0.0, "conv": 0.0,
                     "path": 0.0, "false_mech": 0})
        for s in resolved:
            b = by_seat[str(s.get("seat_id", "?"))]
            o = s["outcome"]
            b["n"] += 1
            b["wins"] += 1 if str(o.get("resolution", "")).startswith("tp") else 0
            b["r"] += float(o.get("realized_r", 0))
            b["conv"] += float(s.get("conviction", 0))
            b["path"] += float(o.get("path_match", 0))
            b["false_mech"] += 1 if o.get("mechanism_verdict") == "false" else 0

        by_regime: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"n": 0, "r": 0.0})
        for s in resolved:
            key = f"{s.get('regime', {}).get('vol_band', '?')}/{s.get('liquidity_session', '?')}"
            by_regime[key]["n"] += 1
            by_regime[key]["r"] += float(s["outcome"].get("realized_r", 0))

        daily_r = [float(x.get("r", 0)) for x in sessions]
        daily_sig = [float(x.get("signals", 0)) for x in sessions]
        r_trend, r_delta = _trend(daily_r)
        c_series = []
        for x in sessions:
            cal = x.get("calibration") or {}
            briers = [v.get("brier", 0.25) for v in cal.values() if v.get("n")]
            if briers:
                c_series.append(-statistics.fmean(briers))   # negated: higher=better
        c_trend, c_delta = _trend(c_series)

        return {
            "sessions": len(sessions),
            "signals": len(signals),
            "resolved": len(resolved),
            "wins": len(wins),
            "win_rate": len(wins) / len(resolved) if resolved else 0.0,
            "total_r": sum(rs),
            "avg_r": statistics.fmean(rs) if rs else 0.0,
            "avg_path_match": statistics.fmean(pm) if pm else 0.0,
            "mechanism_confirmed": mech.count("confirmed"),
            "mechanism_false": mech.count("false"),
            "signals_per_day": statistics.fmean(daily_sig) if daily_sig else 0.0,
            "by_seat": dict(by_seat),
            "by_regime": dict(by_regime),
            "r_trend": r_trend, "r_delta": r_delta,
            "calibration_trend": c_trend, "calibration_delta": c_delta,
        }

    # ---- rendering -------------------------------------------------------

    def _render(self, title: str, period_label: str, a: Dict[str, Any],
                lessons: List[Dict[str, Any]], dates: Sequence[str]) -> str:
        L = [f"# {title}", "", f"**Period:** {period_label} "
             f"({dates[0]} → {dates[-1]}, {a['sessions']} sessions)", ""]

        # Headline: is it improving?
        arrow = {"improving": "📈", "declining": "📉",
                 "flat": "➡️"}.get(a["r_trend"], "•")
        carrow = {"improving": "📈", "declining": "📉",
                  "flat": "➡️"}.get(a["calibration_trend"], "•")
        L += ["## Is the engine improving?", "",
              f"{arrow} **Risk-adjusted return:** {a['r_trend']} "
              f"({a['r_delta']:+.3f} R per session, second half vs first)",
              f"{carrow} **Calibration:** {a['calibration_trend']} "
              f"({a['calibration_delta']:+.4f} Brier improvement)", "",
              "> Calibration is the primary axis. An engine whose returns rise "
              "while its calibration decays is getting lucky, not better — and "
              "that divergence is the earliest warning of a regime break.", ""]

        L += ["## Production", "",
              f"- Signals: **{a['signals']}** ({a['signals_per_day']:.1f}/day "
              f"vs the ≥5 target)",
              f"- Resolved: {a['resolved']} · Wins: {a['wins']} "
              f"(**{a['win_rate']:.0%}**)",
              f"- Net R: **{a['total_r']:+.2f}** (avg {a['avg_r']:+.3f} per signal)",
              f"- Mean path match: **{a['avg_path_match']:.2f}** — how often the "
              f"engine understood the ROUTE, not just the destination", ""]

        fm, mc = a["mechanism_false"], a["mechanism_confirmed"]
        tot = fm + mc
        if tot:
            L += ["### Mechanism truth", "",
                  f"Confirmed **{mc}** · False **{fm}** "
                  f"({fm/tot:.0%} of graded outcomes were right/wrong for the "
                  f"wrong reason)", ""]
            if fm / tot > 0.4:
                L += ["> ⚠️ **Mechanism rot.** More than 40% of outcomes had a "
                      "false stated mechanism. The P&L may look acceptable while "
                      "the engine's understanding decays. Audit the top seat's "
                      "theses before trusting the next promotion.", ""]

        L += ["## Seat performance", "",
              "| seat | n | win rate | net R | avg conviction | path match | false mech |",
              "|---|---|---|---|---|---|---|"]
        for seat, b in sorted(a["by_seat"].items(),
                              key=lambda kv: -kv[1]["r"]):
            n = max(b["n"], 1)
            L.append(f"| {seat} | {b['n']} | {b['wins']/n:.0%} | {b['r']:+.2f} | "
                     f"{b['conv']/n:.2f} | {b['path']/n:.2f} | {b['false_mech']} |")
        L.append("")

        if a["by_regime"]:
            L += ["## Where the edge actually lives", "",
                  "| regime (vol / session) | n | net R | R per signal |",
                  "|---|---|---|---|"]
            for k, v in sorted(a["by_regime"].items(),
                               key=lambda kv: -kv[1]["r"]):
                n = max(int(v["n"]), 1)
                L.append(f"| {k} | {int(v['n'])} | {v['r']:+.2f} | "
                         f"{v['r']/n:+.3f} |")
            L += ["", "> Regime-conditioned performance is the input to the next "
                  "parameter policy. If one regime carries all the edge, the "
                  "honest response is to trade less elsewhere — not to average "
                  "the edge away across all of them.", ""]

        est = [x for x in lessons if x.get("status") == "established"]
        chal = [x for x in lessons if x.get("status") == "challenged"]
        ret = [x for x in lessons if x.get("status") == "retired"]
        unc = [x for x in lessons if x.get("theme") == "unclassified"]

        L += ["## Knowledge state", "",
              f"- **Established:** {len(est)} lessons the seats may lean on",
              f"- **Challenged:** {len(chal)} failing more than they hold",
              f"- **Retired:** {len(ret)} falsified (kept on record so we never "
              f"relearn a dead idea)", ""]
        if est:
            L.append("### Strongest established lessons")
            L.append("")
            for m in sorted(est, key=lambda x: -x.get("confidence", 0))[:6]:
                L.append(f"- `{m['tag']}` — {m['confidence']:.0%} confidence "
                         f"over {m.get('evidence_count', 0)} observations "
                         f"({theme_of(m['tag'])})")
            L.append("")
        if chal:
            L.append("### Lessons under challenge — narrow or retire these")
            L.append("")
            for m in chal[:6]:
                L.append(f"- `{m['tag']}` — down to {m['confidence']:.0%} over "
                         f"{m.get('evidence_count',0)} observations. Regime-scope "
                         f"it rather than deleting it.")
            L.append("")
        if unc:
            L += ["### Unclassified tags awaiting promotion into the canon", "",
                  "These are concepts the engine coined that the controlled "
                  "vocabulary does not yet cover. Promote the useful ones into "
                  "`taxonomy.THEMES`; the vocabulary should grow deliberately, "
                  "not sprawl accidentally.", ""]
            for m in unc[:8]:
                L.append(f"- `{m['tag']}` ({m.get('evidence_count',0)} observations)")
            L.append("")

        L += ["## What to do next", ""]
        L += self._recommendations(a)
        return "\n".join(L)

    def _recommendations(self, a: Dict[str, Any]) -> List[str]:
        """Every retro ends in an action, or it was a status update pretending
        to be analysis."""
        recs: List[str] = []
        if a["signals_per_day"] < 3:
            recs.append("- **Signal drought.** Diagnose before touching anything: "
                        "dead regime, over-strict gates, or genuinely thin tape. "
                        "Tune pre-filter *sensitivity* only — never the quality bar.")
        elif a["signals_per_day"] > 12:
            recs.append("- **Signal flood.** Check for correlated near-duplicates; "
                        "raise the pre-filter threshold and confirm the arbiter's "
                        "correlation cap is binding.")
        if a["calibration_trend"] == "declining":
            recs.append("- **Calibration declining.** Fit the isotonic layer and "
                        "consider a parameter rollback. This is the leading "
                        "indicator of a regime break — act before returns confirm it.")
        if a["avg_path_match"] < 0.45:
            recs.append("- **Poor path understanding.** The seats are calling "
                        "direction without understanding route. Tighten the "
                        "mechanism requirement in the prompt and raise the "
                        "mechanism penalty weight.")
        worst = min(a["by_seat"].items(), key=lambda kv: kv[1]["r"],
                    default=(None, None))
        if worst[0] and worst[1]["n"] >= 10 and worst[1]["r"] < 0:
            recs.append(f"- **Seat {worst[0]} is negative** over {worst[1]['n']} "
                        f"graded calls ({worst[1]['r']:+.2f} R). Spawn a "
                        f"challenger against it; bench it if it does not recover.")
        if not recs:
            recs.append("- No corrective action indicated. Continue accumulating "
                        "evidence; the next promotion window is the thing to watch.")
        return recs

    # ---- public ----------------------------------------------------------

    def weekly(self, week: Optional[str] = None) -> Optional[Path]:
        sessions = self._sessions()
        if not sessions:
            return None
        buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for s in sessions:
            sd = s.get("session_date")
            if sd:
                buckets[_iso_week(sd)].append(s)
        key = week or max(buckets)
        chosen = buckets.get(key, [])
        if not chosen:
            return None
        dates = sorted(x["session_date"] for x in chosen)
        a = self._analyse(chosen, self._signals(dates))
        body = self._render(f"Weekly Retrospective · {key}", key, a,
                            self._lessons(), dates)
        return self.j.write_retro("weekly", key, body)

    def monthly(self, month: Optional[str] = None) -> Optional[Path]:
        sessions = self._sessions()
        if not sessions:
            return None
        buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for s in sessions:
            sd = s.get("session_date")
            if sd:
                buckets[sd[:7]].append(s)
        key = month or max(buckets)
        chosen = buckets.get(key, [])
        if not chosen:
            return None
        dates = sorted(x["session_date"] for x in chosen)
        a = self._analyse(chosen, self._signals(dates))
        body = self._render(f"Monthly Retrospective · {key}", key, a,
                            self._lessons(), dates)
        return self.j.write_retro("monthly", key, body)
