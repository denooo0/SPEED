#!/usr/bin/env python3
"""Generate recurring tier-1 calendar events for the next N months.

Currently emits:
  - NFP — first Friday of each month at 12:30 UTC
  - CPI — second Wednesday of each month at 12:30 UTC (typical schedule)

Other events (FOMC, ECB, BoE, individual Powell speeches) require an actual
schedule lookup and should be added by the operator. This script gives you
the predictable recurring backbone.

Usage:
    python scripts/build_calendar.py > calendar/events.json
    python scripts/build_calendar.py --months 6 --merge calendar/events.json
"""
from __future__ import annotations

import argparse
import json
import sys
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List


def first_friday(year: int, month: int) -> datetime:
    """First Friday of the given month."""
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    # weekday(): Monday=0..Sunday=6; Friday=4
    days_until_friday = (4 - first.weekday()) % 7
    return first + timedelta(days=days_until_friday)


def second_wednesday(year: int, month: int) -> datetime:
    """Second Wednesday of the given month — close to canonical CPI release."""
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    # Wednesday = 2
    days_until_wednesday = (2 - first.weekday()) % 7
    first_wednesday = first + timedelta(days=days_until_wednesday)
    return first_wednesday + timedelta(days=7)


def at_utc(dt: datetime, hour: int = 12, minute: int = 30) -> int:
    """Return ms-epoch for the given date at HH:MM UTC."""
    moment = dt.replace(hour=hour, minute=minute, second=0, microsecond=0, tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def iter_months(start: datetime, count: int):
    """Yield (year, month) pairs starting from `start` for `count` months."""
    y, m = start.year, start.month
    for _ in range(count):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def build_recurring(months: int, start: datetime | None = None) -> List[dict]:
    if start is None:
        start = datetime.now(tz=timezone.utc)
    events: List[dict] = []
    for y, m in iter_months(start, months):
        nfp_date = first_friday(y, m)
        events.append({
            "name": f"NFP {nfp_date.strftime('%b %Y')}",
            "timestamp": at_utc(nfp_date),
            "impact": "high",
            "source": "computed",
        })
        cpi_date = second_wednesday(y, m)
        # Sanity: must be a real day in this month
        if cpi_date.month == m and cpi_date.day <= monthrange(y, m)[1]:
            events.append({
                "name": f"US CPI {cpi_date.strftime('%b %Y')}",
                "timestamp": at_utc(cpi_date),
                "impact": "high",
                "source": "computed",
            })
    events.sort(key=lambda e: e["timestamp"])
    return events


def merge_with_existing(generated: List[dict], path: Path) -> List[dict]:
    """Combine generated events with operator-managed entries from a file.

    Operator entries (source != 'computed') are preserved verbatim. Computed
    entries are replaced wholesale to allow regeneration without duplicates.
    """
    if not path.exists():
        return generated
    try:
        existing = json.loads(path.read_text())
    except json.JSONDecodeError:
        return generated
    operator = [e for e in existing if e.get("source") != "computed"]
    combined = operator + generated
    # Dedupe on (name, timestamp)
    seen = set()
    unique = []
    for e in combined:
        key = (e.get("name"), e.get("timestamp"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    unique.sort(key=lambda e: e.get("timestamp", 0))
    return unique


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--months", type=int, default=4, help="Months ahead to generate (default 4)")
    parser.add_argument(
        "--merge",
        type=str,
        default=None,
        help="Path to existing events.json — merge operator entries with regenerated recurring",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="Write to this path instead of stdout",
    )
    args = parser.parse_args(argv[1:])

    events = build_recurring(months=args.months)
    if args.merge:
        events = merge_with_existing(events, Path(args.merge))

    text = json.dumps(events, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {len(events)} events to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
