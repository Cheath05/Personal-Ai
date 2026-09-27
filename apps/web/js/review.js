// Review: Delta's morning check-in and evening review, the week in numbers, the weekly rollup,
// and Core Memory (kept by Sigma, editable by you).
import { api } from "./api.js";

const $ = (id) => document.getElementById(id);
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
const RESULT = { kept: "Kept it", partly: "Partly", skipped: "Skipped" };

export function createReview({ toast, getDue, goToday, onChange }) {
  const st = { data: null, energy: null, eveningOpen: false };
  const hm = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

  async function refresh() {
    try {
      st.data = await api.review();
      render();
    } catch (e) { $("rv-line").textContent = `Couldn't load: ${e.message}`; }
  }

  // Top-bar chip, even when the Review view isn't open.
  async function refreshChip() {
    try { st.data = await api.review(); renderChipFrom(st.data); } catch { /* API not up */ }
  }

  function render() {
    const d = st.data;
    renderChipFrom(d);
    const m = d.morning, e = d.evening;
    $("rv-line").textContent = [
      m.done ? `Morning done ${hm(m.checkin.ts)}` : m.due ? "Morning check-in is open" : `Morning check-in from ${m.time}`,
      e.done ? `evening done ${hm(e.checkin.ts)}` : e.due ? "evening review is open" : `evening review at ${e.time}`,
      d.week.streak ? `${d.week.streak}-day streak` : null,
    ].filter(Boolean).join(" · ");
    renderMorning(d);
    renderEvening(d);
    renderWeek(d);
    renderRollup(d);
    renderMemory();
  }

  function renderChipFrom(d) {
    const which = d.morning.due ? "Morning check-in" : d.evening.due ? "Evening review" : null;
    $("ci-chip").hidden = !which;
    if (which) $("ci-chip").textContent = which;
  }

  // ---------- Morning ----------
  function renderMorning(d) {
    const m = d.morning;
    $("rv-morning").classList.toggle("neon", m.due);
    $("rv-m-meta").textContent = m.done ? `DONE · ENERGY ${m.checkin.energy}/5` : m.due ? "OPEN NOW" : `FROM ${m.time}`;
    $("rv-m-form").hidden = m.done;
    $("rv-m-done").hidden = !m.done;
    if (!m.done) {
      const f = $("rv-m-form");
      // Pre-fill with last night's first task and anything carried over.
      const pre = d.tasks.map((t) => t.title);
      ["t1", "t2", "t3"].forEach((n, i) => { if (!f[n].value && pre[i]) f[n].value = pre[i]; });
      const due = getDue().map((x) => x.title);
      $("rv-suggest").replaceChildren(...[...new Set(due)].slice(0, 12).map((t) => Object.assign(el("option"), { value: t })));
      return;
    }
    renderTasks(d);
    const reply = $("rv-m-reply");
    reply.hidden = !m.checkin.reply;
    reply.textContent = m.checkin.reply || "";
  }

  function renderTasks(d) {
    const list = $("rv-tasks");
    list.replaceChildren(...(d.tasks.length ? d.tasks.map((t) => {
      const li = el("li");
      const label = el("label");
      const cb = el("input");
      cb.type = "checkbox";
      cb.checked = t.status === "done";
      cb.addEventListener("change", async () => {
        try { await api.setTask(t.id, cb.checked ? "done" : "open"); t.status = cb.checked ? "done" : "open"; li.classList.toggle("done", cb.checked); }
        catch (err) { toast(err.message); cb.checked = !cb.checked; }
      });
      label.append(cb, el("span", null, t.title));
      if (t.source === "carried") label.append(el("small", null, "carried over"));
      if (t.source === "first_task") label.append(el("small", null, "first task"));
      li.append(label);
      li.classList.toggle("done", t.status === "done");
      return li;
    }) : [el("li", "empty-row", "No priorities yet.")]));
  }

  $("rv-energy").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-v]");
    if (!b) return;
    st.energy = Number(b.dataset.v);
    $("rv-energy").querySelectorAll("[data-v]").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
  });

  $("rv-m-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    const top = [f.t1.value, f.t2.value, f.t3.value].map((x) => x.trim()).filter(Boolean);
    if (!top.length) { $("rv-m-note").textContent = "Add at least one priority."; return; }
    if (!st.energy) { $("rv-m-note").textContent = "Pick your energy, 1 to 5."; return; }
    const btn = f.querySelector("button[type=submit]");
    btn.disabled = true;
    $("rv-m-note").textContent = "Delta is reading your day…";
    try {
      await api.morning({ top, energy: st.energy, note: f.note.value || null });
      f.reset();
      st.energy = null;
      $("rv-m-note").textContent = "";
      await refresh();
      onChange?.();
    } catch (err) { $("rv-m-note").textContent = err.message; } finally { btn.disabled = false; }
  });

  $("rv-task-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const input = ev.target.title;
    if (!input.value.trim()) return;
    try { await api.addTask(input.value); input.value = ""; await refresh(); } catch (err) { toast(err.message); }
  });

  // ---------- Evening ----------
  function renderEvening(d) {
    const e = d.evening;
    $("rv-evening").classList.toggle("neon", e.due);
    $("rv-e-meta").textContent = e.done ? "DONE" : e.due ? "OPEN NOW" : `AT ${e.time}`;
    const showForm = !e.done && (e.due || st.eveningOpen);
    $("rv-e-wait").hidden = e.done || showForm;
    $("rv-e-form").hidden = !showForm;
    $("rv-e-done").hidden = !e.done;
    $("rv-e-wait-text").textContent = `Delta asks how the day went at ${e.time}. You can start early if you're done for the day.`;
    if (showForm) {
      const items = [];
      d.tasks.forEach((t) => items.push({ kind: "task", id: t.id, title: t.title, done: t.status === "done" }));
      d.blocks.forEach((b) => items.push({ kind: "block", id: b.id, title: `${b.title} (${hm(b.start)}–${hm(b.end)})`, done: false }));
      $("rv-e-list").replaceChildren(...(items.length ? items.map((it) => {
        const li = el("li");
        const label = el("label");
        const cb = el("input");
        cb.type = "checkbox";
        cb.checked = it.done;
        cb.dataset.kind = it.kind;
        cb.dataset.id = it.id;
        label.append(cb, el("span", null, it.title));
        if (it.kind === "block") label.append(el("small", null, "study block"));
        li.append(label);
        return li;
      }) : [el("li", "empty-row", "No priorities or study blocks today.")]));
    }
    if (e.done) {
      const a = e.checkin.answers;
      const kv = $("rv-e-summary");
      const rows = [];
      const add = (k, v) => { if (v) rows.push(el("dt", null, k), el("dd", null, v)); };
      add("Done", `${a.tasks_done} of ${a.tasks_planned} priorities${a.blocks_planned?.length ? ` · ${a.blocks_done.length} of ${a.blocks_planned.length} study blocks` : ""}`);
      add("Went well", a.went_well);
      add("Didn't", a.didnt);
      add("Why", a.why);
      add("First task", a.first_task);
      kv.replaceChildren(...rows);
      $("rv-e-reply").hidden = !e.checkin.reply;
      $("rv-e-reply").textContent = e.checkin.reply || "";
    }
  }

  $("rv-e-start").addEventListener("click", () => { st.eveningOpen = true; renderEvening(st.data); });

  $("rv-e-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    const checks = [...f.querySelectorAll("#rv-e-list input[type=checkbox]")];
    const body = {
      done_task_ids: checks.filter((c) => c.checked && c.dataset.kind === "task").map((c) => Number(c.dataset.id)),
      done_block_ids: checks.filter((c) => c.checked && c.dataset.kind === "block").map((c) => Number(c.dataset.id)),
      went_well: f.went_well.value, didnt: f.didnt.value, why: f.why.value,
      first_task: f.first_task.value || null, carry: f.carry.checked,
    };
    const btn = f.querySelector("button[type=submit]");
    btn.disabled = true;
    $("rv-e-note").textContent = "Delta is reading your day…";
    try {
      await api.evening(body);
      f.reset();
      st.eveningOpen = false;
      $("rv-e-note").textContent = "";
      await refresh();
      onChange?.();
    } catch (err) { $("rv-e-note").textContent = err.message; } finally { btn.disabled = false; }
  });

  // ---------- The week ----------
  function tile(label, value, sub) {
    const t = el("div", "tile");
    t.append(el("b", null, value), el("span", null, label));
    if (sub) t.append(el("small", null, sub));
    return t;
  }

  function renderWeek(d) {
    const w = d.week;
    $("rv-w-meta").textContent = `FROM MON ${w.week_start.slice(5).replace("-", "/")}`;
    const pct = w.completion == null ? "–" : `${Math.round(w.completion * 100)}%`;
    const trend = w.energy_avg && w.energy_prev_avg ? `last week ${w.energy_prev_avg}` : null;
    $("rv-tiles").replaceChildren(
      tile("Check-ins", `${w.mornings + w.evenings}`, `${w.mornings} mornings · ${w.evenings} evenings`),
      tile("Priorities done", pct, `${w.priorities_done} of ${w.priorities_planned}`),
      tile("Study blocks", `${w.blocks_done}/${w.blocks_planned}`, "done / planned"),
      tile("Energy", w.energy_avg ?? "–", trend || "average, 1–5"),
      tile("Streak", `${w.streak}`, w.streak === 1 ? "day" : "days"),
    );
    $("rv-energy-week").replaceChildren(...w.energy.map((x) => {
      const c = el("div", `eday${x.value ? ` e${x.value}` : ""}${x.date === d.day ? " today" : ""}`);
      c.append(el("span", null, x.day), el("b", null, x.value ?? "·"));
      c.title = x.value ? `${x.day}: energy ${x.value}/5` : `${x.day}: no check-in`;
      return c;
    }));
    const box = $("rv-exps");
    if (!d.experiments.length) { box.replaceChildren(el("p", "note small", "No experiments this week. Delta proposes 3 in Sunday's rollup, for you to authorize.")); return; }
    box.replaceChildren(el("p", "note small", "This week's experiments. How are they going?"), ...d.experiments.map((x) => {
      const row = el("div", "exp");
      row.append(el("span", null, x.text));
      const seg = el("div", "seg");
      Object.entries(RESULT).forEach(([v, name]) => {
        const b = el("button", null, name);
        b.type = "button";
        b.setAttribute("aria-selected", String(x.result === v));
        b.addEventListener("click", async () => {
          try { await api.markExperiment(x.id, x.result === v ? null : v); await refresh(); } catch (err) { toast(err.message); }
        });
        seg.append(b);
      });
      row.append(seg);
      return row;
    }));
  }

  // ---------- Rollup ----------
  function list(title, items) {
    if (!items?.length) return null;
    const wrap = el("div", "rl-block");
    wrap.append(el("h4", null, title));
    const ul = el("ul");
    items.forEach((x) => ul.append(el("li", null, x)));
    wrap.append(ul);
    return wrap;
  }

  function renderRollup(d) {
    const r = d.rollup;
    $("rv-r-meta").textContent = r ? `WEEK OF ${r.week_start}` : `EVERY ${d.rollup_time.toUpperCase()}`;
    const box = $("rv-rollup");
    if (!r) { box.replaceChildren(el("p", "note", `Delta writes the rollup every ${d.rollup_time}: completion, energy, wins, blockers and three experiments for next week.`)); return; }
    const grid = el("div", "rl-grid");
    [list("Wins", r.wins), list("Blockers", r.blockers), list("Next week's focus", r.focus), list("Experiments to try", r.experiments)]
      .filter(Boolean).forEach((x) => grid.append(x));
    const plan = el("p", "note small");
    if (r.plan_action_id) {
      plan.textContent = "Next week's plan is waiting for your OK on Today. ";
      const go = el("button", "linkish", "Review it");
      go.type = "button";
      go.addEventListener("click", goToday);
      plan.append(go);
    }
    box.replaceChildren(el("p", "brief", r.summary), grid, plan);
  }

  $("rv-r-now").addEventListener("click", async () => {
    const b = $("rv-r-now");
    b.disabled = true;
    $("rv-r-note").textContent = "Delta is looking at your week. This can take a minute…";
    try { await api.writeRollup(); $("rv-r-note").textContent = ""; await refresh(); onChange?.(); }
    catch (err) { $("rv-r-note").textContent = err.message; } finally { b.disabled = false; }
  });

  // ---------- Core Memory ----------
  async function renderMemory() {
    try {
      const mems = await api.memory();
      const ul = $("rv-memory");
      ul.replaceChildren(...(mems.length ? mems.map((m) => {
        const li = el("li");
        const info = el("div");
        info.append(el("b", null, m.text), el("span", "sub", [m.kind, m.source === "sigma" ? "found by Sigma" : "you", m.evidence].filter(Boolean).join(" · ")));
        const edit = el("button", "linkish", "Edit");
        const del = el("button", "linkish", "Forget");
        [edit, del].forEach((b) => { b.type = "button"; });
        edit.addEventListener("click", async () => {
          const text = prompt("Edit this memory:", m.text);
          if (text == null || !text.trim()) return;
          try { await api.editMemory(m.id, text); renderMemory(); } catch (err) { toast(err.message); }
        });
        del.addEventListener("click", async () => {
          if (!confirm("Forget this? No agent will see it again.")) return;
          try { await api.deleteMemory(m.id); renderMemory(); } catch (err) { toast(err.message); }
        });
        const acts = el("span", "mem-acts");
        acts.append(edit, del);
        li.append(info, acts);
        return li;
      }) : [el("li", "empty-row", "Nothing yet. Add something below, or let Sigma find patterns after a week of check-ins.")]));
    } catch (err) { toast(err.message); }
  }

  $("rv-mem-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    if (!f.text.value.trim()) return;
    try { await api.addMemory(f.text.value, f.kind.value); f.text.value = ""; renderMemory(); toast("Remembered. Every agent will know this."); }
    catch (err) { toast(err.message); }
  });

  return { refresh, refreshChip };
}
