"""The calendar view: one day at a time, merged from every source, plus items you add in Cardinal.

Sources for a day: both Google accounts, Blackboard due dates, calendar links (e.g. iCloud) and Cardinal's own
items. Your own items are mirrored to the "Cardinal" Google calendar when that's allowed, so they also show
in Apple Calendar on any device that has the Google account added.
"""

import asyncio
import logging
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import Session, col, select

from .config import Settings
from .db import CalendarFeed, CalendarItem, get_engine
from .sources.blackboard import Blackboard, BlackboardError
from .sources.google import SLOTS, Google, GoogleError
from .sources.ics import Feed, FeedError, day_bounds
from .vault import Vault, VaultError

log = logging.getLogger("cardinal.calendar")

KINDS = ("event", "class", "due", "exam", "quiz", "reading", "no_class")
KIND_COLORS = {"event": "#22e3c4", "class": "#3d8bff", "due": "#ffb13d", "exam": "#ff4d6d",
               "quiz": "#ff7a45", "reading": "#a47bff", "no_class": "#7d8ba3"}
FEED_COLORS = ["#a47bff", "#ff4fd8", "#9dff4a", "#22e3c4", "#e0e8ff"]
BLACKBOARD_COLOR = "#ffb13d"
BACK_DAYS, AHEAD_DAYS = 7, 120
CACHE_SECONDS = 180
WARM_SECONDS = 150
MAX_STALE_SECONDS = 1800


class CalendarError(Exception):
    pass


def item_event(it: CalendarItem, tz: ZoneInfo) -> dict:
    return {"id": f"item:{it.id}", "item_id": it.id, "start": it.start.astimezone(tz).isoformat(),
            "end": it.end.astimezone(tz).isoformat(), "all_day": it.all_day, "title": it.title,
            "calendar": it.course or "Cardinal", "source": "cardinal", "kind": it.kind,
            "color": KIND_COLORS.get(it.kind, KIND_COLORS["event"]), "notes": it.notes,
            "synced": bool(it.google_event_id), "deletable": True}


def parse_hhmm(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    hh, mm = value.split(":")[:2]
    h, m = int(hh), int(mm)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(value)
    return h, m


def make_times(day: date, start: str | None, end: str | None, tz: ZoneInfo) -> tuple[datetime, datetime, bool]:
    """(start, end, all_day) from a date and optional HH:MM times. No start time means all day."""
    s = parse_hhmm(start)
    if s is None:
        begin = datetime.combine(day, datetime.min.time(), tz)
        return begin, begin + timedelta(days=1), True
    begin = datetime.combine(day, datetime.min.time(), tz).replace(hour=s[0], minute=s[1])
    e = parse_hhmm(end)
    finish = begin.replace(hour=e[0], minute=e[1]) if e else begin + timedelta(hours=1)
    if finish <= begin:
        finish = begin + timedelta(minutes=30)
    return begin, finish, False


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        log.error("Background calendar refresh failed", exc_info=task.exception())


class Calendar:
    def __init__(self, settings: Settings, google: Google, blackboard: Blackboard, vault: Vault, feed_client=None):
        self.settings = settings
        self.google = google
        self.blackboard = blackboard
        self.vault = vault
        self.feed_client = feed_client
        self._feeds: dict[int, Feed] = {}
        self._cache: dict[str, tuple[float, dict]] = {}  # per day: what came from Google, links and Blackboard
        self._tasks: dict[str, asyncio.Task] = {}
        self.proposals = None  # set by Actions: pending blocks shown as ghosts until you decide

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.timezone)

    def today(self) -> date:
        return datetime.now(self.tz).date()

    def invalidate(self) -> None:
        self._cache.clear()

    # ---------- Reading ----------

    def items_between(self, session: Session, start: datetime, end: datetime) -> list[dict]:
        rows = session.exec(select(CalendarItem).where(CalendarItem.start < end, CalendarItem.end > start)
                            .order_by(col(CalendarItem.start))).all()
        return [item_event(r, self.tz) for r in rows]

    async def feeds_between(self, session: Session, start: datetime, end: datetime) -> tuple[list[dict], list[dict]]:
        async def one(f: CalendarFeed) -> tuple[list[dict], dict | None]:
            try:
                feed = self._feeds.get(f.id)
                if feed is None:
                    feed = self._feeds[f.id] = Feed(self.vault.decrypt(f.url_enc), self.feed_client)
                return [{**e, "id": f"feed:{f.id}:{e['uid']}:{e['start']}", "calendar": f.name, "source": "feed",
                         "kind": "event", "color": f.color, "deletable": False}
                        for e in await feed.between(start, end, self.tz)], None
            except (FeedError, VaultError) as e:
                return [], {"source": f.name, "detail": str(e)}
        results = await asyncio.gather(*(one(f) for f in session.exec(select(CalendarFeed)).all()))
        return [e for evs, _ in results for e in evs], [err for _, err in results if err]

    async def day(self, session: Session, d: date, force: bool = False) -> dict:
        """One day: Google, calendar links and Blackboard (cached, refreshed in the background when stale), plus
        your own items and pending proposals, which are read fresh every time so they're never out of date."""
        today = self.today()
        if not (today - timedelta(days=BACK_DAYS) <= d <= today + timedelta(days=AHEAD_DAYS)):
            raise CalendarError(f"Pick a day from {BACK_DAYS} days ago to {AHEAD_DAYS} days ahead.")
        remote = await self._remote(session, d, force)
        start, end = day_bounds(d, self.tz)
        events = remote["events"] + self.items_between(session, start, end)
        if self.proposals:
            events += self.proposals(session, start, end)
        # Keep only what overlaps this day (a guard against sources that return a wider window).
        events = [e for e in events if datetime.fromisoformat(e["start"]) < end
                  and max(datetime.fromisoformat(e["end"]), datetime.fromisoformat(e["start"])) >= start]
        return {"date": d.isoformat(), "timezone": self.settings.timezone, "today": today.isoformat(),
                "min_date": (today - timedelta(days=BACK_DAYS)).isoformat(),
                "max_date": (today + timedelta(days=AHEAD_DAYS)).isoformat(),
                "events": sorted(events, key=lambda e: (not e["all_day"], e["start"])), "due": remote["due"],
                "errors": remote["errors"], "can_sync": self.google.can_write(self.google.account(session, "personal"))}

    async def _remote(self, session: Session, d: date, force: bool) -> dict:
        hit = self._cache.get(d.isoformat())
        age = time.time() - hit[0] if hit else None
        if hit and not force:
            if age < CACHE_SECONDS:
                return hit[1]
            if age < MAX_STALE_SECONDS:
                self._refresh_soon(d)
                return hit[1]
        return await self._fetch_remote(session, d)

    def warm(self, days: list[date]) -> None:
        """Called by the scheduler for today and tomorrow, so opening the calendar never waits."""
        for d in days:
            hit = self._cache.get(d.isoformat())
            if not hit or time.time() - hit[0] > WARM_SECONDS:
                self._refresh_soon(d)

    def _refresh_soon(self, d: date) -> None:
        key = d.isoformat()
        if key in self._tasks and not self._tasks[key].done():
            return

        async def run():
            with Session(get_engine()) as s:
                await self._fetch_remote(s, d)
        self._tasks[key] = asyncio.create_task(run())
        self._tasks[key].add_done_callback(_log_failure)

    async def _fetch_remote(self, session: Session, d: date) -> dict:
        tz = self.tz
        start, end = day_bounds(d, tz)

        async def account(slot: str, label: str) -> tuple[list[dict], dict | None]:
            acct = self.google.account(session, slot)
            if not acct:
                return [], None
            try:
                return [{**e, "source": "google", "kind": "event", "color": e.get("color") or "#3d8bff", "deletable": False}
                        for e in await self.google.events(session, acct, start, end, tz)], None
            except GoogleError as e:
                return [], {"source": label, "detail": str(e)}

        async def blackboard() -> tuple[list[dict], dict | None]:
            try:
                return [{**dd, "color": BLACKBOARD_COLOR, "source": "blackboard"}
                        for dd in await self.blackboard.between(start, end, tz)], None
            except BlackboardError as e:
                return [], {"source": "Blackboard", "detail": str(e)}

        # Both Google accounts, every calendar link and Blackboard at once.
        accounts, (feed_events, feed_errors), (due, bb_error) = await asyncio.gather(
            asyncio.gather(*(account(slot, label) for slot, label in SLOTS.items())),
            self.feeds_between(session, start, end), blackboard())
        out = {"events": [e for evs, _ in accounts for e in evs] + feed_events, "due": due,
               "errors": [err for _, err in accounts if err] + feed_errors + ([bb_error] if bb_error else [])}
        self._cache[d.isoformat()] = (time.time(), out)
        return out

    # ---------- Your own items ----------

    async def _mirror(self, session: Session, item: CalendarItem) -> str | None:
        """Copy an item to the Cardinal Google calendar. Returns a warning if it couldn't."""
        acct = self.google.account(session, "personal")
        if not self.google.can_write(acct):
            return "Saved in Cardinal. Reconnect Personal Google in Access → Accounts to also put it in Google and Apple Calendar."
        try:
            item.google_event_id = await self.google.put_event(session, acct, item, self.tz)
            session.add(item)
            session.commit()
        except GoogleError as e:
            return f"Saved in Cardinal, but Google didn't take it: {e}"
        return None

    async def add(self, session: Session, *, title: str, day: date, start: str | None = None, end: str | None = None,
                  kind: str = "event", course: str | None = None, notes: str | None = None,
                  source: str = "manual", mirror: bool = True) -> tuple[CalendarItem, str | None]:
        title = title.strip()
        if not title:
            raise CalendarError("Give it a title.")
        if kind not in KINDS:
            kind = "event"
        try:
            begin, finish, all_day = make_times(day, start, end, self.tz)
        except ValueError as e:
            raise CalendarError("Times should look like 14:30.") from e
        item = CalendarItem(title=title[:200], start=begin, end=finish, all_day=all_day, kind=kind,
                            course=(course or "").strip()[:40] or None, notes=(notes or "").strip()[:2000] or None,
                            source=source)
        session.add(item)
        session.commit()
        session.refresh(item)
        warning = await self._mirror(session, item) if mirror else None
        session.refresh(item)  # saving the Google copy expired it
        self.invalidate()
        return item, warning

    async def move(self, session: Session, item_id: int, day: date, start: str | None, end: str | None) -> tuple[CalendarItem, str | None]:
        """Change an item's day and time (and its Google copy)."""
        item = session.get(CalendarItem, item_id)
        if not item:
            raise CalendarError("That item isn't on your calendar any more.")
        if start and not end and not item.all_day:
            end_min = parse_hhmm(start)[0] * 60 + parse_hhmm(start)[1] + int((item.end - item.start).total_seconds() // 60)
            end = f"{min(end_min, 1439) // 60:02d}:{min(end_min, 1439) % 60:02d}"  # keep the same length
        try:
            item.start, item.end, item.all_day = make_times(day, start, end, self.tz)
        except ValueError as e:
            raise CalendarError("Times should look like 14:30.") from e
        session.add(item)
        session.commit()
        warning = await self._mirror(session, item) if item.google_event_id or self.google.can_write(
            self.google.account(session, "personal")) else None
        session.refresh(item)
        self.invalidate()
        return item, warning

    async def delete(self, session: Session, item_id: int) -> None:
        item = session.get(CalendarItem, item_id)
        if not item:
            raise CalendarError("That item is already gone.")
        if item.google_event_id:
            acct = self.google.account(session, "personal")
            if acct:
                try:
                    await self.google.delete_event(session, acct, item.google_event_id)
                except GoogleError:
                    pass  # still remove it here; it can be deleted in Google Calendar by hand
        session.delete(item)
        session.commit()
        self.invalidate()

    async def sync_unsynced(self, session: Session) -> int:
        """After you grant calendar access, copy items that were saved only in Cardinal."""
        acct = self.google.account(session, "personal")
        if not self.google.can_write(acct):
            return 0
        n = 0
        for item in session.exec(select(CalendarItem).where(CalendarItem.google_event_id == None)).all():  # noqa: E711
            if await self._mirror(session, item) is None:
                n += 1
        self.invalidate()
        return n

    # ---------- Calendar links (iCloud and others) ----------

    def feeds(self, session: Session) -> list[dict]:
        return [{"id": f.id, "name": f.name, "color": f.color, "error": f.last_error}
                for f in session.exec(select(CalendarFeed)).all()]

    async def add_feed(self, session: Session, name: str, url: str) -> CalendarFeed:
        url = url.strip()
        if not url.startswith(("https://", "webcal://", "http://")):
            raise CalendarError("Paste the calendar's link. It starts with webcal:// or https://.")
        today = self.today()
        start, _ = day_bounds(today, self.tz)
        try:
            await Feed(url, self.feed_client).between(start, start + timedelta(days=30), self.tz)
        except FeedError as e:
            raise CalendarError(str(e)) from e
        used = {f.color for f in session.exec(select(CalendarFeed)).all()}
        color = next((c for c in FEED_COLORS if c not in used), FEED_COLORS[0])
        feed = CalendarFeed(name=(name or "Calendar").strip()[:40], url_enc=self.vault.encrypt(url), color=color)
        session.add(feed)
        session.commit()
        session.refresh(feed)
        self.invalidate()
        return feed

    def delete_feed(self, session: Session, feed_id: int) -> None:
        feed = session.get(CalendarFeed, feed_id)
        if feed:
            session.delete(feed)
            session.commit()
        self._feeds.pop(feed_id, None)
        self.invalidate()
