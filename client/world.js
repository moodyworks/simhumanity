// world.js — the world-map vertical slice: walk a real, pixel-for-pixel patch of
// Earth, streamed as chunks. One source pixel = one game tile (a square).
//
// The renderer keeps the chunks covering the view (+ a 1-chunk margin = the 3x3
// ring) loaded, drawing only each chunk's visible sub-rectangle so a 480px chunk
// scaled to 480*TILE px is never blitted whole. Untiled regions stay dark (only
// the C1 quadrant — the Fertile Crescent's — is tiled so far).

const canvas = document.getElementById("view");
const ctx = canvas.getContext("2d");
const hud = document.getElementById("hud");
ctx.imageSmoothingEnabled = false; // crisp upscaled pixel-squares

let TILE = 24;          // screen px per game tile (1 source pixel) — tunable with +/-
let man = null;         // manifest (world size, chunk grid, geo bounds)
let px = 0, py = 0;     // player position in global tiles (float, for smooth motion)
let camX = 0, camY = 0, followCam = true;  // camera; the minimap "look" detaches it
let mmRect = null, mmDragging = false;     // minimap click/drag-to-look state
const chunks = new Map(); // "c_r" -> {img, loaded, missing}
const keys = new Set();
let last = 0;
const minimap = new Image(); minimap.src = "/static/minimap.jpg"; // whole-Earth overview
let spawned = false;       // movement is disabled until the player picks a spawn city
let spawnCities = [];      // cities of the age — the spawn options
let ws = null, myPid = null, others = [], lastSent = 0;  // multiplayer presence
let myInv = {}, myPlans = [], myRelics = [], structures = [], ruins = []; // inv / plans / relics / builds
let myRenown = 0;                                       // scholar's renown from site digs
const VISION = 11;                                      // fog-of-war sight radius (tiles)
let explored = new Set(), exTileX = null, exTileY = null, fogOn = true;
let debugOn = false, placingTarget = null;             // debug tools (` to toggle)
const RES_COLOR = {  // resource pip colours (shared by the map + the legend)
  wood: "#4f7a36", stone: "#9a9a9a", food: "#d9c24a", fish: "#58b0d6", ore: "#c0813f",
  herbs: "#6fae4a", mushrooms: "#b06a4a", amber: "#e0a020", game: "#c08050",
  obsidian: "#3a3a48", flint: "#7a7068", clay: "#b08868", olives: "#6a7a3a",
  grapes: "#8a5a9a", flax: "#d8d090", reeds: "#8aa060", bones: "#cab594" };
let worldYear = null, worldEra = "";                     // era clock
let npcs = [], myHp = null, myMaxHp = null;              // NPCs + combat
let cities = [], sites = [], resourceNodes = [];        // cities, sites, gatherable nodes
let moveTarget = null, stepTo = null;                   // click-to-move dest / current tile step
const terrainCache = new Map();                          // chunk -> ImageData (land/water)
let toastT = 0;
function toast(text) {
  const el = document.getElementById("log");
  el.textContent = text; el.style.opacity = "1";
  clearTimeout(toastT); toastT = setTimeout(() => { el.style.opacity = "0"; }, 2500);
}
function toggleRelics() {
  const el = document.getElementById("relics");
  const open = el.style.display !== "flex";
  if (open) document.getElementById("relicList").innerHTML = myRelics.length
    ? myRelics.map((r) => `<div class="relic"><div class="rn">${r.name}</div>` +
        `<div class="rc">${r.clue} — ${r.source}</div></div>`).join("")
    : "<p style='color:#93a3c0'>No relics yet — slay brigands and sea-beasts, or dig the past.</p>";
  el.style.display = open ? "flex" : "none";
}
function toggleLegend() {
  const el = document.getElementById("legend");
  const open = el.style.display !== "block";
  if (open) document.getElementById("legendGrid").innerHTML = Object.entries(RES_COLOR)
    .map(([k, c]) => `<div class="lrow"><span class="sw" style="background:${c}"></span>${k}</div>`).join("");
  el.style.display = open ? "block" : "none";
}
function toggleBuild() {
  const el = document.getElementById("buildMenu");
  const open = el.style.display !== "block";
  if (open) el.innerHTML = `<div class="bhdr">BUILD — click to raise it here</div>` +
    (myPlans.length ? myPlans.map((p) => {
      const cant = Object.entries(p.cost).some(([k, v]) => (myInv[k] || 0) < v);
      const cost = Object.entries(p.cost).map(([k, v]) => `${v} ${k}`).join(", ");
      return `<div class="brow${cant ? " cant" : ""}" data-t="${p.type}">` +
        `<span class="bn">${p.label}</span><span class="bcost">${cost}</span></div>`;
    }).join("") : `<div class="bcost" style="padding:6px">No plans known yet.</div>`);
  el.style.display = open ? "block" : "none";
}
document.getElementById("buildMenu").addEventListener("click", (e) => {
  const row = e.target.closest(".brow");
  if (row && ws && ws.readyState === 1) {
    ws.send(JSON.stringify({ action: "build", kind: row.dataset.t }));
    document.getElementById("buildMenu").style.display = "none";
  }
});

let sqAnswers = {};
function openSiteQuiz(m) {  // standing on a famous site → judge claims true/false
  sqAnswers = {};
  document.getElementById("sqName").textContent = "⛏ Excavating " + m.site;
  document.getElementById("sqNote").textContent = m.note || "";
  document.getElementById("sqList").innerHTML = m.questions.map((q) =>
    `<div class="sqq" data-id="${q.id}"><div class="qt">${q.text}</div>` +
    `<button data-v="1">True</button><button data-v="0">False</button></div>`).join("");
  document.getElementById("sqFoot").innerHTML =
    `<button id="sqSubmit">Submit findings</button>` +
    `<span id="sqHint">judge each claim · walk away to abandon</span>`;
  document.getElementById("siteQuiz").style.display = "flex";
}
function showSiteResult(m) {
  let html = `<div style="margin-bottom:10px;color:${m.correct === m.total ? "#6fcf6f" : "#ffd86b"}">` +
    `You judged ${m.correct}/${m.total} claims correctly.</div>`;
  html += m.bases.map((b) =>
    `<div class="sqq"><div class="qt">${b.text}</div>` +
    `<span class="${b.truth ? "good" : "bad"}">${b.truth ? "TRUE" : "FALSE"}</span>` +
    (b.correct != null
      ? ` <span class="${b.correct ? "good" : "bad"}">${b.correct ? "✓ you judged right" : "✗ you missed it"}</span>`
      : "") +
    `<div class="res">${b.basis}</div></div>`).join("");
  let reward = `Claimed the Relic of ${m.site} · renown now ${m.renown}`;
  if (m.learned) reward += ` · learned to build ${m.learned}`;
  html += `<div style="margin:12px 0 0;color:#ffe08a">${reward}</div>`;
  document.getElementById("sqList").innerHTML = html;
  document.getElementById("sqName").textContent = "⛏ " + m.site;
  document.getElementById("sqNote").textContent = "";
  document.getElementById("sqFoot").innerHTML = `<button id="sqClose">Close</button>`;
}
function closeSiteQuiz(abandon) {
  const el = document.getElementById("siteQuiz");
  if (el.style.display !== "flex") return;
  el.style.display = "none";
  if (abandon && ws && ws.readyState === 1) ws.send(JSON.stringify({ action: "site_abandon" }));
}
document.getElementById("siteQuiz").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn || !ws) return;
  if (btn.id === "sqSubmit") { ws.send(JSON.stringify({ action: "site_answer", answers: sqAnswers })); return; }
  if (btn.id === "sqClose") { closeSiteQuiz(false); return; }
  const row = btn.closest(".sqq");
  if (row && btn.dataset.v != null) {
    sqAnswers[row.dataset.id] = btn.dataset.v === "1";
    row.querySelectorAll("button").forEach((b) => b.classList.toggle("sel", b === btn));
  }
});

// truth-vs-myth quests: judge a dug ruin's legend claim by claim
let questClaims = [];
function openQuest(m) {
  questClaims = m.claims || [];
  document.getElementById("qTitle").textContent = "The legend of " + (m.builder || "one lost to time");
  renderQuest();
  document.getElementById("quest").style.display = "flex";
}
function renderQuest() {
  document.getElementById("qList").innerHTML = questClaims.map((c) => {
    let row = `<div class="sqq" data-id="${c.id}"><div class="qt">It is told ${c.text}</div>`;
    if (c.resolved) {
      const rt = c.mode === "judge" ? (c.correct ? "good" : "bad") : (c.truth ? "good" : "bad");
      row += `<span class="${c.truth ? "good" : "bad"}">${c.truth ? "TRUE" : "FALSE"}</span>` +
        `<div class="res ${rt}">${c.basis}${c.result_text ? " — " + c.result_text : ""}</div>`;
    } else if (c.mode === "hoard") {
      row += `<button class="qbtn hoard" data-act="hoard">Dig for the hoard</button>`;
    } else {
      row += `<button class="qbtn" data-act="t">True</button>` +
        `<button class="qbtn" data-act="f">Embellished</button>`;
    }
    return row + `</div>`;
  }).join("");
}
function applyVerdict(v) {
  const c = questClaims.find((q) => q.id === v.id);
  if (c) { Object.assign(c, v, { resolved: true }); renderQuest(); }
}
function closeQuest() { document.getElementById("quest").style.display = "none"; }
document.getElementById("quest").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn || !ws) return;
  if (btn.id === "qClose") { closeQuest(); return; }
  const row = btn.closest(".sqq"); if (!row) return;
  const id = +row.dataset.id, act = btn.dataset.act;
  if (act === "hoard") ws.send(JSON.stringify({ action: "investigate", claim: id, guess: null }));
  else if (act === "t" || act === "f")
    ws.send(JSON.stringify({ action: "investigate", claim: id, guess: act === "t" }));
});

// merchant trade: buy the merchant's wares / sell your goods for coin
function openTrade(m) {
  document.getElementById("trTitle").textContent = "🪙 " + (m.who || "Merchant");
  document.getElementById("trCoin").textContent = `You have ${m.coin} coin`;
  const row = (w, act) =>
    `<div class="trrow"><span>${w.item}</span>` +
    `<button data-act="${act}" data-item="${w.item}">${act} · ${w.price}</button></div>`;
  document.getElementById("trBuy").innerHTML = m.buy.length
    ? m.buy.map((w) => row(w, "buy")).join("") : "<div class='sub'>nothing for sale</div>";
  document.getElementById("trSell").innerHTML = m.sell.length
    ? m.sell.map((w) => row(w, "sell")).join("") : "<div class='sub'>nothing to sell</div>";
  document.getElementById("trade").style.display = "flex";
}
function closeTrade() { document.getElementById("trade").style.display = "none"; }
document.getElementById("trade").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn || !ws) return;
  if (btn.id === "trClose") { closeTrade(); return; }
  if (btn.dataset.act) ws.send(JSON.stringify({ action: btn.dataset.act, item: btn.dataset.item }));
});

// --- debug tools: jump the world clock + relocate cities/sites --------------
function toggleDebug() {
  debugOn = !debugOn;
  document.getElementById("debugBar").style.display = debugOn ? "block" : "none";
  if (debugOn) populatePlaceSelect(); else placingTarget = null;
  toast(debugOn ? "Debug ON — click teleports; pick a place to relocate it" : "Debug off");
}
function populatePlaceSelect() {
  const items = [];
  for (const c of cities) items.push(["city", c.name]);
  for (const s of sites) items.push(["site", s.name]);
  items.sort((a, b) => a[1].localeCompare(b[1]));
  document.getElementById("placeSelect").innerHTML =
    `<option value="">— relocate a city/site —</option>` +
    items.map(([k, n]) => `<option value="${k}:${n}">${n} (${k})</option>`).join("");
}
document.getElementById("placeSelect").addEventListener("change", (e) => {
  const v = e.target.value;
  if (!v) { placingTarget = null; return; }
  const i = v.indexOf(":");
  placingTarget = { kind: v.slice(0, i), name: v.slice(i + 1) };
  toast(`Click the map to place ${placingTarget.name}`);
});
document.getElementById("dbgYear").addEventListener("click", () => {
  const input = prompt("Debug — jump to year (negative = BC, e.g. -9000 or 1500):", worldYear);
  if (input === null) return;
  const y = parseInt(input, 10);
  if (!Number.isNaN(y) && ws && ws.readyState === 1)
    ws.send(JSON.stringify({ action: "set_year", year: y }));
});

function resize() { canvas.width = innerWidth; canvas.height = innerHeight; }
addEventListener("resize", resize); resize();

function lonlatToTile(lon, lat) {
  const b = man.bounds;
  return [(lon - b.lon_w) / (b.lon_e - b.lon_w) * man.src_w,
          (b.lat_n - lat) / (b.lat_n - b.lat_s) * man.src_h];
}
function tileToLonLat(tx, ty) {
  const b = man.bounds;
  return [b.lon_w + tx / man.src_w * (b.lon_e - b.lon_w),
          b.lat_n - ty / man.src_h * (b.lat_n - b.lat_s)];
}

function ensureChunk(c, r) {
  if (r < 0 || r >= man.rows) return;             // poles are a wall (N/S)
  c = ((c % man.cols) + man.cols) % man.cols;      // wrap around the globe (E/W)
  const k = c + "_" + r;
  if (chunks.has(k)) return;
  const img = new Image();
  const e = { img, loaded: false, missing: false };
  img.onload = () => { e.loaded = true; };
  img.onerror = () => { e.missing = true; }; // region not tiled yet
  img.src = `/tiles/c${c}_r${r}.${man.ext}?v=${man.version}`;
  chunks.set(k, e);
}

function chunkData(ac, r) {  // cache a chunk's pixels so we can read land/water
  const k = ac + "_" + r;
  if (terrainCache.has(k)) return terrainCache.get(k);
  const e = chunks.get(k);
  if (!e || !e.loaded) return null;
  const cv = document.createElement("canvas");
  cv.width = e.img.width; cv.height = e.img.height;
  const cx = cv.getContext("2d", { willReadFrequently: true });
  cx.drawImage(e.img, 0, 0);
  const d = cx.getImageData(0, 0, cv.width, cv.height);
  terrainCache.set(k, d);
  return d;
}

function isWaterTile(tx, ty) {  // water == the rendered sea-blue (matches the map)
  if (!man) return false;
  const cp = man.chunk_px, r = Math.floor(ty / cp);
  if (r < 0 || r >= man.rows) return false;
  const ac = ((Math.floor(tx / cp) % man.cols) + man.cols) % man.cols;
  const d = chunkData(ac, r);
  if (!d) return false;  // not loaded yet — don't block
  const lx = ((Math.floor(tx) % cp) + cp) % cp, ly = Math.floor(ty) - r * cp;
  if (lx >= d.width || ly >= d.height) return false;
  const i = (ly * d.width + lx) * 4, R = d.data[i], G = d.data[i + 1], B = d.data[i + 2];
  return B > R + 20 && B > G && B > 100;
}

function pickWalkStep(cx, cy, sdx, sdy) {  // adjacent walkable tile (diagonal, then axes)
  const boat = (myInv.boat || 0) > 0;
  for (const [ax, ay] of [[sdx, sdy], [sdx, 0], [0, sdy]]) {
    if (!ax && !ay) continue;
    const tx = ((cx + ax) % man.src_w + man.src_w) % man.src_w, ty = cy + ay;
    if (ty < 0 || ty >= man.src_h) continue;
    if (boat || !isWaterTile(tx + 0.5, ty + 0.5)) return { tx, ty };
  }
  return null;
}

// --- fog of war: you see a radius around you; explored ground stays dimly known ---
function markExplored() {
  if (!man || !spawned) return;
  const cx = Math.floor(px), cy = Math.floor(py);
  if (cx === exTileX && cy === exTileY) return;  // only when you cross into a new tile
  exTileX = cx; exTileY = cy;
  for (let dy = -VISION; dy <= VISION; dy++) {
    const wy = cy + dy; if (wy < 0 || wy >= man.src_h) continue;
    for (let dx = -VISION; dx <= VISION; dx++) {
      if (dx * dx + dy * dy > VISION * VISION) continue;
      explored.add((((cx + dx) % man.src_w) + man.src_w) % man.src_w + "," + wy);
    }
  }
}
function inVision(wx, wy) {
  let ddx = wx - px;
  if (ddx > man.src_w / 2) ddx -= man.src_w; else if (ddx < -man.src_w / 2) ddx += man.src_w;
  return ddx * ddx + (wy - py) * (wy - py) <= VISION * VISION;
}
const fogActive = () => fogOn && spawned;
function hideDyn(wx, wy) { return fogActive() && !inVision(wx, wy); }  // mobs/items: sight only
function hideStat(wx, wy) {  // landmarks: sight or remembered
  return fogActive() && !inVision(wx, wy) &&
    !explored.has(Math.floor(wx) + "," + Math.floor(wy));
}
function drawFog(offX, offY) {
  // Scan in fixed screen blocks (coarser when zoomed out) so fog is bounded and
  // still drawn at any zoom level — sampling the world tile at each block centre.
  if (!fogActive()) return;
  const B = Math.max(14, TILE);
  for (let sy = 0; sy < canvas.height; sy += B) {
    for (let sx = 0; sx < canvas.width; sx += B) {
      const wxf = (sx + B / 2 - offX) / TILE, wyf = (sy + B / 2 - offY) / TILE;
      let dark;
      if (wyf < 0 || wyf >= man.src_h) {
        dark = "rgba(3,5,11,0.97)";  // beyond the poles
      } else {
        let ddx = wxf - px;
        if (ddx > man.src_w / 2) ddx -= man.src_w; else if (ddx < -man.src_w / 2) ddx += man.src_w;
        if (ddx * ddx + (wyf - py) * (wyf - py) <= VISION * VISION) continue;  // in sight
        const twx = ((Math.floor(wxf) % man.src_w) + man.src_w) % man.src_w;
        dark = explored.has(twx + "," + Math.floor(wyf)) ? "rgba(3,5,11,0.5)" : "rgba(3,5,11,0.96)";
      }
      ctx.fillStyle = dark;
      ctx.fillRect(sx, sy, B + 1, B + 1);
    }
  }
}

function viewRect() { // visible area in global tiles (centred on the camera)
  const hw = canvas.width / 2 / TILE, hh = canvas.height / 2 / TILE;
  return { x0: camX - hw, y0: camY - hh, x1: camX + hw, y1: camY + hh };
}

function update(dt) {
  if (spawned) {  // grid-locked: step tile-to-tile, always landing on a tile centre
    const onWater = (myInv.boat || 0) > 0 && isWaterTile(px, py);
    let budget = 14 * (keys.has("shift") ? 60 : 1) * (onWater ? 0.5 : 1) * dt; // tiles this frame
    let wdx = 0, wdy = 0;
    if (keys.has("w") || keys.has("arrowup")) wdy -= 1;
    if (keys.has("s") || keys.has("arrowdown")) wdy += 1;
    if (keys.has("a") || keys.has("arrowleft")) wdx -= 1;
    if (keys.has("d") || keys.has("arrowright")) wdx += 1;
    if (wdx || wdy) moveTarget = null;  // WASD cancels click-to-move
    let guard = 0;
    while (budget > 1e-6 && guard++ < 400) {
      if (!stepTo) {  // at a tile centre — choose the next tile to step onto
        const cx = Math.floor(px), cy = Math.floor(py);
        let sdx = wdx, sdy = wdy;
        if (!sdx && !sdy && moveTarget) {
          const mtx = Math.floor(moveTarget.x), mty = Math.floor(moveTarget.y);
          let ddx = mtx - cx;
          if (ddx > man.src_w / 2) ddx -= man.src_w; else if (ddx < -man.src_w / 2) ddx += man.src_w;
          if (!ddx && mty === cy) moveTarget = null;
          else { sdx = Math.sign(ddx); sdy = Math.sign(mty - cy); }
        }
        const s = (sdx || sdy) ? pickWalkStep(cx, cy, sdx, sdy) : null;
        if (!s) { if (moveTarget) moveTarget = null; break; }
        stepTo = { x: s.tx + 0.5, y: s.ty + 0.5 };
        followCam = true;
      }
      let tdx = stepTo.x - px, tdy = stepTo.y - py;
      if (tdx > man.src_w / 2) tdx -= man.src_w; else if (tdx < -man.src_w / 2) tdx += man.src_w;
      const dist = Math.hypot(tdx, tdy);
      if (dist <= budget) {
        px = ((stepTo.x % man.src_w) + man.src_w) % man.src_w; py = stepTo.y;
        budget -= dist; stepTo = null;
      } else {
        px = ((px + tdx / dist * budget) % man.src_w + man.src_w) % man.src_w;
        py += tdy / dist * budget; budget = 0;
      }
    }
  }
  if (followCam) { camX = px; camY = py; }  // else the minimap "look" holds the camera
  if (spawned && ws && ws.readyState === 1 && performance.now() - lastSent > 150) {
    ws.send(JSON.stringify({ action: "move", x: Math.round(px * 10) / 10, y: Math.round(py * 10) / 10 }));
    lastSent = performance.now();
  }
  // load the chunks covering the view plus a one-chunk margin
  const v = viewRect(), cp = man.chunk_px;
  for (let r = Math.floor(v.y0 / cp) - 1; r <= Math.floor(v.y1 / cp) + 1; r++)
    for (let c = Math.floor(v.x0 / cp) - 1; c <= Math.floor(v.x1 / cp) + 1; c++)
      ensureChunk(c, r);
}

function render() {
  ctx.fillStyle = "#05070d";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  const cp = man.chunk_px;
  // snap the camera to whole pixels so adjacent chunks share an exact edge
  // (fractional offsets leave hairline seams between tiles).
  const offX = Math.round(canvas.width / 2 - camX * TILE), offY = Math.round(canvas.height / 2 - camY * TILE);
  markExplored();
  const v = viewRect();
  for (let r = Math.floor(v.y0 / cp); r <= Math.floor(v.y1 / cp); r++) {
    if (r < 0 || r >= man.rows) continue;                   // beyond the poles = void
    for (let c = Math.floor(v.x0 / cp); c <= Math.floor(v.x1 / cp); c++) {
      const ac = ((c % man.cols) + man.cols) % man.cols;    // wrapped actual column
      const e = chunks.get(ac + "_" + r);
      if (!e || !e.loaded) continue;
      const gx = c * cp, gy = r * cp;     // virtual origin (drawn position) — wraps seamlessly
      const ix0 = Math.max(gx, Math.floor(v.x0)), iy0 = Math.max(gy, Math.floor(v.y0));
      const ix1 = Math.min(gx + cp, Math.ceil(v.x1)), iy1 = Math.min(gy + cp, Math.ceil(v.y1));
      if (ix1 <= ix0 || iy1 <= iy0) continue;
      ctx.drawImage(e.img, ix0 - gx, iy0 - gy, ix1 - ix0, iy1 - iy0,
                    offX + ix0 * TILE, offY + iy0 * TILE, (ix1 - ix0) * TILE, (iy1 - iy0) * TILE);
    }
  }
  drawFog(offX, offY);  // darken everything beyond sight (explored ground stays dim)
  // resource nodes (gatherable) — small coloured pips; explored ones stay shown
  // (so a zoomed-out view loads the whole resource spread, not just what's in sight)
  for (const nd of resourceNodes) {
    if (hideStat(nd.x, nd.y)) continue;
    let ox = nd.x; const d = ox - camX;
    if (d > man.src_w / 2) ox -= man.src_w; else if (d < -man.src_w / 2) ox += man.src_w;
    const sx = offX + ox * TILE, sy = offY + nd.y * TILE;
    if (sx < -TILE || sy < -TILE || sx > canvas.width || sy > canvas.height) continue;
    ctx.fillStyle = RES_COLOR[nd.kind] || "#9c7";
    ctx.beginPath(); ctx.arc(sx, sy, Math.max(2, TILE * 0.3), 0, 7); ctx.fill();
    ctx.strokeStyle = "rgba(0,0,0,0.5)"; ctx.lineWidth = 1; ctx.stroke();
  }

  // ancient sites (date-gated) — gold diamonds
  for (const s of sites) {
    if (hideStat(s.x, s.y)) continue;
    let ox = s.x; const dd = ox - camX;
    if (dd > man.src_w / 2) ox -= man.src_w; else if (dd < -man.src_w / 2) ox += man.src_w;
    const sx = offX + ox * TILE, sy = offY + s.y * TILE, r = TILE * 0.6;
    if (sx < -60 || sy < -60 || sx > canvas.width + 60 || sy > canvas.height + 60) continue;
    ctx.fillStyle = "#ffe08a"; ctx.strokeStyle = "#000"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(sx, sy - r); ctx.lineTo(sx + r, sy);
    ctx.lineTo(sx, sy + r); ctx.lineTo(sx - r, sy); ctx.closePath(); ctx.fill(); ctx.stroke();
    ctx.fillStyle = "#ffe08a"; ctx.font = "11px ui-monospace, monospace"; ctx.textAlign = "center";
    ctx.fillText(s.name, sx, sy - r - 3); ctx.textAlign = "left";
  }
  // historical cities, sized by their current era stage
  const sizeName = ["", "hamlet", "town", "city", "metropolis"];
  for (const c of cities) {
    if (hideStat(c.x, c.y)) continue;
    let ox = c.x; const dd = ox - camX;
    if (dd > man.src_w / 2) ox -= man.src_w; else if (dd < -man.src_w / 2) ox += man.src_w;
    const sx = offX + ox * TILE, sy = offY + c.y * TILE;
    if (sx < -80 || sy < -80 || sx > canvas.width + 80 || sy > canvas.height + 80) continue;
    const rad = TILE * (0.45 + 0.22 * c.stage);
    ctx.fillStyle = "#e8d3a0"; ctx.strokeStyle = "#5a4424"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(sx, sy, rad, 0, 7); ctx.fill(); ctx.stroke();
    ctx.fillStyle = "#fff"; ctx.font = "12px ui-monospace, monospace"; ctx.textAlign = "center";
    ctx.fillText(`${c.name} · ${sizeName[c.stage]}`, sx, sy - rad - 4); ctx.textAlign = "left";
  }

  // built structures
  for (const s of structures) {
    if (hideStat(s.x, s.y)) continue;
    let ox = s.x; const d = ox - camX;
    if (d > man.src_w / 2) ox -= man.src_w; else if (d < -man.src_w / 2) ox += man.src_w;
    const sx = offX + ox * TILE, sy = offY + s.y * TILE;
    if (sx < -TILE || sy < -TILE || sx > canvas.width || sy > canvas.height) continue;
    ctx.fillStyle = "#caa472"; ctx.fillRect(sx - TILE / 2, sy - TILE / 2, TILE, TILE);
    ctx.strokeStyle = "#3a2d18"; ctx.lineWidth = 2; ctx.strokeRect(sx - TILE / 2, sy - TILE / 2, TILE, TILE);
  }

  // ruins (decayed past-era structures — dig sites; press E on one)
  for (const s of ruins) {
    if (hideStat(s.x, s.y)) continue;
    let ox = s.x; const d = ox - camX;
    if (d > man.src_w / 2) ox -= man.src_w; else if (d < -man.src_w / 2) ox += man.src_w;
    const sx = offX + ox * TILE, sy = offY + s.y * TILE;
    if (sx < -TILE || sy < -TILE || sx > canvas.width || sy > canvas.height) continue;
    ctx.fillStyle = "#5a4a33"; ctx.fillRect(sx - TILE / 2, sy - TILE / 2, TILE, TILE);
    ctx.strokeStyle = "#241c10"; ctx.lineWidth = 1; ctx.strokeRect(sx - TILE / 2, sy - TILE / 2, TILE, TILE);
  }

  // NPCs (wander / hunt around you) — name, HP bar, hostile outline
  const npcColor = { wanderer: "#9aa3b0", merchant: "#6fd0c8", brigand: "#e08a3a",
    monster: "#d24a6a", eagle: "#e8e2c0", roc: "#b06ad0" };
  for (const n of npcs) {
    if (hideDyn(n.x, n.y)) continue;
    let ox = n.x; const d = ox - camX;
    if (d > man.src_w / 2) ox -= man.src_w; else if (d < -man.src_w / 2) ox += man.src_w;
    const sx = offX + ox * TILE, sy = offY + n.y * TILE, rr = TILE * 0.42;
    if (sx < -TILE * 2 || sy < -TILE * 2 || sx > canvas.width + TILE || sy > canvas.height + TILE) continue;
    if (debugOn) {  // engage radius (mobs, red) or talk radius (friendlies, cyan)
      const eng = n.spot > 0, talk = n.kind === "wanderer" || n.kind === "merchant";
      if (eng || talk) {
        ctx.beginPath(); ctx.arc(sx, sy, (eng ? n.spot : 3) * TILE, 0, 7);
        ctx.strokeStyle = eng ? "rgba(255,60,60,0.5)" : "rgba(90,210,210,0.55)";
        ctx.lineWidth = 1.5; ctx.setLineDash([5, 4]); ctx.stroke(); ctx.setLineDash([]);
      }
    }
    ctx.fillStyle = npcColor[n.kind] || "#aaa";
    ctx.beginPath(); ctx.arc(sx, sy, rr, 0, 7); ctx.fill();
    ctx.lineWidth = n.hostile ? 2.5 : 1; ctx.strokeStyle = n.hostile ? "#ff2a2a" : "#000"; ctx.stroke();
    if (n.hp < n.max_hp) {
      const bw = TILE * 0.9;
      ctx.fillStyle = "rgba(0,0,0,0.6)"; ctx.fillRect(sx - bw / 2, sy - rr - 6, bw, 3);
      ctx.fillStyle = "#5bd64a"; ctx.fillRect(sx - bw / 2, sy - rr - 6, bw * n.hp / n.max_hp, 3);
    }
    if (TILE >= 5) {  // always label NPCs (skip only at extreme zoom-out)
      ctx.fillStyle = n.hostile ? "#ff9a9a" : "#dfe6f0";
      ctx.font = "10px ui-monospace, monospace"; ctx.textAlign = "center";
      ctx.fillText(n.name, sx, sy - rr - 9); ctx.textAlign = "left";
    }
  }

  // other players (multiplayer presence) — drawn at the nearest wrap of their x
  for (const p of others) {
    if (hideDyn(p.x, p.y)) continue;
    let ox = p.x; const d = ox - camX;
    if (d > man.src_w / 2) ox -= man.src_w; else if (d < -man.src_w / 2) ox += man.src_w;
    const sx = offX + ox * TILE, sy = offY + p.y * TILE;
    if (sx < -50 || sy < -50 || sx > canvas.width + 50 || sy > canvas.height + 50) continue;
    ctx.fillStyle = "#ffd24a"; ctx.fillRect(sx - TILE / 2, sy - TILE / 2, TILE, TILE);
    ctx.strokeStyle = "#000"; ctx.lineWidth = 1; ctx.strokeRect(sx - TILE / 2, sy - TILE / 2, TILE, TILE);
    ctx.fillStyle = "#fff"; ctx.font = "12px ui-monospace, monospace"; ctx.textAlign = "center";
    ctx.fillText(p.name, sx, sy - TILE / 2 - 3); ctx.textAlign = "left";
  }

  // player marker (relative to the camera — centred when following, offset when looking)
  let pox = px; const pdd = pox - camX;
  if (pdd > man.src_w / 2) pox -= man.src_w; else if (pdd < -man.src_w / 2) pox += man.src_w;
  const cx = offX + pox * TILE, cy = offY + py * TILE;
  if (debugOn) {  // your field-of-view circle
    ctx.beginPath(); ctx.arc(cx, cy, VISION * TILE, 0, 7);
    ctx.strokeStyle = "rgba(255,220,120,0.5)"; ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 5]); ctx.stroke(); ctx.setLineDash([]);
  }
  ctx.fillStyle = "#ff3b3b";
  ctx.fillRect(cx - TILE / 2, cy - TILE / 2, TILE, TILE);
  ctx.strokeStyle = "#fff"; ctx.lineWidth = 2;
  ctx.strokeRect(cx - TILE / 2, cy - TILE / 2, TILE, TILE);

  // minimap (whole Earth) — orientation; toggle smoothing on for the downscale
  if (minimap.complete && minimap.width) {
    const mmW = 340, mmH = 170, mx = canvas.width - mmW - 12, my = canvas.height - mmH - 12;
    mmRect = { x: mx, y: my, w: mmW, h: mmH };  // for click/drag-to-look
    ctx.imageSmoothingEnabled = true;
    ctx.globalAlpha = 0.92; ctx.drawImage(minimap, mx, my, mmW, mmH); ctx.globalAlpha = 1;
    ctx.imageSmoothingEnabled = false;
    ctx.strokeStyle = "#39405a"; ctx.lineWidth = 1; ctx.strokeRect(mx + 0.5, my + 0.5, mmW, mmH);
    const vr = viewRect();  // viewport rectangle — what's currently on screen
    ctx.strokeStyle = "rgba(255,255,255,0.7)"; ctx.lineWidth = 1;
    ctx.strokeRect(mx + vr.x0 / man.src_w * mmW, my + vr.y0 / man.src_h * mmH,
                   Math.max(2, (vr.x1 - vr.x0) / man.src_w * mmW), Math.max(2, (vr.y1 - vr.y0) / man.src_h * mmH));
    for (const c of spawnCities) {  // cities of the age
      const t = lonlatToTile(c.lon, c.lat);
      ctx.fillStyle = "#7fd6ff";
      ctx.fillRect(mx + t[0] / man.src_w * mmW - 1.5, my + t[1] / man.src_h * mmH - 1.5, 3, 3);
    }
    for (const p of others) {  // other players
      ctx.fillStyle = "#ffd24a";
      ctx.fillRect(mx + p.x / man.src_w * mmW - 1.5, my + p.y / man.src_h * mmH - 1.5, 3, 3);
    }
    for (const n of npcs) {  // hostiles stand out
      ctx.fillStyle = (n.kind === "brigand" || n.kind === "monster") ? "#e0563a" : "#8a93a0";
      ctx.fillRect(mx + n.x / man.src_w * mmW - 1, my + n.y / man.src_h * mmH - 1, 2, 2);
    }
    for (const c of cities) {  // civilization on the overview
      ctx.fillStyle = "#e8d3a0";
      ctx.fillRect(mx + c.x / man.src_w * mmW - 1, my + c.y / man.src_h * mmH - 1, 2, 2);
    }
    const dx = mx + px / man.src_w * mmW, dy = my + py / man.src_h * mmH;
    ctx.fillStyle = "#ff3b3b"; ctx.beginPath(); ctx.arc(dx, dy, 3.5, 0, 7); ctx.fill();
    ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.5; ctx.stroke();
  }

  // --- HUD (structured elements; graphical HP bar) ---
  const [lon, lat] = tileToLonLat(px, py);
  const era = document.getElementById("era");
  if (worldYear == null) era.textContent = "real Earth";
  else era.innerHTML = `${worldEra} age · <b>${worldYear < 0 ? -worldYear + " BC" : worldYear + " AD"}</b>` +
    (spawned ? `   online ${others.length + 1}` : "");
  document.getElementById("pos").textContent =
    `lat ${lat.toFixed(2)}  lon ${lon.toFixed(2)}  ·  tile ${px | 0},${py | 0}`;
  if (debugOn) document.getElementById("dbgYear").textContent =
    `year ${worldYear} (click to jump)`;
  const bar = document.getElementById("hpbar");
  if (spawned && myHp != null) {
    bar.style.display = "";
    const pct = Math.max(0, Math.min(1, myHp / myMaxHp));
    const fill = document.getElementById("hpfill");
    fill.style.width = pct * 100 + "%";
    fill.style.background = pct > 0.6 ? "#4caf50" : pct > 0.3 ? "#d4a017" : "#d9433a";
    document.getElementById("hptext").textContent = `♥ ${myHp}/${myMaxHp}`;
  } else bar.style.display = "none";
  document.getElementById("vitals").textContent = spawned
    ? `${myInv.coin || 0} coin · ${myRenown} renown` : "";
  const items = Object.entries(myInv).filter(([k]) => k !== "coin").map(([k, v]) => `${k} ${v}`);
  document.getElementById("inv").textContent = spawned ? (items.join(" · ") || "(empty pack)") : "";
  document.getElementById("builds").innerHTML = spawned && myPlans.length ?
    "build " + myPlans.map((p, i) => `<b>${i + 1}</b>:${p.label}`).join("  ") : "";
}

function frame(t) {
  const dt = Math.min(0.05, (t - last) / 1000 || 0); last = t;
  update(dt); render();
  requestAnimationFrame(frame);
}

addEventListener("keydown", (e) => {
  const k = e.key.toLowerCase();
  if (k === "shift") keys.add("shift"); else keys.add(k);
  if (k === "+" || k === "=") TILE = Math.min(64, TILE + 4);
  if (k === "-" || k === "_") TILE = Math.max(2, TILE - 4);
  if (k === "`") toggleDebug();  // debug tools: year-jump / teleport / place
  if (["w", "a", "s", "d", "arrowup", "arrowdown", "arrowleft", "arrowright"].includes(k)) {
    closeSiteQuiz(true);  // walking away abandons an open excavation
    closeQuest(); closeTrade();  // (these persist — just close the panel)
  }
  if (!spawned || !ws || ws.readyState !== 1) return;
  if (k === "g" || k === " ") ws.send(JSON.stringify({ action: "gather" }));
  if (k === "e") ws.send(JSON.stringify({ action: "dig" }));
  if (k === "r") ws.send(JSON.stringify({ action: "attack" }));
  if (k === "f") ws.send(JSON.stringify({ action: "talk" }));
  if (k === "i") toggleRelics();
  if (k === "b") toggleBuild();
  if (k === "o") fogOn = !fogOn;  // toggle fog of war
  if (k === "l") toggleLegend();  // resource-colour legend
  if (/^[1-9]$/.test(k) && myPlans[+k - 1])   // 1..N build the plans you know
    ws.send(JSON.stringify({ action: "build", kind: myPlans[+k - 1].type }));
});
addEventListener("keyup", (e) => keys.delete(e.key.toLowerCase()));

// Minimap "look": click/drag the overview to pan the camera (detached from you
// until you move) — mirrors the original game's minimap-to-look.
function minimapLook(clientX, clientY) {
  if (!mmRect || !man) return false;
  const lx = clientX - mmRect.x, ly = clientY - mmRect.y;
  if (lx < 0 || ly < 0 || lx > mmRect.w || ly > mmRect.h) return false;
  camX = lx / mmRect.w * man.src_w;
  camY = ly / mmRect.h * man.src_h;
  followCam = false;
  return true;
}
canvas.addEventListener("mousedown", (e) => {
  if (minimapLook(e.clientX, e.clientY)) { mmDragging = true; return; }
  if (!spawned || !man) return;
  const offX = canvas.width / 2 - camX * TILE, offY = canvas.height / 2 - camY * TILE;
  const tx = (Math.floor((e.clientX - offX) / TILE) % man.src_w + man.src_w) % man.src_w;
  const ty = Math.max(0, Math.min(man.src_h - 1, Math.floor((e.clientY - offY) / TILE)));
  if (debugOn && placingTarget && ws && ws.readyState === 1) {  // place the picked landmark
    ws.send(JSON.stringify({ action: "move_place", kind: placingTarget.kind,
      name: placingTarget.name, x: tx, y: ty }));
    placingTarget = null; document.getElementById("placeSelect").value = "";
    return;
  }
  if (debugOn && e.shiftKey) {  // shift-click teleports (debug only)
    px = tx + 0.5; py = ty + 0.5; camX = px; camY = py; stepTo = null; moveTarget = null;
    followCam = true;
    if (ws && ws.readyState === 1)
      ws.send(JSON.stringify({ action: "move", x: Math.round(px * 10) / 10, y: Math.round(py * 10) / 10 }));
    return;
  }
  closeSiteQuiz(true); closeQuest(); closeTrade();  // plain click always walks
  moveTarget = { x: tx + 0.5, y: ty + 0.5 };
  followCam = true;
});
addEventListener("mousemove", (e) => { if (mmDragging) minimapLook(e.clientX, e.clientY); });
addEventListener("mouseup", () => { mmDragging = false; });

function spawnAt(c) {
  if (c.x != null) { px = c.x + 0.5; py = c.y + 0.5; }  // snapped solid-land tile
  else { [px, py] = lonlatToTile(c.lon, c.lat); px = Math.floor(px) + 0.5; py = Math.floor(py) + 0.5; }
  spawned = true;
  document.getElementById("spawn").style.display = "none";
  connectWorld(c.name);
}

function connectWorld(city) {  // multiplayer presence over /world/ws
  const proto = location.protocol === "https:" ? "wss://" : "ws://";
  ws = new WebSocket(proto + location.host + "/world/ws");
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "welcome") {
      myPid = m.pid;
      ws.send(JSON.stringify({ action: "spawn", x: Math.floor(px) + 0.5, y: Math.floor(py) + 0.5, city }));
    } else if (m.type === "presence") {
      const ps = m.players || [];
      others = ps.filter((p) => p.pid !== myPid);
      const me = ps.find((p) => p.pid === myPid);
      if (me) { myHp = me.hp; myMaxHp = me.max_hp; }
      structures = m.structures || [];
      ruins = m.ruins || [];
      npcs = m.npcs || [];
      cities = m.cities || [];
      sites = m.sites || [];
      worldYear = m.year; worldEra = m.era;
    } else if (m.type === "resources") {  // deterministic field, sent only on change
      resourceNodes = m.resources || [];
    } else if (m.type === "inv") {
      myInv = m.inv || {};
      if (m.plans) myPlans = m.plans;
      if (m.relics) myRelics = m.relics;
      if (m.renown != null) myRenown = m.renown;
      if (m.hp != null) { myHp = m.hp; myMaxHp = m.max_hp; }
    } else if (m.type === "respawn") {
      px = m.x; py = m.y; myHp = m.hp; toast("You were slain — back to your city.");
    } else if (m.type === "site_quiz") {
      openSiteQuiz(m);
    } else if (m.type === "site_result") {
      showSiteResult(m);
    } else if (m.type === "quest") {
      openQuest(m);
    } else if (m.type === "verdict") {
      applyVerdict(m);
    } else if (m.type === "trade") {
      openTrade(m);
    } else if (m.type === "log") {
      toast(m.text);
    }
  };
  ws.onclose = () => { ws = null; };
}

function loadSpawns() {  // cities of the age — pick one to begin
  fetch("/world/spawns").then((r) => r.json()).then((d) => {
    spawnCities = d.spawns || [];
    if (spawnCities.length) {
      const c = spawnCities[0];
      if (c.x != null) { px = c.x + 0.5; py = c.y + 0.5; }
      else { [px, py] = lonlatToTile(c.lon, c.lat); px = Math.floor(px) + 0.5; py = Math.floor(py) + 0.5; }
    }
    const era = d.year < 0 ? `${-d.year} BC` : `${d.year} AD`;
    document.getElementById("spawnEra").textContent = `Cities of the age — ${era}`;
    const list = document.getElementById("spawnList");
    const size = ["", "hamlet", "town", "city", "metropolis"];
    list.innerHTML = "";
    for (const c of spawnCities) {
      const ns = c.lat >= 0 ? "N" : "S", ew = c.lon >= 0 ? "E" : "W";
      const b = document.createElement("button");
      b.innerHTML = `<span class="nm">${c.name}</span><span class="meta">` +
        `${size[c.stage]} · ${Math.abs(c.lat).toFixed(1)}°${ns} ${Math.abs(c.lon).toFixed(1)}°${ew}</span>`;
      b.onclick = () => spawnAt(c);
      list.appendChild(b);
    }
    if (spawnCities.length) document.getElementById("spawn").style.display = "flex";
  }).catch(() => {});
}

fetch("/tiles/manifest.json", { cache: "no-store" }).then((r) => r.json()).then((m) => {
  man = m;
  loadSpawns();
  requestAnimationFrame(frame);
}).catch(() => { hud.textContent = "no world tiles yet — run tools/tile_world.py"; });
