"""Calendar feeds by link (iCal/.ics): Blackboard, iCloud public calendars, anything that offers a link.

Repeating events (a weekly class, say) are expanded, so a feed can be read for any day.
"""

import time
from datetime import date, datetime

import httpx
import recurring_ical_events
from icalendar import Calendar

CACHE_SECONDS = 1800


class FeedError(Exception):
    pass


def _as_dt(value, tz, end_of_day: bool = False) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        return (value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)), False
    t = datetime.max.time().replace(microsecond=0) if end_of_day else datetime.min.time()
    return datetime.combine(value, t, tz), True


def events_between(ics: str | bytes, start: datetime, end: datetime, tz) -> list[dict]:
    """Every event overlapping [start, end), with repeats expanded."""
    try:
        cal = Calendar.from_ical(ics)
    except ValueError as e:
        raise FeedError("That link didn't return a calendar.") from e
    try:
        found = recurring_ical_events.of(cal).between(start, end)
    except Exception as e:  # malformed repeat rules shouldn't take the whole view down
        raise FeedError(f"Couldn't read that calendar's repeating events: {e}") from e
    out = []
    for ev in found:
        s_raw = ev.decoded("DTSTART", None)
        if s_raw is None:
            continue
        s, all_day = _as_dt(s_raw, tz)
        e_raw = ev.decoded("DTEND", None)
        e = _as_dt(e_raw, tz)[0] if e_raw is not None else s
        out.append({"start": s.isoformat(), "end": e.isoformat(), "all_day": all_day,
                    "title": str(ev.get("SUMMARY", "")).strip() or "(untitled)",
                    "description": str(ev.get("DESCRIPTION", "")), "location": str(ev.get("LOCATION", "")) or None,
                    "uid": str(ev.get("UID", ""))})
    return sorted(out, key=lambda x: x["start"])


class Feed:
    """Fetches a calendar link, keeping the result for 30 minutes."""

    def __init__(self, url: str | None, client: httpx.AsyncClient | None = None):
        self.url = url.replace("webcal://", "https://", 1) if url else None
        self.client = client or httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        self._cache: tuple[float, bytes] | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url)

    async def fetch(self) -> bytes:
        if not self.url:
            raise FeedError("No link set.")
        if not self._cache or time.time() - self._cache[0] > CACHE_SECONDS:
            try:
                r = await self.client.get(self.url)
            except httpx.HTTPError as e:
                raise FeedError(f"Couldn't reach the calendar link: {e}") from e
            if r.status_code != 200:
                raise FeedError(f"The calendar link returned {r.status_code}. It may have been reset.")
            self._cache = (time.time(), r.content)
        return self._cache[1]

    async def between(self, start: datetime, end: datetime, tz) -> list[dict]:
        return events_between(await self.fetch(), start, end, tz)


def day_bounds(d: date, tz) -> tuple[datetime, datetime]:
    from datetime import timedelta
    start = datetime.combine(d, datetime.min.time(), tz)
    return start, start + timedelta(days=1)
