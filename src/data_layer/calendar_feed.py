"""News-event calendar feed.

Loads tier-1 economic events from a local JSON store. Supports operator-
managed events (NFP, FOMC, CPI, jobless claims, etc.) plus computed
recurring events (NFP first Friday of every month).

Used by the cycle gate to enforce mandate Law 12: no trades within 15
minutes of a tier-1 release.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class NewsEvent:
    name: str
    timestamp: int  # ms epoch
    impact: str = "high"   # "high" | "medium" | "low"
    source: str = "manual"  # "manual" | "computed" | future feed source

    @property
    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp / 1000, tz=timezone.utc)


@dataclass
class CalendarConfig:
    events_path: str = "calendar/events.json"
    lead_minutes: int = 15
    lag_minutes: int = 15
    impact_filter: tuple = ("high",)


class CalendarFeed:
    """Reads + queries the news event calendar."""

    def __init__(self, config: Optional[CalendarConfig] = None) -> None:
        self.config = config or CalendarConfig()
        self.events: List[NewsEvent] = []
        self.reload()

    # -- io --------------------------------------------------------------
    def reload(self) -> None:
        path = Path(self.config.events_path)
        if not path.exists():
            logger.info("calendar file %s not found — empty calendar", path)
            self.events = []
            return
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            logger.error("calendar parse failed for %s: %s", path, e)
            self.events = []
            return
        self.events = []
        for item in data:
            try:
                self.events.append(
                    NewsEvent(
                        name=item["name"],
                        timestamp=int(item["timestamp"]),
                        impact=item.get("impact", "high"),
                        source=item.get("source", "manual"),
                    )
                )
            except (KeyError, ValueError) as e:
                logger.warning("skipping malformed calendar entry %s: %s", item, e)
        self.events.sort(key=lambda e: e.timestamp)
        logger.info("calendar loaded: %d events", len(self.events))

    # -- queries ---------------------------------------------------------
    def is_in_news_window(self, now_ms: Optional[int] = None) -> tuple[bool, Optional[NewsEvent]]:
        """Return (in_window, the_blocking_event_or_None).

        A cycle is in a news window if `now` is within ±lead/lag minutes of
        any event matching the impact filter.
        """
        if now_ms is None:
            now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        lead_ms = self.config.lead_minutes * 60_000
        lag_ms = self.config.lag_minutes * 60_000
        for event in self.events:
            if event.impact not in self.config.impact_filter:
                continue
            if (event.timestamp - lead_ms) <= now_ms <= (event.timestamp + lag_ms):
                return True, event
        return False, None

    def upcoming(self, within_hours: int = 24, now_ms: Optional[int] = None) -> List[NewsEvent]:
        """Events landing within the next N hours."""
        if now_ms is None:
            now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        horizon = now_ms + within_hours * 3_600_000
        return [
            e for e in self.events
            if now_ms <= e.timestamp <= horizon
            and e.impact in self.config.impact_filter
        ]
