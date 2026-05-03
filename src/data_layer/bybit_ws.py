"""Bybit WebSocket consumer + thread-safe rolling buffers.

Streams trades + L2 order book deltas. Maintains:
  - Rolling deque of recent trades (size, side, ts) — feeds true CVD
  - Order-book snapshot (top + 5-deep) updated from deltas — feeds imbalance
  - Sweep + large-print detection (read by Lens 1)

The bot's main loop is sync (1-min cycles). The WebSocket runs in a
background thread; the buffer is thread-safe via locks. The flow extractor
opportunistically reads from the buffer; if the WS isn't running, it falls
back to candle-based CVD approximation.

This module deliberately does NOT depend on any specific WS library so the
core ATLAS package stays light. Plug in `websocket-client`, `websockets`,
or ccxt.pro by implementing `BybitWSConnector` against the published API.
"""
from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class Trade:
    timestamp: int   # ms
    price: float
    size: float
    side: str        # "buy" | "sell" — aggressor side


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class WSBufferConfig:
    trade_window: int = 5_000           # max trades retained
    book_levels: int = 5                # top N each side
    large_print_multiplier: float = 5.0  # x median trade size
    sweep_levels: int = 2                # min levels eaten to count as sweep


class WSBuffer:
    """Thread-safe rolling buffer the WS writer fills + the feature extractor reads."""

    def __init__(self, config: Optional[WSBufferConfig] = None) -> None:
        self.config = config or WSBufferConfig()
        self._lock = threading.RLock()
        self._trades: Deque[Trade] = deque(maxlen=self.config.trade_window)
        self._bids: List[OrderBookLevel] = []
        self._asks: List[OrderBookLevel] = []
        self._last_book_update_ms: int = 0
        # Sweep tracking — book snapshot at time of trade
        self._recent_sweeps: Deque[Dict[str, Any]] = deque(maxlen=200)
        self._recent_large_prints: Deque[Dict[str, Any]] = deque(maxlen=200)

    # -- writer-side (called by WS thread) -------------------------------
    def push_trade(self, trade: Trade) -> None:
        with self._lock:
            # Sweep detection: how many book levels does this trade eat?
            book_side = self._asks if trade.side == "buy" else self._bids
            levels_eaten = self._levels_eaten(book_side, trade)
            if levels_eaten >= self.config.sweep_levels:
                self._recent_sweeps.append({
                    "ts": trade.timestamp,
                    "side": trade.side,
                    "price": trade.price,
                    "size": trade.size,
                    "levels": levels_eaten,
                })

            # Large-print detection: vs median of recent trade sizes
            if len(self._trades) >= 50:
                sizes = [t.size for t in list(self._trades)[-200:]]
                median = statistics.median(sizes)
                if median > 0 and trade.size > median * self.config.large_print_multiplier:
                    self._recent_large_prints.append({
                        "ts": trade.timestamp,
                        "side": trade.side,
                        "price": trade.price,
                        "size": trade.size,
                        "ratio": trade.size / median,
                    })

            self._trades.append(trade)

    def update_book(self, bids: List[OrderBookLevel], asks: List[OrderBookLevel]) -> None:
        """Replace the top-of-book snapshot. Bids descending price, asks ascending."""
        with self._lock:
            self._bids = sorted(bids, key=lambda l: -l.price)[: self.config.book_levels]
            self._asks = sorted(asks, key=lambda l: l.price)[: self.config.book_levels]
            self._last_book_update_ms = int(time.time() * 1000)

    # -- reader-side (called by main loop) -------------------------------
    def snapshot(self) -> Dict[str, Any]:
        """Read-only snapshot for the Lens 1 feature extractor."""
        with self._lock:
            trades = list(self._trades)
            bids = list(self._bids)
            asks = list(self._asks)
            sweeps = list(self._recent_sweeps)
            prints = list(self._recent_large_prints)
            book_age_ms = int(time.time() * 1000) - self._last_book_update_ms

        # CVD across the whole buffer
        cvd_full = sum(t.size if t.side == "buy" else -t.size for t in trades)
        # CVD over the last minute
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - 60_000
        recent = [t for t in trades if t.timestamp >= cutoff]
        cvd_recent = sum(t.size if t.side == "buy" else -t.size for t in recent)

        # Order book imbalance
        bid_size = sum(l.size for l in bids)
        ask_size = sum(l.size for l in asks)
        if (bid_size + ask_size) > 0:
            imbalance = (bid_size - ask_size) / (bid_size + ask_size)
        else:
            imbalance = 0.0

        spread = (asks[0].price - bids[0].price) if (bids and asks) else None

        return {
            "trades_in_buffer": len(trades),
            "cvd_full": cvd_full,
            "cvd_recent_1m": cvd_recent,
            "ob_bid_size": bid_size,
            "ob_ask_size": ask_size,
            "ob_imbalance": imbalance,
            "ob_spread": spread,
            "ob_age_ms": book_age_ms,
            "sweeps_recent": sweeps[-10:],
            "large_prints_recent": prints[-10:],
            "source": "ws",
        }

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _levels_eaten(book_side: List[OrderBookLevel], trade: Trade) -> int:
        """How many resting levels would this aggressor trade consume."""
        if not book_side:
            return 0
        levels = 0
        if trade.side == "buy":
            for level in book_side:
                if trade.price >= level.price:
                    levels += 1
                else:
                    break
        else:
            for level in book_side:
                if trade.price <= level.price:
                    levels += 1
                else:
                    break
        return levels


class BybitWSConnector:
    """Thin scaffolding for a background-thread WebSocket consumer.

    This is intentionally a stub: in production it connects to Bybit's
    WebSocket API (`wss://stream.bybit.com/v5/public/linear`), subscribes
    to `publicTrade.<symbol>` and `orderbook.50.<symbol>`, parses each
    payload, and forwards into the buffer. The exact wire format is
    operator-tunable, so we leave the network layer pluggable.
    """

    def __init__(
        self,
        buffer: WSBuffer,
        symbol: str = "XAUUSD",
        url: str = "wss://stream.bybit.com/v5/public/linear",
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        self.buffer = buffer
        self.symbol = symbol
        self.url = url
        self.on_error = on_error
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="bybit-ws")
        self._thread.start()
        logger.info("WS thread started (%s, %s)", self.symbol, self.url)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("WS thread stopped")

    def _run(self) -> None:
        """Override / subclass to plug in your WS client of choice."""
        logger.warning(
            "BybitWSConnector._run is a stub; subclass it with your WS client "
            "(websocket-client, websockets, ccxt.pro) and forward to push_trade / update_book"
        )
        while not self._stop.wait(1.0):
            pass


def trade_from_bybit_msg(msg: Dict[str, Any]) -> Optional[Trade]:
    """Parse a Bybit publicTrade payload into a Trade. Returns None if malformed."""
    try:
        return Trade(
            timestamp=int(msg["T"]),
            price=float(msg["p"]),
            size=float(msg["v"]),
            side="buy" if msg["S"].lower().startswith("b") else "sell",
        )
    except (KeyError, ValueError, TypeError) as e:
        logger.warning("trade parse failed: %s (msg=%s)", e, msg)
        return None


def book_levels_from_bybit_msg(msg: Dict[str, Any]) -> Tuple[List[OrderBookLevel], List[OrderBookLevel]]:
    """Parse a Bybit orderbook snapshot/delta payload."""
    def _parse(side_data: List[List[str]]) -> List[OrderBookLevel]:
        out: List[OrderBookLevel] = []
        for entry in side_data:
            try:
                price = float(entry[0])
                size = float(entry[1])
                if size > 0:
                    out.append(OrderBookLevel(price=price, size=size))
            except (IndexError, ValueError, TypeError):
                continue
        return out
    bids = _parse(msg.get("b", []))
    asks = _parse(msg.get("a", []))
    return bids, asks
