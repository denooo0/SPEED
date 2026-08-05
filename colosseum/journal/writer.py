"""The Journal: six strata of memory, arranged so they're findable in three
years, not three days.

Layout (every path derivable from an ID, no index required to navigate):

    journal/
      MANIFEST.json
      INDEX.db                      <- derived, rebuildable
      sessions/YYYY/MM/DD/
          SESSION.md  SESSION.json  MANIFEST.json
          signals/SIG-....md  SIG-....json
          standdowns.jsonl
          events.jsonl
      lessons/<slug>/lesson.md  evidence.jsonl
      lessons/LESSON_INDEX.md
      incidents/YYYY/MM/INC-....md
      retros/weekly/YYYY-Www.md   retros/monthly/YYYY-MM.md

Two formats side by side, always:
  * .md   -- the narrative. A human opens this and reads the machine's mind.
  * .json -- the structure. The learner reads this.

Neither is a summary of the other; they are the same event at two resolutions.
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..core.clock import NS, ns_to_dt
from ..core.ids import content_hash, lesson_slug
from .taxonomy import normalize_all, theme_of

REDACT_KEYS = {"api_key", "token", "secret", "password", "account_id",
               "account", "bearer", "authorization"}


def _enc(o: Any) -> Any:
    if is_dataclass(o) and not isinstance(o, type):
        return {k: _enc(v) for k, v in asdict(o).items() if not k.startswith("_")}
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, dict):
        return {str(k): ("***REDACTED***" if str(k).lower() in REDACT_KEYS
                         else _enc(v)) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_enc(v) for v in o]
    return o


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)          # atomic: a reader never sees a half-written journal


def _append_jsonl(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(_enc(obj), sort_keys=True, separators=(",", ":")) + "\n")


class Journal:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "sessions").mkdir(exist_ok=True)
        (self.root / "lessons").mkdir(exist_ok=True)
        (self.root / "incidents").mkdir(exist_ok=True)
        (self.root / "retros").mkdir(exist_ok=True)

    # ---- paths (derivable from IDs -- survives index loss) ----------------

    def session_dir(self, session_date: str) -> Path:
        y, m, d = session_date.split("-")
        return self.root / "sessions" / y / m / d

    def signal_paths(self, session_date: str, signal_id: str):
        base = self.session_dir(session_date) / "signals"
        return base / f"{signal_id}.md", base / f"{signal_id}.json"

    def lesson_dir(self, tag: str) -> Path:
        return self.root / "lessons" / lesson_slug(tag)

    # ---- STRATUM 0: raw events ------------------------------------------

    def event(self, session_date: str, obj: Dict[str, Any]) -> None:
        _append_jsonl(self.session_dir(session_date) / "events.jsonl", obj)

    # ---- STRATUM 1: decision journals -----------------------------------

    def write_signal(self, signal_id: str, session_date: str,
                     record: Dict[str, Any]) -> Path:
        """The narrative + the structure. Written at emission, before any outcome
        is known -- so the reasoning can never be contaminated by hindsight."""
        md_p, js_p = self.signal_paths(session_date, signal_id)
        _write(js_p, json.dumps(_enc(record), indent=2, sort_keys=True))
        _write(md_p, self._signal_md(signal_id, record))
        return md_p

    def _signal_md(self, sid: str, r: Dict[str, Any]) -> str:
        t = ns_to_dt(r.get("t_ns", 0)).strftime("%Y-%m-%d %H:%M:%S UTC")
        tgts = r.get("targets", [])
        tg = "\n".join(
            f"- **{x.get('label','TP')}** @ `{x.get('price')}` — {x.get('reason','')}"
            for x in tgts) or "- _none_"
        conv = r.get("conviction", 0)
        return f"""# {sid}

> **{str(r.get('direction','?')).upper()}** · conviction **{conv:.0%}** · seat **{r.get('seat_id','?')}** ({r.get('lens','')})
> {t} · session `{r.get('session_date','')}` · {r.get('liquidity_session','')}

## Thesis
{r.get('thesis','')}

## Mechanism — why this should work
{r.get('mechanism','')}

## Predicted path
{r.get('predicted_path','')}

## Levels
| | price | reason |
|---|---|---|
| Entry | `{r.get('entry')}` | {r.get('entry_type','')} |
| Stop | `{r.get('stop')}` | {r.get('stop_reason','')} |

### Targets
{tg}

## Invalidation — what proves me wrong BEFORE the stop
{r.get('invalidation','')}

## Counter-case I argued against myself
{r.get('counter_case','')}

## Context at emission
- Regime: `{json.dumps(r.get('regime', {}), sort_keys=True)}`
- Frame: `{r.get('frame_hash','')}`
- Cost-adjusted edge: `{r.get('edge_after_cost','n/a')}`
- Exploration: `{r.get('is_exploration', False)}`
- Horizon: {r.get('horizon_min','?')} min

---
_Outcome pending. This section is appended, never rewritten._
"""

    def append_outcome(self, signal_id: str, session_date: str,
                       outcome: Dict[str, Any]) -> None:
        """APPEND -- never rewrite. The original reasoning stays verbatim so you
        can always see what was believed before the answer was known."""
        md_p, js_p = self.signal_paths(session_date, signal_id)
        if js_p.exists():
            rec = json.loads(js_p.read_text())
            rec["outcome"] = _enc(outcome)
            _write(js_p, json.dumps(rec, indent=2, sort_keys=True))
        tags = ", ".join(f"`{t}`" for t in outcome.get("lesson_tags", [])) or "_none_"
        block = f"""
## POST-MORTEM

**Resolution:** `{outcome.get('resolution')}` · **Realized R (net):** `{outcome.get('realized_r')}`
**MFE:** `{outcome.get('mfe_r')}` R · **MAE:** `{outcome.get('mae_r')}` R · **Time:** {outcome.get('time_to_outcome_s',0)//60} min

**Path realized:** `{' -> '.join(outcome.get('path_realized', []))}`
**Path match vs prediction:** `{outcome.get('path_match')}`

**Was the MECHANISM right?** `{outcome.get('mechanism_verdict')}`
> This is graded separately from profit on purpose. A win with a false mechanism
> is punished — being right for the wrong reason teaches the engine nothing and
> corrupts what it thinks it knows.

**Lessons:** {tags}
"""
        if md_p.exists():
            with open(md_p, "a", encoding="utf-8") as f:
                f.write(block)

    def stand_down(self, session_date: str, rec: Dict[str, Any]) -> None:
        """The negative class. Without it the learner only sees cases where it
        acted -- textbook selection bias."""
        _append_jsonl(self.session_dir(session_date) / "standdowns.jsonl", rec)

    # ---- STRATUM 2: session diary ---------------------------------------

    def write_session(self, session_date: str, summary: Dict[str, Any],
                      narrative: str) -> Path:
        d = self.session_dir(session_date)
        _write(d / "SESSION.json", json.dumps(_enc(summary), indent=2, sort_keys=True))
        _write(d / "SESSION.md", narrative)
        self.write_manifest(session_date)
        return d / "SESSION.md"

    # ---- STRATUM 4: lesson library --------------------------------------

    def reinforce_lesson(self, tag: str, signal_id: str, supports: bool,
                         note: str, session_date: str) -> Dict[str, Any]:
        """Distill, deduplicate, reinforce.

        Same pattern confirmed again -> evidence_count and confidence rise.
        Contradicted -> the lesson is CHALLENGED, and if it keeps failing it gets
        regime-scoped or retired, with the retirement itself preserved. That is
        institutional memory rather than a pile of notes."""
        d = self.lesson_dir(tag)
        d.mkdir(parents=True, exist_ok=True)
        meta_p = d / "meta.json"
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else {
            "tag": tag, "theme": theme_of(tag), "supports": 0, "contradicts": 0,
            "first_seen": session_date, "last_seen": session_date,
            "status": "active", "confidence": 0.5, "signals": []}

        meta["supports" if supports else "contradicts"] += 1
        meta["last_seen"] = session_date
        meta["signals"] = (meta["signals"] + [signal_id])[-500:]

        s, c = meta["supports"], meta["contradicts"]
        # Laplace-smoothed posterior: one lucky confirmation is not knowledge.
        meta["confidence"] = round((s + 1) / (s + c + 2), 4)
        meta["evidence_count"] = s + c
        if meta["evidence_count"] >= 12 and meta["confidence"] < 0.35:
            meta["status"] = "challenged"
        if meta["evidence_count"] >= 25 and meta["confidence"] < 0.25:
            meta["status"] = "retired"
        elif meta["confidence"] >= 0.65 and meta["evidence_count"] >= 8:
            meta["status"] = "established"

        _write(meta_p, json.dumps(meta, indent=2, sort_keys=True))
        _append_jsonl(d / "evidence.jsonl", {
            "signal_id": signal_id, "supports": supports,
            "session_date": session_date, "note": note})
        _write(d / "lesson.md", self._lesson_md(meta, note))
        return meta

    def _lesson_md(self, m: Dict[str, Any], latest_note: str) -> str:
        bar = "█" * int(m["confidence"] * 20)
        return f"""# Lesson · `{m['tag']}`

**Theme:** {m['theme']} · **Status:** `{m['status']}`
**Confidence:** {m['confidence']:.0%} `{bar:<20}`
**Evidence:** {m['supports']} supporting / {m['contradicts']} contradicting ({m.get('evidence_count',0)} total)
**Span:** {m['first_seen']} → {m['last_seen']}

## What we believe
{latest_note}

## Status meaning
- `active` — accumulating evidence, not yet trusted
- `established` — repeatedly confirmed; seats may lean on it
- `challenged` — failing more than it holds; needs regime-scoping
- `retired` — falsified; kept on record so we never relearn a dead idea

## Evidence trail
See `evidence.jsonl` — every signal that formed or fought this lesson, by ID.
Walk back to any of them: `sessions/<YYYY>/<MM>/<DD>/signals/<SIGNAL_ID>.md`

_Backlinks are bidirectional by design: a signal points to its lessons, and this
lesson points to every signal that shaped it._
"""

    def rebuild_lesson_index(self) -> Path:
        """Human-browsable index, grouped by theme, sorted by confidence."""
        by_theme: Dict[str, List[Dict[str, Any]]] = {}
        for meta_p in sorted((self.root / "lessons").glob("*/meta.json")):
            m = json.loads(meta_p.read_text())
            by_theme.setdefault(m.get("theme", "unclassified"), []).append(m)

        lines = ["# Lesson Library", "",
                 "_What the engine knows, grouped by theme and ranked by "
                 "evidence. Everything here was learned from graded outcomes._", ""]
        for theme in sorted(by_theme):
            lines.append(f"## {theme}")
            lines.append("")
            lines.append("| lesson | status | confidence | evidence |")
            lines.append("|---|---|---|---|")
            for m in sorted(by_theme[theme],
                            key=lambda x: -x.get("confidence", 0)):
                lines.append(
                    f"| [`{m['tag']}`]({lesson_slug(m['tag'])}/lesson.md) "
                    f"| {m['status']} | {m['confidence']:.0%} "
                    f"| {m.get('evidence_count',0)} |")
            lines.append("")
        p = self.root / "lessons" / "LESSON_INDEX.md"
        _write(p, "\n".join(lines))
        return p

    # ---- STRATUM 5: incidents -------------------------------------------

    def write_incident(self, incident_id: str, t_ns: int,
                       rec: Dict[str, Any]) -> Path:
        dt = ns_to_dt(t_ns)
        p = (self.root / "incidents" / dt.strftime("%Y") / dt.strftime("%m")
             / f"{incident_id}.md")
        rungs = "\n".join(f"{i+1}. {r}" for i, r in
                          enumerate(rec.get("repair_ladder", []))) or "_none_"
        _write(p, f"""# Incident {incident_id}

**Detected:** {dt:%Y-%m-%d %H:%M:%S} UTC
**Trigger:** `{rec.get('detector')}` · **Severity:** `{rec.get('severity')}`
**Health transition:** `{rec.get('from_state')}` → `{rec.get('to_state')}`

## What tripped
{rec.get('description','')}

## Repair ladder climbed
{rungs}

## Verification
{rec.get('verification','')}

## Outcome
**Resolved:** `{rec.get('resolved')}` · **Held:** `{rec.get('held')}`
{rec.get('resolution_note','')}

## Tainted window
Signals emitted between `{rec.get('taint_start')}` and `{rec.get('taint_end')}`
are flagged for the learner to down-weight — data born during a degraded state
must never silently become training truth.
""")
        _write(p.with_suffix(".json"), json.dumps(_enc(rec), indent=2, sort_keys=True))
        return p

    # ---- STRATUM 3: retrospectives --------------------------------------

    def write_retro(self, period: str, key: str, body: str) -> Path:
        p = self.root / "retros" / period / f"{key}.md"
        _write(p, body)
        return p

    # ---- manifests (integrity + discoverability) -------------------------

    def write_manifest(self, session_date: str) -> Path:
        d = self.session_dir(session_date)
        files = []
        for f in sorted(d.rglob("*")):
            if f.is_file() and f.name != "MANIFEST.json":
                data = f.read_bytes()
                files.append({"path": str(f.relative_to(d)),
                              "bytes": len(data),
                              "sha": content_hash(data.decode("utf-8", "replace"))})
        man = {"session_date": session_date, "files": files,
               "file_count": len(files),
               "total_bytes": sum(x["bytes"] for x in files)}
        p = d / "MANIFEST.json"
        _write(p, json.dumps(man, indent=2, sort_keys=True))
        return p

    def write_root_manifest(self) -> Path:
        sessions = sorted(
            str(p.parent.relative_to(self.root / "sessions")).replace("/", "-")
            for p in (self.root / "sessions").rglob("SESSION.json"))
        lessons = sorted(p.parent.name
                         for p in (self.root / "lessons").glob("*/meta.json"))
        incidents = sorted(p.stem
                           for p in (self.root / "incidents").rglob("INC-*.md"))
        man = {"sessions": sessions, "session_count": len(sessions),
               "lessons": lessons, "lesson_count": len(lessons),
               "incidents": incidents, "incident_count": len(incidents),
               "layout_version": 1}
        p = self.root / "MANIFEST.json"
        _write(p, json.dumps(man, indent=2, sort_keys=True))
        return p
