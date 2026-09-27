"""Blackboard due dates from its calendar feed (Calendar → settings → "Get external calendar link").

The feed URL works without a login, so it's kept in the hub's .env and never shown in the app.
"""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .ics import Feed, FeedError, events_between

COURSE = re.compile(r"\b([A-Z]{2,5}\s?\d{3}[A-Z]?)\b")


class BlackboardError(Exception):
    pass


def to_due(ev: dict) -> dict:
    found = COURSE.search(ev["title"]) or COURSE.search(ev.get("description") or "")
    due = datetime.fromisoformat(ev["start"])
    if ev["all_day"]:
        due = due.replace(hour=23, minute=59, second=59)
    return {"due": due.isoformat(), "all_day": ev["all_day"], "title": ev["title"],
            "course": found.group(1) if found else None}


def parse_due(ics: str | bytes, now: datetime, days: int, tz: ZoneInfo) -> list[dict]:
    try:
        evs = events_between(ics, now.replace(hour=0, minute=0, second=0, microsecond=0), now + timedelta(days=days), tz)
    except FeedError as e:
        raise BlackboardError("The Blackboard link didn't return a calendar.") from e
    items = [to_due(e) for e in evs]
    return [d for d in items if now <= datetime.fromisoformat(d["due"]) <= now + timedelta(days=days)]


class Blackboard:
    def __init__(self, url: str | None, client: httpx.AsyncClient | None = None):
        self.feed = Feed(url, client)

    @property
    def url(self) -> str | None:
        return self.feed.url

    @property
    def configured(self) -> bool:
        return self.feed.configured

    async def _ics(self) -> bytes:
        try:
            return await self.feed.fetch()
        except FeedError as e:
            raise BlackboardError(str(e).replace("calendar link", "Blackboard feed")) from e

    async def due(self, now: datetime, tz: ZoneInfo, days: int = 7) -> list[dict]:
        if not self.configured:
            return []
        return parse_due(await self._ics(), now, days, tz)

    async def between(self, start: datetime, end: datetime, tz: ZoneInfo) -> list[dict]:
        """Due items in [start, end), e.g. one day of the calendar view."""
        if not self.configured:
            return []
        try:
            evs = events_between(await self._ics(), start, end, tz)
        except FeedError as e:
            raise BlackboardError(str(e)) from e
        return [to_due(e) for e in evs]
