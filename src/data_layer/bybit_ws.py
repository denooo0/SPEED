"""Bybit WebSocket consumer + thread-safe rolling buffers.

Streams trades + L2 order book deltas. Maintains:
  - Rolling deque of recent trades (size, side, ts) — feeds true CVD
  - Order-book snapshot (top + 5-deep) updated from deltas — feeds imbalance
  - Sweep + large-print detection (read by Lens 1)

The bot's main loop is sync (1-min cycles). The WebSocket runs in a
background thread; the buffer is thread-safe via locks. The flow extractor
opportunistically reads from the buffer; if the WS isn't running, it falls
back to candle-based CVD approximation.

The runtime uses `websocket-client` (synchronous, callback-based). The
network code is intentionally narrow — frame parsing + dispatch lives in
pure functions / methods so it can be tested without a live socket.
"""
from __future__ import annotations

import json
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
    """Background-thread WebSocket consumer for Bybit V5 public streams.

    Subscribes to `publicTrade.<symbol>` and `orderbook.50.<symbol>`,
    applies snapshots + deltas to the local order book, forwards trades
    into the buffer. Reconnects with backoff via `websocket-client`'s
    `run_forever(reconnect=...)`. JSON ping every 20s keeps the
    connection alive (Bybit drops idle sockets).

    Frame dispatch is in `handle_frame()` — a pure method that doesn't
    touch the network. Tests inject parsed frames directly.
    """

    def __init__(
        self,
        buffer: WSBuffer,
        symbol: str = "XAUUSD",
        url: str = "wss://stream.bybit.com/v5/public/linear",
        on_error: Optional[Callable[[Exception], None]] = None,
        reconnect_seconds: int = 5,
        ping_interval_seconds: int = 20,
    ) -> None:
        self.buffer = buffer
        self.symbol = symbol
        self.url = url
        self.on_error = on_error
        self.reconnect_seconds = reconnect_seconds
        self.ping_interval_seconds = ping_interval_seconds
        # Local order book state for delta application (price -> size)
        self._bids: Dict[float, float] = {}
        self._asks: Dict[float, float] = {}
        self._book_lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ws_app: Optional[Any] = None  # websocket.WebSocketApp

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="bybit-ws")
        self._thread.start()
        logger.info("WS thread started (%s, %s)", self.symbol, self.url)

    def stop(self) -> None:
        self._stop.set()
        if self._ws_app is not None:
            try:
                self._ws_app.close()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("WS thread stopped")

    # -- frame dispatch (pure, testable) ---------------------------------
    def handle_frame(self, frame: Dict[str, Any]) -> None:
        """Dispatch a parsed Bybit V5 WS frame to the buffer + book state."""
        # Subscription / pong acknowledgements have no `topic` and `success` field
        if "topic" not in frame:
            return
        topic = frame.get("topic", "")
        msg_type = frame.get("type")

        if topic.startswith("publicTrade"):
            data = frame.get("data") or []
            for trade_msg in data:
                trade = trade_from_bybit_msg(trade_msg)
                if trade is not None:
                    self.buffer.push_trade(trade)
            return

        if topic.startswith("orderbook"):
            data = frame.get("data") or {}
            if msg_type == "snapshot":
                self._apply_book_snapshot(data)
            elif msg_type == "delta":
                self._apply_book_delta(data)
            return

    def _apply_book_snapshot(self, data: Dict[str, Any]) -> None:
        with self._book_lock:
            self._bids = {}
            self._asks = {}
            for price_str, size_str in data.get("b", []) or []:
                try:
                    price, size = float(price_str), float(size_str)
                except (TypeError, ValueError):
                    continue
                if size > 0:
                    self._bids[price] = size
            for price_str, size_str in data.get("a", []) or []:
                try:
                    price, size = float(price_str), float(size_str)
                except (TypeError, ValueError):
                    continue
                if size > 0:
                    self._asks[price] = size
        self._publish_book()

    def _apply_book_delta(self, data: Dict[str, Any]) -> None:
        with self._book_lock:
            for price_str, size_str in data.get("b", []) or []:
                try:
                    price, size = float(price_str), float(size_str)
                except (TypeError, ValueError):
                    continue
                if size == 0:
                    self._bids.pop(price, None)
                else:
                    self._bids[price] = size
            for price_str, size_str in data.get("a", []) or []:
                try:
                    price, size = float(price_str), float(size_str)
                except (TypeError, ValueError):
                    continue
                if size == 0:
                    self._asks.pop(price, None)
                else:
                    self._asks[price] = size
        self._publish_book()

    def _publish_book(self) -> None:
        """Push the current top-N book state into the buffer."""
        with self._book_lock:
            bids = [OrderBookLevel(price=p, size=s) for p, s in self._bids.items()]
            asks = [OrderBookLevel(price=p, size=s) for p, s in self._asks.items()]
        self.buffer.update_book(bids, asks)

    # -- network runtime (websocket-client) ------------------------------
    def _run(self) -> None:
        try:
            import websocket  # type: ignore[import-not-found]
        except ImportError:
            logger.error(
                "websocket-client not installed; pip install websocket-client. "
                "WS thread exiting."
            )
            return

        def on_open(ws: Any) -> None:
            sub_msg = {
                "op": "subscribe",
                "args": [
                    f"publicTrade.{self.symbol}",
                    f"orderbook.50.{self.symbol}",
                ],
            }
            ws.send(json.dumps(sub_msg))
            logger.info("WS subscribed: %s", sub_msg["args"])

        def on_message(ws: Any, message: str) -> None:
            try:
                frame = json.loads(message)
            except json.JSONDecodeError:
                logger.warning("WS received non-JSON frame, ignoring")
                return
            try:
                self.handle_frame(frame)
            except Exception as e:  # noqa: BLE001 — never crash the WS thread
                logger.exception("WS frame handler raised: %s", e)
                if self.on_error is not None:
                    try:
                        self.on_error(e)
                    except Exception:  # noqa: BLE001
                        pass

        def on_error(ws: Any, error: BaseException) -> None:
            logger.warning("WS error: %s", error)
            if self.on_error is not None:
                try:
                    self.on_error(error if isinstance(error, Exception) else Exception(str(error)))
                except Exception:  # noqa: BLE001
                    pass

        def on_close(ws: Any, code: Any, msg: Any) -> None:
            logger.info("WS closed (code=%s, msg=%s)", code, msg)

        # WebSocketApp.run_forever blocks; we loop here so that on shutdown
        # `_stop` causes us to exit instead of perpetually reconnecting.
        while not self._stop.is_set():
            try:
                self._ws_app = websocket.WebSocketApp(
                    self.url,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                self._ws_app.run_forever(
                    ping_interval=self.ping_interval_seconds,
                    ping_payload=json.dumps({"op": "ping"}),
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("WS run_forever raised: %s", e)
            if self._stop.is_set():
                break
            logger.info("WS reconnecting in %ds", self.reconnect_seconds)
            self._stop.wait(self.reconnect_seconds)


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
