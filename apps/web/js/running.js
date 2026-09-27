// Training: Vector's status window, this week's runs checked against the plan, logging runs,
// and getting Apple Watch data in (Health Auto Export or an Apple Health export file).
import { api } from "./api.js";

const $ = (id) => document.getElementById(id);
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
const STATUS = { done: ["Done", "ok"], short: ["Short", "warn"], missed: ["Missed", "bad"], today: ["Today", "warn"], upcoming: ["Upcoming", ""] };
const localDate = () => new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());

export function createRunning({ toast, onChange }) {
  let data = null;

  async function refresh() {
    try { data = await api.running(); render(); } catch (e) { $("tr-line").textContent = `Couldn't load: ${e.message}`; }
  }

  function render() {
    const d = data, f = d.fitness, p = d.plan;
    $("tr-line").textContent = p.week ? `Week ${p.week} of 12 · ${d.week_miles} mi this week`
      : p.starts_in_days ? `Your 12-week block starts ${p.start} (in ${p.starts_in_days} day${p.starts_in_days === 1 ? "" : "s"}).` : "The 12-week block is complete. Time for a new one.";
    $("tr-week").textContent = p.week ? `WEEK ${p.week} / 12` : `STARTS ${p.start}`;
    $("tr-vdot").textContent = f.vdot;

    const stats = $("tr-stats");
    const row = (k, v, sub) => { const c = el("div", "stat"); c.append(el("span", null, k), el("b", null, v)); if (sub) c.append(el("small", null, sub)); stats.append(c); };
    stats.replaceChildren();
    row("5K", f.five_k, `target ${f.target_5k}`);
    row("Mile", f.mile, `target ${f.target_mile}`);
    row("Easy", `${f.easy}`, "/mi");
    row("Tempo", f.tempo, "/mi");
    row("400 m", f.interval_400, `goal ${f.goal_400}`);
    const h = d.health;
    row("Resting HR", h.resting_hr ? `${Math.round(h.resting_hr.value)}` : "–", h.resting_hr ? "bpm" : "Apple Watch");
    row("VO₂ max", h.vo2max ? `${h.vo2max.value}` : "–", h.vo2max ? "ml/kg/min" : "Apple Watch");
    row("Sleep", h.sleep_hours ? `${h.sleep_hours.value} h` : "–", h.sleep_hours ? h.sleep_hours.day : "Apple Watch");

    $("tr-weeks").replaceChildren(...Array.from({ length: 12 }, (_, i) => {
      const w = el("i", p.week && i + 1 < p.week ? "past" : p.week === i + 1 ? "now" : "");
      w.title = `Week ${i + 1}: ${p.all[i].sessions.join(", ")}`;
      return w;
    }));
    $("tr-ready").replaceChildren(...d.readiness.map((r) => el("p", "ready-note", r)));

    renderSessions(d);
    renderRuns(d);
    renderPlan(d);
    $("tr-aw-meta").textContent = d.ingest.last ? `LAST SYNC ${new Date(d.ingest.last).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}` : d.ingest.token_set ? "WAITING FOR FIRST SYNC" : "NOT CONNECTED";
  }

  function renderSessions(d) {
    const box = $("tr-sessions");
    if (!d.this_week.length) {
      box.replaceChildren(el("p", "note", d.plan.starts_in_days ? `No planned runs yet: week 1 starts ${d.plan.start}. Log any run you do meanwhile.` : "No planned runs this week."));
      return;
    }
    box.replaceChildren(...d.this_week.map((s) => {
      const c = el("article", `session ${s.status}`);
      const [label, cls] = STATUS[s.status] || [s.status, ""];
      const head = el("div", "s-head");
      head.append(el("span", "s-day", `${s.day} · ${s.date.slice(5).replace("-", "/")}`), el("span", `tag ${cls}`, label));
      c.append(head, el("h4", null, s.title), el("p", "note small", s.detail));
      if (s.run) {
        const r = s.run;
        c.append(el("p", "s-run", `${r.miles} mi · ${r.duration} · ${r.pace} /mi${r.avg_hr ? ` · ${r.avg_hr} bpm` : ""}`));
      }
      (s.notes || []).forEach((n) => c.append(el("p", "s-note", n)));
      if (!s.run && s.status !== "upcoming") {
        const b = el("button", "linkish", "Log it");
        b.type = "button";
        b.addEventListener("click", () => {
          const form = $("tr-log");
          form.date.value = s.date;
          form.miles.value = s.miles;
          form.time_trial.checked = s.kind === "time_trial";
          form.time.focus();
        });
        c.append(b);
      }
      return c;
    }));
  }

  function renderRuns(d) {
    $("tr-runs-meta").textContent = `${d.week_miles} MI THIS WEEK`;
    const ul = $("tr-runs");
    ul.replaceChildren(...(d.runs.length ? d.runs.map((r) => {
      const li = el("li");
      const info = el("div");
      info.append(el("b", null, `${r.miles} mi · ${r.pace} /mi${r.time_trial ? " · time trial" : ""}`),
        el("span", "sub", `${new Date(r.start).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })} · ${r.duration}${r.avg_hr ? ` · ${r.avg_hr} bpm` : ""} · ${{ manual: "logged", auto_export: "Apple Watch", health_export: "Health export" }[r.source]}`));
      li.append(info);
      const del = el("button", "linkish", "Remove");
      del.type = "button";
      del.addEventListener("click", async () => {
        if (!confirm("Remove this run?")) return;
        try { data = await api.deleteRun(r.id); render(); } catch (e) { toast(e.message); }
      });
      li.append(del);
      return li;
    }) : [el("li", "empty-row", "No runs yet. Log one, or connect your Apple Watch below.")]));
  }

  function renderPlan(d) {
    const t = $("tr-plan");
    const head = el("tr");
    ["Week", "Tue", "Wed", "Sun"].forEach((x) => head.append(el("th", null, x)));
    const rows = d.plan.all.map((w) => {
      const tr = el("tr", d.plan.week === w.week ? "now" : "");
      const [sun, tue, wed] = w.sessions;
      [String(w.week), tue, wed, sun].forEach((x) => tr.append(el("td", null, x)));
      return tr;
    });
    t.replaceChildren(head, ...rows);
  }

  $("tr-log").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    const body = { date: f.date.value, start: f.start.value || null, miles: Number(f.miles.value), time: f.time.value,
                   avg_hr: f.avg_hr.value ? Number(f.avg_hr.value) : null, time_trial: f.time_trial.checked, notes: f.notes.value || null };
    try {
      data = await api.logRun(body);
      render();
      f.reset();
      f.date.value = localDate();
      f.start.value = "17:30";
      $("tr-log-note").textContent = "";
      toast(body.time_trial ? "Saved. Your paces were recalculated." : "Run saved.");
      onChange?.();
    } catch (e) { $("tr-log-note").textContent = e.message; }
  });

  $("tr-propose").addEventListener("click", async () => {
    const b = $("tr-propose");
    b.disabled = true;
    try { const r = await api.proposeRuns(); $("tr-propose-note").textContent = r.note; onChange?.(); }
    catch (e) { $("tr-propose-note").textContent = e.message; } finally { b.disabled = false; }
  });

  $("tr-token").addEventListener("click", async () => {
    if (data?.ingest.token_set && !confirm("Make a new secret? The old one stops working, so update Health Auto Export too.")) return;
    try {
      const t = await api.ingestToken();
      const box = $("tr-token-box");
      const rows = [];
      [["URL", t.url], ["Header", t.header], ["Value", t.token]].forEach(([k, v]) => {
        const dd = el("dd");
        const code = el("code", null, v);
        const copy = el("button", "linkish", "Copy");
        copy.type = "button";
        copy.addEventListener("click", () => navigator.clipboard?.writeText(v).then(() => toast(`${k} copied.`), () => toast("Copy it by hand.")));
        dd.append(code, " ", copy);
        rows.push(el("dt", null, k), dd);
      });
      box.replaceChildren(...rows, el("dd", "note small", "Shown once. The hub keeps only a scrambled copy."));
      box.hidden = false;
      $("tr-token").textContent = "Make a new secret";
      refresh();
    } catch (e) { toast(e.message); }
  });

  $("tr-import").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const file = ev.target.file.files[0];
    if (!file) return;
    const btn = ev.target.querySelector("button");
    btn.disabled = true;
    $("tr-import-note").textContent = `Uploading ${(file.size / 1e6).toFixed(0)} MB and reading it. Big exports take a few minutes…`;
    try {
      const r = await api.importHealth(file);
      data = r.status;
      render();
      $("tr-import-note").textContent = `Imported ${r.runs_added} run${r.runs_added === 1 ? "" : "s"} and ${r.metrics} health readings.`;
      onChange?.();
    } catch (e) { $("tr-import-note").textContent = e.message; } finally { btn.disabled = false; }
  });

  $("tr-log").date.value = localDate();
  return { refresh };
}
