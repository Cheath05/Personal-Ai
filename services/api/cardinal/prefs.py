"""Your preferences, changeable from chat ("I can't run before 8:30") or the app.

Stored in the database so every device and agent sees the same value. Each has a default from Settings.
"""

import re

from sqlmodel import Session

from .db import Pref, utcnow

HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# key -> (description, default); times are HH:MM, windows are "HH:MM-HH:MM"
KNOWN = {
    "briefing_time": "when Ordinal writes the morning briefing",
    "morning_checkin": "when the morning check-in opens",
    "evening_checkin": "when the evening review opens",
    "study_window": "the hours study blocks may be planned in",
    "run_window": "the hours runs may be planned in",
}


def get(session: Session, key: str, default: str | None = None) -> str | None:
    p = session.get(Pref, key)
    return p.value if p else default


def put(session: Session, key: str, value: str) -> None:
    p = session.get(Pref, key) or Pref(key=key, value=value)
    p.value, p.updated_at = value, utcnow()
    session.add(p)
    session.commit()


def clean_time(value: str) -> str:
    """ "8:30", "08:30", "8.30", "830", "8:30 am", "5 pm" -> "HH:MM". Raises ValueError."""
    v = str(value).strip().lower().replace(".", ":")
    pm, am = v.endswith("pm") or v.endswith("p"), v.endswith("am") or v.endswith("a")
    v = re.sub(r"\s*(am|pm|a|p)$", "", v)
    if re.fullmatch(r"\d{3,4}", v):
        v = f"{v[:-2]}:{v[-2:]}"
    if re.fullmatch(r"\d{1,2}", v):
        v = f"{v}:00"
    m = HHMM.match(v)
    if not m:
        raise ValueError(f"Not a time: {value}")
    h, mm = int(m.group(1)), int(m.group(2))
    if pm and h < 12:
        h += 12
    if am and h == 12:
        h = 0
    return f"{h:02d}:{mm:02d}"


def window(session: Session, key: str, default: tuple[str, str]) -> tuple[str, str]:
    value = get(session, key)
    if value and "-" in value:
        a, b = value.split("-", 1)
        return a, b
    return default


def set_window(session: Session, key: str, earliest: str, latest: str) -> tuple[str, str]:
    a, b = clean_time(earliest), clean_time(latest)
    if a >= b:
        raise ValueError("The earliest time has to be before the latest.")
    put(session, key, f"{a}-{b}")
    return a, b
