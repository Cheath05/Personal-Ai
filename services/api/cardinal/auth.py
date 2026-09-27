"""Passkey login (Face ID, Touch ID, Windows Hello).

The lock is off until you turn it on: add a passkey on each device (Access → Security), then Lock Cardinal.
While locked, every /api route needs a signed-in browser except the ones in OPEN: signing in itself, the health
check the hub's updater uses, the Google sign-in return, and the endpoints scripts call with their own tokens
(Apple Health and ActivityWatch data in, the backup copy out).

- Only each passkey's public key is stored. The browser keeps a random session token in an HttpOnly cookie;
  the database has its hash.
- Turning the lock off needs a passkey check in the last 5 minutes, not just an old session.
- Another device: a signed-in device makes a one-time code (10 minutes, 5 tries) that lets it add its own passkey.
- Lost every passkey? On the hub: `.venv/bin/python -m cardinal.admin unlock` (see admin.py).
"""

import hashlib
import hmac
import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from . import prefs
from .config import get_settings
from .db import LoginSession, Passkey, get_engine, get_session, utcnow

COOKIE = "cardinal_session"
SESSION_DAYS = 60
FRESH = timedelta(minutes=5)
CODE_TTL = timedelta(minutes=10)
CODE_TRIES = 5
CEREMONY_SECONDS = 300
OPEN = {"/api/health", "/api/health/ingest", "/api/apps/ingest", "/api/backups/download", "/api/google/callback",
        "/api/auth/status",
        "/api/auth/login/options", "/api/auth/login/verify", "/api/auth/register/options", "/api/auth/register/verify"}
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I to misread

router = APIRouter()
_ceremonies: dict[str, tuple[bytes, float, str]] = {}  # id -> (challenge, expires, kind); one process, so memory is fine


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def origin_and_rp() -> tuple[str, str]:
    u = urlparse(get_settings().public_url)
    return f"{u.scheme}://{u.netloc}", u.hostname or "localhost"


def locked(session: Session) -> bool:
    return prefs.get(session, "login_required") == "1"


def current(session: Session, request: Request) -> LoginSession | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    s = session.exec(select(LoginSession).where(LoginSession.token_hash == _hash(token))).first()
    now = utcnow()
    if not s or _aware(s.expires) < now:
        return None
    if now - _aware(s.last_seen) > timedelta(minutes=10):  # sliding expiry, written at most every 10 minutes
        s.last_seen, s.expires = now, now + timedelta(days=SESSION_DAYS)
        session.add(s)
        session.commit()
        session.refresh(s)
    return s


def _start_session(session: Session, response: Response, passkey: Passkey, device: str | None) -> LoginSession:
    token = secrets.token_urlsafe(32)
    s = LoginSession(token_hash=_hash(token), passkey_id=passkey.id, device=(device or passkey.name)[:40],
                     expires=utcnow() + timedelta(days=SESSION_DAYS))
    session.add(s)
    session.commit()
    session.refresh(s)
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", path="/",
                        secure=get_settings().public_url.startswith("https://"))
    return s


def _user_handle(session: Session) -> bytes:
    handle = prefs.get(session, "webauthn_user_id")
    if not handle:
        handle = bytes_to_base64url(secrets.token_bytes(16))
        prefs.put(session, "webauthn_user_id", handle)
    return base64url_to_bytes(handle)


def _remember(challenge: bytes, kind: str) -> str:
    now = time.monotonic()
    for k in [k for k, (_, exp, _) in _ceremonies.items() if exp < now]:
        del _ceremonies[k]
    cid = secrets.token_urlsafe(16)
    _ceremonies[cid] = (challenge, now + CEREMONY_SECONDS, kind)
    return cid


def _take(cid: str, kind: str) -> bytes:
    challenge, exp, k = _ceremonies.pop(cid, (b"", 0.0, ""))
    if k != kind or exp < time.monotonic():
        raise HTTPException(400, "That sign-in took too long. Try again.")
    return challenge


# ---------- One-time codes for adding another device ----------

def new_code(session: Session) -> tuple[str, datetime]:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    expires = utcnow() + CODE_TTL
    prefs.put(session, "login_code", json.dumps({"hash": _hash(raw), "expires": expires.isoformat(), "tries": 0}))
    return f"{raw[:4]}-{raw[4:]}", expires


def code_ok(session: Session, code: str | None, *, consume: bool) -> bool:
    stored = json.loads(prefs.get(session, "login_code") or "null")
    if not code or not stored or datetime.fromisoformat(stored["expires"]) < utcnow() or stored["tries"] >= CODE_TRIES:
        return False
    given = "".join(ch for ch in code.upper() if ch.isalnum())
    if not hmac.compare_digest(_hash(given), stored["hash"]):
        stored["tries"] += 1
        prefs.put(session, "login_code", json.dumps(stored))
        return False
    if consume:
        prefs.put(session, "login_code", "null")
    return True


# ---------- The gate ----------

async def gate(request: Request, call_next):
    """While locked, /api needs a signed-in browser (and same-origin writes). Pages and files stay public:
    they hold no data, and the app has to load to show the sign-in screen."""
    path = request.url.path
    if path.startswith("/api/") and path not in OPEN:
        with Session(get_engine()) as session:
            if locked(session):
                if not current(session, request):
                    return JSONResponse({"detail": "Cardinal is locked. Sign in with your passkey.", "locked": True},
                                        status_code=401)
                origin = request.headers.get("origin")
                if origin and request.method not in ("GET", "HEAD", "OPTIONS") and origin != origin_and_rp()[0]:
                    return JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
    return await call_next(request)


def status_json(session: Session, request: Request) -> dict:
    s = current(session, request)
    keys = session.exec(select(Passkey)).all()
    return {"locked": locked(session), "signed_in": bool(s), "passkeys": len(keys),
            "device": s.device if s else None,
            "fresh": bool(s and utcnow() - _aware(s.verified_at) < FRESH)}


def _need_session(session: Session, request: Request) -> LoginSession | None:
    """Security settings need a signed-in browser whenever the lock is on."""
    s = current(session, request)
    if locked(session) and not s:
        raise HTTPException(401, "Sign in with your passkey first.")
    return s


# ---------- Endpoints ----------

@router.get("/api/auth/status")
def auth_status(request: Request, session: Session = Depends(get_session)):
    return status_json(session, request)


class RegisterAsk(BaseModel):
    code: str | None = Field(default=None, max_length=20)


@router.post("/api/auth/register/options")
def register_options(body: RegisterAsk, request: Request, session: Session = Depends(get_session)):
    if locked(session) and not current(session, request) and not code_ok(session, body.code, consume=False):
        raise HTTPException(401, "Adding a passkey needs a signed-in device, or a one-time code from one.")
    _, rp_id = origin_and_rp()
    name = get_settings().user_name or "Cardinal user"
    opts = generate_registration_options(
        rp_id=rp_id, rp_name="Cardinal", user_name=name, user_display_name=name, user_id=_user_handle(session),
        authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.REQUIRED,
                                                               user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(p.credential_id))
                             for p in session.exec(select(Passkey)).all()])
    return {"ceremony": _remember(opts.challenge, "register"), "options": json.loads(options_to_json(opts))}


class CeremonyIn(BaseModel):
    ceremony: str = Field(max_length=64)
    credential: dict
    name: str = Field(default="", max_length=40)
    code: str | None = Field(default=None, max_length=20)


@router.post("/api/auth/register/verify")
def register_verify(body: CeremonyIn, request: Request, response: Response, session: Session = Depends(get_session)):
    challenge = _take(body.ceremony, "register")
    if locked(session) and not current(session, request) and not code_ok(session, body.code, consume=True):
        raise HTTPException(401, "That one-time code is used up or expired. Make a new one on a signed-in device.")
    origin, rp_id = origin_and_rp()
    try:
        v = verify_registration_response(credential=body.credential, expected_challenge=challenge,
                                         expected_rp_id=rp_id, expected_origin=origin, require_user_verification=True)
    except InvalidRegistrationResponse as e:
        raise HTTPException(400, f"That passkey didn't check out: {e}") from e
    device = request.headers.get("x-cardinal-device")
    pk = Passkey(credential_id=bytes_to_base64url(v.credential_id), public_key=bytes_to_base64url(v.credential_public_key),
                 sign_count=v.sign_count, name=(body.name or device or "Passkey").strip()[:40],
                 synced=v.credential_backed_up, last_used=utcnow())
    session.add(pk)
    session.commit()
    session.refresh(pk)
    s = current(session, request)
    if s:  # already signed in: this passkey now vouches for the session too
        s.passkey_id, s.verified_at = pk.id, utcnow()
        session.add(s)
        session.commit()
    else:
        _start_session(session, response, pk, device)
    return {"passkey": passkey_json(pk)}


@router.post("/api/auth/login/options")
def login_options(session: Session = Depends(get_session)):
    if not session.exec(select(Passkey)).first():
        raise HTTPException(409, "No passkeys yet. Add one in Access → Security.")
    _, rp_id = origin_and_rp()
    # No allow-list: the device offers the passkeys it has for this site (they're discoverable).
    opts = generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED)
    return {"ceremony": _remember(opts.challenge, "login"), "options": json.loads(options_to_json(opts))}


@router.post("/api/auth/login/verify")
def login_verify(body: CeremonyIn, request: Request, response: Response, session: Session = Depends(get_session)):
    challenge = _take(body.ceremony, "login")
    pk = session.exec(select(Passkey).where(Passkey.credential_id == str(body.credential.get("id", "")))).first()
    if not pk:
        raise HTTPException(401, "That passkey isn't one of this Cardinal's. It may have been removed.")
    origin, rp_id = origin_and_rp()
    try:
        v = verify_authentication_response(credential=body.credential, expected_challenge=challenge,
                                           expected_rp_id=rp_id, expected_origin=origin,
                                           credential_public_key=base64url_to_bytes(pk.public_key),
                                           credential_current_sign_count=pk.sign_count, require_user_verification=True)
    except InvalidAuthenticationResponse as e:
        raise HTTPException(401, f"Sign-in failed: {e}") from e
    pk.sign_count, pk.last_used, pk.synced = v.new_sign_count, utcnow(), v.credential_backed_up
    session.add(pk)
    session.commit()
    s = current(session, request)
    if s:  # a fresh check on a signed-in browser (before turning the lock off)
        s.passkey_id, s.verified_at = pk.id, utcnow()
        session.add(s)
        session.commit()
    else:
        _start_session(session, response, pk, request.headers.get("x-cardinal-device"))
    return status_json(session, request) | {"signed_in": True, "fresh": True}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response, session: Session = Depends(get_session)):
    s = current(session, request)
    if s:
        session.delete(s)
        session.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


class LockIn(BaseModel):
    on: bool


@router.post("/api/auth/lock")
def set_lock(body: LockIn, request: Request, session: Session = Depends(get_session)):
    s = current(session, request)
    if body.on:
        if not s or not s.passkey_id or not session.get(Passkey, s.passkey_id):
            raise HTTPException(409, "Add a passkey on this device first, so it stays signed in when the lock turns on.")
        prefs.put(session, "login_required", "1")
    else:
        if locked(session) and (not s or utcnow() - _aware(s.verified_at) > FRESH):
            raise HTTPException(401, "Confirm with your passkey first.")
        prefs.put(session, "login_required", "0")
    return status_json(session, request)


@router.post("/api/auth/code")
def make_code(request: Request, session: Session = Depends(get_session)):
    _need_session(session, request)
    code, expires = new_code(session)
    return {"code": code, "expires": expires.isoformat()}


def passkey_json(p: Passkey) -> dict:
    return {"id": p.id, "name": p.name, "synced": p.synced, "created": _aware(p.created).isoformat(),
            "last_used": _aware(p.last_used).isoformat() if p.last_used else None}


@router.get("/api/auth/passkeys")
def list_passkeys(request: Request, session: Session = Depends(get_session)):
    _need_session(session, request)
    me = current(session, request)
    sessions = session.exec(select(LoginSession).where(LoginSession.expires > utcnow())
                            .order_by(col(LoginSession.last_seen).desc())).all()
    return {"passkeys": [passkey_json(p) for p in session.exec(select(Passkey).order_by(col(Passkey.created))).all()],
            "sessions": [{"id": x.id, "device": x.device, "current": bool(me and me.id == x.id),
                          "created": _aware(x.created).isoformat(), "last_seen": _aware(x.last_seen).isoformat()}
                         for x in sessions]}


@router.delete("/api/auth/passkeys/{passkey_id}")
def delete_passkey(passkey_id: int, request: Request, session: Session = Depends(get_session)):
    _need_session(session, request)
    p = session.get(Passkey, passkey_id)
    if not p:
        raise HTTPException(404, "No such passkey.")
    if locked(session) and len(session.exec(select(Passkey)).all()) == 1:
        raise HTTPException(409, "That's the last passkey. Turn the lock off first, or add another.")
    for s in session.exec(select(LoginSession).where(LoginSession.passkey_id == p.id)).all():
        session.delete(s)  # browsers signed in with it are signed out
    session.delete(p)
    session.commit()
    return {"ok": True}


@router.delete("/api/auth/sessions/{session_id}")
def end_session(session_id: int, request: Request, session: Session = Depends(get_session)):
    _need_session(session, request)
    s = session.get(LoginSession, session_id)
    if s:
        session.delete(s)
        session.commit()
    return {"ok": True}
