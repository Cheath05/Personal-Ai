"""Read a syllabus (web page, Google Doc, PDF, photo or pasted text) and propose calendar items.

Nothing is added on its own: the AI proposes dated items, you review and tick them, and only then are they
saved. Photos are read with RapidOCR on the hub (free, offline). The text is split into small chunks so the
local models' context fits, and the model's answer is constrained to a JSON schema.
"""

import asyncio
import base64
import io
import json
import logging
import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import httpx
from sqlmodel import Session

from .calendar import KINDS
from .db import SyllabusImport, get_engine
from .router import BrainRouter, NoBrainAvailable

log = logging.getLogger("cardinal.syllabus")

CHUNK_CHARS = 5000
MAX_CHARS = 60000
MAX_FILE_BYTES = 12 * 1024 * 1024
GDOC = re.compile(r"https://docs\.google\.com/document/d/([A-Za-z0-9_-]+)")

SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "time": {"type": "string", "description": "HH:MM 24-hour, a range like 10:30-12:30, or empty"},
            "title": {"type": "string"},
            "kind": {"type": "string", "enum": list(KINDS)},
        },
        "required": ["date", "title", "kind"],
    }}},
    "required": ["items"],
}

PROMPT = """Below is part of a course syllabus{course}. Today is {today}.
List every item in it that has a specific calendar date: assignments or projects due, exams, quizzes,
readings due, no-class days or holidays, and class sessions with a special topic or event.

Rules:
- Only use dates written in the text. If an item only says "Week 5" with no date, skip it.
- Dates without a year belong to the current school term around {today}: pick the year that makes sense.
- "date" is YYYY-MM-DD. "time" is the due or start time as written (e.g. "11:59pm" or "10:30am-12:30pm"), else "".
- kind is one of: due, exam, quiz, reading, no_class, class, event.
- Titles are short, like "Project 1 due" or "Midterm exam". Don't add the course name.
- If nothing in this part has a date, return {{"items": []}}.

Syllabus text:
<<<
{text}
>>>"""


class SyllabusError(Exception):
    pass


# ---------- Getting text ----------

class _TextParser(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextParser()
    p.feed(html)
    return tidy("".join(p.parts))


def tidy(text: str) -> str:
    lines = [re.sub(r"[ \t ]+", " ", ln).strip(" |") for ln in text.splitlines()]
    out, blank = [], False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        return tidy("\n".join(page.extract_text() or "" for page in reader.pages))
    except Exception as e:
        raise SyllabusError(f"Couldn't read that PDF: {e}") from e


_ocr = None


def image_to_text(data: bytes) -> str:
    """OCR on the hub's CPU. Lines are rebuilt from word boxes so table rows stay together."""
    global _ocr
    try:
        from rapidocr import RapidOCR
        if _ocr is None:
            _ocr = RapidOCR()
        result = _ocr(data)
    except Exception as e:
        raise SyllabusError(f"Couldn't read text from that image: {e}") from e
    if not result or result.txts is None or not len(result.txts):
        return ""
    words = []
    for box, txt in zip(result.boxes, result.txts):
        ys = [pt[1] for pt in box]
        xs = [pt[0] for pt in box]
        words.append(((min(ys) + max(ys)) / 2, max(ys) - min(ys), min(xs), txt))
    words.sort()
    lines: list[list] = []
    for w in words:
        if lines and abs(w[0] - lines[-1][0][0]) < max(6, 0.5 * w[1]):
            lines[-1].append(w)
        else:
            lines.append([w])
    return "\n".join("  ".join(t for *_, t in sorted(line, key=lambda w: w[2])) for line in lines)


async def fetch_url(url: str, client: httpx.AsyncClient) -> tuple[str, str]:
    """(text, kind) from a web page, Google Doc or PDF link."""
    m = GDOC.match(url)
    fetch = f"https://docs.google.com/document/d/{m.group(1)}/export?format=txt" if m else url
    try:
        r = await client.get(fetch, follow_redirects=True)
    except httpx.HTTPError as e:
        raise SyllabusError(f"Couldn't open that link: {e}") from e
    ctype = r.headers.get("content-type", "")
    if m and (r.status_code != 200 or "accounts.google.com" in str(r.url) or "text/html" in ctype):
        raise SyllabusError("That Google Doc isn't public, so Cardinal can't open it. In the doc, choose "
                            "File → Download → PDF, then upload the PDF here.")
    if r.status_code != 200:
        raise SyllabusError(f"That page returned {r.status_code}. If it needs a login, save it as a PDF and upload it.")
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        return pdf_to_text(r.content), "pdf"
    if m or "text/plain" in ctype:
        return tidy(r.text), "doc"
    return html_to_text(r.text), "page"


def file_to_text(name: str, data: bytes) -> str:
    lower = name.lower()
    if lower.endswith(".pdf") or data[:4] == b"%PDF":
        return pdf_to_text(data)
    if lower.endswith((".txt", ".md")):
        return tidy(data.decode("utf-8", "replace"))
    if lower.endswith((".html", ".htm")):
        return html_to_text(data.decode("utf-8", "replace"))
    return image_to_text(data)


# ---------- Turning text into dated items ----------

def chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split on blank lines or line breaks so a table row or paragraph isn't cut in half."""
    out, cur = [], ""
    for para in re.split(r"(\n)", text):
        if len(cur) + len(para) > size and cur.strip():
            out.append(cur)
            cur = ""
        cur += para
    if cur.strip():
        out.append(cur)
    return out


def fix_year(d: date, today: date) -> date:
    """Models sometimes pick the wrong year for "Oct 3". Keep dates within the school year around today."""
    for candidate in (d, d.replace(year=today.year), d.replace(year=today.year + 1), d.replace(year=today.year - 1)):
        try:
            if today - timedelta(days=200) <= candidate <= today + timedelta(days=300):
                return candidate
        except ValueError:
            continue
    return d


def parse_date(value: str, today: date) -> date | None:
    """ "2026-10-08", "Oct 8", "Thursday, October 8th", "10/8" all work; the year is filled in sensibly."""
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return fix_year(date.fromisoformat(value[:10]), today)
    except ValueError:
        pass
    if not (MONTH.search(value) or re.search(r"\b\d{1,2}/\d{1,2}\b", value)):
        return None  # "Week 5" or "Tuesday" alone isn't a date
    from dateutil import parser as du
    try:
        found = du.parse(value, default=datetime(today.year, today.month, 1), fuzzy=True)
    except (ValueError, OverflowError):
        return None
    return fix_year(found.date(), today)


MONTH = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\b", re.I)
TIME = re.compile(r"\b(noon|midnight|(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?|(\d{1,2}):(\d{2}))", re.I)


def parse_times(value: str) -> tuple[str, str]:
    """(start, end) as HH:MM from "11:59pm", "10:30am-12:30pm", "14:00", "noon". Empty strings if none."""
    out = []
    for m in TIME.finditer(str(value or "")):
        word, h, mm, ap, h24, m24 = m.groups()
        if word and word.lower() == "noon":
            out.append([12, 0, "p"])
        elif word and word.lower() == "midnight":
            out.append([23, 59, "p"])
        elif ap:
            out.append([int(h), int(mm or 0), ap.lower()])
        else:
            out.append([int(h24), int(m24), None])
    def to24(h: int, ap: str | None) -> int:
        if ap == "p" and h < 12:
            return h + 12
        if ap == "a" and h == 12:
            return 0
        return h

    pairs = [[to24(h, ap), m, ap] for h, m, ap in out[:2]]
    if len(pairs) == 2 and pairs[0][2] is None and pairs[1][2] == "p" and pairs[0][0] + 12 < pairs[1][0]:
        pairs[0][0] += 12  # "1:00-2:30pm" means 13:00-14:30
    times = [f"{h:02d}:{m:02d}" for h, m, _ in pairs if 0 <= h < 24 and 0 <= m < 60]
    return (times[0] if times else "", times[1] if len(times) > 1 else "")


def clean_items(raw: list[dict], today: date) -> list[dict]:
    seen, out = set(), []
    for it in raw:
        d = parse_date(it.get("date", ""), today)
        if d is None:
            continue
        title = re.sub(r"\s+", " ", str(it.get("title", ""))).strip()[:120]
        if not title:
            continue
        start, end = parse_times(it.get("time") or "")
        kind = it.get("kind") if it.get("kind") in KINDS else "event"
        key = (d, title.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"date": d.isoformat(), "time": start, "end": end, "title": title, "kind": kind,
                    "past": d < today})
    return sorted(out, key=lambda x: (x["date"], x["time"]))


DATE_MENTION = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b|\b\d{1,2}/\d{1,2}\b", re.I)


def fill_time(item: dict, text: str) -> dict:
    """Small models often drop a time that's on the same line ("Final exam: Dec 14, 10:30am-12:30pm").
    Take it from the syllabus line that has the item's date and a word from its title, looking only
    between that date and the next date on the line, so one row's "due 11:59pm" isn't borrowed by another."""
    if str(item.get("time") or "").strip():
        return item
    raw = str(item.get("date") or "").strip().lower()
    words = re.findall(r"[a-z0-9]{2,}", str(item.get("title", "")).lower())  # the line must also have the date
    if not raw or not words:
        return item
    forms = {raw}
    try:
        d = date.fromisoformat(raw[:10])
        forms |= {f"{d:%b} {d.day}".lower(), f"{d:%B} {d.day}".lower(), f"{d.month}/{d.day}"}
    except ValueError:
        pass
    for line in text.splitlines():
        low = line.lower()
        if not any(w in low for w in words):
            continue
        for f in forms:
            m = re.search(rf"\b{re.escape(f)}\b", low)
            if not m:
                continue
            after = low[m.end():]
            nxt = DATE_MENTION.search(after)
            start, end = parse_times(after[:nxt.start()] if nxt else after)
            if start:
                return {**item, "time": f"{start}-{end}" if end else start}
    return item


def parse_reply(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    items = data.get("items") if isinstance(data, dict) else data
    return [i for i in items or [] if isinstance(i, dict)]


def _json_check(reply) -> str | None:
    try:
        json.loads(reply.text)
        return None
    except json.JSONDecodeError:
        return "answer wasn't valid JSON"


async def extract(session: Session, router: BrainRouter, text: str, course: str | None, today: date,
                  progress=None) -> tuple[list[dict], str | None]:
    parts = chunks(text[:MAX_CHARS])
    found, brain = [], None
    for n, part in enumerate(parts, 1):
        if progress:
            progress(f"Reading part {n} of {len(parts)}…")
        prompt = PROMPT.format(course=f" for {course}" if course else "", today=f"{today:%A %d %B %Y}", text=part)
        try:
            result = await router.run(session, agent_id="axiom", job="syllabus",
                                      system="You extract dates from course syllabi and answer only with JSON.",
                                      messages=[{"role": "user", "content": prompt}],
                                      check=_json_check, json_schema=SCHEMA)
        except NoBrainAvailable as e:
            raise SyllabusError(str(e)) from e
        brain = result.reply.brain
        found += [fill_time(it, part) for it in parse_reply(result.reply.text)]
    return clean_items(found, today), brain


# ---------- The background job ----------

async def run_import(import_id: int, router: BrainRouter, tz: ZoneInfo, *, url: str | None = None,
                     text: str | None = None, file_name: str | None = None, file_b64: str | None = None,
                     client: httpx.AsyncClient | None = None) -> None:
    client = client or httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "Mozilla/5.0 Cardinal"})
    with Session(get_engine()) as session:
        job = session.get(SyllabusImport, import_id)

        def progress(msg: str) -> None:
            job.detail = msg
            session.add(job)
            session.commit()

        try:
            if url:
                progress("Opening the link…")
                body, _ = await fetch_url(url, client)
            elif file_b64:
                progress("Reading the file…")
                data = base64.b64decode(file_b64.split(",", 1)[-1])
                if len(data) > MAX_FILE_BYTES:
                    raise SyllabusError("That file is over 12 MB.")
                body = await asyncio.to_thread(file_to_text, file_name or "upload", data)
            else:
                body = tidy(text or "")
            if len(body) < 40:
                raise SyllabusError("Couldn't find readable text there. Try a PDF, a clearer photo, or paste the schedule.")
            items, brain = await extract(session, router, body, job.course, datetime.now(tz).date(), progress)
            job.proposals = json.dumps(items)
            job.brain = brain
            job.status = "ready"
            job.detail = (f"Found {len(items)} dated items." if items
                          else "No dated items found. If the schedule is in a table, try a photo or PDF of it.")
        except SyllabusError as e:
            job.status, job.detail = "failed", str(e)
        except Exception as e:  # keep a clear message in the app instead of a stuck "reading"
            log.exception("Syllabus import %s failed", import_id)
            job.status, job.detail = "failed", f"Something went wrong: {e}"
        session.add(job)
        session.commit()
