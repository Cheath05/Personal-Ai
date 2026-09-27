import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from cardinal import main, prefs, tools
from cardinal.actions import Actions
from cardinal.brains import BrainReply
from cardinal.db import CalendarItem, Memory, Task

from .conftest import FakeClaude, FakeLocal
from .test_calendar import connect, make_today
from .test_today import gsettings  # noqa: F401  (fixture)

NY = ZoneInfo("America/New_York")
SUN = datetime(2026, 9, 27, 15, 0, tzinfo=NY)


def plan(*calls) -> str:
    return json.dumps({"calls": [{"tool": t, "args": a} for t, a in calls]})


def test_dates_and_times_people_say():
    assert tools.resolve_date("Thursday", SUN) == date(2026, 10, 1)
    assert tools.resolve_date("tomorrow", SUN) == date(2026, 9, 28)
    assert tools.resolve_date("sun", SUN) == date(2026, 9, 27)
    assert tools.resolve_date("2026-10-08", SUN) == date(2026, 10, 8)
    assert [prefs.clean_time(x) for x in ("8:30", "8:30 am", "5pm", "830", "17.45", "12am")] == \
        ["08:30", "08:30", "17:00", "08:30", "17:45", "00:00"]
    with pytest.raises(ValueError):
        prefs.clean_time("soon")


@pytest.fixture
def world(gsettings, tmp_path, session, make_router):  # noqa: F811
    today = make_today(gsettings, tmp_path, [])
    today.calendar.today = lambda: date(2026, 9, 27)
    actions = Actions(today.calendar, {"vector": "Vector", "axiom": "Axiom", "delta": "Delta", "sigma": "Sigma"})

    def ctx(agent, *replies, recent=()):
        router = make_router(local={"g14": FakeLocal("g14", replies=list(replies))}, claude=FakeClaude(configured=False))
        return tools.Ctx(session=session, now=SUN, calendar=today.calendar, actions=actions, agent_id=agent, router=router,
                         extras={"recent_actions": list(recent)})
    return today, actions, ctx


async def test_vector_fits_runs_to_new_hours_instead_of_just_saying_so(world, session):
    today, actions, ctx = world
    await connect(today, session)
    run, _ = await today.calendar.add(session, title="Run: Intervals 6 × 400 m", day=date(2026, 9, 29), start="06:00",
                                      end="07:00", kind="event")
    out = await tools.plan_and_run(ctx("vector", plan(("set_run_hours", {"earliest": "8:30am"}))), "Vector",
                                   [{"role": "user", "content": "I can't run before 8:30"}])
    assert prefs.get(session, "run_window") == "08:30-21:00"
    assert out[0].status == "proposed"
    move = out[0].all_actions[0]
    p = json.loads(move.payload)
    assert move.kind == "calendar.move_item" and p["item_id"] == run.id and p["date"] == "2026-09-29"
    assert p["start"] >= "16:30"  # evenings still preferred inside the new hours
    await actions.approve(session, move.id)
    moved = session.get(CalendarItem, run.id)
    assert f"{moved.start.astimezone(NY):%H:%M}" == p["start"]


async def test_school_day_hours_leave_sunday_alone(world, session):
    today, actions, ctx = world
    await tools.plan_and_run(ctx("vector", plan(("set_run_hours", {"earliest": "08:30", "latest": "21:00", "days": "weekdays"}))),
                             "Vector", [{"role": "user", "content": "weekdays I can't start before 8:30"}])
    from cardinal.running import run_windows
    assert run_windows(session, date(2026, 9, 29))[1] == ("08:30", "21:00")
    assert run_windows(session, date(2026, 10, 4))[1] == ("06:00", "21:00")


async def test_axiom_adds_then_moves_that(world, session):
    _, actions, ctx = world
    first = await tools.plan_and_run(ctx("axiom", plan(("add_to_calendar", {"date": "Thursday", "start": "15:00",
                                                                            "minutes": "90", "kind": "study", "course": "CMSC 341 ”,"}))),
                                     "Axiom", [{"role": "user", "content": "study session Thursday 3pm for 1.5h"}])
    prop = first[0].action
    p = json.loads(prop.payload)
    assert (p["title"], p["date"], p["start"], p["end"], p["item_kind"]) == ("Study CMSC 341", "2026-10-01", "15:00", "16:30", "reading")
    # "move that to 5pm": no id from the model, so "that" is the proposal from this conversation
    second = await tools.plan_and_run(ctx("axiom", plan(("move_calendar_item", {"start": "17:00"})), recent=[prop.id]),
                                      "Axiom", [{"role": "user", "content": "move that to 5pm"}])
    assert second[0].ok and json.loads(session.get(type(prop), prop.id).payload)["start"] == "17:00"
    assert json.loads(session.get(type(prop), prop.id).payload)["end"] == "18:30"


async def test_other_agents_tools_and_aliases(world, session):
    _, actions, ctx = world
    await tools.plan_and_run(ctx("delta", plan(("add_task", {"task": "Email Professor Lee"}))), "Delta", [])
    assert session.exec(select(Task)).one().title == "Email Professor Lee"
    await tools.plan_and_run(ctx("sigma", plan(("remember", {"fact": "Studies best in the library"}))), "Sigma", [])
    assert session.exec(select(Memory)).one().text == "Studies best in the library"
    out = await tools.plan_and_run(ctx("ordinal", plan(("set_briefing_time", {"time": "6:30"}))), "Ordinal", [])
    assert out[0].ok and prefs.get(session, "briefing_time") == "06:30"
    # An agent can't use another agent's tools, even if the model names one.
    out = await tools.plan_and_run(ctx("relay", plan(("remember", {"text": "x"}))), "Relay", [])
    assert out == [] and len(session.exec(select(Memory)).all()) == 1


async def test_removing_always_asks_and_undo_brings_it_back(world, session):
    today, actions, ctx = world
    item, _ = await today.calendar.add(session, title="Study CMSC 341", day=date(2026, 10, 1), start="15:00", end="16:00",
                                       kind="reading")
    out = await tools.plan_and_run(ctx("axiom", plan(("remove_calendar_item", {"id": f"item {item.id}"}))), "Axiom", [])
    act = out[0].action
    assert act.kind == "calendar.remove_item" and act.status == "pending"
    assert actions.rule_preview(session, act.id)["allowed"] is False
    await actions.approve(session, act.id)
    assert session.get(CalendarItem, item.id) is None
    await actions.undo(session, act.id)
    back = session.exec(select(CalendarItem).where(CalendarItem.title == "Study CMSC 341")).one()
    assert f"{back.start.astimezone(NY):%H:%M}" == "15:00"


def test_claim_check_blocks_made_up_changes():
    reply = lambda t: BrainReply(text=t, provider="local", brain="g14", model="m")  # noqa: E731
    check = tools.claim_check([])
    assert check(reply("I've moved your run to 8:30.")) == "claimed a change that didn't happen"
    assert check(reply("Easy runs should feel slow.")) is None
    done = tools.claim_check([tools.Result(True, "Set.")])
    assert done(reply("I've set it.")) is None


@pytest.fixture
def client(engine, make_router, gsettings, tmp_path):  # noqa: F811
    with TestClient(main.app) as c:
        today = make_today(gsettings, tmp_path, [])
        main.app.state.today = today
        main.app.state.actions = Actions(today.calendar, {"vector": "Vector", "axiom": "Axiom"})
        yield c


def test_chat_makes_the_change_and_shows_the_card(client, make_router):
    main.app.state.router = make_router(local={"g14": FakeLocal("g14", replies=[
        plan(("add_to_calendar", {"title": "Office hours", "date": "tomorrow", "start": "13:00"})),
        "It's ready for you to approve below."])}, claude=FakeClaude(configured=False))
    r = client.post("/api/chat", json={"agent_id": "axiom", "message": "Put office hours tomorrow at 1pm"}).json()
    assert r["changes"][0]["status"] == "proposed"
    card = r["message"]["actions"][0]
    assert card["status"] == "pending" and card["title"] == "Office hours"
    history = client.get("/api/agents/axiom/messages").json()
    assert history[-1]["actions"][0]["id"] == card["id"] and history[-1]["changes"][0]["status"] == "proposed"
    client.post(f"/api/actions/{card['id']}/approve", json={})
    assert client.get("/api/agents/axiom/messages").json()[-1]["actions"][0]["status"] == "executed"


def test_chat_flags_a_claim_when_nothing_changed(client, make_router):
    main.app.state.router = make_router(local={"g14": FakeLocal("g14", replies=[
        plan(), "I've moved it to 5pm.", "I've moved it to 5pm."])}, claude=FakeClaude(configured=False))
    r = client.post("/api/chat", json={"agent_id": "axiom", "message": "move it to 5"}).json()
    assert r["changes"] == [] and r["message"]["content"].endswith("(To be clear: nothing was changed.)")
