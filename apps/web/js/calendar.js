// The Today calendar: one day on a time grid, a week of days around it,
// adding and removing your own items, and importing a syllabus (the AI proposes, you confirm).
import { api } from "./api.js";

const $ = (id) => document.getElementById(id);
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

const BANDS = [ // time-of-day tint behind the grid
  { from: 0, to: 6, name: "Night", cls: "night" },
  { from: 6, to: 12, name: "Morning", cls: "morning" },
  { from: 12, to: 17, name: "Afternoon", cls: "afternoon" },
  { from: 17, to: 21, name: "Evening", cls: "evening" },
  { from: 21, to: 24, name: "Night", cls: "night" },
];
const KIND_LABEL = { event: "Event", class: "Class", due: "Due", exam: "Exam", quiz: "Quiz", reading: "Reading / study", no_class: "No class" };
const STRIP_BACK = 3, STRIP_AHEAD = 3;  // a week around the day you're looking at

const addDays = (iso, n) => { const d = new Date(`${iso}T12:00:00Z`); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };

export function createCalendar({ toast, onChange, onProposal }) {
  const st = { date: null, data: null, tz: "America/New_York", syl: null, sylTimer: null };
  const fmt = (opts) => new Intl.DateTimeFormat([], { timeZone: st.tz, ...opts });
  const hm = () => fmt({ hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const minutesOf = (iso) => {
    const p = Object.fromEntries(fmt({ hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date(iso)).map((x) => [x.type, x.value]));
    return Number(p.hour) * 60 + Number(p.minute);
  };
  const dayOf = (iso) => new Intl.DateTimeFormat("en-CA", { timeZone: st.tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(iso));
  const longDay = (iso) => fmt({ weekday: "long", day: "numeric", month: "long" }).format(new Date(`${iso}T12:00:00Z`));

  // ---------- Loading ----------
  async function load(date, refresh = false) {
    st.date = date || st.date || null;
    $("cal-note").textContent = "";
    try {
      const d = await api.calendarDay(st.date || "", refresh);
      st.data = d;
      st.date = d.date;
      st.tz = d.timezone;
      render();
    } catch (e) {
      $("cal-note").textContent = e.message;
    }
  }

  // ---------- Rendering ----------
  function render() {
    const d = st.data;
    const isToday = d.date === d.today;
    $("cal-title").textContent = longDay(d.date);
    $("cal-meta").textContent = isToday ? "TODAY" : d.date < d.today ? "PAST" : "UPCOMING";
    $("cal-prev").disabled = d.date <= d.min_date;
    $("cal-next").disabled = d.date >= d.max_date;
    $("cal-today").disabled = isToday;
    renderStrip(d);
    renderAllDay(d);
    renderGrid(d, isToday);
    const notes = d.errors.map((e) => `${e.source}: ${e.detail}`);
    if (!d.can_sync) notes.push("Items you add are saved in Cardinal. Reconnect Personal Google in Access → Accounts to also put them in Google and Apple Calendar.");
    $("cal-note").textContent = notes.join(" ");
  }

  function renderStrip(d) {
    const strip = $("cal-strip");
    const chips = [];
    for (let i = -STRIP_BACK; i <= STRIP_AHEAD; i++) {
      const iso = addDays(d.date, i);
      if (iso < d.min_date || iso > d.max_date) continue;
      const b = el("button", `chip${iso === d.date ? " on" : ""}${iso === d.today ? " now" : ""}${iso < d.today ? " past" : ""}`);
      b.type = "button";
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", String(iso === d.date));
      const dt = new Date(`${iso}T12:00:00Z`);
      b.append(el("span", "dow", new Intl.DateTimeFormat([], { weekday: "short", timeZone: "UTC" }).format(dt)),
               el("b", null, String(dt.getUTCDate())));
      b.addEventListener("click", () => load(iso));
      chips.push(b);
    }
    strip.replaceChildren(...chips);
    const on = strip.querySelector(".on");  // centre the chosen day without scrolling the page itself
    if (on) strip.scrollLeft += on.getBoundingClientRect().left - strip.getBoundingClientRect().left - (strip.clientWidth - on.offsetWidth) / 2;
  }

  function chipFor(e, time) {
    const c = el("button", "allday-chip");
    c.type = "button";
    c.style.setProperty("--ev", e.color || "var(--acc)");
    c.append(el("i"), el("span", null, e.title));
    if (time) c.append(el("small", null, time));
    c.addEventListener("click", () => openEvent(e));
    return c;
  }

  function dedupe(events) {
    const seen = new Map();
    const out = [];
    events.forEach((e) => {
      const key = e.source === "proposed" ? `p${e.action_id}` : `${e.title.trim().toLowerCase()}|${e.start}|${e.end}`;
      const first = seen.get(key);
      if (first) { first.copies = (first.copies || 1) + 1; return; }
      const copy = { ...e };
      seen.set(key, copy);
      out.push(copy);
    });
    return out;
  }

  function renderAllDay(d) {
    const box = $("cal-allday");
    const chips = dedupe(d.events.filter((e) => e.all_day)).map((e) => chipFor(e));
    d.due.forEach((x) => chips.push(chipFor({ ...x, start: x.due, end: x.due, calendar: x.course ? `Blackboard · ${x.course}` : "Blackboard", kind: "due", source: "blackboard" },
      x.all_day ? "due" : `due ${hm().format(new Date(x.due))}`)));
    box.hidden = !chips.length;
    box.replaceChildren(...chips);
  }

  function layoutColumns(evs) {
    // Group overlapping events and give each a column, like a paper planner.
    const sorted = [...evs].sort((a, b) => a.s - b.s || b.e - a.e);
    let group = [], groupEnd = -1;
    const flush = () => { const n = Math.max(...group.map((g) => g.col)) + 1; group.forEach((g) => { g.cols = n; }); group = []; };
    sorted.forEach((ev) => {
      if (group.length && ev.s >= groupEnd) flush();
      const used = new Set(group.filter((g) => g.e > ev.s).map((g) => g.col));
      let col = 0;
      while (used.has(col)) col++;
      ev.col = col;
      group.push(ev);
      groupEnd = Math.max(groupEnd, ev.e);
    });
    if (group.length) flush();
    return sorted;
  }

  function renderGrid(d, isToday) {
    const grid = $("cal-grid");
    const timed = dedupe(d.events.filter((e) => !e.all_day)).map((e) => {
      const s = dayOf(e.start) < d.date ? 0 : minutesOf(e.start);
      const e2 = dayOf(e.end) > d.date ? 24 * 60 : Math.max(minutesOf(e.end), s + 20);
      return { ev: e, s, e: e2 };
    });
    let startH = 6, endH = 24;
    timed.forEach((t) => { startH = Math.min(startH, Math.floor(t.s / 60)); });
    const hourPx = window.innerWidth < 700 ? 46 : 52;
    const top = (min) => ((min - startH * 60) / 60) * hourPx;
    st.top = top;
    grid.style.height = `${(endH - startH) * hourPx}px`;
    const parts = [];

    BANDS.forEach((b) => {
      const from = Math.max(b.from, startH), to = Math.min(b.to, endH);
      if (to <= from) return;
      const band = el("div", `band ${b.cls}`);
      band.style.top = `${top(from * 60)}px`;
      band.style.height = `${(to - from) * hourPx}px`;
      band.append(el("span", null, b.name));
      parts.push(band);
    });
    for (let h = startH; h <= endH; h++) {
      const line = el("div", "hour");
      line.style.top = `${top(h * 60)}px`;
      if (h < endH) line.append(el("span", null, `${String(h).padStart(2, "0")}:00`));
      parts.push(line);
    }
    layoutColumns(timed).forEach((t) => {
      const e = t.ev;
      const b = el("button", `ev${e.source === "cardinal" ? " mine" : ""}${e.source === "proposed" ? " proposed" : ""}`);
      b.type = "button";
      b.style.setProperty("--ev", e.color || "var(--acc)");
      b.style.top = `${top(t.s) + 1}px`;
      const h = Math.max(22, top(t.e) - top(t.s) - 2);
      b.style.height = `${h}px`;
      if (h < 40) b.classList.add("short");
      b.style.left = `calc(var(--gutter) + (100% - var(--gutter)) * ${t.col / t.cols})`;
      b.style.width = `calc((100% - var(--gutter)) / ${t.cols} - 4px)`;
      b.append(el("b", null, e.copies ? `${e.title} ×${e.copies}` : e.title),
        el("small", null, `${hm().format(new Date(e.start))}–${hm().format(new Date(e.end))}${e.calendar ? ` · ${e.calendar}` : ""}`));
      b.title = `${e.title} (${e.calendar || ""})${e.copies ? `, on your calendars ${e.copies} times` : ""}`;
      b.addEventListener("click", () => (e.source === "proposed" && onProposal ? onProposal(e.action_id) : openEvent(e)));
      parts.push(b);
    });
    if (isToday) {
      const now = el("div", "now-line");
      now.style.top = `${top(minutesOf(new Date().toISOString()))}px`;
      parts.push(now);
    }
    if (!timed.length) {
      const empty = el("p", "grid-empty", d.events.length || d.due.length ? "Nothing at a set time." : "Nothing scheduled. Tap + Add to plan something.");
      empty.style.top = `${top(Math.max(startH, 9) * 60)}px`;
      parts.push(empty);
    }
    grid.replaceChildren(...parts);
    // Start scrolled to the first thing today (or now), not midnight.
    const first = timed.length ? Math.min(...timed.map((t) => t.s)) : isToday ? minutesOf(new Date().toISOString()) : 8 * 60;
    $("cal-scroll").scrollTop = Math.max(0, top(first) - hourPx);
  }

  // ---------- Event details ----------
  function openEvent(e) {
    const dlg = $("ev-dialog");
    $("ev-title").textContent = e.title;
    $("ev-swatch").style.background = e.color || "var(--acc)";
    const info = $("ev-info");
    const rows = [];
    const add = (k, v) => { if (v) rows.push(el("dt", null, k), el("dd", null, v)); };
    const when = e.all_day ? `${longDay(dayOf(e.start))} · all day`
      : e.start === e.end ? `${longDay(dayOf(e.start))} · ${hm().format(new Date(e.start))}`
      : `${longDay(dayOf(e.start))} · ${hm().format(new Date(e.start))}–${hm().format(new Date(e.end))}`;
    add("When", when);
    add("Calendar", e.calendar || (e.source === "blackboard" ? "Blackboard" : ""));
    add("Type", e.source === "cardinal" ? KIND_LABEL[e.kind] : "");
    add("Where", e.location);
    add("Notes", e.notes);
    if (e.source === "cardinal") add("In Google", e.synced ? "Yes, on the Cardinal calendar" : "Not yet (reconnect Personal Google)");
    info.replaceChildren(...rows);
    const actions = $("ev-actions");
    const btns = [];
    if (e.deletable && e.item_id) {
      const del = el("button", "btn danger", "Remove");
      del.type = "button";
      del.addEventListener("click", async () => {
        del.disabled = true;
        try { await api.deleteItem(e.item_id); dlg.close(); toast("Removed."); await load(st.date, true); onChange?.(); }
        catch (err) { toast(err.message); del.disabled = false; }
      });
      btns.push(del);
    }
    if (e.link) {
      const a = el("a", "btn", "Open in Google Calendar");
      a.href = e.link; a.target = "_blank"; a.rel = "noopener";
      btns.push(a);
    }
    if (!e.deletable && e.source !== "cardinal") btns.push(el("span", "note small", e.source === "google" ? "Change or delete this in Google Calendar." : "This comes from a linked calendar."));
    actions.replaceChildren(...btns);
    dlg.showModal();
  }

  // ---------- Add an item ----------
  function openAdd() {
    const f = $("add-form");
    f.reset();
    f.date.value = st.date;
    $("add-note").textContent = "";
    $("add-dialog").showModal();
    f.title.focus();
  }
  $("add-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    const body = { title: f.title.value, date: f.date.value, start: f.start.value || null, end: f.end.value || null,
                   kind: f.kind.value, course: f.course.value || null, notes: f.notes.value || null };
    $("add-note").textContent = "Saving…";
    try {
      const r = await api.addItem(body);
      $("add-dialog").close();
      toast(r.warning || "Added to your calendar.");
      await load(body.date, true);
      onChange?.();
    } catch (err) { $("add-note").textContent = err.message; }
  });

  // ---------- Syllabus import ----------
  const sylForm = $("syl-form");
  let tab = "url";
  $("syl-tabs").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-tab]");
    if (!b) return;
    tab = b.dataset.tab;
    $("syl-tabs").querySelectorAll("[data-tab]").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
    sylForm.querySelectorAll("[data-pane]").forEach((p) => { p.hidden = p.dataset.pane !== tab; });
  });

  function openImport() {
    clearTimeout(st.sylTimer);
    sylForm.hidden = false;
    $("syl-review").hidden = true;
    $("syl-status").textContent = "";
    $("syl-go").disabled = false;
    $("syl-dialog").showModal();
  }

  const readFile = (file) => new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(new Error("Couldn't read that file."));
    r.readAsDataURL(file);
  });

  sylForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const body = { course: sylForm.course.value || null };
    try {
      if (tab === "url") { if (!sylForm.url.value) throw new Error("Paste a link first."); body.url = sylForm.url.value.trim(); }
      if (tab === "text") { if (!sylForm.text.value.trim()) throw new Error("Paste some text first."); body.text = sylForm.text.value; }
      if (tab === "file") {
        const file = sylForm.file.files[0];
        if (!file) throw new Error("Choose a photo or PDF first.");
        if (file.size > 12 * 1024 * 1024) throw new Error("That file is over 12 MB.");
        body.file_name = file.name;
        body.file_b64 = await readFile(file);
      }
      $("syl-go").disabled = true;
      $("syl-status").textContent = "Sending…";
      st.syl = await api.startSyllabus(body);
      poll();
    } catch (err) { $("syl-status").textContent = err.message; $("syl-go").disabled = false; }
  });

  async function poll() {
    try {
      st.syl = await api.syllabus(st.syl.id);
    } catch (err) { $("syl-status").textContent = err.message; $("syl-go").disabled = false; return; }
    const s = st.syl;
    if (s.status === "reading") {
      $("syl-status").textContent = `${s.detail || "Reading…"} This can take a minute.`;
      st.sylTimer = setTimeout(poll, 1500);
      return;
    }
    $("syl-go").disabled = false;
    if (s.status === "failed") { $("syl-status").textContent = s.detail; return; }
    showReview(s);
  }

  function showReview(s) {
    sylForm.hidden = true;
    $("syl-review").hidden = false;
    $("syl-add-note").textContent = "";
    const list = $("syl-list");
    if (!s.items.length) { list.replaceChildren(el("p", "note", s.detail)); $("syl-count").textContent = "Nothing found"; return; }
    list.replaceChildren(...s.items.map((it) => {
      const row = el("label", `syl-row${it.past ? " past" : ""}`);
      const cb = el("input"); cb.type = "checkbox"; cb.checked = !it.past;
      const date = el("input"); date.type = "date"; date.value = it.date;
      const time = el("input"); time.type = "time"; time.value = it.time || "";
      const title = el("input"); title.value = it.title; title.maxLength = 200;
      const kind = el("select");
      Object.entries(KIND_LABEL).forEach(([v, n]) => { const o = el("option", null, n); o.value = v; o.selected = v === it.kind; kind.append(o); });
      row.append(cb, date, time, title, kind);
      row._get = () => ({ checked: cb.checked, date: date.value, time: time.value || null, title: title.value.trim(), kind: kind.value });
      cb.addEventListener("change", count);
      return row;
    }));
    count();
  }
  function rows() { return [...$("syl-list").querySelectorAll(".syl-row")]; }
  function count() {
    const n = rows().filter((r) => r._get().checked).length;
    $("syl-count").textContent = `${n} of ${rows().length} selected`;
    $("syl-add").textContent = n ? `Add ${n} to calendar` : "Add to calendar";
    $("syl-add").disabled = !n;
  }
  $("syl-all").addEventListener("click", () => { rows().forEach((r) => { r.querySelector("input[type=checkbox]").checked = true; }); count(); });
  $("syl-none").addEventListener("click", () => { rows().forEach((r) => { r.querySelector("input[type=checkbox]").checked = false; }); count(); });
  $("syl-again").addEventListener("click", openImport);
  $("syl-add").addEventListener("click", async () => {
    const items = rows().map((r) => r._get()).filter((x) => x.checked && x.date && x.title);
    $("syl-add").disabled = true;
    $("syl-add-note").textContent = "Adding…";
    try {
      const r = await api.addSyllabusItems(st.syl.id, items);
      $("syl-dialog").close();
      toast(r.warning || `Added ${r.added} items to your calendar.`);
      await load(st.date, true);
      onChange?.();
    } catch (err) { $("syl-add-note").textContent = err.message; $("syl-add").disabled = false; }
  });

  // ---------- Wiring ----------
  document.querySelectorAll("dialog.sheet").forEach((dlg) => {
    dlg.addEventListener("click", (ev) => { if (ev.target === dlg || ev.target.closest("[data-close]")) dlg.close(); });
  });
  $("cal-prev").addEventListener("click", () => st.data && load(addDays(st.data.date, -1)));
  $("cal-next").addEventListener("click", () => st.data && load(addDays(st.data.date, 1)));
  $("cal-today").addEventListener("click", () => st.data && load(st.data.today));
  $("cal-add").addEventListener("click", openAdd);
  $("cal-import").addEventListener("click", openImport);
  setInterval(() => { // keep the "now" line moving without re-drawing (and re-scrolling) the day
    const line = document.querySelector("#cal-grid .now-line");
    if (line && st.top) line.style.top = `${st.top(minutesOf(new Date().toISOString()))}px`;
  }, 60000);

  // Open the review dialog for a date-finding job started elsewhere (e.g. "Find dates" on a Brain document).
  function openImportJob(job) {
    clearTimeout(st.sylTimer);
    st.syl = job;
    sylForm.hidden = true;
    $("syl-review").hidden = true;
    $("syl-dialog").showModal();
    $("syl-review").hidden = false;
    $("syl-list").replaceChildren(el("p", "note", "Axiom is reading it for dates…"));
    $("syl-count").textContent = "Reading…";
    (async function wait() {
      try { st.syl = await api.syllabus(job.id); } catch (e) { $("syl-count").textContent = e.message; return; }
      if (st.syl.status === "reading") { $("syl-count").textContent = st.syl.detail || "Reading…"; st.sylTimer = setTimeout(wait, 1500); return; }
      if (st.syl.status === "failed") { $("syl-list").replaceChildren(el("p", "note", st.syl.detail)); $("syl-count").textContent = "Couldn't read it"; return; }
      showReview(st.syl);
    })();
  }

  return { load, refresh: () => load(st.date, true), openImportJob };
}
