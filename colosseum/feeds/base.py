"""Tick feeds: the senses, with failover.

A feed is anything that yields `Tick`. That deliberate minimalism means a
broker websocket, a REST poller, a CSV archive, and the synthetic generator are
all interchangeable -- and critically, the REPLAY feed is the same interface as
the live one, so recovery replays the exact code path that produced the fault.

The FailoverFeed is the practical answer to bottleneck #1: a primary that dies
must not take the engine with it. It promotes a secondary, records the switch as
an incident, and marks the boundary so the learner knows which data came from
where.
"""
from __future__ import annotations

import csv
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, List, Optional

from ..core.clock import NS
from ..core.types import Quality, Tick


class Feed(ABC):
    name: str = "feed"

    @abstractmethod
    def stream(self) -> Iterator[Tick]:
        ...

    def close(self) -> None:
        pass


class IterableFeed(Feed):
    """Wraps any iterable of Ticks. Used for archives, tests, and replay."""

    def __init__(self, ticks: Iterable[Tick], name: str = "iterable"):
        self._ticks = ticks
        self.name = name

    def stream(self) -> Iterator[Tick]:
        yield from self._ticks


class CsvFeed(Feed):
    """Archive replay. Expects columns: t_ns|timestamp, bid, ask, volume.

    Accepts either epoch nanos or ISO-8601 so you can point it at most vendor
    dumps without a conversion step.
    """

    def __init__(self, path: str | Path, name: str = "csv"):
        self.path = Path(path)
        self.name = name

    def stream(self) -> Iterator[Tick]:
        import datetime as _dt
        with open(self.path, newline="") as fh:
            for row in csv.DictReader(fh):
                raw = row.get("t_ns") or row.get("timestamp") or row.get("time")
                if raw is None:
                    continue
                if str(raw).isdigit():
                    t_ns = int(raw)
                    if t_ns < 10 ** 15:          # seconds -> nanos
                        t_ns *= NS
                else:
                    t_ns = int(_dt.datetime.fromisoformat(
                        str(raw).replace("Z", "+00:00")).timestamp() * NS)
                try:
                    yield Tick(t_ns=t_ns, bid=float(row["bid"]),
                               ask=float(row["ask"]),
                               volume=float(row.get("volume", 1.0)),
                               t_ingest_ns=t_ns)
                except (KeyError, ValueError):
                    continue      # a malformed archive row is not fatal


class JsonlFeed(Feed):
    """Ticks as archived by the engine itself -- the canonical replay source."""

    def __init__(self, path: str | Path, name: str = "jsonl"):
        self.path = Path(path)
        self.name = name

    def stream(self) -> Iterator[Tick]:
        with open(self.path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    yield Tick(t_ns=int(d["t_ns"]), bid=float(d["bid"]),
                               ask=float(d["ask"]), volume=float(d.get("volume", 1.0)),
                               t_ingest_ns=int(d.get("t_ingest_ns", d["t_ns"])))
                except Exception:
                    continue


class TickArchiver:
    """Writes every accepted tick to a daily JSONL.

    This is not optional infrastructure -- it IS the substrate that makes
    replay-rebuild possible. Without an immutable tick archive, "re-derive state
    from the log" has nothing to re-derive from.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._fh = None
        self._day = ""
        self.written = 0

    def write(self, t: Tick, session_date: str) -> None:
        if session_date != self._day:
            if self._fh:
                self._fh.close()
            y, m, d = session_date.split("-")
            p = self.root / y / m
            p.mkdir(parents=True, exist_ok=True)
            self._fh = open(p / f"ticks-{session_date}.jsonl", "a")
            self._day = session_date
        self._fh.write(f'{{"t_ns":{t.t_ns},"bid":{t.bid},"ask":{t.ask},'
                       f'"volume":{t.volume},"t_ingest_ns":{t.t_ingest_ns}}}\n')
        self.written += 1
        if self.written % 500 == 0:
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.flush()
            self._fh.close()
            self._fh = None


@dataclass
class FeedHealth:
    switches: int = 0
    reconnects: int = 0
    last_tick_ns: int = 0
    active: str = ""
    history: List[str] = field(default_factory=list)


class FailoverFeed(Feed):
    """Primary with ordered fallbacks and bounded exponential backoff.

    Silence is treated as failure, not patience: a feed that stops printing
    without erroring is the more dangerous case, because nothing raises and the
    engine would happily reason about frozen tape.
    """

    def __init__(self, feeds: List[Feed], *, silence_timeout_s: float = 45.0,
                 max_backoff_s: float = 30.0,
                 on_switch: Optional[Callable[[str, str, str], None]] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 now: Optional[Callable[[], float]] = None):
        if not feeds:
            raise ValueError("at least one feed required")
        self.feeds = feeds
        self.name = "failover"
        self.silence_timeout_s = silence_timeout_s
        self.max_backoff_s = max_backoff_s
        self.on_switch = on_switch
        self._sleep = sleep or time.sleep
        self._now = now or time.monotonic
        self.health = FeedHealth(active=feeds[0].name)
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def stream(self) -> Iterator[Tick]:
        idx, backoff = 0, 1.0
        while not self._stop:
            feed = self.feeds[idx]
            self.health.active = feed.name
            self.health.history.append(feed.name)
            last_seen = self._now()
            produced = False
            try:
                for tick in feed.stream():
                    if self._stop:
                        return
                    produced = True
                    backoff = 1.0            # a healthy tick resets the ladder
                    last_seen = self._now()
                    self.health.last_tick_ns = tick.t_ns
                    yield tick
                reason = "stream_ended"
            except Exception as e:
                reason = f"error:{type(e).__name__}"

            if self._now() - last_seen > self.silence_timeout_s and produced:
                reason = "silence_timeout"

            nxt = (idx + 1) % len(self.feeds)
            if len(self.feeds) > 1 and nxt != idx:
                self.health.switches += 1
                if self.on_switch:
                    self.on_switch(feed.name, self.feeds[nxt].name, reason)
                idx = nxt
            else:
                self.health.reconnects += 1
                self._sleep(min(backoff, self.max_backoff_s))
                backoff *= 2

    def close(self) -> None:
        for f in self.feeds:
            f.close()


class WebsocketFeedTemplate(Feed):
    """Concrete integration point for a live broker.

    Left as a template on purpose: every broker's auth, subscribe message, and
    payload shape differ, and a fake implementation would be worse than an
    honest seam. Implement `_parse` and `_connect` for your provider (OANDA
    v20, Dukascopy, Polygon FX, an MT5 bridge) and the rest of the engine needs
    no changes at all.

    Two rules your implementation must honour:
      1. Emit BROKER event time as `t_ns` and local receipt as `t_ingest_ns` --
         the gap between them is how clock skew is measured.
      2. Never synthesize a tick to fill silence. Silence is data; the Guardian
         needs to see it.
    """

    def __init__(self, url: str, symbol: str = "XAU_USD", name: str = "ws"):
        self.url = url
        self.symbol = symbol
        self.name = name

    def _connect(self):
        raise NotImplementedError(
            "Implement _connect() for your broker's websocket/auth.")

    def _parse(self, msg) -> Optional[Tick]:
        raise NotImplementedError(
            "Implement _parse() to map your broker's payload to a Tick.")

    def stream(self) -> Iterator[Tick]:
        conn = self._connect()
        for msg in conn:
            t = self._parse(msg)
            if t is not None:
                yield t
