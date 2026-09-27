"""What each agent can actually change when you ask it to in chat.

Every chat message to an agent that has tools goes through two steps:
1. Plan: the model sees its tools and the current state (items with ids) and answers with JSON tool calls.
   The output is constrained to a schema, which small local models follow reliably.
2. Run: the calls run here, in code. Calendar changes become Actions that wait for your OK (shown as cards in
   the chat) unless a trust rule covers them. Cardinal's own settings, tasks and memories change right away.
Then the model writes its reply, told exactly what happened, so it can't claim a change that didn't occur.
"""

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlmodel import Session, col, select

from . import prefs
from .actions import Actions, ActionError
from .calendar import KINDS as ITEM_KINDS
from .calendar import Calendar, CalendarError
from .db import Action, CalendarItem, Memory, Task
from .router import BrainRouter, NoBrainAvailable

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class ToolError(Exception):
    pass


@dataclass
class Ctx:
    session: Session
    now: datetime
    calendar: Calendar
    actions: Actions
    agent_id: str
    router: BrainRouter | None = None
    extras: dict = field(default_factory=dict)  # e.g. the Today service and agents, for the briefing tool


@dataclass
class Result:
    ok: bool
    text: str
    action: Action | None = None
    more: list[Action] = field(default_factory=list)  # other actions made along the way (e.g. runs moved)

    @property
    def all_actions(self) -> list[Action]:
        return ([self.action] if self.action else []) + self.more

    @property
    def status(self) -> str:
        if not self.ok:
            return "failed"
        return "proposed" if any(a.status == "pending" for a in self.all_actions) else "done"


@dataclass
class Tool:
    name: str
    agents: tuple[str, ...]
    description: str
    args: dict[str, str]
    run: Callable[[Ctx, dict], Awaitable[Result]]
    example: str = ""
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)  # arg -> other names small models use

    def normalize(self, args: dict) -> dict:
        # Small models leave stray quotes and commas at the ends of strings ("Study CMSC 341”,").
        out = {k: (v.strip().strip('"\'“”‘’,;').strip() if isinstance(v, str) else v) for k, v in args.items()}
        for name, others in self.aliases.items():
            if not out.get(name):
                for o in others:
                    if out.get(o):
                        out[name] = out[o]
                        break
        return out


# ---------- Helpers ----------

def resolve_date(value, now: datetime) -> date:
    v = str(value or "").strip().lower()
    today = now.date()
    if not v or v == "today":
        return today
    if v == "tomorrow":
        return today + timedelta(days=1)
    for i, name in enumerate(WEEKDAYS):
        if v.startswith(name):
            return today + timedelta(days=(i - today.weekday()) % 7)
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        pass
    from .syllabus import parse_date
    d = parse_date(v, today)
    if d is None:
        raise ToolError(f"I couldn't tell which day '{value}' is.")
    return d


def _time(value) -> str | None:
    if value in (None, "", "none", "all day"):
        return None
    try:
        return prefs.clean_time(value)
    except ValueError as e:
        raise ToolError(str(e)) from e


def _recent_ref(ctx: Ctx) -> str | None:
    """What "that" most likely means: the latest calendar change made or proposed in this conversation."""
    for aid in reversed(ctx.extras.get("recent_actions", [])):
        a = ctx.session.get(Action, aid)
        if not a:
            continue
        if a.status == "pending" and a.kind == "calendar.add_block":
            return f"proposal {a.id}"
        if a.status == "executed" and a.result and "item_id" in (res := json.loads(a.result)):
            if ctx.session.get(CalendarItem, res["item_id"]):
                return f"item {res['item_id']}"
    return None


def _find_by_title(ctx: Ctx, title: str | None) -> str | None:
    """An item or proposal whose title matches what the model called it (newest upcoming first)."""
    t = str(title or "").strip().lower()
    if len(t) < 3:
        return None
    for a in reversed(ctx.actions.pending(ctx.session)):
        if a.kind == "calendar.add_block" and t in json.loads(a.payload)["title"].lower():
            return f"proposal {a.id}"
    for it in ctx.session.exec(select(CalendarItem).where(CalendarItem.end >= ctx.now).order_by(col(CalendarItem.start))).all():
        if t in it.title.lower():
            return f"item {it.id}"
    return None


def _ref(value) -> tuple[str, int]:
    """ "item 12", "proposal 9", "12" -> (kind, id)."""
    m = re.search(r"(item|proposal|task|memory)?\s*#?\s*(\d+)", str(value).lower())
    if not m:
        raise ToolError(f"'{value}' isn't an id from the list.")
    return (m.group(1) or "item"), int(m.group(2))


def _fmt(d: date, start: str | None, end: str | None) -> str:
    return f"{d:%a} {d.day} {d:%b} " + (f"{start}–{end}" if start else "all day")


def _item_payload(ctx: Ctx, item: CalendarItem) -> dict:
    tz = ctx.calendar.tz
    return {"date": item.start.astimezone(tz).date().isoformat(),
            "start": None if item.all_day else f"{item.start.astimezone(tz):%H:%M}",
            "end": None if item.all_day else f"{item.end.astimezone(tz):%H:%M}"}


def _is_run(title: str) -> bool:
    return title.lower().startswith("run:")


def calendar_state(ctx: Ctx, runs: bool | None) -> str:
    """Upcoming Cardinal items and pending proposals, with ids. runs=True only runs, False no runs, None all."""
    tz = ctx.calendar.tz
    start = datetime.combine(ctx.now.date(), datetime.min.time(), tz)
    lines = []
    for it in ctx.session.exec(select(CalendarItem).where(CalendarItem.end >= start,
                                                          CalendarItem.start < start + timedelta(days=21))
                               .order_by(col(CalendarItem.start))).all():
        if runs is not None and _is_run(it.title) != runs:
            continue
        p = _item_payload(ctx, it)
        lines.append(f"[item {it.id}] {_fmt(date.fromisoformat(p['date']), p['start'], p['end'])} {it.title}")
    for a in ctx.actions.pending(ctx.session):
        if a.kind != "calendar.add_block":
            continue
        p = json.loads(a.payload)
        if runs is not None and _is_run(p["title"]) != runs:
            continue
        lines.append(f"[proposal {a.id}] {_fmt(date.fromisoformat(p['date']), p.get('start'), p.get('end'))} "
                     f"{p['title']} (proposed, waiting for the user's OK)")
    return "\n".join(lines) or "Nothing on the Cardinal calendar in the next 3 weeks."


# ---------- Calendar (Axiom, Vector, Cardinal) ----------

async def _move(ctx: Ctx, ref: str, day: date, start: str | None, end: str | None, noun: str) -> Result:
    kind, rid = _ref(ref)
    if kind == "proposal":
        a = ctx.session.get(Action, rid)
        if not a or a.status != "pending" or a.kind != "calendar.add_block":
            raise ToolError("That proposal isn't waiting any more.")
        p = json.loads(a.payload)
        length = _length(p)
        p["date"], p["start"] = day.isoformat(), start or p.get("start")
        p["end"] = end or (_add(p["start"], length) if p.get("start") else None)
        ctx.actions.update_pending(ctx.session, a, p)
        return Result(True, f"Changed the proposal to {_fmt(day, p['start'], p['end'])}: {p['title']}. "
                            "It's still waiting for the user's OK.", a)
    item = ctx.session.get(CalendarItem, rid)
    if not item:
        raise ToolError("That item isn't on the calendar any more.")
    before = _item_payload(ctx, item)
    length = int((item.end - item.start).total_seconds() // 60)
    start = start or before["start"]
    end = end or (_add(start, length) if start else None)
    payload = {"item_id": item.id, "title": item.title, "date": day.isoformat(), "start": start, "end": end,
               "from": before, "noun": noun, "window": list(_window_for(ctx, item.title))}
    a = await ctx.actions.propose(ctx.session, agent_id=ctx.agent_id, kind="calendar.move_item",
                                  title=f"Move: {item.title}", reason="You asked in chat.", payload=payload)
    status = "Done (a trust rule allowed it)" if a.status == "executed" else "Waiting for the user's OK (a card under your reply)"
    return Result(a.status != "failed", f"Move {item.title} from {_fmt(date.fromisoformat(before['date']), before['start'], before['end'])} "
                                        f"to {_fmt(day, start, end)}. {status}.", a)


def _length(p: dict) -> int:
    if p.get("start") and p.get("end"):
        return (int(p["end"][:2]) * 60 + int(p["end"][3:])) - (int(p["start"][:2]) * 60 + int(p["start"][3:]))
    return 60


def _add(hhmm: str, minutes: int) -> str:
    t = min(int(hhmm[:2]) * 60 + int(hhmm[3:]) + minutes, 23 * 60 + 59)
    return f"{t // 60:02d}:{t % 60:02d}"


def _window_for(ctx: Ctx, title: str) -> tuple[str, str]:
    if _is_run(title):
        return prefs.window(ctx.session, "run_window", ("06:00", "21:00"))
    return prefs.window(ctx.session, "study_window", ("08:00", "22:00"))


KIND_WORDS = {"study": "reading", "studying": "reading", "review": "reading", "homework": "due", "assignment": "due",
              "deadline": "due", "test": "exam", "lecture": "class", "meeting": "event"}
KIND_TITLES = {"reading": "Study", "due": "Due", "exam": "Exam", "quiz": "Quiz", "class": "Class", "no_class": "No class",
               "event": "Event"}


async def t_add(ctx: Ctx, a: dict) -> Result:
    raw_kind = str(a.get("kind") or "").lower().strip()
    kind = raw_kind if raw_kind in ITEM_KINDS else KIND_WORDS.get(raw_kind, "event")
    course = str(a.get("course") or "").strip() or None
    title = str(a.get("title") or "").strip()
    if not title and course:
        title = f"{KIND_TITLES[kind]} {course}"  # small models sometimes leave the title out
    if not title:
        raise ToolError("It needs a title.")
    day = resolve_date(a.get("date"), ctx.now)
    start, end = _time(a.get("start")), _time(a.get("end"))
    if start and not end:
        try:
            length = int(float(str(a.get("minutes") or 60).split()[0]))
        except ValueError:
            length = 60
        end = _add(start, max(15, min(length, 600)))
    payload = {"date": day.isoformat(), "start": start, "end": end, "title": title[:200], "item_kind": kind,
               "course": course, "notes": "Added from chat.", "noun": "items",
               "window": list(_window_for(ctx, title))}
    act = await ctx.actions.propose(ctx.session, agent_id=ctx.agent_id, kind="calendar.add_block", title=title[:200],
                                    reason="You asked in chat.", payload=payload)
    status = "Added (a trust rule allowed it)" if act.status == "executed" else "Waiting for the user's OK (a card under your reply)"
    return Result(act.status != "failed", f"Add {title} on {_fmt(day, start, end)}. {status}.", act)


async def t_move(ctx: Ctx, a: dict) -> Result:
    if not a.get("id") or str(a.get("id")).lower() in ("none", "that", "it"):
        a = {**a, "id": _find_by_title(ctx, a.get("title")) or _recent_ref(ctx) or a.get("id")}
    kind, rid = _ref(a.get("id"))
    cur_day = None
    if kind == "item" and (it := ctx.session.get(CalendarItem, rid)):
        cur_day = it.start.astimezone(ctx.calendar.tz).date()
    day = resolve_date(a.get("date"), ctx.now) if a.get("date") else (cur_day or ctx.now.date())
    return await _move(ctx, a.get("id"), day, _time(a.get("start")), _time(a.get("end")), "items")


async def t_remove(ctx: Ctx, a: dict) -> Result:
    if not a.get("id") or str(a.get("id")).lower() in ("none", "that", "it"):
        a = {**a, "id": _find_by_title(ctx, a.get("title")) or _recent_ref(ctx) or a.get("id")}
    kind, rid = _ref(a.get("id"))
    if kind == "proposal":
        act = ctx.session.get(Action, rid)
        if act and act.status == "pending":
            ctx.actions.deny(ctx.session, act.id)
            return Result(True, f"Withdrew the proposal: {act.title}.")
        raise ToolError("That proposal isn't waiting any more.")
    item = ctx.session.get(CalendarItem, rid)
    if not item:
        raise ToolError("That item isn't on the calendar.")
    p = {"item_id": item.id, "title": item.title, **_item_payload(ctx, item)}
    act = await ctx.actions.propose(ctx.session, agent_id=ctx.agent_id, kind="calendar.remove_item",
                                    title=f"Remove: {item.title}", reason="You asked in chat.", payload=p)
    return Result(True, f"Remove {item.title}. Removing always waits for the user's OK (a card under your reply).", act)


async def t_study_hours(ctx: Ctx, a: dict) -> Result:
    try:
        lo, hi = prefs.set_window(ctx.session, "study_window", a.get("earliest") or "08:00", a.get("latest") or "22:00")
    except ValueError as e:
        raise ToolError(str(e)) from e
    return Result(True, f"Study blocks will only be planned between {lo} and {hi} from now on.")


async def t_suggest_study(ctx: Ctx, a: dict) -> Result:
    from .planner import plan_study
    r = await plan_study(ctx.session, ctx.calendar, ctx.actions, now=ctx.now)
    return Result(True, f"Checked due dates: {r['note']}")


# ---------- Running (Vector) ----------

async def _replan_runs(ctx: Ctx) -> tuple[list[str], list[Action]]:
    """Fit upcoming runs (on the calendar or proposed) into the run window, on their planned days."""
    from .actions import minutes as to_min
    from .planner import free_slot
    from .running import propose_runs, run_windows

    notes, made = [], []
    tz = ctx.calendar.tz
    horizon = ctx.now.date() + timedelta(days=7)
    targets = []
    for it in ctx.session.exec(select(CalendarItem).where(CalendarItem.start >= ctx.now)).all():
        if _is_run(it.title) and it.start.astimezone(tz).date() <= horizon:
            targets.append(("item", it.id, it.title, _item_payload(ctx, it)))
    for act in ctx.actions.pending(ctx.session):
        p = json.loads(act.payload)
        if act.kind == "calendar.add_block" and _is_run(p["title"]) and date.fromisoformat(p["date"]) <= horizon:
            targets.append(("proposal", act.id, p["title"], p))
    for kind, rid, title, p in targets:
        d = date.fromisoformat(p["date"])
        preferred, allowed = run_windows(ctx.session, d)
        length = _length(p)
        if p.get("start") and to_min(p["start"]) >= to_min(allowed[0]) and to_min(p["end"]) <= to_min(allowed[1]):
            continue  # already fits
        view = await ctx.calendar.day(ctx.session, d, force=True)
        busy = []
        for e in view["events"]:
            if e["all_day"] or (kind == "item" and e.get("item_id") == rid) or (kind == "proposal" and e.get("action_id") == rid):
                continue  # the run being moved doesn't block itself
            st, en = datetime.fromisoformat(e["start"]).astimezone(tz), datetime.fromisoformat(e["end"]).astimezone(tz)
            busy.append((st.hour * 60 + st.minute if st.date() == d else 0, en.hour * 60 + en.minute if en.date() == d else 1440))
        slot = free_slot(busy, length, to_min(allowed[0]), to_min(allowed[1]), (to_min(preferred[0]), to_min(preferred[1])))
        if not slot:
            notes.append(f"{title} on {d:%a} needs about {length} min, but {allowed[0]}–{allowed[1]} doesn't have that much "
                         "free time that day. It stays where it is until the user picks another time or day.")
            continue
        hm = lambda m: f"{m // 60:02d}:{m % 60:02d}"  # noqa: E731
        r = await _move(ctx, f"{kind} {rid}", d, hm(slot[0]), hm(slot[1]), "runs")
        notes.append(r.text)
        made += r.all_actions
    before = {a.id for a in ctx.actions.pending(ctx.session)}
    new = await propose_runs(ctx.session, ctx.calendar, ctx.actions, ctx.now)
    if new["proposed"] or new["auto"]:
        notes.append(f"Proposed {new['proposed'] + new['auto']} more upcoming run(s) in the new hours.")
        made += [a for a in ctx.actions.pending(ctx.session) if a.id not in before]
    return notes, made


DAY_GROUPS = {"weekdays": ["mon", "tue", "wed", "thu", "fri"], "school days": ["mon", "tue", "wed", "thu", "fri"],
              "weekends": ["sat", "sun"], "weekend": ["sat", "sun"]}


def _days(value) -> list[str]:
    v = str(value or "all").strip().lower()
    if v in ("", "all", "every day", "any", "everyday"):
        return []
    if v in DAY_GROUPS:
        return DAY_GROUPS[v]
    found = [d for d in WEEKDAYS if re.search(rf"\b{d}", v)]
    if not found:
        raise ToolError(f"Which days is '{value}'?")
    return found


async def t_run_hours(ctx: Ctx, a: dict) -> Result:
    days = _days(a.get("days"))
    earliest, latest = a.get("earliest") or "06:00", a.get("latest") or "21:00"  # "not before 8:30" gives only one end
    try:
        if days:
            for d in days:
                lo, hi = prefs.set_window(ctx.session, f"run_window_{d}", earliest, latest)
        else:
            lo, hi = prefs.set_window(ctx.session, "run_window", earliest, latest)
    except ValueError as e:
        raise ToolError(str(e)) from e
    which = f"on {', '.join(d.title() for d in days)}" if days else "every day"
    notes, made = await _replan_runs(ctx)
    return Result(True, f"From now on runs {which} are planned between {lo} and {hi}. " + " ".join(notes), more=made)


async def t_move_run(ctx: Ctx, a: dict) -> Result:
    day = resolve_date(a.get("date"), ctx.now)
    to_day = resolve_date(a.get("to_date"), ctx.now) if a.get("to_date") else day
    start = _time(a.get("start"))
    tz = ctx.calendar.tz
    for it in ctx.session.exec(select(CalendarItem).where(CalendarItem.start >= ctx.now - timedelta(hours=12))).all():
        if _is_run(it.title) and it.start.astimezone(tz).date() == day:
            return await _move(ctx, f"item {it.id}", to_day, start, None, "runs")
    for act in ctx.actions.pending(ctx.session):
        p = json.loads(act.payload)
        if act.kind == "calendar.add_block" and _is_run(p["title"]) and p["date"] == day.isoformat():
            return await _move(ctx, f"proposal {act.id}", to_day, start, None, "runs")
    raise ToolError(f"There's no run on the calendar for {day:%a %d %b}.")


async def t_replan_runs(ctx: Ctx, a: dict) -> Result:
    notes, made = await _replan_runs(ctx)
    return Result(True, " ".join(notes) or "Every upcoming run already fits the run hours.", more=made)


async def t_log_run(ctx: Ctx, a: dict) -> Result:
    from .running import MILE, add_run
    day = resolve_date(a.get("date"), ctx.now)
    try:
        miles = float(a.get("miles"))
        parts = [int(x) for x in str(a.get("time")).split(":")]
        h, m, s = ([0] + parts)[-3:] if len(parts) in (2, 3) else (0, 0, 0)
    except (TypeError, ValueError) as e:
        raise ToolError("I need the distance in miles and the time like 31:45.") from e
    seconds = h * 3600 + m * 60 + s
    start = datetime.combine(day, datetime.min.time(), ctx.calendar.tz).replace(hour=17, minute=30)
    r = add_run(ctx.session, start=start, duration_s=seconds, distance_m=miles * MILE,
                avg_hr=float(a["avg_hr"]) if a.get("avg_hr") else None,
                time_trial=str(a.get("time_trial", "")).strip().lower() in ("true", "yes", "1"),
                notes="Logged from chat.")
    if not r:
        raise ToolError("That run looks like it's already logged.")
    return Result(True, f"Logged {miles:g} mi in {a.get('time')} on {day:%a %d %b}.")


# ---------- Tasks and check-ins (Delta, Cardinal) ----------

def _task(ctx: Ctx, ref) -> Task:
    _, tid = _ref(ref)
    t = ctx.session.get(Task, tid)
    if not t:
        raise ToolError("That task isn't in the list.")
    return t


async def t_add_task(ctx: Ctx, a: dict) -> Result:
    title = str(a.get("title") or "").strip()[:200]
    if not title:
        raise ToolError("The task needs a title.")
    day = resolve_date(a.get("date"), ctx.now)
    n = len(ctx.session.exec(select(Task).where(Task.day == day.isoformat())).all())
    ctx.session.add(Task(day=day.isoformat(), title=title, source="chat", position=n))
    ctx.session.commit()
    return Result(True, f"Added task '{title}' for {day:%a %d %b}.")


async def t_set_task(ctx: Ctx, a: dict, status: str) -> Result:
    t = _task(ctx, a.get("id"))
    t.status = status
    ctx.session.add(t)
    ctx.session.commit()
    return Result(True, f"Marked '{t.title}' as {status}.")


async def t_move_task(ctx: Ctx, a: dict) -> Result:
    t = _task(ctx, a.get("id"))
    day = resolve_date(a.get("date"), ctx.now)
    t.day = day.isoformat()
    ctx.session.add(t)
    ctx.session.commit()
    return Result(True, f"Moved '{t.title}' to {day:%a %d %b}.")


async def t_checkin_times(ctx: Ctx, a: dict) -> Result:
    done = []
    for key, label in (("morning", "morning_checkin"), ("evening", "evening_checkin")):
        if a.get(key):
            prefs.put(ctx.session, label, _time(a[key]))
            done.append(f"{key} check-in at {prefs.get(ctx.session, label)}")
    if not done:
        raise ToolError("Which time: morning or evening?")
    return Result(True, "Set the " + " and ".join(done) + ".")


def tasks_state(ctx: Ctx) -> str:
    days = [(ctx.now.date() + timedelta(days=i)).isoformat() for i in range(3)]
    rows = ctx.session.exec(select(Task).where(Task.day.in_(days), Task.status != "dropped")
                            .order_by(col(Task.day), col(Task.position))).all()
    return "\n".join(f"[task {t.id}] {t.day} {t.title} ({t.status})" for t in rows) or "No tasks for the next 3 days."


# ---------- Briefing (Ordinal) ----------

async def t_briefing_time(ctx: Ctx, a: dict) -> Result:
    t = _time(a.get("time"))
    if not t:
        raise ToolError("What time?")
    prefs.put(ctx.session, "briefing_time", t)
    return Result(True, f"The morning briefing will be written at {t} from now on.")


async def t_write_briefing(ctx: Ctx, a: dict) -> Result:
    from .briefing import BriefingError, write_briefing
    try:
        b = await write_briefing(ctx.session, ctx.router, ctx.extras["agents"]["ordinal"], ctx.extras["today"], trigger="manual")
    except (BriefingError, KeyError) as e:
        raise ToolError(str(e)) from e
    return Result(True, f"Wrote a fresh briefing (on Today): {b.text[:200]}")


# ---------- Core Memory (Sigma, Cardinal) ----------

async def t_remember(ctx: Ctx, a: dict) -> Result:
    text = str(a.get("text") or "").strip()[:300]
    if not text:
        raise ToolError("What should I remember?")
    kind = a.get("kind") if a.get("kind") in ("fact", "preference", "pattern") else "fact"
    ctx.session.add(Memory(text=text, kind=kind, source="you"))
    ctx.session.commit()
    return Result(True, f"Saved to Core Memory: {text}")


async def t_forget(ctx: Ctx, a: dict) -> Result:
    _, mid = _ref(a.get("id"))
    m = ctx.session.get(Memory, mid)
    if not m:
        raise ToolError("That memory isn't there.")
    text = m.text
    ctx.session.delete(m)
    ctx.session.commit()
    return Result(True, f"Forgot: {text}")


def memory_state(ctx: Ctx) -> str:
    rows = ctx.session.exec(select(Memory).where(Memory.active == True)).all()  # noqa: E712
    return "\n".join(f"[memory {m.id}] {m.text}" for m in rows) or "Core Memory is empty."


# ---------- The registry ----------

CAL = ("axiom", "cardinal")
TITLE = ("name", "event", "task", "what", "text")
TEXT = ("fact", "memory", "note", "content", "value", "what")
TOOLS = [
    Tool("add_to_calendar", CAL, "Add something to the user's Cardinal calendar (waits for their OK).",
         {"title": "what it is", "date": "YYYY-MM-DD from the day list", "start": "HH:MM or empty for all day",
          "end": "HH:MM (optional)", "minutes": "length in minutes if no end is given", "kind": "event|class|due|exam|quiz|reading|no_class",
          "course": "optional"}, t_add,
         '"add a study session for CMSC 341 Thursday 3-4:30pm" -> add_to_calendar(title "Study CMSC 341", date <Thursday\'s date>, start 15:00, end 16:30, kind reading, course CMSC 341)',
         {"title": TITLE, "date": ("day",), "start": ("time", "start_time"), "end": ("end_time",), "minutes": ("duration", "length")}),
    Tool("move_calendar_item", CAL, "Move an item or proposal from the state list to another day and/or time.",
         {"id": "e.g. 'item 12' or 'proposal 9'", "date": "new day (optional)", "start": "new start HH:MM (optional)",
          "end": "new end HH:MM (optional)", "title": "its title, if you don't know the id"}, t_move, '"move my Tuesday study block to 7pm" -> move_calendar_item(id <its id>, start 19:00)',
         {"id": ("item", "item_id"), "start": ("time",)}),
    Tool("remove_calendar_item", CAL, "Remove an item (waits for OK) or withdraw a proposal.",
         {"id": "e.g. 'item 12' or 'proposal 9'", "title": "its title, if you don't know the id"}, t_remove, "", {"id": ("item", "item_id")}),
    Tool("set_study_hours", ("axiom",), "Set the hours study blocks may be planned in.",
         {"earliest": "HH:MM", "latest": "HH:MM"}, t_study_hours, '"don\'t plan studying after 9pm" -> set_study_hours(latest 21:00)'),
    Tool("suggest_study_time", ("axiom",), "Check due dates now and propose study blocks.", {}, t_suggest_study,
         '"find me time to study" -> suggest_study_time()'),
    Tool("set_run_hours", ("vector",), "Set the hours the user can run in (e.g. they can't run before 8:30). "
         "Also moves upcoming runs to fit.",
         {"earliest": "HH:MM", "latest": "HH:MM (default 21:00)", "days": "all (default), weekdays, weekends, or names like 'tue, wed'"},
         t_run_hours, '"I can\'t run before 8:30" -> set_run_hours(earliest 08:30, latest 21:00); '
                      '"I can only run 7 to 8 on weekdays" -> set_run_hours(earliest 07:00, latest 08:00, days weekdays)',
         {"earliest": ("start", "from"), "latest": ("end", "until", "to")}),
    Tool("move_run", ("vector",), "Move the run planned on a day to another time and/or day.",
         {"date": "the run's current day", "start": "new start HH:MM (optional)", "to_date": "new day (optional)"}, t_move_run,
         '"move Wednesday\'s run to 6pm" -> move_run(date <Wednesday\'s date>, start 18:00)', {"start": ("time",), "to_date": ("new_date",)}),
    Tool("replan_runs", ("vector",), "Re-fit all upcoming runs into the run hours.", {}, t_replan_runs),
    Tool("log_run", ("vector",), "Record a run the user did.",
         {"date": "day", "miles": "number", "time": "mm:ss or h:mm:ss", "avg_hr": "optional", "time_trial": "true if an all-out mile or 5K"},
         t_log_run, '"I ran 3.1 miles in 31:40 today" -> log_run(date today, miles 3.1, time 31:40)',
         {"miles": ("distance",), "time": ("duration",)}),
    Tool("add_task", ("delta", "cardinal"), "Add a task to a day's priorities (a to-do item; it doesn't send anything).",
         {"title": "task", "date": "day (default today)"}, t_add_task,
         '"add email Professor Lee to my tasks" -> add_task(title "Email Professor Lee")', {"title": TITLE, "date": ("day",)}),
    Tool("complete_task", ("delta", "cardinal"), "Mark a task done.", {"id": "e.g. 'task 4'"},
         lambda c, a: t_set_task(c, a, "done"), '"I finished the quiz prep" -> complete_task(id <its id>)', {"id": ("task", "task_id")}),
    Tool("drop_task", ("delta", "cardinal"), "Drop a task.", {"id": "e.g. 'task 4'"}, lambda c, a: t_set_task(c, a, "dropped"),
         "", {"id": ("task", "task_id")}),
    Tool("move_task", ("delta", "cardinal"), "Move a task to another day.", {"id": "e.g. 'task 4'", "date": "day"}, t_move_task,
         "", {"id": ("task", "task_id"), "date": ("day",)}),
    Tool("set_checkin_times", ("delta",), "Change when the morning check-in or evening review opens.",
         {"morning": "HH:MM (optional)", "evening": "HH:MM (optional)"}, t_checkin_times,
         '"do my evening review at 10pm" -> set_checkin_times(evening 22:00)'),
    Tool("set_briefing_time", ("ordinal",), "Change when the morning briefing is written.", {"time": "HH:MM"}, t_briefing_time,
         '"write my briefing at 6:30" -> set_briefing_time(time 06:30)'),
    Tool("write_briefing", ("ordinal",), "Write a fresh briefing now.", {}, t_write_briefing, '"redo my briefing" -> write_briefing()'),
    Tool("remember", ("sigma", "cardinal"), "Save something about the user to Core Memory.",
         {"text": "the fact, in third person", "kind": "fact|preference|pattern"}, t_remember,
         '"remember I study best in the library" -> remember(text "Studies best in the library", kind preference)', {"text": TEXT}),
    Tool("forget", ("sigma", "cardinal"), "Delete a Core Memory entry.", {"id": "e.g. 'memory 3'"}, t_forget,
         "", {"id": ("memory", "memory_id")}),
]


def tools_for(agent_id: str) -> list[Tool]:
    return [t for t in TOOLS if agent_id in t.agents]


def state_for(ctx: Ctx) -> str:
    parts = []
    recent = _recent_ref(ctx)
    if recent:
        parts.append(f"Most recent thing from this conversation (\"that\", \"it\"): {recent}.")
    names = {t.name for t in tools_for(ctx.agent_id)}
    if names & {"move_calendar_item", "move_run", "set_run_hours"}:
        runs = True if ctx.agent_id == "vector" else (False if ctx.agent_id == "axiom" else None)
        parts.append("Calendar (ids to use):\n" + calendar_state(ctx, runs))
    if "set_run_hours" in names:
        hours = [f"{d.title()} {prefs.get(ctx.session, f'run_window_{d}')}" for d in WEEKDAYS
                 if prefs.get(ctx.session, f"run_window_{d}")]
        general = prefs.get(ctx.session, "run_window") or "not set (Cardinal prefers 16:30-20:30)"
        parts.append(f"Run hours: other days {general}" + (f"; {'; '.join(hours)}" if hours else "") + ".")
    if "set_study_hours" in names:
        parts.append(f"Study hours: {'-'.join(prefs.window(ctx.session, 'study_window', ('08:00', '22:00')))}.")
    if "add_task" in names:
        parts.append("Tasks:\n" + tasks_state(ctx))
    if "remember" in names:
        parts.append("Core Memory:\n" + memory_state(ctx))
    return "\n\n".join(parts)


def plan_schema(tools: list[Tool]) -> dict:
    """Constrain the plan: tool names from this agent's list only, and only known argument names (all strings).
    Loose schemas let small models write junk inside a string until they run out of tokens."""
    arg_names = sorted({k for t in tools for k in t.args})
    return {
        "type": "object",
        "properties": {"calls": {"type": "array", "maxItems": 3, "items": {
            "type": "object",
            "properties": {
                "tool": {"type": "string", "enum": [t.name for t in tools]},
                "args": {"type": "object", "properties": {k: {"type": "string"} for k in arg_names},
                         "additionalProperties": False},
            },
            "required": ["tool", "args"], "additionalProperties": False}}},
        "required": ["calls"], "additionalProperties": False,
    }


PLAN_SCHEMA = {"type": "object", "properties": {"calls": {"type": "array"}}, "required": ["calls"]}  # tests/back-compat


def plan_prompt(agent_name: str, tools: list[Tool], state: str, today: datetime) -> str:
    listing = "\n".join(f"- {t.name}({', '.join(f'{k}: {v}' for k, v in t.args.items())}): {t.description}" for t in tools)
    examples = "\n".join(f"  {t.example}" for t in tools if t.example)
    days = ", ".join(f"{(today + timedelta(days=i)):%A} {(today + timedelta(days=i)):%Y-%m-%d}" for i in range(8))
    return f"""You decide which tools {agent_name} should use for the user's latest message.
Days: today is {days.split(', ')[0]}; then {', '.join(days.split(', ')[1:])}. Use these exact dates.

Tools:
{listing}

Examples:
{examples}

Current state:
{state}

Rules:
- Call tools only when the latest message asks for a change or states a constraint that changes the plan.
  Examples: "I can't run before 8:30" -> set_run_hours(earliest 08:30, latest 21:00).
  "I can only run 7 to 8 on weekdays" -> set_run_hours(earliest 07:00, latest 08:00, days weekdays).
- Use ids exactly as shown in the state. Times as HH:MM, 24-hour. Dates as YYYY-MM-DD or a weekday name.
- To change a time or day, use a move tool. Never remove something and add it again.
- Only remove when the user clearly asks to delete or cancel.
- Questions, thanks and small talk need no tools: return {{"calls": []}}.
Answer only with JSON like {{"calls": [{{"tool": "name", "args": {{...}}}}]}}."""


def _parse(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        try:
            data = json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            data = {}
    calls = data.get("calls", []) if isinstance(data, dict) else []
    return [c for c in calls if isinstance(c, dict) and isinstance(c.get("tool"), str)][:4]


async def plan_and_run(ctx: Ctx, agent_name: str, turns: list[dict]) -> list[Result]:
    """Step 1 and 2 for one chat message. Returns what happened (empty if no change was asked for)."""
    tools = {t.name: t for t in tools_for(ctx.agent_id)}
    if not tools or ctx.router is None:
        return []
    system = plan_prompt(agent_name, list(tools.values()), state_for(ctx), ctx.now)
    try:
        r = await ctx.router.run(ctx.session, agent_id=ctx.agent_id, job="tool_plan", system=system,
                                 messages=turns[-6:], json_schema=plan_schema(list(tools.values())),
                                 check=lambda reply: None if _parse(reply.text) is not None else "bad JSON")
    except NoBrainAvailable:
        return []
    results = []
    for call in _parse(r.reply.text):
        tool = tools.get(call["tool"])
        if not tool:
            continue  # not this agent's tool: ignore rather than overreach
        args = tool.normalize(call.get("args") if isinstance(call.get("args"), dict) else {})
        try:
            results.append(await tool.run(ctx, args))
        except (ToolError, CalendarError, ActionError, ValueError) as e:
            results.append(Result(False, f"Couldn't {tool.name.replace('_', ' ')}: {e}"))
    return results


LABELS = {"done": "Done", "proposed": "Proposed, NOT done yet: it happens when the user taps Authorize on the card right "
                                    "below your reply (say it's ready for them to approve, not that it's moved or added)",
          "failed": "Didn't happen"}


CLAIM = re.compile(r"\b(i['’]?ve|i have|i|it['’]?s|it is|that['’]?s|now)\s+(been\s+)?(updated|moved|added|changed|scheduled|"
                   r"rescheduled|saved|set|removed|deleted|created|booked|adjusted|logged|marked)\b", re.I)


def claim_check(results: list[Result]):
    """A reply check: no "I've moved it" unless something was actually done or proposed."""
    acted = any(r.status in ("done", "proposed") for r in results)

    def check(reply):
        from .router import basic_check
        base = basic_check(reply)
        if base:
            return base
        if not acted and CLAIM.search(reply.text):
            return "claimed a change that didn't happen"
        return None
    return check


def results_block(results: list[Result], has_tools: bool = True) -> str:
    if not results:
        if not has_tools:
            return ""
        return ("\n\nNothing was changed by this message: no tool was used. Don't say you changed, moved, added, "
                "saved, set or updated anything. If the user asked for a change, say you couldn't make it and ask "
                "them to say exactly what to change.")
    lines = "\n".join(f"- {LABELS[r.status]}: {r.text}" for r in results)
    return ("\n\nWhat actually happened just now because of the user's message. These are facts. Start your reply by "
            "telling the user this in your own plain words (don't copy the labels), and never claim anything that isn't "
            "listed here as done or waiting:\n" + lines)
