"""Ordinal's morning briefing, and the small scheduler that writes it at 6:00.

The briefing is written only from the day's snapshot. If nothing is connected, no briefing is written:
an empty one would just invite the model to make things up.
"""

import asyncio
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import Session, col, select

from .agents import Agent
from .db import Briefing, get_engine
from .router import BrainRouter, NoBrainAvailable
from .today import ACCESS, Today, connected, context_text, counts

log = logging.getLogger("cardinal.briefing")

PROMPT = """Write my briefing for today.
- 3 to 7 short sentences, spoken style, no lists or markdown.
- Start with the first fixed commitment today and how the day is shaped.
- Then Blackboard work due in the next 48 hours, soonest first.
- Then any email that looks like it needs me today. Skip newsletters.
- Use only the data you were given. If a source is not connected or empty, skip it."""

RETRY_AFTER = timedelta(minutes=15)


class BriefingError(Exception):
    pass


def local_day(now: datetime) -> str:
    return now.date().isoformat()


def latest(session: Session, day: str | None = None) -> Briefing | None:
    q = select(Briefing).order_by(col(Briefing.id).desc())
    if day:
        q = q.where(Briefing.day == day)
    return session.exec(q).first()


async def write_briefing(session: Session, router: BrainRouter, ordinal: Agent, today: Today,
                         trigger: str = "manual", now: datetime | None = None) -> Briefing:
    snap = await today.snapshot(session, force=True, now=now)
    if not connected(snap):
        raise BriefingError("Nothing is connected yet. Connect Google or Blackboard on the Today view first.")
    now = now or datetime.now(today.tz)
    system = ordinal.system_prompt(now=now, context=context_text(snap, set(ACCESS)))
    job = "briefing" if trigger == "scheduled" else "briefing_now"
    try:
        result = await router.run(session, agent_id=ordinal.id, job=job, system=system,
                                  messages=[{"role": "user", "content": PROMPT}])
    except NoBrainAvailable as e:
        raise BriefingError(str(e)) from e
    reply = result.reply
    b = Briefing(day=local_day(now), text=reply.text, brain=reply.brain, model=reply.model,
                 sources=counts(snap), trigger=trigger)
    session.add(b)
    session.commit()
    session.refresh(b)
    return b


def briefing_due(now: datetime, at: str, last_day: str | None, last_try: datetime | None) -> bool:
    hh, mm = (int(x) for x in at.split(":"))
    if now.time() < time(hh, mm) or last_day == local_day(now):
        return False
    return last_try is None or now - last_try >= RETRY_AFTER


async def run_scheduler(state, settings) -> None:
    """Checks every 30 s. Also catches up if the hub was off at 6:00."""
    tz = ZoneInfo(settings.timezone)
    last_try: datetime | None = None
    while True:
        try:
            now = datetime.now(tz)
            with Session(get_engine()) as session:
                last = latest(session)
                if briefing_due(now, settings.briefing_time, last.day if last else None, last_try):
                    last_try = now
                    snap = await state.today.snapshot(session, force=True)
                    if connected(snap):
                        b = await write_briefing(session, state.router, state.agents["ordinal"], state.today,
                                                 trigger="scheduled")
                        log.info("Briefing written for %s on %s", b.day, b.brain)
        except asyncio.CancelledError:
            raise
        except Exception:  # keep the scheduler alive; the next check retries after RETRY_AFTER
            log.exception("Scheduled briefing failed")
        await asyncio.sleep(30)
