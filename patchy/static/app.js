"use strict";
// Patchy front end: vanilla JS, no build step.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ls = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
};

const CARD_W = 240, CARD_H = 72;
const KIND_COLOR = { dante: "var(--dante)", analog: "var(--analog)", aes: "var(--aes)", madi: "var(--aes)",
  sdi: "var(--video)", hdmi: "var(--video)", dmx: "var(--light)", sacn: "var(--light)" };
const STATUS_COLOR = { ok: "var(--ok)", new: "var(--new)", missing: "var(--missing)", error: "var(--error)", manual: "var(--manual)" };
const STATUS_RANK = { manual: 0, ok: 1, new: 2, missing: 3, error: 4 };
const STATUS_TEXT = { ok: "Matches live", new: "Live, not documented", missing: "Documented, not live", error: "Live error", manual: "Documented",
  rerouted: "Rerouted", moved: "Changed (unlocked)" };
const KINDS = ["analog", "aes", "madi", "sdi", "hdmi", "dmx", "sacn", "ethernet", "usb", "other"];
const CATS = ["audio", "video", "lighting", "network", "control"];
// Signal family of each port type: patches stay within a family, and the layer filter uses it.
const FAMILY = { analog: "audio", aes: "audio", madi: "audio", dante: "audio", sdi: "video", hdmi: "video",
  dmx: "lighting", sacn: "lighting", ethernet: "network", usb: "usb", other: "other" };
const canPatch = (a, b) => {
  if (a === "dante" || b === "dante") return a === b;
  return FAMILY[a] === FAMILY[b] || a === "other" || b === "other";
};
const LAYERS = [["all", "All"], ["audio", "Audio"], ["video", "Video"], ["lighting", "Lighting"]];

let S = null;               // server state
let IX = {};                // indexes
let room = ls.get("sm-room", "all");
let view = ls.get("sm-view", "map");
let layer = ls.get("sm-layer", "all");   // all | audio | video | lighting
const narrow = () => window.matchMedia("(max-width: 760px)").matches;
const camKey = () => "sm-cam-" + (narrow() ? "phone" : "wide");   // phones and desktops keep their own view
let cam = ls.get(camKey(), null) || { x: 40, y: 20, k: 0.8 };
let liftedId = null;        // card being moved (touch: after press-and-hold)
let selected = null;        // {type:'device'|'edge'|'drift', id}
let query = "";

// ---------- API ----------
async function api(method, path, body, retry = true) {
  const r = await fetch(path, {
    method, headers: { "Content-Type": "application/json", "X-Token": ls.get("sm-token", "") },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401 && retry) {
    const t = prompt("Edit token (shown in Settings on the computer running Patchy):");
    if (!t) throw new Error("Edit token required");
    ls.set("sm-token", t.trim());
    return api(method, path, body, false);
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { toast(data.error || r.statusText); throw new Error(data.error); }
  return data;
}
async function load() {
  S = await (await fetch("/api/state")).json();
  index();
  render();
}
async function write(method, path, body) {
  const res = await api(method, path, body);
  await load();
  return res;
}
function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast"; t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

// ---------- indexes ----------
function index() {
  const dev = new Map(S.devices.map((d) => [d.id, d]));
  const port = new Map(), portsByDev = new Map(), connsByPort = new Map();
  for (const p of S.ports) {
    if (!dev.has(p.device_id)) continue;
    port.set(p.id, p);
    if (!portsByDev.has(p.device_id)) portsByDev.set(p.device_id, []);
    portsByDev.get(p.device_id).push(p);
  }
  const conns = S.connections.filter((c) => port.has(c.from_port) && port.has(c.to_port));
  for (const c of conns) {
    c.status = status(c, port);
    for (const pid of [c.from_port, c.to_port]) {
      if (!connsByPort.has(pid)) connsByPort.set(pid, []);
      connsByPort.get(pid).push(c);
    }
  }
  IX = { dev, port, portsByDev, connsByPort, conns };
}
// Router crosspoints (Videohub outputs): what is routed to an output now vs. what is documented.
// Locked outputs that change are drift; unlocked ones get re-routed during services, so they're only shown.
function xpStatus(p) {
  if (p.xp_live == null) return null;
  if (p.xp_live === p.xp_doc) return "ok";
  return p.xp_locked ? "rerouted" : "moved";
}
function xpCell(p) {
  const st = xpStatus(p);
  if (!st) return "";
  const src = IX.port.get(p.xp_live), was = IX.port.get(p.xp_doc);
  return `<div>⇐ ${esc(src ? pname(src.id) : "?")} <span class="pill ${st}">${STATUS_TEXT[st]}</span>${p.xp_locked ? ' <span class="sub">locked</span>' : ""}
    ${st !== "ok" && was ? ` <span class="sub">was ${esc(pname(was.id))}</span> <button class="mini" data-xpaccept="${p.id}" title="Accept as documented">✓</button>` : ""}</div>`;
}
const xpDrift = () => S.ports.filter((p) => xpStatus(p) === "rerouted" && IX.dev.has(p.device_id) && inRoom(IX.dev.get(p.device_id)));
// Ports read from a router or Dante are only listed when they're labelled or connected (an 80x80 hub has 160).
const worthShowing = (p) => (p.kind !== "dante" && p.source !== "videohub") || !!p.label
  || (IX.connsByPort.get(p.id) || []).length > 0;
function status(c, port) {
  const isDante = port.get(c.from_port).kind === "dante";
  if (c.live && c.live_severity && !["ok", "info"].includes(c.live_severity)) return "error";
  if (c.live) return c.documented ? "ok" : "new";
  if (isDante && c.documented) return "missing";
  return "manual";
}
const pdev = (pid) => IX.dev.get(IX.port.get(pid).device_id);
const pname = (pid) => { const p = IX.port.get(pid); return p.label ? `${p.name} (${p.label})` : p.name; };
const roomName = (id) => (S.rooms.find((r) => r.id === id) || {}).name || "Unassigned";
const seenAgo = (d) => {
  if (d.source !== "dante" && d.source !== "videohub") return null;
  if (!d.last_seen) return Infinity;
  return Date.now() / 1000 - d.last_seen;
};
const isOffline = (d) => { const a = seenAgo(d); return a !== null && a > (d.source === "videohub" ? 300 : (S.sync_minutes || 5) * 60 * 3 + 120); };
function ago(sec) {
  if (sec === Infinity || sec == null) return "never";
  if (sec < 90) return `${Math.round(sec)} s ago`;
  if (sec < 5400) return `${Math.round(sec / 60)} min ago`;
  if (sec < 172800) return `${Math.round(sec / 3600)} h ago`;
  return `${Math.round(sec / 86400)} days ago`;
}

// ---------- header ----------
function renderHeader() {
  if (typeof room === "number" && !S.rooms.some((r) => r.id === room)) room = "all";   // room was deleted
  const rooms = [{ id: "all", name: "All rooms" }, ...S.rooms];
  if (S.devices.some((d) => d.room_id == null)) rooms.push({ id: "none", name: "Unassigned" });
  $("#rooms").innerHTML = rooms.map((r) =>
    `<button data-room="${r.id}" class="${String(r.id) === String(room) ? "on" : ""}">${esc(r.name)}</button>`).join("")
    + `<button id="addroom" title="Add room">+</button>`;
  const pickRoom = (v) => { room = v === "all" || v === "none" ? v : +v; ls.set("sm-room", room); render(); fit(); };
  const addRoom = async () => { const n = prompt("New room name:"); if (n) await write("POST", "/api/rooms", { name: n }); };
  $$("#rooms [data-room]").forEach((b) => b.onclick = () => pickRoom(b.dataset.room));
  $("#addroom").onclick = addRoom;
  // phones: the same rooms as a dropdown
  const sel = $("#roomsel");
  sel.innerHTML = rooms.map((r) => `<option value="${r.id}" ${String(r.id) === String(room) ? "selected" : ""}>${esc(r.name)}</option>`).join("")
    + `<option value="+">+ Add room…</option>`;
  sel.onchange = async () => { if (sel.value === "+") { sel.value = String(room); await addRoom(); } else pickRoom(sel.value); };

  const drift = IX.conns.filter((c) => ["new", "missing", "error"].includes(c.status) && inRoom(pdev(c.to_port)) );
  const n = (s) => drift.filter((c) => c.status === s).length;
  const b = $("#drift");
  const nx = xpDrift().length;
  b.textContent = drift.length + nx ? "Drift: " + [[n("missing"), "missing"], [n("new"), "new"], [n("error"), "error"], [nx, "rerouted"]]
    .filter(([k]) => k).map(([k, w]) => `${k} ${w}`).join(" · ") : "No drift";
  b.classList.toggle("has", drift.length + nx > 0);
  b.onclick = () => openPanel({ type: "drift" });

  $("#sitename").textContent = S.site_name || "";
  document.title = S.site_name ? `Patchy · ${S.site_name}` : "Patchy";
  const ls_ = S.last_sync, ok = S.last_ok_sync;
  let txt = ok ? `Dante read ${ago(Date.now() / 1000 - ok.finished)}` : "Dante not read yet";
  if (S.sync_running) txt = "Reading Dante…";
  else if (!S.dante_enabled) txt = "Dante reading off";
  else if (ls_ && !ls_.ok && ls_.message !== "running") txt += " · last read failed";
  $("#syncinfo").textContent = txt;
  $("#syncinfo").title = ls_ && !ls_.ok ? ls_.message || "" : "";
  $("#syncnow").disabled = !!S.sync_running || (!S.dante_enabled && !Object.keys(S.videohubs || {}).length);
  $("#syncnow").textContent = narrow() ? (S.sync_running ? "Reading" : "Sync") : (S.sync_running ? "Reading…" : "Sync now");
  $("#syncnow").title = txt;
  $("#csv").href = "/api/export.csv" + (typeof room === "number" ? `?room=${room}` : "");
}
function inRoom(d) {
  if (room === "all") return true;
  if (room === "none") return d.room_id == null;
  return d.room_id === room;
}

const connInLayer = (c) => layer === "all" || FAMILY[IX.port.get(c.from_port).kind] === layer;
const portInLayer = (p) => layer === "all" || FAMILY[p.kind] === layer;
let LAYER_DEVS = null;   // device ids shown in the current layer (null = all)
function computeLayer() {
  if (layer === "all") { LAYER_DEVS = null; return; }
  const ids = new Set(S.devices.filter((d) => d.category === layer).map((d) => d.id));
  for (const c of IX.conns) if (connInLayer(c)) { ids.add(pdev(c.from_port).id); ids.add(pdev(c.to_port).id); }
  LAYER_DEVS = ids;
}
const inLayer = (d) => !LAYER_DEVS || LAYER_DEVS.has(d.id);
function renderLayers() {
  const html = LAYERS.map(([k, t]) => `<button data-layer="${k}" class="${k === layer ? "on" : ""}" aria-pressed="${k === layer}">${t}</button>`).join("");
  $$(".layers").forEach((el) => {
    el.innerHTML = html;
    $$("[data-layer]", el).forEach((b) => b.onclick = () => { layer = b.dataset.layer; ls.set("sm-layer", layer); render(); if (view === "map") fit(); });
  });
}

// ---------- map ----------
function layout() {
  // returns Map deviceId -> {x,y,ghost}
  const pos = new Map();
  const matches = (d) => !query || d.name.toLowerCase().includes(query) || (d.model || "").toLowerCase().includes(query)
    || (IX.portsByDev.get(d.id) || []).some((p) => (p.name + " " + p.label).toLowerCase().includes(query));
  if (room === "all") {
    let off = 0;
    const groups = [...S.rooms.map((r) => r.id), null];
    for (const rid of groups) {
      const ds = S.devices.filter((d) => d.room_id === rid && inLayer(d));
      if (!ds.length) continue;
      const minX = Math.min(...ds.map((d) => d.x ?? 0));
      let maxX = 0;
      for (const d of ds) { const x = (d.x ?? 0) - minX + off; pos.set(d.id, { x, y: d.y ?? 0, off: off - minX, dim: !matches(d) }); maxX = Math.max(maxX, x); }
      off = maxX + CARD_W + 260;
    }
    return pos;
  }
  const mine = S.devices.filter((d) => inRoom(d) && inLayer(d));
  for (const d of mine) pos.set(d.id, { x: d.x ?? 0, y: d.y ?? 0, off: 0, dim: !matches(d) });
  // neighbours from other rooms become ghosts at the edges
  const minX = Math.min(0, ...mine.map((d) => d.x ?? 0)), maxX = Math.max(0, ...mine.map((d) => d.x ?? 0));
  const ins = new Set(), outs = new Set();
  for (const c of IX.conns) {
    if (!connInLayer(c)) continue;
    const f = pdev(c.from_port), t = pdev(c.to_port);
    if (pos.has(t.id) && !pos.has(f.id) && !inRoom(f)) ins.add(f.id);
    if (pos.has(f.id) && !pos.has(t.id) && !inRoom(t)) outs.add(t.id);
  }
  [...ins].forEach((id, i) => pos.set(id, { x: minX - CARD_W - 160, y: 40 + i * 92, ghost: true }));
  [...outs].filter((id) => !ins.has(id)).forEach((id, i) => pos.set(id, { x: maxX + CARD_W + 160, y: 40 + i * 92, ghost: true }));
  return pos;
}

let POS = new Map();
function renderEmpty() {
  const none = !S.devices.length;
  $("#empty").hidden = !none;
  if (!none) return;
  const last = S.last_sync;
  $("#emptymsg").textContent = !S.dante_enabled
    ? "Dante reading is off. Add your gear by hand, or turn Dante on in Settings."
    : S.sync_running || !last
      ? "Looking for Dante devices on this network. The first read takes about a minute. You can add gear by hand at the same time."
      : last.ok
        ? "No Dante devices answered. Check that this computer is connected to the Dante network (Settings → Network adapter), or add your gear by hand."
        : "Reading Dante failed: " + (last.message || "unknown error");
}
function renderMap() {
  renderEmpty();
  POS = layout();
  const driftOnly = $("#driftonly").checked;
  // edges grouped by device pair
  const edges = new Map();
  for (const c of IX.conns) {
    if (!connInLayer(c)) continue;
    const f = pdev(c.from_port), t = pdev(c.to_port);
    if (!POS.has(f.id) || !POS.has(t.id)) continue;
    if (POS.get(f.id).ghost && POS.get(t.id).ghost) continue;
    if (driftOnly && !["new", "missing", "error"].includes(c.status)) continue;
    const key = `${f.id}>${t.id}`;
    if (!edges.has(key)) edges.set(key, { key, from: f.id, to: t.id, conns: [], status: "manual", kinds: new Set() });
    const e = edges.get(key);
    e.conns.push(c); e.kinds.add(IX.port.get(c.from_port).kind);
    if (STATUS_RANK[c.status] > STATUS_RANK[e.status]) e.status = c.status;
  }
  // cards
  const cards = [];
  for (const [id, p] of POS) {
    const d = IX.dev.get(id);
    const drift = (IX.portsByDev.get(id) || []).flatMap((pt) => IX.connsByPort.get(pt.id) || [])
      .filter((c) => c.to_port && pdev(c.to_port).id === id && ["new", "missing", "error"].includes(c.status));
    const nNew = drift.filter((c) => c.status === "new").length, nBad = drift.length - nNew;
    const nRe = (IX.portsByDev.get(id) || []).filter((pt) => xpStatus(pt) === "rerouted").length;
    const sel = selected && selected.type === "device" && selected.id === id;
    cards.push(`<div class="card ${p.ghost ? "ghost" : ""} ${p.dim ? "dim" : ""} ${sel ? "sel" : ""} ${id === liftedId ? "lifted" : ""}" data-id="${id}" style="left:${p.x}px;top:${p.y}px">
      <div class="tag"><b class="cat-${d.category}">${esc(d.category)}</b>${d.source === "dante" ? "Dante" : d.source === "videohub" ? "Videohub" : ""}${p.ghost ? " · " + esc(roomName(d.room_id)) : ""}</div>
      <div class="nm" title="${esc(d.name)}">${esc(d.name)}</div>
      <div class="md">${esc([d.make, d.model].filter(Boolean).join(" ") || " ")}${d.ip ? " · " + esc(d.ip) : ""}${d.control_ip ? " · ctl " + esc(d.control_ip) : ""}</div>
      <div class="badges">${nBad ? `<span class="pill missing">${nBad}</span>` : ""}${nNew ? `<span class="pill new">${nNew}</span>` : ""}${nRe ? `<span class="pill rerouted" title="Locked outputs re-routed">${nRe}</span>` : ""}
        ${seenAgo(d) !== null ? `<span class="dot ${isOffline(d) ? "off" : ""}" title="Seen ${ago(seenAgo(d))}"></span>` : ""}</div>
    </div>`);
  }
  $("#cards").innerHTML = cards.join("");
  // wires
  const paths = [];
  for (const e of edges.values()) {
    const a = POS.get(e.from), b = POS.get(e.to);
    const x1 = a.x + CARD_W, y1 = a.y + CARD_H / 2, x2 = b.x, y2 = b.y + CARD_H / 2;
    const dx = Math.max(60, Math.abs(x2 - x1) / 2);
    const d = `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
    const kind = e.kinds.size === 1 ? [...e.kinds][0] : "mixed";
    const color = e.status === "ok" || e.status === "manual" ? (KIND_COLOR[kind] || "var(--manual)") : STATUS_COLOR[e.status];
    const dash = e.status === "missing" ? ' stroke-dasharray="6 5"' : e.status === "new" ? ' stroke-dasharray="2 4"' : "";
    const sel = selected && selected.type === "edge" && selected.id === e.key;
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
    const label = e.conns.length > 1 ? `${e.conns.length} ch` : pname(e.conns[0].from_port).slice(0, 18);
    const w = label.length * 6.2 + 14;
    paths.push(`<path d="${d}" stroke="${color}"${dash} class="${sel ? "sel" : ""}" opacity="${e.status === "ok" || e.status === "manual" ? .75 : 1}"/>
      <path d="${d}" class="hit" data-edge="${e.key}"/>
      <g class="wl" data-edge="${e.key}" transform="translate(${mx - w / 2},${my - 9})"><rect width="${w}" height="18" fill="${color}" opacity=".9"/><text x="7" y="13">${esc(label)}</text></g>`);
  }
  $("#wires").innerHTML = paths.join("");
  $$("#wires [data-edge]").forEach((el) => el.addEventListener("click", (ev) => { ev.stopPropagation(); openPanel({ type: "edge", id: el.dataset.edge }); }));
  applyCam();
}
function applyCam() {
  const t = `translate(${cam.x}px,${cam.y}px) scale(${cam.k})`;
  $("#cards").style.transform = t; $("#wires").style.transform = t;
  ls.set(camKey(), cam);
}
function fit() {
  if (view !== "map") return;
  const pts = [...layout().values()];
  if (!pts.length) return;
  const minX = Math.min(...pts.map((p) => p.x)), maxX = Math.max(...pts.map((p) => p.x)) + CARD_W;
  const minY = Math.min(...pts.map((p) => p.y)), maxY = Math.max(...pts.map((p) => p.y)) + CARD_H;
  const el = $("#canvas"), pw = selected && !narrow() ? 460 : 0;
  const top = narrow() ? 52 : 20, bottom = $(".maptools").offsetHeight + 30;
  const W = el.clientWidth - pw - 40, H = el.clientHeight - top - bottom;
  // Phones: never shrink cards below readable size; start at the top-left and pan from there.
  const minK = narrow() ? 0.5 : window.matchMedia("(pointer: coarse)").matches ? 0.35 : 0.1;
  cam.k = Math.min(1.2, Math.max(minK, Math.min(W / (maxX - minX), H / (maxY - minY))));
  cam.x = 20 + Math.max(0, (W - (maxX - minX) * cam.k) / 2) - minX * cam.k; cam.y = top - minY * cam.k;
  applyCam();
}
function zoomAt(k, mx, my) {
  k = Math.min(2.5, Math.max(0.1, k));
  cam.x = mx - (mx - cam.x) * (k / cam.k); cam.y = my - (my - cam.y) * (k / cam.k); cam.k = k;
  applyCam();
}
function zoomBy(f) { const el = $("#canvas"); zoomAt(cam.k * f, el.clientWidth / 2, el.clientHeight / 2); }

// pan / pinch-zoom / move cards. Mouse: drag a card to move it. Touch: one finger pans (even on a card),
// press and hold a card to pick it up, two fingers pinch-zoom.
(function interactions() {
  const canvas = $("#canvas");
  const pts = new Map();      // active pointers
  let drag = null, pinch = null, holdTimer = null;
  const local = (ev) => { const r = canvas.getBoundingClientRect(); return { x: ev.clientX - r.left, y: ev.clientY - r.top }; };
  const hideHint = () => $("#hint").classList.add("gone");
  function lift(d) {
    d.type = "card"; liftedId = d.id; d.cardEl.classList.add("lifted");
    if (navigator.vibrate) navigator.vibrate(15);
  }
  canvas.addEventListener("pointerdown", (ev) => {
    pts.set(ev.pointerId, local(ev));
    try { canvas.setPointerCapture(ev.pointerId); } catch { /* pointer already gone */ }
    if (pts.size === 2) {           // second finger: switch to pinch, drop whatever one finger was doing
      clearTimeout(holdTimer);
      if (drag && drag.type === "card") { liftedId = null; load(); }
      const [a, b] = [...pts.values()];
      pinch = { d: Math.hypot(a.x - b.x, a.y - b.y) || 1, mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2, cam: { ...cam } };
      drag = null; hideHint();
      return;
    }
    if (pts.size > 2) return;
    const card = ev.target.closest(".card");
    const touch = ev.pointerType !== "mouse";
    if (card && !card.classList.contains("ghost")) {
      const id = +card.dataset.id, p = POS.get(id);
      drag = { type: touch ? "maybe" : "card", id, cardEl: card, sx: ev.clientX, sy: ev.clientY, ox: p.x, oy: p.y,
        cx: cam.x, cy: cam.y, moved: false };
      if (touch) holdTimer = setTimeout(() => { if (drag && drag.type === "maybe" && !drag.moved) lift(drag); }, 380);
    } else if (card) {
      drag = { type: "ghost", id: +card.dataset.id, sx: ev.clientX, sy: ev.clientY, cx: cam.x, cy: cam.y, moved: false };
    } else if (!ev.target.closest(".wl, path.hit")) {
      drag = { type: "pan", sx: ev.clientX, sy: ev.clientY, cx: cam.x, cy: cam.y, moved: false };
      canvas.classList.add("panning");
    }
  });
  canvas.addEventListener("pointermove", (ev) => {
    if (!pts.has(ev.pointerId)) return;
    pts.set(ev.pointerId, local(ev));
    if (pinch && pts.size >= 2) {
      const [a, b] = [...pts.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y) || 1, mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      const k = Math.min(2.5, Math.max(0.1, pinch.cam.k * d / pinch.d));
      // keep the point that was under the fingers under the fingers, and follow them as they move
      cam.x = mx - (pinch.mx - pinch.cam.x) * (k / pinch.cam.k);
      cam.y = my - (pinch.my - pinch.cam.y) * (k / pinch.cam.k); cam.k = k;
      applyCam();
      return;
    }
    if (!drag) return;
    const dx = ev.clientX - drag.sx, dy = ev.clientY - drag.sy;
    if (Math.abs(dx) + Math.abs(dy) > (ev.pointerType === "mouse" ? 4 : 9)) {
      if (!drag.moved) hideHint();
      drag.moved = true;
      if (drag.type === "maybe" || drag.type === "ghost") { clearTimeout(holdTimer); drag.type = "pan"; }
    }
    if (drag.type === "pan" && drag.moved) { cam.x = drag.cx + dx; cam.y = drag.cy + dy; applyCam(); }
    if (drag.type === "card" && drag.moved) {
      liftedId = drag.id;
      const p = POS.get(drag.id);
      p.x = Math.round((drag.ox + dx / cam.k) / 10) * 10; p.y = Math.round((drag.oy + dy / cam.k) / 10) * 10;
      redrawWiresOnly();
    }
  });
  async function end(ev) {
    pts.delete(ev.pointerId);
    clearTimeout(holdTimer);
    if (pinch) { if (pts.size < 2) pinch = null; return; }   // no tap after a pinch
    const d = drag; drag = null; canvas.classList.remove("panning");
    const wasLifted = liftedId; liftedId = null;
    if (!d) return;
    if (ev.type === "pointercancel") { if (wasLifted) renderMap(); return; }
    if (!d.moved && (d.type === "card" || d.type === "maybe" || d.type === "ghost")) {
      if (wasLifted) { renderMap(); return; }           // picked up but not moved: just put it down
      return openPanel({ type: "device", id: d.id });
    }
    if (!d.moved && d.type === "pan" && selected && !narrow()) return closePanel();
    if (d.type === "card" && d.moved) {
      const p = POS.get(d.id), dev = IX.dev.get(d.id);
      dev.x = p.x - (p.off || 0); dev.y = p.y;
      renderMap();
      try { await api("PATCH", `/api/devices/${d.id}`, { x: dev.x, y: dev.y }); } catch { load(); }
    }
  }
  canvas.addEventListener("pointerup", end);
  canvas.addEventListener("pointercancel", end);
  canvas.addEventListener("contextmenu", (ev) => { if (ev.target.closest(".card")) ev.preventDefault(); });
  canvas.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const p = local(ev);
    // trackpad pinch arrives as ctrl+wheel; plain two-finger scroll pans
    if (ev.ctrlKey || ev.deltaMode !== 0 || Math.abs(ev.deltaX) < 1 && Number.isInteger(ev.deltaY) && Math.abs(ev.deltaY) >= 50) {
      zoomAt(cam.k * Math.exp(-ev.deltaY * (ev.ctrlKey ? 0.01 : 0.0015)), p.x, p.y);
    } else { cam.x -= ev.deltaX; cam.y -= ev.deltaY; applyCam(); }
  }, { passive: false });
  // Safari's own pinch gesture events would zoom the whole page
  ["gesturestart", "gesturechange"].forEach((t) => canvas.addEventListener(t, (e) => e.preventDefault()));
})();
let wireTimer = null;
function redrawWiresOnly() {
  if (wireTimer) return;
  wireTimer = requestAnimationFrame(() => {
    wireTimer = null;
    const saved = POS; renderMapKeepPos(saved);
  });
}
function renderMapKeepPos(saved) {
  const orig = layout; layout = () => saved; // reuse dragged positions for this redraw
  try { renderMap(); } finally { layout = orig; }
}

// ---------- side panel ----------
let panelDirty = false;   // unsaved edits in the device form: background refreshes must not redraw it
function openPanel(sel) {
  if (panelDirty && !confirm("Discard unsaved changes to this device?")) return;
  panelDirty = false; selected = sel; renderPanel(); if (view === "map") renderMap();
}
function closePanel() {
  if (panelDirty && !confirm("Discard unsaved changes to this device?")) return;
  panelDirty = false; selected = null; $("#panel").classList.remove("open"); if (view === "map") renderMap();
}

function connRow(c, side, pid) {
  const other = side === "out" ? c.to_port : c.from_port;
  const od = pdev(other);
  const acts = [];
  if (c.status === "new") acts.push(`<button class="mini" data-accept="${c.id}" title="Accept as documented">✓</button>`);
  acts.push(`<button class="mini" data-unpatch="${c.id}" title="${c.live ? "Mark not documented" : "Remove"}">✕</button>`);
  return `<div>${side === "out" ? "→" : "←"} <span class="lnk" data-dev="${od.id}">${esc(od.name)}</span> · ${esc(pname(other))}
    <span class="pill ${c.status}" title="${esc(c.live_label || STATUS_TEXT[c.status])}">${STATUS_TEXT[c.status]}${c.transport === "multicast" ? " · mcast" : ""}</span>
    ${c.cable ? `<span class="sub">cable ${esc(c.cable)}</span>` : ""} ${acts.join("")}</div>`;
}
function portTable(d, direction, showAll) {
  const ports = (IX.portsByDev.get(d.id) || []).filter((p) => p.direction === direction);
  const shown = ports.filter((p) => showAll || worthShowing(p));
  if (!ports.length) return `<div class="sub">None</div>`;
  const rows = shown.map((p) => {
    const cs = (IX.connsByPort.get(p.id) || []).filter((c) => (direction === "out" ? c.from_port : c.to_port) === p.id);
    return `<tr><td><span class="kind ${p.kind}">${p.kind}</span></td>
      <td>${esc(p.name)}${p.label ? `<div class="sub">${esc(p.label)}</div>` : ""}</td>
      <td>${xpCell(p) + cs.map((c) => connRow(c, direction, p.id)).join("") || '<span class="sub">—</span>'}</td>
      <td style="white-space:nowrap"><button class="mini" data-patch="${p.id}" title="Patch">+</button>
        <button class="mini" data-editport="${p.id}" title="Label / rename">✎</button></td></tr>`;
  }).join("");
  const hidden = ports.length - shown.length;
  return `<table class="ports pt"><tr><th>Type</th><th>Port</th><th>Connected</th><th></th></tr>${rows}</table>
    ${hidden ? `<div class="sub">${hidden} unused port(s) hidden · <span class="lnk" data-showall>show</span></div>` : ""}`;
}
let showAllPorts = false;
function renderPanel() {
  const P = $("#panel");
  if (!selected) { P.classList.remove("open"); return; }
  let html = `<button class="close" id="pclose">✕</button>`;
  if (selected.type === "device") {
    const d = IX.dev.get(selected.id);
    if (!d) return closePanel();
    html += `<h2>${esc(d.name)}</h2><div class="sub">${esc(roomName(d.room_id))}${seenAgo(d) !== null ? ` · ${d.source === "dante" ? "Dante" : "Videohub"}, seen ${ago(seenAgo(d))}` : ""}${d.dante_name && d.dante_name !== d.name ? ` · Dante name ${esc(d.dante_name)}` : ""}</div>
      <details class="devdetails" ${narrow() && !panelDirty ? "" : "open"}><summary><h3>Details</h3></summary>
      <form id="devform" class="grid">
        <label>Name</label><input name="name" value="${esc(d.name)}">
        <label>Room</label><select name="room_id"><option value="">Unassigned</option>${S.rooms.map((r) => `<option value="${r.id}" ${r.id === d.room_id ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select>
        <label>Category</label><select name="category">${CATS.map((c) => `<option ${c === d.category ? "selected" : ""}>${c}</option>`).join("")}</select>
        <label>Make</label><input name="make" value="${esc(d.make)}">
        <label>Model</label><input name="model" value="${esc(d.model)}">
        <label>${d.source === "dante" ? "Dante IP" : "IP"}</label><input name="ip" value="${esc(d.ip)}">
        <label>Control IP</label><input name="control_ip" value="${esc(d.control_ip || "")}" placeholder="Control / editor network port">        <label>Location</label><input name="location" value="${esc(d.location)}" placeholder="Rack, switch port…">
        <label>Notes</label><textarea name="notes" rows="3">${esc(d.notes)}</textarea>
      </form>
      <div class="row"><button class="primary" id="devsave">Save details</button>
        <button class="danger" id="devdel">${d.source !== "manual" ? "Hide device" : "Delete device"}</button></div></details>
      <h3>Inputs</h3>${portTable(d, "in", showAllPorts)}
      <h3>Outputs</h3>${portTable(d, "out", showAllPorts)}
      <div class="row"><button id="addports">+ Add ports</button></div>`;
  } else if (selected.type === "edge") {
    const [f, t] = selected.id.split(">").map(Number);
    const cs = IX.conns.filter((c) => pdev(c.from_port).id === f && pdev(c.to_port).id === t)
      .sort((a, b) => (IX.port.get(a.to_port).number ?? 0) - (IX.port.get(b.to_port).number ?? 0));
    html += `<h2>${esc(IX.dev.get(f).name)} → ${esc(IX.dev.get(t).name)}</h2><div class="sub">${cs.length} route(s)</div>
      ${cs.some((c) => c.status === "new") ? `<div class="row"><button data-acceptall="${cs.filter((c) => c.status === "new").map((c) => c.id).join(",")}">Accept all new as documented</button></div>` : ""}
      ${routeTable(cs)}`;
  } else if (selected.type === "drift") {
    const cs = IX.conns.filter((c) => ["new", "missing", "error"].includes(c.status) && inRoom(pdev(c.to_port)));
    const xs = xpDrift();
    html += `<h2>Drift</h2><div class="sub">Where the live routing differs from the documented map${room === "all" ? "" : " (this room)"}.</div>
      ${xs.length ? `<h3>Videohub routes</h3><div class="sub">Locked outputs now fed from a different input than documented.</div>
        <div class="row"><button data-xpacceptall="${xs.map((p) => p.id).join(",")}">Accept all as documented</button></div>${xpTable(xs)}` : ""}
      ${cs.length ? `<h3>Dante routes</h3><div class="sub">Missing = documented route the receiver no longer has. New = live route nobody has documented yet.</div>
        ${cs.some((c) => c.status === "new") ? `<div class="row"><button data-acceptall="${cs.filter((c) => c.status === "new").map((c) => c.id).join(",")}">Accept all new as documented</button></div>` : ""}
        ${routeTable(cs, true)}` : ""}
      ${cs.length || xs.length ? "" : '<p class="sub">Nothing to review.</p>'}`;
  }
  P.innerHTML = html;
  P.classList.add("open");
  bindPanel();
}
function xpTable(ps) {
  return `<table class="ports xt"><tr><th>Output</th><th>Now</th><th>Was</th><th></th></tr>${ps.map((p) => `<tr>
    <td><span class="lnk" data-dev="${p.device_id}">${esc(IX.dev.get(p.device_id).name)}</span><br>${esc(pname(p.id))}</td>
    <td>⇐ ${esc(pname(p.xp_live))}</td><td class="sub">${p.xp_doc ? "was " + esc(pname(p.xp_doc)) : "—"}</td>
    <td><button class="mini" data-xpaccept="${p.id}" title="Accept as documented">✓</button></td></tr>`).join("")}</table>`;
}
function routeTable(cs, showDevices = true) {
  return `<table class="ports rt"><tr><th>From</th><th>To</th><th>Status</th><th></th></tr>${cs.map((c) => `<tr>
    <td>${showDevices ? `<span class="lnk" data-dev="${pdev(c.from_port).id}">${esc(pdev(c.from_port).name)}</span><br>` : ""}${esc(pname(c.from_port))}</td>
    <td>${showDevices ? `<span class="lnk" data-dev="${pdev(c.to_port).id}">${esc(pdev(c.to_port).name)}</span><br>` : ""}${esc(pname(c.to_port))}</td>
    <td><span class="pill ${c.status}" title="${esc(c.live_label)}">${STATUS_TEXT[c.status]}</span>${c.notes ? `<div class="notes">${esc(c.notes)}</div>` : ""}</td>
    <td style="white-space:nowrap">${c.status === "new" ? `<button class="mini" data-accept="${c.id}" title="Accept as documented">✓</button>` : ""}
      <button class="mini" data-unpatch="${c.id}" title="${c.live ? "Mark not documented" : "Remove"}">✕</button></td></tr>`).join("")}</table>`;
}
function bindPanel() {
  const P = $("#panel");
  $("#pclose", P).onclick = closePanel;
  $$("[data-dev]", P).forEach((el) => el.onclick = () => openPanel({ type: "device", id: +el.dataset.dev }));
  $$("[data-accept]", P).forEach((el) => el.onclick = () => write("POST", "/api/connections", { ids: [+el.dataset.accept] }));
  $$("[data-acceptall]", P).forEach((el) => el.onclick = () => write("POST", "/api/connections", { ids: el.dataset.acceptall.split(",").map(Number) }));
  $$("[data-xpaccept]", P).forEach((el) => el.onclick = () => write("POST", "/api/xp", { port_ids: [+el.dataset.xpaccept] }));
  $$("[data-xpacceptall]", P).forEach((el) => el.onclick = () => write("POST", "/api/xp", { port_ids: el.dataset.xpacceptall.split(",").map(Number) }));
  $$("[data-unpatch]", P).forEach((el) => el.onclick = () => {
    const c = IX.conns.find((x) => x.id === +el.dataset.unpatch);
    const msg = c.live ? "This route is live on the network. Mark it as not documented? (Nothing changes on the gear.)" : "Remove this route from the map?";
    if (confirm(msg)) write("DELETE", `/api/connections/${c.id}`);
  });
  $$("[data-patch]", P).forEach((el) => el.onclick = () => patchDialog(+el.dataset.patch));
  $$("[data-editport]", P).forEach((el) => el.onclick = () => portDialog(+el.dataset.editport));
  const sa = $("[data-showall]", P); if (sa) sa.onclick = () => { showAllPorts = true; renderPanel(); };
  const save = $("#devsave", P);
  const form = $("#devform", P);
  if (form) form.addEventListener("input", () => { panelDirty = true; save.textContent = "Save details •"; });
  if (save) save.onclick = async () => {
    const f = Object.fromEntries(new FormData($("#devform")));
    f.room_id = f.room_id ? +f.room_id : null;
    try {
      await api("PATCH", `/api/devices/${selected.id}`, f);
      panelDirty = false;
      await load(); toast("Saved");
    } catch (e) { toast("Not saved: " + (e.message || e)); }
  };
  const del = $("#devdel", P);
  if (del) del.onclick = async () => {
    const d = IX.dev.get(selected.id);
    if (!confirm(d.source === "dante" ? `Hide ${d.name}? It stays hidden even when Dante sees it.` : `Delete ${d.name} and its ports and cables?`)) return;
    await write("DELETE", `/api/devices/${d.id}`); closePanel();
  };
  const ap = $("#addports", P); if (ap) ap.onclick = () => addPortsDialog(selected.id);
}

// ---------- dialogs ----------
function dialog(html, onSubmit) {
  const dlg = $("#dlg"), form = $("#dlgform");
  form.innerHTML = html + `<div class="err" id="dlgerr"></div><div class="row"><button value="cancel" formnovalidate>Cancel</button><button class="primary" value="ok">Save</button></div>`;
  form.onsubmit = async (ev) => {
    if (ev.submitter && ev.submitter.value === "cancel") return;
    ev.preventDefault();
    try { await onSubmit(Object.fromEntries(new FormData(form))); dlg.close(); }
    catch (e) { $("#dlgerr").textContent = e.message || String(e); }
  };
  dlg.showModal();
}
function patchDialog(pid) {
  const p = IX.port.get(pid), d = pdev(pid);
  const want = p.direction === "out" ? "in" : "out";
  const ok = (q) => q.direction === want && canPatch(p.kind, q.kind);
  const devs = S.devices.filter((x) => x.id !== d.id || p.kind !== "dante").filter((x) => (IX.portsByDev.get(x.id) || []).some(ok))
    .sort((a, b) => roomName(a.room_id).localeCompare(roomName(b.room_id)) || a.name.localeCompare(b.name));
  dialog(`<h3>Patch ${esc(d.name)} · ${esc(p.name)} ${p.direction === "out" ? "→" : "←"}</h3>
    ${p.kind === "dante" ? '<p class="sub">This documents the route only. Make the actual subscription in Dante Controller.</p>' : ""}
    <div class="grid"><label>Device</label><select id="pdev" required><option value="">Choose…</option>${devs.map((x) => `<option value="${x.id}">${esc(roomName(x.room_id))} · ${esc(x.name)}</option>`).join("")}</select>
    <label>Port</label><select name="port" id="pport" required></select>
    <label>Cable</label><input name="cable" placeholder="Cable label">
    <label>Notes</label><input name="notes"></div>`, async (f) => {
    const other = +f.port;
    if (!other) throw new Error("Choose a port");
    await write("POST", "/api/connections", { from_port: p.direction === "out" ? pid : other, to_port: p.direction === "out" ? other : pid, cable: f.cable, notes: f.notes });
  });
  $("#pdev").onchange = () => {
    const ports = (IX.portsByDev.get(+$("#pdev").value) || []).filter(ok);
    $("#pport").innerHTML = ports.map((q) => `<option value="${q.id}">${esc(q.kind)} · ${esc(pname(q.id))}${(IX.connsByPort.get(q.id) || []).length ? " (in use)" : ""}</option>`).join("");
  };
}
function portDialog(pid) {
  const p = IX.port.get(pid);
  dialog(`<h3>Port ${esc(p.name)}</h3><div class="grid">
    ${p.kind === "dante" ? `<label>Name</label><div class="sub">${esc(p.name)} (rename in Dante Controller)</div>` : `<label>Name</label><input name="name" value="${esc(p.name)}" required>`}
    <label>Label</label><input name="label" value="${esc(p.label)}" placeholder="What's on it, e.g. Pastor lav"></div>
    ${p.source !== "dante" ? `<div class="row"><button type="button" class="danger" id="delport">Delete port</button></div>` : ""}`, async (f) => {
    await write("PATCH", `/api/ports/${pid}`, f);
  });
  const dp = $("#delport");
  if (dp) dp.onclick = async () => { if (confirm("Delete this port and its cables?")) { await write("DELETE", `/api/ports/${pid}`); $("#dlg").close(); } };
}
function addPortsDialog(did) {
  dialog(`<h3>Add ports to ${esc(IX.dev.get(did).name)}</h3><div class="grid">
    <label>Direction</label><select name="direction"><option value="in">Input</option><option value="out">Output</option></select>
    <label>Type</label><select name="kind">${KINDS.map((k) => `<option>${k}</option>`).join("")}</select>
    <label>Name</label><input name="name" required placeholder="e.g. Input, AES Out, SDI In">
    <label>How many</label><input name="count" type="number" min="1" max="128" value="1">
    <label>Start at</label><input name="start" type="number" min="0" value="1"></div>
    <p class="sub">With more than one, ports are numbered: "Input 1", "Input 2"…</p>`, async (f) => {
    await write("POST", "/api/ports", { device_id: did, ...f, count: +f.count, start: +f.start });
  });
}
function addDeviceDialog() {
  const rid = typeof room === "number" ? room : (S.rooms[0] || {}).id;
  dialog(`<h3>Add device</h3><div class="grid">
    <label>Name</label><input name="name" required>
    <label>Room</label><select name="room_id"><option value="">No room</option>${S.rooms.map((r) => `<option value="${r.id}" ${r.id === rid ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select>
    <label>Category</label><select name="category">${CATS.map((c) => `<option>${c}</option>`).join("")}</select>
    <label>Make</label><input name="make"><label>Model</label><input name="model">
    <label>IP</label><input name="ip"><label>Location</label><input name="location"></div>
    <p class="sub">Dante devices appear on their own after a sync. Use this for analog/AES gear, speakers, amps, video and lighting.</p>`, async (f) => {
    const el = $("#canvas");
    f.room_id = f.room_id ? +f.room_id : null;
    f.x = Math.round((-cam.x + el.clientWidth / 2) / cam.k); f.y = Math.round((-cam.y + el.clientHeight / 2) / cam.k);
    const r = await write("POST", "/api/devices", f);
    openPanel({ type: "device", id: r.id });
  });
}

// ---------- settings ----------
async function settingsDialog() {
  const s = await (await fetch("/api/settings")).json();
  const ro = !s.local;
  const rules = (s.room_rules || []).map(([p, r]) => `${p} => ${r}`).join("\n");
  const hubs = (s.videohubs || []).map((h) => [h.host, h.name, h.room].filter(Boolean).join(", ")).join("\n");
  const lanUrl = s.lan_ip ? `http://${s.lan_ip}:${s.running_port}/` : "";
  const chk = (name, on, text) => `<label class="chk"><input type="checkbox" name="${name}" ${on ? "checked" : ""}> ${text}</label>`;
  dialog(`<h3>Settings</h3>
    ${ro ? '<p class="sub">Settings can only be changed on the computer running Patchy.</p>' : ""}
    <fieldset ${ro ? "disabled" : ""}>
    <div class="grid"><label>Site name</label><input name="site_name" value="${esc(s.site_name)}" placeholder="Shown next to the logo"></div>
    <h4>Dante</h4>
    <div class="grid">
      <label>Read Dante</label>${chk("dante_enabled", s.dante_enabled, "Find Dante devices and routes (read-only)")}
      <label>Every</label><span><input name="sync_minutes" type="number" min="1" max="1440" value="${s.sync_minutes}" class="num"> minutes</span>
      <label>Network adapter</label><input name="netaudio_interface" value="${esc(s.netaudio_interface || "")}" placeholder="Automatic">
      <label>Rooms</label><textarea name="room_rules" rows="4" placeholder="FOH => Main Hall&#10;^Stage => Stage">${esc(rules)}</textarea>
    </div>
    <p class="sub">Rooms: one rule per line, <code>text in the Dante name => room</code>, used when a device is first found
      (regular expressions work). You can move any device to another room in its details.</p>
    <h4>Blackmagic Videohubs</h4>
    <textarea name="videohubs" rows="3" placeholder="192.168.1.50, Main Videohub, Control Room">${esc(hubs)}</textarea>
    <p class="sub">One per line: <code>IP address, name, room</code> (name and room are optional). Patchy only listens to the hub and never changes a route.</p>
    <h4>Access</h4>
    <div class="grid">
      <label>Network</label>${chk("allow_lan", s.allow_lan, "Allow phones, tablets and other computers")}
      <label>Port</label><input name="port" type="number" min="1024" max="65535" value="${s.port}" class="num">
      ${s.local ? `<label>At login</label>${chk("autostart", s.autostart, "Start Patchy when I log in")}
      <label>Browser</label>${chk("open_browser", s.open_browser, "Open the map when Patchy starts")}` : ""}
    </div>
    ${lanUrl ? `<p class="sub">On other devices, open <b>${esc(lanUrl)}</b></p>` : ""}
    ${s.local ? `<p class="sub">With network access on, anyone on the network can view the map. Editing from another device asks once for this token:
      <code class="sel">${esc(s.token)}</code></p>
      <p class="sub">Data folder: <code class="sel">${esc(s.data_dir)}</code></p>` : ""}
    </fieldset>
    <p class="sub">Patchy ${esc(s.version)}${s.local ? ' · <button type="button" class="mini danger" id="quitapp">Quit Patchy</button>' : ""}</p>`, async (f) => {
    if (ro) return;
    const bad = f.room_rules.split("\n").find((l) => l.trim() && !l.includes("=>"));
    if (bad) throw new Error(`Room rule needs "=>": ${bad}`);
    const r = await api("PATCH", "/api/settings", {
      site_name: f.site_name, dante_enabled: !!f.dante_enabled, sync_minutes: +f.sync_minutes,
      netaudio_interface: f.netaudio_interface, allow_lan: !!f.allow_lan, port: +f.port,
      autostart: !!f.autostart, open_browser: !!f.open_browser,
      room_rules: f.room_rules.split("\n").filter((l) => l.includes("=>"))
        .map((l) => { const i = l.indexOf("=>"); return [l.slice(0, i).trim(), l.slice(i + 2).trim()]; }),
      videohubs: f.videohubs.split("\n").map((l) => l.split(",").map((x) => x.trim())).filter((p) => p[0])
        .map(([host, name = "", rm = ""]) => ({ host, name, room: rm })),
    });
    await load();
    if (r.restart_needed) toast("Quit and start Patchy again to apply the network or port change");
  });
  const q = $("#quitapp");
  if (q) q.onclick = async () => {
    if (!confirm("Stop Patchy on this computer?")) return;
    await api("POST", "/api/quit");
    $("#dlg").close();
    document.body.innerHTML = '<div class="stopped"><h2>Patchy has stopped</h2><p>Start it again from your apps to keep mapping.</p></div>';
  };
}

// ---------- patch table ----------
function renderTable() {
  const allDante = $("#alldante").checked;
  const rooms = room === "all" ? [...S.rooms.map((r) => r.id), null] : [room === "none" ? null : room];
  let html = "";
  for (const rid of rooms) {
    const ds = S.devices.filter((d) => d.room_id === rid && inLayer(d))
      .filter((d) => !query || d.name.toLowerCase().includes(query) || (IX.portsByDev.get(d.id) || []).some((p) => (p.name + " " + p.label).toLowerCase().includes(query)));
    if (!ds.length) continue;
    html += `<div class="proom">${esc(rid == null ? "Unassigned" : roomName(rid))}</div>`;
    for (const d of ds) {
      const ports = (IX.portsByDev.get(d.id) || []).filter((p) => (allDante || worthShowing(p)) && portInLayer(p));
      const rows = [];
      for (const p of ports.sort((a, b) => (a.direction === b.direction ? 0 : a.direction === "in" ? -1 : 1))) {
        const cs = (IX.connsByPort.get(p.id) || []).filter((c) => (p.direction === "out" ? c.from_port : c.to_port) === p.id);
        const xp = xpStatus(p);
        if (xp) {
          const src = IX.port.get(p.xp_live);
          rows.push(`<tr><td class="a">Out</td><td class="k"><span class="kind ${p.kind}">${p.kind}</span></td><td class="pp">${esc(p.name)}</td>
            <td class="lb">${esc(p.label)}</td><td class="cn">⇐ ${esc(src ? pname(src.id) : "?")}${p.xp_locked ? ' <span class="sub">locked</span>' : ""}</td>
            <td class="st"><span class="pill ${xp}">${STATUS_TEXT[xp]}</span></td><td class="cb"></td></tr>`);
        }
        const cells = cs.length ? cs : xp ? [] : [null];
        cells.forEach((c, j) => {
          const i = j + (xp ? 1 : 0);
          const other = c && (p.direction === "out" ? c.to_port : c.from_port);
          // classes let phones stack each row: "In DANTE 01 Kick (label)" / "← device · port" / status, cable
          rows.push(`<tr class="${i ? "cont" : ""}"><td class="a">${i ? "" : p.direction === "in" ? "In" : "Out"}</td><td class="k">${i ? "" : `<span class="kind ${p.kind}">${p.kind}</span>`}</td>
            <td class="pp">${i ? "" : esc(p.name)}</td><td class="lb">${i ? "" : esc(p.label)}</td>
            <td class="cn">${c ? `${p.direction === "out" ? "→" : "←"} ${esc(pdev(other).name)} · ${esc(pname(other))}` : '<span class="sub">—</span>'}</td>
            <td class="st">${c ? `<span class="pill ${c.status}">${STATUS_TEXT[c.status]}</span>` : ""}</td><td class="cb" ${c && c.cable ? 'data-l="Cable"' : ""}>${c ? esc(c.cable) : ""}</td></tr>`);
        });
      }
      html += `<div class="pdev"><h4>${esc(d.name)}<span>${esc([d.make, d.model, d.ip, d.location].filter(Boolean).join(" · "))}</span></h4>
        ${rows.length ? `<table class="ports"><tr><th></th><th>Type</th><th>Port</th><th>Label</th><th>Connected</th><th>Status</th><th>Cable</th></tr>${rows.join("")}</table>` : '<div class="sub">No ports patched</div>'}</div>`;
    }
  }
  $("#patch").innerHTML = html || '<p class="sub">No devices.</p>';
}

// ---------- activity ----------
async function renderActivity() {
  const [ch, sy] = await Promise.all([fetch("/api/changes").then((r) => r.json()), fetch("/api/syncs").then((r) => r.json())]);
  const t = (s) => new Date(s * 1000).toLocaleString();
  $("#act").innerHTML = `<h3>Changes</h3><table class="act">${ch.map((c) => `<tr><td>${t(c.at)}</td><td>${esc(c.who)}</td><td>${esc(c.what)}</td></tr>`).join("")}</table>
    <h3>Dante reads</h3><table class="act">${sy.map((s) => `<tr><td>${t(s.started)}</td><td>${s.ok ? "OK" : s.message === "running" ? "Running" : "Failed"}</td>
      <td>${s.ok ? `${s.devices} devices, ${s.subscriptions} routes, ${Math.round(s.finished - s.started)} s ${esc(s.message)}` : esc(s.message)}</td></tr>`).join("")}</table>`;
}

// ---------- top-level ----------
function render() {
  renderHeader();
  computeLayer();
  renderLayers();
  $$(".views button").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("on", v.id === view));
  if (view === "map") renderMap();
  if (view === "table") renderTable();
  if (view === "activity") renderActivity();
  if (selected && !panelDirty) renderPanel();
}
$$(".views button").forEach((b) => b.onclick = () => { view = b.dataset.view; ls.set("sm-view", view); render(); if (view === "map") fit(); });
$("#fit").onclick = fit;
$("#zoomin").onclick = () => zoomBy(1.3);
$("#zoomout").onclick = () => zoomBy(1 / 1.3);
$("#arrange").onclick = arrange;

// Layered layout: senders left, receivers right, ordered to reduce crossings.
async function arrange() {
  const rids = typeof room === "number" ? [room] : room === "none" ? [null] : [...S.rooms.map((r) => r.id), null];
  const what = rids.length > 1 ? "every room" : roomName(rids[0]);
  if (!confirm(`Re-arrange the devices in ${what}?`)) return;
  for (const rid of rids) await arrangeRoom(rid);
  await load(); fit();
}
async function arrangeRoom(rid) {
  const ds = S.devices.filter((d) => d.room_id === rid), ids = new Set(ds.map((d) => d.id));
  if (!ds.length) return;
  const succ = new Map(ds.map((d) => [d.id, new Set()])), pred = new Map(ds.map((d) => [d.id, new Set()]));
  for (const c of IX.conns) {
    const f = pdev(c.from_port).id, t = pdev(c.to_port).id;
    if (f !== t && ids.has(f) && ids.has(t)) { succ.get(f).add(t); pred.get(t).add(f); }
  }
  // longest-path ranks, capped so cycles (console <-> stage box) settle
  const rank = new Map(ds.map((d) => [d.id, 0]));
  for (let i = 0; i < ds.length; i++) {
    let changed = false;
    for (const [f, ts] of succ) for (const t of ts) {
      if (rank.get(t) < rank.get(f) + 1 && rank.get(f) + 1 < ds.length) { rank.set(t, rank.get(f) + 1); changed = true; }
    }
    if (!changed) break;
  }
  const layers = [];
  for (const d of ds) (layers[rank.get(d.id)] ||= []).push(d.id);
  const compact = layers.filter(Boolean);
  const yOf = new Map();
  compact.forEach((layer, li) => {
    if (li) layer.sort((a, b) => bary(a) - bary(b));
    layer.forEach((id, i) => yOf.set(id, i));
  });
  function bary(id) { const ps = [...pred.get(id)].filter((p) => yOf.has(p)); return ps.length ? ps.reduce((s, p) => s + yOf.get(p), 0) / ps.length : 999; }
  const saves = [];
  compact.forEach((layer, li) => layer.forEach((id, i) => {
    const d = IX.dev.get(id); d.x = 60 + li * 330; d.y = 60 + i * 100;
    saves.push(api("PATCH", `/api/devices/${id}`, { x: d.x, y: d.y }));
  }));
  await Promise.all(saves);
}
$("#driftonly").onchange = renderMap;
$("#alldante").onchange = renderTable;
$("#adddevice").onclick = addDeviceDialog;
$("#emptyadd").onclick = addDeviceDialog;
$("#settings").onclick = settingsDialog;
$("#emptysettings").onclick = settingsDialog;
$("#search").oninput = (e) => { query = e.target.value.trim().toLowerCase(); render(); };
$("#syncnow").onclick = async () => { await api("POST", "/api/sync"); S.sync_running = true; renderHeader(); poll(); };
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && selected && !$("#dlg").open) closePanel(); });

let pollTimer = null;
function poll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    if (!$("#dlg").open && !document.hidden) { try { await load(); } catch { /* server restarting */ } }
    poll();
  }, S && S.sync_running ? 3000 : 30000);
}
// The hint is for first-time phone users; once they've moved the map it stays gone.
if (ls.get("sm-hint-done", false)) $("#hint").classList.add("gone");
else $("#canvas").addEventListener("pointerdown", () => setTimeout(() => ls.set("sm-hint-done", true), 0), { once: true });
// Crossing between phone and desktop widths (rotating a tablet, resizing a window): use that size's view.
let wasNarrow = narrow();
window.addEventListener("resize", () => {
  if (narrow() === wasNarrow) return;
  wasNarrow = narrow();
  const saved = ls.get(camKey(), null);
  if (saved) { cam = saved; applyCam(); } else fit();
  if (S) renderHeader();
});
const hadSavedView = !!ls.get(camKey(), null);   // check before the first draw saves one
load().then(() => { if (!hadSavedView) fit(); poll(); });
