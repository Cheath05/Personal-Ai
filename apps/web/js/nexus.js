// The Nexus: animated agent cores on a full-screen canvas.
// The selected agent is the large core; the rest orbit it. States: idle, listening, thinking, speaking.

const TAU = Math.PI * 2;
const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
const rgb = (hex) => { const n = parseInt(hex.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; };
const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${clamp(a, 0, 1).toFixed(3)})`;
const ICE = [225, 240, 255];

// Who Cardinal visibly "asks" while thinking (pulses along links).
const DELEGATES = { cardinal: ["ordinal", "vector", "axiom"], ordinal: ["relay"], delta: ["sigma"] };

export function createNexus({ canvas, stage, agents, onSelect, reduceMotion = false }) {
  const ctx = canvas.getContext("2d");
  let W = 0, H = 0, sel = 0, view = "nexus", state = "idle";
  let t = 0, last = 0, orbitA = 0, amp = 0, burst = 0, lastRipple = 0, glyphT = 0, running = false;
  const mix = { listen: 0, think: 0, speak: 0 };
  const ripples = [];
  let glyphs = [];

  const P = [];
  const NP = 560;
  for (let k = 0; k < NP; k++) {
    const y = 1 - (k / (NP - 1)) * 2, rr = Math.sqrt(1 - y * y), th = k * 2.399963;
    P.push({ x: Math.cos(th) * rr, y, z: Math.sin(th) * rr, ph: Math.random() * TAU, band: k % 3, ba: Math.random() * TAU, bs: 0.8 + Math.random() * 0.8 });
  }
  const bandTilt = [[0.4, 0.2], [-0.9, 0.9], [1.3, -0.6]];
  const dust = Array.from({ length: 80 }, () => ({ x: Math.random(), y: Math.random(), s: Math.random() * 1.4 + 0.3, v: Math.random() * 0.01 + 0.004 }));

  const nodes = agents.map((a, i) => {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "node";
    el.setAttribute("aria-label", `Select ${a.name}, ${a.type}`);
    const label = document.createElement("span");
    label.className = "nm";
    const dot = document.createElement("i");
    label.append(dot, document.createTextNode(a.name));
    el.append(label);
    el.addEventListener("click", () => onSelect(i));
    el.addEventListener("mouseenter", () => { nodes[i].hover = 1; });
    el.addEventListener("mouseleave", () => { nodes[i].hover = 0; });
    stage.append(el);
    return { a, c: rgb(a.color), el, dot, x: 0, y: 0, r: 0, tx: 0, ty: 0, tr: 0, init: false,
             rot: Math.random() * 6, flash: 0, hover: 0, hv: 0, pulses: [] };
  });

  function regenGlyphs() {
    glyphs = Array.from({ length: 26 }, () => Math.random() < 0.5
      ? (Math.random() * 256 | 0).toString(16).toUpperCase().padStart(2, "0")
      : (Math.random() * 16 | 0).toString(2).padStart(4, "0"));
  }
  regenGlyphs();

  function simSpeak(t) {
    const gate = 0.5 + 0.5 * Math.sin(t * 3.1) + 0.35 * Math.sin(t * 7.3 + 1.1);
    const v = Math.abs(Math.sin(t * 12.7) * Math.sin(t * 5.3 + 0.7)) * 0.8 + 0.2 * Math.abs(Math.sin(t * 23.1));
    return gate > 0.3 ? v : v * 0.15;
  }

  function layout(nex) {
    let ox = 0, oy = 0, aw = W, ah = H;
    if (nex) { const r = stage.getBoundingClientRect(); ox = r.left; oy = r.top; aw = r.width; ah = r.height; }
    const wide = nex && window.innerWidth >= 1100;
    const cx = aw / 2, cy = ah * (wide ? 0.47 : 0.5);
    const Rb = nex ? Math.min(aw * (wide ? 0.1 : 0.19), ah * 0.19) : Math.min(W, H) * 0.13;
    const Rs = Math.max(15, Rb * 0.25);
    let rx = nex ? Math.min(aw / 2 - (wide ? 430 : Rs + 22), Rb * 3.3) : Math.min(W, H) * 0.44;
    rx = Math.max(rx, Rb * 1.9);
    const ry = nex ? Math.min(ah / 2 - Rs - 30, Rb * 2.35) : Math.min(W, H) * 0.34;
    const n = nodes.length - 1;
    let k = 0;
    nodes.forEach((nd, i) => {
      if (i === sel) { nd.tx = cx; nd.ty = cy; nd.tr = Rb; }
      else { const a = orbitA + (k / n) * TAU - Math.PI / 2; nd.tx = cx + Math.cos(a) * rx; nd.ty = cy + Math.sin(a) * ry; nd.tr = Rs; k++; }
    });
    return { ox, oy, cx, cy, Rb, Rs, rx, ry };
  }

  function drawSphere(n, X, Y, R, d, isSel, A) {
    const c = n.c, stride = d > 0.6 ? 1 : d > 0.25 ? 2 : 4;
    const ry = n.rot, rx = 0.38 + Math.sin(t * 0.21 + n.rot) * 0.12;
    const cyR = Math.cos(ry), syR = Math.sin(ry), cxR = Math.cos(rx), sxR = Math.sin(rx);
    const th = isSel ? mix.think : 0, sp = isSel ? mix.speak : 0, ls = isSel ? mix.listen : 0;
    const base = R * 0.62 * (1 + 0.025 * Math.sin(t * 1.3) - ls * 0.07 + sp * A * 0.12);
    for (let i = 0; i < NP; i += stride) {
      const p = P[i];
      let x = p.x, y = p.y, z = p.z;
      if (th > 0.01) {
        const ang = p.ba + t * p.bs * 2.2, tl = bandTilt[p.band];
        const bx = Math.cos(ang) * 1.18, bz = Math.sin(ang) * 1.18;
        const c1 = Math.cos(tl[0]), s1 = Math.sin(tl[0]);
        const by2 = -bz * s1, bz2 = bz * c1;
        const c2 = Math.cos(tl[1]), s2 = Math.sin(tl[1]);
        const bx3 = bx * c2 - by2 * s2, by3 = bx * s2 + by2 * c2;
        x += (bx3 - x) * th; y += (by3 - y) * th; z += (bz2 - z) * th;
      }
      const f = 1 + sp * A * 0.2 * Math.sin(t * 11 + p.ph * 3 + y * 4) + 0.02 * Math.sin(t * 0.9 + p.ph);
      const X1 = x * cyR - z * syR, Z1 = x * syR + z * cyR;
      const Y1 = y * cxR - Z1 * sxR, Z2 = y * sxR + Z1 * cxR;
      const depth = (Z2 + 1) / 2;
      ctx.fillStyle = depth > 0.62 ? rgba(ICE, 0.35 + depth * 0.6) : rgba(c, 0.18 + depth * 0.75);
      const s = (0.55 + depth * 1.7) * (R > 40 ? 1 : 0.85);
      ctx.fillRect(X + X1 * base * f - s / 2, Y + Y1 * base * f - s / 2, s, s);
    }
    const cr = R * 0.34 * (1 + A * 0.6);
    const cg = ctx.createRadialGradient(X, Y, 0, X, Y, cr);
    cg.addColorStop(0, `rgba(255,255,255,${(0.5 + A * 0.45).toFixed(3)})`);
    cg.addColorStop(0.4, rgba(c, 0.35 + A * 0.3));
    cg.addColorStop(1, rgba(c, 0));
    ctx.fillStyle = cg; ctx.beginPath(); ctx.arc(X, Y, cr, 0, TAU); ctx.fill();
  }

  function segRing(X, Y, r, segs, rot, w, col) {
    const span = TAU / segs, gap = Math.min(0.25, span * 0.3);
    ctx.lineWidth = w; ctx.strokeStyle = col;
    for (let s = 0; s < segs; s++) {
      const a0 = rot + s * span + gap / 2, a1 = rot + (s + 1) * span - gap / 2 - (s % 2 ? span * 0.3 : 0);
      ctx.beginPath(); ctx.arc(X, Y, r, a0, a1); ctx.stroke();
    }
  }

  function drawHUD(n, X, Y, R, d, A) {
    const c = n.c, th = mix.think, sp = mix.speak, ls = mix.listen, spd = 1 + th * 3.2 + ls * 0.8;
    const tr = R * 1.55, ticks = 96;
    for (let i = 0; i < ticks; i++) {
      const a = t * 0.05 * spd + (i / ticks) * TAU, major = i % 8 === 0, len = major ? R * 0.07 : R * 0.03;
      ctx.strokeStyle = rgba(c, (major ? 0.8 : 0.4) * d); ctx.lineWidth = major ? 1.6 : 1;
      ctx.beginPath(); ctx.moveTo(X + Math.cos(a) * tr, Y + Math.sin(a) * tr); ctx.lineTo(X + Math.cos(a) * (tr - len), Y + Math.sin(a) * (tr - len)); ctx.stroke();
    }
    const tight = 1 - ls * 0.06;
    segRing(X, Y, R * 1.4 * tight, 3, -t * 0.3 * spd, 2, rgba(ICE, 0.75 * d));
    segRing(X, Y, R * 1.3 * tight, 7, t * 0.18 * spd, 6, rgba(c, 0.28 * d));
    segRing(X, Y, R * 0.98, 5, t * 0.55 * spd, 1.4, rgba(ICE, 0.55 * d));
    const dots = 60, dr = R * 1.18 * tight, filled = th > 0.3 ? Math.floor((t * 24) % dots) : Math.round(dots * 0.74);
    const ds = Math.max(2, R * 0.022);
    for (let q = 0; q < dots; q++) {
      const da = -Math.PI / 2 + (q / dots) * TAU, x = X + Math.cos(da) * dr, y = Y + Math.sin(da) * dr;
      if (q < filled) { ctx.fillStyle = rgba(ICE, (0.5 + A * 0.5) * d); ctx.fillRect(x - ds / 2, y - ds / 2, ds, ds); }
      else { ctx.strokeStyle = rgba(ICE, 0.28 * d); ctx.lineWidth = 1; ctx.strokeRect(x - ds / 2, y - ds / 2, ds, ds); }
    }
    const barAmt = Math.max(sp, ls);
    if (barAmt > 0.01) {
      const nb = 90, r0 = R * 1.24;
      ctx.lineWidth = Math.max(1.5, R * 0.018); ctx.lineCap = "round";
      for (let b = 0; b < nb; b++) {
        const bb = Math.min(b, nb - b);
        const vS = Math.abs(Math.sin(bb * 0.55 + t * 9) * Math.sin(bb * 0.21 - t * 4.1)) * A;
        const vL = (0.2 + 0.8 * Math.abs(Math.sin(b * 0.9 + t * 6.3) * Math.sin(b * 0.33 - t * 2.7))) * (0.35 + 0.65 * A);
        const h = R * 0.38 * (sp * (0.12 + vS) + ls * vL * 0.7) * d;
        const ang = (b / nb) * TAU - Math.PI / 2;
        ctx.strokeStyle = rgba(c, 0.9 * barAmt);
        ctx.beginPath(); ctx.moveTo(X + Math.cos(ang) * r0, Y + Math.sin(ang) * r0); ctx.lineTo(X + Math.cos(ang) * (r0 + h), Y + Math.sin(ang) * (r0 + h)); ctx.stroke();
      }
      ctx.lineCap = "butt";
    }
    for (let i = ripples.length - 1; i >= 0; i--) {
      const rp = ripples[i], al = (1 - rp.age / 1.4) * 0.5 * d;
      if (al <= 0) { ripples.splice(i, 1); continue; }
      ctx.strokeStyle = rgba(c, al); ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(X, Y, R * (1.25 + rp.age * 1.5), 0, TAU); ctx.stroke();
    }
    if (th > 0.01) {
      const sw = t * 3.4;
      for (let w = 0; w < 10; w++) {
        ctx.fillStyle = rgba(c, 0.05 * th * (1 - w / 10) * d);
        ctx.beginPath(); ctx.moveTo(X, Y); ctx.arc(X, Y, R * 1.52, sw - (w + 1) * 0.07, sw - w * 0.07); ctx.closePath(); ctx.fill();
      }
      ctx.strokeStyle = rgba(ICE, 0.7 * th * d); ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.moveTo(X, Y); ctx.lineTo(X + Math.cos(sw) * R * 1.52, Y + Math.sin(sw) * R * 1.52); ctx.stroke();
      ctx.font = `${Math.max(9, Math.round(R * 0.075))}px 'JetBrains Mono', monospace`;
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      glyphs.forEach((g, i) => {
        const ga = (i / glyphs.length) * TAU - t * 0.4;
        const bright = (i * 7 + (t * 10 | 0)) % 3 === 0;
        ctx.fillStyle = rgba(i % 5 === 0 ? ICE : c, (0.35 + (bright ? 0.5 : 0)) * th * d);
        ctx.fillText(g, X + Math.cos(ga) * R * 1.75, Y + Math.sin(ga) * R * 1.75);
      });
    }
    for (let gl = 0; gl < 3; gl++) {
      const dir = gl % 2 ? -1 : 1, ga = t * (0.6 + gl * 0.25) * dir + gl * 2.1, gr = R * (1.4 - gl * 0.08) * tight;
      for (let k = 0; k < 6; k++) {
        const a2 = ga - k * 0.035 * dir;
        ctx.fillStyle = rgba(ICE, (0.9 - k * 0.15) * d * (1 - th * 0.6));
        ctx.fillRect(X + Math.cos(a2) * gr - 1.2, Y + Math.sin(a2) * gr - 1.2, 2.4, 2.4);
      }
    }
  }

  function drawSmall(n, X, Y, R) {
    const c = n.c;
    ctx.strokeStyle = rgba(c, 0.7 + n.hv * 0.3); ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.arc(X, Y, R * 1.25, t * 0.8 + n.rot, t * 0.8 + n.rot + TAU * 0.7); ctx.stroke();
    ctx.strokeStyle = rgba(c, 0.3); ctx.beginPath(); ctx.arc(X, Y, R * 1.45, -t * 0.5, -t * 0.5 + TAU * 0.35); ctx.stroke();
    if (n.flash > 0.01) { ctx.strokeStyle = rgba(c, n.flash); ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(X, Y, R * (1.5 + (1 - n.flash) * 0.8), 0, TAU); ctx.stroke(); }
  }

  function frame(now, snap = false) {
    const dt = snap ? 1 : Math.min(0.05, (now - last) / 1000 || 0.016);
    last = now;
    if (!snap) t += dt;
    const nex = view === "nexus";
    const L = layout(nex);
    if (!snap) orbitA += dt * 0.035;
    const kL = snap ? 1 : 1 - Math.exp(-dt * 4.5), kM = snap ? 1 : 1 - Math.exp(-dt * 3.5);
    mix.listen += ((state === "listening" ? 1 : 0) - mix.listen) * kM;
    mix.think += ((state === "thinking" ? 1 : 0) - mix.think) * kM;
    mix.speak += ((state === "speaking" ? 1 : 0) - mix.speak) * kM;
    const target = mix.speak * (0.2 + 0.8 * simSpeak(t)) + mix.listen * (0.25 + 0.5 * Math.abs(Math.sin(t * 5.7) * Math.sin(t * 2.2))) + 0.04 + 0.03 * Math.sin(t * 1.3);
    amp += (target - amp) * (snap ? 1 : 1 - Math.exp(-dt * 16));
    if (mix.speak > 0.5 && amp > 0.62 && t - lastRipple > 0.28) { ripples.push({ age: 0 }); lastRipple = t; }
    ripples.forEach((r) => { r.age += snap ? 0 : dt; });
    if (t - glyphT > 0.12) { regenGlyphs(); glyphT = t; }
    burst = Math.max(0, burst - dt * 1.3);

    nodes.forEach((n, i) => {
      if (!n.init || snap) { n.x = n.tx; n.y = n.ty; n.r = n.tr; n.init = true; }
      n.x += (n.tx - n.x) * kL; n.y += (n.ty - n.y) * kL; n.r += (n.tr - n.r) * kL;
      n.rot += dt * (0.3 + (i === sel ? mix.think * 1.6 : 0));
      n.flash = Math.max(0, n.flash - dt * 1.6);
      n.hv += (n.hover - n.hv) * (snap ? 1 : 1 - Math.exp(-dt * 8));
    });

    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "rgba(200,225,255,0.22)";
    dust.forEach((p) => {
      if (!snap) { p.y -= p.v * dt; if (p.y < 0) { p.y = 1; p.x = Math.random(); } }
      ctx.fillRect(p.x * W, p.y * H, p.s, p.s);
    });

    const C = nodes[sel], cc = C.c, CX = L.ox + C.x, CY = L.oy + C.y;
    ctx.setLineDash([2, 6]); ctx.strokeStyle = "rgba(200,225,255,0.1)"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.ellipse(L.ox + L.cx, L.oy + L.cy, L.rx, Math.max(1, L.ry), 0, 0, TAU); ctx.stroke(); ctx.setLineDash([]);

    const delegates = DELEGATES[C.a.id] || [];
    nodes.forEach((n, i) => {
      if (i === sel) return;
      const X = L.ox + n.x, Y = L.oy + n.y, dx = X - CX, dy = Y - CY, len = Math.hypot(dx, dy) || 1, ux = dx / len, uy = dy / len;
      if (len < C.r * 1.6 + n.r * 1.6) return;
      const x0 = CX + ux * C.r * 1.6, y0 = CY + uy * C.r * 1.6, x1 = X - ux * n.r * 1.6, y1 = Y - uy * n.r * 1.6;
      const g = ctx.createLinearGradient(x0, y0, x1, y1);
      g.addColorStop(0, rgba(cc, 0.35)); g.addColorStop(1, rgba(n.c, 0.35 + n.hv * 0.4));
      ctx.strokeStyle = g; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
      if (!snap) {
        const deleg = delegates.includes(n.a.id);
        if (Math.random() < (0.12 + (deleg ? mix.think * 3 : 0)) * dt) n.pulses.push({ p: 0, out: true });
        if (deleg && mix.speak > 0.5 && Math.random() < 0.8 * dt) n.pulses.push({ p: 0, out: false });
      }
      for (let q = n.pulses.length - 1; q >= 0; q--) {
        const pu = n.pulses[q];
        pu.p += (snap ? 0 : dt) * 0.75;
        if (pu.p >= 1) { if (pu.out) n.flash = 1; n.pulses.splice(q, 1); continue; }
        for (let k = 0; k < 5; k++) {
          const pp = clamp(pu.p - k * 0.025, 0, 1), f = pu.out ? pp : 1 - pp;
          ctx.fillStyle = rgba(k === 0 ? [255, 255, 255] : (pu.out ? cc : n.c), 0.9 - k * 0.17);
          const s = 3 - k * 0.4;
          ctx.fillRect(x0 + (x1 - x0) * f - s / 2, y0 + (y1 - y0) * f - s / 2, s, s);
        }
      }
    });

    nodes.forEach((n, i) => {
      const X = L.ox + n.x, Y = L.oy + n.y, R = n.r, isSel = i === sel;
      const d = clamp((R - L.Rs) / Math.max(1, L.Rb - L.Rs), 0, 1);
      const A = isSel ? amp : 0.05;
      const gg = ctx.createRadialGradient(X, Y, 0, X, Y, R * 2.3);
      gg.addColorStop(0, rgba(n.c, 0.26 + A * 0.25 + n.flash * 0.3 + n.hv * 0.2));
      gg.addColorStop(1, rgba(n.c, 0));
      ctx.fillStyle = gg; ctx.beginPath(); ctx.arc(X, Y, R * 2.3, 0, TAU); ctx.fill();
      drawSphere(n, X, Y, R, d, isSel, A);
      if (d > 0.02) drawHUD(n, X, Y, R, d, A);
      if (d < 0.98) { ctx.globalAlpha = 1 - d; drawSmall(n, X, Y, R); ctx.globalAlpha = 1; }
      if (nex) {
        const hs = R * 2.6;
        n.el.style.width = `${hs}px`; n.el.style.height = `${hs}px`;
        n.el.style.transform = `translate(${n.x - hs / 2}px,${n.y - hs / 2}px)`;
      }
    });

    if (burst > 0.01) {
      ctx.strokeStyle = rgba(cc, burst * 0.7); ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(CX, CY, C.r * (1.2 + (1 - burst) * 2.6), 0, TAU); ctx.stroke();
      ctx.strokeStyle = rgba([255, 255, 255], burst * 0.4); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(CX, CY, C.r * (1 + (1 - burst) * 1.8), 0, TAU); ctx.stroke();
    }
  }

  function loop(now) { if (!running) return; frame(now); requestAnimationFrame(loop); }
  function start() { if (reduceMotion || running || document.hidden) return; running = true; last = performance.now(); requestAnimationFrame(loop); }
  function requestDraw() { if (!running) frame(performance.now(), true); }
  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = window.innerWidth; H = window.innerHeight;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    requestDraw();
  }

  document.addEventListener("visibilitychange", () => { if (document.hidden) running = false; else start(); });
  window.addEventListener("resize", resize);
  if (reduceMotion) window.addEventListener("scroll", requestDraw, { passive: true });
  resize();
  start();

  return {
    select(i) {
      sel = i;
      burst = 1;
      nodes.forEach((n, k) => { n.el.classList.toggle("sel", k === i); n.el.setAttribute("aria-pressed", String(k === i)); });
      requestDraw();
    },
    setState(s) { state = s; requestDraw(); },
    setView(v) { view = v; requestDraw(); },
    setStatus(level) { nodes.forEach((n) => { n.dot.className = level; }); },
  };
}
