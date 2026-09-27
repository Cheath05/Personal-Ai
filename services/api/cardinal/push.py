"""Notifications on your phone and computers (Web Push), Phase 7.

- Each device that turns notifications on registers a push subscription. Messages are encrypted for that device
  (RFC 8291), so Apple's, Google's and Microsoft's push services only carry sealed bytes.
- The hub signs each push with its own VAPID key (data/push.pem, made on first use, never in git or backups).
- What sends one: the morning briefing, a check-in that's due, things waiting for your OK for 10+ minutes, and urgent
  email. Each kind can be turned off, and nothing is sent during quiet hours (default 22:00–06:00).
- "Hide details on the lock screen" replaces the text with a plain "Open Cardinal" line.
"""

import json
import logging
import os
import time as _time
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02
from py_vapid.utils import b64urlencode
from pywebpush import WebPusher
from sqlmodel import Session, col, select

from . import prefs
from .config import data_dir, get_settings
from .db import Action, EmailItem, PushSub, utcnow

log = logging.getLogger("cardinal.push")

KINDS = {
    "briefing": "Morning briefing is ready",
    "checkin": "Check-in time (morning and evening)",
    "approvals": "Something has waited 10 minutes for your OK",
    "urgent_mail": "Urgent email (Relay)",
    "system": "A backup check failed",
}
DEFAULT_QUIET = "22:00-06:00"
APPROVAL_WAIT = timedelta(minutes=10)
CHECKIN_WINDOW = timedelta(hours=3)  # after the hub was down, a check-in more than 3 h late doesn't ping you
TTL = 4 * 3600
MAX_FAILURES = 10

_vapid: Vapid02 | None = None
_client: httpx.AsyncClient | None = None  # tests swap in a mock transport


def key_file() -> Path:
    return data_dir() / "push.pem"


def vapid() -> Vapid02:
    global _vapid
    if _vapid is None:
        path = key_file()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            v = Vapid02()
            v.generate_keys()
            pem = v.private_pem()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(pem)
        _vapid = Vapid02.from_file(str(path))
    return _vapid


def public_key() -> str:
    raw = vapid().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64urlencode(raw)


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15.0)
    return _client


# ---------- Settings ----------

def settings_json(session: Session) -> dict:
    kinds = json.loads(prefs.get(session, "push_kinds") or "{}")
    return {"kinds": {k: kinds.get(k, True) for k in KINDS}, "labels": KINDS,
            "quiet": prefs.get(session, "push_quiet", DEFAULT_QUIET),
            "private": prefs.get(session, "push_private") == "1"}


def save_settings(session: Session, kinds: dict | None, quiet: str | None, private: bool | None) -> dict:
    if kinds is not None:
        prefs.put(session, "push_kinds", json.dumps({k: bool(v) for k, v in kinds.items() if k in KINDS}))
    if quiet is not None:
        prefs.put(session, "push_quiet", quiet)
    if private is not None:
        prefs.put(session, "push_private", "1" if private else "0")
    return settings_json(session)


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def quiet_now(session: Session, now: datetime) -> bool:
    window = prefs.get(session, "push_quiet", DEFAULT_QUIET) or ""
    if "-" not in window:
        return False
    start, end = (_hm(x) for x in window.split("-"))
    t = now.time()
    return (start <= t < end) if start < end else (t >= start or t < end)


# ---------- Sending ----------

def subscribe(session: Session, sub: dict, device: str | None) -> PushSub:
    endpoint = str(sub.get("endpoint", ""))
    keys = sub.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("That isn't a push subscription.")
    row = session.exec(select(PushSub).where(PushSub.endpoint == endpoint)).first() or PushSub(endpoint=endpoint, p256dh="", auth="")
    row.p256dh, row.auth, row.device, row.failures = keys["p256dh"], keys["auth"], (device or "")[:40] or None, 0
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


async def _send_one(session: Session, sub: PushSub, payload: bytes, topic: str | None, urgency: str) -> bool:
    body = WebPusher({"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}).encode(payload)["body"]
    u = urlparse(sub.endpoint)
    public = get_settings().public_url
    claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int(_time.time()) + 12 * 3600,
              "sub": public if public.startswith("https://") else "mailto:cardinal@localhost"}
    headers = {**vapid().sign(claims), "TTL": str(TTL), "Content-Encoding": "aes128gcm",
               "Content-Type": "application/octet-stream", "Urgency": urgency}
    if topic:
        headers["Topic"] = topic  # a newer push with the same topic replaces one the device hasn't received yet
    try:
        r = await client().post(sub.endpoint, content=body, headers=headers)
    except httpx.HTTPError as e:
        log.warning("Push to %s failed: %s", sub.device, e)
        r = None
    if r is not None and r.status_code in (404, 410):  # the device unsubscribed or the browser was reset
        session.delete(sub)
        session.commit()
        return False
    if r is None or r.status_code >= 300:
        sub.failures += 1
        if sub.failures >= MAX_FAILURES:
            session.delete(sub)
        else:
            session.add(sub)
        session.commit()
        if r is not None:
            log.warning("Push to %s refused: %s %s", sub.device, r.status_code, r.text[:200])
        return False
    sub.last_ok, sub.failures = utcnow(), 0
    session.add(sub)
    session.commit()
    return True


async def notify(session: Session, kind: str, title: str, body: str, url: str = "/", *, now: datetime | None = None,
                 force: bool = False, only: int | None = None) -> int:
    """Send to every device (or just `only`). Returns how many took it. `force` skips the kind and quiet checks."""
    if not force:
        s = settings_json(session)
        tz = ZoneInfo(get_settings().timezone)
        if not s["kinds"].get(kind, True) or quiet_now(session, now or datetime.now(tz)):
            return 0
    if prefs.get(session, "push_private") == "1":
        body = "Open Cardinal to see it."
    payload = json.dumps({"title": title[:80], "body": body[:180], "url": url, "tag": kind}).encode()
    q = select(PushSub)
    if only:
        q = q.where(PushSub.id == only)
    sent = 0
    for sub in session.exec(q).all():
        sent += await _send_one(session, sub, payload, topic=kind.replace("_", "-")[:32],
                                urgency="high" if kind in ("urgent_mail", "test") else "normal")
    return sent


# ---------- What triggers them (called by the scheduler every 30 s) ----------

def _once(session: Session, key: str, value: str) -> bool:
    """True the first time `value` is seen for `key` (so each thing pings you once)."""
    if prefs.get(session, key) == value:
        return False
    prefs.put(session, key, value)
    return True


async def tick(session: Session, now: datetime, settings) -> None:
    if not session.exec(select(PushSub)).first():
        return
    from . import review
    st = review.status(session, now, settings)
    for kind, label, body in (("morning", "Morning check-in", "Your top 3 for today and your energy. About a minute."),
                              ("evening", "Evening review", "What got done, and tomorrow's first task.")):
        c = st[kind]
        at = datetime.combine(now.date(), _hm(c["time"]), now.tzinfo)
        if c["due"] and now - at < CHECKIN_WINDOW and _once(session, f"push_checkin_{kind}", st["day"]):
            await notify(session, "checkin", f"Delta · {label}", body, "/#review", now=now)

    # Proposals that have waited 10 minutes (if you're in the app you've likely already seen the card).
    last = int(prefs.get(session, "push_approvals_seen", "0") or 0)
    waiting = session.exec(select(Action).where(Action.status == "pending", Action.id > last)
                           .order_by(col(Action.id))).all()
    old = [a for a in waiting if utcnow() - (a.ts if a.ts.tzinfo else a.ts.replace(tzinfo=UTC)) >= APPROVAL_WAIT]
    if old:
        prefs.put(session, "push_approvals_seen", str(old[-1].id))
        n = len(session.exec(select(Action).where(Action.status == "pending")).all())
        names = "; ".join(a.title for a in old[:2])
        await notify(session, "approvals", f"{n} thing{'s' if n != 1 else ''} need{'s' if n == 1 else ''} your OK",
                     names, "/#today", now=now)


async def after_briefing(session: Session, text: str, now: datetime) -> None:
    first = text.split(". ")[0].strip().rstrip(".") + "."
    await notify(session, "briefing", "Ordinal · Your briefing", first, "/#today", now=now)


async def after_sort(session: Session, now: datetime) -> None:
    last = int(prefs.get(session, "push_mail_seen", "0") or 0)
    new = session.exec(select(EmailItem).where(EmailItem.id > last).order_by(col(EmailItem.id))).all()
    if not new:
        return
    prefs.put(session, "push_mail_seen", str(new[-1].id))
    urgent = [e for e in new if e.category == "urgent" and not e.done]
    if urgent and last:  # the very first sort isn't news
        e = urgent[0]
        more = f" (+{len(urgent) - 1} more)" if len(urgent) > 1 else ""
        await notify(session, "urgent_mail", f"Relay · Urgent from {e.sender}"[:80], f"{e.subject}{more}", "/#today", now=now)
