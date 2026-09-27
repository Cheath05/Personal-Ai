"""Action Previews, trust rules and the Activity Log.

Agents never change anything directly. They propose an Action; it runs only when you authorize it, or when a
trust rule you created covers it (and then you still get a note with Undo). Some kinds of action can never be
covered by a rule. This module is the only code path that executes changes.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import Session, col, select

from .calendar import Calendar, CalendarError
from .db import Action, TrustRule, utcnow

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


# ---------- calendar.add_block: put a block on the Cardinal calendar ----------

def _block_preview(p: dict, tz: ZoneInfo) -> dict:
    d = date.fromisoformat(p["date"])
    when = f"{d:%a} {d.day} {d:%b} · {p['start']}–{p['end']}"
    return {"change": "Add to your Cardinal calendar", "before": "Free time", "after": f"{when} · {p['title']}",
            "where": "Cardinal calendar (shows in Google and Apple Calendar)"}


def _block_rule(p: dict, agent_name: str) -> tuple[dict, str]:
    dur = minutes(p["end"]) - minutes(p["start"])
    cap = max(120, -(-dur // 30) * 30)
    earliest, latest = p.get("window", ["08:00", "22:00"])
    cond = {"max_minutes": cap, "earliest": earliest, "latest": latest}
    hours = f"{cap // 60} h" + (f" {cap % 60} min" if cap % 60 else "")
    return cond, (f"{agent_name} may add study blocks of up to {hours} to your Cardinal calendar, "
                  f"between {earliest} and {latest}. Nothing else. You still get a note with Undo.")


def _block_matches(cond: dict, p: dict) -> bool:
    s, e = minutes(p["start"]), minutes(p["end"])
    return (e - s <= cond["max_minutes"] and s >= minutes(cond["earliest"]) and e <= minutes(cond["latest"]))


KINDS = {
    "calendar.add_block": Kind("calendar.add_block", "Add a calendar block", True,
                               _block_preview, _block_rule, _block_matches),
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
            if "date" in p and "start" in p:
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
                            "all_day": False, "title": p["title"], "source": "proposed", "kind": "reading",
                            "calendar": f"Proposed by {self.agent_names.get(a.agent_id, a.agent_id)}",
                            "color": "#a47bff", "deletable": False})
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
            if a.kind == "calendar.add_block":
                item, warning = await self.calendar.add(
                    session, title=p["title"], day=date.fromisoformat(p["date"]), start=p["start"], end=p["end"],
                    kind="reading", course=p.get("course"), notes=p.get("notes"), source=f"action:{a.id}")
                a.result = json.dumps({"item_id": item.id, "warning": warning})
            else:
                raise ActionError(f"No way to run {a.kind} yet.")
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
        result = json.loads(a.result or "{}")
        if a.kind == "calendar.add_block":
            try:
                await self.calendar.delete(session, result["item_id"])
            except CalendarError:
                pass  # already removed by hand
        a.status = "undone"
        session.add(a)
        session.commit()
        self.calendar.invalidate()
        return a
