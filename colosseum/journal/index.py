"""Journal index: full-text + backlink graph.

The naming scheme already makes journals findable without an index (you can
guess a path from an ID). This makes it *instant*, and adds the thing paths
can't give you: search across reasoning text, and a walkable bidirectional
graph of signal <-> lesson <-> incident <-> session.

The index is DERIVED. It is rebuilt from journal files at any time. If it ever
disagrees with the files, the files win.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


class JournalIndex:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS docs (
      doc_id TEXT PRIMARY KEY,
      kind TEXT NOT NULL,           -- signal|session|lesson|incident|retro|standdown
      session_date TEXT,
      t_ns INTEGER,
      seat_id TEXT,
      direction TEXT,
      conviction REAL,
      resolution TEXT,
      realized_r REAL,
      mechanism_verdict TEXT,
      path TEXT,
      title TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_docs_kind ON docs(kind, t_ns);
    CREATE INDEX IF NOT EXISTS ix_docs_sess ON docs(session_date);
    CREATE INDEX IF NOT EXISTS ix_docs_seat ON docs(seat_id, resolution);

    CREATE TABLE IF NOT EXISTS links (
      src TEXT NOT NULL, dst TEXT NOT NULL, rel TEXT NOT NULL,
      PRIMARY KEY (src, dst, rel)
    );
    CREATE INDEX IF NOT EXISTS ix_links_dst ON links(dst, rel);

    CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
      doc_id UNINDEXED, kind UNINDEXED, body, tokenize='porter'
    );
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.db = sqlite3.connect(self.root / "INDEX.db")
        self.db.executescript(self.SCHEMA)
        self.db.commit()

    # ---- write ----------------------------------------------------------

    def upsert(self, doc_id: str, kind: str, body: str, path: str = "",
               title: str = "", **cols) -> None:
        fields = ("session_date", "t_ns", "seat_id", "direction", "conviction",
                  "resolution", "realized_r", "mechanism_verdict")
        vals = [cols.get(f) for f in fields]
        self.db.execute(
            "INSERT OR REPLACE INTO docs (doc_id,kind,session_date,t_ns,seat_id,"
            "direction,conviction,resolution,realized_r,mechanism_verdict,path,"
            "title) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [doc_id, kind] + vals + [path, title])
        self.db.execute("DELETE FROM fts WHERE doc_id=?", (doc_id,))
        self.db.execute("INSERT INTO fts (doc_id,kind,body) VALUES (?,?,?)",
                        (doc_id, kind, body))
        self.db.commit()

    def link(self, src: str, dst: str, rel: str) -> None:
        """Bidirectional by construction: every edge is stored with its inverse,
        so you can walk the graph from either end."""
        inverse = {"produced_lesson": "lesson_from_signal",
                   "in_session": "session_contains",
                   "tainted_by": "tainted_signal",
                   "from_frame": "frame_produced"}
        self.db.execute("INSERT OR IGNORE INTO links VALUES (?,?,?)", (src, dst, rel))
        if rel in inverse:
            self.db.execute("INSERT OR IGNORE INTO links VALUES (?,?,?)",
                            (dst, src, inverse[rel]))
        self.db.commit()

    # ---- read -----------------------------------------------------------

    def search(self, query: str, kind: Optional[str] = None,
               limit: int = 25) -> List[Dict[str, Any]]:
        """Full-text over reasoning. 'why did we fade the 2-sigma stretch in
        August' is answerable in milliseconds, years later."""
        q = ("SELECT f.doc_id, f.kind, d.title, d.path, d.session_date, "
             "d.resolution, d.realized_r, snippet(fts,2,'[',']','…',12) "
             "FROM fts f JOIN docs d ON d.doc_id=f.doc_id "
             "WHERE fts MATCH ?")
        args: List[Any] = [query]
        if kind:
            q += " AND f.kind=?"
            args.append(kind)
        q += " ORDER BY rank LIMIT ?"
        args.append(limit)
        return [{"doc_id": a, "kind": b, "title": c, "path": d,
                 "session_date": e, "resolution": f, "realized_r": g,
                 "snippet": h}
                for a, b, c, d, e, f, g, h in self.db.execute(q, args)]

    def backlinks(self, doc_id: str, rel: Optional[str] = None):
        q = "SELECT src, rel FROM links WHERE dst=?"
        args: List[Any] = [doc_id]
        if rel:
            q += " AND rel=?"
            args.append(rel)
        return [{"src": s, "rel": r} for s, r in self.db.execute(q, args)]

    def forward_links(self, doc_id: str, rel: Optional[str] = None):
        q = "SELECT dst, rel FROM links WHERE src=?"
        args: List[Any] = [doc_id]
        if rel:
            q += " AND rel=?"
            args.append(rel)
        return [{"dst": d, "rel": r} for d, r in self.db.execute(q, args)]

    def seat_scoreboard(self) -> List[Dict[str, Any]]:
        rows = self.db.execute(
            "SELECT seat_id, COUNT(*) n, "
            "  SUM(CASE WHEN resolution LIKE 'tp%' THEN 1 ELSE 0 END) wins, "
            "  AVG(realized_r) avg_r, AVG(conviction) avg_conv, "
            "  SUM(CASE WHEN mechanism_verdict='confirmed' THEN 1 ELSE 0 END) mech_ok "
            "FROM docs WHERE kind='signal' AND resolution IS NOT NULL "
            "GROUP BY seat_id ORDER BY avg_r DESC")
        return [{"seat_id": a, "n": b, "wins": c, "avg_r": d, "avg_conviction": e,
                 "mechanism_confirmed": f} for a, b, c, d, e, f in rows]

    def sessions(self) -> List[str]:
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT session_date FROM docs WHERE session_date IS NOT NULL "
            "ORDER BY session_date")]

    def stats(self) -> Dict[str, int]:
        return {k: n for k, n in self.db.execute(
            "SELECT kind, COUNT(*) FROM docs GROUP BY kind")}

    # ---- rebuild (index is derived, files are truth) ---------------------

    def rebuild(self, journal_root: Optional[Path] = None) -> Dict[str, int]:
        root = Path(journal_root or self.root)
        self.db.execute("DELETE FROM docs")
        self.db.execute("DELETE FROM fts")
        self.db.execute("DELETE FROM links")
        self.db.commit()
        n = {"signal": 0, "lesson": 0, "session": 0, "incident": 0}

        for js in (root / "sessions").rglob("signals/*.json"):
            r = json.loads(js.read_text())
            sid = js.stem
            out = r.get("outcome") or {}
            body = " ".join(str(r.get(k, "")) for k in (
                "thesis", "mechanism", "predicted_path", "invalidation",
                "counter_case", "stop_reason"))
            self.upsert(sid, "signal", body, path=str(js.with_suffix(".md")),
                        title=f"{r.get('direction','')} @ {r.get('entry','')}",
                        session_date=r.get("session_date"), t_ns=r.get("t_ns"),
                        seat_id=r.get("seat_id"), direction=r.get("direction"),
                        conviction=r.get("conviction"),
                        resolution=out.get("resolution"),
                        realized_r=out.get("realized_r"),
                        mechanism_verdict=out.get("mechanism_verdict"))
            if r.get("session_date"):
                self.link(sid, f"SESSION-{r['session_date']}", "in_session")
            for tag in out.get("lesson_tags", []):
                self.link(sid, f"LESSON-{tag}", "produced_lesson")
            n["signal"] += 1

        for meta_p in (root / "lessons").glob("*/meta.json"):
            m = json.loads(meta_p.read_text())
            doc = f"LESSON-{m['tag']}"
            self.upsert(doc, "lesson",
                        f"{m['tag']} {m.get('theme','')} {m.get('status','')}",
                        path=str(meta_p.parent / "lesson.md"),
                        title=m["tag"], session_date=m.get("last_seen"))
            n["lesson"] += 1

        for sp in (root / "sessions").rglob("SESSION.json"):
            s = json.loads(sp.read_text())
            sd = s.get("session_date", "")
            self.upsert(f"SESSION-{sd}", "session", json.dumps(s),
                        path=str(sp.with_suffix(".md")), title=f"Session {sd}",
                        session_date=sd)
            n["session"] += 1

        for ip in (root / "incidents").rglob("INC-*.json"):
            r = json.loads(ip.read_text())
            doc = ip.stem
            self.upsert(doc, "incident",
                        f"{r.get('detector','')} {r.get('description','')} "
                        f"{r.get('resolution_note','')}",
                        path=str(ip.with_suffix(".md")),
                        title=r.get("detector", "incident"), t_ns=r.get("t_ns"))
            n["incident"] += 1

        return n

    def close(self) -> None:
        self.db.close()
