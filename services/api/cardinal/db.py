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


class GoogleAccount(SQLModel, table=True):
    """A connected Google account. Tokens are encrypted by the Vault; the key is not in this database."""

    id: int | None = Field(default=None, primary_key=True)
    slot: str = Field(index=True, unique=True)  # "personal" | "school"
    email: str | None = None
    refresh_token_enc: str
    access_token_enc: str | None = None
    access_expires: datetime | None = None
    scopes: str = ""
    connected_at: datetime = Field(default_factory=utcnow)
    last_error: str | None = None
    cardinal_calendar_id: str | None = None  # the "Cardinal" calendar Cardinal created (and may edit)


class CalendarItem(SQLModel, table=True):
    """Something you added in Cardinal, typed in or confirmed from a syllabus.

    Mirrored to the "Cardinal" Google calendar when that's allowed, so it shows in Apple Calendar too."""

    id: int | None = Field(default=None, primary_key=True)
    title: str
    start: datetime = Field(index=True)
    end: datetime
    all_day: bool = False
    kind: str = "event"  # event | class | due | exam | quiz | reading | no_class
    course: str | None = None
    notes: str | None = None
    source: str = "manual"  # "manual" | "syllabus:<import id>"
    google_event_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class CalendarFeed(SQLModel, table=True):
    """Another calendar by link (e.g. an iCloud calendar's public link). The link is encrypted."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    url_enc: str
    color: str = "#a47bff"
    created_at: datetime = Field(default_factory=utcnow)
    last_error: str | None = None


class SyllabusImport(SQLModel, table=True):
    """A syllabus you gave Cardinal. Proposed dates wait here until you confirm them."""

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow)
    course: str | None = None
    source: str  # the link, or the file name
    status: str = "reading"  # reading | ready | failed | added
    detail: str | None = None  # progress or the error
    proposals: str | None = None  # JSON list of proposed items
    brain: str | None = None


class Briefing(SQLModel, table=True):
    """Ordinal's briefing for a day. Written from real data only; `sources` records what was used."""

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    day: str = Field(index=True)  # local date, YYYY-MM-DD
    text: str
    brain: str | None = None
    model: str | None = None
    sources: str | None = None  # e.g. "4 events · 2 due · 9 unread"
    trigger: str = "scheduled"  # "scheduled" | "manual"


class Action(SQLModel, table=True):
    """Something an agent wants to change. Nothing runs until you authorize it or a trust rule covers it.

    Also the Activity Log: executed, denied, undone and failed actions stay here."""

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    agent_id: str
    kind: str = Field(index=True)  # e.g. "calendar.add_block"
    title: str
    reason: str
    payload: str  # JSON
    status: str = Field(default="pending", index=True)  # pending | executed | denied | undone | failed | expired
    undoable: bool = True
    dedupe_key: str | None = Field(default=None, index=True)
    rule_id: int | None = None  # set when a trust rule ran it for you
    decided_at: datetime | None = None
    executed_at: datetime | None = None
    result: str | None = None  # JSON, e.g. the created item's id (used to undo)
    error: str | None = None


class TrustRule(SQLModel, table=True):
    """A remembered approval: a narrow "always allow" for one agent and one kind of action."""

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=utcnow)
    agent_id: str
    kind: str
    conditions: str  # JSON, e.g. {"max_minutes": 120, "earliest": "08:00", "latest": "22:00"}
    description: str  # exactly what you agreed to, in words
    uses: int = 0
    last_used: datetime | None = None
    active: bool = True
    source_action_id: int | None = None


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
