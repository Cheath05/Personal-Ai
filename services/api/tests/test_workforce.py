import base64
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from cardinal import appusage, main, relay, research, tools
from cardinal.actions import ActionError, Actions
from cardinal.agents import load_agents
from cardinal.calendar import Calendar
from cardinal.db import Action, EmailItem
from cardinal.sources.blackboard import Blackboard
from cardinal.sources.google import SCOPE_CALENDAR, SCOPE_COMPOSE, SCOPE_GMAIL, Google
from cardinal.today import Today
from cardinal.vault import Vault

from .conftest import FakeClaude, FakeLocal
from .test_today import gsettings  # noqa: F401  (fixture)

NY = ZoneInfo("America/New_York")
AGENTS = load_agents()


def gmail_handler(calls, compose=True):
    def handler(request: httpx.Request):
        url = str(request.url)
        calls.append(f"{request.method} {url}")
        if url.startswith("https://oauth2.googleapis.com/token"):
            scopes = f"openid email {SCOPE_CALENDAR} {SCOPE_GMAIL}" + (f" {SCOPE_COMPOSE}" if compose else "")
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": scopes})
        if "userinfo" in url:
            return httpx.Response(200, json={"email": "w.alexbenton@gmail.com"})
        if "/messages?" in url:
            return httpx.Response(200, json={"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]})
        if url.split("?")[0].endswith("/messages/m1") and "format=full" in url:
            body = base64.urlsafe_b64encode(b"Hi Alex, can you come to office hours Thursday at 2pm? - Prof. Lee").decode()
            return httpx.Response(200, json={"id": "m1", "threadId": "t1", "payload": {
                "mimeType": "text/plain", "body": {"data": body},
                "headers": [{"name": "From", "value": "Prof. Lee <lee@umbc.edu>"}, {"name": "Subject", "value": "Office hours"},
                            {"name": "Message-ID", "value": "<abc@umbc.edu>"}]}})
        for mid, frm, subj, labels in (("m1", "Prof. Lee <lee@umbc.edu>", "Office hours", ["INBOX", "UNREAD", "IMPORTANT"]),
                                       ("m2", "Shop <deals@x.com>", "50% off", ["INBOX", "CATEGORY_PROMOTIONS"]),
                                       ("m3", "Registrar <reg@umbc.edu>", "Add/drop deadline Oct 3", ["INBOX"])):
            if url.split("?")[0].endswith(f"/messages/{mid}"):
                return httpx.Response(200, json={"id": mid, "threadId": f"t{mid}", "internalDate": "1790000000000",
                                                 "labelIds": labels, "snippet": f"{subj} snippet",
                                                 "payload": {"headers": [{"name": "From", "value": frm},
                                                                         {"name": "Subject", "value": subj}]}})
        if request.method == "POST" and url.endswith("/drafts"):
            raw = json.loads(request.content)["message"]
            return httpx.Response(200, json={"id": "d1", "message": {"threadId": raw.get("threadId")}})
        if request.method == "POST" and url.endswith("/drafts/send"):
            return httpx.Response(200, json={"id": "sent1"})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)
    return handler


def build(gsettings, tmp_path, calls, compose=True):  # noqa: F811
    client = httpx.AsyncClient(transport=httpx.MockTransport(gmail_handler(calls, compose)))
    vault = Vault(tmp_path / "k")
    google = Google(gsettings, vault, client=client)
    bb = Blackboard(None)
    return Today(gsettings, google, bb, Calendar(gsettings, google, bb, vault))


async def connect(today, session):
    state = parse_qs(urlparse(today.google.auth_url("personal")).query)["state"][0]
    return await today.google.finish(session, "code", state)


SORT = json.dumps({"emails": [
    {"n": 1, "category": "reply", "reason": "Professor asks about a meeting", "task": "Answer Prof. Lee", "due": "Thursday 2pm"},
    {"n": 2, "category": "urgent", "reason": "Deadline soon", "task": "Decide on add/drop", "due": "Oct 3"}]})


def router(make_router, *replies):
    return make_router(local={"g14": FakeLocal("g14", replies=list(replies))}, claude=FakeClaude(configured=False))


async def test_relay_sorts_new_mail_once(gsettings, tmp_path, session, make_router):  # noqa: F811
    today = build(gsettings, tmp_path, [])
    await connect(today, session)
    out = await relay.sort_inbox(session, router(make_router, SORT), today.google)
    assert out == {"sorted": 3, "errors": []}
    items = {e.subject: e for e in relay.inbox(session)}
    assert items["50% off"].category == "noise" and items["50% off"].sorted_by == "rule"  # Gmail's label, no model
    assert items["Office hours"].category == "reply" and items["Office hours"].task == "Answer Prof. Lee"
    assert items["Add/drop deadline Oct 3"].due == "Oct 3"
    assert [e.category for e in relay.inbox(session)][:2] == ["urgent", "reply"]  # most pressing first
    again = await relay.sort_inbox(session, router(make_router, SORT), today.google)
    assert again["sorted"] == 0


async def test_draft_is_saved_in_thread_and_sending_always_asks(gsettings, tmp_path, session, make_router):  # noqa: F811
    calls = []
    today = build(gsettings, tmp_path, calls)
    await connect(today, session)
    await relay.sort_inbox(session, router(make_router, SORT), today.google)
    item = session.exec(select(EmailItem).where(EmailItem.subject == "Office hours")).one()
    d = await relay.write_draft(session, router(make_router, "Subject: Re\nHi Prof. Lee, Thursday at 2 works. Thanks! Alex"),
                                AGENTS["relay"], today.google, item)
    assert d["body"].startswith("Hi Prof. Lee") and d["subject"] == "Re: Office hours" and d["in_reply_to"] == "<abc@umbc.edu>"
    actions = Actions(today.calendar, {"relay": "Relay"})
    draft = await actions.propose(session, agent_id="relay", kind="email.draft", title="Draft", reason="r", payload=d)
    assert draft.status == "pending" and not any("/drafts" in c for c in calls)  # nothing touches Gmail before the OK
    await actions.approve(session, draft.id)
    assert json.loads(session.get(Action, draft.id).result)["draft_id"] == "d1"
    # Sending: never automatic, even with a rule for drafts.
    await actions.approve(session, (await actions.propose(session, agent_id="relay", kind="email.draft", title="D2",
                                                          reason="r", payload=d)).id, remember=True)
    send = await actions.propose(session, agent_id="relay", kind="email.send", title="Send", reason="r",
                                 payload={**d, "draft_id": "d1"})
    assert send.status == "pending" and actions.rule_preview(session, send.id)["allowed"] is False
    with pytest.raises(ActionError):
        actions.remember(session, send.id)
    await actions.approve(session, send.id)
    assert any(c.endswith("/drafts/send") for c in calls) and session.get(EmailItem, item.id).done
    assert actions.to_json(session.get(Action, send.id))["can_undo"] is False


async def test_drafts_need_the_compose_permission(gsettings, tmp_path, session, make_router):  # noqa: F811
    today = build(gsettings, tmp_path, [], compose=False)
    await connect(today, session)
    actions = Actions(today.calendar, {"relay": "Relay"})
    a = await actions.propose(session, agent_id="relay", kind="email.draft", title="D", reason="r",
                              payload={"to": "x@y.z", "subject": "Re: hi", "body": "ok", "account": "personal"})
    a, _ = await actions.approve(session, a.id)
    assert a.status == "failed" and "Reconnect" in a.error


DDG = """<div><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FAVL_tree&amp;rut=x">AVL tree - Wikipedia</a>
<a class="result__snippet" href="#">An <b>AVL tree</b> is a self-balancing binary search tree.</a></div>
<div><a class="result__a" href="https://www.youtube.com/watch?v=1">Video</a></div>
<div><a class="result__a" href="https://cs.example.edu/avl.html">AVL notes</a><a class="result__snippet" href="#">Rotations</a></div>"""
PAGE = "<html><body><p>Menu</p><p>AVL trees rebalance after insertions using single and double rotations, keeping height O(log n).</p><p>Unrelated footer text about cookies and privacy settings here.</p></body></html>"


async def test_radix_gathers_numbered_sources():
    def handler(request):
        if "duckduckgo" in str(request.url):
            return httpx.Response(200, text=DDG)
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sources = await research.gather("how do AVL trees rebalance", client)
    assert [s["n"] for s in sources] == [1, 2] and sources[0]["url"] == "https://en.wikipedia.org/wiki/AVL_tree"
    assert all("youtube" not in s["url"] for s in sources)  # video sites skipped
    assert "rotations" in sources[0]["text"] and "cookies" not in sources[0]["text"]
    assert "[1]" in research.web_context(sources) and research.worth_searching("thanks!") is False


LITE = """<tr><td><a rel="nofollow" href="https://en.wikipedia.org/wiki/Spacing_effect" class='result-link'>Spacing effect - Wikipedia</a></td></tr>
<tr><td class='result-snippet'>The <b>spacing effect</b> is when learning is greater when studying is spread out.</td></tr>"""


async def test_radix_uses_ddg_lite_when_blocked_and_drops_off_topic_pages():
    seen = []

    def handler(request):
        url = str(request.url)
        seen.append(url)
        if "html.duckduckgo" in url:
            return httpx.Response(202, text="anomaly")  # DuckDuckGo's bot check
        if "lite.duckduckgo" in url:
            return httpx.Response(200, text=LITE)
        return httpx.Response(200, text="<p>The spacing effect: studying spread over days beats cramming the night before.</p>",
                              headers={"content-type": "text/html"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    q = "What is the spacing effect and does it help with studying?"
    sources = await research.gather(q, client)
    assert [s["url"] for s in sources] == ["https://en.wikipedia.org/wiki/Spacing_effect"]
    assert not any("wikipedia.org/w/api.php" in u for u in seen)  # the lite page was enough
    assert research.on_topic(q, "Sentence spacing", "Spaces between sentences have an effect on reading; it may help.") is False
    assert research.on_topic("how do black holes form", "Black hole", "A black hole forms when a star collapses")


async def test_unreadable_sort_is_retried_next_time(gsettings, tmp_path, session, make_router):  # noqa: F811
    today = build(gsettings, tmp_path, [])
    await connect(today, session)
    out = await relay.sort_inbox(session, router(make_router, "or go over project or go over project"), today.google)
    assert out["sorted"] == 1 and "try again" in out["errors"][0]  # only the Gmail-labelled promo was filed
    again = await relay.sort_inbox(session, router(make_router, SORT), today.google)
    assert again["sorted"] == 2
    props = relay.SORT_SCHEMA["properties"]["emails"]["items"]["properties"]
    assert all("maxLength" in props[k] for k in ("reason", "task", "due"))  # a small model can't loop inside a string


def test_app_usage_ingest_and_summary(session):
    start = datetime(2026, 9, 28, 9, tzinfo=NY)
    hours = [{"start": (start + timedelta(hours=i)).astimezone(UTC).isoformat(), "apps": [
        {"app": "Code", "seconds": 2400}, {"app": "Discord", "seconds": 600}, {"app": "Safari", "seconds": 300},
        {"app": "Finder", "seconds": 10}]} for i in range(2)]
    assert appusage.ingest(session, "Mac", hours) == 6  # under 30 s isn't stored
    assert appusage.ingest(session, "Mac", hours[:1]) == 3  # resending an hour replaces it
    day = appusage.day_summary(session, datetime(2026, 9, 28, tzinfo=NY))
    assert day["totals"] == {"focus": 4800, "distraction": 1200, "neutral": 600}
    assert day["top"][0] == {"app": "Code", "minutes": 80, "category": "focus"} and day["hours"][9]["focus"] == 40
    assert appusage.category("Visual Studio Code") == "focus" and appusage.category("Google Chrome") == "neutral"
    assert appusage.category("Steam.exe") == "distraction"
    assert "focus 1h 20m" in appusage.context_text(session, datetime(2026, 9, 28, 20, tzinfo=NY))


def test_questions_do_not_trigger_changes():
    assert not tools.wants_change("How does my workout schedule look, do I workout tomorrow?")
    assert not tools.wants_change("I was just asking, not add it to my schedule")
    assert tools.wants_change("tell Vector I can't run before 8:30") and tools.wants_change("check my email")


async def test_cardinal_hands_requests_to_teammates(gsettings, tmp_path, session, make_router):  # noqa: F811
    today = build(gsettings, tmp_path, [])
    actions = Actions(today.calendar, {"vector": "Vector"})
    plan_cardinal = json.dumps({"calls": [{"tool": "ask_teammate", "args": {"agent": "delta", "request": "add call mom to my tasks"}}]})
    plan_delta = json.dumps({"calls": [{"tool": "add_task", "args": {"title": "Call mom"}}]})
    ctx = tools.Ctx(session=session, now=datetime(2026, 9, 27, 15, tzinfo=NY), calendar=today.calendar, actions=actions,
                    agent_id="cardinal", router=router(make_router, plan_cardinal, plan_delta),
                    extras={"agents": AGENTS})
    out = await tools.plan_and_run(ctx, "Cardinal", [{"role": "user", "content": "have Delta add call mom to my tasks"}])
    assert out[0].ok and out[0].text.startswith("Delta: Added task 'Call mom'")


@pytest.fixture
def client(engine, make_router, gsettings, tmp_path):  # noqa: F811
    with TestClient(main.app) as c:
        today = build(gsettings, tmp_path, [])
        main.app.state.today = today
        main.app.state.actions = Actions(today.calendar, {a.id: a.name for a in AGENTS.values()})
        yield c


def test_inbox_and_apps_api(client, make_router, session):
    import asyncio
    asyncio.run(connect(main.app.state.today, session))
    main.app.state.router = router(make_router, SORT, "Hi Prof. Lee, Thursday at 2 works. Alex")
    r = client.post("/api/inbox/sort").json()
    assert r["sorted"] == 3 and r["items"][0]["category"] == "urgent"
    office = next(i for i in r["items"] if i["subject"] == "Office hours")
    d = client.post(f"/api/inbox/{office['id']}/draft", json={"instructions": "say yes"}).json()
    saved = client.post(f"/api/inbox/{office['id']}/save-draft", json={k: d[k] for k in ("to", "subject", "body", "thread_id", "in_reply_to", "references")})
    assert saved.status_code == 200 and saved.json()["status"] == "executed"
    reg = next(i for i in r["items"] if i["subject"].startswith("Add/drop"))
    due = client.post(f"/api/inbox/{reg['id']}/due").json()
    assert due["kind"] == "calendar.add_block" and due["payload"]["date"].endswith("-10-03")
    assert client.post(f"/api/inbox/{office['id']}/task").json()["title"] == "Answer Prof. Lee"

    assert client.post("/api/apps/ingest", json={"device": "Mac", "hours": []}).status_code == 401
    tok = client.post("/api/apps/token").json()["token"]
    ok = client.post("/api/apps/ingest", headers={"X-Cardinal-Token": tok}, json={"device": "Mac", "hours": [
        {"start": datetime.now(UTC).replace(minute=0, second=0, microsecond=0).isoformat(), "apps": [{"app": "Code", "seconds": 1200}]}]})
    assert ok.json() == {"rows": 1} and client.get("/api/apps/day").json()["totals"]["focus"] == 1200
    status = client.get("/api/agents/status").json()
    assert set(status) == set(AGENTS) and status["axiom"]["level"] == "warn"
