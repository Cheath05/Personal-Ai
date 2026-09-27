"""Google Calendar and Gmail, read-only, for the personal and UMBC accounts.

Uses the plain REST APIs over httpx. Tokens are encrypted by the Vault before they reach the database.
Changing anything (calendar blocks, sending mail) arrives with Action Previews in Phase 2b.
"""

import secrets
import time
from datetime import UTC, date, datetime, timedelta
from email.utils import parseaddr
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import httpx
from sqlmodel import Session, select

from ..config import Settings
from ..db import GoogleAccount, utcnow
from ..vault import Vault, VaultError

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CAL = "https://www.googleapis.com/calendar/v3"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"

SCOPE_CALENDAR = "https://www.googleapis.com/auth/calendar.readonly"
SCOPE_GMAIL = "https://www.googleapis.com/auth/gmail.readonly"
SCOPES = ["openid", "email", SCOPE_CALENDAR, SCOPE_GMAIL]

SLOTS = {"personal": "Personal Google", "school": "UMBC Google"}
STATE_TTL = 600
NOISE = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS", "SPAM"}


class GoogleError(Exception):
    pass


def _aware(dt: datetime | None) -> datetime | None:
    # SQLite hands datetimes back without a timezone; they were stored as UTC.
    return dt.replace(tzinfo=UTC) if dt and dt.tzinfo is None else dt


def parse_event(item: dict, calendar: str, slot: str, tz: ZoneInfo) -> dict | None:
    if item.get("status") == "cancelled":
        return None
    if any(a.get("self") and a.get("responseStatus") == "declined" for a in item.get("attendees", [])):
        return None
    start, end = item.get("start", {}), item.get("end", {})
    if "date" in start:
        s = datetime.combine(date.fromisoformat(start["date"]), datetime.min.time(), tz)
        e = datetime.combine(date.fromisoformat(end.get("date", start["date"])), datetime.min.time(), tz)
        all_day = True
    elif "dateTime" in start:
        s = datetime.fromisoformat(start["dateTime"]).astimezone(tz)
        e = datetime.fromisoformat(end.get("dateTime", start["dateTime"])).astimezone(tz)
        all_day = False
    else:
        return None
    return {"start": s.isoformat(), "end": e.isoformat(), "all_day": all_day,
            "title": (item.get("summary") or "(no title)").strip(), "calendar": calendar,
            "account": slot, "location": item.get("location")}


def parse_message(msg: dict) -> dict:
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    name, addr = parseaddr(headers.get("from", ""))
    labels = set(msg.get("labelIds", []))
    return {"from": name or addr or "Unknown", "subject": (headers.get("subject") or "(no subject)").strip(),
            "ts": datetime.fromtimestamp(int(msg.get("internalDate", 0)) / 1000, UTC).isoformat(),
            "important": "IMPORTANT" in labels, "noise": bool(labels & NOISE)}


class Google:
    def __init__(self, settings: Settings, vault: Vault, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.vault = vault
        self.client = client or httpx.AsyncClient(timeout=15.0)
        self._states: dict[str, tuple[str, float]] = {}

    @property
    def configured(self) -> bool:
        return bool(self.settings.google_client_id and self.settings.google_client_secret)

    @property
    def redirect_uri(self) -> str:
        return f"{self.settings.public_url.rstrip('/')}/api/google/callback"

    # ---------- Connecting ----------

    def auth_url(self, slot: str) -> str:
        if slot not in SLOTS:
            raise GoogleError(f"Unknown account slot: {slot}")
        if not self.configured:
            raise GoogleError("Google isn't set up on the hub yet (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET).")
        now = time.time()
        self._states = {k: v for k, v in self._states.items() if v[1] > now}
        state = secrets.token_urlsafe(24)
        self._states[state] = (slot, now + STATE_TTL)
        params = {"client_id": self.settings.google_client_id, "redirect_uri": self.redirect_uri,
                  "response_type": "code", "scope": " ".join(SCOPES), "access_type": "offline",
                  "prompt": "consent", "include_granted_scopes": "true", "state": state}
        return f"{AUTH_URL}?{urlencode(params)}"

    async def finish(self, session: Session, code: str, state: str) -> GoogleAccount:
        slot, expires = self._states.pop(state, (None, 0))
        if not slot or expires < time.time():
            raise GoogleError("This sign-in link expired or was already used. Start again from the Today view.")
        tok = await self._token_request({"grant_type": "authorization_code", "code": code,
                                         "redirect_uri": self.redirect_uri})
        if "refresh_token" not in tok:
            raise GoogleError("Google didn't return a long-lived token. Remove Cardinal at "
                              "myaccount.google.com/permissions and connect again.")
        r = await self.client.get(USERINFO_URL, headers={"Authorization": f"Bearer {tok['access_token']}"})
        email = r.json().get("email") if r.status_code == 200 else None

        acct = session.exec(select(GoogleAccount).where(GoogleAccount.slot == slot)).first() or GoogleAccount(
            slot=slot, refresh_token_enc="")
        acct.email = email
        acct.refresh_token_enc = self.vault.encrypt(tok["refresh_token"])
        acct.access_token_enc = self.vault.encrypt(tok["access_token"])
        acct.access_expires = utcnow() + timedelta(seconds=int(tok.get("expires_in", 3600)) - 60)
        acct.scopes = tok.get("scope", "")
        acct.connected_at = utcnow()
        acct.last_error = None
        session.add(acct)
        session.commit()
        session.refresh(acct)
        return acct

    async def disconnect(self, session: Session, slot: str) -> None:
        acct = self.account(session, slot)
        if not acct:
            return
        try:
            await self.client.post(REVOKE_URL, data={"token": self.vault.decrypt(acct.refresh_token_enc)})
        except (httpx.HTTPError, VaultError):
            pass  # removing it here is what matters; Google also lists it at myaccount.google.com/permissions
        session.delete(acct)
        session.commit()

    def account(self, session: Session, slot: str) -> GoogleAccount | None:
        return session.exec(select(GoogleAccount).where(GoogleAccount.slot == slot)).first()

    def status(self, session: Session) -> list[dict]:
        out = []
        for slot, label in SLOTS.items():
            acct = self.account(session, slot)
            item = {"slot": slot, "label": label, "connected": bool(acct), "configured": self.configured}
            if acct:
                item.update(email=acct.email, error=acct.last_error,
                            calendar=SCOPE_CALENDAR in acct.scopes, gmail=SCOPE_GMAIL in acct.scopes)
            out.append(item)
        return out

    # ---------- Tokens ----------

    async def _token_request(self, data: dict) -> dict:
        data = {**data, "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret}
        try:
            r = await self.client.post(TOKEN_URL, data=data)
        except httpx.HTTPError as e:
            raise GoogleError(f"Couldn't reach Google: {e}") from e
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200:
            if body.get("error") == "invalid_grant":
                raise GoogleError("Google sign-in expired or was revoked. Reconnect this account.")
            raise GoogleError(f"Google token error: {body.get('error_description') or body.get('error') or r.status_code}")
        return body

    async def _access_token(self, session: Session, acct: GoogleAccount, force: bool = False) -> str:
        expires = _aware(acct.access_expires)
        if not force and acct.access_token_enc and expires and expires > utcnow():
            return self.vault.decrypt(acct.access_token_enc)
        try:
            tok = await self._token_request({"grant_type": "refresh_token",
                                             "refresh_token": self.vault.decrypt(acct.refresh_token_enc)})
        except (GoogleError, VaultError) as e:
            acct.last_error = str(e)
            session.add(acct)
            session.commit()
            raise GoogleError(str(e)) from e
        acct.access_token_enc = self.vault.encrypt(tok["access_token"])
        acct.access_expires = utcnow() + timedelta(seconds=int(tok.get("expires_in", 3600)) - 60)
        acct.last_error = None
        session.add(acct)
        session.commit()
        return tok["access_token"]

    async def _get(self, session: Session, acct: GoogleAccount, url: str, params=None) -> dict:
        for attempt in (0, 1):
            token = await self._access_token(session, acct, force=attempt == 1)
            try:
                r = await self.client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as e:
                raise GoogleError(f"Couldn't reach Google: {e}") from e
            if r.status_code == 401 and attempt == 0:
                continue
            if r.status_code == 403:
                raise GoogleError("Google refused access. The permission may not have been granted when connecting.")
            if r.status_code != 200:
                raise GoogleError(f"Google API error {r.status_code}")
            return r.json()
        raise GoogleError("Google rejected the sign-in. Reconnect this account.")

    # ---------- Reading ----------

    async def events(self, session: Session, acct: GoogleAccount, start: datetime, end: datetime,
                     tz: ZoneInfo) -> list[dict]:
        cals = await self._get(session, acct, f"{CAL}/users/me/calendarList", {"minAccessRole": "reader"})
        out = []
        for cal in [c for c in cals.get("items", []) if c.get("selected") or c.get("primary")][:10]:
            data = await self._get(session, acct, f"{CAL}/calendars/{quote(cal['id'], safe='')}/events", {
                "timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                "orderBy": "startTime", "maxResults": 50})
            name = cal.get("summaryOverride") or cal.get("summary") or "Calendar"
            out += [e for e in (parse_event(i, name, acct.slot, tz) for i in data.get("items", [])) if e]
        return sorted(out, key=lambda e: e["start"])

    async def inbox(self, session: Session, acct: GoogleAccount, limit: int = 8) -> dict:
        label = await self._get(session, acct, f"{GMAIL}/labels/INBOX")
        listing = await self._get(session, acct, f"{GMAIL}/messages",
                                  {"q": "in:inbox is:unread newer_than:2d", "maxResults": limit * 2})
        recent = []
        for m in listing.get("messages", []):
            msg = await self._get(session, acct, f"{GMAIL}/messages/{m['id']}", [
                ("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject")])
            parsed = parse_message(msg)
            if not parsed["noise"]:
                recent.append(parsed)
            if len(recent) >= limit:
                break
        return {"account": acct.slot, "email": acct.email, "unread": int(label.get("messagesUnread", 0)),
                "recent": recent}
