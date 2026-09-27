"""Axiom's study planner: proposes study blocks before things are due.

Plain code, not the language model: finding free time is exact work a small model gets wrong. It looks at the
next 7 days of due dates (Blackboard, plus syllabus items you added), groups everything due the same day into
one block, finds free time around your events, and proposes blocks. Each proposal goes through Actions, so it
waits for your OK unless you made a trust rule for it.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlmodel import Session, select

from .actions import Actions, minutes
from .calendar import Calendar, CalendarError
from .db import CalendarItem
from .sources.blackboard import BlackboardError

LOOKAHEAD_DAYS = 7
PREFERRED = ("14:00", "21:00")  # try afternoons and evenings first
BUFFER_MIN = 10
DAY_MAX_BLOCKS = 2
DAY_MAX_MINUTES = 180
AGENT = "axiom"


@dataclass
class Due:
    title: str
    due: datetime
    kind: str  # work | quiz | exam
    course: str | None = None


@dataclass
class Plan:
    key: str
    items: list[Due]
    minutes: int
    days_before: list[int]  # candidate days, relative to the due date
    label: str = ""
    extra: dict = field(default_factory=dict)


def clean_title(title: str) -> str:
    """ "6. Submit: Biology Module Full Project" -> "Biology Module Full Project" """
    t = re.sub(r"^\s*\d+[.)]\s*", "", title)
    t = re.sub(r"^(submit|complete|turn in|due)\s*:\s*", "", t, flags=re.I)
    t = re.sub(r"\s*[\[(][A-Z]{2,5}\s?\d{3}[A-Z]?[\])]\s*", " ", t)  # "[CMSC 341]": the course is shown separately
    t = re.sub(r"\s+due\s*$", "", t.strip(), flags=re.I)
    return t.strip() or title.strip()


def classify(title: str, kind: str | None = None) -> str:
    if kind in ("exam", "quiz"):
        return kind
    low = title.lower()
    written = re.search(r"\b(paper|project|essay|report|draft|presentation|portfolio|submission|reflection|lab)\b", low)
    if re.search(r"\b(exam|midterm)\b", low) or (re.search(r"\bfinal\b", low) and not written):
        return "exam"  # "Final exam" or "Final" alone; a "Final Paper" is work to do, not a test
    if re.search(r"\bquiz", low):
        return "quiz"
    return "work"


def _norm(t: str) -> set[str]:
    # "2:2" stays one token so Homework 2:2 and 2:3 are different things
    words = re.findall(r"[a-z0-9]+(?::[0-9]+)?", t.lower())
    return {w for w in words if (len(w) > 2 or w[0].isdigit()) and w not in {"due", "the", "and", "for"}}


def _same(a: str, b: str) -> bool:
    x, y = _norm(a), _norm(b)
    return bool(x and y) and len(x & y) / min(len(x), len(y)) >= 0.8


def dedupe(items: list[Due]) -> list[Due]:
    """The same assignment can come from Blackboard and from a syllabus you imported."""
    out: list[Due] = []
    for it in items:
        dup = any(o.due.date() == it.due.date() and _same(o.title, it.title) for o in out)
        if not dup:
            out.append(it)
    return out


def make_plans(items: list[Due]) -> list[Plan]:
    plans = []
    by_day: dict[date, list[Due]] = {}
    for it in items:
        if it.kind == "work":
            by_day.setdefault(it.due.date(), []).append(it)
        elif it.kind == "exam":
            key = f"study:exam:{it.due.date()}:{'-'.join(sorted(_norm(it.title)))[:60]}"
            plans.append(Plan(f"{key}:1", [it], 90, [1, 2, 3], "Review"))
            plans.append(Plan(f"{key}:2", [it], 90, [2, 3, 1], "Review"))
        else:
            key = f"study:quiz:{it.due.date()}:{'-'.join(sorted(_norm(it.title)))[:60]}"
            plans.append(Plan(key, [it], 45, [1, 0, 2], "Prep"))
    for d, group in by_day.items():
        mins = min(150, 60 + 20 * (len(group) - 1))
        plans.append(Plan(f"study:work:{d}", group, mins, [1, 0, 2], "Work on"))
    return sorted(plans, key=lambda p: min(i.due for i in p.items))


def free_slot(busy: list[tuple[int, int]], length: int, lo: int, hi: int,
              preferred: tuple[int, int] | None = None) -> tuple[int, int] | None:
    """Earliest gap of `length` minutes in [lo, hi] (minutes since midnight), trying `preferred` first."""
    blocks = sorted((max(0, s - BUFFER_MIN), e + BUFFER_MIN) for s, e in busy)
    windows = [(max(lo, preferred[0]), min(hi, preferred[1])), (lo, hi)] if preferred else [(lo, hi)]
    for w_lo, w_hi in windows:
        t = -(-w_lo // 15) * 15  # on a quarter hour
        for s, e in blocks + [(10**6, 10**6)]:
            if s - t >= length and t + length <= w_hi:
                return t, t + length
            if e > t:
                t = -(-e // 15) * 15
            if t + length > w_hi:
                break
    return None


def _hm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


async def collect_due(session: Session, cal: Calendar, now: datetime) -> list[Due]:
    tz = cal.tz
    items: list[Due] = []
    try:
        for d in await cal.blackboard.due(now, tz, days=LOOKAHEAD_DAYS):
            items.append(Due(clean_title(d["title"]), datetime.fromisoformat(d["due"]), classify(d["title"]), d.get("course")))
    except BlackboardError:
        pass
    end = now + timedelta(days=LOOKAHEAD_DAYS)
    for it in session.exec(select(CalendarItem).where(CalendarItem.kind.in_(["due", "exam", "quiz"]),
                                                      CalendarItem.start >= now - timedelta(days=1),
                                                      CalendarItem.start <= end)).all():
        due = it.start.astimezone(tz)
        if it.all_day:
            due = due.replace(hour=23, minute=59)
        if due > now:
            items.append(Due(clean_title(it.title), due, classify(it.title, it.kind), it.course))
    return dedupe(sorted(items, key=lambda i: i.due))


async def plan_study(session: Session, cal: Calendar, actions: Actions, *, now: datetime | None = None,
                     window: tuple[str, str] = ("08:00", "22:00")) -> dict:
    tz = cal.tz
    now = now or datetime.now(tz)
    if not any(cal.google.account(session, s) for s in ("personal", "school")):
        return {"proposed": 0, "auto": 0, "note": "Connect Google first, so study blocks don't land on top of your classes."}
    due = await collect_due(session, cal, now)
    lo_day, hi_day = minutes(window[0]), minutes(window[1])
    placed: dict[date, list[tuple[int, int]]] = {}
    proposed = auto = 0
    skipped: list[str] = []

    for plan in make_plans(due):
        if actions.seen(session, plan.key):
            continue  # proposed before (approved, denied or undone): don't nag
        first = plan.items[0]
        slot_day = slot = None
        for back in plan.days_before:
            d = first.due.date() - timedelta(days=back)
            if d < now.date():
                continue
            try:
                view = await cal.day(session, d)
            except CalendarError:
                continue
            busy = []
            study_blocks, study_minutes = 0, 0
            for e in view["events"]:
                if e["all_day"]:
                    continue
                s, en = datetime.fromisoformat(e["start"]).astimezone(tz), datetime.fromisoformat(e["end"]).astimezone(tz)
                busy.append((s.hour * 60 + s.minute if s.date() == d else 0, en.hour * 60 + en.minute if en.date() == d else 24 * 60))
                if e["source"] == "proposed" or (e["source"] == "cardinal" and e.get("kind") == "reading"):
                    study_blocks += 1
                    study_minutes += busy[-1][1] - busy[-1][0]
            busy += placed.get(d, [])
            study_blocks += len(placed.get(d, []))
            study_minutes += sum(e - s for s, e in placed.get(d, []))
            if study_blocks >= DAY_MAX_BLOCKS or study_minutes + plan.minutes > DAY_MAX_MINUTES:
                continue
            lo = lo_day
            if d == now.date():
                lo = max(lo, now.hour * 60 + now.minute + 30)
            hi = hi_day
            if d == first.due.date():
                hi = min(hi, first.due.hour * 60 + first.due.minute - 60)
            found = free_slot(busy, plan.minutes, lo, hi, (minutes(PREFERRED[0]), minutes(PREFERRED[1])))
            if found:
                slot_day, slot = d, found
                break
        if not slot:
            skipped.append(f"{first.title}" + (" (a second review)" if plan.key.endswith(":2") else ""))
            continue
        placed.setdefault(slot_day, []).append(slot)

        names = [i.title for i in plan.items]
        what = names[0] if len(names) == 1 else f"{names[0]} + {len(names) - 1} more"
        title = f"{plan.label}: {what}"
        due_when = f"{first.due:%a} {first.due.day} {first.due:%b}, {first.due:%H:%M}"
        listed = "; ".join(names[:4]) + (f"; and {len(names) - 4} more" if len(names) > 4 else "")
        verb = ("is on" if first.kind == "exam" else "is due") if len(names) == 1 else "are due"
        reason = (f"{listed} {verb} {due_when}. "
                  f"You're free {slot_day:%a} {_hm(slot[0])}–{_hm(slot[1])}.")
        courses = sorted({i.course for i in plan.items if i.course})
        payload = {"date": slot_day.isoformat(), "start": _hm(slot[0]), "end": _hm(slot[1]), "title": title,
                   "course": courses[0] if len(courses) == 1 else None, "window": list(window),
                   "notes": f"For: {listed} (due {due_when}). Suggested by Axiom.", "for": names}
        a = await actions.propose(session, agent_id=AGENT, kind="calendar.add_block", title=title, reason=reason,
                                  payload=payload, dedupe_key=plan.key)
        if a.status == "executed":
            auto += 1
        else:
            proposed += 1

    note = (f"{proposed} to review" if proposed else "Nothing new to suggest") + (f", {auto} added by your rules" if auto else "")
    if skipped:
        note += f". Couldn't fit: {', '.join(skipped[:3])} (no free time before it; I'll try again tomorrow)"
    return {"proposed": proposed, "auto": auto, "skipped": skipped, "note": note + "."}
