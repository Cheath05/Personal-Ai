import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from cardinal import main, planner
from cardinal.actions import ActionError, Actions
from cardinal.db import Action, CalendarItem, TrustRule, utcnow

from .test_calendar import connect, make_today
from .test_today import gsettings  # noqa: F401  (fixture)

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 28, 7, 30, tzinfo=NY)
NAMES = {"axiom": "Axiom", "relay": "Relay"}


def block(day="2026-09-29", start="15:00", end="16:30", title="Work on: Project 1"):
    return {"date": day, "start": start, "end": end, "title": title, "window": ["08:00", "22:00"]}


@pytest.fixture
def svc(gsettings, tmp_path, session):  # noqa: F811
    today = make_today(gsettings, tmp_path, [])
    today.calendar.today = lambda: date(2026, 9, 27)
    return Actions(today.calendar, NAMES)


def test_free_slot():
    fs = planner.free_slot
    assert fs([], 60, 480, 1320, (840, 1260)) == (840, 900)  # empty day: 14:00 (afternoon first)
    assert fs([(840, 960)], 60, 480, 1320, (840, 1260)) == (975, 1035)  # after a 14:00-16:00 class, +10 min buffer
    assert fs([(840, 1260)], 90, 480, 1320, (840, 1260)) == (480, 570)  # afternoon full: morning
    assert fs([(480, 1320)], 30, 480, 1320) is None


def test_grouping_titles_and_duplicates():
    assert planner.clean_title("6. Submit: Biology Module Full Project") == "Biology Module Full Project"
    assert planner.classify("Midterm Exam 1") == "exam" and planner.classify("6. Complete: Chemistry Quiz") == "quiz"
    assert planner.classify("Ethical Analysis 2 Final Paper") == "work" and planner.classify("Final") == "exam"
    wed = datetime(2026, 9, 30, 23, 59, tzinfo=NY)
    items = [planner.Due(t, wed, planner.classify(t)) for t in ("Homework 2:2", "Homework 2:3", "Unit 2a dialogue", "Chemistry Quiz")]
    plans = planner.make_plans(items)
    work = next(p for p in plans if p.key.startswith("study:work"))
    assert len(work.items) == 3 and work.minutes == 100  # one block for everything due that day
    assert any(p.key.startswith("study:quiz") and p.minutes == 45 for p in plans)
    dup = planner.dedupe([planner.Due("Project 1 due", wed, "work"), planner.Due("Project 1 [CMSC 341]", wed, "work")])
    assert len(dup) == 1
    hw = planner.dedupe([planner.Due("Homework 2:2", wed, "work"), planner.Due("Homework 2:3", wed, "work")])
    assert len(hw) == 2


async def test_propose_approve_undo(svc, session):
    a = await svc.propose(session, agent_id="axiom", kind="calendar.add_block", title="Work on: Project 1",
                          reason="Due Wed.", payload=block())
    assert a.status == "pending" and session.exec(__import__("sqlmodel").select(CalendarItem)).all() == []
    ghost = svc.proposed_events(session, datetime(2026, 9, 29, tzinfo=NY), datetime(2026, 9, 30, tzinfo=NY))
    assert ghost[0]["source"] == "proposed" and ghost[0]["action_id"] == a.id
    a, suggestion = await svc.approve(session, a.id)
    assert a.status == "executed" and suggestion is None
    item_id = json.loads(a.result)["item_id"]
    assert session.get(CalendarItem, item_id).title == "Work on: Project 1"
    with pytest.raises(ActionError):
        await svc.approve(session, a.id)  # can't run twice
    await svc.undo(session, a.id)
    assert session.get(CalendarItem, item_id) is None and session.get(Action, a.id).status == "undone"


async def test_deny_leaves_calendar_alone(svc, session):
    a = await svc.propose(session, agent_id="axiom", kind="calendar.add_block", title="x", reason="y", payload=block())
    assert svc.deny(session, a.id).status == "denied"
    assert session.exec(__import__("sqlmodel").select(CalendarItem)).all() == []


async def test_trust_rule_runs_matching_actions_only(svc, session):
    first = await svc.propose(session, agent_id="axiom", kind="calendar.add_block", title="a", reason="r", payload=block())
    preview = svc.rule_preview(session, first.id)
    assert preview["allowed"] and "up to 2 h" in preview["description"] and "08:00 and 22:00" in preview["description"]
    await svc.approve(session, first.id, remember=True)
    rule = svc.rules(session)[0]

    auto = await svc.propose(session, agent_id="axiom", kind="calendar.add_block", title="b", reason="r",
                             payload=block(day="2026-09-30", start="18:00", end="19:30"))
    assert auto.status == "executed" and auto.rule_id == rule.id
    assert session.get(TrustRule, rule.id).uses == 1 and svc.recent_auto(session)[0].id == auto.id

    too_long = await svc.propose(session, agent_id="axiom", kind="calendar.add_block", title="c", reason="r",
                                 payload=block(day="2026-10-01", start="09:00", end="12:00"))
    too_late = await svc.propose(session, agent_id="axiom", kind="calendar.add_block", title="d", reason="r",
                                 payload=block(day="2026-10-01", start="21:30", end="22:30"))
    other_agent = await svc.propose(session, agent_id="relay", kind="calendar.add_block", title="e", reason="r",
                                    payload=block(day="2026-10-02"))
    assert {too_long.status, too_late.status, other_agent.status} == {"pending"}

    svc.revoke(session, rule.id)
    again = await svc.propose(session, agent_id="axiom", kind="calendar.add_block", title="f", reason="r",
                              payload=block(day="2026-10-03"))
    assert again.status == "pending"


async def test_unused_rules_expire(svc, session):
    first = await svc.propose(session, agent_id="axiom", kind="calendar.add_block", title="a", reason="r", payload=block())
    await svc.approve(session, first.id, remember=True)
    rule = svc.rules(session)[0]
    rule.created_at = utcnow() - timedelta(days=61)
    session.add(rule)
    session.commit()
    later = await svc.propose(session, agent_id="axiom", kind="calendar.add_block", title="b", reason="r",
                              payload=block(day="2026-09-30"))
    assert later.status == "pending" and svc.rules(session) == []


def test_always_ask_kinds_cannot_become_rules(svc, session):
    a = Action(agent_id="relay", kind="email.send", title="Reply to Prof. Lee", reason="r", payload="{}")
    session.add(a)
    session.commit()
    assert svc.rule_preview(session, a.id)["allowed"] is False
    with pytest.raises(ActionError, match="can't be made automatic"):
        svc.remember(session, a.id)


async def test_sigma_suggests_a_rule_after_three_approvals(svc, session):
    suggestion = None
    for i in range(3):
        a = await svc.propose(session, agent_id="axiom", kind="calendar.add_block", title=f"t{i}", reason="r",
                              payload=block(day=f"2026-10-0{1 + i}"))
        _, suggestion = await svc.approve(session, a.id)
    assert suggestion and suggestion["from"] == "Sigma" and "3 times" in suggestion["text"]


async def test_pending_blocks_expire_once_their_time_passes(svc, session):
    a = await svc.propose(session, agent_id="axiom", kind="calendar.add_block", title="x", reason="r",
                          payload=block(day="2020-01-01"))
    assert svc.pending(session) == [] and session.get(Action, a.id).status == "expired"


async def test_planner_proposes_once_around_your_events(svc, session, gsettings, tmp_path):  # noqa: F811
    cal = svc.calendar
    await connect(cal, session)  # Calendar has .google, which is all connect() needs
    session.add(CalendarItem(title="Midterm exam", kind="exam", course="CMSC 341",
                             start=datetime(2026, 10, 2, 10, 0, tzinfo=NY), end=datetime(2026, 10, 2, 11, 15, tzinfo=NY)))
    session.commit()
    result = await planner.plan_study(session, cal, svc, now=NOW)
    pending = svc.pending(session)
    titles = sorted(a.title for a in pending)
    assert result["proposed"] == len(pending) >= 3, result
    assert any(t.startswith("Work on: Project 1") for t in titles)  # Blackboard, due tonight
    assert any(t.startswith("Prep: Reading quiz") for t in titles)
    assert sum(t.startswith("Review: Midterm exam") for t in titles) == 2
    project = next(a for a in pending if a.title.startswith("Work on: Project 1"))
    p = json.loads(project.payload)
    assert p["date"] == "2026-09-28" and p["start"] >= "14:00"  # after the 10:00 lecture, in the afternoon
    by_day: dict[str, int] = {}
    for a in pending:
        by_day[json.loads(a.payload)["date"]] = by_day.get(json.loads(a.payload)["date"], 0) + 1
    assert max(by_day.values()) <= planner.DAY_MAX_BLOCKS
    again = await planner.plan_study(session, cal, svc, now=NOW)
    assert again["proposed"] == 0  # no nagging with the same suggestions


@pytest.fixture
def client(engine, make_router, gsettings, tmp_path):  # noqa: F811
    with TestClient(main.app) as c:
        today = make_today(gsettings, tmp_path, [])
        today.calendar.today = lambda: date(2026, 9, 27)
        main.app.state.today = today
        main.app.state.actions = Actions(today.calendar, NAMES)
        yield c


def test_approval_flow_over_the_api(client):
    svc = main.app.state.actions
    import asyncio
    from sqlmodel import Session

    from cardinal.db import get_engine
    with Session(get_engine()) as s:
        a = asyncio.run(svc.propose(s, agent_id="axiom", kind="calendar.add_block", title="Work on: Project 1",
                                    reason="Due Wed.", payload=block()))
        aid = a.id
    body = client.get("/api/actions").json()
    assert [x["id"] for x in body["pending"]] == [aid]
    card = body["pending"][0]
    assert card["agent"] == "Axiom" and card["preview"]["after"].startswith("Tue 29 Sep · 15:00–16:30") and card["undoable"]
    assert client.get(f"/api/actions/{aid}/rule-preview").json()["allowed"]
    r = client.post(f"/api/actions/{aid}/approve", json={"remember": True}).json()
    assert r["action"]["status"] == "executed" and r["action"]["can_undo"]
    assert len(client.get("/api/rules").json()) == 1
    day = client.get("/api/calendar/day", params={"date": "2026-09-29"}).json()
    assert any(e["title"] == "Work on: Project 1" and e["source"] == "cardinal" for e in day["events"])
    assert client.post(f"/api/actions/{aid}/undo").json()["status"] == "undone"
    assert client.get("/api/actions/log").json()[0]["status"] == "undone"
    rule_id = client.get("/api/rules").json()[0]["id"]
    assert client.delete(f"/api/rules/{rule_id}").json() == []
