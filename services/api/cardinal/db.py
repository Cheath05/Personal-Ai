"""Database models and session helpers (SQLite locally, Postgres on the server)."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Field, Session, SQLModel, create_engine

from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(UTC)


class Message(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    agent_id: str = Field(index=True)
    role: str  # "user" | "assistant"
    content: str
    provider: str | None = None  # "local" | "claude" for assistant messages
    model: str | None = None
    brain: str | None = None


class UsageEvent(SQLModel, table=True):
    """One model call. Local calls are free but still counted, so you can see load."""

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    agent_id: str = Field(index=True)
    job: str
    provider: str = Field(index=True)  # "local" | "claude"
    brain: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    ok: bool = True
    reason: str | None = None  # why this call happened (e.g. an escalation)


class CreditTopUp(SQLModel, table=True):
    """Prepaid Anthropic credit you told Cardinal about, for the "credit left" estimate."""

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow)
    amount_usd: float


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        url = get_settings().database_url
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
        SQLModel.metadata.create_all(_engine)
    return _engine


def set_engine(engine) -> None:
    """Used by tests to point at an in-memory database."""
    global _engine
    _engine = engine
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
