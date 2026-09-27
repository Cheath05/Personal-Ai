// Phase 6: Relay's sorted Inbox (reply drafts, sending with your OK) and the Focus panel
// (which apps you used, from ActivityWatch on your laptops).
import { api } from "./api.js";

const $ = (id) => document.getElementById(id);
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
const button = (cls, text, onClick) => {
  const b = el("button", cls, text);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
};
const hhmm = (d) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
function when(iso) {
  const d = new Date(iso), now = new Date();
  if (d.toDateString() === now.toDateString()) return hhmm(d);
  return d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
}
function hm(min) { return min >= 60 ? `${Math.floor(min / 60)}h ${String(min % 60).padStart(2, "0")}m` : `${min}m`; }

// A button that needs a second tap within 4 s ("Confirm"), like Clear in the chat.
function twoTap(btn, run) {
  const label = btn.textContent;
  let timer = null;
  const disarm = () => { clearTimeout(timer); btn.classList.remove("armed"); btn.textContent = label; };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (!btn.classList.contains("armed")) {
      btn.classList.add("armed");
      btn.textContent = "Confirm";
      timer = setTimeout(disarm, 4000);
      return;
    }
    disarm();
    run();
  });
  document.addEventListener("click", (e) => { if (e.target !== btn) disarm(); });
  return disarm;
}

function glance(id, value, sub, tone = "") {  // a tile in Today's glance strip
  const t = document.getElementById(id);
  t.querySelector("b").textContent = value;
  t.querySelector("small").textContent = sub || "";
  t.dataset.tone = tone;
}

/* ---------- Inbox ---------- */
const INBOX_SHOWN = 5;
const CAT = { urgent: ["Urgent", "bad"], reply: ["Needs reply", "warn"], fyi: ["FYI", ""], noise: ["Noise", ""] };
const TABS = { todo: ["urgent", "reply"], fyi: ["fyi"], noise: ["noise"] };
const tabOf = (item) => Object.keys(TABS).find((t) => TABS[t].includes(item.category)) || "fyi";

// Focus chart colors, checked for contrast and colorblind separation on the dark panel.
const SERIES = [["focus", "Focus", "#199e70"], ["neutral", "Other", "#3987e5"], ["distraction", "Distraction", "#d95926"]];

export function createWorkforce({ toast, onChange }) {
  const ib = { data: null, tab: "todo", all: false, open: new Set() };
  const fo = { data: null, table: false };

  async function refreshInbox() {
    try {
      ib.data = await api.inbox();
      renderInbox();
    } catch (e) { $("inbox-list").replaceChildren(el("li", "empty-row", `Couldn't load the inbox: ${e.message}`)); }
  }

  const canDraft = (account) => ib.data?.accounts.some((a) => a.slot === account && a.connected && a.can_draft);

  function renderInbox() {
    const d = ib.data;
    const connected = d.accounts.filter((a) => a.connected);
    const counts = { todo: 0, fyi: 0, noise: 0 };
    d.items.forEach((i) => { counts[tabOf(i)] += 1; });
    document.querySelectorAll("#ib-tabs button").forEach((b) => {
      const on = b.dataset.cat === ib.tab;
      b.setAttribute("aria-selected", String(on));
      b.dataset.label ??= b.textContent;
      b.replaceChildren(document.createTextNode(b.dataset.label), el("span", "n", String(counts[b.dataset.cat])));
    });
    $("ib-meta").textContent = d.last_sorted ? `SORTED ${hhmm(new Date(d.last_sorted))} · RELAY` : "NOT SORTED YET";
    $("ib-sort").disabled = !connected.length;
    $("ib-note").textContent = !connected.length
      ? "Connect Google in Access → Accounts, and Relay sorts your mail every hour."
      : !connected.some((a) => a.can_draft)
        ? "Relay can sort your mail. To let it save reply drafts and send (only when you press Send), press Reconnect in Access → Accounts."
        : "";
    const urgent = d.items.filter((i) => i.category === "urgent").length;
    const reply = d.items.filter((i) => i.category === "reply").length;
    if (!connected.length) glance("g-mail", "Not connected", "Access → Accounts");
    else if (!counts.todo) glance("g-mail", "All clear", d.last_sorted ? `Sorted ${hhmm(new Date(d.last_sorted))}` : "Not sorted yet");
    else glance("g-mail", `${counts.todo} need you`, [urgent && `${urgent} urgent`, reply && `${reply} to answer`].filter(Boolean).join(" · "), urgent ? "soon" : "");

    const list = $("inbox-list");
    const items = d.items.filter((i) => tabOf(i) === ib.tab);
    if (!items.length) {
      const empty = !connected.length ? "No inbox connected."
        : !d.last_sorted ? "Relay hasn't sorted yet. Press Sort now."
        : { todo: "Nothing needs you right now.", fyi: "No FYI mail this week.", noise: "No noise this week." }[ib.tab];
      list.replaceChildren(el("li", "empty-row", empty));
      return;
    }
    const shown = ib.all ? items : items.slice(0, INBOX_SHOWN);
    const rows = shown.map(itemRow);
    if (items.length > shown.length || ib.all && items.length > INBOX_SHOWN) {
      const li = el("li", "ib-more");
      li.append(button("linkish", ib.all ? "Show fewer" : `Show ${items.length - shown.length} more`, () => { ib.all = !ib.all; renderInbox(); }));
      rows.push(li);
    }
    list.replaceChildren(...rows);
  }

  // One line per email; tap it for Relay's reason, the to-do and date it found, and the buttons.
  function itemRow(item) {
    const li = el("li", "ib-item");
    const [catName, catCls] = CAT[item.category] || CAT.fyi;
    const head = button("ib-head", "", () => {
      const open = body.hidden;
      body.hidden = !open;
      head.setAttribute("aria-expanded", String(open));
      if (open) ib.open.add(item.id); else ib.open.delete(item.id);
    });
    const many = ib.data.accounts.filter((a) => a.connected).length > 1; // name the inbox only when there are two
    head.append(el("span", `tag ${catCls} ib-cat`, item.category === "reply" ? "Reply" : catName),
      el("span", "ib-from", item.sender), el("span", "ib-subj", item.subject || "(no subject)"),
      el("span", "ib-when", [when(item.received_at), many && item.account_label].filter(Boolean).join(" · ")));
    const body = el("div", "ib-body");
    body.hidden = !ib.open.has(item.id);
    head.setAttribute("aria-expanded", String(!body.hidden));
    li.append(head, body);
    if (item.reason) body.append(el("p", "ib-why", item.reason));
    const chips = el("div", "ib-chips");
    if (item.task) chips.append(el("span", "ib-chip", `To do: ${item.task}`));
    if (item.due) chips.append(el("span", "ib-chip", `Date: ${item.due}`));
    if (item.draft_action_id) chips.append(el("span", "ib-chip", "Draft saved in Gmail"));
    if (chips.childElementCount) body.append(chips);

    const acts = el("div", "ib-acts");
    if (item.category !== "noise") acts.append(button("linkish", "Reply", () => openReply(item)));
    if (item.category !== "noise") {
      acts.append(button("linkish", "Add to today", async (e) => {
        e.target.disabled = true;
        try { const r = await api.emailTask(item.id); toast(`Added to today's priorities: ${r.title}`); onChange?.(); } catch (err) { toast(err.message); e.target.disabled = false; }
      }));
    }
    if (item.due) {
      acts.append(button("linkish", "Put date on calendar", async (e) => {
        e.target.disabled = true;
        try {
          const a = await api.emailDue(item.id);
          toast(a.status === "executed" ? "Added to your calendar." : "Relay proposed it. Approve it under Needs your OK.");
          onChange?.();
        } catch (err) { toast(err.message); e.target.disabled = false; }
      }));
    }
    acts.append(button("linkish", "Done", async () => {
      try {
        await api.emailDone(item.id);
        ib.data.items = ib.data.items.filter((x) => x.id !== item.id);
        renderInbox();
      } catch (err) { toast(err.message); }
    }));
    body.append(acts);
    return li;
  }

  document.querySelectorAll("#ib-tabs button").forEach((b) => b.addEventListener("click", () => {
    ib.tab = b.dataset.cat;
    ib.all = false;
    if (ib.data) renderInbox();
  }));

  $("ib-sort").addEventListener("click", async () => {
    const btn = $("ib-sort");
    btn.disabled = true;
    btn.textContent = "Sorting…";
    $("ib-note").textContent = "Relay is reading senders, subjects and previews…";
    try {
      const r = await api.sortInbox();
      ib.data = r;
      renderInbox();
      toast(r.errors?.length ? r.errors[0] : r.sorted ? `Relay sorted ${r.sorted} new email${r.sorted === 1 ? "" : "s"}.` : "No new mail to sort.");
    } catch (e) { toast(e.message); $("ib-note").textContent = ""; } finally {
      btn.textContent = "Sort now";
      btn.disabled = false;
    }
  });

  /* ---------- Reply dialog ---------- */
  const dlg = $("mail-dialog"), form = $("mail-form");
  let cur = null, meta = {};
  dlg.addEventListener("click", (ev) => { if (ev.target === dlg || ev.target.closest("[data-close]")) dlg.close(); });

  function openReply(item) {
    cur = item;
    meta = { thread_id: item.thread_id };
    $("mail-h").textContent = `Reply to ${item.sender}`;
    const orig = $("mail-orig");
    orig.replaceChildren(el("b", null, item.subject || "(no subject)"),
      el("span", "sub", `${item.sender} · ${item.account_label} · ${when(item.received_at)}`), el("p", null, item.snippet || ""));
    form.reset();
    form.to.value = item.sender_raw || "";
    form.subject.value = /^re:/i.test(item.subject || "") ? item.subject : `Re: ${item.subject || ""}`;
    const ok = canDraft(item.account);
    $("mail-save").disabled = !ok;
    $("mail-send").disabled = !ok;
    $("mail-note").textContent = ok ? "" : "Reconnect Google in Access → Accounts to allow drafts and sending.";
    dlg.showModal();
    form.instructions.focus();
  }

  $("mail-write").addEventListener("click", async () => {
    const btn = $("mail-write");
    btn.disabled = true;
    $("mail-note").textContent = "Relay is reading the email and writing…";
    try {
      const r = await api.draftEmail(cur.id, form.instructions.value.trim());
      form.to.value = r.to;
      form.subject.value = r.subject;
      form.body.value = r.body;
      meta = { thread_id: r.thread_id, in_reply_to: r.in_reply_to, references: r.references };
      $("mail-note").textContent = `Written on ${r.brain_label || r.brain}. Change anything before you save or send.`;
    } catch (e) { $("mail-note").textContent = e.message; } finally { btn.disabled = false; }
  });

  function draft() {
    const d = { to: form.to.value.trim(), subject: form.subject.value.trim(), body: form.body.value, ...meta };
    if (!d.to || !d.body.trim()) { $("mail-note").textContent = "It needs a To address and a message."; return null; }
    return d;
  }

  $("mail-save").addEventListener("click", async () => {
    const d = draft();
    if (!d) return;
    $("mail-save").disabled = true;
    try {
      await api.saveDraft(cur.id, d);
      dlg.close();
      toast("Saved in Gmail drafts. Nothing was sent.");
      refreshInbox();
      onChange?.();
    } catch (e) { $("mail-note").textContent = e.message; } finally { $("mail-save").disabled = false; }
  });

  twoTap($("mail-send"), async () => {
    const d = draft();
    if (!d) return;
    $("mail-send").disabled = true;
    $("mail-note").textContent = "Sending…";
    try {
      await api.sendEmail(cur.id, d);
      dlg.close();
      toast(`Sent to ${d.to.replace(/<.*>/, "").trim() || d.to}.`);
      refreshInbox();
      onChange?.();
    } catch (e) { $("mail-note").textContent = e.message; } finally { $("mail-send").disabled = false; }
  });

  /* ---------- Focus (app usage) ---------- */
  async function refreshFocus() {
    try {
      fo.data = await api.appsDay();
      renderFocus();
    } catch (e) { $("fo-meta").textContent = "COULDN'T LOAD"; $("fo-apps").replaceChildren(el("p", "note small", e.message)); }
  }

  function renderFocus() {
    const d = fo.data;
    const seen = Object.entries(d.devices_seen || {});
    const t = d.totals;
    const total = Math.round((t.focus + t.neutral + t.distraction) / 60);
    $("fo-meta").textContent = total ? `APP TIME TODAY · ${hm(total).toUpperCase()}` : "APP TIME TODAY";
    const chart = document.querySelector(".fo-chart");
    const hasData = d.devices.length > 0;
    chart.hidden = !hasData;
    $("fo-kpis").hidden = !hasData;
    if (!seen.length) document.querySelector(".fo-connect").open = true;

    // Tiles
    $("fo-kpis").replaceChildren(...SERIES.map(([k, name, color]) => {
      const min = Math.round(t[k] / 60);
      const tile = el("div", "fo-kpi");
      const label = el("span");
      label.append(Object.assign(el("i", "key"), { style: `background:${color}` }), document.createTextNode(name));
      tile.append(label, el("b", null, hm(min)), el("small", null, total ? `${Math.round((min / total) * 100)}% of today` : "–"));
      return tile;
    }));

    // Legend (identity never rests on color alone: the tooltip and table name every series)
    $("fo-legend").replaceChildren(...SERIES.map(([, name, color]) => {
      const s = el("span");
      s.append(Object.assign(el("i", "key"), { style: `background:${color}` }), document.createTextNode(name));
      return s;
    }));

    renderPlot(d.hours);
    renderTable(d.hours);

    // Top apps and laptops
    const apps = el("div");
    apps.append(el("p", "fo-h", "Top apps"));
    const ul = el("ul", "list fo-applist");
    if (!d.top.length) ul.append(el("li", "empty-row", seen.length ? "No app time yet today." : "No laptop connected yet."));
    d.top.forEach((a) => {
      const li = el("li");
      const [, name, color] = SERIES.find(([k]) => k === a.category) || SERIES[1];
      const cat = el("span", "fo-cat");
      cat.append(Object.assign(el("i", "key"), { style: `background:${color}` }), document.createTextNode(name));
      const left = el("div", "fo-app");
      left.append(el("b", null, a.app), cat);
      li.append(left, el("span", "fo-min", hm(a.minutes)));
      ul.append(li);
    });
    apps.append(ul);
    const devs = el("div");
    devs.append(el("p", "fo-h", "Laptops"));
    const dl = el("ul", "list");
    if (!seen.length) dl.append(el("li", "empty-row", "None yet. Open Connect a laptop below."));
    seen.forEach(([name, iso]) => {
      const li = el("li");
      const ago = Date.now() - new Date(iso);
      li.append(el("span", null, name), el("span", `tag ${ago < 2 * 3600e3 ? "ok" : ago < 48 * 3600e3 ? "" : "warn"}`,
        `Last data ${when(iso)}`));
      dl.append(li);
    });
    devs.append(dl);
    $("fo-apps").replaceChildren(apps, devs);
  }

  const PLOT_MAX = 60; // minutes in an hour: every column shares one fixed scale
  function renderPlot(hours) {
    const plot = $("fo-plot");
    const lines = [60, 30, 0].map((m) => {
      const g = el("div", `fo-grid${m === 0 ? " base" : ""}`);
      g.style.top = `calc(var(--ph) * ${1 - m / PLOT_MAX})`;
      if (m) g.append(el("span", "fo-ylab", `${m}m`));
      return g;
    });
    const cols = el("div", "fo-cols");
    hours.forEach((h) => {
      const sum = h.focus + h.neutral + h.distraction;
      const col = el("button", "fo-col");
      col.type = "button";
      const parts = SERIES.filter(([k]) => h[k] > 0);
      col.setAttribute("aria-label", `${String(h.hour).padStart(2, "0")}:00, ${sum ? parts.map(([k, n]) => `${n} ${h[k]} minutes`).join(", ") : "no app time"}`);
      parts.forEach(([k, , color], i) => {
        const seg = el("i", `fo-seg${i === parts.length - 1 ? " cap" : ""}`);
        seg.style.background = color;
        seg.style.height = `max(2px, calc((var(--ph) - ${2 * (parts.length - 1)}px) * ${Math.min(h[k], PLOT_MAX) / PLOT_MAX}))`;
        col.append(seg);
      });
      if (h.hour % 6 === 0) col.append(el("span", "x", `${String(h.hour).padStart(2, "0")}:00`));
      const show = () => showTip(col, h);
      col.addEventListener("pointerenter", show);
      col.addEventListener("focus", show);
      col.addEventListener("click", show);
      col.addEventListener("pointerleave", hideTip);
      col.addEventListener("blur", hideTip);
      cols.append(col);
    });
    plot.setAttribute("aria-label", "Minutes of focus, other and distraction apps for each hour today. A table view is below.");
    plot.replaceChildren(...lines, cols);
  }

  function showTip(col, h) {
    const tip = $("fo-tip");
    const next = String((h.hour + 1) % 24).padStart(2, "0");
    tip.replaceChildren(el("b", null, `${String(h.hour).padStart(2, "0")}:00–${next}:00`));
    SERIES.forEach(([k, name, color]) => {
      const row = el("div", "fo-tip-row");
      row.append(Object.assign(el("i", "key"), { style: `background:${color}` }), el("span", null, name), el("span", "v", hm(h[k])));
      tip.append(row);
    });
    tip.hidden = false;
    const fig = col.closest("figure").getBoundingClientRect(), c = col.getBoundingClientRect();
    const w = tip.offsetWidth;
    const right = c.left - fig.left + c.width / 2 < fig.width / 2;
    tip.style.left = `${Math.max(0, Math.min(fig.width - w, right ? c.right - fig.left + 8 : c.left - fig.left - w - 8))}px`;
    tip.style.top = `${$("fo-plot").offsetTop}px`;
  }
  function hideTip() { $("fo-tip").hidden = true; }

  function renderTable(hours) {
    const table = $("fo-table");
    const head = el("tr");
    ["Hour", ...SERIES.map(([, n]) => n)].forEach((h) => head.append(el("th", null, h)));
    const rows = hours.filter((h) => h.focus + h.neutral + h.distraction > 0).map((h) => {
      const tr = el("tr");
      tr.append(el("td", null, `${String(h.hour).padStart(2, "0")}:00`), ...SERIES.map(([k]) => el("td", null, hm(h[k]))));
      return tr;
    });
    if (!rows.length) {
      const tr = el("tr");
      const td = el("td", null, "No app time yet today.");
      td.colSpan = 4;
      tr.append(td);
      rows.push(tr);
    }
    const thead = el("thead"), tbody = el("tbody");
    thead.append(head);
    tbody.append(...rows);
    table.replaceChildren(thead, tbody);
  }

  $("fo-table-btn").addEventListener("click", () => {
    fo.table = !fo.table;
    $("fo-table").hidden = !fo.table;
    $("fo-plot").hidden = fo.table;
    $("fo-legend").hidden = fo.table;
    $("fo-table-btn").textContent = fo.table ? "Show as a chart" : "Show as a table";
    hideTip();
  });

  async function makeToken() {
    const btn = $("fo-token");
    btn.disabled = true;
    try {
      const r = await api.appsToken();
      $("fo-token-val").textContent = r.token;
      $("fo-token-val").hidden = false;
      $("fo-token-copy").hidden = false;
      fo.data.token_set = true;
      toast("New secret made. It's shown once: paste it when the installer asks.");
    } catch (e) { toast(e.message); } finally { btn.disabled = false; }
  }
  // Making a new secret disconnects laptops using the old one, so that takes a second tap.
  const tokenBtn = $("fo-token");
  let armed = false, armTimer = null;
  tokenBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (!fo.data?.token_set || armed) {
      armed = false;
      tokenBtn.classList.remove("armed");
      tokenBtn.textContent = "Make a secret";
      makeToken();
      return;
    }
    armed = true;
    tokenBtn.classList.add("armed");
    tokenBtn.textContent = "Confirm";
    tokenBtn.title = "A new secret replaces the old one: laptops already connected will need it too.";
    armTimer = setTimeout(() => { armed = false; tokenBtn.classList.remove("armed"); tokenBtn.textContent = "Make a secret"; }, 4000);
  });
  document.addEventListener("click", (e) => {
    if (e.target !== tokenBtn && armed) { clearTimeout(armTimer); armed = false; tokenBtn.classList.remove("armed"); tokenBtn.textContent = "Make a secret"; }
  });
  $("fo-token-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("fo-token-val").textContent); toast("Copied."); } catch { toast("Select the secret and copy it."); }
  });

  return { refreshInbox, refreshFocus };
}
