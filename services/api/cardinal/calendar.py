"""The calendar view: one day at a time, merged from every source, plus items you add in Cardinal.

Sources for a day: both Google accounts, Blackboard due dates, calendar links (e.g. iCloud) and Cardinal's own
items. Your own items are mirrored to the "Cardinal" Google calendar when that's allowed, so they also show
in Apple Calendar on any device that has the Google account added.
"""

import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import Session, col, select

from .config import Settings
from .db import CalendarFeed, CalendarItem
from .sources.blackboard import Blackboard, BlackboardError
from .sources.google import SLOTS, Google, GoogleError
from .sources.ics import Feed, FeedError, day_bounds
from .vault import Vault, VaultError

KINDS = ("event", "class", "due", "exam", "quiz", "reading", "no_class")
KIND_COLORS = {"event": "#22e3c4", "class": "#3d8bff", "due": "#ffb13d", "exam": "#ff4d6d",
               "quiz": "#ff7a45", "reading": "#a47bff", "no_class": "#7d8ba3"}
FEED_COLORS = ["#a47bff", "#ff4fd8", "#9dff4a", "#22e3c4", "#e0e8ff"]
BLACKBOARD_COLOR = "#ffb13d"
BACK_DAYS, AHEAD_DAYS = 7, 120
CACHE_SECONDS = 180


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


class Calendar:
    def __init__(self, settings: Settings, google: Google, blackboard: Blackboard, vault: Vault, feed_client=None):
        self.settings = settings
        self.google = google
        self.blackboard = blackboard
        self.vault = vault
        self.feed_client = feed_client
        self._feeds: dict[int, Feed] = {}
        self._cache: dict[str, tuple[float, dict]] = {}
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
        events, errors = [], []
        for f in session.exec(select(CalendarFeed)).all():
            try:
                feed = self._feeds.get(f.id)
                if feed is None:
                    feed = self._feeds[f.id] = Feed(self.vault.decrypt(f.url_enc), self.feed_client)
                for e in await feed.between(start, end, self.tz):
                    events.append({**e, "id": f"feed:{f.id}:{e['uid']}:{e['start']}", "calendar": f.name,
                                   "source": "feed", "kind": "event", "color": f.color, "deletable": False})
            except (FeedError, VaultError) as e:
                errors.append({"source": f.name, "detail": str(e)})
        return events, errors

    async def day(self, session: Session, d: date, force: bool = False) -> dict:
        today = self.today()
        if not (today - timedelta(days=BACK_DAYS) <= d <= today + timedelta(days=AHEAD_DAYS)):
            raise CalendarError(f"Pick a day from {BACK_DAYS} days ago to {AHEAD_DAYS} days ahead.")
        key = d.isoformat()
        hit = self._cache.get(key)
        if hit and not force and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        tz = self.tz
        start, end = day_bounds(d, tz)
        events, due, errors = [], [], []

        for slot, label in SLOTS.items():
            acct = self.google.account(session, slot)
            if not acct:
                continue
            try:
                for e in await self.google.events(session, acct, start, end, tz):
                    events.append({**e, "source": "google", "kind": "event",
                                   "color": e.get("color") or "#3d8bff", "deletable": False})
            except GoogleError as e:
                errors.append({"source": label, "detail": str(e)})

        events += self.items_between(session, start, end)
        if self.proposals:
            events += self.proposals(session, start, end)
        feed_events, feed_errors = await self.feeds_between(session, start, end)
        events += feed_events
        errors += feed_errors

        try:
            for dd in await self.blackboard.between(start, end, tz):
                due.append({**dd, "color": BLACKBOARD_COLOR, "source": "blackboard"})
        except BlackboardError as e:
            errors.append({"source": "Blackboard", "detail": str(e)})

        # Keep only what overlaps this day (a guard against sources that return a wider window).
        events = [e for e in events if datetime.fromisoformat(e["start"]) < end
                  and max(datetime.fromisoformat(e["end"]), datetime.fromisoformat(e["start"])) >= start]
        out = {"date": key, "timezone": self.settings.timezone, "today": today.isoformat(),
               "min_date": (today - timedelta(days=BACK_DAYS)).isoformat(),
               "max_date": (today + timedelta(days=AHEAD_DAYS)).isoformat(),
               "events": sorted(events, key=lambda e: (not e["all_day"], e["start"])), "due": due, "errors": errors,
               "can_sync": self.google.can_write(self.google.account(session, "personal"))}
        self._cache[key] = (time.time(), out)
        return out

    # ---------- Your own items ----------

    async def _mirror(self, session: Session, item: CalendarItem) -> str | None:
        """Copy an item to the Cardinal Google calendar. Returns a warning if it couldn't."""
        acct = self.google.account(session, "personal")
        if not self.google.can_write(acct):
            return "Saved in Cardinal. Reconnect Personal Google on Today to also put it in Google and Apple Calendar."
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
