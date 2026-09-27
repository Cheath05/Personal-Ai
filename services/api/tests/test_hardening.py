import base64
import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import cbor2
import http_ece
import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlmodel import select

from cardinal import admin, auth, backups, main, push
from cardinal.db import Action, EmailItem, LoginSession, Passkey, PushSub

from .conftest import FakeClaude, FakeLocal

ORIGIN = "http://localhost:8000"
NY = ZoneInfo("America/New_York")


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class SoftKey:
    """A software passkey: signs exactly like Face ID or Windows Hello would, so the real checks run."""

    def __init__(self, rp_id="localhost", origin=ORIGIN):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.count = 0
        self.rp_hash = hashlib.sha256(rp_id.encode()).digest()
        self.origin = origin

    def _cose(self) -> bytes:
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def create(self, options: dict) -> dict:
        cdj = json.dumps({"type": "webauthn.create", "challenge": options["challenge"], "origin": self.origin}).encode()
        auth_data = (self.rp_hash + bytes([0x45]) + self.count.to_bytes(4, "big") + b"\0" * 16
                     + len(self.cred_id).to_bytes(2, "big") + self.cred_id + self._cose())
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(cdj), "attestationObject": b64u(att)}}

    def get(self, options: dict, origin: str | None = None) -> dict:
        self.count += 1
        cdj = json.dumps({"type": "webauthn.get", "challenge": options["challenge"], "origin": origin or self.origin}).encode()
        auth_data = self.rp_hash + bytes([0x05]) + self.count.to_bytes(4, "big")
        sig = self.key.sign(auth_data + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(cdj), "authenticatorData": b64u(auth_data),
                             "signature": b64u(sig)}}


@pytest.fixture
def client(engine, make_router, monkeypatch):
    monkeypatch.setenv("CARDINAL_PUBLIC_URL", ORIGIN)
    with TestClient(main.app) as c:
        main.app.state.router = make_router(local={"g14": FakeLocal("g14")}, claude=FakeClaude(configured=False))
        yield c


def register(client, key: SoftKey, name="iPhone", code=None):
    r = client.post("/api/auth/register/options", json={"code": code})
    assert r.status_code == 200, r.text
    o = r.json()
    return client.post("/api/auth/register/verify", json={"ceremony": o["ceremony"], "credential": key.create(o["options"]),
                                                          "name": name, "code": code})


def login(client, key: SoftKey, origin=None):
    o = client.post("/api/auth/login/options").json()
    return client.post("/api/auth/login/verify", json={"ceremony": o["ceremony"], "credential": key.get(o["options"], origin)})


def test_passkey_lock_end_to_end(client, session):
    assert client.get("/api/auth/status").json() == {"locked": False, "signed_in": False, "passkeys": 0,
                                                    "device": None, "fresh": False}
    assert client.get("/api/agents").status_code == 200  # open until you turn the lock on
    phone = SoftKey()
    r = register(client, phone)
    assert r.status_code == 200 and r.json()["passkey"]["name"] == "iPhone"
    assert "cardinal_session" in client.cookies
    stored = session.exec(select(LoginSession)).one()
    assert stored.token_hash != client.cookies["cardinal_session"]  # only the hash is kept
    assert client.post("/api/auth/lock", json={"on": True}).json()["locked"] is True

    client.cookies.clear()  # another browser, not signed in
    r = client.get("/api/agents")
    assert r.status_code == 401 and r.json()["locked"] is True
    assert client.get("/api/health").status_code == 200  # the updater's check stays open
    assert client.post("/api/health/ingest", json={}).json()["detail"].startswith("Missing or wrong")  # its own token
    assert client.get("/").status_code == 200  # the app itself loads, to show the sign-in screen

    assert login(client, SoftKey()).status_code == 401  # a passkey Cardinal never saw
    assert login(client, phone, origin="https://evil.example").status_code == 401  # phished from another site
    o = client.post("/api/auth/login/options").json()
    cred = phone.get(o["options"])
    assert client.post("/api/auth/login/verify", json={"ceremony": o["ceremony"], "credential": cred}).status_code == 200
    assert client.post("/api/auth/login/verify", json={"ceremony": o["ceremony"], "credential": cred}).status_code == 400  # no replay
    assert client.get("/api/agents").status_code == 200
    assert session.exec(select(Passkey)).one().sign_count == 2  # the counter moves on (clones show up as going back)
    assert client.post("/api/push/settings", json={"private": True}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_new_device_needs_a_one_time_code(client, session):
    phone = SoftKey()
    register(client, phone)
    client.post("/api/auth/lock", json={"on": True})
    code = client.post("/api/auth/code").json()["code"]
    client.cookies.clear()
    laptop = SoftKey()
    assert client.post("/api/auth/register/options", json={}).status_code == 401
    assert client.post("/api/auth/register/options", json={"code": "WRONG-CODE"}).status_code == 401
    assert register(client, laptop, name="G14", code=code.lower()).status_code == 200  # typed in lower case is fine
    assert client.get("/api/agents").status_code == 200  # signed in on the new device
    client.cookies.clear()
    assert client.post("/api/auth/register/options", json={"code": code}).status_code == 401  # used up
    for _ in range(auth.CODE_TRIES):  # guessing burns the code
        assert not auth.code_ok(session, "AAAA-AAAA", consume=False)
    assert login(client, laptop).status_code == 200
    names = {p["name"] for p in client.get("/api/auth/passkeys").json()["passkeys"]}
    assert names == {"iPhone", "G14"}


def test_turning_the_lock_off_needs_a_fresh_passkey_check(client, session):
    phone = SoftKey()
    register(client, phone)
    client.post("/api/auth/lock", json={"on": True})
    s = session.exec(select(LoginSession)).one()
    s.verified_at = datetime.now(UTC) - timedelta(minutes=30)
    session.add(s)
    session.commit()
    assert client.post("/api/auth/lock", json={"on": False}).status_code == 401
    only = session.exec(select(Passkey)).one()
    assert client.delete(f"/api/auth/passkeys/{only.id}").status_code == 409  # can't delete the last one while locked
    assert login(client, phone).json()["fresh"] is True  # Face ID again
    assert client.post("/api/auth/lock", json={"on": False}).json()["locked"] is False
    assert admin.main(["admin", "status"]) == 0


def test_admin_unlock_and_code(client, session, capsys):
    register(client, SoftKey())
    client.post("/api/auth/lock", json={"on": True})
    client.cookies.clear()
    assert admin.main(["admin", "code"]) == 0
    assert "One-time code" in capsys.readouterr().out
    admin.main(["admin", "unlock"])
    assert client.get("/api/agents").status_code == 200


def test_security_headers(client):
    page = client.get("/")
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    api = client.get("/api/health")
    assert api.headers["x-content-type-options"] == "nosniff" and api.headers["cache-control"] == "no-store"
    assert "content-security-policy" not in api.headers  # JSON, not a page


# ---------- Notifications ----------

class Device:
    """A browser's push subscription, with the private key to read what arrives."""

    def __init__(self, endpoint="https://web.push.apple.com/abc"):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        pub = self.key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.sub = {"endpoint": endpoint, "keys": {"p256dh": b64u(pub), "auth": b64u(self.auth)}}

    def read(self, body: bytes) -> dict:
        return json.loads(http_ece.decrypt(body, private_key=self.key, auth_secret=self.auth, version="aes128gcm"))


@pytest.fixture
def pushes(monkeypatch, tmp_path):
    sent, status = [], {"code": 201}

    def handler(request):
        sent.append(request)
        return httpx.Response(status["code"])
    monkeypatch.setattr(push, "key_file", lambda: tmp_path / "push.pem")
    monkeypatch.setattr(push, "_vapid", None)
    monkeypatch.setattr(push, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return sent, status


async def test_push_is_encrypted_signed_and_respects_settings(session, pushes, tmp_path):
    sent, status = pushes
    phone = Device()
    push.subscribe(session, phone.sub, "iPhone")
    noon = datetime(2026, 9, 28, 12, tzinfo=NY)
    assert await push.notify(session, "briefing", "Ordinal · Your briefing", "Class at 10.", "/#today", now=noon) == 1
    r = sent[-1]
    assert r.headers["content-encoding"] == "aes128gcm" and r.headers["authorization"].startswith("vapid t=")
    assert r.headers["topic"] == "briefing" and oct((tmp_path / "push.pem").stat().st_mode)[-3:] == "600"
    assert phone.read(r.content) == {"title": "Ordinal · Your briefing", "body": "Class at 10.", "url": "/#today",
                                     "tag": "briefing"}
    assert push.public_key() in r.headers["authorization"]
    assert await push.notify(session, "briefing", "x", "y", now=datetime(2026, 9, 28, 23, 30, tzinfo=NY)) == 0  # quiet hours
    push.save_settings(session, {"briefing": False}, None, True)
    assert await push.notify(session, "briefing", "x", "y", now=noon) == 0
    await push.notify(session, "urgent_mail", "Relay", "Grades posted", now=noon)
    assert phone.read(sent[-1].content)["body"] == "Open Cardinal to see it."  # hidden on the lock screen
    status["code"] = 410  # the phone unsubscribed
    assert await push.notify(session, "test", "t", "b", force=True) == 0
    assert session.exec(select(PushSub)).first() is None


async def test_push_triggers_fire_once(session, pushes, settings):
    sent, _ = pushes
    phone = Device()
    push.subscribe(session, phone.sub, "iPhone")
    morning = datetime(2026, 9, 28, 7, 5, tzinfo=NY)
    await push.tick(session, morning, settings)
    await push.tick(session, morning + timedelta(minutes=1), settings)
    assert [phone.read(r.content)["title"] for r in sent] == ["Delta · Morning check-in"]
    session.add(Action(agent_id="axiom", kind="calendar.add_block", title="Study for CMSC 341", reason="r", payload="{}",
                       ts=datetime.now(UTC) - timedelta(minutes=11)))
    session.add(Action(agent_id="vector", kind="calendar.add_block", title="Tempo run", reason="r", payload="{}"))
    session.commit()
    at = datetime.now(NY).replace(hour=12)
    await push.tick(session, at, settings)
    await push.tick(session, at, settings)
    got = [phone.read(r.content) for r in sent[1:]]
    assert [g["title"] for g in got] == ["2 things need your OK"] and got[0]["body"] == "Study for CMSC 341"
    for n, cat in enumerate(["fyi", "urgent"]):
        session.add(EmailItem(account="personal", message_id=f"m{n}", sender="Prof. Lee", subject="Exam moved",
                              received_at=datetime.now(UTC), category=cat))
        session.commit()
        await push.after_sort(session, at)
    assert phone.read(sent[-1].content)["title"] == "Relay · Urgent from Prof. Lee" and len(sent) == 3


def test_push_api(client, pushes):
    phone = Device()
    assert client.get("/api/push/key").json()["key"]
    assert client.post("/api/push/subscribe", json={"subscription": {"endpoint": "http://x"}}).status_code == 400
    assert client.post("/api/push/subscribe", json={"subscription": phone.sub, "device": "iPhone"}).status_code == 200
    assert client.post("/api/push/test", json={"endpoint": phone.sub["endpoint"]}).json() == {"sent": 1}
    s = client.post("/api/push/settings", json={"quiet": "23:00-07:00", "kinds": {"approvals": False}}).json()
    assert s["quiet"] == "23:00-07:00" and s["kinds"]["approvals"] is False and s["devices"][0]["device"] == "iPhone"
    assert client.post("/api/push/settings", json={"quiet": "late"}).status_code == 422


# ---------- Restore drills ----------

def _db(path, rows=3):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE message (id INTEGER PRIMARY KEY, content TEXT)")
    conn.executemany("INSERT INTO message (content) VALUES (?)", [("hi",)] * rows)
    conn.commit()
    conn.close()


def test_restore_drill(tmp_path, monkeypatch):
    monkeypatch.setattr(backups, "data_dir", lambda: tmp_path / "data")
    live, folder = tmp_path / "live.db", tmp_path / "backups"
    folder.mkdir()
    _db(live, rows=40)
    assert backups.drill(live, folder)["problems"][0].startswith("There are no backups yet")
    _db(folder / "cardinal-2026-09-27.db", rows=39)
    r = backups.drill(live, folder)
    assert r["ok"] and r["integrity"] == "ok" and r["rows"]["message"] == [39, 40]
    later = backups.drill(live, folder, now=datetime.now(UTC) + timedelta(days=3))
    assert not later["ok"] and "hours old" in later["problems"][0]
    (folder / "pre-update-abc1234.db").write_bytes(b"not a database at all" * 100)
    bad = backups.drill(live, folder)
    assert not bad["ok"] and "won't open" in bad["problems"][0]


def test_backup_download_needs_its_secret_even_when_locked(client, monkeypatch, tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    _db(folder / "cardinal-2026-09-27.db")
    monkeypatch.setattr(backups, "backup_dir", lambda: folder)
    token = client.post("/api/backups/token").json()["token"]
    register(client, SoftKey())
    client.post("/api/auth/lock", json={"on": True})
    client.cookies.clear()  # the Mac's copy script has no session, only its secret
    assert client.get("/api/backups/download").status_code == 401
    r = client.get("/api/backups/download", headers={"X-Cardinal-Token": token})
    assert r.status_code == 200 and r.content.startswith(b"SQLite format 3")
    assert client.post("/api/backups/token").status_code == 401  # making a new secret needs a signed-in browser
