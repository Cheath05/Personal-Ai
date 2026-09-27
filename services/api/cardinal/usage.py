"""Token and cost tracking, the monthly budget guard, and the usage summary."""

import calendar
from collections import defaultdict
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from .brains import BrainReply
from .config import Settings
from .db import CreditTopUp, UsageEvent

NORMAL, ESSENTIALS, LOCAL_ONLY = "normal", "essentials", "local_only"


def record(session: Session, agent_id: str, job: str, reply: BrainReply,
           ok: bool = True, reason: str | None = None) -> UsageEvent:
    event = UsageEvent(
        agent_id=agent_id, job=job, provider=reply.provider, brain=reply.brain, model=reply.model,
        input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
        cache_read_tokens=reply.cache_read_tokens, cache_write_tokens=reply.cache_write_tokens,
        cost_usd=reply.cost_usd, latency_ms=reply.latency_ms, ok=ok, reason=reason,
    )
    session.add(event)
    session.commit()
    return event


def _as_utc(ts: datetime) -> datetime:
    # SQLite hands datetimes back without a timezone; they were stored as UTC.
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _month_start(now_local: datetime) -> datetime:
    return datetime.combine(now_local.date().replace(day=1), time(), now_local.tzinfo).astimezone(UTC)


def _day_start(now_local: datetime) -> datetime:
    return datetime.combine(now_local.date(), time(), now_local.tzinfo).astimezone(UTC)


def _events_since(session: Session, since: datetime) -> list[UsageEvent]:
    rows = session.exec(select(UsageEvent).where(UsageEvent.ts >= since)).all()
    return [e for e in rows if _as_utc(e.ts) >= since]


def claude_spend_this_month(session: Session, settings: Settings, now: datetime | None = None) -> float:
    now_local = (now or datetime.now(UTC)).astimezone(ZoneInfo(settings.timezone))
    events = _events_since(session, _month_start(now_local))
    return sum(e.cost_usd for e in events if e.provider == "claude")


def budget_mode(spent: float, settings: Settings) -> str:
    if spent >= settings.budget_cap_usd:
        return LOCAL_ONLY
    if spent >= settings.budget_soft_usd:
        return ESSENTIALS
    return NORMAL


def _totals(events: list[UsageEvent]) -> dict:
    local = [e for e in events if e.provider == "local"]
    claude = [e for e in events if e.provider == "claude"]
    tokens = lambda es: sum(e.input_tokens + e.output_tokens + e.cache_read_tokens + e.cache_write_tokens for e in es)
    return {
        "requests": len(events),
        "local_requests": len(local),
        "claude_requests": len(claude),
        "local_tokens": tokens(local),
        "claude_tokens": tokens(claude),
        "claude_cost": round(sum(e.cost_usd for e in claude), 4),
        "local_share": round(len(local) / len(events), 3) if events else None,
    }


def summary(session: Session, settings: Settings, agent_names: dict[str, str],
            now: datetime | None = None) -> dict:
    now_utc = now or datetime.now(UTC)
    now_local = now_utc.astimezone(ZoneInfo(settings.timezone))
    month_events = _events_since(session, _month_start(now_local))
    day_start = _day_start(now_local)
    today_events = [e for e in month_events if _as_utc(e.ts) >= day_start]

    month = _totals(month_events)
    days_in_month = calendar.monthrange(now_local.year, now_local.month)[1]
    elapsed_days = max((now_utc - _month_start(now_local)).total_seconds() / 86400, 1.0)
    month["projected_cost"] = round(month["claude_cost"] / elapsed_days * days_in_month, 2)
    month["label"] = now_local.strftime("%B %Y")

    by_agent: dict[str, dict] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "requests": 0, "claude_requests": 0})
    for e in month_events:
        row = by_agent[e.agent_id]
        row["tokens"] += e.input_tokens + e.output_tokens + e.cache_read_tokens + e.cache_write_tokens
        row["cost"] += e.cost_usd
        row["requests"] += 1
        row["claude_requests"] += e.provider == "claude"
    month["by_agent"] = sorted(
        ({"agent_id": k, "name": agent_names.get(k, k), **v, "cost": round(v["cost"], 4)} for k, v in by_agent.items()),
        key=lambda r: (r["cost"], r["tokens"]), reverse=True,
    )

    topups = session.exec(select(CreditTopUp)).all()
    credit = None
    if topups:
        first = min(_as_utc(t.ts) for t in topups)
        spent_since = sum(e.cost_usd for e in _events_since(session, first) if e.provider == "claude")
        loaded = sum(t.amount_usd for t in topups)
        credit = {"loaded_usd": round(loaded, 2), "spent_usd": round(spent_since, 4),
                  "left_usd": round(loaded - spent_since, 2)}

    mode = budget_mode(month["claude_cost"], settings)
    return {
        "mode": mode,
        "cap_usd": settings.budget_cap_usd,
        "soft_usd": settings.budget_soft_usd,
        "month": month,
        "today": _totals(today_events),
        "credit": credit,
        "tips": _tips(month, mode, credit, settings),
    }


def _tips(month: dict, mode: str, credit: dict | None, settings: Settings) -> list[str]:
    tips = []
    if mode == LOCAL_ONLY:
        tips.append(f"The ${settings.budget_cap_usd:.0f} cap is reached. Everything runs locally until the 1st.")
    elif mode == ESSENTIALS:
        tips.append(f"Past ${settings.budget_soft_usd:.0f}: only essential jobs use Claude until the 1st.")
    elif month["projected_cost"] > settings.budget_cap_usd:
        tips.append(f"At this pace you'd pass the ${settings.budget_cap_usd:.0f} cap before month end. "
                    "Cardinal switches to local-only when you reach it.")
    if month["requests"] and month["claude_cost"] == 0:
        tips.append("Everything has run locally this month. Claude spend is $0.")
    top = next((a for a in month["by_agent"] if a["cost"] > 0), None)
    if top and month["claude_cost"] and top["cost"] / month["claude_cost"] > 0.5 and month["claude_cost"] > 1:
        tips.append(f"{top['name']} accounts for most of this month's Claude spend.")
    if credit and credit["left_usd"] < 5:
        tips.append("Prepaid credit is under $5. Top up in the Anthropic Console if you want Claude to keep helping.")
    return tips
