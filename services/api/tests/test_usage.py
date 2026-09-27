from datetime import UTC, datetime, timedelta

from cardinal import usage
from cardinal.db import CreditTopUp, UsageEvent
from cardinal.pricing import claude_cost

NAMES = {"cardinal": "Cardinal", "delta": "Delta", "relay": "Relay"}
NOW = datetime(2026, 9, 15, 16, 0, tzinfo=UTC)  # 15 Sep, noon in New York


def add(session, ts, agent, provider, cost=0.0, tokens=(100, 50)):
    session.add(UsageEvent(ts=ts, agent_id=agent, job="chat", provider=provider,
                           brain="g14" if provider == "local" else "claude",
                           model="qwen3:8b" if provider == "local" else "claude-sonnet-5",
                           input_tokens=tokens[0], output_tokens=tokens[1], cost_usd=cost))
    session.commit()


def test_claude_cost_matches_list_prices():
    assert claude_cost("claude-sonnet-5", 1_000_000, 0) == 2.0
    assert claude_cost("claude-haiku-4-5", 0, 1_000_000) == 5.0
    # cache reads at 0.1x input, 5-minute writes at 1.25x input
    assert round(claude_cost("claude-sonnet-5", 0, 0, 1_000_000, 1_000_000), 6) == 0.2 + 2.5


def test_budget_modes(settings):
    assert usage.budget_mode(3, settings) == usage.NORMAL
    assert usage.budget_mode(15, settings) == usage.ESSENTIALS
    assert usage.budget_mode(20, settings) == usage.LOCAL_ONLY


def test_summary_counts_month_today_and_agents(session, settings):
    add(session, NOW - timedelta(hours=1), "cardinal", "local")
    add(session, NOW - timedelta(hours=2), "relay", "local")
    add(session, NOW - timedelta(days=3), "delta", "claude", cost=1.50, tokens=(4000, 800))
    add(session, NOW - timedelta(days=40), "delta", "claude", cost=9.0)  # last month: excluded

    s = usage.summary(session, settings, NAMES, now=NOW)
    m = s["month"]
    assert m["requests"] == 3
    assert m["claude_cost"] == 1.5
    assert m["local_tokens"] == 300 and m["claude_tokens"] == 4800
    assert m["local_share"] == round(2 / 3, 3)
    assert m["by_agent"][0]["name"] == "Delta"
    assert s["today"]["requests"] == 2 and s["today"]["claude_cost"] == 0
    assert s["mode"] == usage.NORMAL
    # 1.50 over ~14.7 elapsed days, projected to 30 days
    assert 3.0 < m["projected_cost"] < 3.2


def test_credit_left_and_low_credit_tip(session, settings):
    session.add(CreditTopUp(ts=NOW - timedelta(days=5), amount_usd=5.0))
    session.commit()
    add(session, NOW - timedelta(days=1), "delta", "claude", cost=1.25)
    s = usage.summary(session, settings, NAMES, now=NOW)
    assert s["credit"] == {"loaded_usd": 5.0, "spent_usd": 1.25, "left_usd": 3.75}
    assert any("under $5" in t for t in s["tips"])


def test_all_local_tip(session, settings):
    add(session, NOW - timedelta(hours=1), "cardinal", "local")
    tips = usage.summary(session, settings, NAMES, now=NOW)["tips"]
    assert "Everything has run locally this month. Claude spend is $0." in tips


def test_old_database_gets_new_columns_without_losing_rows(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from cardinal.db import add_missing_columns

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:  # a database from before the "device" column existed
        conn.execute(text("CREATE TABLE message (id INTEGER PRIMARY KEY, ts DATETIME, agent_id VARCHAR NOT NULL, "
                          "role VARCHAR NOT NULL, content VARCHAR NOT NULL, provider VARCHAR, model VARCHAR, brain VARCHAR)"))
        conn.execute(text("INSERT INTO message (agent_id, role, content) VALUES ('vector', 'user', 'keep me')"))
    assert "message.device" in add_missing_columns(engine)
    assert "device" in {c["name"] for c in inspect(engine).get_columns("message")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT content FROM message")).scalar() == "keep me"
