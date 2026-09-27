"""Cardinal API: agents, chat through the brain router, usage, today's data, and the web app."""

import asyncio
import contextlib
import html
import json
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from . import briefing, planner, review, running, syllabus, usage
from .actions import ActionError, Actions
from .agents import load_agents
from .brains import ClaudeBrain, OllamaBrain
from .config import ROOT, WEB_DIR, get_settings, load_brains_config, load_routing_config
from .calendar import Calendar, CalendarError
from .db import CreditTopUp, Experiment, Memory, Message, SyllabusImport, Task, get_engine, get_session, utcnow
from .router import BrainRouter, NoBrainAvailable
from .sources.blackboard import Blackboard
from .sources.google import SLOTS, Google, GoogleError
from .today import Today, context_text
from .vault import Vault

DEFAULT_TIERS = {"default": "claude-sonnet-5", "cheap": "claude-haiku-4-5", "deep": "claude-sonnet-5"}


def build_router() -> BrainRouter:
    settings = get_settings()
    cfg = load_brains_config()
    local = {
        name: OllamaBrain(name, b["url"], b["model"], b.get("label", ""))
        for name, b in (cfg.get("local") or {}).items()
    }
    tiers = {**DEFAULT_TIERS, **(cfg.get("claude") or {})}
    claude = ClaudeBrain(settings.anthropic_api_key, tiers["default"])
    return BrainRouter(local, cfg.get("order") or {}, claude, tiers, load_routing_config(), settings)


def build_today(settings) -> Today:
    vault = Vault(ROOT / "data" / "secret.key")
    google = Google(settings, vault)
    blackboard = Blackboard(settings.blackboard_ics_url)
    return Today(settings, google, blackboard, Calendar(settings, google, blackboard, vault))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    get_engine()
    app.state.agents = load_agents()
    app.state.router = build_router()
    app.state.today = build_today(settings)
    app.state.actions = Actions(app.state.today.calendar, {a.id: a.name for a in app.state.agents.values()})
    task = asyncio.create_task(briefing.run_scheduler(app.state, settings)) if settings.scheduler else None
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Cardinal", lifespan=lifespan)


def pretty_model(model: str) -> str:
    names = {"claude-sonnet-5": "Claude Sonnet 5", "claude-haiku-4-5": "Claude Haiku 4.5",
             "claude-opus-5": "Claude Opus 5", "claude-opus-5-5": "Claude Opus 5.5"}
    return names.get(model, model)


def get_agent(agent_id: str):
    agent = app.state.agents.get(agent_id)
    if not agent:
        raise HTTPException(404, f"Unknown agent: {agent_id}")
    return agent


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/agents")
def list_agents():
    return [a.public() for a in app.state.agents.values()]


@app.get("/api/brains")
async def brains(session: Session = Depends(get_session)):
    status = await app.state.router.status()
    spent = usage.claude_spend_this_month(session, get_settings())
    status["budget_mode"] = usage.budget_mode(spent, get_settings())
    return status


@app.get("/api/agents/{agent_id}/messages")
def messages(agent_id: str, limit: int = 40, session: Session = Depends(get_session)):
    get_agent(agent_id)
    rows = session.exec(
        select(Message).where(Message.agent_id == agent_id).order_by(col(Message.id).desc()).limit(limit)
    ).all()
    return [m.model_dump() for m in reversed(rows)]


class ChatIn(BaseModel):
    agent_id: str
    message: str | None = Field(default=None, max_length=8000)
    retry_with_claude: bool = False


@app.post("/api/chat")
async def chat(body: ChatIn, session: Session = Depends(get_session),
               x_cardinal_device: str | None = Header(default=None)):
    agent = get_agent(body.agent_id)
    settings = get_settings()
    router: BrainRouter = app.state.router

    if body.retry_with_claude:
        last_user = session.exec(
            select(Message).where(Message.agent_id == agent.id, Message.role == "user")
            .order_by(col(Message.id).desc())
        ).first()
        if not last_user:
            raise HTTPException(400, "There is no earlier message to send to Claude.")
    else:
        text = (body.message or "").strip()
        if not text:
            raise HTTPException(400, "Message is empty.")
        device = (x_cardinal_device or "").strip()[:40] or None
        session.add(Message(agent_id=agent.id, role="user", content=text, device=device))
        session.commit()

    history = session.exec(
        select(Message).where(Message.agent_id == agent.id)
        .order_by(col(Message.id).desc()).limit(settings.history_turns)
    ).all()
    turns = [{"role": m.role, "content": m.content} for m in reversed(history)]
    while turns and turns[0]["role"] != "user":
        turns.pop(0)

    context = ""
    if agent.access:
        context = context_text(await app.state.today.for_chat(session), set(agent.access))
    if "tasks" in agent.access:
        extra = review.context_text(session, datetime.now(app.state.today.tz))
        context = f"{context}\n{extra}".strip()
    if "running" in agent.access:
        context = f"{context}\n{running.context_text(session, datetime.now(app.state.today.tz))}".strip()
    try:
        result = await router.run(
            session, agent_id=agent.id, job=agent.job,
            system=agent.system_prompt(context=context, memory=review.memory_text(session)),
            messages=turns, force_claude=body.retry_with_claude,
        )
    except NoBrainAvailable as e:
        raise HTTPException(503, str(e)) from e

    reply = result.reply
    saved = Message(agent_id=agent.id, role="assistant", content=reply.text,
                    provider=reply.provider, model=reply.model, brain=reply.brain)
    session.add(saved)
    session.commit()
    session.refresh(saved)

    local = router.local.get(reply.brain)
    return {
        "message": saved.model_dump(),
        "route": {
            "provider": reply.provider,
            "brain": reply.brain,
            "label": local.label if local else pretty_model(reply.model),
            "model": reply.model,
            "escalated": result.escalated,
            "reason": result.reason,
            "latency_ms": reply.latency_ms,
            "tokens": reply.input_tokens + reply.output_tokens + reply.cache_read_tokens + reply.cache_write_tokens,
            "cost_usd": round(reply.cost_usd, 5),
            "attempts": [a.__dict__ for a in result.attempts],
        },
        "claude_available": await router.claude.available(),
    }


@app.get("/api/usage/summary")
def usage_summary(session: Session = Depends(get_session)):
    names = {a.id: a.name for a in app.state.agents.values()}
    return usage.summary(session, get_settings(), names)


class CreditIn(BaseModel):
    amount_usd: float = Field(gt=0, le=1000)


@app.post("/api/usage/credit")
def add_credit(body: CreditIn, session: Session = Depends(get_session)):
    session.add(CreditTopUp(amount_usd=body.amount_usd))
    session.commit()
    return usage_summary(session)


# ---------- Today: calendar, Blackboard, inboxes, briefing ----------

def briefing_json(b) -> dict | None:
    if not b:
        return None
    local = app.state.router.local.get(b.brain)
    return {**b.model_dump(), "brain_label": local.label if local else pretty_model(b.model or "")}


@app.get("/api/today")
async def today(refresh: bool = False, session: Session = Depends(get_session)):
    t: Today = app.state.today
    snap = await t.snapshot(session, force=refresh)
    day = briefing.local_day(datetime.now(t.tz))
    return {**snap, "google": t.google.status(session), "google_configured": t.google.configured,
            "blackboard_configured": t.blackboard.configured,
            "briefing": briefing_json(briefing.latest(session, day) or briefing.latest(session)),
            "briefing_time": get_settings().briefing_time}


@app.post("/api/briefing")
async def write_briefing_now(session: Session = Depends(get_session)):
    try:
        b = await briefing.write_briefing(session, app.state.router, app.state.agents["ordinal"],
                                          app.state.today, trigger="manual")
    except briefing.BriefingError as e:
        raise HTTPException(409, str(e)) from e
    return briefing_json(b)


def message_page(title: str, text: str, status: int = 400) -> HTMLResponse:
    body = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>Cardinal</title><body style='font:16px system-ui;background:#050a14;color:#dbe8ff;"
            f"padding:32px;max-width:560px;margin:auto'><h2>{html.escape(title)}</h2>"
            f"<p>{html.escape(text)}</p><p><a style='color:#6ab0ff' href='/#today'>Back to Today</a></p>")
    return HTMLResponse(body, status_code=status)


@app.get("/api/google/connect")
def google_connect(slot: str):
    try:
        return RedirectResponse(app.state.today.google.auth_url(slot), status_code=302)
    except GoogleError as e:
        return message_page("Can't connect Google yet", str(e))


@app.get("/api/google/callback")
async def google_callback(code: str | None = None, state: str | None = None, error: str | None = None,
                          session: Session = Depends(get_session)):
    if error or not code or not state:
        why = "You cancelled the sign-in." if error == "access_denied" else f"Google said: {error or 'no code'}."
        return message_page("Google wasn't connected", why)
    try:
        acct = await app.state.today.google.finish(session, code, state)
    except GoogleError as e:
        return message_page("Google wasn't connected", str(e))
    app.state.today.invalidate()
    if acct.slot == "personal":
        await app.state.today.calendar.sync_unsynced(session)  # items saved before calendar access was granted
    return RedirectResponse(f"/?connected={acct.slot}#today", status_code=302)


class SlotIn(BaseModel):
    slot: str


@app.post("/api/google/disconnect")
async def google_disconnect(body: SlotIn, session: Session = Depends(get_session)):
    if body.slot not in SLOTS:
        raise HTTPException(404, "Unknown account.")
    await app.state.today.google.disconnect(session, body.slot)
    app.state.today.invalidate()
    return {"google": app.state.today.google.status(session)}


# ---------- Calendar: day view, your own items, calendar links ----------

def calendar_svc() -> Calendar:
    return app.state.today.calendar


def parse_day(value: str | None) -> date:
    if not value:
        return calendar_svc().today()
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise HTTPException(400, "Dates look like 2026-09-28.") from e


@app.get("/api/calendar/day")
async def calendar_day(date: str | None = None, refresh: bool = False, session: Session = Depends(get_session)):
    try:
        return await calendar_svc().day(session, parse_day(date), force=refresh)
    except CalendarError as e:
        raise HTTPException(400, str(e)) from e


class ItemIn(BaseModel):
    title: str = Field(max_length=200)
    date: str
    start: str | None = None  # HH:MM; empty means all day
    end: str | None = None
    kind: str = "event"
    course: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=2000)


@app.post("/api/calendar/items")
async def add_item(body: ItemIn, session: Session = Depends(get_session)):
    try:
        item, warning = await calendar_svc().add(session, title=body.title, day=parse_day(body.date), start=body.start,
                                                 end=body.end, kind=body.kind, course=body.course, notes=body.notes)
    except CalendarError as e:
        raise HTTPException(400, str(e)) from e
    app.state.today.invalidate()
    return {"item": item.model_dump(), "warning": warning}


@app.delete("/api/calendar/items/{item_id}")
async def delete_item(item_id: int, session: Session = Depends(get_session)):
    try:
        await calendar_svc().delete(session, item_id)
    except CalendarError as e:
        raise HTTPException(404, str(e)) from e
    app.state.today.invalidate()
    return {"ok": True}


@app.get("/api/calendar/feeds")
def list_feeds(session: Session = Depends(get_session)):
    return calendar_svc().feeds(session)


class FeedIn(BaseModel):
    name: str = Field(max_length=40)
    url: str = Field(max_length=2000)


@app.post("/api/calendar/feeds")
async def add_feed(body: FeedIn, session: Session = Depends(get_session)):
    try:
        await calendar_svc().add_feed(session, body.name, body.url)
    except CalendarError as e:
        raise HTTPException(400, str(e)) from e
    app.state.today.invalidate()
    return calendar_svc().feeds(session)


@app.delete("/api/calendar/feeds/{feed_id}")
def delete_feed(feed_id: int, session: Session = Depends(get_session)):
    calendar_svc().delete_feed(session, feed_id)
    app.state.today.invalidate()
    return calendar_svc().feeds(session)


# ---------- Syllabus import: the AI proposes dates, you confirm ----------

class SyllabusIn(BaseModel):
    course: str | None = Field(default=None, max_length=40)
    url: str | None = Field(default=None, max_length=2000)
    text: str | None = Field(default=None, max_length=syllabus.MAX_CHARS)
    file_name: str | None = Field(default=None, max_length=200)
    file_b64: str | None = Field(default=None, max_length=17_000_000)


def import_json(job: SyllabusImport) -> dict:
    return {**job.model_dump(exclude={"proposals"}), "items": json.loads(job.proposals) if job.proposals else []}


@app.post("/api/syllabus")
async def start_syllabus(body: SyllabusIn, session: Session = Depends(get_session)):
    if not (body.url or body.text or body.file_b64):
        raise HTTPException(400, "Give a link, a file or some text.")
    if body.url and not body.url.startswith(("https://", "http://")):
        raise HTTPException(400, "Links start with https://.")
    job = SyllabusImport(course=(body.course or "").strip() or None,
                         source=body.url or body.file_name or "Pasted text", detail="Starting…")
    session.add(job)
    session.commit()
    session.refresh(job)
    task = asyncio.create_task(syllabus.run_import(job.id, app.state.router, calendar_svc().tz, url=body.url,
                                                   text=body.text, file_name=body.file_name, file_b64=body.file_b64))
    app.state.jobs = getattr(app.state, "jobs", set())
    app.state.jobs.add(task)
    task.add_done_callback(app.state.jobs.discard)
    return import_json(job)


@app.get("/api/syllabus")
def list_syllabus(session: Session = Depends(get_session)):
    rows = session.exec(select(SyllabusImport).order_by(col(SyllabusImport.id).desc()).limit(10)).all()
    return [import_json(r) for r in rows]


@app.get("/api/syllabus/{import_id}")
def get_syllabus(import_id: int, session: Session = Depends(get_session)):
    job = session.get(SyllabusImport, import_id)
    if not job:
        raise HTTPException(404, "No such import.")
    return import_json(job)


class ProposalIn(BaseModel):
    date: str
    time: str | None = None
    end: str | None = None
    title: str = Field(max_length=200)
    kind: str = "event"


class AddProposalsIn(BaseModel):
    items: list[ProposalIn] = Field(max_length=300)


@app.post("/api/syllabus/{import_id}/add")
async def add_syllabus_items(import_id: int, body: AddProposalsIn, session: Session = Depends(get_session)):
    job = session.get(SyllabusImport, import_id)
    if not job or job.status not in ("ready", "added"):
        raise HTTPException(404, "That import isn't ready.")
    cal, added, warning = calendar_svc(), 0, None
    for p in body.items:
        try:
            _, w = await cal.add(session, title=p.title, day=parse_day(p.date), start=p.time or None,
                                 end=(p.end or None) if p.time else None, kind=p.kind,
                                 course=job.course, source=f"syllabus:{job.id}")
        except CalendarError:
            continue
        added += 1
        warning = warning or w
    job.status = "added"
    job.detail = f"Added {added} items to your calendar."
    session.add(job)
    session.commit()
    app.state.today.invalidate()
    return {"added": added, "warning": warning}


# ---------- Action Previews, trust rules, Activity Log ----------

def actions_svc() -> Actions:
    return app.state.actions


def action_error(e: ActionError) -> HTTPException:
    return HTTPException(409, str(e))


@app.get("/api/actions")
def list_actions(session: Session = Depends(get_session)):
    svc = actions_svc()
    return {"pending": [svc.to_json(a) for a in svc.pending(session)],
            "recent_auto": [svc.to_json(a) for a in svc.recent_auto(session)]}


@app.get("/api/actions/log")
def action_log(limit: int = 60, session: Session = Depends(get_session)):
    svc = actions_svc()
    return [svc.to_json(a) for a in svc.log(session, min(limit, 200))]


class ApproveIn(BaseModel):
    remember: bool = False


@app.post("/api/actions/{action_id}/approve")
async def approve_action(action_id: int, body: ApproveIn, session: Session = Depends(get_session)):
    svc = actions_svc()
    try:
        a, suggestion = await svc.approve(session, action_id, remember=body.remember)
    except ActionError as e:
        raise action_error(e) from e
    app.state.today.invalidate()
    return {"action": svc.to_json(a), "suggestion": suggestion}


@app.post("/api/actions/{action_id}/deny")
def deny_action(action_id: int, session: Session = Depends(get_session)):
    try:
        return actions_svc().to_json(actions_svc().deny(session, action_id))
    except ActionError as e:
        raise action_error(e) from e


@app.post("/api/actions/{action_id}/undo")
async def undo_action(action_id: int, session: Session = Depends(get_session)):
    try:
        a = await actions_svc().undo(session, action_id)
    except ActionError as e:
        raise action_error(e) from e
    app.state.today.invalidate()
    return actions_svc().to_json(a)


@app.get("/api/actions/{action_id}/rule-preview")
def rule_preview(action_id: int, session: Session = Depends(get_session)):
    try:
        return actions_svc().rule_preview(session, action_id)
    except ActionError as e:
        raise action_error(e) from e


@app.post("/api/actions/{action_id}/remember")
def remember_action(action_id: int, session: Session = Depends(get_session)):
    try:
        return actions_svc().rule_json(actions_svc().remember(session, action_id))
    except ActionError as e:
        raise action_error(e) from e


@app.get("/api/rules")
def list_rules(session: Session = Depends(get_session)):
    return [actions_svc().rule_json(r) for r in actions_svc().rules(session)]


@app.delete("/api/rules/{rule_id}")
def revoke_rule(rule_id: int, session: Session = Depends(get_session)):
    actions_svc().revoke(session, rule_id)
    return list_rules(session)


@app.post("/api/planner/run")
async def run_planner(session: Session = Depends(get_session)):
    result = await planner.plan_study(session, calendar_svc(), actions_svc())
    app.state.today.invalidate()
    return result


# ---------- Review: Delta's check-ins and rollup, Core Memory ----------

def now_local() -> datetime:
    return datetime.now(app.state.today.tz)


def review_error(e: review.ReviewError) -> HTTPException:
    return HTTPException(409, str(e))


async def delta_context(session: Session, now: datetime) -> str:
    snap = await app.state.today.for_chat(session)
    base = context_text(snap, {"calendar"})
    extra = review.context_text(session, now)
    return f"{base}\n{extra}".strip()


@app.get("/api/review")
def review_status(session: Session = Depends(get_session)):
    return review.status(session, now_local(), get_settings())


class MorningIn(BaseModel):
    top: list[str] = Field(max_length=3)
    energy: int = Field(ge=1, le=5)
    note: str | None = Field(default=None, max_length=1000)


@app.post("/api/review/morning")
async def morning_checkin(body: MorningIn, session: Session = Depends(get_session),
                          x_cardinal_device: str | None = Header(default=None)):
    now = now_local()
    try:
        c = await review.morning(session, app.state.router, app.state.agents["delta"], now=now, top=body.top,
                                 energy=body.energy, note=body.note, context=await delta_context(session, now),
                                 device=(x_cardinal_device or "")[:40] or None)
    except review.ReviewError as e:
        raise review_error(e) from e
    return review.checkin_json(c)


class EveningIn(BaseModel):
    done_task_ids: list[int] = []
    done_block_ids: list[int] = []
    went_well: str = Field(default="", max_length=2000)
    didnt: str = Field(default="", max_length=2000)
    why: str = Field(default="", max_length=2000)
    first_task: str | None = Field(default=None, max_length=200)
    carry: bool = True


@app.post("/api/review/evening")
async def evening_review(body: EveningIn, session: Session = Depends(get_session),
                         x_cardinal_device: str | None = Header(default=None)):
    now = now_local()
    c = await review.evening(session, app.state.router, app.state.agents["delta"], now=now,
                             done_task_ids=body.done_task_ids, done_block_ids=body.done_block_ids,
                             went_well=body.went_well.strip(), didnt=body.didnt.strip(), why=body.why.strip(),
                             first_task=body.first_task, carry=body.carry, context=await delta_context(session, now),
                             device=(x_cardinal_device or "")[:40] or None)
    return review.checkin_json(c)


class TaskIn(BaseModel):
    title: str = Field(max_length=200)


@app.post("/api/review/tasks")
def add_task(body: TaskIn, session: Session = Depends(get_session)):
    if not body.title.strip():
        raise HTTPException(400, "Give it a title.")
    day = now_local().date().isoformat()
    t = Task(day=day, title=body.title.strip(), source="manual", position=len(review.tasks_for(session, day)))
    session.add(t)
    session.commit()
    session.refresh(t)
    return t.model_dump()


class TaskStatusIn(BaseModel):
    status: str


@app.post("/api/review/tasks/{task_id}")
def set_task(task_id: int, body: TaskStatusIn, session: Session = Depends(get_session)):
    t = session.get(Task, task_id)
    if not t or body.status not in ("open", "done", "dropped"):
        raise HTTPException(404, "No such task.")
    t.status, t.done_at = body.status, utcnow() if body.status == "done" else None
    session.add(t)
    session.commit()
    session.refresh(t)
    return t.model_dump()


class ExperimentIn(BaseModel):
    result: str | None


@app.post("/api/review/experiments/{exp_id}")
def mark_experiment(exp_id: int, body: ExperimentIn, session: Session = Depends(get_session)):
    e = session.get(Experiment, exp_id)
    if not e or body.result not in (None, "kept", "partly", "skipped"):
        raise HTTPException(404, "No such experiment.")
    e.result = body.result
    session.add(e)
    session.commit()
    session.refresh(e)
    return e.model_dump()


@app.post("/api/review/rollup")
async def rollup_now(session: Session = Depends(get_session)):
    snap = await app.state.today.snapshot(session)
    due = "; ".join(f"{d['title']} ({d['due'][:10]})" for d in snap["due"][:8])
    try:
        r = await review.write_rollup(session, app.state.router, app.state.agents["delta"], app.state.actions,
                                      now=now_local(), due_text=due, trigger="manual")
    except review.ReviewError as e:
        raise review_error(e) from e
    return review.rollup_json(r)


@app.get("/api/memory")
def list_memory(session: Session = Depends(get_session)):
    return [m.model_dump() for m in review.memories(session)]


class MemoryIn(BaseModel):
    text: str = Field(max_length=300)
    kind: str = "fact"


@app.post("/api/memory")
def add_memory(body: MemoryIn, session: Session = Depends(get_session)):
    if not body.text.strip():
        raise HTTPException(400, "Write something to remember.")
    m = Memory(text=body.text.strip(), kind=body.kind if body.kind in ("fact", "pattern", "preference") else "fact")
    session.add(m)
    session.commit()
    return list_memory(session)


@app.patch("/api/memory/{memory_id}")
def edit_memory(memory_id: int, body: MemoryIn, session: Session = Depends(get_session)):
    m = session.get(Memory, memory_id)
    if not m or not body.text.strip():
        raise HTTPException(404, "No such memory.")
    m.text, m.updated_at, m.source, m.confidence = body.text.strip(), utcnow(), "you", 1.0
    session.add(m)
    session.commit()
    return list_memory(session)


@app.delete("/api/memory/{memory_id}")
def delete_memory(memory_id: int, session: Session = Depends(get_session)):
    m = session.get(Memory, memory_id)
    if m:
        session.delete(m)
        session.commit()
    return list_memory(session)


# ---------- Running: Vector's plan, your runs, Apple Watch data ----------

@app.get("/api/running")
def running_status(session: Session = Depends(get_session)):
    return running.status(session, now_local())


class RunIn(BaseModel):
    date: str
    start: str | None = None  # HH:MM
    miles: float = Field(gt=0.1, le=60)
    time: str  # mm:ss or h:mm:ss
    avg_hr: float | None = Field(default=None, ge=40, le=230)
    time_trial: bool = False
    notes: str | None = Field(default=None, max_length=500)


def parse_clock(value: str) -> int:
    parts = [int(p) for p in value.strip().split(":")]
    if not 2 <= len(parts) <= 3 or any(p < 0 for p in parts):
        raise ValueError(value)
    h, m, s = ([0] + parts)[-3:]
    return h * 3600 + m * 60 + s


@app.post("/api/running/runs")
def log_run(body: RunIn, session: Session = Depends(get_session)):
    tz = app.state.today.tz
    try:
        seconds = parse_clock(body.time)
        hh, mm = (int(x) for x in (body.start or "07:00").split(":"))
        start = datetime.combine(date.fromisoformat(body.date), datetime.min.time(), tz).replace(hour=hh, minute=mm)
    except ValueError as e:
        raise HTTPException(400, "Time looks like 31:45 or 1:02:30, and the date like 2026-09-27.") from e
    r = running.add_run(session, start=start, duration_s=seconds, distance_m=body.miles * running.MILE,
                        avg_hr=body.avg_hr, time_trial=body.time_trial, notes=body.notes)
    if not r:
        raise HTTPException(409, "That run is already logged.")
    return running.status(session, now_local())


@app.delete("/api/running/runs/{run_id}")
def delete_run(run_id: int, session: Session = Depends(get_session)):
    r = session.get(running.Run, run_id)
    if r:
        session.delete(r)
        session.commit()
    return running.status(session, now_local())


@app.post("/api/running/propose")
async def propose_runs(session: Session = Depends(get_session)):
    result = await running.propose_runs(session, calendar_svc(), actions_svc(), now_local())
    n, auto = result["proposed"], result["auto"]
    note = f"{n} run{'s' if n != 1 else ''} to review on Today" if n else "No new runs to put on the calendar"
    if auto:
        note += f", {auto} added by your rules"
    if result["skipped"]:
        note += f". No free time for: {', '.join(result['skipped'])}"
    return {**result, "note": note + "."}


@app.post("/api/health/token")
def new_ingest_token(session: Session = Depends(get_session)):
    """A new secret for Health Auto Export's header. Shown once; the hub keeps only its hash."""
    return {"token": running.new_token(session), "url": f"{get_settings().public_url.rstrip('/')}/api/health/ingest",
            "header": "X-Cardinal-Token"}


@app.post("/api/health/ingest")
async def health_ingest(request: Request, session: Session = Depends(get_session),
                        x_cardinal_token: str | None = Header(default=None)):
    if not running.token_ok(session, x_cardinal_token):
        raise HTTPException(401, "Missing or wrong X-Cardinal-Token.")
    try:
        payload = await request.json()
    except ValueError as e:
        raise HTTPException(400, "Send JSON (Health Auto Export: Export Format → JSON).") from e
    return running.ingest_auto_export(session, payload, app.state.today.tz)


@app.post("/api/health/import")
async def health_import(file: UploadFile = File(...), session: Session = Depends(get_session)):
    import os
    import shutil
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".zip")
    try:
        with os.fdopen(fd, "wb") as out:
            await asyncio.to_thread(shutil.copyfileobj, file.file, out, 1024 * 1024)
        try:
            result = await asyncio.to_thread(running.import_health_export, session, path, app.state.today.tz)
        except (ValueError, OSError) as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:  # zipfile.BadZipFile and friends
            raise HTTPException(400, f"Couldn't read that file: {e}") from e
    finally:
        os.unlink(path)
    return {**result, "status": running.status(session, now_local())}


# The web app (plain HTML/JS, no build step). Mounted last so /api routes win.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
