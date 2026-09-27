"""App usage from ActivityWatch on your laptops: which apps, for how many minutes, per hour.

The bridge on each laptop (scripts/aw_bridge.py) sends app names and seconds only. Never window titles,
URLs, keystrokes or screenshots. Apps are grouped into focus, distraction and neutral with a small list you
can change later; browsers are neutral because without titles Cardinal can't tell homework from YouTube.
"""

import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from statistics import mean

from sqlmodel import Session, select

from .db import AppUsage

FOCUS = ["code", "visual studio", "xcode", "pycharm", "intellij", "terminal", "iterm", "warp", "powershell",
         "windows terminal", "notion", "obsidian", "word", "excel", "powerpoint", "pages", "numbers", "keynote",
         "onenote", "notes", "preview", "acrobat", "zotero", "anki", "goodnotes", "matlab", "rstudio", "jupyter",
         "overleaf", "libreoffice", "mathematica", "logisim", "eclipse", "sublime", "vim", "cursor", "cardinal"]
DISTRACT = ["discord", "steam", "epic games", "battle.net", "riot", "valorant", "league of legends", "minecraft",
            "roblox", "spotify", "music", "netflix", "tiktok", "instagram", "twitch", "whatsapp", "messages", "telegram",
            "messenger", "facetime", "tv", "hulu", "prime video", "disney"]
NEUTRAL = ["safari", "chrome", "firefox", "edge", "arc", "brave", "opera", "finder", "explorer", "mail", "outlook",
           "calendar", "settings", "system preferences", "zoom", "teams", "slack"]


def category(app: str) -> str:
    a = app.lower().removesuffix(".exe")
    for kind, names in (("neutral", NEUTRAL), ("focus", FOCUS), ("distraction", DISTRACT)):
        if any(re.search(rf"\b{re.escape(n)}\b", a) for n in names):
            return kind
    return "neutral"


def ingest(session: Session, device: str, hours: list[dict]) -> int:
    """Upsert {start, apps: [{app, seconds}]} per hour. Re-sending an hour replaces it (the bridge may resend)."""
    device = re.sub(r"[^\w .-]", "", device)[:40] or "Laptop"
    n = 0
    for h in hours:
        try:
            start = datetime.fromisoformat(str(h["start"]).replace("Z", "+00:00")).astimezone(UTC)
        except (KeyError, ValueError):
            continue
        start = start.replace(minute=0, second=0, microsecond=0)
        for old in session.exec(select(AppUsage).where(AppUsage.device == device, AppUsage.hour == start)).all():
            session.delete(old)
        for a in h.get("apps", [])[:60]:
            app = str(a.get("app") or "").strip()[:80]
            secs = int(max(0, min(3600, float(a.get("seconds") or 0))))
            if app and secs >= 30:
                session.add(AppUsage(device=device, hour=start, app=app, seconds=secs))
                n += 1
    session.commit()
    return n


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def day_summary(session: Session, day_start: datetime) -> dict:
    """Totals for one local day. day_start is local midnight (tz-aware)."""
    rows = session.exec(select(AppUsage).where(AppUsage.hour >= day_start.astimezone(UTC),
                                               AppUsage.hour < (day_start + timedelta(days=1)).astimezone(UTC))).all()
    by_app: dict[str, int] = defaultdict(int)
    by_hour = [{"focus": 0, "distraction": 0, "neutral": 0} for _ in range(24)]
    devices = set()
    for r in rows:
        by_app[r.app] += r.seconds
        devices.add(r.device)
        local = _aware(r.hour).astimezone(day_start.tzinfo)
        by_hour[local.hour][category(r.app)] += r.seconds
    totals = {k: sum(h[k] for h in by_hour) for k in ("focus", "distraction", "neutral")}
    top = sorted(by_app.items(), key=lambda x: -x[1])[:8]
    return {"date": day_start.date().isoformat(), "totals": totals, "devices": sorted(devices),
            "top": [{"app": a, "minutes": round(s / 60), "category": category(a)} for a, s in top],
            "hours": [{"hour": i, **{k: round(v / 60) for k, v in h.items()}} for i, h in enumerate(by_hour)],
            "last_sync": max((_aware(r.hour) for r in rows), default=None)}


def last_seen(session: Session) -> dict[str, datetime]:
    out = {}
    for r in session.exec(select(AppUsage)).all():
        t = _aware(r.hour)
        if r.device not in out or t > out[r.device]:
            out[r.device] = t
    return out


def hm(seconds: int) -> str:
    m = round(seconds / 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def context_text(session: Session, now: datetime) -> str:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    s = day_summary(session, start)
    if not s["devices"]:
        return ""
    t = s["totals"]
    top = ", ".join(f"{a['app']} {a['minutes']}m" for a in s["top"][:4])
    return (f"Computer time today (app names only): focus {hm(t['focus'])}, distraction {hm(t['distraction'])}, "
            f"other {hm(t['neutral'])}. Top apps: {top}.")


def week_totals(session: Session, monday: datetime) -> dict:
    tot = {"focus": 0, "distraction": 0, "neutral": 0}
    for i in range(7):
        d = day_summary(session, monday + timedelta(days=i))
        for k in tot:
            tot[k] += d["totals"][k]
    return {k: round(v / 3600, 1) for k, v in tot.items()}


def focus_patterns(session: Session, now: datetime) -> list[dict]:
    """For Sigma: the hours you most often focus, from at least 5 days of data."""
    days = []
    for i in range(1, 29):
        d = day_summary(session, (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0))
        if d["totals"]["focus"] + d["totals"]["distraction"] >= 1800:
            days.append(d)
    if len(days) < 5:
        return []
    avg = [mean(d["hours"][h]["focus"] for d in days) for h in range(24)]
    best = max(range(23), key=lambda h: avg[h] + avg[h + 1])
    out = []
    if avg[best] + avg[best + 1] >= 30:
        out.append({"key": f"focus-hours:{best}",
                    "text": f"Focuses best around {best:02d}:00–{best + 2:02d}:00 on the computer.",
                    "evidence": f"Average {round(avg[best] + avg[best + 1])} focus minutes in those hours over {len(days)} days."})
    dis = [mean(d["hours"][h]["distraction"] for d in days) for h in range(24)]
    worst = max(range(23), key=lambda h: dis[h] + dis[h + 1])
    if dis[worst] + dis[worst + 1] >= 30:
        out.append({"key": f"distraction-hours:{worst}",
                    "text": f"Most distracted around {worst:02d}:00–{worst + 2:02d}:00.",
                    "evidence": f"Average {round(dis[worst] + dis[worst + 1])} minutes of games, chat or video then, over {len(days)} days."})
    return out
