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
const chunks = new Map(); // "c_r" -> {img, loaded, missing}
const keys = new Set();
let last = 0;
const minimap = new Image(); minimap.src = "/static/minimap.jpg"; // whole-Earth overview
let spawned = false;       // movement is disabled until the player picks a spawn city
let spawnCities = [];      // cities of the age — the spawn options
let ws = null, myPid = null, others = [], lastSent = 0;  // multiplayer presence

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

function viewRect() { // visible area in global tiles
  const hw = canvas.width / 2 / TILE, hh = canvas.height / 2 / TILE;
  return { x0: px - hw, y0: py - hh, x1: px + hw, y1: py + hh };
}

function update(dt) {
  if (spawned) {  // movement (Shift = run); realistic scale, "game-fast" for the demo
    const sp = 14 * (keys.has("shift") ? 60 : 1) * dt; // tiles/sec (10x run)
    let dx = 0, dy = 0;
    if (keys.has("w") || keys.has("arrowup")) dy -= 1;
    if (keys.has("s") || keys.has("arrowdown")) dy += 1;
    if (keys.has("a") || keys.has("arrowleft")) dx -= 1;
    if (keys.has("d") || keys.has("arrowright")) dx += 1;
    if (dx || dy) { const m = Math.hypot(dx, dy) || 1; px += dx / m * sp; py += dy / m * sp; }
    px = ((px % man.src_w) + man.src_w) % man.src_w;  // wrap around the globe E/W
    py = Math.max(0, Math.min(man.src_h - 1, py));     // poles are a wall N/S
  }
  if (spawned && ws && ws.readyState === 1 && performance.now() - lastSent > 150) {
    ws.send(JSON.stringify({ action: "move", x: Math.round(px), y: Math.round(py) }));
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
  const offX = Math.round(canvas.width / 2 - px * TILE), offY = Math.round(canvas.height / 2 - py * TILE);
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
  // other players (multiplayer presence) — drawn at the nearest wrap of their x
  for (const p of others) {
    let ox = p.x; const d = ox - px;
    if (d > man.src_w / 2) ox -= man.src_w; else if (d < -man.src_w / 2) ox += man.src_w;
    const sx = offX + ox * TILE, sy = offY + p.y * TILE;
    if (sx < -50 || sy < -50 || sx > canvas.width + 50 || sy > canvas.height + 50) continue;
    ctx.fillStyle = "#ffd24a"; ctx.fillRect(sx - TILE / 2, sy - TILE / 2, TILE, TILE);
    ctx.strokeStyle = "#000"; ctx.lineWidth = 1; ctx.strokeRect(sx - TILE / 2, sy - TILE / 2, TILE, TILE);
    ctx.fillStyle = "#fff"; ctx.font = "12px ui-monospace, monospace"; ctx.textAlign = "center";
    ctx.fillText(p.name, sx, sy - TILE / 2 - 3); ctx.textAlign = "left";
  }

  // player marker
  const cx = canvas.width / 2, cy = canvas.height / 2;
  ctx.fillStyle = "#ff3b3b";
  ctx.fillRect(cx - TILE / 2, cy - TILE / 2, TILE, TILE);
  ctx.strokeStyle = "#fff"; ctx.lineWidth = 2;
  ctx.strokeRect(cx - TILE / 2, cy - TILE / 2, TILE, TILE);

  // minimap (whole Earth) — orientation; toggle smoothing on for the downscale
  if (minimap.complete && minimap.width) {
    const mmW = 340, mmH = 170, mx = canvas.width - mmW - 12, my = canvas.height - mmH - 12;
    ctx.imageSmoothingEnabled = true;
    ctx.globalAlpha = 0.92; ctx.drawImage(minimap, mx, my, mmW, mmH); ctx.globalAlpha = 1;
    ctx.imageSmoothingEnabled = false;
    ctx.strokeStyle = "#39405a"; ctx.lineWidth = 1; ctx.strokeRect(mx + 0.5, my + 0.5, mmW, mmH);
    for (const c of spawnCities) {  // cities of the age
      const t = lonlatToTile(c.lon, c.lat);
      ctx.fillStyle = "#7fd6ff";
      ctx.fillRect(mx + t[0] / man.src_w * mmW - 1.5, my + t[1] / man.src_h * mmH - 1.5, 3, 3);
    }
    for (const p of others) {  // other players
      ctx.fillStyle = "#ffd24a";
      ctx.fillRect(mx + p.x / man.src_w * mmW - 1.5, my + p.y / man.src_h * mmH - 1.5, 3, 3);
    }
    const dx = mx + px / man.src_w * mmW, dy = my + py / man.src_h * mmH;
    ctx.fillStyle = "#ff3b3b"; ctx.beginPath(); ctx.arc(dx, dy, 3.5, 0, 7); ctx.fill();
    ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.5; ctx.stroke();
  }

  const [lon, lat] = tileToLonLat(px, py);
  hud.innerHTML =
    `simhumanity — <b>real Earth</b>` + (spawned ? `   online <b>${others.length + 1}</b>` : ``) + `\n` +
    `lat ${lat.toFixed(2)}  lon ${lon.toFixed(2)}   tile ${px | 0},${py | 0}\n` +
    `zoom <b>${TILE}</b> px/tile   <b>WASD</b> move · <b>Shift</b> run · <b>+/-</b> zoom`;
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
});
addEventListener("keyup", (e) => keys.delete(e.key.toLowerCase()));

function spawnAt(c) {
  [px, py] = lonlatToTile(c.lon, c.lat);
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
      ws.send(JSON.stringify({ action: "spawn", x: Math.round(px), y: Math.round(py), city }));
    } else if (m.type === "presence") {
      others = (m.players || []).filter((p) => p.pid !== myPid);
    }
  };
  ws.onclose = () => { ws = null; };
}

function loadSpawns() {  // cities of the age — pick one to begin
  fetch("/world/spawns").then((r) => r.json()).then((d) => {
    spawnCities = d.spawns || [];
    if (spawnCities.length) [px, py] = lonlatToTile(spawnCities[0].lon, spawnCities[0].lat);
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
