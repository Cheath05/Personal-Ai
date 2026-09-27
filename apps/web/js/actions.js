// Action Previews: what an agent wants to change, and your Authorize / Always allow / Deny.
// Also the trust rules and Activity Log in Access, and the small "to OK" chip in the top bar.
import { api } from "./api.js";

const $ = (id) => document.getElementById(id);
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
const STATUS = { executed: ["Done", "ok"], denied: ["Denied", ""], undone: ["Undone", ""], failed: ["Failed", "bad"], expired: ["Expired", ""], pending: ["Waiting", "warn"] };

export function createActions({ toast, agentColor, onChange }) {
  const st = { pending: [], auto: [], ruleFor: null, ruleDone: null };
  const when = (iso) => new Date(iso).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

  async function afterChange(msg) {
    if (msg) toast(msg);
    await refresh();
    onChange?.();
  }

  // ---------- One card ----------
  // opts.onDone(label) lets a card inside a chat reply collapse into a one-line result.
  function card(a, opts = {}) {
    const c = el("article", `act-card${opts.inChat ? " in-chat" : ""}`);
    c.style.setProperty("--ag", agentColor(a.agent_id));
    const head = el("div", "act-head");
    head.append(el("span", "act-agent", a.agent), el("span", "act-kind", a.label));
    c.append(head, el("h4", null, a.title));
    const dl = el("dl", "act-diff");
    const row = (k, v, cls) => { if (v) dl.append(el("dt", null, k), el("dd", cls, v)); };
    row("Change", a.preview.change);
    row("Before", a.preview.before, "before");
    row("After", a.preview.after, "after");
    row("Why", a.reason);
    row("Undo", a.undoable ? "Yes, one tap in the Activity log" : "No, this can't be undone");
    c.append(dl);
    const btns = el("div", "row-actions");
    const approve = el("button", "btn primary", "Authorize");
    const always = el("button", "btn", "Always allow…");
    const deny = el("button", "btn danger", "Deny");
    [approve, always, deny].forEach((b) => { b.type = "button"; });
    const busy = (on) => [approve, always, deny].forEach((b) => { b.disabled = on; });
    approve.addEventListener("click", () => authorize(a, opts, busy));
    always.addEventListener("click", async () => {
      busy(true);
      try {
        const p = await api.rulePreview(a.id);
        busy(false);
        if (!p.allowed) { toast(p.description); return; }
        st.ruleFor = a.id;
        st.ruleDone = opts.onDone || null;
        st.ruleTitle = a.title;
        $("rule-text").textContent = p.description;
        $("rule-dialog").showModal();
      } catch (e) { toast(e.message); busy(false); }
    });
    deny.addEventListener("click", () => refuse(a, opts, busy));
    if (a.always_ask) always.hidden = true;
    btns.append(approve, always, deny);
    c.append(btns);
    return c;
  }

  async function authorize(a, opts, busy) {
    busy(true);
    try {
      const r = await api.approve(a.id);
      $("act-dialog").close();
      if (r.action.status === "failed") toast(`Couldn't do it: ${r.action.error}`);
      if (r.suggestion) showSigma(r.suggestion);
      opts.onDone?.(r.action.status === "executed" ? `✓ Authorized: ${a.title}` : `✗ Failed: ${r.action.error}`);
      await afterChange(r.action.status === "executed" ? "Done. It's on your calendar." : null);
    } catch (e) { toast(e.message); busy(false); }
  }

  async function refuse(a, opts, busy) {
    busy(true);
    try { await api.deny(a.id); $("act-dialog").close(); opts.onDone?.(`Denied: ${a.title}`); await afterChange(`Denied. ${a.agent} won't suggest this one again.`); }
    catch (e) { toast(e.message); busy(false); }
  }

  // ---------- One line on Today: what and when, with Authorize / Deny; tap for the full preview ----------
  function row(a) {
    const r = el("div", "act-row");
    r.style.setProperty("--ag", agentColor(a.agent_id));
    const main = el("button", "act-row-main");
    main.type = "button";
    main.setAttribute("aria-expanded", "false");
    const after = (a.preview.after || a.preview.change || "").split(" · ").slice(0, 2).join(" · ");
    main.append(el("span", "act-dot"), el("span", "act-row-t", a.title), el("span", "act-row-s", `${a.agent}${after ? ` · ${after}` : ""}`));
    const ok = el("button", "btn primary", "Authorize");
    const no = el("button", "btn danger", "Deny");
    [ok, no].forEach((b) => { b.type = "button"; });
    const busy = (on) => [ok, no].forEach((b) => { b.disabled = on; });
    ok.addEventListener("click", () => authorize(a, {}, busy));
    no.addEventListener("click", () => refuse(a, {}, busy));
    const btns = el("div", "act-row-btns");
    btns.append(ok, no);
    let full = null;
    main.addEventListener("click", () => {
      if (full) { full.remove(); full = null; } else { full = card(a); r.append(full); }
      main.setAttribute("aria-expanded", String(!!full));
      btns.hidden = !!full;  // the full card has its own buttons
    });
    r.append(main, btns);
    return r;
  }

  $("rule-yes").addEventListener("click", async () => {
    const id = st.ruleFor;
    $("rule-yes").disabled = true;
    try {
      await api.approve(id, true);
      st.ruleDone?.(`✓ Authorized and remembered: ${st.ruleTitle}`);
      st.ruleDone = null;
      $("rule-dialog").close();
      $("act-dialog").close();
      await afterChange("Done, and remembered. Matching study blocks will be added for you, with a note.");
    } catch (e) { toast(e.message); } finally { $("rule-yes").disabled = false; }
  });

  function showSigma(s) {
    const box = $("sigma");
    box.replaceChildren();
    box.append(el("b", null, `${s.from}: ${s.text}`), el("p", "rule-text", s.rule));
    const yes = el("button", "btn primary", "Make it automatic");
    const no = el("button", "btn", "Not now");
    [yes, no].forEach((b) => { b.type = "button"; });
    yes.addEventListener("click", async () => {
      try { await api.remember(s.action_id); box.hidden = true; await afterChange("Rule saved. See it in Access → Trust rules."); }
      catch (e) { toast(e.message); }
    });
    no.addEventListener("click", () => { box.hidden = true; renderPanel(); });
    const row = el("div", "row-actions");
    row.append(yes, no);
    box.append(row);
    box.hidden = false;
    $("ok-panel").hidden = false;
  }

  // ---------- Today panel + top chip ----------
  function renderPanel() {
    const list = $("ok-list");
    const items = st.pending.map((a) => row(a));
    st.auto.forEach((a) => {
      const done = el("div", "auto-note");
      done.append(el("span", null, `✓ ${a.title} · ${a.preview.after?.split(" · ").slice(0, 2).join(" · ") || ""} (trust rule #${a.rule_id})`));
      if (a.can_undo) {
        const u = el("button", "linkish", "Undo");
        u.type = "button";
        u.addEventListener("click", async () => { try { await api.undo(a.id); await afterChange("Undone."); } catch (e) { toast(e.message); } });
        done.append(u);
      }
      items.push(done);
    });
    list.replaceChildren(...items);
    $("ok-panel").hidden = !items.length && $("sigma").hidden;  // nothing to decide: no panel
    $("ok-meta").textContent = st.pending.length ? `${st.pending.length} WAITING · TAP ONE FOR DETAILS` : "DONE BY YOUR RULES";
    const g = $("g-ok");
    g.querySelector("b").textContent = st.pending.length ? `${st.pending.length} waiting` : "All clear";
    g.querySelector("small").textContent = st.pending.length ? st.pending[0].title : "Nothing to approve";
    g.dataset.tone = "";
    const chip = $("ok-chip");
    chip.hidden = !st.pending.length;
    chip.textContent = `${st.pending.length} to OK`;
  }

  async function refresh() {
    try {
      const r = await api.actions();
      st.pending = r.pending;
      st.auto = r.recent_auto;
      renderPanel();
    } catch { /* API not up yet */ }
  }

  function open(actionId) {
    const a = st.pending.find((x) => x.id === actionId);
    if (!a) { refresh(); return; }
    $("act-body").replaceChildren(card(a));
    $("act-dialog").showModal();
  }

  $("plan-now").addEventListener("click", async () => {
    const b = $("plan-now");
    b.disabled = true;
    $("plan-note").textContent = "Axiom is looking at your due dates and free time…";
    try {
      const r = await api.runPlanner();
      $("plan-note").textContent = r.note;
      await afterChange();
    } catch (e) { $("plan-note").textContent = e.message; } finally { b.disabled = false; }
  });

  // ---------- Access: rules and log ----------
  async function renderAccess() {
    try {
      const [rules, log] = await Promise.all([api.rules(), api.actionLog()]);
      const rl = $("rules-list");
      rl.replaceChildren(...(rules.length ? rules.map((r) => {
        const li = el("li");
        const info = el("div");
        info.append(el("b", null, `#${r.id} · ${r.agent}`), el("span", "sub", r.description),
          el("span", "sub", `Used ${r.uses} time${r.uses === 1 ? "" : "s"} · expires ${r.expires} unless used`));
        const rm = el("button", "linkish", "Revoke");
        rm.type = "button";
        rm.addEventListener("click", async () => {
          if (!confirm("Revoke this rule? Matching changes will ask you again.")) return;
          await api.revokeRule(r.id); renderAccess(); toast("Rule revoked.");
        });
        li.append(info, rm);
        return li;
      }) : [el("li", "empty-row", "No rules yet. Use Always allow on an Action Preview to make one.")]));

      const ll = $("log-list");
      ll.replaceChildren(...(log.length ? log.map((a) => {
        const li = el("li");
        const info = el("div");
        const [label, cls] = STATUS[a.status] || [a.status, ""];
        info.append(el("b", null, a.title),
          el("span", "sub", `${a.agent} · ${when(a.decided_at || a.ts)}${a.rule_id ? ` · by trust rule #${a.rule_id}` : a.status === "executed" ? " · you authorized" : ""}${a.error ? ` · ${a.error}` : ""}`));
        li.append(info, el("span", `tag ${cls}`, label));
        if (a.can_undo) {
          const u = el("button", "linkish", "Undo");
          u.type = "button";
          u.addEventListener("click", async () => { try { await api.undo(a.id); renderAccess(); await afterChange("Undone."); } catch (e) { toast(e.message); } });
          li.append(u);
        }
        return li;
      }) : [el("li", "empty-row", "Nothing yet. Every change an agent makes will be listed here.")]));
    } catch (e) { toast(e.message); }
  }

  return { refresh, open, renderAccess, card };
}
