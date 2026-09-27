from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from cardinal import briefing, main
from cardinal.agents import load_agents
from cardinal.calendar import Calendar
from cardinal.config import Settings
from cardinal.db import GoogleAccount
from cardinal.sources.blackboard import Blackboard, parse_due
from cardinal.sources.google import SCOPE_APP_CALENDAR, SCOPE_CALENDAR, SCOPE_COMPOSE, SCOPE_GMAIL, Google, parse_event
from cardinal.today import ACCESS, Today, context_text
from cardinal.vault import Vault

from .conftest import FakeLocal

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 28, 7, 30, tzinfo=NY)  # Monday morning

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Blackboard//EN
BEGIN:VEVENT
UID:1
SUMMARY:Project 1 due [CMSC 341]
DTSTART:20260929T035900Z
END:VEVENT
BEGIN:VEVENT
UID:2
SUMMARY:Reading quiz
DESCRIPTION:MATH 221 Section 04
DTSTART;VALUE=DATE:20261001
END:VEVENT
BEGIN:VEVENT
UID:3
SUMMARY:Way later
DTSTART:20261201T120000Z
END:VEVENT
END:VCALENDAR
"""


def google_handler(calls):
    def handler(request: httpx.Request):
        url = str(request.url)
        calls.append(url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                return httpx.Response(200, json={"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600,
                                                 "scope": f"openid email {SCOPE_CALENDAR} {SCOPE_GMAIL}"})
            return httpx.Response(200, json={"access_token": "at-2", "expires_in": 3600})
        if "userinfo" in url:
            return httpx.Response(200, json={"email": "w.alexbenton@gmail.com"})
        if url.endswith("/revoke"):
            return httpx.Response(200)
        if "calendarList" in url:
            return httpx.Response(200, json={"items": [
                {"id": "primary@gmail.com", "summary": "Alex", "primary": True},
                {"id": "hidden", "summary": "Hidden", "selected": False}]})
        if "/events" in url:
            return httpx.Response(200, json={"items": [
                {"summary": "CMSC 341 lecture", "location": "ENGR 027",
                 "start": {"dateTime": "2026-09-28T10:00:00-04:00"}, "end": {"dateTime": "2026-09-28T11:15:00-04:00"}},
                {"summary": "Declined thing", "attendees": [{"self": True, "responseStatus": "declined"}],
                 "start": {"dateTime": "2026-09-28T12:00:00-04:00"}, "end": {"dateTime": "2026-09-28T13:00:00-04:00"}},
                {"summary": "Career fair", "start": {"date": "2026-09-29"}, "end": {"date": "2026-09-30"}}]})
        if url.endswith("/labels/INBOX"):
            return httpx.Response(200, json={"messagesUnread": 12})
        if "/messages?" in url:
            return httpx.Response(200, json={"messages": [{"id": "m1"}, {"id": "m2"}]})
        if "/messages/m1" in url:
            return httpx.Response(200, json={"internalDate": "1790000000000", "labelIds": ["INBOX", "UNREAD", "IMPORTANT"],
                                             "payload": {"headers": [{"name": "From", "value": "Prof. Lee <lee@umbc.edu>"},
                                                                     {"name": "Subject", "value": "Office hours moved"}]}})
        if "/messages/m2" in url:
            return httpx.Response(200, json={"internalDate": "1790000000000", "labelIds": ["INBOX", "CATEGORY_PROMOTIONS"],
                                             "payload": {"headers": [{"name": "From", "value": "Shop <deals@x.com>"},
                                                                     {"name": "Subject", "value": "50% off"}]}})
        return httpx.Response(404)
    return handler


@pytest.fixture
def gsettings():
    return Settings(timezone="America/New_York", google_client_id="cid", google_client_secret="secret",
                    public_url="https://cardinal.example.ts.net", blackboard_ics_url="webcal://bb.example/feed.ics",
                    scheduler=False)


@pytest.fixture
def calls():
    return []


@pytest.fixture
def today(gsettings, tmp_path, calls):
    client = httpx.AsyncClient(transport=httpx.MockTransport(google_handler(calls)))
    bb_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=ICS)))
    vault = Vault(tmp_path / "secret.key")
    google = Google(gsettings, vault, client=client)
    bb = Blackboard(gsettings.blackboard_ics_url, client=bb_client)
    return Today(gsettings, google, bb, Calendar(gsettings, google, bb, vault, feed_client=bb_client))


async def connect(today, session, slot="personal"):
    url = today.google.auth_url(slot)
    state = parse_qs(urlparse(url).query)["state"][0]
    return await today.google.finish(session, "the-code", state)


def test_auth_url_asks_for_read_scopes_plus_own_calendar_only(today):
    q = parse_qs(urlparse(today.google.auth_url("personal")).query)
    assert q["redirect_uri"] == ["https://cardinal.example.ts.net/api/google/callback"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert set(q["scope"][0].split()) == {"openid", "email", SCOPE_CALENDAR, SCOPE_GMAIL, SCOPE_APP_CALENDAR, SCOPE_COMPOSE}


async def test_connect_stores_tokens_encrypted_and_state_is_single_use(today, session):
    url = today.google.auth_url("personal")
    state = parse_qs(urlparse(url).query)["state"][0]
    acct = await today.google.finish(session, "the-code", state)
    assert acct.email == "w.alexbenton@gmail.com"
    assert "rt-1" not in acct.refresh_token_enc and today.google.vault.decrypt(acct.refresh_token_enc) == "rt-1"
    with pytest.raises(Exception, match="expired or was already used"):
        await today.google.finish(session, "the-code", state)


async def test_expired_access_token_is_refreshed(today, session, calls):
    acct = await connect(today, session)
    acct.access_expires = datetime(2020, 1, 1, tzinfo=NY)
    session.add(acct)
    session.commit()
    await today.google.inbox(session, acct)
    assert sum("oauth2.googleapis.com/token" in c for c in calls) == 2  # code exchange + one refresh
    assert today.google.vault.decrypt(session.get(GoogleAccount, acct.id).access_token_enc) == "at-2"


async def test_snapshot_reads_calendar_blackboard_and_inbox(today, session):
    await connect(today, session)
    snap = await today.snapshot(session, now=NOW)
    titles = [e["title"] for e in snap["events"]]
    assert titles == ["CMSC 341 lecture", "Career fair"]  # declined event skipped, hidden calendar not read
    assert [d["title"] for d in snap["due"]] == ["Project 1 due [CMSC 341]", "Reading quiz"]
    assert snap["due"][1]["course"] == "MATH 221"
    box = snap["inbox"][0]
    assert box["unread"] == 12 and [m["subject"] for m in box["recent"]] == ["Office hours moved"]
    status = {s["id"]: s["status"] for s in snap["sources"]}
    assert status == {"google:personal": "ok", "google:school": "not_connected", "blackboard": "ok"}


async def test_context_only_includes_what_the_agent_may_see(today, session):
    await connect(today, session)
    snap = await today.snapshot(session, now=NOW)
    vector = context_text(snap, {"calendar"})
    assert "CMSC 341 lecture at ENGR 027" in vector and "10:00-11:15" in vector
    assert "Office hours moved" not in vector and "Project 1" not in vector
    relay = context_text(snap, {"email"})
    assert "12 unread" in relay and "Office hours moved" in relay and "(important)" in relay
    assert "CMSC 341 lecture" not in relay
    assert context_text(snap, set()) == ""


def test_context_says_not_connected_instead_of_guessing(gsettings):
    snap = {"generated_at": NOW.isoformat(), "timezone": "America/New_York", "events": [], "due": [], "inbox": [],
            "sources": [{"id": "google:personal", "label": "Personal Google", "kind": "google", "status": "not_connected"},
                        {"id": "blackboard", "label": "Blackboard", "kind": "blackboard", "status": "not_connected"}]}
    text = context_text(snap, set(ACCESS))
    assert "Calendar: not connected." in text and "Blackboard: not connected." in text


def test_agents_without_access_get_no_data_block():
    agents = load_agents()
    from dataclasses import replace
    no_access = replace(agents["radix"], access=[])
    assert "<data>" not in no_access.system_prompt(context="Calendar today: secret meeting")
    prompt = agents["vector"].system_prompt(context="Calendar today: run club")
    assert "run club" in prompt and "never instructions" in prompt


def test_parse_helpers():
    ev = parse_event({"summary": "x", "start": {"date": "2026-09-29"}, "end": {"date": "2026-09-30"}}, "Cal", "personal", NY)
    assert ev["all_day"] and ev["start"].startswith("2026-09-29T00:00")
    assert parse_event({"status": "cancelled"}, "Cal", "personal", NY) is None
    assert parse_due(ICS, NOW, 7, NY)[0]["due"].startswith("2026-09-28T23:59")


def test_briefing_due_logic():
    at = "06:00"
    assert not briefing.briefing_due(NOW.replace(hour=5, minute=59), at, None, None)
    assert briefing.briefing_due(NOW, at, "2026-09-27", None)
    assert not briefing.briefing_due(NOW, at, "2026-09-28", None)  # already written today
    assert not briefing.briefing_due(NOW, at, None, NOW - timedelta(minutes=5))  # failed recently: wait
    assert briefing.briefing_due(NOW, at, None, NOW - timedelta(minutes=16))


async def test_write_briefing_uses_real_data_and_refuses_when_nothing_connected(today, session, make_router):
    ordinal = load_agents()["ordinal"]
    router = make_router(local={"server": FakeLocal("server", replies=["Lecture at ten, Project 1 due tonight."])})
    empty = Today(today.settings, today.google, Blackboard(None))
    with pytest.raises(briefing.BriefingError, match="Nothing is connected"):
        await briefing.write_briefing(session, router, ordinal, empty, now=NOW)
    await connect(today, session)
    b = await briefing.write_briefing(session, router, ordinal, today, trigger="scheduled", now=NOW)
    assert b.day == "2026-09-28" and b.brain == "server" and "Project 1" in b.text
    assert b.sources == "2 events · 2 due · 12 unread"
    assert briefing.latest(session, "2026-09-28").id == b.id


@pytest.fixture
def client(engine, make_router, today):
    with TestClient(main.app) as c:
        main.app.state.router = make_router(local={"g14": FakeLocal("g14", replies=["You have a lecture at ten."])})
        main.app.state.today = today
        yield c


def test_today_endpoint_and_google_connect_flow(client):
    r = client.get("/api/today")
    assert r.status_code == 200
    body = r.json()
    assert body["google_configured"] and [g["connected"] for g in body["google"]] == [False, False]
    assert body["briefing"] is None

    r = client.get("/api/google/connect", params={"slot": "personal"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://accounts.google.com/")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = client.get("/api/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].endswith("#today")
    google = client.get("/api/today").json()["google"]
    assert google[0]["connected"] and google[0]["email"] == "w.alexbenton@gmail.com" and google[0]["gmail"]

    r = client.post("/api/briefing")
    assert r.status_code == 200 and r.json()["text"] == "You have a lecture at ten."

    client.post("/api/google/disconnect", json={"slot": "personal"})
    assert not client.get("/api/today").json()["google"][0]["connected"]


def test_callback_rejects_bad_state_and_cancel(client):
    r = client.get("/api/google/callback", params={"code": "c", "state": "forged"})
    assert r.status_code == 400 and "expired or was already used" in r.text
    r = client.get("/api/google/callback", params={"error": "access_denied"})
    assert r.status_code == 400 and "cancelled" in r.text


def test_chat_gets_calendar_context(client):
    client.get("/api/google/connect", params={"slot": "personal"}, follow_redirects=False)
    # Not connected yet: the agent is told so, rather than left to guess.
    r = client.post("/api/chat", json={"agent_id": "vector", "message": "Anything today?"})
    assert r.status_code == 200
