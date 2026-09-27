"""Blackboard due dates from its calendar feed (Calendar → settings → "Get external calendar link").

The feed URL works without a login, so it's kept in the hub's .env and never shown in the app.
"""

import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from icalendar import Calendar

CACHE_SECONDS = 1800
COURSE = re.compile(r"\b([A-Z]{2,5}\s?\d{3}[A-Z]?)\b")


class BlackboardError(Exception):
    pass


def parse_due(ics: str | bytes, now: datetime, days: int, tz: ZoneInfo) -> list[dict]:
    try:
        cal = Calendar.from_ical(ics)
    except ValueError as e:
        raise BlackboardError("The Blackboard link didn't return a calendar.") from e
    end = now + timedelta(days=days)
    out = []
    for ev in cal.walk("VEVENT"):
        start = ev.decoded("DTSTART", None)
        if start is None:
            continue
        if isinstance(start, datetime):
            due = start.astimezone(tz) if start.tzinfo else start.replace(tzinfo=tz)
            all_day = False
        elif isinstance(start, date):
            due = datetime.combine(start, datetime.max.time().replace(microsecond=0), tz)
            all_day = True
        else:
            continue
        if not (now <= due <= end):
            continue
        title = str(ev.get("SUMMARY", "")).strip() or "(untitled)"
        found = COURSE.search(title) or COURSE.search(str(ev.get("DESCRIPTION", "")))
        out.append({"due": due.isoformat(), "all_day": all_day, "title": title,
                    "course": found.group(1) if found else None})
    return sorted(out, key=lambda d: d["due"])


class Blackboard:
    def __init__(self, url: str | None, client: httpx.AsyncClient | None = None):
        self.url = url.replace("webcal://", "https://", 1) if url else None
        self.client = client or httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        self._cache: tuple[float, bytes] | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url)

    async def due(self, now: datetime, tz: ZoneInfo, days: int = 7) -> list[dict]:
        if not self.url:
            return []
        if not self._cache or time.time() - self._cache[0] > CACHE_SECONDS:
            try:
                r = await self.client.get(self.url)
            except httpx.HTTPError as e:
                raise BlackboardError(f"Couldn't reach Blackboard: {e}") from e
            if r.status_code != 200:
                raise BlackboardError(f"Blackboard feed returned {r.status_code}. The link may have been reset.")
            self._cache = (time.time(), r.content)
        return parse_due(self._cache[1], now, days, tz)
