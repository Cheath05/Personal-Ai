"""Delta (check-ins and the weekly rollup) and Sigma (Core Memory).

Numbers are computed in code; the models only write words about them. Anything that changes your plan or
Core Memory goes through Actions, so it waits for your OK unless you made a trust rule for it.
"""

import json
import re
from datetime import date, datetime, timedelta
from statistics import mean, median
from zoneinfo import ZoneInfo

from sqlmodel import Session, col, select

from .actions import Actions
from .agents import Agent
from .db import CalendarItem, CheckIn, Experiment, Memory, Message, Rollup, Task, utcnow
from .router import BrainRouter, NoBrainAvailable

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MEMORY_LIMIT = 15


class ReviewError(Exception):
    pass


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _hm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def _after(now: datetime, hhmm: str) -> bool:
    h, m = _hm(hhmm)
    return (now.hour, now.minute) >= (h, m)


def _answers(c: CheckIn | None) -> dict:
    return json.loads(c.answers) if c else {}


# ---------- Reading ----------

def checkin(session: Session, day: str, kind: str) -> CheckIn | None:
    return session.exec(select(CheckIn).where(CheckIn.day == day, CheckIn.kind == kind)
                        .order_by(col(CheckIn.id).desc())).first()


def tasks_for(session: Session, day: str) -> list[Task]:
    return list(session.exec(select(Task).where(Task.day == day, Task.status != "dropped")
                             .order_by(col(Task.position), col(Task.id))).all())


def study_blocks(session: Session, d: date, tz: ZoneInfo) -> list[CalendarItem]:
    start = datetime.combine(d, datetime.min.time(), tz)
    return list(session.exec(select(CalendarItem).where(CalendarItem.kind == "reading", CalendarItem.start >= start,
                                                        CalendarItem.start < start + timedelta(days=1))
                             .order_by(col(CalendarItem.start))).all())


def memories(session: Session) -> list[Memory]:
    return list(session.exec(select(Memory).where(Memory.active == True)  # noqa: E712
                             .order_by(col(Memory.confidence).desc(), col(Memory.id))).all())


def memory_text(session: Session) -> str:
    """Core Memory for agents' prompts."""
    return "\n".join(f"- {m.text}" for m in memories(session)[:MEMORY_LIMIT])


def context_text(session: Session, now: datetime) -> str:
    """Today's priorities, energy and last night's review, for agents with "tasks" access."""
    day = now.date().isoformat()
    lines = []
    tasks = tasks_for(session, day)
    if tasks:
        lines.append("Today's priorities: " + "; ".join(f"{t.title} ({'done' if t.status == 'done' else 'open'})"
                                                         for t in tasks) + ".")
    m = checkin(session, day, "morning")
    if m and m.energy:
        lines.append(f"Energy this morning: {m.energy}/5.")
    y = checkin(session, (now.date() - timedelta(days=1)).isoformat(), "evening")
    if y:
        a = _answers(y)
        bits = [f"went well: {a['went_well']}" if a.get("went_well") else "",
                f"didn't: {a['didnt']}" if a.get("didnt") else "", f"why: {a['why']}" if a.get("why") else ""]
        if any(bits):
            lines.append("Last night's review: " + "; ".join(b for b in bits if b) + ".")
    exps = session.exec(select(Experiment).where(Experiment.week_start == monday(now.date()).isoformat())).all()
    if exps:
        lines.append("This week's experiments: " + "; ".join(e.text for e in exps) + ".")
    return "\n".join(lines)


def status(session: Session, now: datetime, settings) -> dict:
    tz = now.tzinfo
    day = now.date().isoformat()
    m, e = checkin(session, day, "morning"), checkin(session, day, "evening")
    blocks = study_blocks(session, now.date(), tz)
    week = monday(now.date()).isoformat()
    return {
        "day": day, "now": now.isoformat(),
        "morning": {"done": bool(m), "due": not m and _after(now, settings.morning_checkin),
                    "time": settings.morning_checkin, "checkin": checkin_json(m)},
        "evening": {"done": bool(e), "due": not e and _after(now, settings.evening_checkin),
                    "time": settings.evening_checkin, "checkin": checkin_json(e)},
        "tasks": [t.model_dump() for t in tasks_for(session, day)],
        "blocks": [{"id": b.id, "title": b.title, "start": b.start.astimezone(tz).isoformat(),
                    "end": b.end.astimezone(tz).isoformat()} for b in blocks],
        "experiments": [x.model_dump() for x in session.exec(select(Experiment).where(Experiment.week_start == week)).all()],
        "week": week_stats(session, monday(now.date()), now),
        "rollup": rollup_json(latest_rollup(session)),
        "rollup_time": f"Sun {settings.rollup_time}",
    }


def checkin_json(c: CheckIn | None) -> dict | None:
    return {**c.model_dump(exclude={"answers"}), "answers": _answers(c)} if c else None


# ---------- Morning and evening ----------

def _save_messages(session: Session, user_text: str, reply, device: str | None) -> None:
    session.add(Message(agent_id="delta", role="user", content=user_text, device=device))
    if reply is not None:
        session.add(Message(agent_id="delta", role="assistant", content=reply.text, provider=reply.provider,
                            model=reply.model, brain=reply.brain))
    session.commit()


async def _delta_reply(session: Session, router: BrainRouter, delta: Agent, context: str, memory: str,
                       prompt: str):
    try:
        result = await router.run(session, agent_id="delta", job="checkin",
                                  system=delta.system_prompt(context=context, memory=memory),
                                  messages=[{"role": "user", "content": prompt}])
        return result.reply
    except NoBrainAvailable:
        return None  # the check-in still counts; Delta just can't answer right now


async def morning(session: Session, router: BrainRouter, delta: Agent, *, now: datetime, top: list[str], energy: int,
                  note: str | None, context: str, device: str | None = None) -> CheckIn:
    top = [t.strip()[:200] for t in top if t and t.strip()][:3]
    if not top:
        raise ReviewError("Add at least one priority.")
    if not 1 <= energy <= 5:
        raise ReviewError("Energy is 1 to 5.")
    day = now.date().isoformat()
    existing = [t.title.lower() for t in tasks_for(session, day)]
    for i, title in enumerate(top):
        if title.lower() not in existing:
            session.add(Task(day=day, title=title, source="morning", position=i))
    session.commit()
    summary = (f"Morning check-in. Top {len(top)}: " + "; ".join(top) + f". Energy {energy}/5."
               + (f" Note: {note.strip()}" if note and note.strip() else ""))
    reply = await _delta_reply(session, router, delta, context, memory_text(session),
                               summary + "\n\nReply in 2-3 short sentences: acknowledge the plan, point out anything in "
                                         "today's calendar that clashes with it or leaves little time, and give one tip "
                                         "that fits my energy level.")
    c = CheckIn(day=day, kind="morning", energy=energy, answers=json.dumps({"top": top, "note": note or ""}),
                reply=reply.text if reply else None, brain=reply.brain if reply else None)
    session.add(c)
    session.commit()
    session.refresh(c)
    _save_messages(session, summary, reply, device)
    session.refresh(c)  # the commit above expired it
    return c


async def evening(session: Session, router: BrainRouter, delta: Agent, *, now: datetime, done_task_ids: list[int],
                  done_block_ids: list[int], went_well: str, didnt: str, why: str, first_task: str | None,
                  carry: bool, context: str, device: str | None = None) -> CheckIn:
    day = now.date().isoformat()
    tomorrow = (now.date() + timedelta(days=1)).isoformat()
    tasks = tasks_for(session, day)
    for t in tasks:
        if t.id in done_task_ids and t.status != "done":
            t.status, t.done_at = "done", utcnow()
        elif t.id not in done_task_ids and t.status == "done":
            t.status, t.done_at = "open", None
        session.add(t)
    unfinished = [t for t in tasks if t.status != "done"]
    pos = 0
    if first_task and first_task.strip():
        session.add(Task(day=tomorrow, title=first_task.strip()[:200], source="first_task", position=pos))
        pos += 1
    if carry:
        for t in unfinished:
            session.add(Task(day=tomorrow, title=t.title, source="carried", position=pos))
            pos += 1
    session.commit()
    blocks = study_blocks(session, now.date(), now.tzinfo)
    done_titles = [t.title for t in tasks if t.status == "done"]
    summary = (f"Evening review. Done: {'; '.join(done_titles) or 'none of the priorities'}. "
               f"Not done: {'; '.join(t.title for t in unfinished) or 'nothing'}. "
               f"Study blocks: {len([b for b in blocks if b.id in done_block_ids])} of {len(blocks)}. "
               f"Went well: {went_well or '-'}. Didn't: {didnt or '-'}. Why: {why or '-'}. "
               f"Tomorrow's first task: {first_task or '-'}.")
    reply = await _delta_reply(session, router, delta, context, memory_text(session),
                               summary + "\n\nReply in 2-3 short sentences: name one thing that went well, and suggest "
                                         "one small, specific change for tomorrow based on why things slipped.")
    answers = {"went_well": went_well, "didnt": didnt, "why": why, "first_task": first_task or "", "carry": carry,
               "blocks_done": [b.id for b in blocks if b.id in done_block_ids], "blocks_planned": [b.id for b in blocks],
               "tasks_done": len(done_titles), "tasks_planned": len(tasks)}
    c = CheckIn(day=day, kind="evening", answers=json.dumps(answers),
                reply=reply.text if reply else None, brain=reply.brain if reply else None)
    session.add(c)
    session.commit()
    session.refresh(c)
    _save_messages(session, summary, reply, device)
    session.refresh(c)  # the commit above expired it
    return c


# ---------- The week ----------

def week_stats(session: Session, start: date, now: datetime) -> dict:
    days = [start + timedelta(days=i) for i in range(7)]
    elapsed = [d for d in days if d <= now.date()]
    keys = [d.isoformat() for d in days]
    checks = session.exec(select(CheckIn).where(CheckIn.day.in_(keys))).all()
    mornings = {c.day: c for c in checks if c.kind == "morning"}
    evenings = {c.day: c for c in checks if c.kind == "evening"}
    tasks = session.exec(select(Task).where(Task.day.in_(keys), Task.status != "dropped")).all()
    blocks_planned = sum(len(_answers(e).get("blocks_planned", [])) for e in evenings.values())
    blocks_done = sum(len(_answers(e).get("blocks_done", [])) for e in evenings.values())
    energy = [mornings[k].energy if k in mornings else None for k in keys]
    known = [x for x in energy if x]
    prev_keys = [(start - timedelta(days=7 - i)).isoformat() for i in range(7)]
    prev = [c.energy for c in session.exec(select(CheckIn).where(CheckIn.day.in_(prev_keys), CheckIn.kind == "morning")).all()
            if c.energy]
    streak, d = 0, now.date()
    days_with = {c.day for c in session.exec(select(CheckIn).where(CheckIn.day >= (now.date() - timedelta(days=60)).isoformat())).all()}
    if d.isoformat() not in days_with:
        d -= timedelta(days=1)  # today isn't over yet
    while d.isoformat() in days_with:
        streak += 1
        d -= timedelta(days=1)
    done = sum(1 for t in tasks if t.status == "done")
    from .running import week_summary
    run = week_summary(session, start, now)
    return {
        "runs_done": run["runs_done"], "runs_planned": run["runs_planned"], "run_miles": run["miles"],
        "week_start": start.isoformat(), "days_elapsed": len(elapsed),
        "mornings": len(mornings), "evenings": len(evenings),
        "priorities_done": done, "priorities_planned": len(tasks),
        "completion": round(done / len(tasks), 2) if tasks else None,
        "blocks_done": blocks_done, "blocks_planned": blocks_planned,
        "energy": [{"day": DAYS[i], "date": keys[i], "value": energy[i]} for i in range(7)],
        "energy_avg": round(mean(known), 1) if known else None,
        "energy_prev_avg": round(mean(prev), 1) if prev else None,
        "streak": streak,
    }


def latest_rollup(session: Session) -> Rollup | None:
    return session.exec(select(Rollup).order_by(col(Rollup.id).desc())).first()


def rollup_json(r: Rollup | None) -> dict | None:
    if not r:
        return None
    return {**r.model_dump(exclude={"stats", "wins", "blockers", "experiments", "focus"}),
            "stats": json.loads(r.stats), "wins": json.loads(r.wins), "blockers": json.loads(r.blockers),
            "experiments": json.loads(r.experiments), "focus": json.loads(r.focus)}


ROLLUP_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "wins": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "items": {"type": "string"}},
        "experiments": {"type": "array", "items": {"type": "string"}},
        "focus": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "wins", "blockers", "experiments", "focus"],
}


def _stats_text(s: dict) -> str:
    energy = ", ".join(f"{e['day']} {e['value']}" for e in s["energy"] if e["value"])
    comp = f"{s['priorities_done']} of {s['priorities_planned']}" if s["priorities_planned"] else "no priorities set"
    trend = ""
    if s["energy_avg"] and s["energy_prev_avg"]:
        trend = f" (last week {s['energy_prev_avg']})"
    return (f"Morning check-ins: {s['mornings']} of {s['days_elapsed']} days. Evening reviews: {s['evenings']}. "
            f"Priorities done: {comp}. Study blocks done: {s['blocks_done']} of {s['blocks_planned']}. "
            f"Energy: {energy or 'not logged'}; average {s['energy_avg'] or '-'}{trend}. Check-in streak: {s['streak']} days. "
            f"Runs: {s.get('runs_done', 0)} of {s.get('runs_planned', 0)} planned, {s.get('run_miles', 0)} mi.")


def _parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        return json.loads(m.group(0)) if m else {}


def _rollup_check(reply) -> str | None:
    try:
        data = _parse_json(reply.text)
    except json.JSONDecodeError:
        return "answer wasn't valid JSON"
    return None if isinstance(data, dict) and data.get("summary") else "no summary"


async def write_rollup(session: Session, router: BrainRouter, delta: Agent, actions: Actions, *, now: datetime,
                       due_text: str = "", trigger: str = "scheduled") -> Rollup:
    start = monday(now.date())
    stats = week_stats(session, start, now)
    keys = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    notes = []
    for c in session.exec(select(CheckIn).where(CheckIn.day.in_(keys), CheckIn.kind == "evening")
                          .order_by(col(CheckIn.day))).all():
        a = _answers(c)
        d = date.fromisoformat(c.day)
        notes.append(f"{DAYS[d.weekday()]}: went well: {a.get('went_well') or '-'}; didn't: {a.get('didnt') or '-'}; "
                     f"why: {a.get('why') or '-'}")
    exps = session.exec(select(Experiment).where(Experiment.week_start == start.isoformat())).all()
    exp_text = "; ".join(f"{e.text} ({e.result or 'not marked'})" for e in exps) or "none this week"
    prompt = f"""Write my weekly rollup for the week of {start:%d %B}.

The numbers (already calculated, use them as given):
{_stats_text(stats)}

This week's experiments and how they went: {exp_text}

My evening notes:
{chr(10).join(notes) or "No evening reviews this week."}

Coming up next week: {due_text or "nothing known"}

Rules: write to me in the second person ("you"), as my assistant, never as me. Use the numbers exactly as given
and never contradict them (if it says 1 check-in, don't say none). If there's little data, say so briefly.

Answer as JSON:
- summary: 3-4 sentences, honest and warm, about how the week actually went.
- wins: up to 3 short wins, from the notes and numbers only.
- blockers: up to 3 short blockers, with the reason if the notes give one.
- experiments: exactly 3 small, specific experiments for next week that target the blockers. Keep what worked.
- focus: up to 3 short priorities for next week, based on what's coming up."""
    job = "weekly_rollup" if trigger == "scheduled" else "rollup_now"
    try:
        result = await router.run(session, agent_id="delta", job=job,
                                  system=delta.system_prompt(memory=memory_text(session)),
                                  messages=[{"role": "user", "content": prompt}], check=_rollup_check,
                                  json_schema=ROLLUP_SCHEMA)
    except NoBrainAvailable as e:
        raise ReviewError(str(e)) from e
    try:
        data = _parse_json(result.reply.text)
    except json.JSONDecodeError:
        data = {}
    if not str(data.get("summary", "")).strip():
        raise ReviewError("Delta's answer wasn't usable this time. Try again, or wait for the G14 to be online.")
    clean = lambda xs, n: [str(x).strip()[:200] for x in (xs or []) if str(x).strip()][:n]  # noqa: E731
    r = Rollup(week_start=start.isoformat(), stats=json.dumps(stats), summary=str(data.get("summary", "")).strip(),
               wins=json.dumps(clean(data.get("wins"), 3)), blockers=json.dumps(clean(data.get("blockers"), 3)),
               experiments=json.dumps(clean(data.get("experiments"), 3)), focus=json.dumps(clean(data.get("focus"), 3)),
               brain=result.reply.brain)
    session.add(r)
    session.commit()
    session.refresh(r)
    next_week = start + timedelta(days=7)
    if json.loads(r.experiments) or json.loads(r.focus):
        comp = f"{round(stats['completion'] * 100)}% of priorities done" if stats["completion"] is not None else "no priorities logged"
        a = await actions.propose(session, agent_id="delta", kind="plan.set_week",
                                  title=f"Plan for the week of {next_week:%a} {next_week.day} {next_week:%b}",
                                  reason=f"From this week's rollup: {comp}, {stats['mornings']} morning check-ins. "
                                         "Experiments target this week's blockers.",
                                  payload={"week_start": next_week.isoformat(), "focus": json.loads(r.focus),
                                           "experiments": json.loads(r.experiments)},
                                  dedupe_key=f"plan:{next_week.isoformat()}:{r.id}")
        r.plan_action_id = a.id
        session.add(r)
        session.commit()
    session.refresh(r)  # commits above expired it
    return r


def rollup_due(now: datetime, at: str, last_week: str | None) -> bool:
    return now.weekday() == 6 and _after(now, at) and last_week != monday(now.date()).isoformat()


# ---------- Sigma: nightly patterns for Core Memory ----------

PATTERN_SCHEMA = {
    "type": "object",
    "properties": {"patterns": {"type": "array", "items": {
        "type": "object", "properties": {"text": {"type": "string"}, "evidence": {"type": "string"}},
        "required": ["text", "evidence"]}}},
    "required": ["patterns"],
}


def _norm_key(text: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", text.lower()))[:80]


def stat_patterns(session: Session, now: datetime) -> list[dict]:
    """Patterns that are just arithmetic on your check-ins (no model needed)."""
    since = (now.date() - timedelta(days=28)).isoformat()
    checks = session.exec(select(CheckIn).where(CheckIn.day >= since)).all()
    out = []
    mornings = [c for c in checks if c.kind == "morning" and c.energy]
    if len(mornings) >= 5:
        overall = mean(c.energy for c in mornings)
        by_day: dict[int, list[int]] = {}
        for c in mornings:
            by_day.setdefault(date.fromisoformat(c.day).weekday(), []).append(c.energy)
        for wd, vals in by_day.items():
            if len(vals) >= 2 and abs(mean(vals) - overall) >= 1.0:
                low = mean(vals) < overall
                out.append({"key": f"energy:{DAYS[wd]}:{'low' if low else 'high'}",
                            "text": f"Energy tends to be {'lower' if low else 'higher'} on {DAYS[wd]}days.",
                            "evidence": f"Average {mean(vals):.1f}/5 on {DAYS[wd]}days vs {overall:.1f}/5 overall, "
                                        f"from {len(vals)} {DAYS[wd]} check-ins."})
        times = [c.ts.replace(tzinfo=c.ts.tzinfo or ZoneInfo("UTC")).astimezone(now.tzinfo) for c in mornings]
        mins = median(t.hour * 60 + t.minute for t in times)
        rounded = int(round(mins / 30) * 30)
        out.append({"key": f"checkin-time:{rounded}",
                    "text": f"Usually does the morning check-in around {rounded // 60:02d}:{rounded % 60:02d}.",
                    "evidence": f"Median of {len(mornings)} morning check-ins."})
    tasks = session.exec(select(Task).where(Task.day >= since, Task.day < now.date().isoformat(), Task.status != "dropped")).all()
    days = {t.day for t in tasks}
    if len(days) >= 6:
        per_day = mean(sum(1 for t in tasks if t.day == d and t.status == "done") for d in days)
        planned = mean(sum(1 for t in tasks if t.day == d) for d in days)
        out.append({"key": f"completion:{round(per_day)}of{round(planned)}",
                    "text": f"Usually finishes about {round(per_day)} of {round(planned)} daily priorities.",
                    "evidence": f"Average over {len(days)} days with priorities."})
    return out


async def sigma_nightly(session: Session, router: BrainRouter, sigma: Agent, actions: Actions, now: datetime) -> int:
    """Propose new Core Memory patterns. They wait for your OK (or a trust rule)."""
    known = " ".join(m.text.lower() for m in session.exec(select(Memory)).all())
    candidates = stat_patterns(session, now)
    since = (now.date() - timedelta(days=14)).isoformat()
    evenings = session.exec(select(CheckIn).where(CheckIn.day >= since, CheckIn.kind == "evening")
                            .order_by(col(CheckIn.day))).all()
    if len(evenings) >= 3:
        notes = "\n".join(f"{c.day}: went well: {_answers(c).get('went_well') or '-'}; didn't: "
                          f"{_answers(c).get('didnt') or '-'}; why: {_answers(c).get('why') or '-'}" for c in evenings)
        prompt = (f"These are my evening reviews from the last two weeks:\n{notes}\n\n"
                  "Find at most 2 patterns that repeat on at least 2 different days, such as what tends to derail me "
                  "or what helps. For each, give a short statement about me and the evidence (the dates). "
                  "If nothing clearly repeats, return an empty list. Don't guess.")
        try:
            result = await router.run(session, agent_id="sigma", job="sigma_patterns",
                                      system=sigma.system_prompt(memory=memory_text(session)),
                                      messages=[{"role": "user", "content": prompt}], json_schema=PATTERN_SCHEMA)
            for p in _parse_json(result.reply.text).get("patterns", [])[:2]:
                text, ev = str(p.get("text", "")).strip(), str(p.get("evidence", "")).strip()
                if text and len(re.findall(r"\d{4}-\d{2}-\d{2}", ev)) >= 2:  # evidence must cite real days
                    candidates.append({"key": f"llm:{_norm_key(text)}", "text": text[:200], "evidence": ev[:300]})
        except NoBrainAvailable:
            pass
    proposed = 0
    for c in candidates:
        key = f"memory:{c['key']}"
        if actions.seen(session, key) or c["text"].lower() in known:
            continue
        await actions.propose(session, agent_id="sigma", kind="memory.add", title=f"Remember: {c['text']}",
                              reason=c["evidence"], payload={"kind": "pattern", "text": c["text"],
                                                             "evidence": c["evidence"], "confidence": 0.6},
                              dedupe_key=key)
        proposed += 1
    return proposed
