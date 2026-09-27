import io
import json
import zipfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from cardinal import main, running
from cardinal.actions import Actions
from cardinal.db import HealthMetric, Run

from .test_calendar import connect, make_today
from .test_today import gsettings  # noqa: F401  (fixture)

NY = ZoneInfo("America/New_York")
SUN = datetime(2026, 9, 27, 9, 0, tzinfo=NY)


def test_fitness_math_matches_the_plan():
    v = running.vdot(running.FIVE_K, 30.0)
    assert round(v, 1) == 30.8
    z = running.zones(v)
    assert running.clock(z["tempo"]) == "10:05"
    assert running.clock(z["easy"][0]) == "11:48" and running.clock(z["easy"][1]) == "12:46"
    assert running.clock(z["interval_400"]) == "2:19" and running.clock(z["goal_400"]) == "1:52"
    assert 26 < running.race_time(running.vdot(running.MILE, 8.0), running.FIVE_K) < 28  # 8:00 mile ~ 27 min 5K


def test_plan_starts_next_monday_and_follows_the_weekly_shape(session):
    assert running.plan_start(session, SUN.date()) == date(2026, 9, 28)
    week1 = running.sessions(session, date(2026, 9, 28), date(2026, 10, 4), SUN.date())
    assert [(s["day"], s["kind"]) for s in week1] == [("Tue", "intervals"), ("Wed", "easy"), ("Sun", "long")]
    assert week1[0]["title"] == "Intervals 6 × 400 m" and week1[2]["miles"] == 3.0
    week6 = running.sessions(session, date(2026, 11, 2), date(2026, 11, 8), SUN.date())
    assert week6[-1]["title"] == "5K time trial"
    assert running.sessions(session, date(2027, 1, 1), date(2027, 1, 7), SUN.date()) == []  # after week 12


def test_runs_dedupe_and_easy_pace_check(session):
    start = datetime(2026, 9, 30, 17, 0, tzinfo=NY)
    r = running.add_run(session, start=start, duration_s=21 * 60, distance_m=2 * running.MILE)  # 10:30 /mi
    assert r and running.add_run(session, start=start + timedelta(minutes=1), duration_s=1300, distance_m=2 * running.MILE + 10) is None
    f = running.fitness(session)
    plan = {"kind": "easy", "miles": 2.0}
    ev = running.evaluate(plan, [running.run_json(r, NY)], f)
    assert ev["status"] == "done" and "faster than easy" in ev["notes"][0]
    assert running.evaluate(plan, [], f)["status"] == "missed"


def test_time_trial_recalculates_paces(session):
    before = running.fitness(session)
    running.add_run(session, start=SUN, duration_s=28 * 60, distance_m=running.FIVE_K, time_trial=True)
    after = running.fitness(session)
    assert after["vdot"] > before["vdot"] and after["zones"]["tempo"] < before["zones"]["tempo"]
    assert round(after["five_k_min"], 2) == 28.0


HAE = {"data": {
    "workouts": [
        {"id": "W1", "name": "Outdoor Run", "start": "2026-09-27 07:00:00 -0400", "end": "2026-09-27 07:37:00 -0400",
         "duration": 2220, "distance": {"qty": 3.05, "units": "mi"},
         "heartRate": {"avg": {"qty": 148, "units": "bpm"}, "max": {"qty": 171, "units": "bpm"}}},
        {"id": "W2", "name": "Traditional Strength Training", "start": "2026-09-26 18:00:00 -0400", "duration": 1800},
        {"id": "W3", "name": "Indoor Run", "start": "2026-09-25 18:00:00 -0400", "duration": 1500,
         "distance": {"qty": 4.0, "units": "km"}, "avgHeartRate": {"qty": 150}},
    ],
    "metrics": [
        {"name": "resting_heart_rate", "units": "count/min", "data": [{"date": "2026-09-27 00:00:00 -0400", "qty": 56}]},
        {"name": "sleep_analysis", "units": "hr", "data": [{"date": "2026-09-27 00:00:00 -0400", "totalSleep": 5.4}]},
        {"name": "vo2_max", "units": "ml/(kg·min)", "data": [{"date": "2026-09-27 00:00:00 -0400", "qty": 39.2}]},
    ]}}


def test_health_auto_export_ingest(session):
    out = running.ingest_auto_export(session, HAE, NY)
    assert out == {"runs_added": 2, "metrics": 3}
    runs = session.exec(select(Run).order_by(Run.start)).all()
    assert [round(r.distance_m) for r in runs] == [4000, round(3.05 * running.MILE)]
    assert runs[1].avg_hr == 148 and runs[1].max_hr == 171 and runs[0].avg_hr == 150
    assert running.ingest_auto_export(session, HAE, NY)["runs_added"] == 0  # sent twice: no duplicates
    assert "Only 5.4 h of sleep" in running.readiness(session, date(2026, 9, 27))[0]


EXPORT = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Record type="HKQuantityTypeIdentifierRestingHeartRate" unit="count/min" value="58" startDate="2026-09-20 08:00:00 -0400" endDate="2026-09-20 08:00:00 -0400"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="33.5" durationUnit="min" sourceName="Apple Watch" startDate="2026-09-20 07:00:00 -0400" endDate="2026-09-20 07:33:30 -0400">
  <WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning" sum="3.02" unit="mi"/>
  <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" average="152" minimum="95" maximum="176" unit="count/min"/>
 </Workout>
 <Workout workoutActivityType="HKWorkoutActivityTypeWalking" duration="40" durationUnit="min" startDate="2026-09-21 12:00:00 -0400" endDate="2026-09-21 12:40:00 -0400" totalDistance="2" totalDistanceUnit="mi"/>
</HealthData>"""


def export_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("apple_health_export/export.xml", EXPORT)
    return buf.getvalue()


def test_apple_health_export_import(session, tmp_path):
    path = tmp_path / "export.zip"
    path.write_bytes(export_zip())
    out = running.import_health_export(session, str(path), NY, since_days=100000)
    assert out == {"runs_added": 1, "metrics": 1}
    run = session.exec(select(Run)).one()
    assert run.duration_s == 2010 and run.avg_hr == 152 and run.source == "health_export"
    assert session.exec(select(HealthMetric)).one().value == 58


async def test_vector_proposes_runs_on_their_days(gsettings, tmp_path, session):  # noqa: F811
    today = make_today(gsettings, tmp_path, [])
    today.calendar.today = lambda: date(2026, 9, 27)
    await connect(today, session)
    actions = Actions(today.calendar, {"vector": "Vector"})
    out = await running.propose_runs(session, today.calendar, actions, datetime(2026, 9, 28, 6, 0, tzinfo=NY))
    pending = actions.pending(session)
    assert out["proposed"] == len(pending) == 3
    days = [json.loads(a.payload)["date"] for a in pending]
    assert days == ["2026-09-29", "2026-09-30", "2026-10-04"]
    p = json.loads(pending[0].payload)
    assert pending[0].title == "Run: Intervals 6 × 400 m" and p["item_kind"] == "event" and p["start"] >= "16:30"
    assert "runs of up to" in actions.rule_preview(session, pending[0].id)["description"]
    again = await running.propose_runs(session, today.calendar, actions, datetime(2026, 9, 28, 7, 0, tzinfo=NY))
    assert again["proposed"] == 0


@pytest.fixture
def client(engine, make_router, gsettings, tmp_path):  # noqa: F811
    with TestClient(main.app) as c:
        today = make_today(gsettings, tmp_path, [])
        main.app.state.today = today
        main.app.state.actions = Actions(today.calendar, {"vector": "Vector"})
        yield c


def test_running_api(client):
    s = client.get("/api/running").json()
    assert s["fitness"]["vdot"] == 30.8 and s["plan"]["weeks"] == 12 and not s["ingest"]["token_set"]
    r = client.post("/api/running/runs", json={"date": "2026-09-27", "start": "17:30", "miles": 3.1, "time": "31:10", "avg_hr": 150})
    assert r.status_code == 200 and r.json()["runs"][0]["pace"] == "10:03"
    assert client.post("/api/running/runs", json={"date": "2026-09-27", "start": "17:31", "miles": 3.1, "time": "31:10"}).status_code == 409
    assert client.post("/api/running/runs", json={"date": "2026-09-27", "miles": 3, "time": "abc"}).status_code == 400

    assert client.post("/api/health/ingest", json=HAE).status_code == 401
    tok = client.post("/api/health/token").json()
    assert tok["header"] == "X-Cardinal-Token" and tok["url"].endswith("/api/health/ingest")
    r = client.post("/api/health/ingest", json=HAE, headers={"X-Cardinal-Token": tok["token"]})
    assert r.status_code == 200 and r.json()["runs_added"] == 2
    assert client.post("/api/health/ingest", json=HAE, headers={"X-Cardinal-Token": "nope"}).status_code == 401

    r = client.post("/api/health/import", files={"file": ("export.zip", export_zip(), "application/zip")})
    assert r.status_code == 200
    bad = client.post("/api/health/import", files={"file": ("x.zip", b"not a zip", "application/zip")})
    assert bad.status_code == 400


def test_each_run_counts_for_one_session_only():
    planned = [{"date": "2026-09-22"}, {"date": "2026-09-23"}, {"date": "2026-09-27"}]
    runs = [{"id": 1, "date": "2026-09-22"}, {"id": 2, "date": "2026-09-23"}, {"id": 3, "date": "2026-09-26"}]
    got = running.match_runs(planned, runs)
    assert [[r["id"] for r in m] for m in got] == [[1], [2], [3]]  # Saturday's run counts for Sunday
    assert running.match_runs(planned, [{"id": 9, "date": "2026-09-22"}])[1] == []  # Tuesday's run isn't Wednesday's too


def test_schedule_lines_answer_do_i_run_tomorrow(session):
    running.plan_start(session, SUN.date())  # week 1 starts Mon 28 Sep
    lines = running.schedule_lines(session, SUN)
    assert lines[0] == "Today (Sun 27 Sep): rest day, no run planned."
    assert lines[1] == "Tomorrow (Mon 28 Sep): rest day, no run planned."
    assert "Tue 29 Sep: Intervals 6 × 400 m, hard (quality day), not on the calendar yet" in lines[2]
    assert "Wed 30 Sep: Easy 2 mi, easy" in lines[2]
