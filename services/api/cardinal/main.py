"""Cardinal API: agents, chat through the brain router, usage, today's data, and the web app."""

import asyncio
import contextlib
import html
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from . import briefing, usage
from .agents import load_agents
from .brains import ClaudeBrain, OllamaBrain
from .config import ROOT, WEB_DIR, get_settings, load_brains_config, load_routing_config
from .db import CreditTopUp, Message, get_engine, get_session
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
    google = Google(settings, Vault(ROOT / "data" / "secret.key"))
    return Today(settings, google, Blackboard(settings.blackboard_ics_url))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    get_engine()
    app.state.agents = load_agents()
    app.state.router = build_router()
    app.state.today = build_today(settings)
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
    try:
        result = await router.run(
            session, agent_id=agent.id, job=agent.job, system=agent.system_prompt(context=context),
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


# The web app (plain HTML/JS, no build step). Mounted last so /api routes win.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
