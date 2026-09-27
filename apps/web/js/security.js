// Phase 7: the passkey lock (and its sign-in screen), notifications on this device, and backup checks.
import { api, DEVICE } from "./api.js";

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
const when = (iso) => {
  if (!iso) return "never";
  const d = new Date(iso), now = new Date();
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  return d.toDateString() === now.toDateString() ? `today ${hm}` : `${d.toLocaleDateString([], { day: "numeric", month: "short" })} ${hm}`;
};
const kb = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.round(n / 1e3)} KB`);

// Tap once to arm ("Confirm"), again within 4 s to do it: for things you'd regret doing by accident.
function twoTap(label, cls, run) {
  const b = el("button", cls, label);
  b.type = "button";
  let t = null;
  const disarm = () => { clearTimeout(t); b.classList.remove("armed"); b.textContent = label; };
  b.addEventListener("click", (e) => {
    e.stopPropagation();
    if (!b.classList.contains("armed")) { b.classList.add("armed"); b.textContent = "Confirm"; t = setTimeout(disarm, 4000); return; }
    disarm();
    run();
  });
  return b;
}

/* ---------- Passkeys (WebAuthn): the server's JSON uses base64url, the browser wants bytes ---------- */
const toBuf = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), (c) => c.charCodeAt(0)).buffer;
const toB64u = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const canPasskey = () => !!window.PublicKeyCredential && window.isSecureContext;

async function makePasskey(o) {
  const cred = await navigator.credentials.create({ publicKey: {
    ...o, challenge: toBuf(o.challenge), user: { ...o.user, id: toBuf(o.user.id) },
    excludeCredentials: (o.excludeCredentials || []).map((c) => ({ ...c, id: toBuf(c.id) })),
  } });
  return { id: cred.id, rawId: toB64u(cred.rawId), type: cred.type, authenticatorAttachment: cred.authenticatorAttachment,
    response: { clientDataJSON: toB64u(cred.response.clientDataJSON), attestationObject: toB64u(cred.response.attestationObject),
      transports: cred.response.getTransports?.() || [] },
    clientExtensionResults: cred.getClientExtensionResults?.() || {} };
}

async function usePasskey(o) {
  const cred = await navigator.credentials.get({ publicKey: {
    ...o, challenge: toBuf(o.challenge), allowCredentials: (o.allowCredentials || []).map((c) => ({ ...c, id: toBuf(c.id) })),
  } });
  const r = cred.response;
  return { id: cred.id, rawId: toB64u(cred.rawId), type: cred.type, authenticatorAttachment: cred.authenticatorAttachment,
    response: { clientDataJSON: toB64u(r.clientDataJSON), authenticatorData: toB64u(r.authenticatorData),
      signature: toB64u(r.signature), userHandle: r.userHandle ? toB64u(r.userHandle) : null },
    clientExtensionResults: cred.getClientExtensionResults?.() || {} };
}

function passkeyError(e) {
  if (e.name === "NotAllowedError") return "Cancelled, or the passkey prompt timed out.";
  if (e.name === "InvalidStateError") return "This device already has a Cardinal passkey. Use Sign in instead.";
  if (e.name === "SecurityError") return "Passkeys only work on Cardinal's own https address.";
  return e.message;
}

async function signIn() {
  const o = await api.loginOptions();
  return api.loginVerify({ ceremony: o.ceremony, credential: await usePasskey(o.options) });
}
async function addPasskey(code) {
  const o = await api.registerOptions(code);
  return api.registerVerify({ ceremony: o.ceremony, credential: await makePasskey(o.options), name: DEVICE, code });
}

export function createSecurity({ toast }) {
  /* ---------- The lock screen ---------- */
  const lockErr = (msg) => { $("lock-err").textContent = msg || ""; };
  window.addEventListener("cardinal:locked", () => {
    if (!$("lock").hidden) return;
    $("lock").hidden = false;
    document.body.classList.add("is-locked");
    if (!canPasskey()) lockErr("This browser can't use passkeys here. Open Cardinal at its https address.");
    setTimeout(() => $("lock-in").focus(), 50);
  });
  $("lock-in").addEventListener("click", async () => {
    lockErr("");
    $("lock-in").disabled = true;
    try { await signIn(); location.reload(); } catch (e) { lockErr(passkeyError(e)); } finally { $("lock-in").disabled = false; }
  });
  $("lock-code-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    lockErr("");
    const btn = e.target.querySelector("button");
    btn.disabled = true;
    try { await addPasskey(e.target.code.value.trim()); location.reload(); } catch (err) { lockErr(passkeyError(err)); } finally { btn.disabled = false; }
  });

  /* ---------- Access → Security ---------- */
  async function refreshSecurity() {
    let st;
    try { st = await api.authStatus(); } catch (e) { $("sec-note").textContent = e.message; return; }
    $("sec-meta").textContent = st.locked ? "LOCKED WITH PASSKEYS" : "OPEN ON YOUR TAILNET";
    $("sec-state").replaceChildren(el("span", `tag ${st.locked ? "ok" : "warn"}`, st.locked ? "Lock on" : "Lock off"),
      el("span", "note", st.locked ? "Every browser needs its passkey (Face ID, Touch ID or Windows Hello)."
        : "Anyone on your Tailscale network can open Cardinal. Only your own devices are on it."));
    const acts = [];
    if (canPasskey()) {
      acts.push(button(st.passkeys ? "btn" : "btn primary", "Add a passkey on this device", async (e) => {
        e.target.disabled = true;
        try { await addPasskey(); toast("Passkey saved. This device is signed in."); refreshSecurity(); }
        catch (err) { toast(passkeyError(err)); } finally { e.target.disabled = false; }
      }));
    }
    if (!st.locked && st.passkeys && st.signed_in) {
      acts.push(button("btn primary", "Turn the lock on", async () => {
        try { await api.setLock(true); toast("Locked. Other browsers now need their passkey."); refreshSecurity(); } catch (err) { toast(err.message); }
      }));
    }
    if (st.locked) {
      acts.push(twoTap("Turn the lock off", "btn", async () => {
        try {
          if (!(await api.authStatus()).fresh) await signIn();  // a fresh Face ID / Touch ID check first
          await api.setLock(false);
          toast("The lock is off.");
          refreshSecurity();
        } catch (err) { toast(passkeyError(err)); }
      }));
    }
    if (st.passkeys && (st.signed_in || !st.locked)) {
      acts.push(button("btn", "Sign in another device", async () => {
        try {
          const c = await api.loginCode();
          const box = $("sec-code");
          box.hidden = false;
          box.replaceChildren(el("b", null, c.code), el("span", "note small",
            `On the other device: open Cardinal → "New device? Use a one-time code". Works once, until ${when(c.expires).replace("today ", "")}.`));
        } catch (err) { toast(err.message); }
      }));
    }
    if (st.signed_in) {
      acts.push(button("linkish", "Sign out", async () => {
        try { await api.logout(); location.reload(); } catch (err) { toast(err.message); }
      }));
    }
    $("sec-actions").replaceChildren(...acts);
    $("sec-note").textContent = !canPasskey() ? "Passkeys need Cardinal's https address (not a local copy on http)."
      : !st.passkeys ? "Add a passkey on each device you use (iPhone, Mac, G14), then turn the lock on. A passkey made on the iPhone also works on the Mac through iCloud Keychain."
      : !st.signed_in && !st.locked ? "This browser isn't signed in. Add a passkey here (or sign in) before turning the lock on." : "";

    try {
      const d = await api.passkeys();
      $("sec-keys").replaceChildren(...(d.passkeys.length ? d.passkeys.map((p) => {
        const li = el("li");
        const info = el("div");
        info.append(el("b", null, p.name), el("span", "sub", `Made ${when(p.created)} · last used ${when(p.last_used)}${p.synced ? " · synced" : ""}`));
        li.append(info, twoTap("Remove", "linkish", async () => {
          try { await api.deletePasskey(p.id); refreshSecurity(); } catch (err) { toast(err.message); }
        }));
        return li;
      }) : [el("li", "empty-row", "No passkeys yet.")]));
      $("sec-sessions").replaceChildren(...(d.sessions.length ? d.sessions.map((x) => {
        const li = el("li");
        const info = el("div");
        info.append(el("b", null, x.device || "Browser"), el("span", "sub", `Signed in ${when(x.created)} · seen ${when(x.last_seen)}`));
        li.append(info, x.current ? el("span", "tag ok", "This one") : button("linkish", "Sign out", async () => {
          try { await api.endSession(x.id); refreshSecurity(); } catch (err) { toast(err.message); }
        }));
        return li;
      }) : [el("li", "empty-row", "None.")]));
    } catch (e) { $("sec-keys").replaceChildren(el("li", "empty-row", e.message)); }
  }

  /* ---------- Access → Notifications ---------- */
  const isIOS = /iPhone|iPad/.test(navigator.userAgent) || (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);
  const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  async function registration() {
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) return null;
    return (await navigator.serviceWorker.getRegistration()) || null;
  }

  let pushState = null;
  async function refreshNotify() {
    let st;
    try { st = await api.pushSettings(); } catch (e) { $("nt-note").textContent = e.message; return; }
    pushState = st;
    const reg = await registration();
    const sub = reg ? await reg.pushManager.getSubscription() : null;
    const on = !!(sub && st.devices.some((d) => d.endpoint === sub.endpoint));
    $("nt-meta").textContent = on ? "ON FOR THIS DEVICE" : "OFF FOR THIS DEVICE";
    const acts = [];
    let note = "";
    if (!reg) {
      note = isIOS && !standalone ? "On iPhone, open Cardinal from its Home Screen icon, then turn notifications on here (iOS 16.4 or later)."
        : "This browser can't get notifications from Cardinal here.";
    } else if (Notification.permission === "denied") {
      note = "Notifications are blocked for Cardinal in this browser's settings. Allow them there, then come back.";
    } else if (on) {
      acts.push(button("btn", "Send a test", async (e) => {
        e.target.disabled = true;
        try { await api.pushTest(sub.endpoint); toast("Sent. It should arrive in a few seconds."); } catch (err) { toast(err.message); } finally { e.target.disabled = false; }
      }));
      acts.push(button("linkish", "Turn off on this device", async () => {
        try { await api.pushUnsubscribe(sub.endpoint); await sub.unsubscribe(); refreshNotify(); } catch (err) { toast(err.message); }
      }));
    } else {
      acts.push(button("btn primary", "Turn on for this device", async (e) => {
        e.target.disabled = true;
        try {
          if ((await Notification.requestPermission()) !== "granted") throw new Error("Notifications weren't allowed.");
          const { key } = await api.pushKey();
          const s = (await reg.pushManager.getSubscription())
            || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: new Uint8Array(toBuf(key)) });
          await api.pushSubscribe(s.toJSON());
          toast("Notifications are on for this device.");
          refreshNotify();
        } catch (err) { toast(err.message); } finally { e.target.disabled = false; }
      }));
    }
    $("nt-actions").replaceChildren(...acts);
    $("nt-note").textContent = note;

    $("nt-kinds").replaceChildren(...Object.entries(st.labels).map(([k, label]) => {
      const l = el("label", "tog");
      const box = Object.assign(el("input"), { type: "checkbox", checked: st.kinds[k] });
      box.addEventListener("change", () => save({ kinds: { ...pushState.kinds, [k]: box.checked } }));
      l.append(box, document.createTextNode(` ${label}`));
      return l;
    }));
    const [from, to] = (st.quiet || "22:00-06:00").split("-");
    $("nt-quiet-on").checked = !!st.quiet;
    $("nt-quiet-from").value = from;
    $("nt-quiet-to").value = to;
    $("nt-quiet-from").disabled = $("nt-quiet-to").disabled = !st.quiet;
    $("nt-private").checked = st.private;
    $("nt-devices").replaceChildren(...(st.devices.length ? st.devices.map((d) => {
      const li = el("li");
      const info = el("div");
      info.append(el("b", null, d.device || "Browser"), el("span", "sub", `Last delivered ${when(d.last_ok)}`));
      li.append(info, sub && d.endpoint === sub.endpoint ? el("span", "tag ok", "This one")
        : button("linkish", "Remove", async () => { try { await api.pushForget(d.id); refreshNotify(); } catch (err) { toast(err.message); } }));
      return li;
    }) : [el("li", "empty-row", "No devices yet.")]));
  }
  async function save(body) {
    try { await api.savePushSettings(body); refreshNotify(); } catch (e) { toast(e.message); }
  }
  const saveQuiet = () => save({ quiet: $("nt-quiet-on").checked ? `${$("nt-quiet-from").value}-${$("nt-quiet-to").value}` : "" });
  ["nt-quiet-on", "nt-quiet-from", "nt-quiet-to"].forEach((id) => $(id).addEventListener("change", saveQuiet));
  $("nt-private").addEventListener("change", () => save({ private: $("nt-private").checked }));

  /* ---------- Access → Backups ---------- */
  async function refreshBackups() {
    let b;
    try { b = await api.backups(); } catch (e) { $("bk-note").textContent = e.message; return; }
    const d = b.drill;
    $("bk-meta").textContent = !d ? "NOT CHECKED YET" : d.ok ? "LAST CHECK PASSED" : "NEEDS A LOOK";
    const box = $("bk-state");
    box.replaceChildren();
    if (d) {
      const head = el("p", "bk-line");
      head.append(el("span", `tag ${d.ok ? "ok" : "bad"}`, d.ok ? "Restores fine" : "Problem"),
        document.createTextNode(` Checked ${when(d.checked_at)}${d.backup ? `: ${d.backup}, ${kb(d.size)}, ${d.age_hours} h old` : ""}.`));
      box.append(head);
      if (d.rows) {
        const rows = Object.entries(d.rows).filter(([, [, live]]) => live > 0)
          .map(([t, [bk, live]]) => `${t} ${bk}/${live}`).join(" · ");
        box.append(el("p", "note small", `Integrity ${d.integrity}, ${d.tables} tables. Rows in the backup / now: ${rows}.`));
      }
      [...(d.problems || []), ...(d.notes || [])].forEach((t) => box.append(el("p", "note small", t)));
    } else box.append(el("p", "note small", "Cardinal checks the newest backup once a week, after the nightly copy."));
    $("bk-list").replaceChildren(...(b.backups.length ? b.backups.slice(0, 5).map((f) => {
      const li = el("li");
      const info = el("div");
      info.append(el("b", null, f.name), el("span", "sub", `${kb(f.size)} · ${when(f.modified)}`));
      li.append(info);
      return li;
    }) : [el("li", "empty-row", "No backups yet.")]));
  }
  $("bk-drill").addEventListener("click", async () => {
    const btn = $("bk-drill");
    btn.disabled = true;
    $("bk-note").textContent = "Restoring the newest backup into a scratch folder…";
    try { const r = await api.runDrill(); $("bk-note").textContent = r.ok ? "It restores fine." : r.problems[0]; refreshBackups(); }
    catch (e) { $("bk-note").textContent = e.message; } finally { btn.disabled = false; }
  });

  // A new secret replaces the old one (a Mac already copying would need it too), so it takes a second tap.
  const makeToken = async () => {
    try {
      const r = await api.backupToken();
      $("bk-token-val").textContent = r.token;
      $("bk-token-val").hidden = $("bk-token-copy").hidden = false;
      toast("New secret made. It's shown once: paste it when the installer asks.");
    } catch (e) { toast(e.message); }
  };
  $("bk-token").replaceWith(Object.assign(twoTap("Make a secret", "btn", makeToken), { id: "bk-token" }));
  $("bk-token-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("bk-token-val").textContent); toast("Copied."); } catch { toast("Select the secret and copy it."); }
  });

  return { refresh: () => Promise.all([refreshSecurity(), refreshNotify(), refreshBackups()]) };
}
