"""Action Previews, trust rules and the Activity Log.

Agents never change anything directly. They propose an Action; it runs only when you authorize it, or when a
trust rule you created covers it (and then you still get a note with Undo). Some kinds of action can never be
covered by a rule. This module is the only code path that executes changes.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import Session, col, select

from .calendar import Calendar, CalendarError
from .db import Action, Experiment, Memory, TrustRule, utcnow

# Never automatic, whatever rules exist: these can't be undone, or they speak or pay for you (PLAN §6).
ALWAYS_ASK = {
    "email.send": "sends a message to another person",
    "calendar.remove_item": "deletes something",
    "files.delete": "deletes files",
    "payment": "spends money",
    "settings.security": "changes security or privacy settings",
    "device.operator": "controls your screen",
}
RULE_TTL = timedelta(days=60)  # unused rules expire
UNDO_WINDOW = timedelta(days=14)
SUGGEST_AFTER = 3  # Sigma offers a rule after this many approvals of the same thing


class ActionError(Exception):
    pass


def _aware(dt: datetime | None) -> datetime | None:
    from datetime import UTC
    return dt.replace(tzinfo=UTC) if dt and dt.tzinfo is None else dt


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


@dataclass
class Kind:
    name: str
    label: str
    undoable: bool
    preview: Callable[[dict, ZoneInfo], dict]
    rule_from: Callable[[dict, str], tuple[dict, str]]  # (conditions, description)
    matches: Callable[[dict, dict], bool]
    execute: Callable[["Actions", Session, Action, dict], Awaitable[dict]]  # returns what undo needs
    undo: Callable[["Actions", Session, dict], Awaitable[None]]


# ---------- calendar.add_block: put a block on the Cardinal calendar ----------

def _block_preview(p: dict, tz: ZoneInfo) -> dict:
    d = date.fromisoformat(p["date"])
    when = f"{d:%a} {d.day} {d:%b} · " + (f"{p['start']}–{p['end']}" if p.get("start") else "all day")
    return {"change": "Add to your Cardinal calendar", "before": "Free time", "after": f"{when} · {p['title']}",
            "where": "Cardinal calendar (shows in Google and Apple Calendar)"}


def _block_rule(p: dict, agent_name: str) -> tuple[dict, str]:
    dur = minutes(p["end"]) - minutes(p["start"]) if p.get("start") and p.get("end") else 60
    cap = max(120, -(-dur // 30) * 30)
    earliest, latest = p.get("window", ["08:00", "22:00"])
    cond = {"max_minutes": cap, "earliest": earliest, "latest": latest}
    hours = f"{cap // 60} h" + (f" {cap % 60} min" if cap % 60 else "")
    noun = p.get("noun", "study blocks")
    return cond, (f"{agent_name} may add {noun} of up to {hours} to your Cardinal calendar, "
                  f"between {earliest} and {latest}. Nothing else. You still get a note with Undo.")


def _block_matches(cond: dict, p: dict) -> bool:
    if not p.get("start") or not p.get("end"):
        return False
    s, e = minutes(p["start"]), minutes(p["end"])
    return (e - s <= cond["max_minutes"] and s >= minutes(cond["earliest"]) and e <= minutes(cond["latest"]))


async def _block_execute(svc: "Actions", session: Session, a: Action, p: dict) -> dict:
    item, warning = await svc.calendar.add(
        session, title=p["title"], day=date.fromisoformat(p["date"]), start=p["start"], end=p["end"],
        kind=p.get("item_kind", "reading"), course=p.get("course"), notes=p.get("notes"), source=f"action:{a.id}")
    return {"item_id": item.id, "warning": warning}


async def _block_undo(svc: "Actions", session: Session, result: dict) -> None:
    try:
        await svc.calendar.delete(session, result["item_id"])
    except CalendarError:
        pass  # already removed by hand


# ---------- memory.add: Sigma saves a pattern to Core Memory ----------

def _memory_preview(p: dict, tz: ZoneInfo) -> dict:
    return {"change": "Add to Core Memory (every agent will read it)", "before": "Not known",
            "after": p["text"], "where": "Core Memory, in Review. You can edit or delete it."}


def _memory_rule(p: dict, agent_name: str) -> tuple[dict, str]:
    return {}, (f"{agent_name} may save new patterns to Core Memory without asking. "
                "You still get a note with Undo, and you can edit or delete any memory.")


async def _memory_execute(svc: "Actions", session: Session, a: Action, p: dict) -> dict:
    m = Memory(kind=p.get("kind", "pattern"), text=p["text"][:300], evidence=(p.get("evidence") or "")[:500] or None,
               confidence=float(p.get("confidence", 0.6)), source="sigma")
    session.add(m)
    session.commit()
    session.refresh(m)
    return {"memory_id": m.id}


async def _memory_undo(svc: "Actions", session: Session, result: dict) -> None:
    m = session.get(Memory, result.get("memory_id"))
    if m:
        session.delete(m)
        session.commit()


# ---------- plan.set_week: Delta's plan for next week (focus + experiments) ----------

def _plan_preview(p: dict, tz: ZoneInfo) -> dict:
    d = date.fromisoformat(p["week_start"])
    focus = "; ".join(p.get("focus", [])) or "none"
    exps = "; ".join(p.get("experiments", [])) or "none"
    return {"change": f"Set your plan for the week of {d:%a} {d.day} {d:%b}", "before": "No plan yet",
            "after": f"Focus: {focus} · Experiments: {exps}", "where": "Review → This week"}


def _plan_rule(p: dict, agent_name: str) -> tuple[dict, str]:
    return {}, (f"{agent_name} may set your weekly focus and experiments each Sunday without asking. "
                "You still get a note with Undo.")


async def _plan_execute(svc: "Actions", session: Session, a: Action, p: dict) -> dict:
    ids = []
    for text in p.get("experiments", [])[:3]:
        e = Experiment(week_start=p["week_start"], text=text[:200])
        session.add(e)
        session.commit()
        session.refresh(e)
        ids.append(e.id)
    return {"experiment_ids": ids}


async def _plan_undo(svc: "Actions", session: Session, result: dict) -> None:
    for eid in result.get("experiment_ids", []):
        e = session.get(Experiment, eid)
        if e:
            session.delete(e)
    session.commit()


# ---------- calendar.move_item: change when something on the Cardinal calendar happens ----------

def _when(p: dict) -> str:
    d = date.fromisoformat(p["date"])
    return f"{d:%a} {d.day} {d:%b} · " + (f"{p['start']}–{p['end']}" if p.get("start") else "all day")


def _move_preview(p: dict, tz: ZoneInfo) -> dict:
    return {"change": "Move it on your Cardinal calendar", "before": f"{_when(p['from'])} · {p['title']}",
            "after": f"{_when(p)} · {p['title']}", "where": "Cardinal calendar (and Google and Apple Calendar)"}


def _move_rule(p: dict, agent_name: str) -> tuple[dict, str]:
    earliest, latest = p.get("window", ["06:00", "22:00"])
    noun = p.get("noun", "items")
    return ({"earliest": earliest, "latest": latest, "max_minutes": 240},
            f"{agent_name} may move {noun} on your Cardinal calendar to times between {earliest} and {latest}. "
            "You still get a note with Undo.")


def _move_matches(cond: dict, p: dict) -> bool:
    if not p.get("start"):
        return False
    return _block_matches(cond, p)


async def _move_execute(svc: "Actions", session: Session, a: Action, p: dict) -> dict:
    item, warning = await svc.calendar.move(session, p["item_id"], date.fromisoformat(p["date"]), p.get("start"), p.get("end"))
    return {"item_id": item.id, "from": p["from"], "warning": warning}


async def _move_undo(svc: "Actions", session: Session, result: dict) -> None:
    f = result["from"]
    try:
        await svc.calendar.move(session, result["item_id"], date.fromisoformat(f["date"]), f.get("start"), f.get("end"))
    except CalendarError:
        pass


# ---------- calendar.remove_item: always asks ----------

def _remove_preview(p: dict, tz: ZoneInfo) -> dict:
    return {"change": "Remove it from your Cardinal calendar", "before": f"{_when(p)} · {p['title']}",
            "after": "Removed", "where": "Cardinal calendar (and Google and Apple Calendar)"}


async def _remove_execute(svc: "Actions", session: Session, a: Action, p: dict) -> dict:
    from .db import CalendarItem
    item = session.get(CalendarItem, p["item_id"])
    if not item:
        raise ActionError("It's already gone.")
    snap = {"title": item.title, "date": item.start.astimezone(svc.tz).date().isoformat(),
            "start": None if item.all_day else f"{item.start.astimezone(svc.tz):%H:%M}",
            "end": None if item.all_day else f"{item.end.astimezone(svc.tz):%H:%M}",
            "kind": item.kind, "course": item.course, "notes": item.notes, "source": item.source}
    await svc.calendar.delete(session, item.id)
    return {"removed": snap}


async def _remove_undo(svc: "Actions", session: Session, result: dict) -> None:
    r = result["removed"]
    await svc.calendar.add(session, title=r["title"], day=date.fromisoformat(r["date"]), start=r["start"], end=r["end"],
                           kind=r["kind"], course=r["course"], notes=r["notes"], source=r["source"])


# ---------- email.draft / email.send: Relay's replies ----------

def _email_preview(p: dict, tz: ZoneInfo) -> dict:
    from .relay import recipient_name
    return {"change": "Save a reply as a Gmail draft (in the same thread). Nothing is sent.",
            "before": "No reply", "after": f"To {recipient_name(p['to'])} · {p['subject']}\n\n{p['body']}",
            "where": f"Your {'UMBC' if p.get('account') == 'school' else 'personal'} Gmail drafts"}


def _send_preview(p: dict, tz: ZoneInfo) -> dict:
    from .relay import recipient_name
    return {"change": "SEND this email. It can't be unsent.", "before": "Draft",
            "after": f"To {recipient_name(p['to'])} · {p['subject']}\n\n{p['body']}",
            "where": f"From your {'UMBC' if p.get('account') == 'school' else 'personal'} Gmail"}


def _draft_rule(p: dict, agent_name: str) -> tuple[dict, str]:
    return {}, (f"{agent_name} may save reply drafts in your Gmail without asking. It never sends them: "
                "sending always asks. You still get a note with Undo.")


def _gmail(svc: "Actions", session: Session, p: dict):
    google = svc.calendar.google
    acct = google.account(session, p.get("account", "personal"))
    if not google.can_draft(acct):
        raise ActionError("Gmail drafts aren't allowed yet. Reconnect that Google account in Access → Accounts to allow them.")
    return google, acct


async def _draft_execute(svc: "Actions", session: Session, a: Action, p: dict) -> dict:
    google, acct = _gmail(svc, session, p)
    try:
        draft_id = await google.create_draft(session, acct, to=p["to"], subject=p["subject"], body=p["body"],
                                             thread_id=p.get("thread_id"), in_reply_to=p.get("in_reply_to"),
                                             references=p.get("references"))
    except Exception as e:
        raise ActionError(f"Gmail didn't take the draft: {e}") from e
    if p.get("email_item_id"):
        from .db import EmailItem
        item = session.get(EmailItem, p["email_item_id"])
        if item:
            item.draft_action_id = a.id
            session.add(item)
            session.commit()
    return {"draft_id": draft_id, "account": p.get("account", "personal")}


async def _draft_undo(svc: "Actions", session: Session, result: dict) -> None:
    google = svc.calendar.google
    acct = google.account(session, result.get("account", "personal"))
    if acct:
        await google.delete_draft(session, acct, result["draft_id"])


async def _send_execute(svc: "Actions", session: Session, a: Action, p: dict) -> dict:
    google, acct = _gmail(svc, session, p)
    try:
        draft_id = p.get("draft_id") or await google.create_draft(
            session, acct, to=p["to"], subject=p["subject"], body=p["body"], thread_id=p.get("thread_id"),
            in_reply_to=p.get("in_reply_to"), references=p.get("references"))
        sent_id = await google.send_draft(session, acct, draft_id)
    except Exception as e:
        raise ActionError(f"Gmail didn't send it: {e}") from e
    if p.get("email_item_id"):
        from .db import EmailItem
        item = session.get(EmailItem, p["email_item_id"])
        if item:
            item.done = True
            session.add(item)
            session.commit()
    return {"sent_id": sent_id}


async def _no_undo(svc: "Actions", session: Session, result: dict) -> None:
    raise ActionError("Sent email can't be unsent.")


KINDS = {
    "email.draft": Kind("email.draft", "Save a reply draft", True,
                        _email_preview, _draft_rule, lambda c, p: True, _draft_execute, _draft_undo),
    "email.send": Kind("email.send", "Send an email", False,
                       _send_preview, lambda p, n: ({}, ""), lambda c, p: False, _send_execute, _no_undo),
    "calendar.move_item": Kind("calendar.move_item", "Move a calendar item", True,
                               _move_preview, _move_rule, _move_matches, _move_execute, _move_undo),
    "calendar.remove_item": Kind("calendar.remove_item", "Remove a calendar item", True,
                                 _remove_preview, lambda p, n: ({}, ""), lambda c, p: False, _remove_execute, _remove_undo),
    "calendar.add_block": Kind("calendar.add_block", "Add a calendar block", True,
                               _block_preview, _block_rule, _block_matches, _block_execute, _block_undo),
    "memory.add": Kind("memory.add", "Save to Core Memory", True,
                       _memory_preview, _memory_rule, lambda cond, p: True, _memory_execute, _memory_undo),
    "plan.set_week": Kind("plan.set_week", "Set next week's plan", True,
                          _plan_preview, _plan_rule, lambda cond, p: True, _plan_execute, _plan_undo),
}


class Actions:
    def __init__(self, calendar: Calendar, agent_names: dict[str, str]):
        self.calendar = calendar
        self.agent_names = agent_names
        calendar.proposals = self.proposed_events  # pending blocks show on the calendar as ghosts

    @property
    def tz(self) -> ZoneInfo:
        return self.calendar.tz

    # ---------- Views ----------

    def to_json(self, a: Action) -> dict:
        kind = KINDS.get(a.kind)
        payload = json.loads(a.payload)
        undo_until = _aware(a.executed_at) + UNDO_WINDOW if a.executed_at else None
        return {
            "id": a.id, "ts": a.ts.isoformat(), "agent_id": a.agent_id,
            "agent": self.agent_names.get(a.agent_id, a.agent_id), "kind": a.kind,
            "label": kind.label if kind else a.kind, "title": a.title, "reason": a.reason,
            "preview": kind.preview(payload, self.tz) if kind else {}, "payload": payload, "status": a.status,
            "undoable": a.undoable, "always_ask": a.kind in ALWAYS_ASK, "rule_id": a.rule_id, "error": a.error,
            "decided_at": a.decided_at.isoformat() if a.decided_at else None,
            "executed_at": a.executed_at.isoformat() if a.executed_at else None,
            "can_undo": bool(a.status == "executed" and a.undoable and undo_until and undo_until > utcnow()),
        }

    def expire_stale(self, session: Session) -> None:
        now = datetime.now(self.tz)
        changed = False
        for a in session.exec(select(Action).where(Action.status == "pending")).all():
            p = json.loads(a.payload)
            if p.get("date") and p.get("start"):
                start = datetime.combine(date.fromisoformat(p["date"]), datetime.min.time(), self.tz).replace(
                    hour=int(p["start"][:2]), minute=int(p["start"][3:5]))
                if start <= now:
                    a.status, a.decided_at = "expired", utcnow()
                    session.add(a)
                    changed = True
        if changed:
            session.commit()
            self.calendar.invalidate()

    def pending(self, session: Session) -> list[Action]:
        self.expire_stale(session)
        return list(session.exec(select(Action).where(Action.status == "pending").order_by(col(Action.id))).all())

    def recent_auto(self, session: Session, hours: int = 36) -> list[Action]:
        since = utcnow() - timedelta(hours=hours)
        return list(session.exec(select(Action).where(Action.rule_id != None, Action.executed_at >= since)  # noqa: E711
                                 .order_by(col(Action.id).desc())).all())

    def log(self, session: Session, limit: int = 60) -> list[Action]:
        return list(session.exec(select(Action).where(Action.status != "pending")
                                 .order_by(col(Action.id).desc()).limit(limit)).all())

    def proposed_events(self, session: Session, start: datetime, end: datetime) -> list[dict]:
        out = []
        for a in session.exec(select(Action).where(Action.status == "pending", Action.kind == "calendar.add_block")).all():
            p = json.loads(a.payload)
            d = date.fromisoformat(p["date"])
            s = datetime.combine(d, datetime.min.time(), self.tz).replace(hour=int(p["start"][:2]), minute=int(p["start"][3:5]))
            e = datetime.combine(d, datetime.min.time(), self.tz).replace(hour=int(p["end"][:2]), minute=int(p["end"][3:5]))
            if s < end and e > start:
                out.append({"id": f"action:{a.id}", "action_id": a.id, "start": s.isoformat(), "end": e.isoformat(),
                            "all_day": False, "title": p["title"], "source": "proposed", "kind": p.get("item_kind", "reading"),
                            "calendar": f"Proposed by {self.agent_names.get(a.agent_id, a.agent_id)}",
                            "color": "#ff4d6d" if a.agent_id == "vector" else "#a47bff", "deletable": False})
        return out

    # ---------- Rules ----------

    def rules(self, session: Session) -> list[TrustRule]:
        return list(session.exec(select(TrustRule).where(TrustRule.active == True)  # noqa: E712
                                 .order_by(col(TrustRule.id))).all())

    def rule_json(self, r: TrustRule) -> dict:
        last = _aware(r.last_used) or _aware(r.created_at)
        return {"id": r.id, "agent": self.agent_names.get(r.agent_id, r.agent_id), "agent_id": r.agent_id,
                "kind": r.kind, "label": KINDS[r.kind].label if r.kind in KINDS else r.kind,
                "description": r.description, "uses": r.uses,
                "last_used": r.last_used.isoformat() if r.last_used else None,
                "expires": (last + RULE_TTL).date().isoformat()}

    def _matching_rule(self, session: Session, a: Action) -> TrustRule | None:
        if a.kind in ALWAYS_ASK or a.kind not in KINDS:
            return None
        payload = json.loads(a.payload)
        now = utcnow()
        for r in session.exec(select(TrustRule).where(TrustRule.active == True, TrustRule.agent_id == a.agent_id,  # noqa: E712
                                                      TrustRule.kind == a.kind)).all():
            if (_aware(r.last_used) or _aware(r.created_at)) + RULE_TTL < now:
                r.active = False  # unused for 60 days: expired
                session.add(r)
                continue
            try:
                if KINDS[a.kind].matches(json.loads(r.conditions), payload):
                    return r
            except (KeyError, ValueError):
                continue
        session.commit()
        return None

    def rule_preview(self, session: Session, action_id: int) -> dict:
        a = self._get(session, action_id)
        if a.kind in ALWAYS_ASK:
            return {"allowed": False, "description": f"This always asks, because it {ALWAYS_ASK[a.kind]}."}
        cond, desc = KINDS[a.kind].rule_from(json.loads(a.payload), self.agent_names.get(a.agent_id, a.agent_id))
        return {"allowed": True, "description": desc, "conditions": cond}

    def remember(self, session: Session, action_id: int) -> TrustRule:
        a = self._get(session, action_id)
        if a.kind in ALWAYS_ASK:
            raise ActionError(f"This can't be made automatic, because it {ALWAYS_ASK[a.kind]}.")
        cond, desc = KINDS[a.kind].rule_from(json.loads(a.payload), self.agent_names.get(a.agent_id, a.agent_id))
        rule = TrustRule(agent_id=a.agent_id, kind=a.kind, conditions=json.dumps(cond), description=desc,
                         source_action_id=a.id)
        session.add(rule)
        session.commit()
        session.refresh(rule)
        return rule

    def revoke(self, session: Session, rule_id: int) -> None:
        r = session.get(TrustRule, rule_id)
        if r:
            r.active = False
            session.add(r)
            session.commit()

    def suggestion(self, session: Session, a: Action) -> dict | None:
        """Sigma: after the same kind of approval a few times, offer to make it automatic."""
        if a.kind in ALWAYS_ASK or any(r.agent_id == a.agent_id and r.kind == a.kind for r in self.rules(session)):
            return None
        since = utcnow() - timedelta(days=30)
        n = len(session.exec(select(Action).where(Action.agent_id == a.agent_id, Action.kind == a.kind,
                                                  Action.rule_id == None, Action.executed_at >= since)).all())  # noqa: E711
        if n < SUGGEST_AFTER:
            return None
        preview = self.rule_preview(session, a.id)
        return {"from": "Sigma", "text": f"You've approved this {n} times. Make it automatic?",
                "rule": preview["description"], "action_id": a.id}

    # ---------- Lifecycle ----------

    def _get(self, session: Session, action_id: int) -> Action:
        a = session.get(Action, action_id)
        if not a:
            raise ActionError("That action doesn't exist.")
        return a

    def update_pending(self, session: Session, a: Action, payload: dict) -> Action:
        """Change a proposal that's still waiting (e.g. you asked for a different time before approving it)."""
        if a.status != "pending":
            raise ActionError("That's no longer waiting.")
        a.payload = json.dumps(payload)
        a.reason = f"{a.reason} Changed from chat to {payload.get('date')} {payload.get('start') or ''}".strip()
        session.add(a)
        session.commit()
        self.calendar.invalidate()
        return a

    def seen(self, session: Session, dedupe_key: str) -> bool:
        return session.exec(select(Action).where(Action.dedupe_key == dedupe_key)).first() is not None

    async def propose(self, session: Session, *, agent_id: str, kind: str, title: str, reason: str, payload: dict,
                      dedupe_key: str | None = None) -> Action:
        if kind not in KINDS:
            raise ActionError(f"Unknown action kind: {kind}")
        a = Action(agent_id=agent_id, kind=kind, title=title, reason=reason, payload=json.dumps(payload),
                   undoable=KINDS[kind].undoable, dedupe_key=dedupe_key)
        session.add(a)
        session.commit()
        session.refresh(a)
        rule = self._matching_rule(session, a)
        if rule:
            await self._execute(session, a, rule=rule)
        self.calendar.invalidate()
        return a

    async def approve(self, session: Session, action_id: int, remember: bool = False) -> tuple[Action, dict | None]:
        a = self._get(session, action_id)
        if a.status != "pending":
            raise ActionError(f"This was already {a.status}.")
        rule = self.remember(session, a.id) if remember else None
        await self._execute(session, a, rule=rule, by_rule=False)
        return a, (None if remember or a.status != "executed" else self.suggestion(session, a))

    def deny(self, session: Session, action_id: int) -> Action:
        a = self._get(session, action_id)
        if a.status != "pending":
            raise ActionError(f"This was already {a.status}.")
        a.status, a.decided_at = "denied", utcnow()
        session.add(a)
        session.commit()
        self.calendar.invalidate()
        return a

    async def _execute(self, session: Session, a: Action, rule: TrustRule | None = None, by_rule: bool = True) -> None:
        p = json.loads(a.payload)
        a.decided_at = utcnow()
        try:
            kind = KINDS.get(a.kind)
            if not kind:
                raise ActionError(f"No way to run {a.kind} yet.")
            a.result = json.dumps(await kind.execute(self, session, a, p))
            a.status, a.executed_at, a.error = "executed", utcnow(), None
            if rule and by_rule:
                a.rule_id = rule.id
                rule.uses += 1
                rule.last_used = utcnow()
                session.add(rule)
        except (CalendarError, ActionError, KeyError, ValueError) as e:
            a.status, a.error = "failed", str(e)
        session.add(a)
        session.commit()
        self.calendar.invalidate()

    async def undo(self, session: Session, action_id: int) -> Action:
        a = self._get(session, action_id)
        info = self.to_json(a)
        if not info["can_undo"]:
            raise ActionError("This can't be undone any more.")
        await KINDS[a.kind].undo(self, session, json.loads(a.result or "{}"))
        a.status = "undone"
        session.add(a)
        session.commit()
        self.calendar.invalidate()
        return a
