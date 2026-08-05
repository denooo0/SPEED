"""The Ledger: immutable, hash-chained, indexed memory of every decision.

Three properties, each load-bearing:

  1. IMMUTABLE + APPEND-ONLY. A signal row is written once at emission. The
     outcome is a SEPARATE amendment record, not an edit. You can therefore
     always reconstruct exactly what the engine believed at emission time,
     unpolluted by hindsight -- which is the only way calibration means anything.

  2. HASH-CHAINED. Each record binds to its predecessor. Altering history breaks
     every subsequent hash, and `verify_chain()` detects it. The engine can prove
     its own memory is untampered.

  3. INDEXED. A SQLite index is a DERIVED artifact, rebuildable from the log at
     any time. Never the source of truth -- if the index and the log disagree,
     the log wins and the index is rebuilt.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..core.ids import chain_hash, content_hash
from .wal import WriteAheadLog

REC_SIGNAL = "signal"
REC_OUTCOME = "outcome"
REC_STANDDOWN = "standdown"
REC_HEALTH = "health"
REC_CHECKPOINT = "checkpoint"
REC_INCIDENT = "incident"


def _enc(o: Any) -> Any:
    if is_dataclass(o) and not isinstance(o, type):
        return {k: _enc(v) for k, v in asdict(o).items() if not k.startswith("_")}
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, dict):
        return {str(k): _enc(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_enc(v) for v in o]
    return o


class Ledger:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS records (
      seq INTEGER PRIMARY KEY,
      rec_id TEXT NOT NULL,
      kind TEXT NOT NULL,
      ref_id TEXT,
      t_ns INTEGER NOT NULL,
      session_date TEXT NOT NULL,
      seat_id TEXT,
      direction TEXT,
      conviction REAL,
      frame_hash TEXT,
      code_version TEXT,
      prev_hash TEXT,
      hash TEXT NOT NULL,
      payload TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_rec_id     ON records(rec_id);
    CREATE INDEX IF NOT EXISTS ix_kind_t     ON records(kind, t_ns);
    CREATE INDEX IF NOT EXISTS ix_session    ON records(session_date, kind);
    CREATE INDEX IF NOT EXISTS ix_seat       ON records(seat_id, t_ns);
    CREATE INDEX IF NOT EXISTS ix_ref        ON records(ref_id);
    """

    def __init__(self, root: str | Path, durable: bool = True):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.wal = WriteAheadLog(self.root / "ledger.wal", durable=durable)
        self.db = sqlite3.connect(self.root / "ledger_index.db")
        self.db.executescript(self.SCHEMA)
        self.db.commit()
        self._seq, self._tip = self._load_tip()

    def _load_tip(self):
        row = self.db.execute(
            "SELECT seq, hash FROM records ORDER BY seq DESC LIMIT 1").fetchone()
        return (row[0], row[1]) if row else (0, "")

    # ---- write -----------------------------------------------------------

    def append(self, kind: str, rec_id: str, t_ns: int, session_date: str,
               payload: Dict[str, Any], ref_id: Optional[str] = None,
               seat_id: Optional[str] = None, direction: Optional[str] = None,
               conviction: Optional[float] = None,
               frame_hash: Optional[str] = None,
               code_version: Optional[str] = None) -> str:
        """Append one immutable record. Idempotent: re-appending an identical
        (rec_id, kind) is a no-op, so recovery/replay can run repeatedly."""
        existing = self.db.execute(
            "SELECT hash FROM records WHERE rec_id=? AND kind=?",
            (rec_id, kind)).fetchone()
        if existing:
            return existing[0]

        body = _enc(payload)
        h = chain_hash(self._tip, {"k": kind, "id": rec_id, "t": t_ns, "b": body})
        rec = {
            "seq": self._seq + 1, "rec_id": rec_id, "kind": kind,
            "ref_id": ref_id, "t_ns": t_ns, "session_date": session_date,
            "seat_id": seat_id, "direction": direction, "conviction": conviction,
            "frame_hash": frame_hash, "code_version": code_version,
            "prev_hash": self._tip, "hash": h, "payload": body,
        }
        self.wal.append(json.dumps(rec, sort_keys=True,
                                   separators=(",", ":")).encode())
        self.db.execute(
            "INSERT INTO records (seq,rec_id,kind,ref_id,t_ns,session_date,"
            "seat_id,direction,conviction,frame_hash,code_version,prev_hash,"
            "hash,payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["seq"], rec_id, kind, ref_id, t_ns, session_date, seat_id,
             direction, conviction, frame_hash, code_version, self._tip, h,
             json.dumps(body, sort_keys=True, separators=(",", ":"))))
        self.db.commit()
        self._seq += 1
        self._tip = h
        return h

    # ---- read ------------------------------------------------------------

    def get(self, rec_id: str, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        q = "SELECT kind,rec_id,t_ns,session_date,payload,hash FROM records WHERE rec_id=?"
        args: list = [rec_id]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        return [{"kind": k, "rec_id": r, "t_ns": t, "session_date": s,
                 "payload": json.loads(p), "hash": h}
                for k, r, t, s, p, h in self.db.execute(q, args)]

    def by_session(self, session_date: str, kind: Optional[str] = None):
        q = "SELECT rec_id,kind,t_ns,payload FROM records WHERE session_date=?"
        args: list = [session_date]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        q += " ORDER BY t_ns"
        return [{"rec_id": r, "kind": k, "t_ns": t, "payload": json.loads(p)}
                for r, k, t, p in self.db.execute(q, args)]

    def signals_for_seat(self, seat_id: str, limit: int = 500):
        rows = self.db.execute(
            "SELECT rec_id,t_ns,conviction,direction,payload FROM records "
            "WHERE kind=? AND seat_id=? ORDER BY t_ns DESC LIMIT ?",
            (REC_SIGNAL, seat_id, limit))
        return [{"rec_id": r, "t_ns": t, "conviction": c, "direction": d,
                 "payload": json.loads(p)} for r, t, c, d, p in rows]

    def resolved_pairs(self, limit: int = 2000):
        """(signal, outcome) joined -- the learner's training set."""
        rows = self.db.execute(
            "SELECT s.rec_id, s.seat_id, s.conviction, s.direction, s.payload,"
            "       o.payload, s.t_ns "
            "FROM records s JOIN records o ON o.ref_id = s.rec_id AND o.kind=? "
            "WHERE s.kind=? ORDER BY s.t_ns DESC LIMIT ?",
            (REC_OUTCOME, REC_SIGNAL, limit))
        return [{"signal_id": a, "seat_id": b, "conviction": c, "direction": d,
                 "signal": json.loads(e), "outcome": json.loads(f), "t_ns": g}
                for a, b, c, d, e, f, g in rows]

    def counts(self) -> Dict[str, int]:
        return {k: n for k, n in self.db.execute(
            "SELECT kind, COUNT(*) FROM records GROUP BY kind")}

    # ---- integrity -------------------------------------------------------

    def verify_chain(self) -> Dict[str, Any]:
        """Walk the hash chain. Any tampering shows up as the first bad link."""
        prev, checked = "", 0
        for seq, kind, rec_id, t_ns, payload, ph, h in self.db.execute(
                "SELECT seq,kind,rec_id,t_ns,payload,prev_hash,hash "
                "FROM records ORDER BY seq"):
            if ph != prev:
                return {"ok": False, "broken_at": seq, "reason": "prev_hash mismatch"}
            expect = chain_hash(prev, {"k": kind, "id": rec_id, "t": t_ns,
                                       "b": json.loads(payload)})
            if expect != h:
                return {"ok": False, "broken_at": seq, "reason": "hash mismatch"}
            prev = h
            checked += 1
        return {"ok": True, "records": checked, "tip": prev}

    def rebuild_index(self) -> int:
        """The log is truth; the index is derived. Nuke and re-derive."""
        self.db.execute("DELETE FROM records")
        n = 0
        for raw in self.wal.read_all():
            r = json.loads(raw)
            self.db.execute(
                "INSERT OR REPLACE INTO records (seq,rec_id,kind,ref_id,t_ns,"
                "session_date,seat_id,direction,conviction,frame_hash,"
                "code_version,prev_hash,hash,payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["seq"], r["rec_id"], r["kind"], r.get("ref_id"), r["t_ns"],
                 r["session_date"], r.get("seat_id"), r.get("direction"),
                 r.get("conviction"), r.get("frame_hash"), r.get("code_version"),
                 r["prev_hash"], r["hash"],
                 json.dumps(r["payload"], sort_keys=True, separators=(",", ":"))))
            n += 1
        self.db.commit()
        self._seq, self._tip = self._load_tip()
        return n

    def close(self) -> None:
        self.wal.close()
        self.db.close()
