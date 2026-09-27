"""Restore drills: proof that the backups would actually bring Cardinal back.

infra/backup-db.sh copies the database (and your uploaded files) to ~/cardinal-backups every night, and the updater
keeps a copy from before each update. Once a week (and from Access → Backups) the newest one is copied to a scratch
folder and checked there, never touching the original or the live database:

- SQLite's own integrity check passes;
- every table the live database has is there, with roughly as many rows (a backup is at most a day behind);
- it's recent (a warning after 36 hours);
- the Google sign-ins in it open with this hub's secret.key (the key is deliberately not in the backups, so a
  stolen backup is useless; if the whole VM were lost you'd reconnect Google once).
"""

import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import data_dir

KEY_TABLES = ("message", "calendaritem", "task", "checkin", "memory", "document", "passage", "flashcard", "run",
              "action", "trustrule", "emailitem", "passkey")
STALE = timedelta(hours=36)


def backup_dir() -> Path:
    return Path.home() / "cardinal-backups"


def list_backups(folder: Path | None = None) -> list[Path]:
    folder = folder or backup_dir()
    if not folder.is_dir():
        return []
    files = [p for p in folder.glob("*.db") if p.name.startswith(("cardinal-", "pre-update-"))]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                                         "AND name NOT LIKE 'passage_fts%'")]
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}


def drill(live_db: Path, folder: Path | None = None, key_file: Path | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    backups = list_backups(folder)
    out: dict = {"checked_at": now.isoformat(), "ok": False, "problems": [], "notes": []}
    if not backups:
        out["problems"].append("There are no backups yet. The first nightly one runs at 03:30.")
        return out
    src = backups[0]
    age = now - datetime.fromtimestamp(src.stat().st_mtime, UTC)
    out.update(backup=src.name, size=src.stat().st_size, age_hours=round(age.total_seconds() / 3600, 1))
    if age > STALE:
        out["problems"].append(f"The newest backup is {out['age_hours']:.0f} hours old. Check cardinal-backup.timer.")
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "restore-test.db"
        shutil.copy2(src, copy)
        try:
            conn = sqlite3.connect(copy)
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            restored = _counts(conn)
            tokens = [r[0] for r in conn.execute("SELECT refresh_token_enc FROM googleaccount LIMIT 3")] \
                if "googleaccount" in restored else []
            conn.close()
        except sqlite3.DatabaseError as e:
            out["problems"].append(f"The backup won't open: {e}")
            return out
    out["integrity"] = check
    if check != "ok":
        out["problems"].append(f"SQLite's integrity check failed: {check}")
    live_conn = sqlite3.connect(f"file:{live_db}?mode=ro", uri=True)
    live = _counts(live_conn)
    live_conn.close()
    missing = sorted(t for t in live if t not in restored)
    if missing:  # tables added by an update since this backup are expected; say so rather than fail
        out["notes"].append(f"Newer than this backup: {', '.join(missing)}.")
    out["rows"] = {t: [restored.get(t, 0), live.get(t, 0)] for t in KEY_TABLES if t in live}
    shrunk = [t for t, (b, lv) in out["rows"].items() if lv >= 20 and b < lv * 0.5]
    if shrunk:
        out["problems"].append(f"Far fewer rows in the backup than now: {', '.join(shrunk)}.")
    out["tables"] = len(restored)
    key_file = key_file or data_dir() / "secret.key"
    if tokens:
        try:
            f = Fernet(key_file.read_bytes().strip())
            for t in tokens:
                f.decrypt(t.encode())
            out["notes"].append("Google sign-ins in the backup open with this hub's key.")
        except (OSError, InvalidToken, ValueError):
            out["notes"].append("Google sign-ins in the backup don't open with this hub's key: after a restore, "
                                "reconnect Google in Access → Accounts.")
    files = data_dir() / "files"
    if files.is_dir() and any(files.iterdir()):
        saved = (folder or backup_dir()) / "files"
        n_live = sum(1 for _ in files.rglob("*") if _.is_file())
        n_saved = sum(1 for _ in saved.rglob("*") if _.is_file()) if saved.is_dir() else 0
        out["files"] = [n_saved, n_live]
        if n_saved < n_live:
            out["notes"].append(f"{n_live - n_saved} uploaded file(s) aren't in the backup yet (they're copied nightly).")
    out["ok"] = not out["problems"]
    return out
