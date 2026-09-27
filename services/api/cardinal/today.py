"""One snapshot of your day: calendar, Blackboard deadlines and both inboxes.

The Today view shows it, Ordinal's briefing is written from it, and agents see the parts they're allowed
to (their `access` list in agents.yaml). The allow-list is enforced here, not left to the model.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import Session

from .config import Settings
from .db import get_engine
from .sources.blackboard import Blackboard, BlackboardError
from .sources.google import SLOTS, Google, GoogleError

log = logging.getLogger("cardinal.today")

CACHE_SECONDS = 300
WARM_SECONDS = 240  # the scheduler refreshes a bit before the cache runs out
MAX_STALE_SECONDS = 1800  # older than this, wait for fresh data instead of showing it
CHAT_WAIT_SECONDS = 5.0
ACCESS = ("calendar", "email", "blackboard")


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        log.error("Background refresh failed", exc_info=task.exception())


class Today:
    def __init__(self, settings: Settings, google: Google, blackboard: Blackboard, calendar=None):
        self.settings = settings
        self.google = google
        self.blackboard = blackboard
        self.calendar = calendar  # adds your own Cardinal items and calendar links (iCloud etc.)
        self._cache: tuple[float, dict] | None = None
        self._task: asyncio.Task | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.timezone)

    async def snapshot(self, session: Session, force: bool = False, now: datetime | None = None) -> dict:
        """The day's data. A fresh copy is returned as is; a stale one (under 30 min) is returned at once while a
        new one is fetched in the background, so opening Today never waits on Google. The scheduler keeps it warm."""
        age = time.time() - self._cache[0] if self._cache else None
        if not force and age is not None:
            if age < CACHE_SECONDS:
                return self._cache[1]
            if age < MAX_STALE_SECONDS:
                self.refresh_soon()
                return self._cache[1]
        return await self._build(session, now)

    def warm(self) -> None:
        """Called by the scheduler: refresh a little before the cache runs out."""
        if not self._cache or time.time() - self._cache[0] > WARM_SECONDS:
            self.refresh_soon()

    def refresh_soon(self) -> None:
        if self._task and not self._task.done():
            return

        async def run():
            with Session(get_engine()) as s:
                await self._build(s)
        self._task = asyncio.create_task(run())
        self._task.add_done_callback(_log_failure)

    async def _build(self, session: Session, now: datetime | None = None) -> dict:
        tz = self.tz
        now = now or datetime.now(tz)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        window = (day_start, day_start + timedelta(days=2))

        async def account(slot: str, label: str):
            acct = self.google.account(session, slot)
            src = {"id": f"google:{slot}", "label": label, "kind": "google", "slot": slot,
                   "status": "not_connected", "detail": None}
            if not acct:
                return src, [], None
            src.update(status="ok", detail=acct.email)
            events, box = await asyncio.gather(self.google.events(session, acct, *window, tz),
                                               self.google.inbox(session, acct), return_exceptions=True)
            err = next((x for x in (events, box) if isinstance(x, BaseException)), None)
            if err:
                if not isinstance(err, GoogleError):
                    raise err
                src.update(status="error", detail=str(err))
                return src, [], None
            return src, events, box

        async def own():  # your Cardinal items and calendar links (iCloud etc.)
            if not self.calendar:
                return [], None
            extra, errors = await self.calendar.feeds_between(session, *window)
            src = None
            if self.calendar.feeds(session):
                src = {"id": "feeds", "label": "Calendar links", "kind": "feeds", "status": "error" if errors else "ok",
                       "detail": "; ".join(e["detail"] for e in errors) or None}
            return self.calendar.items_between(session, *window) + extra, src

        async def blackboard():
            bb = {"id": "blackboard", "label": "Blackboard", "kind": "blackboard", "status": "not_connected", "detail": None}
            if not self.blackboard.configured:
                return bb, []
            try:
                due = await self.blackboard.due(now, tz)
                bb["status"] = "ok"
                return bb, due
            except BlackboardError as e:
                bb.update(status="error", detail=str(e))
                return bb, []

        # Everything at once: both accounts, calendar links and Blackboard.
        accounts, (own_events, feeds_src), (bb, due) = await asyncio.gather(
            asyncio.gather(*(account(slot, label) for slot, label in SLOTS.items())), own(), blackboard())
        sources = [a[0] for a in accounts] + ([feeds_src] if feeds_src else []) + [bb]
        events = sorted([e for a in accounts for e in a[1]] + own_events, key=lambda e: e["start"])
        inboxes = [a[2] for a in accounts if a[2]]
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
