"""Google Calendar and Gmail for the personal and UMBC accounts.

Reading: your calendars and inboxes (read-only scopes). Writing: only the "Cardinal" calendar that Cardinal
creates itself (the calendar.app.created scope can't touch any other calendar). Uses the plain REST APIs
over httpx; tokens are encrypted by the Vault before they reach the database.
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
SCOPE_APP_CALENDAR = "https://www.googleapis.com/auth/calendar.app.created"  # only calendars Cardinal made
SCOPE_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"  # Relay's drafts, and sending you approved
SCOPES = ["openid", "email", SCOPE_CALENDAR, SCOPE_GMAIL, SCOPE_APP_CALENDAR, SCOPE_COMPOSE]
CARDINAL_CALENDAR = "Cardinal"

SLOTS = {"personal": "Personal Google", "school": "UMBC Google"}
STATE_TTL = 600
NOISE = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS", "SPAM"}


class GoogleError(Exception):
    pass


class NotFound(GoogleError):
    pass


def _aware(dt: datetime | None) -> datetime | None:
    # SQLite hands datetimes back without a timezone; they were stored as UTC.
    return dt.replace(tzinfo=UTC) if dt and dt.tzinfo is None else dt


def parse_event(item: dict, calendar: str, slot: str, tz: ZoneInfo, color: str | None = None) -> dict | None:
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
            "account": slot, "location": item.get("location"), "color": color, "link": item.get("htmlLink"),
            "id": item.get("id")}


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
                            calendar=SCOPE_CALENDAR in acct.scopes, gmail=SCOPE_GMAIL in acct.scopes,
                            can_write=SCOPE_APP_CALENDAR in acct.scopes, can_draft=SCOPE_COMPOSE in acct.scopes)
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

    async def _request(self, session: Session, acct: GoogleAccount, method: str, url: str, *,
                       params=None, json: dict | None = None) -> dict:
        for attempt in (0, 1):
            token = await self._access_token(session, acct, force=attempt == 1)
            try:
                r = await self.client.request(method, url, params=params, json=json,
                                              headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as e:
                raise GoogleError(f"Couldn't reach Google: {e}") from e
            if r.status_code == 401 and attempt == 0:
                continue
            if r.status_code == 403:
                raise GoogleError("Google refused access. The permission may not have been granted when connecting.")
            if r.status_code == 404:
                raise NotFound("Google couldn't find that item.")
            if r.status_code not in (200, 204):
                raise GoogleError(f"Google API error {r.status_code}")
            return r.json() if r.content else {}
        raise GoogleError("Google rejected the sign-in. Reconnect this account.")

    async def _get(self, session: Session, acct: GoogleAccount, url: str, params=None) -> dict:
        return await self._request(session, acct, "GET", url, params=params)

    # ---------- Reading ----------

    async def events(self, session: Session, acct: GoogleAccount, start: datetime, end: datetime,
                     tz: ZoneInfo) -> list[dict]:
        cals = await self._get(session, acct, f"{CAL}/users/me/calendarList", {"minAccessRole": "reader"})
        out = []
        chosen = [c for c in cals.get("items", []) if (c.get("selected") or c.get("primary"))
                  and c["id"] != acct.cardinal_calendar_id]
        for cal in chosen[:10]:
            data = await self._get(session, acct, f"{CAL}/calendars/{quote(cal['id'], safe='')}/events", {
                "timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                "orderBy": "startTime", "maxResults": 50})
            name = cal.get("summaryOverride") or cal.get("summary") or "Calendar"
            color = cal.get("backgroundColor")
            out += [e for e in (parse_event(i, name, acct.slot, tz, color) for i in data.get("items", [])) if e]
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

    # ---------- Writing (the Cardinal calendar only) ----------

    def can_write(self, acct: GoogleAccount | None) -> bool:
        return bool(acct and SCOPE_APP_CALENDAR in acct.scopes)

    async def _cardinal_calendar(self, session: Session, acct: GoogleAccount, tz: ZoneInfo) -> str:
        if acct.cardinal_calendar_id:
            return acct.cardinal_calendar_id
        cal = await self._request(session, acct, "POST", f"{CAL}/calendars",
                                  json={"summary": CARDINAL_CALENDAR, "timeZone": str(tz),
                                        "description": "Added from Cardinal: your own items and syllabus dates."})
        acct.cardinal_calendar_id = cal["id"]
        session.add(acct)
        session.commit()
        return cal["id"]

    async def put_event(self, session: Session, acct: GoogleAccount, item, tz: ZoneInfo) -> str:
        """Create or update an item's event on the Cardinal calendar. Returns the Google event id."""
        if item.all_day:
            start = {"date": item.start.astimezone(tz).date().isoformat()}
            end_day = max(item.end.astimezone(tz).date(), item.start.astimezone(tz).date() + timedelta(days=1))
            end = {"date": end_day.isoformat()}
        else:
            start = {"dateTime": item.start.isoformat(), "timeZone": str(tz)}
            end = {"dateTime": item.end.isoformat(), "timeZone": str(tz)}
        body = {"summary": item.title, "start": start, "end": end,
                "description": "\n".join(x for x in (item.course and f"Course: {item.course}", item.notes) if x) or None}
        for attempt in (0, 1):
            cal_id = await self._cardinal_calendar(session, acct, tz)
            base = f"{CAL}/calendars/{quote(cal_id, safe='')}/events"
            try:
                if item.google_event_id:
                    ev = await self._request(session, acct, "PUT", f"{base}/{quote(item.google_event_id, safe='')}", json=body)
                else:
                    ev = await self._request(session, acct, "POST", base, json=body)
                return ev["id"]
            except NotFound:
                if attempt:
                    raise
                # The Cardinal calendar was deleted in Google: make a new one and try once more.
                acct.cardinal_calendar_id = None
                item.google_event_id = None
        raise GoogleError("Couldn't save to the Cardinal calendar.")

    async def delete_event(self, session: Session, acct: GoogleAccount, event_id: str) -> None:
        if not acct.cardinal_calendar_id:
            return
        url = f"{CAL}/calendars/{quote(acct.cardinal_calendar_id, safe='')}/events/{quote(event_id, safe='')}"
        try:
            await self._request(session, acct, "DELETE", url)
        except NotFound:
            pass  # already gone


    # ---------- Email for Relay: full messages, drafts in the thread, sending a draft you approved ----------

    def can_draft(self, acct: GoogleAccount | None) -> bool:
        return bool(acct and SCOPE_COMPOSE in acct.scopes)

    async def recent_messages(self, session: Session, acct: GoogleAccount, days: int = 7, limit: int = 25) -> list[dict]:
        listing = await self._get(session, acct, f"{GMAIL}/messages",
                                  {"q": f"in:inbox newer_than:{days}d", "maxResults": limit})
        out = []
        for m in listing.get("messages", []):
            msg = await self._get(session, acct, f"{GMAIL}/messages/{m['id']}", [
                ("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject"),
                ("metadataHeaders", "Date")])
            p = parse_message(msg)
            out.append({**p, "id": msg["id"], "thread_id": msg.get("threadId"), "snippet": msg.get("snippet", ""),
                        "unread": "UNREAD" in msg.get("labelIds", []), "account": acct.slot,
                        "from_raw": next((h["value"] for h in msg.get("payload", {}).get("headers", [])
                                          if h["name"].lower() == "from"), "")})
        return out

    async def full_message(self, session: Session, acct: GoogleAccount, message_id: str) -> dict:
        """Headers and the plain-text body (HTML stripped). Used only to draft a reply you asked for."""
        import base64

        from ..syllabus import html_to_text

        msg = await self._get(session, acct, f"{GMAIL}/messages/{message_id}", {"format": "full"})
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}

        def walk(part, want):
            if part.get("mimeType") == want and part.get("body", {}).get("data"):
                return base64.urlsafe_b64decode(part["body"]["data"] + "==").decode("utf-8", "replace")
            for sub in part.get("parts", []) or []:
                found = walk(sub, want)
                if found:
                    return found
            return None

        payload = msg.get("payload", {})
        body = walk(payload, "text/plain")
        if not body:
            html_body = walk(payload, "text/html")
            body = html_to_text(html_body) if html_body else msg.get("snippet", "")
        return {"id": msg["id"], "thread_id": msg.get("threadId"), "headers": headers, "body": body[:6000]}

    async def create_draft(self, session: Session, acct: GoogleAccount, *, to: str, subject: str, body: str,
                           thread_id: str | None, in_reply_to: str | None, references: str | None) -> str:
        import base64
        from email.message import EmailMessage

        m = EmailMessage()
        m["To"] = to
        m["From"] = acct.email or ""
        m["Subject"] = subject
        if in_reply_to:
            m["In-Reply-To"] = in_reply_to
            m["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
        m.set_content(body)
        raw = base64.urlsafe_b64encode(m.as_bytes()).decode()
        draft = await self._request(session, acct, "POST", f"{GMAIL}/drafts",
                                    json={"message": {"raw": raw, **({"threadId": thread_id} if thread_id else {})}})
        return draft["id"]

    async def delete_draft(self, session: Session, acct: GoogleAccount, draft_id: str) -> None:
        try:
            await self._request(session, acct, "DELETE", f"{GMAIL}/drafts/{draft_id}")
        except NotFound:
            pass

    async def send_draft(self, session: Session, acct: GoogleAccount, draft_id: str) -> str:
        sent = await self._request(session, acct, "POST", f"{GMAIL}/drafts/send", json={"id": draft_id})
        return sent.get("id", "")
