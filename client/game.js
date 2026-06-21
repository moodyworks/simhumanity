// simhumanity client — a thin renderer. All authority lives on the server;
// we just draw what it sends and forward the player's intent.
"use strict";

const TILE = 24; // pixels per tile

// Keyed by single-char terrain code (server sends compact char rows).
//   ~ water g grass f forest h hills m stone d desert
//   M mountain (impassable)  G glacier/snow (impassable)  P pass (walkable)
const COLORS = {
  "~": "#2b4a6f", g: "#4a7a3a", f: "#2f5a2a",
  h: "#7a6a4a", m: "#6b6f78", d: "#c2a766",
  M: "#58545f", G: "#dfe7ee", P: "#9a8a5a",
};

// Resource caps per terrain code — mirrors server RESOURCE_BY_TERRAIN. Lets the
// client derive the (full-at-start) resource grid from terrain, so the server
// needn't ship 65k numbers at connect; only depletion deltas stream after.
const RESOURCE_CAP = { f: 20, h: 15, m: 30, g: 10 };

// Ground items, colored by category so the map reads at a glance.
const ITEM_COLORS = {
  olives: "#7faa4a", grapes: "#9a6fc0", herbs: "#8fd86a", mushrooms: "#d98a8a",
  flint: "#cfd3da", obsidian: "#3a3550", amber: "#e0a13c",
  shells: "#e7dcc2", clay: "#b07a4a", reeds: "#6aa88a",
  bones: "#efe7d2",
};

const canvas = document.getElementById("view");
const ctx = canvas.getContext("2d");

let myPid = null;
let terrain = null;     // string[height][width]
let mapW = 0, mapH = 0;
let state = null;       // latest snapshot
let era = "";
let gameSocket = null;  // active WebSocket, for actions fired outside keydown
let currentLegend = null; // {builder, text, x, y, claims:[...]} being viewed
let resourceGrid = null;  // full grid from init, kept current via per-tick deltas
let terrainCanvas = null; // offscreen 1px/tile render of terrain, for the minimap
let itemsMap = null;      // "x,y" -> item type, from init + per-tick deltas
let moveTarget = null;    // {x,y} click-to-move destination, for a marker
let landmarks = [];       // famous ancient sites: {name,x,y,era}
let camera = { x: 0, y: 0 }; // view center in tile coords (may be free of player)
let followPlayer = true;  // when true the camera tracks the player each frame
let dialogueOpen = false; // whether a panel (#legend) is currently shown
let knownPlans = [];      // build plans the player has discovered
let buildMenuOpen = false;
let explored = null;      // Uint8Array[h][w] of tiles ever seen (fog of war)
const VISION = 8;         // tile radius the player currently sees
let floaters = [];        // floating combat damage numbers {x,y,dmg,age}
let merchantView = null;  // open barter session {eid,...}

function resize() {
  canvas.width = window.innerWidth;
  canvas.height = window.innerHeight;
}
window.addEventListener("resize", resize);
resize();

// ---- networking -----------------------------------------------------------
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  gameSocket = ws;

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "init") {
      myPid = msg.pid;
      terrain = msg.terrain;
      mapW = msg.width;
      mapH = msg.height;
      era = msg.era;
      // Derive the resource grid from terrain (every tile starts at its cap).
      resourceGrid = terrain.map((row) =>
        Array.from(row, (ch) => RESOURCE_CAP[ch] || 0));
      itemsMap = {};
      for (const it of msg.items || []) itemsMap[it.x + "," + it.y] = it.type;
      landmarks = msg.landmarks || [];
      explored = Array.from({ length: mapH }, () => new Uint8Array(mapW));
      buildTerrainCanvas();
      sizeMinimap();
      document.getElementById("era").textContent = `· ${era} age`;
    } else if (msg.type === "state") {
      state = msg;
      if (resourceGrid && msg.resource_changes) {
        for (const c of msg.resource_changes) resourceGrid[c.y][c.x] = c.amount;
      }
      if (itemsMap && msg.item_changes) {
        for (const c of msg.item_changes) {
          const k = c.x + "," + c.y;
          if (c.type == null) delete itemsMap[k];
          else itemsMap[k] = c.type;
        }
      }
      // Age existing damage floaters; add any new combat hits.
      floaters = floaters.filter((f) => (f.age += 0.18) < 1);
      for (const c of msg.combat || []) floaters.push({ x: c.x, y: c.y, dmg: c.dmg, age: 0 });
      draw();
      updateHud();
    } else if (msg.type === "log") {
      toast(msg.text, 4000);
    } else if (msg.type === "event") {
      toast(msg.text, 7000);
    } else if (msg.type === "myth_pending") {
      showLegendPending();
    } else if (msg.type === "myth") {
      showLegend(msg);
    } else if (msg.type === "verdict") {
      applyVerdict(msg);
    } else if (msg.type === "landmark") {
      showSite(msg);
    } else if (msg.type === "site_response") {
      applySiteAnswer(msg);
    } else if (msg.type === "plans") {
      knownPlans = msg.plans || knownPlans;
      if (buildMenuOpen) renderBuildMenu();
    } else if (msg.type === "merchant") {
      showMerchant(msg);
    } else if (msg.type === "npc") {
      showNpc(msg);
    }
  };

  ws.onclose = () => setTimeout(connect, 1000); // auto-reconnect

  // ---- input: send intent, server decides ----
  const send = (obj) => { if (ws.readyState === 1) ws.send(JSON.stringify(obj)); };
  window.addEventListener("keydown", (e) => {
    let dx = 0, dy = 0;
    switch (e.key) {
      case "w": case "ArrowUp":    dy = -1; break;
      case "s": case "ArrowDown":  dy = 1;  break;
      case "a": case "ArrowLeft":  dx = -1; break;
      case "d": case "ArrowRight": dx = 1;  break;
      case " ": send({ action: "gather" }); e.preventDefault(); return;
      case "b": case "B": toggleBuildMenu(); e.preventDefault(); return;
      case "e": send({ action: "dig" }); return;
      case "f": case "F": send({ action: "interact" }); return;
      case "r": case "R": send({ action: "attack" }); return;
      case "Escape": closeDialogue(); return;
      default: return;
    }
    e.preventDefault();
    // Moving cancels any open dialogue and re-attaches the camera to the player.
    closeDialogue();
    followPlayer = true;
    send({ action: "move", dx, dy });
  });
}

// Fire an action over the active socket (used by legend buttons, outside keydown).
function sendAction(obj) {
  if (gameSocket && gameSocket.readyState === 1) {
    gameSocket.send(JSON.stringify(obj));
  }
}

// ---- transient on-screen messages ----------------------------------------
let toastTimer = null;
function toast(text, ms) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.style.opacity = "1";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.style.opacity = "0"; }, ms);
}

// The legend a ruin "remembers" — the distorted myth from the AI Historian.
let legendTimer = null;
function showLegendPending() {
  const el = document.getElementById("legend");
  el.innerHTML =
    `<div class="legend-title">A legend stirs…</div>` +
    `<div class="legend-body">The Historian sifts the dust of ages, ` +
    `recalling the tale of who once stood here…</div>`;
  el.style.opacity = "1";
  el.style.pointerEvents = "none";
  dialogueOpen = true;
  clearTimeout(legendTimer);
}
function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// Render one claim row: either interactive (judge / hoard) or a revealed verdict.
function claimRowHTML(c) {
  if (c.resolved) {
    const tag = c.mode === "hoard"
      ? (c.truth ? "TRUE" : "FALSE")
      : (c.correct ? "you were right" : "you were wrong");
    const cls = (c.mode === "hoard" ? c.truth : c.correct) ? "ok" : "bad";
    return (
      `<div class="claim resolved">` +
      `<div class="claim-text">…${esc(c.text)}</div>` +
      `<div class="claim-verdict ${cls}">${esc(c.result_text || "")} ` +
      `<span class="claim-basis">— ${esc(c.basis || "")} (${tag})</span></div>` +
      `</div>`
    );
  }
  let controls;
  if (c.mode === "hoard") {
    controls =
      `<button class="claim-btn dig" onclick="investigateClaim(${c.id}, null)">` +
      `⛏ Dig for the hoard</button>`;
  } else {
    controls =
      `<button class="claim-btn" onclick="investigateClaim(${c.id}, true)">It's true</button>` +
      `<button class="claim-btn" onclick="investigateClaim(${c.id}, false)">Embellished</button>`;
  }
  return (
    `<div class="claim">` +
    `<div class="claim-text">…${esc(c.text)}</div>` +
    `<div class="claim-controls">${controls}</div>` +
    `</div>`
  );
}

function renderLegend() {
  const el = document.getElementById("legend");
  const L = currentLegend;
  if (!L) return;
  const claimsHTML = (L.claims || []).map(claimRowHTML).join("");
  el.innerHTML =
    `<div class="legend-title">The legend of ${esc(L.builder || "the forgotten")}</div>` +
    `<div class="legend-body">${esc(L.text)}</div>` +
    (claimsHTML
      ? `<div class="claims-head">Judge the tale — which parts are true?</div>` +
        `<div class="claims">${claimsHTML}</div>`
      : "") +
    `<div class="legend-close" onclick="hideLegend()">✕ dismiss</div>`;
  el.style.opacity = "1";
  el.style.pointerEvents = "auto";
}

function showLegend(msg) {
  currentLegend = {
    builder: msg.builder, text: msg.text,
    x: msg.x, y: msg.y, claims: msg.claims || [],
  };
  clearTimeout(legendTimer);
  dialogueOpen = true;
  renderLegend();
  // Auto-dismiss only if there's nothing to interact with.
  if (!currentLegend.claims.some((c) => !c.resolved)) {
    legendTimer = setTimeout(() => hideLegend(), 20000);
  }
}

function investigateClaim(id, guess) {
  if (!currentLegend) return;
  sendAction({
    action: "investigate",
    x: currentLegend.x, y: currentLegend.y,
    claim: id, guess: guess,
  });
}

function applyVerdict(msg) {
  if (!currentLegend || msg.x !== currentLegend.x || msg.y !== currentLegend.y) return;
  const c = currentLegend.claims.find((cl) => cl.id === msg.id);
  if (!c) return;
  Object.assign(c, {
    resolved: true, truth: msg.truth, basis: msg.basis,
    correct: msg.correct, result_text: msg.result_text,
  });
  if (msg.result_text) toast(msg.result_text, 4000);
  renderLegend();
}

function hideLegend() {
  const el = document.getElementById("legend");
  el.style.opacity = "0";
  el.style.pointerEvents = "none";
  currentLegend = null;
  dialogueOpen = false;
}

// Close whatever panel is open. If the player abandons an unanswered site quiz,
// tell the server so the site is left un-excavated (re-diggable later).
function closeDialogue() {
  if (currentSite && !currentSite.done) {
    sendAction({ action: "site_abandon", x: currentSite.x, y: currentSite.y });
  }
  currentSite = null;
  merchantView = null;
  closeBuildMenu();
  hideLegend();
}

// ---- famous ancient site: a quiz you must answer to claim the relic --------
let currentSite = null; // {x,y,name,era,note,questions:[...],done}
function showSite(msg) {
  currentLegend = null;
  currentSite = {
    x: msg.x, y: msg.y, name: msg.name, era: msg.era, note: msg.note,
    questions: msg.questions || [], done: !!msg.done,
  };
  renderSite();
}

function renderSite() {
  const s = currentSite;
  if (!s) return;
  const el = document.getElementById("legend");
  const qHTML = s.questions.map((q) => {
    if (q.resolved) {
      const cls = q.correct ? "ok" : "bad";
      return `<div class="claim resolved"><div class="claim-text">${esc(q.text)}</div>` +
        `<div class="claim-verdict ${cls}">${q.correct ? "Correct" : "Wrong"} — ` +
        `<span class="claim-basis">${esc(q.basis || "")}</span></div></div>`;
    }
    return `<div class="claim"><div class="claim-text">${esc(q.text)}</div>` +
      `<div class="claim-controls">` +
      `<button class="claim-btn" onclick="answerSite(${q.id},true)">True</button>` +
      `<button class="claim-btn" onclick="answerSite(${q.id},false)">False</button>` +
      `</div></div>`;
  }).join("");
  const allDone = s.questions.every((q) => q.resolved);
  el.innerHTML =
    `<div class="legend-title">${esc(s.name)} &nbsp;·&nbsp; ${esc(s.era)}</div>` +
    `<div class="legend-body" style="font-style:normal">${esc(s.note)}</div>` +
    (s.done
      ? `<div class="legend-hint">You have already studied this site.</div>`
      : `<div class="claims-head">Verify the record to claim the relic — ` +
        `judge each account:</div><div class="claims">${qHTML}</div>` +
        (allDone ? "" : `<div class="legend-hint">Walk away and the dig is ` +
          `abandoned — the site keeps its secrets.</div>`)) +
    `<div class="legend-close" onclick="closeDialogue()">✕ dismiss</div>`;
  el.style.opacity = "1";
  el.style.pointerEvents = "auto";
  dialogueOpen = true;
}

function answerSite(qid, guess) {
  if (!currentSite) return;
  sendAction({ action: "site_answer", x: currentSite.x, y: currentSite.y,
    q: qid, guess: guess });
}

function applySiteAnswer(msg) {
  if (!currentSite || msg.x !== currentSite.x || msg.y !== currentSite.y) return;
  const q = currentSite.questions.find((qq) => qq.id === msg.id);
  if (q) Object.assign(q, { resolved: true, correct: msg.correct, basis: msg.basis });
  if (msg.complete) {
    currentSite.done = true;
    if (msg.result_text) toast(msg.result_text, 5000);
  }
  renderSite();
}

// ---- build menu: pick from discovered plans you can afford -----------------
function toggleBuildMenu() {
  buildMenuOpen ? closeBuildMenu() : openBuildMenu();
}
function openBuildMenu() {
  closeDialogue();
  buildMenuOpen = true;
  renderBuildMenu();
}
function closeBuildMenu() {
  buildMenuOpen = false;
  const el = document.getElementById("legend");
  if (el && !currentSite && !currentLegend) {
    el.style.opacity = "0";
    el.style.pointerEvents = "none";
  }
}
function invCount(res) {
  const self = me();
  return self && self.inventory ? (self.inventory[res] || 0) : 0;
}
function renderBuildMenu() {
  const el = document.getElementById("legend");
  const rows = knownPlans.map((p) => {
    const costStr = Object.entries(p.cost || {})
      .map(([r, n]) => `${n} ${r}`).join(", ") || "free";
    const afford = Object.entries(p.cost || {}).every(([r, n]) => invCount(r) >= n);
    const btn = afford
      ? `<button class="claim-btn" onclick="buildPlan('${p.type}')">Build</button>`
      : `<span class="claim-basis">need more</span>`;
    return `<div class="claim"><div class="claim-text"><b>${esc(p.label)}</b> ` +
      `— <span class="claim-basis">${esc(p.desc || "")}</span></div>` +
      `<div class="claim-controls"><span class="claim-basis">${esc(costStr)}</span> ${btn}</div></div>`;
  }).join("");
  el.innerHTML =
    `<div class="legend-title">Build &nbsp;·&nbsp; what to raise here</div>` +
    (knownPlans.length
      ? `<div class="claims">${rows}</div>`
      : `<div class="legend-body" style="font-style:normal">You know no plans ` +
        `yet. Discover them by excavating ruins and ancient sites.</div>`) +
    `<div class="legend-close" onclick="closeBuildMenu()">✕ close</div>`;
  el.style.opacity = "1";
  el.style.pointerEvents = "auto";
  dialogueOpen = true;
}
function buildPlan(type) {
  sendAction({ action: "build", type: type });
  closeBuildMenu();
}

// ---- NPC dialogue + merchant barter ---------------------------------------
function showNpc(msg) {
  currentSite = null; merchantView = null;
  const el = document.getElementById("legend");
  el.innerHTML =
    `<div class="legend-title">${esc(msg.name)}</div>` +
    `<div class="legend-body">“${esc(msg.line)}”</div>` +
    `<div class="legend-close" onclick="closeDialogue()">✕ leave</div>`;
  el.style.opacity = "1"; el.style.pointerEvents = "auto"; dialogueOpen = true;
}

function showMerchant(msg) {
  currentSite = null;
  merchantView = msg;
  const buy = (msg.wares || []).map((w) =>
    `<div class="claim"><div class="claim-text">${esc(w.item)} ` +
    `<span class="claim-basis">${w.price} coin</span></div>` +
    `<div class="claim-controls"><button class="claim-btn" ` +
    `onclick="barter('buy','${w.item}')">Buy</button></div></div>`).join("");
  const sell = (msg.sell || []).map((s) =>
    `<div class="claim"><div class="claim-text">${esc(s.item)} ×${s.qty} ` +
    `<span class="claim-basis">${s.price} coin each</span></div>` +
    `<div class="claim-controls"><button class="claim-btn" ` +
    `onclick="barter('sell','${s.item}')">Sell</button></div></div>`).join("");
  const el = document.getElementById("legend");
  el.innerHTML =
    `<div class="legend-title">${esc(msg.name)} &nbsp;·&nbsp; your coin: ${msg.coin}</div>` +
    `<div class="legend-body" style="font-style:normal">“${esc(msg.line)}”</div>` +
    `<div class="claims-head">Wares for sale</div><div class="claims">${buy || "<div class='legend-hint'>Nothing today.</div>"}</div>` +
    `<div class="claims-head">Sell your goods</div><div class="claims">${sell || "<div class='legend-hint'>Your pack is empty.</div>"}</div>` +
    `<div class="legend-close" onclick="closeDialogue()">✕ leave</div>`;
  el.style.opacity = "1"; el.style.pointerEvents = "auto"; dialogueOpen = true;
}

function barter(trade, item) {
  if (!merchantView) return;
  sendAction({ action: "barter", eid: merchantView.eid, trade, item, qty: 1 });
}

// ---- rendering ------------------------------------------------------------
function me() {
  if (!state || !myPid) return null;
  return state.players.find((p) => p.pid === myPid) || null;
}

function drawStar(cx, cy, points, outer, inner, fill, stroke) {
  ctx.beginPath();
  for (let i = 0; i < points * 2; i++) {
    const r = i % 2 === 0 ? outer : inner;
    const a = (Math.PI * i) / points - Math.PI / 2;
    const fn = i === 0 ? "moveTo" : "lineTo";
    ctx[fn](cx + Math.cos(a) * r, cy + Math.sin(a) * r);
  }
  ctx.closePath();
  ctx.fillStyle = fill;
  ctx.fill();
  ctx.lineWidth = 1;
  ctx.strokeStyle = stroke;
  ctx.stroke();
}

function draw() {
  if (!terrain || !state) return;
  ctx.fillStyle = "#0c0d12";
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  // Camera follows the player unless the view was panned via the minimap.
  const self = me();
  if (followPlayer && self) { camera.x = self.x; camera.y = self.y; }
  const { offX, offY } = cameraOffset();

  // Only iterate the tiles actually on screen — the map is large (300x219).
  const x0 = Math.max(0, Math.floor(-offX / TILE));
  const y0 = Math.max(0, Math.floor(-offY / TILE));
  const x1 = Math.min(mapW, Math.ceil((canvas.width - offX) / TILE));
  const y1 = Math.min(mapH, Math.ceil((canvas.height - offY) / TILE));
  for (let y = y0; y < y1; y++) {
    for (let x = x0; x < x1; x++) {
      const px = offX + x * TILE;
      const py = offY + y * TILE;
      ctx.fillStyle = COLORS[terrain[y][x]] || "#000";
      ctx.fillRect(px, py, TILE, TILE);

      // Resource dot — fades as the tile is depleted.
      const amt = resourceGrid ? resourceGrid[y][x] : 0;
      if (amt > 0) {
        ctx.fillStyle = `rgba(230, 220, 120, ${Math.min(0.85, amt / 20)})`;
        ctx.beginPath();
        ctx.arc(px + TILE / 2, py + TILE / 2, 2.5, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }

  // Ground items — small bright markers you can walk onto and gather.
  for (const k in (itemsMap || {})) {
    const ci = k.indexOf(",");
    const ix = +k.slice(0, ci), iy = +k.slice(ci + 1);
    const px = offX + ix * TILE;
    const py = offY + iy * TILE;
    if (px < -TILE || py < -TILE || px > canvas.width || py > canvas.height)
      continue;
    const cx = px + TILE / 2, cy = py + TILE / 2;
    ctx.fillStyle = ITEM_COLORS[itemsMap[k]] || "#ffffff";
    ctx.strokeStyle = "rgba(0,0,0,0.5)";
    ctx.lineWidth = 1;
    ctx.beginPath();                 // a small diamond
    ctx.moveTo(cx, cy - 4);
    ctx.lineTo(cx + 4, cy);
    ctx.lineTo(cx, cy + 4);
    ctx.lineTo(cx - 4, cy);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
  }

  // Ruins (buried mounds) — drawn under players.
  for (const r of state.ruins || []) {
    const px = offX + r.x * TILE;
    const py = offY + r.y * TILE;
    if (r.excavated) {
      // Open pit.
      ctx.fillStyle = "#1a1407";
      ctx.fillRect(px + 4, py + 4, TILE - 8, TILE - 8);
    } else {
      // Buried mound with a marker.
      ctx.fillStyle = "#5a4a32";
      ctx.beginPath();
      ctx.ellipse(px + TILE / 2, py + TILE * 0.62, TILE * 0.36,
        TILE * 0.24, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#d8c98a";
      ctx.font = "bold 12px monospace";
      ctx.textAlign = "center";
      ctx.fillText("?", px + TILE / 2, py + TILE * 0.5);
    }
  }

  // Structures (current era).
  for (const s of state.structures || []) {
    const px = offX + s.x * TILE;
    const py = offY + s.y * TILE;
    const cx = px + TILE / 2;
    const cy = py + TILE / 2;
    if (s.type === "stone_circle") {
      ctx.strokeStyle = "#cfd3da";
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.arc(cx, cy, TILE * 0.34, 0, Math.PI * 2);
      ctx.stroke();
    } else if (s.type === "hut") {
      ctx.fillStyle = "#8a5a2a";
      ctx.fillRect(px + 6, py + 8, TILE - 12, TILE - 12);
      ctx.fillStyle = "#b5793c";
      ctx.beginPath();
      ctx.moveTo(px + 4, py + 9);
      ctx.lineTo(cx, py + 2);
      ctx.lineTo(px + TILE - 4, py + 9);
      ctx.fill();
    } else { // cache
      ctx.fillStyle = "#7a6a45";
      ctx.fillRect(px + 8, py + 8, TILE - 16, TILE - 16);
    }
  }

  // Famous ancient sites — a gold star + always-on label.
  for (const lm of landmarks) {
    const px = offX + lm.x * TILE;
    const py = offY + lm.y * TILE;
    if (px < -60 || py < -20 || px > canvas.width + 60 || py > canvas.height)
      continue;
    const cx = px + TILE / 2, cy = py + TILE / 2;
    drawStar(cx, cy, 5, TILE * 0.5, TILE * 0.22, "#ffd86b", "#5a4012");
    ctx.fillStyle = "rgba(0,0,0,0.55)";
    const w = ctx.measureText(lm.name).width + 8;
    ctx.fillRect(cx - w / 2, py - 15, w, 13);
    ctx.fillStyle = "#ffe9a8";
    ctx.font = "11px monospace";
    ctx.textAlign = "center";
    ctx.fillText(lm.name, cx, py - 5);
  }

  // Entities: merchants, wanderers, roaming brigands.
  for (const en of state.entities || []) {
    const px = offX + en.x * TILE, py = offY + en.y * TILE;
    if (px < -TILE || py < -TILE || px > canvas.width || py > canvas.height)
      continue;
    const cx = px + TILE / 2, cy = py + TILE / 2;
    if (en.kind === "merchant") {
      ctx.fillStyle = "#e3c25a";
      drawStar(cx, cy, 4, TILE * 0.34, TILE * 0.16, "#e3c25a", "#5a4a12");
    } else if (en.kind === "brigand") {
      ctx.fillStyle = en.hostile ? "#ff5a4a" : "#9a4438";
      ctx.beginPath();
      ctx.arc(cx, cy, TILE * 0.32, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#1a0d0a";
      ctx.font = "bold 11px monospace";
      ctx.textAlign = "center";
      ctx.fillText("☠", cx, cy + 4);
    } else { // wanderer
      ctx.fillStyle = "#5aa6c0";
      ctx.beginPath();
      ctx.arc(cx, cy, TILE * 0.3, 0, Math.PI * 2);
      ctx.fill();
    }
    // HP bar when wounded.
    if (en.hp < en.max_hp) {
      const w = TILE * 0.7;
      ctx.fillStyle = "#300";
      ctx.fillRect(cx - w / 2, py - 4, w, 3);
      ctx.fillStyle = "#d44";
      ctx.fillRect(cx - w / 2, py - 4, w * Math.max(0, en.hp / en.max_hp), 3);
    }
  }

  // Floating combat damage numbers.
  for (const f of floaters) {
    const px = offX + f.x * TILE + TILE / 2, py = offY + f.y * TILE - f.age * 12;
    ctx.fillStyle = `rgba(255,90,80,${Math.max(0, 1 - f.age)})`;
    ctx.font = "bold 13px monospace";
    ctx.textAlign = "center";
    ctx.fillText(`-${f.dmg}`, px, py);
  }

  // Click-to-move destination marker (cleared once we arrive).
  const self2 = me();
  if (moveTarget && self2) {
    if (self2.x === moveTarget.x && self2.y === moveTarget.y) {
      moveTarget = null;
    } else {
      const mx = offX + moveTarget.x * TILE + TILE / 2;
      const my = offY + moveTarget.y * TILE + TILE / 2;
      ctx.strokeStyle = "#ffe08a";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(mx, my, TILE * 0.4, 0, Math.PI * 2);
      ctx.moveTo(mx - 5, my); ctx.lineTo(mx + 5, my);
      ctx.moveTo(mx, my - 5); ctx.lineTo(mx, my + 5);
      ctx.stroke();
    }
  }

  // Players — drawn as a ring (not a solid disk) so whatever is on the tile
  // (a hut you just built, a ruin you're standing on) stays visible underneath.
  for (const p of state.players) {
    const px = offX + p.x * TILE;
    const py = offY + p.y * TILE;
    // A player standing on water is in a boat — draw a little hull under them.
    if (terrain[p.y] && terrain[p.y][p.x] === "~") {
      ctx.fillStyle = "#6b4a2a";
      ctx.beginPath();
      ctx.moveTo(px + 3, py + TILE * 0.55);
      ctx.lineTo(px + TILE - 3, py + TILE * 0.55);
      ctx.lineTo(px + TILE - 7, py + TILE * 0.8);
      ctx.lineTo(px + 7, py + TILE * 0.8);
      ctx.closePath();
      ctx.fill();
    }
    const color = p.pid === myPid ? "#ffd35c" : "#e06b6b";
    ctx.lineWidth = 3;
    ctx.strokeStyle = color;
    ctx.beginPath();
    ctx.arc(px + TILE / 2, py + TILE / 2, TILE * 0.38, 0, Math.PI * 2);
    ctx.stroke();
    // Small solid core so the player is still easy to spot.
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(px + TILE / 2, py + TILE / 2, TILE * 0.12, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#dfe3ea";
    ctx.font = "10px monospace";
    ctx.textAlign = "center";
    ctx.fillText(p.name, px + TILE / 2, py - 2);
  }

  // ---- fog of war: clear within vision, dim the explored, hide the unknown.
  if (explored && self) {
    for (let y = y0; y < y1; y++) {
      for (let x = x0; x < x1; x++) {
        const dx = x - self.x, dy = y - self.y;
        if (dx * dx + dy * dy <= VISION * VISION) { explored[y][x] = 1; continue; }
        ctx.fillStyle = explored[y][x] ? "rgba(8,9,14,0.55)" : "rgba(8,9,14,1)";
        ctx.fillRect(offX + x * TILE, offY + y * TILE, TILE, TILE);
      }
    }
  }

  renderMinimap();
}

// ---- minimap --------------------------------------------------------------
function buildTerrainCanvas() {
  terrainCanvas = document.createElement("canvas");
  terrainCanvas.width = mapW;
  terrainCanvas.height = mapH;
  const tctx = terrainCanvas.getContext("2d");
  for (let y = 0; y < mapH; y++) {
    for (let x = 0; x < mapW; x++) {
      tctx.fillStyle = COLORS[terrain[y][x]] || "#000";
      tctx.fillRect(x, y, 1, 1);
    }
  }
}

function sizeMinimap() {
  const mm = document.getElementById("minimap");
  const targetW = 260;
  mm.width = targetW;
  mm.height = Math.round(targetW * mapH / mapW);
}

function renderMinimap() {
  const mm = document.getElementById("minimap");
  if (!terrainCanvas || !mm) return;
  const m = mm.getContext("2d");
  const MW = mm.width, MH = mm.height;
  m.imageSmoothingEnabled = false;
  m.drawImage(terrainCanvas, 0, 0, MW, MH);

  // Current viewport rectangle (reflects the camera, which may be panned).
  const tilesW = canvas.width / TILE, tilesH = canvas.height / TILE;
  m.strokeStyle = "rgba(255,255,255,0.85)";
  m.lineWidth = 1;
  m.strokeRect(
    (camera.x - tilesW / 2) / mapW * MW, (camera.y - tilesH / 2) / mapH * MH,
    tilesW / mapW * MW, tilesH / mapH * MH,
  );

  // Ancient sites — small gold pips.
  for (const lm of landmarks) {
    m.fillStyle = "#ffd86b";
    m.fillRect(lm.x / mapW * MW - 1, lm.y / mapH * MH - 1, 2.5, 2.5);
  }

  // Players.
  if (state) {
    for (const p of state.players) {
      m.fillStyle = p.pid === myPid ? "#ffd35c" : "#e06b6b";
      m.fillRect(p.x / mapW * MW - 1.5, p.y / mapH * MH - 1.5, 3, 3);
    }
  }
}

function updateHud() {
  if (!state) return;
  const mins = state.world_time;
  const days = Math.floor(mins / (60 * 24));
  document.getElementById("clock").textContent =
    `tick ${state.tick} · day ${days}`;
  const self = me();
  const inv = self ? self.inventory : {};
  const parts = Object.entries(inv).map(([k, v]) => `${k}: ${v}`);
  document.getElementById("inv").textContent =
    parts.length ? parts.join("  ·  ") : "(empty pack)";
  const lore = self ? (self.lore || 0) : 0;
  const hp = self ? self.hp : 0, maxhp = self ? self.max_hp : 100;
  const coin = self ? (self.coin || 0) : 0;
  document.getElementById("vitals").textContent =
    `HP ${hp}/${maxhp} · ${coin} coin · renown ${lore}`;
  document.getElementById("lore").textContent = "";
}

// Camera top-left offset in screen pixels — shared by draw() and click math.
function cameraOffset() {
  return {
    offX: canvas.width / 2 - camera.x * TILE - TILE / 2,
    offY: canvas.height / 2 - camera.y * TILE - TILE / 2,
  };
}

// ---- click-to-move (attached once; survives reconnects) -------------------
function gotoTile(tx, ty) {
  if (tx < 0 || ty < 0 || tx >= mapW || ty >= mapH) return;
  moveTarget = { x: tx, y: ty };
  sendAction({ action: "goto", x: tx, y: ty });
}

// Clicking the world: an adjacent entity → interact/attack; else walk there.
canvas.addEventListener("click", (e) => {
  if (!state) return;
  const { offX, offY } = cameraOffset();
  const tx = Math.floor((e.clientX - offX) / TILE);
  const ty = Math.floor((e.clientY - offY) / TILE);
  const self = me();
  const en = (state.entities || []).find((q) => q.x === tx && q.y === ty);
  if (en && self && Math.abs(en.x - self.x) + Math.abs(en.y - self.y) <= 1) {
    sendAction({ action: en.kind === "brigand" ? "attack" : "interact" });
    return;
  }
  closeDialogue();
  followPlayer = true;
  gotoTile(tx, ty);
});

// Clicking the minimap PANS the view only — it never moves the player.
function panFromMinimap(e) {
  if (!mapW) return;
  const r = e.currentTarget.getBoundingClientRect();
  camera.x = Math.max(0, Math.min(mapW - 1, (e.clientX - r.left) / r.width * mapW));
  camera.y = Math.max(0, Math.min(mapH - 1, (e.clientY - r.top) / r.height * mapH));
  followPlayer = false; // detach from the player until they move again
  draw();
}
const minimapEl = document.getElementById("minimap");
minimapEl.addEventListener("click", panFromMinimap);
// Drag to pan as well.
let panning = false;
minimapEl.addEventListener("mousedown", (e) => { panning = true; panFromMinimap(e); });
window.addEventListener("mousemove", (e) => {
  if (panning && e.target === minimapEl) panFromMinimap({ clientX: e.clientX,
    clientY: e.clientY, currentTarget: minimapEl });
});
window.addEventListener("mouseup", () => { panning = false; });

connect();
