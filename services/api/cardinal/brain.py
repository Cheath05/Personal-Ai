"""Axiom's Second Brain: your school files, searchable, summarized, and turned into flashcards and practice tests.

- Reading keeps page numbers (PDF pages, slide numbers), so every answer can cite "Lecture 5, p. 12".
- Search is SQLite FTS5 (built in, BM25 ranking): no extra model or service.
- The local model writes summaries, key terms, flashcards and questions as JSON; answers cite numbered sources.
- Flashcards are scheduled with FSRS (the `fsrs` package): Again / Hard / Good / Easy sets when you see it next.
"""

import asyncio
import io
import json
import logging
import random
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from fsrs import Card, Rating, Scheduler
from sqlalchemy import text as sql
from sqlmodel import Session, col, select

from .db import Document, Flashcard, Passage, Quiz, get_engine, utcnow
from .router import BrainRouter, NoBrainAvailable
from .syllabus import SyllabusError, fetch_url, html_to_text, image_to_text, tidy

log = logging.getLogger("cardinal.brain")

PASSAGE_CHARS = 900
PAGE_CHARS = 3000  # "pages" for formats without real pages (notes, Word, web pages)
DIGEST_CHUNKS = 4
DIGEST_CHARS = 4500
MAX_FILE_BYTES = 40 * 1024 * 1024
COURSE = re.compile(r"\b([A-Z]{2,5}\s?\d{3}[A-Z]?)\b")  # "CMSC 341", "MATH221"
STOP = set("""a an and are as at be but by can could did do does for from had has have how i if in into is it its
me my no not of on or our so than that the their them then there these they this to too was we were what when
where which who why will with would you your about explain tell give show please define""".split())

_scheduler = Scheduler()


class BrainError(Exception):
    pass


# ---------- Reading files into pages ----------

def _chunks(text: str, size: int) -> list[str]:
    out, cur = [], ""
    for para in re.split(r"\n\s*\n|\n", text):
        para = para.strip()
        if not para:
            continue
        if cur and len(cur) + len(para) + 1 > size:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{para}" if cur else para
        while len(cur) > size * 1.5:  # one giant paragraph
            out.append(cur[:size])
            cur = cur[size:]
    if cur.strip():
        out.append(cur)
    return out


def _xml_text(xml: str, tag: str) -> str:
    paras = re.split(r"</(?:w|a):p>", xml)
    lines = []
    for p in paras:
        runs = re.findall(rf"<{tag}(?:\s[^>]*)?>([^<]*)</{tag}>", p)
        line = "".join(runs).strip()
        if line:
            lines.append(line)
    from html import unescape
    return unescape("\n".join(lines))


def pages_from_file(name: str, data: bytes) -> tuple[str, list[str]]:
    """(kind, pages). Each page is plain text; page numbers are list positions + 1."""
    lower = name.lower()
    if lower.endswith(".pdf") or data[:4] == b"%PDF":
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(data))
            return "pdf", [tidy(p.extract_text() or "") for p in reader.pages]
        except Exception as e:
            raise BrainError(f"Couldn't read that PDF: {e}") from e
    if lower.endswith(".pptx"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                            key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
            pages = []
            for n in slides:
                t = _xml_text(z.read(n).decode("utf-8", "replace"), "a:t")
                notes = n.replace("slides/slide", "notesSlides/notesSlide")
                if notes in z.namelist():
                    extra = _xml_text(z.read(notes).decode("utf-8", "replace"), "a:t")
                    if extra:
                        t += f"\n(Speaker notes) {extra}"
                pages.append(tidy(t))
        return "slides", pages
    if lower.endswith(".docx"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            body = _xml_text(z.read("word/document.xml").decode("utf-8", "replace"), "w:t")
        return "doc", _chunks(tidy(body), PAGE_CHARS)
    if lower.endswith((".txt", ".md", ".markdown")):
        return "text", _chunks(tidy(data.decode("utf-8", "replace")), PAGE_CHARS)
    if lower.endswith((".html", ".htm")):
        return "text", _chunks(html_to_text(data.decode("utf-8", "replace")), PAGE_CHARS)
    if lower.endswith((".png", ".jpg", ".jpeg", ".heic", ".webp", ".bmp", ".gif")) or data[:3] in (b"\xff\xd8\xff", b"\x89PN"):
        try:
            return "image", [image_to_text(data)]
        except SyllabusError as e:
            raise BrainError(str(e)) from e
    raise BrainError("Cardinal reads PDF, PowerPoint (.pptx), Word (.docx), text, web pages and photos.")


def passages_from_pages(pages: list[str]) -> list[tuple[int, str]]:
    out = []
    for i, page in enumerate(pages, 1):
        for piece in _chunks(page, PASSAGE_CHARS):
            if len(piece.strip()) >= 20:
                out.append((i, piece.strip()))
    return out


def guess_course(pages: list[str]) -> str | None:
    head = "\n".join(pages[:3])[:6000]
    m = COURSE.search(head)
    return re.sub(r"\s+", " ", m.group(1)) if m else None


# ---------- Search ----------

def fts_query(q: str) -> str | None:
    words = [w for w in re.findall(r"[a-z0-9]+", q.lower()) if len(w) > 2 and w not in STOP][:12]
    if not words:
        return None
    return " OR ".join(f'"{w}"*' for w in dict.fromkeys(words))


def search(session: Session, q: str, *, course: str | None = None, limit: int = 6) -> list[dict]:
    query = fts_query(q)
    if not query:
        return []
    rows = session.exec(sql(
        "SELECT rowid, document_id, page, bm25(passage_fts) AS score, "
        "snippet(passage_fts, 0, '[', ']', ' … ', 24) AS snip "
        "FROM passage_fts WHERE passage_fts MATCH :q ORDER BY score LIMIT :n"
    ).bindparams(q=query, n=limit * 4)).all()
    docs = {d.id: d for d in session.exec(select(Document)).all()}
    out, per_doc = [], {}
    for rowid, doc_id, page, score, snip in rows:
        d = docs.get(int(doc_id))
        if not d or (course and (d.course or "").lower() != course.lower()):
            continue
        if per_doc.get(d.id, 0) >= 3:  # spread results across documents
            continue
        per_doc[d.id] = per_doc.get(d.id, 0) + 1
        p = session.get(Passage, int(rowid))
        out.append({"passage_id": int(rowid), "document_id": d.id, "title": d.title, "course": d.course,
                    "page": int(page), "score": float(score), "snippet": snip, "text": p.text if p else snip})
        if len(out) >= limit:
            break
    return out


def notes_context(session: Session, q: str, limit: int = 4) -> tuple[str, list[dict]]:
    """For Axiom's chat: the best passages as numbered sources, if any match."""
    hits = search(session, q, limit=limit)
    if not hits:
        return "", []
    lines = [f"[{i}] {h['title']}, p. {h['page']}: {h['text'][:700]}" for i, h in enumerate(hits, 1)]
    return ("From the user's notes (cite as [1], [2] when you use them; if they don't answer the question, say so):\n"
            + "\n".join(lines)), hits


# ---------- Documents ----------

def storage_dir() -> Path:
    from .config import ROOT
    d = ROOT / "data" / "files"
    d.mkdir(parents=True, exist_ok=True)
    return d


def index_pages(session: Session, doc: Document, pages: list[str]) -> int:
    doc.pages = len(pages)
    n = 0
    for page, text_ in passages_from_pages(pages):
        p = Passage(document_id=doc.id, page=page, position=n, text=text_)
        session.add(p)
        session.flush()
        session.exec(sql("INSERT INTO passage_fts(rowid, text, document_id, page) VALUES (:r, :t, :d, :p)")
                     .bindparams(r=p.id, t=text_, d=doc.id, p=page))
        n += 1
    session.add(doc)
    session.commit()
    return n


def delete_document(session: Session, doc_id: int) -> None:
    doc = session.get(Document, doc_id)
    if not doc:
        return
    session.exec(sql("DELETE FROM passage_fts WHERE document_id = :d").bindparams(d=doc_id))
    for p in session.exec(select(Passage).where(Passage.document_id == doc_id)).all():
        session.delete(p)
    for c in session.exec(select(Flashcard).where(Flashcard.document_id == doc_id)).all():
        session.delete(c)
    if doc.path:
        Path(doc.path).unlink(missing_ok=True)
    session.delete(doc)
    session.commit()


def document_pages(session: Session, doc_id: int) -> list[dict]:
    rows = session.exec(select(Passage).where(Passage.document_id == doc_id).order_by(col(Passage.position))).all()
    pages: dict[int, list[str]] = {}
    for p in rows:
        pages.setdefault(p.page, []).append(p.text)
    return [{"page": k, "text": "\n\n".join(v)} for k, v in sorted(pages.items())]


def doc_json(session: Session, d: Document) -> dict:
    cards = session.exec(select(Flashcard).where(Flashcard.document_id == d.id)).all()
    return {**d.model_dump(exclude={"path", "terms"}), "terms": json.loads(d.terms or "[]"), "cards": len(cards)}


# ---------- Axiom's digest: summary, key terms, flashcards ----------

DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "points": {"type": "array", "items": {"type": "string"}},
        "terms": {"type": "array", "items": {"type": "object", "properties": {
            "term": {"type": "string"}, "definition": {"type": "string"}, "page": {"type": "string"}},
            "required": ["term", "definition"]}},
        "cards": {"type": "array", "items": {"type": "object", "properties": {
            "front": {"type": "string"}, "back": {"type": "string"}, "page": {"type": "string"}},
            "required": ["front", "back"]}},
    },
    "required": ["points", "terms", "cards"],
}

DIGEST_PROMPT = """This is part of a course document ({title}{course}). Page markers look like [p. 3].

{text}

Answer as JSON:
- points: 3-5 short bullet points of what this part teaches.
- terms: up to 5 key terms with a one-sentence definition from the text, and the page they're on.
- cards: up to 6 flashcards that test understanding (not trivia), each with the page. Front is a question; back is a
  short answer from the text.
Use only what the text says."""


def _parse(text_: str) -> dict:
    try:
        return json.loads(text_)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text_, re.DOTALL)
        try:
            return json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            return {}


def _page(value, default: int) -> int:
    m = re.search(r"\d+", str(value or ""))
    return int(m.group(0)) if m else default


def digest_chunks(pages: list[str], n: int = DIGEST_CHUNKS) -> list[tuple[str, int]]:
    """Up to n pieces spread across the document (start, middle, end), each with page markers."""
    marked = [(i, f"[p. {i}] {p}") for i, p in enumerate(pages, 1) if p.strip()]
    groups, cur, first = [], "", None
    for i, p in marked:
        if cur and len(cur) + len(p) > DIGEST_CHARS:
            groups.append((cur, first))
            cur, first = "", None
        cur += ("\n" if cur else "") + p[:DIGEST_CHARS]
        first = first or i
    if cur:
        groups.append((cur, first))
    if len(groups) <= n:
        return groups
    step = (len(groups) - 1) / (n - 1)
    return [groups[round(k * step)] for k in range(n)]


async def digest(session: Session, router: BrainRouter, doc: Document, pages: list[str], progress=None) -> None:
    points, terms, cards = [], [], []
    chunks = digest_chunks(pages)
    for k, (chunk, first_page) in enumerate(chunks, 1):
        if progress:
            progress(f"Axiom is reading part {k} of {len(chunks)}…")
        try:
            r = await router.run(session, agent_id="axiom", job="notes_digest",
                                 system="You turn course material into study notes and answer only with JSON.",
                                 messages=[{"role": "user", "content": DIGEST_PROMPT.format(
                                     title=doc.title, course=f", {doc.course}" if doc.course else "", text=chunk)}],
                                 json_schema=DIGEST_SCHEMA)
        except NoBrainAvailable as e:
            raise BrainError(str(e)) from e
        data = _parse(r.reply.text)
        points += [str(p).strip() for p in data.get("points", []) if str(p).strip()][:5]
        terms += [{"term": str(t.get("term", "")).strip()[:80], "definition": str(t.get("definition", "")).strip()[:300],
                   "page": _page(t.get("page"), first_page)} for t in data.get("terms", []) if isinstance(t, dict) and t.get("term")][:5]
        cards += [{"front": str(c.get("front", "")).strip()[:300], "back": str(c.get("back", "")).strip()[:500],
                   "page": _page(c.get("page"), first_page)} for c in data.get("cards", [])
                  if isinstance(c, dict) and c.get("front") and c.get("back")][:6]
    if progress:
        progress("Writing the summary…")
    summary = ""
    if points:
        try:
            r = await router.run(session, agent_id="axiom", job="notes_overview",
                                 system="You write short, clear study summaries.",
                                 messages=[{"role": "user", "content": "Key points from a course document:\n- "
                                            + "\n- ".join(points) + "\n\nWrite a 3-4 sentence summary a student could "
                                            "read before class. Plain text, no lists."}])
            summary = r.reply.text.strip()
        except NoBrainAvailable:
            summary = " ".join(points[:4])
    seen = set()
    doc.terms = json.dumps([t for t in terms if not (t["term"].lower() in seen or seen.add(t["term"].lower()))])
    doc.summary = summary
    for c in cards:
        session.add(Flashcard(document_id=doc.id, course=doc.course, front=c["front"], back=c["back"], page=c["page"]))
    session.add(doc)
    session.commit()


async def ingest(doc_id: int, router: BrainRouter, *, data: bytes | None = None, text_: str | None = None,
                 url: str | None = None) -> None:
    """The background job for one document: read, index (searchable right away), then digest."""
    with Session(get_engine()) as session:
        doc = session.get(Document, doc_id)

        def progress(msg: str) -> None:
            doc.detail = msg
            session.add(doc)
            session.commit()

        try:
            if data is not None:
                progress("Reading the file…")
                kind, pages = await asyncio.to_thread(pages_from_file, doc.filename or "file", data)
            elif url:
                progress("Opening the link…")
                import httpx
                async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "Mozilla/5.0 Cardinal"}) as client:
                    body, _ = await fetch_url(url, client)
                kind, pages = "link", _chunks(body, PAGE_CHARS)
            else:
                kind, pages = "text", _chunks(tidy(text_ or ""), PAGE_CHARS)
            if sum(len(p) for p in pages) < 40:
                raise BrainError("Couldn't find readable text. If it's a scan, try a clearer photo.")
            doc.kind = kind if doc.kind in ("file", None) else doc.kind
            doc.course = doc.course or guess_course(pages)
            n = index_pages(session, doc, pages)
            doc.status = "summarizing"
            progress(f"Searchable now ({n} passages). Axiom is writing notes and flashcards…")
            await digest(session, router, doc, pages, progress)
            doc.status, doc.detail = "ready", None
        except (BrainError, SyllabusError) as e:
            doc.status, doc.detail = ("failed" if not doc.pages else "ready"), str(e)
        except Exception as e:
            log.exception("Document %s failed", doc_id)
            doc.status, doc.detail = ("failed" if not doc.pages else "ready"), f"Something went wrong: {e}"
        session.add(doc)
        session.commit()


# ---------- Ask your notes ----------

LEVELS = {
    "simple": "Explain simply, as to someone new to the subject: plain words, one everyday analogy, no jargon.",
    "class": "Explain at the level of the course: the terms and ideas the class uses, with a short example.",
    "deep": "Explain in depth: the reasoning, how it connects to related ideas, edge cases, and a worked example.",
}


async def ask(session: Session, router: BrainRouter, axiom, question: str, *, level: str = "class",
              course: str | None = None, memory: str = "") -> dict:
    hits = search(session, question, course=course, limit=6)
    sources = "\n\n".join(f"[{i}] {h['title']} (p. {h['page']}):\n{h['text']}" for i, h in enumerate(hits, 1))
    style = LEVELS.get(level, LEVELS["class"])
    if hits:
        prompt = (f"Question: {question}\n\nSources from my notes:\n{sources}\n\n{style}\n"
                  "Answer from the sources. After each sentence that uses a source, cite it like [1] or [2][3]. "
                  "If the sources don't cover something, say it isn't in the notes, then answer briefly from general "
                  "knowledge and mark that part (general knowledge).")
    else:
        prompt = (f"Question: {question}\n\n{style}\nNothing in my notes matched. Start with \"This isn't in your notes, "
                  "so this is general knowledge:\" and then answer.")
    try:
        r = await router.run(session, agent_id="axiom", job="notes_answer",
                             system=axiom.system_prompt(memory=memory), messages=[{"role": "user", "content": prompt}])
    except NoBrainAvailable as e:
        raise BrainError(str(e)) from e
    cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", r.reply.text) if 0 < int(n) <= len(hits)})
    return {"answer": r.reply.text, "level": level, "brain": r.reply.brain,
            "sources": [{**h, "n": i, "cited": i in cited} for i, h in enumerate(hits, 1)]}


# ---------- Flashcards (FSRS) ----------

RATINGS = {"again": Rating.Again, "hard": Rating.Hard, "good": Rating.Good, "easy": Rating.Easy}


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def due_cards(session: Session, *, course: str | None = None, limit: int = 30) -> list[Flashcard]:
    q = select(Flashcard).where(Flashcard.suspended == False, Flashcard.due <= utcnow())  # noqa: E712
    if course:
        q = q.where(Flashcard.course == course)
    return list(session.exec(q.order_by(col(Flashcard.due)).limit(limit)).all())


def review(session: Session, card_id: int, rating: str) -> Flashcard:
    c = session.get(Flashcard, card_id)
    if not c or rating not in RATINGS:
        raise BrainError("No such card or rating.")
    state = json.loads(c.fsrs or "{}")
    card = Card.from_dict(state) if state else Card()
    card, _ = _scheduler.review_card(card, RATINGS[rating], review_datetime=datetime.now(UTC))
    c.fsrs = json.dumps(card.to_dict(), default=str)
    c.due = _aware(card.due)
    c.reviews += 1
    c.lapses += rating == "again"
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def card_json(session: Session, c: Flashcard, titles: dict[int, str] | None = None) -> dict:
    titles = titles or {}
    return {**c.model_dump(exclude={"fsrs"}), "due": _aware(c.due).isoformat(),
            "title": titles.get(c.document_id) if c.document_id else None}


def card_stats(session: Session) -> dict:
    now = utcnow()
    cards = session.exec(select(Flashcard).where(Flashcard.suspended == False)).all()  # noqa: E712
    due = [c for c in cards if _aware(c.due) <= now]
    return {"total": len(cards), "due": len(due), "new": sum(1 for c in cards if c.reviews == 0),
            "courses": sorted({c.course for c in due if c.course})}


# ---------- Practice tests (exam mode) ----------

QUIZ_SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {"type": "object", "properties": {
        "question": {"type": "string"},
        "right_answer": {"type": "string", "description": "the full text of the correct answer"},
        "wrong_answers": {"type": "array", "items": {"type": "string"}, "description": "3 full-text false answers"},
        "explanation": {"type": "string"},
        "source": {"type": "string", "description": "the source label, like S2"}},
        "required": ["question", "right_answer", "wrong_answers", "explanation", "source"]}}},
    "required": ["questions"],
}
CHECK_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string", "enum": ["A", "B", "C", "D"]}},
                "required": ["answer"]}


def _real_answer(text_: str) -> bool:
    """An answer is words, not a stray label like "A" or "S2"."""
    t = text_.strip()
    return len(t) >= 3 and not re.fullmatch(r"(?i)(option\s*)?[A-D]|S?\d+|true|false", t)


async def verify(session: Session, router: BrainRouter, q: dict, passage: str) -> bool:
    """Solve the question again from the source alone. Keep it only if the answer key agrees.
    Small models sometimes mark the wrong option correct; a second, independent pass catches most of that."""
    opts = "\n".join(f"{'ABCD'[i]}. {o}" for i, o in enumerate(q["options"]))
    try:
        r = await router.run(session, agent_id="axiom", job="quiz_check",
                             system="You answer multiple-choice questions strictly from the given material. Answer only with JSON.",
                             messages=[{"role": "user", "content": f"Material:\n{passage}\n\nQuestion: {q['question']}\n{opts}\n\n"
                                                                    "Which option is correct according to the material?"}],
                             json_schema=CHECK_SCHEMA)
    except NoBrainAvailable:
        return False
    return str(_parse(r.reply.text).get("answer", "")).strip().upper()[:1] == "ABCD"[q["answer"]]


async def write_quiz(quiz_id: int, router: BrainRouter, *, count: int = 8) -> None:
    with Session(get_engine()) as session:
        quiz = session.get(Quiz, quiz_id)
        try:
            docs = session.exec(select(Document).where(Document.pages > 0)).all()
            docs = [d for d in docs if not quiz.course or (d.course or "").lower() == quiz.course.lower()]
            if not docs:
                raise BrainError("No documents for that course yet. Add some to the library first.")
            passages = session.exec(select(Passage).where(Passage.document_id.in_([d.id for d in docs]))).all()
            passages = [p for p in passages if len(p.text) > 200]
            random.shuffle(passages)
            titles = {d.id: d.title for d in docs}
            questions: list[dict] = []
            batch = 4
            for start in range(0, min(len(passages), count * 3), batch * 2):
                if len(questions) >= count:
                    break
                group = passages[start:start + batch * 2]
                labels = {f"S{i + 1}": p for i, p in enumerate(group)}  # not letters: those get confused with answers
                material = "\n\n".join(f"[{k}] ({titles[p.document_id]}, p. {p.page}) {p.text}" for k, p in labels.items())
                want = min(batch, count - len(questions))
                quiz.detail = f"Writing questions {len(questions) + 1}–{len(questions) + want} of {count}…"
                session.add(quiz)
                session.commit()
                r = await router.run(session, agent_id="axiom", job="quiz",
                                     system="You write fair exam questions from course material and answer only with JSON.",
                                     messages=[{"role": "user", "content": f"""Course material:
{material}

Write {want} multiple-choice questions that test understanding of this material, like a real exam.
For each: the question; "right_answer": the full text of the answer that is true according to the material;
"wrong_answers": 3 full-text answers that are plausible but false; a one-sentence explanation of why the right
answer is right; and the source label (like S2). Answers are sentences or phrases, never letters.
Answer as JSON."""}], json_schema=QUIZ_SCHEMA)
                for q in _parse(r.reply.text).get("questions", [])[:want]:
                    correct = str(q.get("right_answer") or q.get("correct") or "").strip()
                    wrong = [str(w).strip() for w in (q.get("wrong_answers") or q.get("wrong") or [])
                             if str(w).strip() and str(w).strip() != correct][:3]
                    if (not _real_answer(correct) or len(wrong) != 3 or not all(_real_answer(w) for w in wrong)
                            or not str(q.get("question", "")).strip()):
                        continue
                    opts = wrong + [correct]
                    random.shuffle(opts)  # the code places the right answer, so the key can't point at the wrong letter
                    m = re.search(r"S?\s*(\d+)", str(q.get("source", "")), re.I)
                    src = labels.get(f"S{m.group(1)}") if m else None
                    item = {"question": str(q["question"]).strip(), "options": opts, "answer": opts.index(correct),
                            "explanation": str(q.get("explanation", "")).strip(),
                            "document_id": src.document_id if src else None,
                            "title": titles.get(src.document_id) if src else None, "page": src.page if src else None}
                    quiz.detail = f"Checking question {len(questions) + 1}…"
                    session.add(quiz)
                    session.commit()
                    if await verify(session, router, item, src.text if src else material):
                        questions.append(item)
            if not questions:
                raise BrainError("Couldn't write questions from these notes. Try again, or add more material.")
            quiz.questions, quiz.status, quiz.detail = json.dumps(questions), "ready", None
        except NoBrainAvailable as e:
            quiz.status, quiz.detail = "failed", str(e)
        except BrainError as e:
            quiz.status, quiz.detail = "failed", str(e)
        except Exception as e:
            log.exception("Quiz %s failed", quiz_id)
            quiz.status, quiz.detail = "failed", f"Something went wrong: {e}"
        session.add(quiz)
        session.commit()


def grade(session: Session, quiz_id: int, answers: list[int | None], add_missed: bool = True) -> Quiz:
    quiz = session.get(Quiz, quiz_id)
    if not quiz or quiz.status not in ("ready", "done"):
        raise BrainError("That test isn't ready.")
    qs = json.loads(quiz.questions)
    answers = (answers + [None] * len(qs))[:len(qs)]
    score = sum(1 for q, a in zip(qs, answers) if a == q["answer"])
    if add_missed and quiz.status == "ready":
        for q, a in zip(qs, answers):
            if a != q["answer"]:
                session.add(Flashcard(document_id=q.get("document_id"), course=quiz.course, front=q["question"],
                                      back=f"{q['options'][q['answer']]}. {q['explanation']}".strip(),
                                      page=q.get("page"), source="quiz"))
    quiz.answers, quiz.score, quiz.status, quiz.finished_at = json.dumps(answers), score, "done", utcnow()
    session.add(quiz)
    session.commit()
    session.refresh(quiz)
    return quiz


def quiz_json(q: Quiz, reveal: bool = False) -> dict:
    qs = json.loads(q.questions or "[]")
    if not reveal and q.status != "done":
        qs = [{k: v for k, v in x.items() if k not in ("answer", "explanation")} for x in qs]
    return {**q.model_dump(exclude={"questions", "answers"}), "questions": qs, "answers": json.loads(q.answers or "[]")}
