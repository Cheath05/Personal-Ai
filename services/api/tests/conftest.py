import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

from cardinal import db
from cardinal.brains import BrainError, BrainReply
from cardinal.config import Settings
from cardinal.pricing import claude_cost
from cardinal.router import BrainRouter

ROUTING = {
    "essential_jobs": ["weekly_rollup"],
    "local_attempts": 2,
    "jobs": {
        "chat": {"policy": "local_then_claude", "lane": "interactive", "tier": "default", "max_tokens": 500},
        "quick": {"policy": "local", "lane": "interactive", "max_tokens": 200},
        "triage": {"policy": "local", "lane": "background", "max_tokens": 200},
        "weekly_rollup": {"policy": "claude", "lane": "background", "tier": "deep", "max_tokens": 1000},
    },
}
TIERS = {"default": "claude-sonnet-5", "cheap": "claude-haiku-4-5", "deep": "claude-sonnet-5"}


class FakeLocal:
    provider = "local"

    def __init__(self, name, replies=None, online=True, model="qwen3:4b"):
        self.name, self.model, self.label, self.online = name, model, name.upper(), online
        self.replies = list(replies or ["Sure, here you go."])
        self.calls = 0

    async def available(self):
        return self.online

    async def chat(self, system, messages, *, max_tokens, effort=None):
        self.calls += 1
        item = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(item, Exception):
            raise item
        return BrainReply(text=item, provider="local", brain=self.name, model=self.model,
                          input_tokens=120, output_tokens=40)


class FakeClaude:
    provider = "claude"
    name = "claude"

    def __init__(self, configured=True, text="Claude's careful answer."):
        self._client = object() if configured else None
        self.model = "claude-sonnet-5"
        self.label = self.model
        self.text = text
        self.calls = []

    def with_model(self, model):
        self.model = model
        return self

    async def available(self):
        return self._client is not None

    async def chat(self, system, messages, *, max_tokens, effort=None):
        if self._client is None:
            raise BrainError("No Anthropic API key is set.")
        self.calls.append(self.model)
        return BrainReply(text=self.text, provider="claude", brain="claude", model=self.model,
                          input_tokens=1000, output_tokens=200,
                          cost_usd=claude_cost(self.model, 1000, 200))


@pytest.fixture
def settings():
    return Settings(budget_cap_usd=20, budget_soft_usd=15, timezone="America/New_York",
                    database_url="sqlite://", anthropic_api_key=None)


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.set_engine(eng)
    return eng


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def make_router(settings):
    def _make(local=None, claude=None, order=None):
        local = local if local is not None else {"g14": FakeLocal("g14")}
        order = order or {"interactive": ["g14", "mac", "server"], "background": ["server", "g14", "mac"]}
        return BrainRouter(local, order, claude or FakeClaude(), TIERS, ROUTING, settings)
    return _make
