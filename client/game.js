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
let cameraInit = false;   // snap to the player on first sight
let lastPlayerTile = null, lastMoveAt = 0; // for the move/stop camera behaviour
let dialogueOpen = false; // whether a panel (#legend) is currently shown
let knownPlans = [];      // build plans the player has discovered
let buildMenuOpen = false;
let explored = null;      // Uint8Array[h][w] of tiles ever seen (fog of war)
let fogCanvas = null;     // map-res fog layer (opaque where unexplored), for the minimap
let fogCtx = null;
let visibleSet = null;    // "x,y" tiles in current line of sight
let lastVisKey = null;    // player tile the visibleSet was computed for
const VISION = 8;         // tile radius the player currently sees

// Mountains and glaciers block sight.
function isOpaque(x, y) {
  const t = terrain[y] && terrain[y][x];
  return t === "M" || t === "G";
}
// Bresenham line of sight: clear unless an opaque tile lies before the target.
function hasLOS(px, py, tx, ty) {
  let x = px, y = py;
  const dx = Math.abs(tx - px), dy = Math.abs(ty - py);
  const sx = px < tx ? 1 : -1, sy = py < ty ? 1 : -1;
  let err = dx - dy;
  for (let i = 0; i < dx + dy + 2; i++) {
    const e2 = 2 * err;
    if (e2 > -dy) { err -= dy; x += sx; }
    if (e2 < dx) { err += dy; y += sy; }
    if (x === tx && y === ty) return true;
    if (isOpaque(x, y)) return false;
  }
  return true;
}
let floaters = [];        // floating combat damage numbers {x,y,dmg,age}
let merchantView = null;  // open barter session {eid,...}
let myRelics = [];        // this player's relics {id,name,clue,source}
let relicPanelOpen = false;
let fogEnabled = true;    // debug toggle for fog of war
let kmPerTile = 0;        // real km each tile spans, for the scale bar

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
      kmPerTile = msg.km_per_tile || 0;
      explored = Array.from({ length: mapH }, () => new Uint8Array(mapW));
      // Fog layer for the minimap: starts fully dark, cleared as tiles are seen.
      fogCanvas = document.createElement("canvas");
      fogCanvas.width = mapW; fogCanvas.height = mapH;
      fogCtx = fogCanvas.getContext("2d");
      fogCtx.fillStyle = "#0c0d12";
      fogCtx.fillRect(0, 0, mapW, mapH);
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
      updateHud();  // the render loop (rAF) draws continuously for a smooth camera
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
    } else if (msg.type === "relics") {
      myRelics = msg.relics || [];
      if (relicPanelOpen) renderRelics();
    }
  };

  ws.onclose = () => setTimeout(connect, 1000); // auto-reconnect
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
  relicPanelOpen = false;
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
  const plans = (msg.plans || []).map((pl) => {
    const btn = pl.known
      ? `<span class="claim-basis">known</span>`
      : `<button class="claim-btn" onclick="barter('plan','${pl.type}')">Learn (${pl.price})</button>`;
    return `<div class="claim"><div class="claim-text">📜 ${esc(pl.label)} plan ` +
      `<span class="claim-basis">${pl.price} coin</span></div>` +
      `<div class="claim-controls">${btn}</div></div>`;
  }).join("");
  const el = document.getElementById("legend");
  el.innerHTML =
    `<div class="legend-title">${esc(msg.name)} &nbsp;·&nbsp; your coin: ${msg.coin}</div>` +
    `<div class="legend-body" style="font-style:normal">“${esc(msg.line)}”</div>` +
    (plans ? `<div class="claims-head">Build plans</div><div class="claims">${plans}</div>` : "") +
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

// Deterministic settlement: buildings within a radius set by stage, faded ruins
// in the former extent when a city has declined, plus a name label.
function drawCity(px, py, c) {
  const stage = c.stage, mx = c.max;
  let s = (c.x * 92821 + c.y * 68917) >>> 0;
  const rnd = () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296);
  const ringR = (lvl) => lvl * 7 + 4; // pixel radius per stage level
  // A building only sits on land — keep settlements out of the sea.
  const onLand = (bx, by) => {
    const tx = c.x + Math.round((bx - px) / TILE);
    const ty = c.y + Math.round((by - py) / TILE);
    return terrain[ty] && terrain[ty][tx] && terrain[ty][tx] !== "~";
  };

  // Faded ruins in the ring(s) the city has shrunk out of.
  if (mx > stage) {
    for (let lvl = stage + 1; lvl <= mx; lvl++) {
      const r = ringR(lvl), n = lvl * 3;
      for (let i = 0; i < n; i++) {
        const a = rnd() * Math.PI * 2, rr = r * (0.6 + 0.4 * rnd());
        const bx = px + Math.cos(a) * rr, by = py + Math.sin(a) * rr;
        if (!onLand(bx, by)) continue;
        ctx.fillStyle = "rgba(120,110,96,0.5)";
        ctx.fillRect(bx - 1.5, by - 1.5, 3, 3);
      }
    }
  }
  // Living buildings, denser and larger with stage.
  if (stage > 0) {
    const r = ringR(stage), n = stage * stage * 4;
    for (let i = 0; i < n; i++) {
      const a = rnd() * Math.PI * 2, rr = r * Math.sqrt(rnd());
      const bx = px + Math.cos(a) * rr, by = py + Math.sin(a) * rr;
      if (!onLand(bx, by)) continue;
      const sz = 2 + stage * 0.4;
      ctx.fillStyle = i % 4 === 0 ? "#caa46a" : "#9c7846";
      ctx.fillRect(bx - sz / 2, by - sz / 2, sz, sz);
    }
    if (stage >= 3) {            // a wall ring for cities/metropolises
      ctx.strokeStyle = "rgba(210,200,180,0.5)";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(px, py, r + 2, 0, Math.PI * 2);
      ctx.stroke();
    }
  }
  // Label.
  const label = stage === 0 ? `${c.name} (ruins)` : c.name;
  ctx.font = stage >= 3 ? "bold 12px monospace" : "11px monospace";
  ctx.textAlign = "center";
  const lw = ctx.measureText(label).width + 8;
  ctx.fillStyle = "rgba(0,0,0,0.5)";
  ctx.fillRect(px - lw / 2, py - ringR(Math.max(stage, mx)) - 16, lw, 13);
  ctx.fillStyle = stage === 0 ? "#b8a98c" : "#f0e2bf";
  ctx.fillText(label, px, py - ringR(Math.max(stage, mx)) - 6);
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

  const self = me();
  const { offX, offY } = cameraOffset();

  // Line-of-sight set (within vision AND not blocked by mountains/glaciers).
  // Depends only on the player's tile, so recompute only when that changes —
  // the render loop runs every frame for the camera, not the LOS.
  if (explored && self) {
    const visKey = self.x + "," + self.y;
    if (visKey !== lastVisKey || !visibleSet) {
      lastVisKey = visKey;
      visibleSet = new Set();
      const x0v = Math.max(0, self.x - VISION), x1v = Math.min(mapW - 1, self.x + VISION);
      const y0v = Math.max(0, self.y - VISION), y1v = Math.min(mapH - 1, self.y + VISION);
      for (let yy = y0v; yy <= y1v; yy++) {
        for (let xx = x0v; xx <= x1v; xx++) {
          const ddx = xx - self.x, ddy = yy - self.y;
          if (ddx * ddx + ddy * ddy > VISION * VISION) continue;
          if (!hasLOS(self.x, self.y, xx, yy)) continue;
          visibleSet.add(xx + "," + yy);
          if (!explored[yy][xx]) { explored[yy][xx] = 1; fogCtx.clearRect(xx, yy, 1, 1); }
        }
      }
    }
  } else {
    visibleSet = null;
  }

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

  // Famous ancient sites — a gold star marking an excavatable site. Suppress a
  // landmark's label only when a city that's *actually drawn here* shows its own
  // (so an unfounded/fogged city doesn't leave a nameless star).
  const cityTiles = new Set();
  for (const c of state.cities || []) {
    if (c.stage === 0 && c.max === 0) continue;
    if (fogEnabled && !(explored && explored[c.y] && explored[c.y][c.x])) continue;
    cityTiles.add(c.x + "," + c.y);
  }
  for (const lm of landmarks) {
    const px = offX + lm.x * TILE;
    const py = offY + lm.y * TILE;
    if (px < -60 || py < -20 || px > canvas.width + 60 || py > canvas.height)
      continue;
    const cx = px + TILE / 2, cy = py + TILE / 2;
    drawStar(cx, cy, 5, TILE * 0.5, TILE * 0.22, "#ffd86b", "#5a4012");
    if (cityTiles.has(lm.x + "," + lm.y)) continue; // a city here shows the name
    ctx.fillStyle = "rgba(0,0,0,0.55)";
    const w = ctx.measureText(lm.name).width + 8;
    ctx.fillRect(cx - w / 2, py - 15, w, 13);
    ctx.fillStyle = "#ffe9a8";
    ctx.font = "11px monospace";
    ctx.textAlign = "center";
    ctx.fillText(lm.name, cx, py - 5);
  }

  // Cities: settlements that grow and crumble across the ages.
  for (const c of state.cities || []) {
    if (c.stage === 0 && c.max === 0) continue; // not founded yet — hidden
    if (fogEnabled && !(explored && explored[c.y] && explored[c.y][c.x])) continue;
    const px = offX + c.x * TILE + TILE / 2, py = offY + c.y * TILE + TILE / 2;
    if (px < -120 || py < -80 || px > canvas.width + 120 || py > canvas.height + 40)
      continue;
    drawCity(px, py, c);
  }

  // Entities: merchants, wanderers, brigands, sea monsters. Mobile creatures
  // are only shown within your current line of sight (vision radius) — unless
  // fog is toggled off (O), which reveals them all.
  for (const en of state.entities || []) {
    // Only show creatures in current line of sight (unless fog is off).
    if (fogEnabled && (!visibleSet || !visibleSet.has(en.x + "," + en.y)))
      continue;
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
    } else if (en.kind === "monster") {
      ctx.fillStyle = en.hostile ? "#7b4fb0" : "#3f6f74";
      ctx.beginPath();
      ctx.arc(cx, cy, TILE * 0.4, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = ctx.fillStyle;     // a few tentacles
      ctx.lineWidth = 2;
      for (let a = 0; a < 6; a++) {
        const ang = (Math.PI * 2 * a) / 6;
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(ang) * TILE * 0.35, cy + Math.sin(ang) * TILE * 0.35);
        ctx.lineTo(cx + Math.cos(ang) * TILE * 0.6, cy + Math.sin(ang) * TILE * 0.6);
        ctx.stroke();
      }
      ctx.fillStyle = "#f0e8ff";
      ctx.font = "bold 10px monospace";
      ctx.textAlign = "center";
      ctx.fillText(en.name[0], cx, cy + 3);
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

  // ---- fog of war: visible (LOS) tiles are clear; explored dims; rest hidden.
  if (explored && self && fogEnabled) {
    for (let y = y0; y < y1; y++) {
      for (let x = x0; x < x1; x++) {
        if (visibleSet && visibleSet.has(x + "," + y)) continue; // in sight
        ctx.fillStyle = explored[y][x] ? "rgba(8,9,14,0.55)" : "rgba(8,9,14,1)";
        ctx.fillRect(offX + x * TILE, offY + y * TILE, TILE, TILE);
      }
    }
  }

  renderMinimap();
  drawScaleBar();
}

// A distance scale bar in the bottom-right, in km and miles.
function drawScaleBar() {
  if (!kmPerTile) return;
  const nice = [5, 10, 20, 50, 100, 200, 500, 1000];
  let km = nice[0];
  for (const k of nice) if ((k / kmPerTile) * TILE <= 170) km = k;
  const w = (km / kmPerTile) * TILE;
  const x = canvas.width - w - 24, y = canvas.height - 26;
  ctx.fillStyle = "rgba(0,0,0,0.45)";
  ctx.fillRect(x - 8, y - 22, w + 16, 30);
  ctx.strokeStyle = "#fff"; ctx.fillStyle = "#fff"; ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(x, y - 5); ctx.lineTo(x, y); ctx.lineTo(x + w, y); ctx.lineTo(x + w, y - 5);
  ctx.stroke();
  ctx.font = "12px monospace"; ctx.textAlign = "center";
  ctx.fillText(`${km} km · ${Math.round(km * 0.621)} mi`, x + w / 2, y - 9);
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
  // Fog of war: black out the parts of the world you haven't explored.
  if (fogCanvas && fogEnabled) m.drawImage(fogCanvas, 0, 0, MW, MH);

  // Current viewport rectangle (reflects the camera, which may be panned).
  const tilesW = canvas.width / TILE, tilesH = canvas.height / TILE;
  m.strokeStyle = "rgba(255,255,255,0.85)";
  m.lineWidth = 1;
  m.strokeRect(
    (camera.x - tilesW / 2) / mapW * MW, (camera.y - tilesH / 2) / mapH * MH,
    tilesW / mapW * MW, tilesH / mapH * MH,
  );

  const seen = (x, y) => !fogEnabled || (explored && explored[y] && explored[y][x]);

  // Ancient sites — gold pips, but only once you've discovered them.
  for (const lm of landmarks) {
    if (!seen(lm.x, lm.y)) continue;
    m.fillStyle = "#ffd86b";
    m.fillRect(lm.x / mapW * MW - 1, lm.y / mapH * MH - 1, 2.5, 2.5);
  }

  // Cities you've discovered — size/brightness by stage.
  if (state) {
    for (const c of state.cities || []) {
      if (!seen(c.x, c.y) || c.stage <= 0) continue;
      const sz = 1 + c.stage * 0.6;
      m.fillStyle = c.stage >= 3 ? "#fff2cf" : "#d8b87a";
      m.fillRect(c.x / mapW * MW - sz / 2, c.y / mapH * MH - sz / 2, sz, sz);
    }
  }

  // Players — yourself always; others only where you've explored.
  if (state) {
    for (const p of state.players) {
      if (p.pid !== myPid && !seen(p.x, p.y)) continue;
      m.fillStyle = p.pid === myPid ? "#ffd35c" : "#e06b6b";
      m.fillRect(p.x / mapW * MW - 1.5, p.y / mapH * MH - 1.5, 3, 3);
    }
  }
}

function fmtYear(y) {
  if (y === undefined || y === null) return "";
  return y < 0 ? `${-y} BC` : `${y} AD`;
}

function updateHud() {
  if (!state) return;
  document.getElementById("era").textContent =
    `· ${state.era} age · ${fmtYear(state.year)}`;
  document.getElementById("clock").textContent = `tick ${state.tick}`;
  const self = me();
  const inv = self ? self.inventory : {};
  const parts = Object.entries(inv).map(([k, v]) => `${k}: ${v}`);
  document.getElementById("inv").textContent =
    parts.length ? parts.join("  ·  ") : "(empty pack)";
  const lore = self ? (self.lore || 0) : 0;
  const hp = self ? Math.max(0, self.hp) : 0, maxhp = self ? self.max_hp : 100;
  const coin = self ? (self.coin || 0) : 0;
  const pct = maxhp ? hp / maxhp : 0;
  const fill = document.getElementById("hpfill");
  fill.style.width = `${pct * 100}%`;
  fill.style.background = pct > 0.6 ? "#4caf50" : pct > 0.3 ? "#d4a017" : "#d9433a";
  document.getElementById("hptext").textContent = `♥ ${hp}/${maxhp}`;
  document.getElementById("vitals").textContent = `${coin} coin · renown ${lore}`;
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
    const hostile = en.kind === "brigand" || en.kind === "monster";
    sendAction({ action: hostile ? "attack" : "interact" });
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

// ---- keyboard: heading-based movement, run (Shift), and actions ------------
const DIRS = {
  w: [0, -1], ArrowUp: [0, -1], s: [0, 1], ArrowDown: [0, 1],
  a: [-1, 0], ArrowLeft: [-1, 0], d: [1, 0], ArrowRight: [1, 0],
};
let heldDirs = [];            // stack of held direction keys; last wins
let sentHeading = [0, 0];

function pushHeading() {
  const dir = heldDirs.length ? DIRS[heldDirs[heldDirs.length - 1]] : [0, 0];
  if (dir[0] !== sentHeading[0] || dir[1] !== sentHeading[1]) {
    sentHeading = dir;
    sendAction({ action: "move", dx: dir[0], dy: dir[1] });
  }
}

window.addEventListener("keydown", (e) => {
  if (e.target && /^(INPUT|TEXTAREA)$/.test(e.target.tagName)) return;
  const k = e.key;
  if (k in DIRS) {
    e.preventDefault();
    if (!e.repeat) {
      if (!heldDirs.includes(k)) heldDirs.push(k);
      closeDialogue();
      followPlayer = true;
      pushHeading();
    }
    return;
  }
  if (e.repeat) return;
  switch (k) {
    case "Shift": sendAction({ action: "run", on: true }); break;
    case " ": sendAction({ action: "gather" }); e.preventDefault(); break;
    case "b": case "B": toggleBuildMenu(); e.preventDefault(); break;
    case "e": case "E": sendAction({ action: "dig" }); break;
    case "f": case "F": sendAction({ action: "interact" }); break;
    case "r": case "R": sendAction({ action: "attack" }); break;
    case "i": case "I": toggleRelics(); break;
    case "o": case "O": toggleFog(); break;
    case "Escape": closeDialogue(); break;
  }
});

window.addEventListener("keyup", (e) => {
  const k = e.key;
  if (k in DIRS) {
    heldDirs = heldDirs.filter((d) => d !== k);
    pushHeading();
  } else if (k === "Shift") {
    sendAction({ action: "run", on: false });
  }
});

function toggleFog() {
  fogEnabled = !fogEnabled;
  const btn = document.getElementById("fogBtn");
  if (btn) { btn.textContent = `Fog: ${fogEnabled ? "on" : "off"} (O)`; btn.classList.toggle("off", !fogEnabled); }
  if (state) draw();
}

// ---- relic inventory: click a relic to read its clue -----------------------
function toggleRelics() { relicPanelOpen ? closeRelics() : openRelics(); }
function openRelics() { closeDialogue(); relicPanelOpen = true; renderRelics(); }
function closeRelics() {
  relicPanelOpen = false;
  const el = document.getElementById("legend");
  if (!currentSite && !currentLegend && !merchantView && !buildMenuOpen) {
    el.style.opacity = "0"; el.style.pointerEvents = "none";
  }
}
let openRelicId = null;
function renderRelics() {
  const el = document.getElementById("legend");
  const rows = myRelics.map((r) => {
    const clue = openRelicId === r.id
      ? `<div class="claim-verdict ok">${esc(r.clue)}</div>` : "";
    return `<div class="claim"><div class="claim-text" style="cursor:pointer" ` +
      `onclick="toggleRelicClue(${r.id})">🏺 ${esc(r.name)} ` +
      `<span class="claim-basis">(${esc(r.source)})</span></div>${clue}</div>`;
  }).join("");
  el.innerHTML =
    `<div class="legend-title">Relics &nbsp;·&nbsp; ${myRelics.length} held</div>` +
    (myRelics.length
      ? `<div class="claims-head">Click a relic to read its clue</div><div class="claims">${rows}</div>`
      : `<div class="legend-body" style="font-style:normal">No relics yet. ` +
        `Excavate ancient sites and ruins, or take them from brigands.</div>`) +
    `<div class="legend-close" onclick="closeRelics()">✕ close</div>`;
  el.style.opacity = "1"; el.style.pointerEvents = "auto"; dialogueOpen = true;
}
function toggleRelicClue(id) {
  openRelicId = openRelicId === id ? null : id;
  renderRelics();
}

// ---- camera: deadzone while moving, ease-to-centre when stopped ------------
function updateCamera() {
  const self = me();
  if (!self) return;
  if (!cameraInit) { camera.x = self.x; camera.y = self.y; cameraInit = true; return; }
  if (!followPlayer) return;            // panned via minimap — hold position
  const now = performance.now();
  const key = self.x + "," + self.y;
  if (key !== lastPlayerTile) { lastPlayerTile = key; lastMoveAt = now; }
  const moving = (now - lastMoveAt) < 400;
  const dzX = (canvas.width * 0.25) / TILE;  // tiles from centre to deadzone edge
  const dzY = (canvas.height * 0.25) / TILE;
  if (moving) {
    // Don't scroll until the player nears a screen edge (within 25%); then the
    // camera follows just enough to keep them inside the central deadzone.
    camera.x = Math.max(self.x - dzX, Math.min(self.x + dzX, camera.x));
    camera.y = Math.max(self.y - dzY, Math.min(self.y + dzY, camera.y));
  } else {
    // Stopped: glide the camera until the player is centred again.
    camera.x += (self.x - camera.x) * 0.12;
    camera.y += (self.y - camera.y) * 0.12;
    if (Math.abs(self.x - camera.x) < 0.03) camera.x = self.x;
    if (Math.abs(self.y - camera.y) < 0.03) camera.y = self.y;
  }
}

// Continuous render loop so the camera moves smoothly between ticks.
function renderLoop() {
  updateCamera();
  draw();
  requestAnimationFrame(renderLoop);
}
requestAnimationFrame(renderLoop);

connect();
