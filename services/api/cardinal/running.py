"""Vector: the 12-week mile and 5K plan, your runs, and fitness from the Apple Watch.

Everything here is arithmetic, not a language model: VDOT (Jack Daniels' running fitness score) from your latest
time trials, training paces from VDOT, a fixed 12-week plan (PLAN §8.2), and checks of each run against the day's
session. Runs arrive three ways: typed in, an Apple Health export file, or Health Auto Export posting to the hub.
"""

import hashlib
import math
import re
import secrets
import zipfile
from datetime import UTC, date, datetime, timedelta
from statistics import median
from xml.etree.ElementTree import iterparse
from zoneinfo import ZoneInfo

from sqlmodel import Session, col, select

from .db import HealthMetric, Pref, Run, utcnow

MILE = 1609.344
FIVE_K = 5000.0
START_MILE_MIN, START_5K_MIN = 8.0, 30.0  # your starting point
TARGET_MILE_MIN, TARGET_5K_MIN = 7.5, 27.5
DAY_INDEX = {"Tue": 1, "Wed": 2, "Sun": 6}


# ---------- Fitness math (Daniels & Gilbert) ----------

def vdot(dist_m: float, minutes: float) -> float:
    v = dist_m / minutes  # m/min
    vo2 = -4.60 + 0.182258 * v + 0.000104 * v * v
    pct = 0.8 + 0.1894393 * math.exp(-0.012778 * minutes) + 0.2989558 * math.exp(-0.1932605 * minutes)
    return vo2 / pct


def pace_at(v: float, fraction: float) -> float:
    """Minutes per mile when running at `fraction` of VDOT."""
    target = fraction * v
    speed = (-0.182258 + math.sqrt(0.182258 ** 2 + 4 * 0.000104 * (4.60 + target))) / (2 * 0.000104)
    return MILE / speed


def race_time(v: float, dist_m: float) -> float:
    lo, hi = 1.0, 400.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if vdot(dist_m, mid) > v:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def clock(minutes: float | None) -> str:
    if minutes is None:
        return "–"
    total = round(minutes * 60)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def zones(v: float) -> dict:
    four = 400 / MILE
    return {"easy": [pace_at(v, 0.72), pace_at(v, 0.65)], "tempo": pace_at(v, 0.88),
            "interval_400": pace_at(v, 0.975) * four, "goal_400": TARGET_MILE_MIN * four}


# ---------- The plan ----------

# (Sunday, Tuesday, Wednesday) for weeks 1-12. Endurance is the limiter, so the long run and tempo build most.
WEEKS = [
    (("long", 3.0), ("intervals", 6), ("easy", 2.0)),
    (("long", 3.25), ("tempo", 15), ("easy", 2.0)),
    (("long", 3.5), ("intervals", 6), ("easy", 2.25)),
    (("long", 3.0), ("mile_tt", 1), ("easy", 2.0)),
    (("long", 3.75), ("intervals", 7), ("easy", 2.5)),
    (("5k_tt", 1), ("tempo", 18), ("easy", 2.5)),
    (("long_tempo", 4.25), ("intervals", 8), ("easy", 2.75)),
    (("long", 3.5), ("mile_tt", 1), ("easy", 2.25)),
    (("long_tempo", 4.5), ("goal_intervals", 8), ("easy", 3.0)),
    (("long_tempo", 5.0), ("tempo", 25), ("easy", 3.0)),
    (("long_tempo", 5.5), ("goal_intervals", 8), ("easy", 3.0)),
    (("5k_tt", 1), ("tempo", 12), ("easy", 2.0)),
]


def describe(kind: str, n: float, z: dict, est: dict) -> dict:
    easy = f"{clock(z['easy'][0])}–{clock(z['easy'][1])} /mi"
    if kind in ("long", "long_tempo"):
        extra = f", last mile at tempo ({clock(z['tempo'])} /mi)" if kind == "long_tempo" else ", plus 4 × 20 s strides"
        return {"title": f"Long easy {n:g} mi", "detail": f"{n:g} mi at {easy}{extra}", "miles": n, "target": easy,
                "kind": "long"}
    if kind == "easy":
        return {"title": f"Easy {n:g} mi", "detail": f"{n:g} mi very easy at {easy}, plus 4 × 20 s strides",
                "miles": n, "target": easy, "kind": "easy"}
    if kind in ("intervals", "goal_intervals"):
        rep = z["goal_400"] if kind == "goal_intervals" else z["interval_400"]
        return {"title": f"Intervals {int(n)} × 400 m", "kind": "intervals", "target": f"{clock(rep)} per 400 m",
                "detail": f"10 min warm-up, {int(n)} × 400 m at {clock(rep)} with 90 s jog, 10 min cool-down",
                "miles": round(1.7 + 0.25 * n + 0.1 * (n - 1), 1)}
    if kind == "tempo":
        detail = f"1 mi easy, {int(n)} min at {clock(z['tempo'])} /mi, 1 mi easy"
        if n >= 24:
            detail = f"1 mi easy, 2 × 12 min at {clock(z['tempo'])} /mi (3 min jog), 1 mi easy"
        return {"title": f"Tempo {int(n)} min", "kind": "tempo", "target": f"{clock(z['tempo'])} /mi",
                "detail": detail, "miles": round(2 + n / z["tempo"], 1)}
    if kind == "mile_tt":
        return {"title": "Mile time trial", "kind": "time_trial", "target": f"about {clock(est['mile'])}",
                "detail": f"1 mi warm-up with strides, 1 mile all-out (aim for {clock(est['mile'])}), 1 mi cool-down",
                "miles": 3.0, "distance_m": MILE}
    return {"title": "5K time trial", "kind": "time_trial", "target": f"about {clock(est['5k'])}",
            "detail": f"1 mi warm-up, 5K at an even hard effort (aim for {clock(est['5k'])}), easy cool-down",
            "miles": 4.6, "distance_m": FIVE_K}


def get_pref(session: Session, key: str) -> str | None:
    p = session.get(Pref, key)
    return p.value if p else None


def set_pref(session: Session, key: str, value: str) -> None:
    p = session.get(Pref, key) or Pref(key=key, value=value)
    p.value, p.updated_at = value, utcnow()
    session.add(p)
    session.commit()


def plan_start(session: Session, today: date) -> date:
    """Week 1 starts on the first Monday after the plan was first opened."""
    value = get_pref(session, "run_plan_start")
    if value:
        return date.fromisoformat(value)
    start = today if today.weekday() == 0 else today + timedelta(days=7 - today.weekday())
    set_pref(session, "run_plan_start", start.isoformat())
    return start


def fitness(session: Session) -> dict:
    five = float(get_pref(session, "current_5k_min") or START_5K_MIN)
    mile = float(get_pref(session, "current_mile_min") or START_MILE_MIN)
    v = vdot(FIVE_K, five)
    return {"vdot": round(v, 1), "mile_vdot": round(vdot(MILE, mile), 1), "five_k_min": five, "mile_min": mile,
            "zones": zones(v), "est": {"5k": race_time(v, FIVE_K), "mile": min(mile, race_time(vdot(MILE, mile), MILE))},
            "targets": {"5k": TARGET_5K_MIN, "mile": TARGET_MILE_MIN}}


def sessions(session: Session, start: date, end: date, today: date) -> list[dict]:
    """Planned runs with dates in [start, end]."""
    first = plan_start(session, today)
    f = fitness(session)
    out = []
    d = start
    while d <= end:
        week = (d - first).days // 7 + 1
        day = {1: "Tue", 2: "Wed", 6: "Sun"}.get(d.weekday())
        if 1 <= week <= len(WEEKS) and day:
            kind, n = WEEKS[week - 1][{"Sun": 0, "Tue": 1, "Wed": 2}[day]]
            out.append({"date": d.isoformat(), "day": day, "week": week, **describe(kind, n, f["zones"], f["est"])})
        d += timedelta(days=1)
    return out


# ---------- Runs ----------

def pace(run: Run) -> float | None:
    return (run.duration_s / 60) / (run.distance_m / MILE) if run.distance_m > 0 else None


def run_json(r: Run, tz: ZoneInfo) -> dict:
    p = pace(r)
    return {"id": r.id, "start": r.start.astimezone(tz).isoformat(), "date": r.start.astimezone(tz).date().isoformat(),
            "miles": round(r.distance_m / MILE, 2), "duration": clock(r.duration_s / 60), "pace": clock(p) if p else "–",
            "pace_min": p, "avg_hr": round(r.avg_hr) if r.avg_hr else None, "max_hr": round(r.max_hr) if r.max_hr else None,
            "source": r.source, "time_trial": r.time_trial, "notes": r.notes, "name": r.name}


def _dup(session: Session, start: datetime, distance_m: float, external_id: str | None) -> bool:
    if external_id and session.exec(select(Run).where(Run.external_id == external_id)).first():
        return True
    near = session.exec(select(Run).where(Run.start >= start - timedelta(minutes=3),
                                          Run.start <= start + timedelta(minutes=3))).all()
    return any(abs(r.distance_m - distance_m) <= max(80, 0.05 * distance_m) for r in near)


def add_run(session: Session, *, start: datetime, duration_s: int, distance_m: float, avg_hr=None, max_hr=None,
            source="manual", external_id=None, name=None, time_trial=False, notes=None) -> Run | None:
    if duration_s <= 0 or distance_m < 200 or _dup(session, start, distance_m, external_id):
        return None
    r = Run(start=start, duration_s=int(duration_s), distance_m=float(distance_m), avg_hr=avg_hr, max_hr=max_hr,
            source=source, external_id=external_id, name=name, time_trial=time_trial, notes=notes)
    session.add(r)
    session.commit()
    session.refresh(r)
    if time_trial:
        record_time_trial(session, r)
    return r


def record_time_trial(session: Session, r: Run) -> str | None:
    """A mile or 5K effort resets your current race time, and every pace with it."""
    for dist, key in ((MILE, "current_mile_min"), (FIVE_K, "current_5k_min")):
        if 0.9 * dist <= r.distance_m <= 1.15 * dist:
            minutes = (r.duration_s / 60) * (dist / r.distance_m) ** 1.06  # Riegel, to the exact distance
            set_pref(session, key, f"{minutes:.3f}")
            return key
    return None


def match_runs(planned: list[dict], runs: list[dict]) -> list[list[dict]]:
    """Give each run to at most one session: same day first, then a day either side (a run moved by a day)."""
    used: set[int] = set()
    out: list[list[dict]] = [[] for _ in planned]
    for gap in (0, 1):
        for i, s in enumerate(planned):
            if out[i]:
                continue
            d = date.fromisoformat(s["date"])
            for r in runs:
                if r["id"] not in used and abs((date.fromisoformat(r["date"]) - d).days) == gap:
                    out[i].append(r)
                    used.add(r["id"])
    return out


def evaluate(s: dict, runs: list[dict], f: dict) -> dict:
    """How a planned session went: done, how far, and whether an easy run stayed easy."""
    if not runs:
        return {"status": "missed"}
    r = max(runs, key=lambda x: x["miles"])
    notes = []
    status = "done" if r["miles"] >= 0.8 * s["miles"] or s["kind"] in ("intervals", "time_trial") else "short"
    if s["kind"] in ("long", "easy") and r["pace_min"]:
        fast = f["zones"]["easy"][0]
        if r["pace_min"] < fast - 0.25:
            notes.append(f"{r['pace']} /mi is faster than easy ({clock(fast)} or slower). Slow down: easy runs build the endurance your 5K needs.")
        else:
            notes.append("Easy pace: right where it should be.")
    if s["kind"] == "time_trial":
        notes.append(f"{r['duration']} for {r['miles']} mi. Paces were recalculated from this.")
    return {"status": status, "run": r, "notes": notes}


def status(session: Session, now: datetime) -> dict:
    tz = now.tzinfo
    today = now.date()
    first = plan_start(session, today)
    week = (today - first).days // 7 + 1
    monday = today - timedelta(days=today.weekday())
    f = fitness(session)
    recent = session.exec(select(Run).order_by(col(Run.start).desc()).limit(40)).all()
    runs = [run_json(r, tz) for r in recent]
    this_week = []
    planned = sessions(session, monday, monday + timedelta(days=6), today)
    matched = match_runs(planned, runs)
    for s, near in zip(planned, matched):
        if s["date"] > today.isoformat() and not near:
            ev = {"status": "upcoming"}
        elif s["date"] == today.isoformat() and not near:
            ev = {"status": "today"}
        else:
            ev = evaluate(s, near, f)
        this_week.append({**s, **ev})
    week_runs = [r for r in runs if monday.isoformat() <= r["date"] <= today.isoformat()]
    z = f["zones"]
    return {
        "plan": {"start": first.isoformat(), "week": week if 1 <= week <= 12 else None, "weeks": len(WEEKS),
                 "starts_in_days": (first - today).days if today < first else 0,
                 "all": [{"week": i + 1, "sessions": [describe(k, n, z, f["est"])["title"] for k, n in w]}
                         for i, w in enumerate(WEEKS)]},
        "this_week": this_week, "runs": runs[:12],
        "week_miles": round(sum(r["miles"] for r in week_runs), 1),
        "fitness": {"vdot": f["vdot"], "mile_vdot": f["mile_vdot"], "mile": clock(f["mile_min"]), "five_k": clock(f["five_k_min"]),
                    "est_5k": clock(f["est"]["5k"]), "est_mile": clock(f["est"]["mile"]),
                    "target_5k": clock(TARGET_5K_MIN), "target_mile": clock(TARGET_MILE_MIN),
                    "easy": f"{clock(z['easy'][0])}–{clock(z['easy'][1])}", "tempo": clock(z["tempo"]),
                    "interval_400": clock(z["interval_400"]), "goal_400": clock(z["goal_400"])},
        "health": latest_metrics(session), "readiness": readiness(session, today),
        "ingest": {"token_set": bool(get_pref(session, "ingest_token_hash")),
                   "last": get_pref(session, "ingest_last")},
    }


# ---------- Apple Watch health metrics ----------

def put_metric(session: Session, day: str, name: str, value: float, unit: str | None, source: str) -> None:
    m = session.exec(select(HealthMetric).where(HealthMetric.day == day, HealthMetric.name == name)).first()
    if m:
        m.value, m.unit, m.source = value, unit, source
    else:
        m = HealthMetric(day=day, name=name, value=value, unit=unit, source=source)
    session.add(m)


def latest_metrics(session: Session) -> dict:
    out = {}
    for name in ("resting_hr", "hrv", "vo2max", "sleep_hours"):
        m = session.exec(select(HealthMetric).where(HealthMetric.name == name)
                         .order_by(col(HealthMetric.day).desc())).first()
        if m:
            out[name] = {"value": round(m.value, 1), "day": m.day}
    return out


def readiness(session: Session, today: date) -> list[str]:
    """Plain rules from PLAN §8.2: short sleep or a raised resting heart rate make today's run easier."""
    notes = []
    sleep = session.exec(select(HealthMetric).where(HealthMetric.name == "sleep_hours",
                                                    HealthMetric.day == today.isoformat())).first()
    if sleep and sleep.value < 6:
        notes.append(f"Only {sleep.value:.1f} h of sleep last night: keep today's run easy, or swap it with a rest day.")
    rhr = session.exec(select(HealthMetric).where(HealthMetric.name == "resting_hr")
                       .order_by(col(HealthMetric.day).desc()).limit(15)).all()
    if len(rhr) >= 5 and rhr[0].day == today.isoformat():
        base = median(m.value for m in rhr[1:])
        if rhr[0].value >= base + 5:
            notes.append(f"Resting heart rate is {rhr[0].value:.0f}, above your usual {base:.0f}: take it easy today.")
    return notes


# ---------- Health Auto Export (REST automation) ----------

def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(session: Session, key: str = "ingest_token_hash") -> str:
    token = secrets.token_urlsafe(24)
    set_pref(session, key, token_hash(token))
    return token


def token_ok(session: Session, token: str | None, key: str = "ingest_token_hash") -> bool:
    stored = get_pref(session, key)
    return bool(token and stored and secrets.compare_digest(stored, token_hash(token)))


def _dt(value: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(value.strip())


def _meters(qty: float, unit: str | None) -> float:
    u = (unit or "").lower()
    return qty * (MILE if u in ("mi", "mile", "miles") else 1000 if u in ("km", "kilometer") else 1 if u in ("m",) else MILE)


def _qty(x) -> float | None:
    if isinstance(x, dict):
        x = x.get("qty", x.get("Avg", x.get("avg")))
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


RUNNING = re.compile(r"\brun", re.I)


def ingest_auto_export(session: Session, payload: dict, tz: ZoneInfo) -> dict:
    """Health Auto Export's JSON: data.workouts and data.metrics. Unknown fields are ignored."""
    data = payload.get("data", payload)
    added = 0
    for w in data.get("workouts", []) or []:
        if not RUNNING.search(str(w.get("name", ""))):
            continue
        try:
            start = _dt(w["start"])
            end = _dt(w["end"]) if w.get("end") else None
        except (KeyError, ValueError):
            continue
        dist = w.get("distance") or {}
        if not isinstance(dist, dict) or _qty(dist) is None:
            continue
        duration = _qty(w.get("duration")) or ((end - start).total_seconds() if end else 0)
        hr = w.get("heartRate") or {}
        avg_hr = _qty(hr.get("avg")) if isinstance(hr, dict) else None
        max_hr = _qty(hr.get("max")) if isinstance(hr, dict) else None
        avg_hr = avg_hr or _qty(w.get("avgHeartRate"))
        max_hr = max_hr or _qty(w.get("maxHeartRate"))
        if add_run(session, start=start, duration_s=int(duration), distance_m=_meters(_qty(dist), dist.get("units")),
                   avg_hr=avg_hr, max_hr=max_hr, source="auto_export", external_id=f"hae:{w.get('id') or start.isoformat()}",
                   name=w.get("name")):
            added += 1
    names = {"resting_heart_rate": "resting_hr", "heart_rate_variability": "hrv", "vo2_max": "vo2max"}
    metrics = 0
    for m in data.get("metrics", []) or []:
        name = m.get("name")
        for point in m.get("data", []) or []:
            try:
                day = _dt(point["date"]).astimezone(tz).date().isoformat()
            except (KeyError, ValueError):
                continue
            if name in names and _qty(point) is not None:
                put_metric(session, day, names[name], _qty(point), m.get("units"), "auto_export")
                metrics += 1
            elif name == "sleep_analysis":
                hours = _qty(point.get("totalSleep")) or _qty(point.get("asleep"))
                if hours:
                    put_metric(session, day, "sleep_hours", hours, "hr", "auto_export")
                    metrics += 1
    session.commit()
    set_pref(session, "ingest_last", datetime.now(UTC).isoformat())
    return {"runs_added": added, "metrics": metrics}


# ---------- Apple Health export (export.zip from the Health app) ----------

RECORDS = {"HKQuantityTypeIdentifierRestingHeartRate": "resting_hr",
           "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv",
           "HKQuantityTypeIdentifierVO2Max": "vo2max"}


def import_health_export(session: Session, path: str, tz: ZoneInfo, since_days: int = 400) -> dict:
    """Stream through export.xml (it can be hundreds of MB) and keep runs and a few daily metrics."""
    cutoff = datetime.now(tz) - timedelta(days=since_days)
    added = metrics = 0
    with zipfile.ZipFile(path) as z:
        name = next((n for n in z.namelist() if n.endswith("export.xml") and "cda" not in n.lower()), None)
        if not name:
            raise ValueError("That zip doesn't contain export.xml. In the Health app: profile picture → Export All Health Data.")
        with z.open(name) as f:
            for _, el in iterparse(f, events=("end",)):
                if el.tag == "Workout":
                    if "Running" in el.get("workoutActivityType", ""):
                        added += _import_workout(session, el, cutoff)
                    el.clear()
                elif el.tag == "Record":
                    kind = RECORDS.get(el.get("type", ""))
                    if kind:
                        try:
                            when = _dt(el.get("startDate", ""))
                            if when >= cutoff:
                                put_metric(session, when.astimezone(tz).date().isoformat(), kind,
                                           float(el.get("value")), el.get("unit"), "health_export")
                                metrics += 1
                        except (ValueError, TypeError):
                            pass
                    el.clear()
    session.commit()
    return {"runs_added": added, "metrics": metrics}


def _import_workout(session: Session, el, cutoff: datetime) -> int:
    try:
        start = _dt(el.get("startDate", ""))
    except ValueError:
        return 0
    if start < cutoff:
        return 0
    minutes = float(el.get("duration") or 0) if (el.get("durationUnit") or "min") == "min" else float(el.get("duration") or 0) / 60
    dist = float(el.get("totalDistance") or 0)
    unit = el.get("totalDistanceUnit")
    avg_hr = max_hr = None
    for st in el.iter("WorkoutStatistics"):
        t = st.get("type", "")
        if "DistanceWalkingRunning" in t and st.get("sum"):
            dist, unit = float(st.get("sum")), st.get("unit")
        elif t.endswith("HeartRate"):
            avg_hr = float(st.get("average")) if st.get("average") else None
            max_hr = float(st.get("maximum")) if st.get("maximum") else None
    if not dist:
        return 0
    r = add_run(session, start=start, duration_s=int(minutes * 60), distance_m=_meters(dist, unit), avg_hr=avg_hr,
                max_hr=max_hr, source="health_export", external_id=f"hx:{start.isoformat()}",
                name="Indoor Run" if "Indoor" in (el.get("sourceName") or "") else "Run")
    return 1 if r else 0


# ---------- For agents and the weekly rollup ----------

def context_text(session: Session, now: datetime) -> str:
    s = status(session, now)
    f = s["fitness"]
    lines = [f"Running fitness: VDOT {f['vdot']} (5K {f['five_k']}, mile {f['mile']}); targets 5K {f['target_5k']}, mile {f['target_mile']}. "
             f"Paces: easy {f['easy']} /mi, tempo {f['tempo']} /mi, 400 m {f['interval_400']}."]
    if s["plan"]["week"]:
        lines.append(f"Plan week {s['plan']['week']} of 12. This week: " + "; ".join(
            f"{x['day']} {x['title']} ({x['status']}{', ' + x['run']['pace'] + ' /mi' if x.get('run') else ''})" for x in s["this_week"]) + ".")
    else:
        lines.append(f"The 12-week plan starts {s['plan']['start']}.")
    lines += schedule_lines(session, now)
    for r in s["runs"][:3]:
        lines.append(f"Run {r['date']}: {r['miles']} mi in {r['duration']} ({r['pace']} /mi)" + (f", avg HR {r['avg_hr']}" if r["avg_hr"] else "") + ".")
    h = s["health"]
    if h:
        lines.append("Watch: " + ", ".join(f"{k.replace('_', ' ')} {v['value']} ({v['day']})" for k, v in h.items()) + ".")
    lines += s["readiness"]
    return "\n".join(lines)


EFFORT = {"long": "easy, long", "easy": "easy", "intervals": "hard (quality day)", "tempo": "hard (quality day)",
          "time_trial": "all-out time trial"}


def schedule_lines(session: Session, now: datetime, days: int = 7) -> list[str]:
    """Plain answers for "do I run tomorrow? easy or hard?": today, tomorrow, then the next runs with their
    calendar times. Small models answer much better from a sentence than from a table they must work out."""
    from .db import CalendarItem

    today = now.date()
    tz = now.tzinfo
    planned = {s["date"]: s for s in sessions(session, today, today + timedelta(days=days), today)}
    start = datetime.combine(today, datetime.min.time(), tz)
    on_cal = {}
    for it in session.exec(select(CalendarItem).where(CalendarItem.start >= start,
                                                      CalendarItem.start < start + timedelta(days=days + 1))).all():
        if it.title.lower().startswith("run:"):
            local = it.start.astimezone(tz)
            on_cal.setdefault(local.date().isoformat(), f"{local:%H:%M}–{it.end.astimezone(tz):%H:%M}")

    def describe_day(d: date) -> str:
        s = planned.get(d.isoformat())
        if not s:
            return "rest day, no run planned"
        when = on_cal.get(d.isoformat())
        return f"{s['title']}, {EFFORT.get(s['kind'], s['kind'])}" + (f", on the calendar {when}" if when else ", not on the calendar yet")

    tomorrow = today + timedelta(days=1)
    out = [f"Today ({today:%a %d %b}): {describe_day(today)}.", f"Tomorrow ({tomorrow:%a %d %b}): {describe_day(tomorrow)}."]
    upcoming = [f"{date.fromisoformat(k):%a %d %b}: {describe_day(date.fromisoformat(k))}" for k in sorted(planned)
                if k > tomorrow.isoformat()][:3]
    if upcoming:
        out.append("Next runs after that: " + "; ".join(upcoming) + ".")
    return out


def week_summary(session: Session, monday: date, now: datetime) -> dict:
    end = min(monday + timedelta(days=6), now.date())
    planned = sessions(session, monday, end, now.date())
    start_dt = datetime.combine(monday, datetime.min.time(), now.tzinfo)
    runs = session.exec(select(Run).where(Run.start >= start_dt, Run.start < start_dt + timedelta(days=7))).all()
    return {"runs_done": len(runs), "runs_planned": len(planned), "miles": round(sum(r.distance_m for r in runs) / MILE, 1)}


# ---------- Putting runs on the calendar (proposed, like study blocks) ----------

RUN_PREFERRED = ("16:30", "20:30")
RUN_WINDOW = ("06:00", "21:00")


DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def run_windows(session: Session, d: date | None = None) -> tuple[tuple[str, str], tuple[str, str]]:
    """(preferred, allowed) for a day. Hours you told Vector for that weekday win, then your general hours."""
    from . import prefs
    keys = ([f"run_window_{DAY_KEYS[d.weekday()]}"] if d else []) + ["run_window"]
    for key in keys:
        if prefs.get(session, key):
            mine = prefs.window(session, key, RUN_WINDOW)
            # Still prefer late afternoon/evening inside your hours; your hours are the hard limit.
            lo, hi = max(mine[0], RUN_PREFERRED[0]), min(mine[1], RUN_PREFERRED[1])
            return ((lo, hi) if lo < hi else mine), mine
    return RUN_PREFERRED, RUN_WINDOW


def run_length(s: dict, f: dict) -> int:
    """Minutes for a session: the running plus warm-up and cool-down, rounded to 15."""
    return int(round((s["miles"] * f["zones"]["easy"][1] + 15) / 15) * 15)


async def propose_runs(session: Session, cal, actions, now: datetime, days: int = 7) -> dict:
    """Vector proposes the next week's planned runs as calendar blocks, at a free time on the planned day."""
    from .actions import minutes as to_min
    from .calendar import CalendarError
    from .planner import free_slot

    today = now.date()
    proposed = auto = 0
    skipped = []
    f = fitness(session)
    for s in sessions(session, today, today + timedelta(days=days - 1), today):
        key = f"run:{s['date']}"
        if actions.seen(session, key):
            continue
        d = date.fromisoformat(s["date"])
        preferred, allowed = run_windows(session, d)
        length = run_length(s, f)
        try:
            view = await cal.day(session, d)
        except CalendarError:
            continue
        busy = []
        for e in view["events"]:
            if e["all_day"]:
                continue
            st, en = datetime.fromisoformat(e["start"]).astimezone(now.tzinfo), datetime.fromisoformat(e["end"]).astimezone(now.tzinfo)
            busy.append((st.hour * 60 + st.minute if st.date() == d else 0, en.hour * 60 + en.minute if en.date() == d else 1440))
        lo = to_min(allowed[0])
        if d == today:
            lo = max(lo, now.hour * 60 + now.minute + 30)
        slot = free_slot(busy, length, lo, to_min(allowed[1]), (to_min(preferred[0]), to_min(preferred[1])))
        if not slot:
            skipped.append(f"{s['day']} {s['title']}")
            continue
        hm = lambda m: f"{m // 60:02d}:{m % 60:02d}"  # noqa: E731
        payload = {"date": s["date"], "start": hm(slot[0]), "end": hm(slot[1]), "title": f"Run: {s['title']}",
                   "notes": f"{s['detail']}. Target: {s['target']}. Week {s['week']} of 12. Suggested by Vector.",
                   "window": list(allowed), "item_kind": "event", "noun": "runs"}
        a = await actions.propose(session, agent_id="vector", kind="calendar.add_block", title=f"Run: {s['title']}",
                                  reason=f"Week {s['week']}, {s['day']}: {s['detail']}. You're free {hm(slot[0])}–{hm(slot[1])}.",
                                  payload=payload, dedupe_key=key)
        if a.status == "executed":
            auto += 1
        else:
            proposed += 1
    return {"proposed": proposed, "auto": auto, "skipped": skipped}
