import { api, DEVICE } from "./api.js";
import { createNexus } from "./nexus.js";

const $ = (id) => document.getElementById(id);
// Motion is a per-device choice. Full by default: Windows reports "reduce motion" whenever its
// animation effects are off (often just for speed), which used to freeze the whole Nexus.
const MOTION_KEY = "cardinal.motion";
const MOTION_LABELS = { full: "Full", reduced: "Reduced", device: "Follow device" };
function readMotion() { try { return localStorage.getItem(MOTION_KEY) || "full"; } catch { return "full"; } }
const motionPref = MOTION_LABELS[readMotion()] ? readMotion() : "full";
const reduceMotion = motionPref === "reduced"
  || (motionPref === "device" && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
document.documentElement.dataset.motion = reduceMotion ? "reduced" : "full";
function setMotion(pref) {
  try { localStorage.setItem(MOTION_KEY, pref); } catch { /* private mode: applies to this visit only */ }
  location.reload();
}
const VIEWS = ["nexus", "today", "training", "brain", "review", "access"];
const pad = (n) => String(n).padStart(2, "0");

const state = { agents: [], sel: 0, view: "nexus", busy: false, brains: null, usage: null };
let nexus = null;

/* ---------- Formatting ---------- */
function fmtTokens(n) {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}
const fmtUsd = (n) => `$${(n ?? 0).toFixed(2)}`;
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function toast(msg) {
  const t = $("toast");
  $("toast-msg").textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 3200);
}

/* ---------- Views ---------- */
function go(view) {
  if (!VIEWS.includes(view)) view = "nexus";
  const prev = $(`v-${state.view}`), next = $(`v-${view}`);
  document.querySelectorAll(".nav button").forEach((b) => {
    if (b.dataset.view === view) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  });
  if (prev !== next) {
    prev.hidden = true;
    next.hidden = false;
    if (!reduceMotion) { next.classList.remove("enter"); void next.offsetWidth; next.classList.add("enter"); }
    window.scrollTo(0, 0);
  }
  state.view = view;
  document.body.dataset.view = view;
  nexus?.setView(view);
  history.replaceState(null, "", `#${view}`);
  if (view === "access") refreshAccess();
}

/* ---------- Agent selection ---------- */
function agentStatus() {
  const b = state.brains;
  if (!b) return { level: "", label: "Checking", line: "Checking brains…" };
  const online = b.local.filter((x) => x.online);
  if (online.length) {
    return { level: "ok", label: "Nominal", line: `Thinking on ${online[0].label} (${online[0].model}), free.` };
  }
  if (b.claude.configured && b.budget_mode !== "local_only") {
    return { level: "warn", label: "Degraded", line: "No local brain online. Using Claude as backup." };
  }
  return { level: "bad", label: "Offline", line: "No brain online. Start Ollama on this Mac or the G14." };
}

function renderAgentCard() {
  const a = state.agents[state.sel];
  if (!a) return;
  document.documentElement.style.setProperty("--acc", a.color);
  $("a-uid").textContent = `[${a.unit}] · ${a.type.toUpperCase()}`;
  $("a-name").textContent = a.name;
  $("a-role").textContent = a.role;
  $("a-sys").textContent = pad(a.sees.length);
  $("a-obj").textContent = pad(a.changes.length);
  $("a-voice-short").textContent = a.voice.split(" · ")[0];
  const s = agentStatus();
  const light = $("a-light");
  light.className = `light ${s.level}`;
  light.textContent = s.label;
  $("a-status").textContent = s.line;
  nexus?.setStatus(s.level);
  const sees = $("a-sys-list"), changes = $("a-obj-list");
  sees.replaceChildren(...a.sees.map((x) => el("li", null, x)));
  changes.replaceChildren(...a.changes.map((x) => el("li", null, x)));
  $("c-title").textContent = `Talk to ${a.name}`;
  $("composer-input").placeholder = `Message ${a.name}`;
}

async function select(i) {
  if (i === state.sel && state.loaded) return;
  state.sel = i;
  state.loaded = true;
  nexus?.select(i);
  setCoreState("idle");
  renderAgentCard();
  const a = state.agents[i];
  const box = $("transcript");
  box.replaceChildren(el("p", "empty", "Loading…"));
  try {
    const msgs = await api.messages(a.id);
    if (state.agents[state.sel] !== a) return;
    box.replaceChildren();
    if (!msgs.length) box.append(el("p", "empty", `Say hello to ${a.name}. Replies come from your local brain first.`));
    msgs.forEach((m) => box.append(messageEl(m)));
    box.scrollTop = box.scrollHeight;
  } catch (e) {
    box.replaceChildren(systemEl(e.message));
  }
}

/* ---------- Transcript ---------- */
function messageEl(m, route) {
  const a = state.agents[state.sel];
  const div = el("div", m.role === "user" ? "msg" : "msg ai");
  div.append(el("span", "who", m.role === "user" ? (m.device ? `You · ${m.device}` : "You") : a.name));
  const p = el("p", null, m.content);
  div.append(p);
  const metaText = route ? routeLabel(route) : m.role === "assistant" && m.model ? m.model : "";
  if (metaText) {
    const meta = el("div", "meta");
    meta.append(el("span", null, metaText));
    div.append(meta);
  }
  return div;
}

function systemEl(text) {
  const div = el("div", "msg sys");
  div.append(el("span", "who", "System"), el("p", null, text));
  return div;
}

function routeLabel(r) {
  const parts = [r.label, `${(r.latency_ms / 1000).toFixed(1)}s`];
  if (r.provider === "claude") parts.push(`$${r.cost_usd.toFixed(4)}`);
  else parts.push("free");
  if (r.reason) parts.push(r.reason);
  return parts.join(" · ");
}

function typeOut(p, text) {
  return new Promise((resolve) => {
    if (reduceMotion) { p.textContent = text; resolve(); return; }
    const step = Math.max(1, Math.ceil(text.length / 160));
    let i = 0;
    p.classList.add("caret");
    const tick = () => {
      i = Math.min(text.length, i + step);
      p.textContent = text.slice(0, i);
      $("transcript").scrollTop = $("transcript").scrollHeight;
      if (i < text.length) setTimeout(tick, 16);
      else { p.classList.remove("caret"); resolve(); }
    };
    tick();
  });
}

function setCoreState(s) {
  nexus?.setState(s);
  $("c-state").textContent = s.toUpperCase();
}

async function send(text, { askClaude = false } = {}) {
  if (state.busy) return;
  const a = state.agents[state.sel];
  const box = $("transcript");
  box.querySelector(".empty")?.remove();
  box.querySelectorAll(".ask-claude").forEach((b) => b.remove());
  if (!askClaude) box.append(messageEl({ role: "user", content: text, device: DEVICE }));
  box.scrollTop = box.scrollHeight;
  state.busy = true;
  $("send").disabled = true;
  setCoreState("thinking");
  try {
    const res = askClaude ? await api.askClaude(a.id) : await api.chat(a.id, text);
    if (state.agents[state.sel] !== a) return;
    setCoreState("speaking");
    const div = messageEl({ role: "assistant", content: "" }, res.route);
    box.append(div);
    await typeOut(div.querySelector("p"), res.message.content);
    if (res.route.provider === "local" && res.claude_available) {
      const btn = el("button", "linkish ask-claude", "Ask Claude instead");
      btn.type = "button";
      btn.addEventListener("click", () => send(null, { askClaude: true }));
      div.querySelector(".meta").append(btn);
    }
  } catch (e) {
    box.append(systemEl(e.message));
  } finally {
    state.busy = false;
    $("send").disabled = false;
    setCoreState("idle");
    box.scrollTop = box.scrollHeight;
    refreshUsage();
  }
}

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("composer-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  send(text);
});

/* ---------- Usage (low-key chip + popover) ---------- */
function usageBody(u, { withCredit = true } = {}) {
  const m = u.month;
  const frag = document.createDocumentFragment();

  const big = el("div", "u-big");
  big.append(el("b", null, fmtUsd(m.claude_cost)), el("span", null, `Claude of ${fmtUsd(u.cap_usd)} cap`));
  frag.append(big);

  const meter = el("div", "bmeter");
  meter.setAttribute("role", "img");
  meter.setAttribute("aria-label", `${fmtUsd(m.claude_cost)} of ${fmtUsd(u.cap_usd)}. Essentials-only mode starts at ${fmtUsd(u.soft_usd)}.`);
  const fill = el("i");
  fill.style.width = `${Math.min(100, (m.claude_cost / u.cap_usd) * 100)}%`;
  const mark = el("b");
  mark.style.left = `${(u.soft_usd / u.cap_usd) * 100}%`;
  meter.append(fill, mark);
  const scale = el("div", "bscale");
  scale.append(el("span", null, "$0"), el("span", null, `${fmtUsd(u.soft_usd)} essentials`), el("span", null, `${fmtUsd(u.cap_usd)} stop`));
  frag.append(meter, scale);

  const rows = el("dl", "u-rows");
  const row = (k, v) => rows.append(el("dt", null, k), el("dd", null, v));
  row("Local tokens (free)", fmtTokens(m.local_tokens));
  row("Claude tokens", fmtTokens(m.claude_tokens));
  row("Handled locally", m.local_share == null ? "–" : `${Math.round(m.local_share * 100)}% of ${m.requests}`);
  row("Projected month", fmtUsd(m.projected_cost));
  row("Today", `${fmtTokens(u.today.local_tokens + u.today.claude_tokens)} tok · ${fmtUsd(u.today.claude_cost)}`);
  if (u.credit) row("Credit left (est.)", `${fmtUsd(u.credit.left_usd)} of ${fmtUsd(u.credit.loaded_usd)}`);
  m.by_agent.slice(0, 3).forEach((a) => row(a.name, `${fmtTokens(a.tokens)} · ${fmtUsd(a.cost)}`));
  frag.append(rows);

  u.tips.slice(0, 2).forEach((tip) => frag.append(el("p", "u-tip", tip)));

  if (withCredit) {
    const form = el("form", "credit-form");
    const label = el("label", "note small", "Added credit?");
    label.htmlFor = "credit-amount";
    const input = el("input");
    Object.assign(input, { id: "credit-amount", type: "number", min: "1", max: "1000", step: "1", placeholder: "$10" });
    const btn = el("button", "btn", "Save");
    btn.type = "submit";
    form.append(label, input, btn);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const amount = Number(input.value);
      if (!(amount > 0)) return;
      try {
        state.usage = await api.addCredit(amount);
        renderUsage();
        toast(`Recorded ${fmtUsd(amount)} of prepaid credit.`);
      } catch (err) { toast(err.message); }
    });
    frag.append(form);
  }
  return frag;
}

function renderUsage() {
  const u = state.usage;
  if (!u) return;
  const m = u.month;
  const chip = $("usage-chip");
  $("usage-chip-text").textContent = `${fmtTokens(m.local_tokens + m.claude_tokens)} tok · ${fmtUsd(m.claude_cost)}`;
  chip.classList.toggle("warn", u.mode !== "normal");
  chip.title = `This month: ${fmtTokens(m.local_tokens)} local tokens (free), ${fmtTokens(m.claude_tokens)} Claude tokens, ${fmtUsd(m.claude_cost)} spent`;
  $("usage-pop-month").textContent = m.label;
  $("usage-pop-body").replaceChildren(usageBody(u));
  if (state.view === "access") {
    $("usage-month").textContent = m.label;
    $("usage-full").replaceChildren(usageBody(u, { withCredit: false }));
  }
}

async function refreshUsage() {
  try { state.usage = await api.usage(); renderUsage(); } catch { /* API not up yet */ }
}

function toggleUsage(open) {
  const pop = $("usage-pop"), chip = $("usage-chip");
  const show = open ?? pop.hidden;
  pop.hidden = !show;
  chip.setAttribute("aria-expanded", String(show));
  if (show) refreshUsage();
}
$("usage-chip").addEventListener("click", (e) => { e.stopPropagation(); toggleUsage(); });
document.addEventListener("click", (e) => {
  if (!$("usage-pop").hidden && !$("usage-pop").contains(e.target)) toggleUsage(false);
});

/* ---------- Brains / Access ---------- */
async function refreshBrains() {
  try {
    state.brains = await api.brains();
  } catch { state.brains = null; }
  renderAgentCard();
}

async function refreshAccess() {
  await Promise.all([refreshBrains(), refreshUsage()]);
  const b = state.brains;
  const list = $("brains-list");
  if (!b) { list.replaceChildren(el("li", null, "The API is not answering.")); return; }
  $("brains-mode").textContent = { normal: "NORMAL", essentials: "ESSENTIALS ONLY", local_only: "LOCAL ONLY" }[b.budget_mode];
  const items = b.local.map((x) => {
    const li = el("li");
    const left = el("span", null, x.label);
    left.append(el("span", "sub", x.model));
    li.append(left, el("span", `tag ${x.online ? "ok" : ""}`, x.online ? "Online" : "Offline"));
    return li;
  });
  const cl = el("li");
  const left = el("span", null, "Claude (backup)");
  left.append(el("span", "sub", `${b.claude.models.default} · cheap: ${b.claude.models.cheap}`));
  cl.append(left, el("span", `tag ${b.claude.configured ? "ok" : ""}`, b.claude.configured ? "Key set" : "No key"));
  list.replaceChildren(...items, cl);
}

/* ---------- System Call palette ---------- */
const palette = $("palette"), scrim = $("scrim"), palInput = $("pal-input"), palList = $("pal-list");
let commands = [], filtered = [];
function buildCommands() {
  commands = [
    ...state.agents.map((a, i) => ({ label: `Summon ${a.name}`, hint: a.type, run: () => { go("nexus"); select(i); } })),
    ...[["today", "Open Today"], ["training", "Open Training"], ["brain", "Open Second Brain"], ["review", "Open Review"], ["access", "Open Access"]]
      .map(([v, label]) => ({ label, hint: "View", run: () => go(v) })),
    { label: "Show usage", hint: "Tokens and cost", run: () => toggleUsage(true) },
    ...Object.entries(MOTION_LABELS).map(([pref, name]) => ({
      label: `Motion: ${name}`, hint: pref === motionPref ? "Current, this device" : "This device", run: () => setMotion(pref),
    })),
  ];
}
function renderPalette() {
  const q = palInput.value.trim().toLowerCase().replace(/^system call:?\s*/, "");
  filtered = commands.filter((c) => !q || c.label.toLowerCase().includes(q));
  palList.replaceChildren(...filtered.map((c, i) => {
    const li = el("li");
    const b = el("button", i === 0 ? "act" : "", c.label);
    b.type = "button";
    b.append(el("span", null, c.hint));
    b.addEventListener("click", () => { closePalette(); c.run(); });
    li.append(b);
    return li;
  }));
}
function openPalette() { scrim.hidden = false; palette.hidden = false; palInput.value = ""; renderPalette(); palInput.focus(); }
function closePalette() { scrim.hidden = true; palette.hidden = true; }
$("open-pal").addEventListener("click", openPalette);
scrim.addEventListener("click", closePalette);
palInput.addEventListener("input", renderPalette);
$("pal-form").addEventListener("submit", (e) => { e.preventDefault(); if (filtered[0]) { closePalette(); filtered[0].run(); } });
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); }
  if (e.key === "Escape") { closePalette(); toggleUsage(false); }
});

/* ---------- Clock ---------- */
function tick() { const d = new Date(); $("clock").textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; }
tick();
setInterval(tick, 1000);

/* ---------- Boot ---------- */
async function boot() {
  document.querySelectorAll(".nav button").forEach((b) => b.addEventListener("click", () => go(b.dataset.view)));
  try {
    state.agents = await api.agents();
  } catch (e) {
    $("transcript").replaceChildren(systemEl(`Can't reach the Cardinal API: ${e.message}`));
    return;
  }
  nexus = createNexus({ canvas: $("bg"), stage: $("stage"), agents: state.agents, onSelect: select, reduceMotion });
  buildCommands();
  await select(0);
  const hash = location.hash.replace("#", "");
  if (hash && hash !== "nexus") go(hash);
  refreshBrains();
  refreshUsage();
  setInterval(refreshBrains, 30000);
  setInterval(refreshUsage, 60000);
  if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost")) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
}
boot();
