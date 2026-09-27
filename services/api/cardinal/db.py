"""Database models and session helpers (SQLite locally, Postgres on the server)."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import event, inspect, text
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
    device: str | None = None  # where you were when you sent it ("iPhone", "Mac", ...); memory is shared


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
        if url.startswith("sqlite"):
            event.listen(_engine, "connect", _sqlite_pragmas)
        SQLModel.metadata.create_all(_engine)
        add_missing_columns(_engine)
    return _engine


def add_missing_columns(engine) -> list[str]:
    """Add columns that newer code expects to an older database, so upgrades never lose your history.

    Only handles the common case (new optional columns). Bigger changes will get real migrations."""
    inspector = inspect(engine)
    added = []
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing or not column.nullable:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))
                added.append(f"{table.name}.{column.name}")
    return added


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    # WAL lets the phone, the Mac and scheduled jobs read while another device writes.
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def set_engine(engine) -> None:
    """Used by tests to point at an in-memory database."""
    global _engine
    _engine = engine
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
