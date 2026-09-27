import pytest
from fastapi.testclient import TestClient

from cardinal import main

from .conftest import FakeClaude, FakeLocal


@pytest.fixture
def client(engine, make_router):
    with TestClient(main.app) as c:
        main.app.state.router = make_router(local={"g14": FakeLocal("g14", replies=["Easy 2 miles today."])},
                                            claude=FakeClaude())
        yield c


def test_chat_saves_both_messages_and_reports_route(client):
    r = client.post("/api/chat", json={"agent_id": "vector", "message": "What's my run?"})
    assert r.status_code == 200
    body = r.json()
    assert body["message"]["content"] == "Easy 2 miles today."
    assert body["route"]["provider"] == "local" and body["route"]["cost_usd"] == 0
    history = client.get("/api/agents/vector/messages").json()
    assert [m["role"] for m in history] == ["user", "assistant"]


def test_ask_claude_reuses_last_message_and_costs_money(client):
    client.post("/api/chat", json={"agent_id": "vector", "message": "Plan my week"})
    r = client.post("/api/chat", json={"agent_id": "vector", "retry_with_claude": True})
    assert r.json()["route"]["provider"] == "claude"
    history = client.get("/api/agents/vector/messages").json()
    assert [m["role"] for m in history] == ["user", "assistant", "assistant"]
    usage = client.get("/api/usage/summary").json()
    # 2 local calls: the tool-planning step, then the reply. Claude only answered the retry.
    assert usage["month"]["claude_cost"] > 0 and usage["month"]["local_requests"] == 2


def test_unknown_agent_and_empty_message_are_rejected(client):
    assert client.post("/api/chat", json={"agent_id": "nobody", "message": "hi"}).status_code == 404
    assert client.post("/api/chat", json={"agent_id": "vector", "message": "  "}).status_code == 400


def test_credit_topup_shows_in_summary(client):
    r = client.post("/api/usage/credit", json={"amount_usd": 10})
    assert r.json()["credit"]["loaded_usd"] == 10


def test_web_app_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Cardinal" in r.text


def test_privacy_policy_is_public_page(client):
    r = client.get("/privacy.html")
    assert r.status_code == 200 and "Google API Services User Data Policy" in r.text
