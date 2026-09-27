"""Cardinal API: agents, chat through the brain router, usage, and the web app."""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from . import usage
from .agents import load_agents
from .brains import ClaudeBrain, OllamaBrain
from .config import WEB_DIR, get_settings, load_brains_config, load_routing_config
from .db import CreditTopUp, Message, get_engine, get_session
from .router import BrainRouter, NoBrainAvailable

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_engine()
    app.state.agents = load_agents()
    app.state.router = build_router()
    yield


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

    try:
        result = await router.run(
            session, agent_id=agent.id, job=agent.job, system=agent.system_prompt(),
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


# The web app (plain HTML/JS, no build step). Mounted last so /api routes win.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
