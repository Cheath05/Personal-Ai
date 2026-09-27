import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from cardinal import main, review
from cardinal.actions import Actions
from cardinal.agents import load_agents
from cardinal.db import CalendarItem, CheckIn, Experiment, Memory, Message, Task

from .conftest import FakeLocal
from .test_calendar import make_today
from .test_today import gsettings  # noqa: F401  (fixture)

NY = ZoneInfo("America/New_York")
MON = datetime(2026, 9, 28, 7, 40, tzinfo=NY)  # a Monday morning
AGENTS = load_agents()


@pytest.fixture
def svc(gsettings, tmp_path):  # noqa: F811
    today = make_today(gsettings, tmp_path, [])
    return Actions(today.calendar, {a.id: a.name for a in AGENTS.values()})


def router_with(make_router, *replies):
    from .conftest import FakeClaude
    return make_router(local={"g14": FakeLocal("g14", replies=list(replies))}, claude=FakeClaude(configured=False))


async def test_morning_checkin_sets_priorities_and_talks_in_deltas_chat(session, make_router):
    r = router_with(make_router, "Good plan. Your lecture at 10 leaves the afternoon for the paper.")
    c = await review.morning(session, r, AGENTS["delta"], now=MON, top=["Paper draft", " Chem quiz prep ", ""],
                             energy=3, note="slept late", context="Calendar today: 10:00 lecture")
    assert c.energy == 3 and "afternoon" in c.reply
    assert [t.title for t in review.tasks_for(session, "2026-09-28")] == ["Paper draft", "Chem quiz prep"]
    chat = session.exec(select(Message).where(Message.agent_id == "delta")).all()
    assert [m.role for m in chat] == ["user", "assistant"] and "Energy 3/5" in chat[0].content
    with pytest.raises(review.ReviewError):
        await review.morning(session, r, AGENTS["delta"], now=MON, top=[" "], energy=3, note=None, context="")


async def test_evening_review_prefills_and_carries_over(session, make_router):
    r = router_with(make_router, "Nice work on the paper. Tomorrow, start the quiz prep before lunch.")
    await review.morning(session, r, AGENTS["delta"], now=MON, top=["Paper draft", "Chem quiz prep"], energy=4,
                         note=None, context="")
    block = CalendarItem(title="Work on: Paper", kind="reading", start=MON.replace(hour=15), end=MON.replace(hour=16))
    session.add(block)
    session.commit()
    paper = review.tasks_for(session, "2026-09-28")[0]
    evening_now = MON.replace(hour=21, minute=40)
    c = await review.evening(session, r, AGENTS["delta"], now=evening_now, done_task_ids=[paper.id],
                             done_block_ids=[block.id], went_well="Finished the draft", didnt="Skipped quiz prep",
                             why="Club ran late", first_task="Quiz prep", carry=True, context="")
    a = json.loads(c.answers)
    assert a["tasks_done"] == 1 and a["tasks_planned"] == 2 and a["blocks_done"] == [block.id]
    tomorrow = [(t.title, t.source) for t in review.tasks_for(session, "2026-09-29")]
    assert tomorrow == [("Quiz prep", "first_task"), ("Chem quiz prep", "carried")]
    ctx = review.context_text(session, MON + timedelta(days=1))
    assert "Quiz prep (open)" in ctx and "why: Club ran late" in ctx


async def test_week_stats_and_streak(session, make_router):
    r = router_with(make_router, "ok")
    for i, energy in enumerate([2, 4, 4]):
        now = MON + timedelta(days=i)
        await review.morning(session, r, AGENTS["delta"], now=now, top=[f"t{i}"], energy=energy, note=None, context="")
        t = review.tasks_for(session, now.date().isoformat())[0]
        await review.evening(session, r, AGENTS["delta"], now=now.replace(hour=22), done_task_ids=[t.id] if i else [],
                             done_block_ids=[], went_well="", didnt="", why="", first_task=None, carry=False, context="")
    s = review.week_stats(session, date(2026, 9, 28), MON + timedelta(days=2, hours=14))
    assert (s["mornings"], s["evenings"], s["priorities_done"], s["priorities_planned"]) == (3, 3, 2, 3)
    assert s["completion"] == 0.67 and s["energy_avg"] == 3.3 and s["streak"] == 3
    assert [e["value"] for e in s["energy"]][:4] == [2, 4, 4, None]


ROLLUP = json.dumps({"summary": "A steady week.", "wins": ["Draft done"], "blockers": ["Late club nights"],
                     "experiments": ["Quiz prep before lunch", "Phone away after 22:00", "Plan tomorrow at 21:30"],
                     "focus": ["Midterm review", "Paper final"]})


async def test_rollup_proposes_a_plan_you_authorize(session, make_router, svc):
    r = router_with(make_router, ROLLUP)
    sunday = datetime(2026, 10, 4, 18, 5, tzinfo=NY)
    assert review.rollup_due(sunday, "18:00", None) and not review.rollup_due(sunday, "18:00", "2026-09-28")
    assert not review.rollup_due(MON, "18:00", None)
    roll = await review.write_rollup(session, r, AGENTS["delta"], svc, now=sunday, due_text="Midterm Oct 8")
    assert roll.week_start == "2026-09-28" and json.loads(roll.focus) == ["Midterm review", "Paper final"]
    assert review.rollup_json(roll)["summary"] == "A steady week."
    plan = svc.pending(session)[0]
    assert plan.kind == "plan.set_week" and plan.id == roll.plan_action_id
    assert session.exec(select(Experiment)).all() == []  # nothing set until you authorize
    await svc.approve(session, plan.id)
    exps = session.exec(select(Experiment)).all()
    assert [e.week_start for e in exps] == ["2026-10-05"] * 3
    await svc.undo(session, plan.id)
    assert session.exec(select(Experiment)).all() == []


async def test_sigma_proposes_patterns_that_wait_for_you(session, make_router, svc):
    # Mondays low, other days high: an arithmetic pattern Sigma can back with numbers.
    for i in range(8):
        d = date(2026, 9, 7) + timedelta(days=i * 2)
        energy = 1 if d.weekday() == 0 else 4
        session.add(CheckIn(day=d.isoformat(), kind="morning", energy=energy,
                            ts=datetime.combine(d, datetime.min.time(), NY).replace(hour=8, minute=20)))
    session.add(CheckIn(day="2026-09-14", kind="morning", energy=1, ts=datetime(2026, 9, 14, 8, 10, tzinfo=NY)))
    session.commit()
    for i in range(3):
        session.add(CheckIn(day=f"2026-09-2{i + 2}", kind="evening",
                            answers=json.dumps({"didnt": "No quiz prep", "why": "Late club night"})))
    session.commit()
    llm = json.dumps({"patterns": [
        {"text": "Late club nights push study to the next day.", "evidence": "2026-09-22, 2026-09-23, 2026-09-24"},
        {"text": "Loves mornings.", "evidence": "a feeling"}]})  # no dates: must be dropped
    r = router_with(make_router, llm)
    n = await review.sigma_nightly(session, r, AGENTS["sigma"], svc, datetime(2026, 9, 27, 2, 5, tzinfo=NY))
    titles = [a.title for a in svc.pending(session)]
    assert n == len(titles) and any("lower on Mondays" in t for t in titles)
    assert any("Late club nights" in t for t in titles) and not any("Loves mornings" in t for t in titles)
    assert session.exec(select(Memory)).all() == []  # nothing saved until you say so
    first = svc.pending(session)[0]
    await svc.approve(session, first.id)
    assert review.memory_text(session).startswith("- ")
    again = await review.sigma_nightly(session, r, AGENTS["sigma"], svc, datetime(2026, 9, 28, 2, 5, tzinfo=NY))
    assert again == 0  # never proposes the same pattern twice


def test_core_memory_reaches_agents_but_not_radix():
    mem = "- Best focus 9-11 AM."
    assert "Best focus 9-11 AM" in AGENTS["vector"].system_prompt(memory=mem)
    assert "Best focus" not in AGENTS["radix"].system_prompt(memory=mem)


@pytest.fixture
def client(engine, make_router, gsettings, tmp_path):  # noqa: F811
    with TestClient(main.app) as c:
        today = make_today(gsettings, tmp_path, [])
        main.app.state.today = today
        main.app.state.actions = Actions(today.calendar, {a.id: a.name for a in AGENTS.values()})
        main.app.state.router = router_with(make_router, "Sounds good.")
        yield c


def test_review_api_flow(client):
    s = client.get("/api/review").json()
    assert s["morning"]["done"] is False and s["tasks"] == []
    r = client.post("/api/review/morning", json={"top": ["Paper draft", "Gym"], "energy": 4})
    assert r.status_code == 200, r.text
    assert r.json().get("reply") == "Sounds good.", r.json()
    s = client.get("/api/review").json()
    assert s["morning"]["done"] and [t["title"] for t in s["tasks"]] == ["Paper draft", "Gym"]
    tid = s["tasks"][0]["id"]
    assert client.post(f"/api/review/tasks/{tid}", json={"status": "done"}).json()["status"] == "done"
    r = client.post("/api/review/evening", json={"done_task_ids": [tid], "went_well": "Draft", "first_task": "Edit"})
    assert r.status_code == 200 and client.get("/api/review").json()["evening"]["done"]
    assert client.post("/api/review/morning", json={"top": [], "energy": 9}).status_code == 422

    mem = client.post("/api/memory", json={"text": "Prefers studying in the library"}).json()
    assert mem[0]["source"] == "you"
    assert client.patch(f"/api/memory/{mem[0]['id']}", json={"text": "Prefers the library, 3rd floor"}).json()[0]["text"].endswith("3rd floor")
    assert client.delete(f"/api/memory/{mem[0]['id']}").json() == []
