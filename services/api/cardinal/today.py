"""One snapshot of your day: calendar, Blackboard deadlines and both inboxes.

The Today view shows it, Ordinal's briefing is written from it, and agents see the parts they're allowed
to (their `access` list in agents.yaml). The allow-list is enforced here, not left to the model.
"""

import asyncio
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import Session

from .config import Settings
from .sources.blackboard import Blackboard, BlackboardError
from .sources.google import SLOTS, Google, GoogleError

CACHE_SECONDS = 300
CHAT_WAIT_SECONDS = 5.0
ACCESS = ("calendar", "email", "blackboard")


class Today:
    def __init__(self, settings: Settings, google: Google, blackboard: Blackboard, calendar=None):
        self.settings = settings
        self.google = google
        self.blackboard = blackboard
        self.calendar = calendar  # adds your own Cardinal items and calendar links (iCloud etc.)
        self._cache: tuple[float, dict] | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.timezone)

    async def snapshot(self, session: Session, force: bool = False, now: datetime | None = None) -> dict:
        if not force and self._cache and time.time() - self._cache[0] < CACHE_SECONDS:
            return self._cache[1]
        tz = self.tz
        now = now or datetime.now(tz)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        sources, events, inboxes, due = [], [], [], []

        for slot, label in SLOTS.items():
            acct = self.google.account(session, slot)
            src = {"id": f"google:{slot}", "label": label, "kind": "google", "slot": slot,
                   "status": "not_connected", "detail": None}
            if acct:
                src.update(status="ok", detail=acct.email)
                try:
                    events += await self.google.events(session, acct, day_start, day_start + timedelta(days=2), tz)
                    inboxes.append(await self.google.inbox(session, acct))
                except GoogleError as e:
                    src.update(status="error", detail=str(e))
            sources.append(src)

        if self.calendar:
            window = (day_start, day_start + timedelta(days=2))
            events += self.calendar.items_between(session, *window)
            extra, _errors = await self.calendar.feeds_between(session, *window)
            events += extra
            if self.calendar.feeds(session):
                sources.append({"id": "feeds", "label": "Calendar links", "kind": "feeds",
                                "status": "error" if _errors else "ok",
                                "detail": "; ".join(e["detail"] for e in _errors) or None})
        events.sort(key=lambda e: e["start"])

        bb = {"id": "blackboard", "label": "Blackboard", "kind": "blackboard", "status": "not_connected", "detail": None}
        if self.blackboard.configured:
            try:
                due = await self.blackboard.due(now, tz)
                bb["status"] = "ok"
            except BlackboardError as e:
                bb.update(status="error", detail=str(e))
        sources.append(bb)

        snap = {"generated_at": now.isoformat(), "timezone": self.settings.timezone, "sources": sources,
                "events": events, "due": due, "inbox": inboxes}
        self._cache = (time.time(), snap)
        return snap

    async def for_chat(self, session: Session) -> dict | None:
        """A recent snapshot without making a chat wait long on Google."""
        try:
            return await asyncio.wait_for(self.snapshot(session), CHAT_WAIT_SECONDS)
        except TimeoutError:
            return self._cache[1] if self._cache else None

    def invalidate(self) -> None:
        self._cache = None


def _clock(dt: datetime) -> str:
    return f"{dt:%H:%M}"


def _day(dt: datetime) -> str:
    return f"{dt:%a} {dt.day} {dt:%b}"


def counts(snap: dict) -> str:
    unread = sum(i["unread"] for i in snap["inbox"])
    parts = [f"{len(snap['events'])} events", f"{len(snap['due'])} due"]
    if snap["inbox"]:
        parts.append(f"{unread} unread")
    return " · ".join(parts)


def connected(snap: dict) -> bool:
    return any(s["status"] == "ok" for s in snap["sources"])


def context_text(snap: dict | None, access: set[str]) -> str:
    """The data block for an agent's prompt, limited to what that agent may see."""
    access = access & set(ACCESS)
    if not access:
        return ""
    if snap is None:
        return "Couldn't load the user's accounts just now. Say so if asked about schedule, email or deadlines."
    tz = ZoneInfo(snap["timezone"])
    now = datetime.fromisoformat(snap["generated_at"]).astimezone(tz)
    today, tomorrow = now.date(), (now + timedelta(days=1)).date()
    status = {s["id"]: s for s in snap["sources"]}
    google_ok = [s for s in snap["sources"] if s["kind"] == "google" and s["status"] == "ok"]
    lines = [f"Now: {_day(now)}, {_clock(now)}."]

    if "calendar" in access:
        if not google_ok and not snap["events"]:
            lines.append("Calendar: not connected.")
        else:
            for label, d in (("Today", today), ("Tomorrow", tomorrow)):
                items = []
                for e in snap["events"]:
                    s = datetime.fromisoformat(e["start"]).astimezone(tz)
                    if s.date() != d:
                        continue
                    when = "all day" if e["all_day"] else f"{_clock(s)}-{_clock(datetime.fromisoformat(e['end']).astimezone(tz))}"
                    where = f" at {e['location']}" if e.get("location") else ""
                    items.append(f"{when} {e['title']}{where} [{e['calendar']}]")
                lines.append(f"Calendar {label.lower()} ({_day(datetime.combine(d, now.time(), tz))}): "
                             + ("; ".join(items) if items else "nothing scheduled") + ".")

    if "blackboard" in access:
        bb = status.get("blackboard", {})
        if bb.get("status") != "ok":
            lines.append("Blackboard: " + ("not connected." if bb.get("status") == "not_connected" else "couldn't load."))
        else:
            items = []
            for d in snap["due"][:10]:
                due = datetime.fromisoformat(d["due"]).astimezone(tz)
                course = f" ({d['course']})" if d.get("course") else ""
                items.append(f"{_day(due)} {'end of day' if d['all_day'] else _clock(due)} {d['title']}{course}")
            lines.append("Blackboard due in the next 7 days: " + ("; ".join(items) if items else "nothing") + ".")

    if "email" in access:
        if not google_ok:
            lines.append("Email: not connected.")
        for box in snap["inbox"]:
            name = SLOTS.get(box["account"], box["account"])
            subjects = "; ".join(f"\"{m['subject']}\" from {m['from']}" + (" (important)" if m["important"] else "")
                                 for m in box["recent"][:6])
            lines.append(f"{name} inbox: {box['unread']} unread. Recent unread (last 2 days): {subjects or 'none'}.")

    for s in snap["sources"]:
        relevant = ("blackboard" in access if s["kind"] == "blackboard"
                    else "calendar" in access if s["kind"] == "feeds" else bool(access & {"calendar", "email"}))
        if s["status"] == "error" and relevant:
            lines.append(f"{s['label']} couldn't be read: {s['detail']}")
    return "\n".join(lines)
