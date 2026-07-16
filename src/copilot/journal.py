"""Trade journal + per-setup learning.

Persists proposed/closed trades to JSONL and accumulates SetupStats. This is
the "gets better over time" core: as the operator logs real trades and their
outcomes, the copilot builds an evidence base on THEIR personal edge — which
of their setups actually make money, at what win rate and expectancy — and
feeds that back into sizing and coaching.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

from src.copilot.schema import SetupStats, TradeProposal, TradeRecord


class TradeJournal:
    def __init__(self, root: str | Path = "memory/copilot/") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.trades_path = self.root / "trades.jsonl"

    # -- persistence -----------------------------------------------------
    def _append(self, record: TradeRecord) -> None:
        with self.trades_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), default=str) + "\n")

    def _rewrite(self, records: List[TradeRecord]) -> None:
        with self.trades_path.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(asdict(r), default=str) + "\n")

    def load_all(self) -> List[TradeRecord]:
        if not self.trades_path.exists():
            return []
        out: List[TradeRecord] = []
        for line in self.trades_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(TradeRecord(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    # -- lifecycle -------------------------------------------------------
    def open_trade(
        self,
        proposal: TradeProposal,
        risk_pct: float,
        size: float,
        trade_id: Optional[str] = None,
    ) -> TradeRecord:
        tid = trade_id or f"{int(time.time()*1000)}-{proposal.setup}"
        rec = TradeRecord(
            trade_id=tid,
            instrument=proposal.instrument,
            direction=proposal.direction,
            entry=proposal.entry,
            stop=proposal.stop,
            target=proposal.target,
            setup=proposal.setup,
            thesis=proposal.thesis,
            kill_thesis=proposal.kill_thesis,
            conviction=proposal.conviction,
            session=proposal.session,
            risk_pct=risk_pct,
            size=size,
            opened_at=int(time.time() * 1000),
            status="OPEN",
            notes=proposal.notes,
        )
        self._append(rec)
        return rec

    def close_trade(
        self,
        trade_id: str,
        exit_price: float,
        exit_reason: str = "manual",
        notes: str = "",
    ) -> Optional[TradeRecord]:
        records = self.load_all()
        target: Optional[TradeRecord] = None
        for r in records:
            if r.trade_id == trade_id and r.status == "OPEN":
                target = r
                break
        if target is None:
            return None
        risk = abs(target.entry - target.stop)
        if target.direction == "long":
            move = exit_price - target.entry
        else:
            move = target.entry - exit_price
        r_mult = move / risk if risk > 0 else 0.0
        target.exit_price = exit_price
        target.closed_at = int(time.time() * 1000)
        target.r_multiple = r_mult
        # Account PnL as a fraction: risk_pct * R (fixed-fractional).
        target.pnl_account = (target.risk_pct / 100.0) * r_mult
        target.exit_reason = exit_reason
        target.status = "CLOSED"
        if notes:
            target.notes = (target.notes + " | " + notes).strip(" |")
        self._rewrite(records)
        return target

    def open_positions(self) -> List[TradeRecord]:
        return [r for r in self.load_all() if r.status == "OPEN"]

    # -- learning --------------------------------------------------------
    def setup_stats(self) -> Dict[str, SetupStats]:
        """Accumulate per-setup performance from closed trades."""
        stats: Dict[str, SetupStats] = {}
        for r in self.load_all():
            if r.status != "CLOSED" or r.r_multiple is None:
                continue
            s = stats.setdefault(r.setup, SetupStats(setup=r.setup))
            s.n += 1
            s.sum_r += r.r_multiple
            if r.r_multiple > 1e-9:
                s.wins += 1
                s.sum_win_r += r.r_multiple
            elif r.r_multiple < -1e-9:
                s.losses += 1
                s.sum_loss_r += r.r_multiple
            else:
                s.breakeven += 1
        return stats

    def stats_for(self, setup: str) -> SetupStats:
        return self.setup_stats().get(setup, SetupStats(setup=setup))
