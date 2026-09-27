// Second Brain: ask your notes (with page citations), flashcard review (FSRS), exam mode,
// the library of school files, and Core Memory as a ring of square dots.
import { api } from "./api.js";

const $ = (id) => document.getElementById(id);
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
const KIND = { pdf: "PDF", slides: "Slides", doc: "Word", image: "Photo", text: "Notes", link: "Link", file: "File" };
const MEM_COLOR = { fact: "#3d8bff", preference: "#22e3c4", pattern: "#ff4fd8" };

export function createBrain({ toast, openDates, goReview }) {
  const st = { data: null, level: "class", poll: null, doc: null, cards: [], ci: 0, quiz: null, quizPoll: null };

  async function refresh() {
    try { st.data = await api.brain(); render(); } catch (e) { $("br-line").textContent = `Couldn't load: ${e.message}`; }
  }

  function render() {
    const d = st.data;
    const busy = d.documents.filter((x) => x.status === "reading" || x.status === "summarizing");
    $("br-line").textContent = `${d.documents.length} file${d.documents.length === 1 ? "" : "s"} · ${d.courses.length} course${d.courses.length === 1 ? "" : "s"} · ${d.cards.due} card${d.cards.due === 1 ? "" : "s"} due`;
    document.querySelectorAll("select.br-course").forEach((sel) => {
      const keep = sel.value;
      const first = sel.options[0];
      sel.replaceChildren(first, ...d.courses.map((c) => Object.assign(el("option", null, c), { value: c })));
      sel.value = d.courses.includes(keep) ? keep : "";
    });
    $("br-course-list").replaceChildren(...d.courses.map((c) => Object.assign(el("option"), { value: c })));
    $("br-due").textContent = d.cards.due;
    $("br-cards-meta").textContent = `${d.cards.total} CARDS`;
    $("br-cards-line").textContent = d.cards.total ? `${d.cards.new} new · ${d.cards.total - d.cards.new} learning.${d.cards.due ? " A few minutes a day keeps them." : " All caught up."}`
      : "Axiom makes cards from every file you add. You can add your own too.";
    $("br-review").disabled = !d.cards.due;
    renderLibrary(d);
    renderQuizzes(d);
    renderRing();
    clearTimeout(st.poll);
    if (busy.length) st.poll = setTimeout(refresh, 3000);  // keep progress fresh while Axiom reads
  }

  // ---------- Library ----------
  function renderLibrary(d) {
    $("br-lib-meta").textContent = `${d.documents.length} FILES`;
    const box = $("br-library");
    if (!d.documents.length) { box.replaceChildren(el("p", "note", "Nothing here yet. Add lecture slides, readings or your own notes.")); return; }
    const groups = {};
    d.documents.forEach((doc) => { (groups[doc.course || "No course"] ||= []).push(doc); });
    box.replaceChildren(...Object.entries(groups).sort(([a], [b]) => (a === "No course") - (b === "No course") || a.localeCompare(b)).map(([course, docs]) => {
      const g = el("div", "lib-group");
      g.append(el("h4", null, course));
      const grid = el("div", "lib-grid");
      docs.forEach((doc) => {
        const c = el("button", `lib-doc ${doc.status}`);
        c.type = "button";
        const head = el("div", "lib-head");
        head.append(el("span", "lib-kind", KIND[doc.kind] || doc.kind), el("span", "lib-pages", doc.pages ? `${doc.pages} p.` : ""));
        c.append(head, el("b", null, doc.title));
        if (doc.status === "ready" || doc.status === "summarizing") c.append(el("p", null, doc.summary ? doc.summary.slice(0, 150) + (doc.summary.length > 150 ? "…" : "") : doc.detail || ""));
        else c.append(el("p", `lib-status ${doc.status}`, doc.detail || doc.status));
        if (doc.cards) c.append(el("small", null, `${doc.cards} flashcards`));
        c.addEventListener("click", () => openDoc(doc.id));
        grid.append(c);
      });
      g.append(grid);
      return g;
    }));
  }

  const drop = $("br-drop");
  ["dragover", "dragenter"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("over")));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    $("br-upload").files.files = e.dataTransfer.files;
    $("br-upload").requestSubmit();
  });
  drop.querySelector("input").addEventListener("change", () => {
    const n = drop.querySelector("input").files.length;
    drop.querySelector("span").textContent = n ? `${n} file${n === 1 ? "" : "s"} chosen. Press Upload.` : "PDF, slides (.pptx), Word (.docx), text, or photos of notes.";
  });

  $("br-upload").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const files = [...f.files.files];
    if (!files.length) { $("br-upload-note").textContent = "Choose a file first."; return; }
    const btn = f.querySelector("button[type=submit]");
    btn.disabled = true;
    let ok = 0;
    for (const [i, file] of files.entries()) {
      $("br-upload-note").textContent = `Uploading ${i + 1} of ${files.length}: ${file.name}…`;
      try { await api.uploadDoc(file, f.course.value.trim()); ok++; } catch (err) { toast(`${file.name}: ${err.message}`); }
    }
    btn.disabled = false;
    f.reset();
    drop.querySelector("span").textContent = "PDF, slides (.pptx), Word (.docx), text, or photos of notes.";
    $("br-upload-note").textContent = ok ? `Added ${ok}. Axiom is reading ${ok === 1 ? "it" : "them"}: searchable in seconds, notes and flashcards in a minute or two.` : "";
    refresh();
  });

  $("br-note-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      await api.addNote({ title: f.title.value || null, url: f.url.value || null, text: f.text.value || null, course: f.course.value || null });
      f.reset();
      f.closest("details").open = false;
      refresh();
    } catch (err) { toast(err.message); }
  });

  // ---------- A document ----------
  async function openDoc(id, page) {
    try { st.doc = await api.doc(id); } catch (e) { toast(e.message); return; }
    renderDocHead();
    showTab(page ? "pages" : "summary", page);
    $("doc-dialog").showModal();
  }

  function renderDocHead() {
    const d = st.doc;
    $("doc-h").textContent = d.title;
    const meta = $("doc-meta");
    const course = el("input", "doc-course");
    Object.assign(course, { value: d.course || "", placeholder: "Course", maxLength: 20 });
    course.setAttribute("list", "br-course-list");
    course.addEventListener("change", async () => { try { st.doc = { ...st.doc, ...(await api.editDoc(d.id, { course: course.value })) }; refresh(); } catch (e) { toast(e.message); } });
    const acts = el("span", "doc-acts");
    if (d.has_file) { const a = el("a", "linkish", "Original file"); a.href = `/api/brain/documents/${d.id}/file`; a.target = "_blank"; acts.append(a); }
    const dates = el("button", "linkish", "Find dates");
    dates.type = "button";
    dates.title = "Look for due dates and exams in this file and add them to your calendar";
    dates.addEventListener("click", async () => { try { const job = await api.docDates(d.id); $("doc-dialog").close(); openDates(job); } catch (e) { toast(e.message); } });
    const del = el("button", "linkish danger-link", "Delete");
    del.type = "button";
    let armed = null;
    del.addEventListener("click", async () => {
      if (!armed) { del.textContent = "Confirm"; armed = setTimeout(() => { del.textContent = "Delete"; armed = null; }, 4000); return; }
      clearTimeout(armed);
      try { await api.deleteDoc(d.id); $("doc-dialog").close(); toast("Deleted, with its flashcards."); refresh(); } catch (e) { toast(e.message); }
    });
    acts.append(dates, del);
    meta.replaceChildren(el("span", "lib-kind", KIND[d.kind] || d.kind), course, el("span", "note small", `${d.pages} pages · ${d.cards} flashcards`), acts);
  }

  function showTab(tab, page) {
    $("doc-tabs").querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
    const d = st.doc, body = $("doc-body");
    if (tab === "summary") {
      const parts = [el("p", "brief", d.summary || (d.status === "ready" ? "No summary." : d.detail || "Axiom is still reading."))];
      if (d.terms.length) {
        const dl = el("dl", "terms");
        d.terms.forEach((t) => { dl.append(el("dt", null, t.term)); const dd = el("dd", null, t.definition); dd.append(pageLink(t.page)); dl.append(dd); });
        parts.push(el("h4", null, "Key terms"), dl);
      }
      body.replaceChildren(...parts);
    } else if (tab === "cards") {
      body.replaceChildren(...(d.flashcards.length ? d.flashcards.map((c) => {
        const row = el("div", "fc-row");
        const q = el("div");
        q.append(el("b", null, c.front), el("p", null, c.back));
        const del = el("button", "linkish", "Remove");
        del.type = "button";
        del.addEventListener("click", async () => { await api.deleteCard(c.id); row.remove(); refresh(); });
        row.append(q, del);
        return row;
      }) : [el("p", "note", "No flashcards from this file yet.")]));
    } else {
      body.replaceChildren(...d.pages_text.map((p) => {
        const sec = el("section", "doc-page");
        sec.id = `doc-page-${p.page}`;
        sec.append(el("span", "page-no", `p. ${p.page}`), el("p", null, p.text));
        return sec;
      }));
      if (page) requestAnimationFrame(() => { const t = $(`doc-page-${page}`); t?.scrollIntoView({ block: "start" }); t?.classList.add("hit"); });
    }
  }
  function pageLink(page) {
    const b = el("button", "linkish page-link", ` p. ${page}`);
    b.type = "button";
    b.addEventListener("click", () => showTab("pages", page));
    return b;
  }
  $("doc-tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) showTab(b.dataset.tab); });

  // ---------- Ask ----------
  $("br-level").addEventListener("click", (e) => {
    const b = e.target.closest("[data-level]");
    if (!b) return;
    st.level = b.dataset.level;
    $("br-level").querySelectorAll("[data-level]").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
  });
  const askBox = $("br-ask").question;
  askBox.addEventListener("input", () => { askBox.style.height = "auto"; askBox.style.height = `${Math.min(askBox.scrollHeight + 2, 85)}px`; });
  askBox.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !window.matchMedia("(pointer: coarse)").matches) { e.preventDefault(); $("br-ask").requestSubmit(); } });

  $("br-ask").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const q = f.question.value.trim();
    if (!q) return;
    const box = $("br-answer");
    box.hidden = false;
    box.replaceChildren(el("p", "note", "Axiom is looking through your notes…"));
    const btn = f.querySelector("button[type=submit]");
    btn.disabled = true;
    try { renderAnswer(await api.askNotes({ question: q, level: st.level, course: f.course.value || null })); }
    catch (err) { box.replaceChildren(el("p", "note", err.message)); } finally { btn.disabled = false; }
  });

  function renderAnswer(a) {
    const box = $("br-answer");
    const p = el("p", "br-answer-text");
    // Turn [1] into buttons that open the cited page.
    a.answer.split(/(\[\d+\])/).forEach((part) => {
      const m = part.match(/^\[(\d+)\]$/);
      const src = m && a.sources.find((s) => s.n === Number(m[1]));
      if (src) {
        const b = el("button", "cite", m[1]);
        b.type = "button";
        b.title = `${src.title}, p. ${src.page}`;
        b.addEventListener("click", () => openDoc(src.document_id, src.page));
        p.append(b);
      } else p.append(document.createTextNode(part));
    });
    const list = el("ol", "sources");
    a.sources.filter((s) => s.cited).forEach((s) => {
      const li = el("li");
      li.value = s.n;
      const b = el("button", "linkish", `${s.title}, p. ${s.page}`);
      b.type = "button";
      b.addEventListener("click", () => openDoc(s.document_id, s.page));
      li.append(b, el("span", "sub", s.snippet.replace(/\[|\]/g, "")));
      list.append(li);
    });
    box.replaceChildren(p);
    if (list.children.length) box.append(el("h4", null, "Sources"), list);
    else if (!a.sources.length) box.append(el("p", "note small", "Nothing in your library matched, so this is general knowledge."));
  }

  // ---------- Flashcard review ----------
  $("br-review").addEventListener("click", async () => {
    try { st.cards = await api.dueCards(); } catch (e) { toast(e.message); return; }
    st.ci = 0;
    showCard(false);
    $("card-dialog").showModal();
  });

  function showCard(revealed) {
    const body = $("card-body");
    const c = st.cards[st.ci];
    if (!c) {
      $("card-h").textContent = "Review";
      body.replaceChildren(el("p", "brief", "All done for now. Cards come back when you're about to forget them."));
      refresh();
      return;
    }
    $("card-h").textContent = `Card ${st.ci + 1} of ${st.cards.length}`;
    const card = el("div", `flash${revealed ? " flipped" : ""}`);
    card.append(el("p", "fq", c.front));
    if (revealed) card.append(el("p", "fa", c.back));
    if (c.title) card.append(el("small", null, `${c.course ? `${c.course} · ` : ""}${c.title}${c.page ? `, p. ${c.page}` : ""}`));
    const row = el("div", "row-actions rate");
    if (!revealed) {
      const show = el("button", "btn primary", "Show answer");
      show.type = "button";
      show.addEventListener("click", () => showCard(true));
      row.append(show);
    } else {
      [["again", "Again"], ["hard", "Hard"], ["good", "Good"], ["easy", "Easy"]].forEach(([r, label]) => {
        const b = el("button", `btn rate-${r}`, label);
        b.type = "button";
        b.addEventListener("click", async () => {
          try { await api.reviewCard(c.id, r); } catch (e) { toast(e.message); return; }
          st.ci += 1;
          showCard(false);
        });
        row.append(b);
      });
    }
    body.replaceChildren(card, row);
  }

  $("br-card-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try { await api.addCard({ front: f.front.value, back: f.back.value, course: f.course.value || null }); f.reset(); toast("Card added. It's due now."); refresh(); }
    catch (err) { toast(err.message); }
  });

  // ---------- Exam mode ----------
  function renderQuizzes(d) {
    const ul = $("br-quizzes");
    ul.replaceChildren(...d.quizzes.map((q) => {
      const li = el("li");
      const info = el("div");
      info.append(el("b", null, `${q.course || "All courses"} · ${q.questions} questions`),
        el("span", "sub", q.status === "done" ? `Score ${q.score}/${q.questions}` : q.status === "failed" ? q.detail : q.status === "writing" ? "Being written…" : "Ready to take"));
      li.append(info);
      if (q.status === "ready" || q.status === "done") {
        const b = el("button", "linkish", q.status === "done" ? "Review" : "Take it");
        b.type = "button";
        b.addEventListener("click", () => openQuiz(q.id));
        li.append(b);
      }
      return li;
    }));
  }

  $("br-quiz-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      const q = await api.startQuiz({ course: f.course.value || null, count: Number(f.count.value) });
      $("br-quiz-note").textContent = "Axiom is writing your test…";
      pollQuiz(q.id);
    } catch (err) { $("br-quiz-note").textContent = err.message; }
  });

  async function pollQuiz(id) {
    clearTimeout(st.quizPoll);
    try {
      const q = await api.quiz(id);
      if (q.status === "writing") { $("br-quiz-note").textContent = q.detail || "Writing…"; st.quizPoll = setTimeout(() => pollQuiz(id), 1500); return; }
      $("br-quiz-note").textContent = q.status === "failed" ? q.detail : "";
      refresh();
      if (q.status === "ready") openQuiz(id);
    } catch (err) { $("br-quiz-note").textContent = err.message; }
  }

  async function openQuiz(id) {
    try { st.quiz = await api.quiz(id); } catch (e) { toast(e.message); return; }
    renderQuiz();
    $("quiz-dialog").showModal();
  }

  function renderQuiz() {
    const q = st.quiz, body = $("quiz-body"), done = q.status === "done";
    $("quiz-h").textContent = done ? `Score: ${q.score} / ${q.questions.length}` : `Practice test${q.course ? ` · ${q.course}` : ""}`;
    const form = el("form", "quiz");
    q.questions.forEach((x, i) => {
      const fs = el("fieldset", done ? (q.answers[i] === x.answer ? "right" : "wrong") : "");
      fs.append(el("legend", null, `${i + 1}. ${x.question}`));
      x.options.forEach((o, j) => {
        const lab = el("label", done && j === x.answer ? "correct" : done && q.answers[i] === j ? "chosen" : "");
        const r = el("input");
        Object.assign(r, { type: "radio", name: `q${i}`, value: j, disabled: done, checked: q.answers[i] === j });
        lab.append(r, el("span", null, o));
        fs.append(lab);
      });
      if (done) {
        const ex = el("p", "explain", x.explanation || "");
        if (x.document_id) {
          const b = el("button", "linkish", ` ${x.title}, p. ${x.page}`);
          b.type = "button";
          b.addEventListener("click", () => { $("quiz-dialog").close(); openDoc(x.document_id, x.page); });
          ex.append(b);
        }
        fs.append(ex);
      }
      form.append(fs);
    });
    if (!done) {
      const row = el("div", "row-actions");
      const submit = el("button", "btn primary", "Finish and grade");
      submit.type = "submit";
      row.append(submit, el("span", "note small", "Questions you miss become flashcards."));
      form.append(row);
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const answers = q.questions.map((_, i) => { const c = form.querySelector(`input[name=q${i}]:checked`); return c ? Number(c.value) : null; });
        try { st.quiz = await api.gradeQuiz(q.id, answers); renderQuiz(); refresh(); body.scrollTop = 0; } catch (err) { toast(err.message); }
      });
    }
    body.replaceChildren(form);
  }

  // ---------- Core Memory ring (Fluctlight) ----------
  async function renderRing() {
    let mems = [];
    try { mems = await api.memory(); } catch { return; }
    $("br-mem-meta").textContent = `${mems.length} THING${mems.length === 1 ? "" : "S"} KNOWN`;
    const svg = $("br-ring");
    const NS = "http://www.w3.org/2000/svg";
    const make = (tag, attrs) => { const n = document.createElementNS(NS, tag); Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v)); return n; };
    const nodes = [make("circle", { cx: 160, cy: 160, r: 118, class: "ring-guide" }), make("circle", { cx: 160, cy: 160, r: 92, class: "ring-guide faint" })];
    const slots = Math.max(48, mems.length);
    // Spread memories evenly around the ring; the empty slots in between show room to learn more.
    const at = new Map(mems.map((m, i) => [Math.round((i * slots) / mems.length), m]));
    for (let i = 0; i < slots; i++) {
      const a = (i / slots) * Math.PI * 2 - Math.PI / 2;
      const m = at.get(i);
      const r = 118;
      const x = 160 + Math.cos(a) * r, y = 160 + Math.sin(a) * r;
      const size = m ? 9 : 4;
      const sq = make("rect", { x: x - size / 2, y: y - size / 2, width: size, height: size, transform: `rotate(${(a * 180) / Math.PI + 90} ${x} ${y})`,
                                class: m ? "mem" : "slot" });
      if (m) {
        sq.style.fill = MEM_COLOR[m.kind] || "#e0e8ff";
        sq.style.opacity = String(0.45 + 0.55 * (m.confidence ?? 1));
        sq.setAttribute("tabindex", "0");
        const t = make("title", {}); t.textContent = m.text; sq.append(t);
        const show = () => { $("br-mem-read").textContent = `${m.text}${m.evidence ? ` (${m.evidence})` : ""}`; svg.querySelectorAll(".mem.on").forEach((n) => n.classList.remove("on")); sq.classList.add("on"); };
        sq.addEventListener("mouseenter", show);
        sq.addEventListener("focus", show);
        sq.addEventListener("click", show);
      }
      nodes.push(sq);
    }
    const count = make("text", { x: 160, y: 158, class: "ring-count" }); count.textContent = String(mems.length);
    const label = make("text", { x: 160, y: 182, class: "ring-label" }); label.textContent = "CORE MEMORY";
    nodes.push(count, label);
    svg.replaceChildren(...nodes);
    if (!mems.length) $("br-mem-read").textContent = "Empty for now. Check in with Delta for a week and Sigma will start finding patterns, or add things yourself.";
    else if ($("br-mem-read").textContent === "–") $("br-mem-read").textContent = "Hover or tap a square.";
  }
  $("br-mem-edit").addEventListener("click", goReview);

  return { refresh, openDoc };
}
