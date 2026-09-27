import io
import json
import time
import zipfile
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from cardinal import brain, main
from cardinal.agents import load_agents
from cardinal.db import Document, Flashcard, Passage, Quiz

from .conftest import FakeClaude, FakeLocal


def tiny_pdf(pages: list[str]) -> bytes:
    """A real PDF with a text layer, one line per page."""
    objs = ["<</Type/Catalog/Pages 2 0 R>>", None]
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET"
        objs.append(f"<</Length {len(stream)}>>\nstream\n{stream}\nendstream")
        content = len(objs)
        objs.append(f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents {content} 0 R/Resources<</Font<</F1 FONT 0 R>>>>>>")
        kids.append(len(objs))
    objs.append("<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")
    font = len(objs)
    objs[1] = f"<</Type/Pages/Kids[{' '.join(f'{k} 0 R' for k in kids)}]/Count {len(kids)}>>"
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o.replace('FONT', str(font))}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    return out + f"trailer\n<</Size {len(objs) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF".encode()


def office_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return buf.getvalue()


PPTX = office_zip({
    "ppt/slides/slide2.xml": "<p:sld><a:p><a:r><a:t>AVL trees keep balance with rotations</a:t></a:r></a:p></p:sld>",
    "ppt/slides/slide1.xml": "<p:sld><a:p><a:r><a:t>CMSC 341 Lecture 7</a:t></a:r></a:p><a:p><a:r><a:t>Balanced trees</a:t></a:r></a:p></p:sld>",
    "ppt/slides/slide10.xml": "<p:sld><a:p><a:r><a:t>Heaps &amp; priority queues</a:t></a:r></a:p></p:sld>",
    "ppt/notesSlides/notesSlide2.xml": "<p:notes><a:p><a:r><a:t>Mention the four rotation cases</a:t></a:r></a:p></p:notes>",
})
DOCX = office_zip({"word/document.xml": "<w:document><w:body><w:p><w:r><w:t>Big-O describes </w:t></w:r><w:r><w:t>growth rates.</w:t></w:r></w:p>"
                                        "<w:p><w:r><w:t>Hashing gives O(1) average lookups.</w:t></w:r></w:p></w:body></w:document>"})


def test_reading_files_keeps_pages():
    kind, pages = brain.pages_from_file("lec.pdf", tiny_pdf(["Stacks and queues in CMSC 341", "Binary search trees"]))
    assert kind == "pdf" and pages == ["Stacks and queues in CMSC 341", "Binary search trees"]
    kind, slides = brain.pages_from_file("lecture7.pptx", PPTX)
    assert kind == "slides" and slides[0] == "CMSC 341 Lecture 7\nBalanced trees"
    assert slides[1].startswith("AVL trees") and "four rotation cases" in slides[1] and slides[2] == "Heaps & priority queues"
    kind, pages = brain.pages_from_file("notes.docx", DOCX)
    assert kind == "doc" and pages == ["Big-O describes growth rates.\nHashing gives O(1) average lookups."]
    assert brain.guess_course(slides) == "CMSC 341"
    with pytest.raises(brain.BrainError):
        brain.pages_from_file("song.mp3", b"ID3....")


def test_search_ranks_and_cites_pages(session):
    doc = Document(title="Lecture 7", course="CMSC 341")
    other = Document(title="Linear algebra notes", course="MATH 221")
    session.add_all([doc, other])
    session.commit()
    brain.index_pages(session, doc, ["Intro and outline", "AVL trees rebalance with rotations after insertions."])
    brain.index_pages(session, other, ["Eigenvalues and eigenvectors of a matrix."])
    hits = brain.search(session, "How do AVL trees rebalance?")
    assert hits[0]["title"] == "Lecture 7" and hits[0]["page"] == 2 and "rotation" in hits[0]["text"]
    assert brain.search(session, "rotations", course="MATH 221") == []
    assert brain.search(session, "what is the") == []  # only stopwords
    brain.delete_document(session, doc.id)
    assert brain.search(session, "rotations") == []
    assert session.exec(select(Passage).where(Passage.document_id == doc.id)).all() == []


DIGEST = json.dumps({"points": ["AVL trees stay balanced", "Rotations fix imbalance"],
                     "terms": [{"term": "AVL tree", "definition": "A self-balancing BST.", "page": "p. 2"}],
                     "cards": [{"front": "What keeps an AVL tree balanced?", "back": "Rotations after inserts.", "page": "2"},
                               {"front": "", "back": "dropped: no front"}]})


def router_with(make_router, *replies):
    return make_router(local={"g14": FakeLocal("g14", replies=list(replies))}, claude=FakeClaude(configured=False))


async def test_ingest_indexes_then_digests(session, make_router, engine):
    doc = Document(title="lecture7", filename="lecture7.pptx")
    session.add(doc)
    session.commit()
    await brain.ingest(doc.id, router_with(make_router, DIGEST, "AVL trees balance themselves with rotations."), data=PPTX)
    session.expire_all()
    d = session.get(Document, doc.id)
    assert (d.status, d.kind, d.course, d.pages) == ("ready", "slides", "CMSC 341", 3)
    assert d.summary.startswith("AVL trees") and json.loads(d.terms)[0]["page"] == 2
    cards = session.exec(select(Flashcard).where(Flashcard.document_id == doc.id)).all()
    assert [c.front for c in cards] == ["What keeps an AVL tree balanced?"] and cards[0].course == "CMSC 341"


async def test_ask_cites_sources(session, make_router):
    doc = Document(title="Lecture 7", course="CMSC 341")
    session.add(doc)
    session.commit()
    brain.index_pages(session, doc, ["AVL trees rebalance with rotations after insertions."])
    out = await brain.ask(session, router_with(make_router, "They use rotations [1]."), load_agents()["axiom"],
                          "How do AVL trees rebalance?", level="simple")
    assert out["sources"][0]["cited"] and out["sources"][0]["page"] == 1 and out["level"] == "simple"
    none = await brain.ask(session, router_with(make_router, "This isn't in your notes..."), load_agents()["axiom"],
                           "Who won the 1998 World Cup?")
    assert none["sources"] == []


def test_fsrs_scheduling(session):
    c = Flashcard(front="Q", back="A")
    session.add(c)
    session.commit()
    assert [x.id for x in brain.due_cards(session)] == [c.id]  # new cards are due now
    good = brain.review(session, c.id, "good")
    assert good.reviews == 1 and brain._aware(good.due) > datetime.now(UTC)
    assert brain.due_cards(session) == []
    again = brain.review(session, c.id, "again")
    assert again.lapses == 1
    with pytest.raises(brain.BrainError):
        brain.review(session, c.id, "meh")


QUIZ = json.dumps({"questions": [
    {"question": "What do AVL trees use to rebalance?", "right_answer": "Rotations",
     "wrong_answers": ["Hashing", "Heaps", "Sorting"], "explanation": "Rotations restore the height balance.", "source": "S1"},
    {"question": "Broken", "right_answer": "only", "wrong_answers": ["two"], "explanation": "", "source": "S1"},
    {"question": "Label as answer", "right_answer": "A", "wrong_answers": ["Hashing", "Heaps", "Sorting"],
     "explanation": "", "source": "S1"}]})


class Checker(FakeLocal):
    """A fake brain that also answers the verification pass: it picks the letter whose option contains `truth`."""

    def __init__(self, truth, *replies):
        super().__init__("g14", replies=list(replies))
        self.truth = truth

    async def chat(self, system, messages, *, max_tokens, effort=None, json_schema=None):
        import re

        from cardinal.brains import BrainReply
        text = messages[-1]["content"]
        if "Which option is correct" in text:
            letter = next((l for l, opt in re.findall(r"^([ABCD])\. (.*)$", text, re.M) if self.truth in opt), "A")
            return BrainReply(text=json.dumps({"answer": letter}), provider="local", brain="g14", model="m")
        return await super().chat(system, messages, max_tokens=max_tokens, json_schema=json_schema)


async def test_practice_test_and_grading(session, make_router, engine):
    doc = Document(title="Lecture 7", course="CMSC 341")
    session.add(doc)
    session.commit()
    brain.index_pages(session, doc, ["AVL trees rebalance with rotations after insertions. " * 6])
    q = Quiz(course="CMSC 341")
    session.add(q)
    session.commit()
    router = make_router(local={"g14": Checker("Rotations", QUIZ)}, claude=FakeClaude(configured=False))
    await brain.write_quiz(q.id, router, count=3)
    session.expire_all()
    q = session.get(Quiz, q.id)
    assert q.status == "ready"
    hidden = brain.quiz_json(q)
    assert "answer" not in hidden["questions"][0] and len(hidden["questions"]) >= 1  # malformed ones dropped
    right = brain.quiz_json(q, reveal=True)["questions"][0]
    assert right["options"][right["answer"]] == "Rotations"  # the code placed the right answer
    done = brain.grade(session, q.id, [(right["answer"] + 1) % 4])  # wrong on purpose
    assert done.score == 0
    missed = session.exec(select(Flashcard).where(Flashcard.source == "quiz")).all()
    assert missed and missed[0].back.startswith("Rotations")


@pytest.fixture
def client(engine, make_router):
    with TestClient(main.app) as c:
        yield c


def wait_for(fn, cond, tries=100):
    for _ in range(tries):
        v = fn()
        if cond(v):
            return v
        time.sleep(0.03)
    return v


def test_library_api(client, make_router, tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "storage_dir", lambda: tmp_path)
    main.app.state.router = router_with(make_router, DIGEST, "A summary.", "Rotations [1].")
    r = client.post("/api/brain/documents", files={"file": ("lecture7.pptx", PPTX, "application/octet-stream")})
    doc = r.json()
    assert r.status_code == 200 and doc["title"] == "lecture7"
    d = wait_for(lambda: client.get(f"/api/brain/documents/{doc['id']}").json(), lambda x: x["status"] == "ready")
    assert d["course"] == "CMSC 341" and d["pages_text"][1]["page"] == 2 and d["has_file"]
    assert client.get(f"/api/brain/documents/{doc['id']}/file").content == PPTX
    over = client.get("/api/brain").json()
    assert over["courses"] == ["CMSC 341"] and over["cards"]["due"] == 1
    ans = client.post("/api/brain/ask", json={"question": "How do AVL trees stay balanced?"}).json()
    assert ans["sources"] and ans["sources"][0]["cited"]
    card = client.get("/api/brain/cards/due").json()[0]
    assert card["title"] == "lecture7"
    assert client.post(f"/api/brain/cards/{card['id']}/review", json={"rating": "easy"}).json()["reviews"] == 1
    assert client.patch(f"/api/brain/documents/{doc['id']}", json={"course": "cmsc 341h"}).json()["course"] == "CMSC 341H"
    client.delete(f"/api/brain/documents/{doc['id']}")
    assert client.get("/api/brain").json()["documents"] == []


def test_axiom_chat_uses_notes(client, make_router, session):
    doc = Document(title="Lecture 7", course="CMSC 341")
    session.add(doc)
    session.commit()
    brain.index_pages(session, doc, ["AVL trees rebalance with rotations after insertions."])
    main.app.state.router = router_with(make_router, "They rebalance with rotations [1].")  # a question: no tool step
    r = client.post("/api/chat", json={"agent_id": "axiom", "message": "How do AVL trees rebalance?"}).json()
    assert r["message"]["sources"] == [{"document_id": doc.id, "title": "Lecture 7", "page": 1,
                                        "snippet": r["message"]["sources"][0]["snippet"], "n": 1}]


def test_notes_quiz_and_dates_start_over_the_api(client, make_router, tmp_path, monkeypatch):
    # These start background jobs, so they must run on the event loop (a sync endpoint would crash).
    main.app.state.router = make_router(local={"g14": Checker("Rotations", DIGEST, "A summary.", QUIZ, json.dumps({"items": []}))},
                                        claude=FakeClaude(configured=False))
    note = client.post("/api/brain/notes", json={"title": "AVL", "text": "AVL trees rebalance with rotations. " * 20,
                                                 "course": "cmsc 341"})
    assert note.status_code == 200 and note.json()["course"] == "CMSC 341"
    wait_for(lambda: client.get(f"/api/brain/documents/{note.json()['id']}").json(), lambda x: x["status"] == "ready")
    quiz = client.post("/api/brain/quizzes", json={"course": "CMSC 341", "count": 3})
    assert quiz.status_code == 200
    q = wait_for(lambda: client.get(f"/api/brain/quizzes/{quiz.json()['id']}").json(), lambda x: x["status"] != "writing")
    assert q["status"] == "ready"
    dates = client.post(f"/api/brain/documents/{note.json()['id']}/dates")
    assert dates.status_code == 200 and dates.json()["course"] == "CMSC 341"


async def test_questions_whose_key_fails_the_check_are_dropped(session, make_router, engine):
    doc = Document(title="Lecture 7", course="CMSC 341")
    session.add(doc)
    session.commit()
    brain.index_pages(session, doc, ["AVL trees rebalance with rotations after insertions. " * 6])
    q = Quiz(course="CMSC 341")
    session.add(q)
    session.commit()
    # The checker believes "Heaps" is right, so it disagrees with the key ("Rotations"): the question is dropped.
    router = make_router(local={"g14": Checker("Heaps", QUIZ)}, claude=FakeClaude(configured=False))
    await brain.write_quiz(q.id, router, count=3)
    session.expire_all()
    assert session.get(Quiz, q.id).status == "failed"
