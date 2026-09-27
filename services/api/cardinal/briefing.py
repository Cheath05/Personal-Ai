"""Ordinal's morning briefing, and the small scheduler that writes it at 6:00.

The briefing is written only from the day's snapshot. If nothing is connected, no briefing is written:
an empty one would just invite the model to make things up.
"""

import asyncio
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import Session, col, select

from . import prefs
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
    from . import review
    extra = review.context_text(session, now)
    system = ordinal.system_prompt(now=now, context=context_text(snap, set(ACCESS)) + (f"\n{extra}" if extra else ""),
                                   memory=review.memory_text(session))
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
    """Checks every 30 s. Also catches up if the hub was off at 6:00.

    Each morning: Ordinal's briefing, then Axiom's study-block suggestions (which wait for your OK)."""
    from . import review
    from .planner import plan_study

    tz = ZoneInfo(settings.timezone)
    last_try: datetime | None = None
    last_plan_day: str | None = None
    last_sigma_day: str | None = None
    last_rollup_try: datetime | None = None
    last_inbox: datetime | None = None
    while True:
        try:  # Relay sorts new mail every hour (only if an account is connected)
            now = datetime.now(tz)
            if last_inbox is None or now - last_inbox >= timedelta(hours=1):
                last_inbox = now
                from . import relay
                with Session(get_engine()) as session:
                    if any(state.today.google.account(session, slot) for slot in ("personal", "school")):
                        r = await relay.sort_inbox(session, state.router, state.today.google,
                                                   user_name=settings.user_name)
                        prefs.put(session, "inbox_sorted_at", datetime.now(tz).isoformat())
                        log.info("Relay sorted %s new emails", r["sorted"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Relay's inbox sort failed")
        try:  # Delta's weekly rollup, Sundays at rollup_time (retries every 15 min if no brain answered)
            now = datetime.now(tz)
            with Session(get_engine()) as session:
                last = review.latest_rollup(session)
                if (review.rollup_due(now, settings.rollup_time, last.week_start if last else None)
                        and (last_rollup_try is None or now - last_rollup_try >= RETRY_AFTER)):
                    last_rollup_try = now
                    snap = await state.today.snapshot(session)
                    due = "; ".join(f"{d['title']} ({d['due'][:10]})" for d in snap["due"][:8])
                    r = await review.write_rollup(session, state.router, state.agents["delta"], state.actions,
                                                  now=now, due_text=due)
                    log.info("Weekly rollup written for %s on %s", r.week_start, r.brain)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Weekly rollup failed")
        try:  # Sigma's nightly Core Memory pass
            now = datetime.now(tz)
            hh, mm = (int(x) for x in settings.sigma_time.split(":"))
            if now.time() >= time(hh, mm) and last_sigma_day != local_day(now) and getattr(state, "actions", None):
                last_sigma_day = local_day(now)
                with Session(get_engine()) as session:
                    n = await review.sigma_nightly(session, state.router, state.agents["sigma"], state.actions, now)
                    log.info("Sigma proposed %s Core Memory patterns", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Sigma's nightly pass failed")
        try:
            now = datetime.now(tz)
            with Session(get_engine()) as s0:
                at = prefs.get(s0, "briefing_time", settings.briefing_time)
            hh, mm = (int(x) for x in at.split(":"))
            if now.time() >= time(hh, mm) and last_plan_day != local_day(now) and getattr(state, "actions", None):
                last_plan_day = local_day(now)
                with Session(get_engine()) as session:
                    result = await plan_study(session, state.today.calendar, state.actions)
                    log.info("Study planner: %s", result["note"])
                    from .running import propose_runs
                    runs = await propose_runs(session, state.today.calendar, state.actions, now)
                    log.info("Run planner: %s proposed, %s added by rules", runs["proposed"], runs["auto"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Study planner failed")
        try:
            now = datetime.now(tz)
            with Session(get_engine()) as session:
                last = latest(session)
                if briefing_due(now, prefs.get(session, "briefing_time", settings.briefing_time),
                                last.day if last else None, last_try):
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
