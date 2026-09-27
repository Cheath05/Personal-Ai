import json
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from cardinal import main, syllabus
from cardinal.calendar import Calendar, CalendarError, make_times
from cardinal.db import CalendarItem
from cardinal.sources.blackboard import Blackboard
from cardinal.sources.google import SCOPE_APP_CALENDAR, SCOPE_CALENDAR, SCOPE_GMAIL, Google
from cardinal.today import Today
from cardinal.vault import Vault

from .conftest import FakeLocal
from .test_today import ICS, google_handler, gsettings  # noqa: F401  (fixture)

NY = ZoneInfo("America/New_York")

ICLOUD = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:gym
SUMMARY:Gym
DTSTART;TZID=America/New_York:20260915T070000
DTEND;TZID=America/New_York:20260915T080000
RRULE:FREQ=WEEKLY;BYDAY=TU
END:VEVENT
END:VCALENDAR
"""


def writer_handler(calls, grant_write=True):
    base = google_handler(calls)

    def handler(request: httpx.Request):
        url = str(request.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            form = parse_qs(request.content.decode())
            scopes = f"openid email {SCOPE_CALENDAR} {SCOPE_GMAIL}" + (f" {SCOPE_APP_CALENDAR}" if grant_write else "")
            if form["grant_type"] == ["authorization_code"]:
                return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600,
                                                 "scope": scopes})
        calls.append(f"{request.method} {url}")
        if request.method == "POST" and url.endswith("/calendar/v3/calendars"):
            return httpx.Response(200, json={"id": "cardinal-cal"})
        if request.method == "POST" and "/calendars/cardinal-cal/events" in url:
            return httpx.Response(200, json={"id": f"ev{len(calls)}"})
        if request.method == "DELETE":
            return httpx.Response(204)
        return base(request)
    return handler


def make_today(gsettings, tmp_path, calls, grant_write=True):  # noqa: F811
    client = httpx.AsyncClient(transport=httpx.MockTransport(writer_handler(calls, grant_write)))

    def feeds(request):
        return httpx.Response(200, content=ICLOUD if "icloud" in str(request.url) else ICS)

    feed_client = httpx.AsyncClient(transport=httpx.MockTransport(feeds))
    vault = Vault(tmp_path / "secret.key")
    google = Google(gsettings, vault, client=client)
    bb = Blackboard(gsettings.blackboard_ics_url, client=feed_client)
    return Today(gsettings, google, bb, Calendar(gsettings, google, bb, vault, feed_client=feed_client))


async def connect(today, session):
    state = parse_qs(urlparse(today.google.auth_url("personal")).query)["state"][0]
    return await today.google.finish(session, "code", state)


def test_make_times():
    d = date(2026, 9, 28)
    s, e, all_day = make_times(d, None, None, NY)
    assert all_day and s.hour == 0 and (e - s).days == 1
    s, e, all_day = make_times(d, "14:30", "16:00", NY)
    assert not all_day and (s.hour, s.minute, e.hour) == (14, 30, 16)
    s, e, _ = make_times(d, "09:00", None, NY)
    assert (e - s).seconds == 3600
    with pytest.raises(ValueError):
        make_times(d, "25:00", None, NY)


async def test_day_view_merges_sources_and_limits_range(gsettings, tmp_path, session):  # noqa: F811
    calls = []
    today = make_today(gsettings, tmp_path, calls)
    cal = today.calendar
    await connect(today, session)
    cal.today = lambda: date(2026, 9, 27)  # freeze "today" so the allowed range doesn't drift
    d = date(2026, 9, 28)  # the mock Google's events are on this day
    await cal.add(session, title="Study CMSC 341", day=d, start="15:00", end="16:30", kind="reading")
    await cal.add_feed(session, "iCloud", "webcal://p01-caldav.icloud.com/published/2/x")
    view = await cal.day(session, d, force=True)
    sources = {e["source"] for e in view["events"]}
    assert {"google", "cardinal"} <= sources
    assert all(e["start"][:10] == "2026-09-28" or e["all_day"] for e in view["events"])
    mine = next(e for e in view["events"] if e["source"] == "cardinal")
    assert mine["deletable"] and mine["synced"] and mine["color"]
    assert view["can_sync"] is True
    with pytest.raises(CalendarError):
        await cal.day(session, date(2020, 1, 1))


async def test_calendar_link_repeats_weekly(gsettings, tmp_path, session):  # noqa: F811
    today = make_today(gsettings, tmp_path, [])
    cal = today.calendar
    await cal.add_feed(session, "iCloud", "webcal://icloud.example/cal")
    d = cal.today()
    tuesday = d + __import__("datetime").timedelta(days=(1 - d.weekday()) % 7)
    view = await cal.day(session, tuesday, force=True)
    gym = [e for e in view["events"] if e["title"] == "Gym"]
    assert len(gym) == 1 and gym[0]["source"] == "feed" and "T07:00" in gym[0]["start"]


async def test_items_mirror_to_cardinal_calendar_and_delete_there_too(gsettings, tmp_path, session):  # noqa: F811
    calls = []
    today = make_today(gsettings, tmp_path, calls)
    cal = today.calendar
    await connect(today, session)
    item, warning = await cal.add(session, title="Midterm", day=cal.today(), start="10:00", kind="exam")
    assert warning is None and item.google_event_id
    assert any(c.startswith("POST") and c.endswith("/calendars") for c in calls)  # made the Cardinal calendar once
    await cal.delete(session, item.id)
    assert any(c.startswith("DELETE") for c in calls)
    assert session.get(CalendarItem, item.id) is None


async def test_without_write_access_items_stay_local_then_sync(gsettings, tmp_path, session):  # noqa: F811
    calls = []
    today = make_today(gsettings, tmp_path, calls, grant_write=False)
    cal = today.calendar
    await connect(today, session)
    item, warning = await cal.add(session, title="Office hours", day=cal.today(), start="13:00")
    assert "Reconnect" in warning and item.google_event_id is None
    acct = today.google.account(session, "personal")
    acct.scopes += f" {SCOPE_APP_CALENDAR}"
    session.add(acct)
    session.commit()
    assert await cal.sync_unsynced(session) == 1


def test_text_helpers():
    html = "<html><style>x{}</style><table><tr><td>Sep 30</td><td>Project 1 due</td></tr></table><p>Hi</p></html>"
    assert "Sep 30 | Project 1 due" in syllabus.html_to_text(html)
    parts = syllabus.chunks("line\n" * 3000, size=1000)
    assert len(parts) > 10 and all(len(p) <= 1005 for p in parts)
    today = date(2026, 9, 27)
    assert syllabus.fix_year(date(2025, 10, 8), today) == date(2026, 10, 8)
    assert syllabus.fix_year(date(2026, 2, 3), today) == date(2027, 2, 3)
    items = syllabus.clean_items([
        {"date": "2026-10-08", "time": "9:30", "title": "Midterm  exam", "kind": "exam"},
        {"date": "2026-10-08", "title": "midterm exam", "kind": "exam"},
        {"date": "someday", "title": "x", "kind": "due"},
        {"date": "2026-09-01", "title": "Syllabus quiz", "kind": "weird"}], today)
    assert [i["title"] for i in items] == ["Syllabus quiz", "Midterm exam"]
    assert items[0]["kind"] == "event" and items[0]["past"] and items[1]["time"] == "09:30"
    assert syllabus.parse_reply('Sure! {"items": [{"date": "2026-10-01", "title": "A", "kind": "due"}]}')[0]["title"] == "A"


async def test_private_google_doc_explains_how_to_upload():
    def handler(request):
        return httpx.Response(200, text="<html>Sign in</html>", headers={"content-type": "text/html"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(syllabus.SyllabusError, match="Download → PDF"):
        await syllabus.fetch_url("https://docs.google.com/document/d/abc123/edit", client)


REPLY = json.dumps({"items": [
    {"date": "2026-10-08", "time": "", "title": "Midterm exam", "kind": "exam"},
    {"date": "2026-11-26", "time": "", "title": "No class: Thanksgiving", "kind": "no_class"}]})


@pytest.fixture
def client(engine, make_router, gsettings, tmp_path):  # noqa: F811
    with TestClient(main.app) as c:
        main.app.state.router = make_router(local={"g14": FakeLocal("g14", replies=[REPLY])})
        main.app.state.today = make_today(gsettings, tmp_path, [])
        yield c


def test_syllabus_import_proposes_then_adds_only_what_you_confirm(client):
    r = client.post("/api/syllabus", json={"course": "CMSC 341", "text": "Oct 8 Midterm exam\nNov 26 No class (Thanksgiving)\n" * 3})
    job = r.json()
    for _ in range(50):
        job = client.get(f"/api/syllabus/{job['id']}").json()
        if job["status"] != "reading":
            break
        __import__("time").sleep(0.05)
    assert job["status"] == "ready", job["detail"]
    assert [i["title"] for i in job["items"]] == ["Midterm exam", "No class: Thanksgiving"]
    r = client.post(f"/api/syllabus/{job['id']}/add", json={"items": [job["items"][0]]})
    assert r.json()["added"] == 1
    day = client.get("/api/calendar/day", params={"date": "2026-10-08"}).json()
    mine = [e for e in day["events"] if e["source"] == "cardinal"]
    assert [(e["title"], e["kind"], e["calendar"]) for e in mine] == [("Midterm exam", "exam", "CMSC 341")]
    assert client.get("/api/calendar/day", params={"date": "2026-11-26"}).json()["events"] == []  # not ticked


def test_add_and_remove_an_item_through_the_api(client):
    d = datetime.now(NY).date().isoformat()
    r = client.post("/api/calendar/items", json={"title": "Run: 3 mi easy", "date": d, "start": "17:30", "end": "18:15"})
    assert r.status_code == 200
    item_id = r.json()["item"]["id"]
    assert r.json()["warning"]  # Google not connected in this test: saved in Cardinal only
    assert client.post("/api/calendar/items", json={"title": " ", "date": d}).status_code == 400
    assert client.delete(f"/api/calendar/items/{item_id}").status_code == 200
    assert client.get("/api/calendar/day", params={"date": "2019-01-01"}).status_code == 400


def test_model_answers_in_human_formats_still_parse():
    today = date(2026, 9, 27)
    assert syllabus.parse_date("Thursday, October 8th", today) == date(2026, 10, 8)
    assert syllabus.parse_date("Jan 20", today) == date(2027, 1, 20)
    assert syllabus.parse_date("Week 5", today) is None and syllabus.parse_date("Tuesday", today) is None
    assert syllabus.parse_times("11:59 PM") == ("23:59", "")
    assert syllabus.parse_times("1:00-2:30pm") == ("13:00", "14:30")
    assert syllabus.parse_times("10:30-12:30pm") == ("10:30", "12:30")
    items = syllabus.clean_items([{"date": "Sep 30", "time": "11:59pm", "title": "Project 1 due", "kind": "due"}], today)
    assert items == [{"date": "2026-09-30", "time": "23:59", "end": "", "title": "Project 1 due", "kind": "due", "past": False}]
    line = "Final exam: Monday Dec 14, 10:30am-12:30pm, ENGR 027"
    assert syllabus.fill_time({"date": "2026-12-14", "title": "Final exam", "time": ""}, line)["time"] == "10:30-12:30"
    assert syllabus.fill_time({"date": "Dec 1", "title": "Final exam", "time": ""}, line)["time"] == ""


def test_fill_time_does_not_borrow_another_items_time():
    row = "3 | Sep 10 | Linked lists | Project 0 due Sep 12 at 11:59pm"
    assert syllabus.fill_time({"date": "2026-09-10", "title": "Linked lists", "time": ""}, row)["time"] == ""
    assert syllabus.fill_time({"date": "Sep 12", "title": "Project 0 due", "time": ""}, row)["time"] == "23:59"
    assert syllabus.fill_time({"date": "2026-09-30", "title": "HW 4 due", "time": ""},
                              "Wed Sep 30 | Vector spaces | HW 4 due 11:59 PM")["time"] == "23:59"


async def test_added_item_is_complete_after_the_google_copy(gsettings, tmp_path, session):  # noqa: F811
    today = make_today(gsettings, tmp_path, [])
    await connect(today, session)
    item, _ = await today.calendar.add(session, title="Gym", day=today.calendar.today(), start="07:00")
    assert item.model_dump()["title"] == "Gym" and item.model_dump()["google_event_id"]


async def test_views_answer_from_cache_and_refresh_in_the_background(gsettings, tmp_path, session):  # noqa: F811
    import time as _time
    calls = []
    today = make_today(gsettings, tmp_path, calls)
    cal = today.calendar
    await connect(today, session)
    cal.today = lambda: date(2026, 9, 27)
    d = date(2026, 9, 28)
    await cal.day(session, d)
    snap = await today.snapshot(session)
    n = len(calls)
    # Stale, but under 30 minutes old: the old copy comes back at once, a new one is fetched behind it.
    cal._cache[d.isoformat()] = (_time.time() - 600, cal._cache[d.isoformat()][1])
    today._cache = (_time.time() - 600, snap)
    assert await today.snapshot(session) is snap and len(calls) == n
    await cal.day(session, d)
    await today._task
    await cal._tasks[d.isoformat()]
    assert len(calls) > n and _time.time() - today._cache[0] < 5 and _time.time() - cal._cache[d.isoformat()][0] < 5
    # Your own items are never stale: added after caching, they show up without a refresh.
    await cal.add(session, title="Office hours", day=d, start="14:00", end="15:00", kind="event")
    assert "Office hours" in [e["title"] for e in (await cal.day(session, d))["events"]]
    today.warm()  # fresh: nothing to do
    assert today._task.done()
