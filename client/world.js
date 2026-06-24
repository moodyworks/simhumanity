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
let builds = {}, myInv = {}, structures = [], ruins = []; // gather / build / dig state
let worldYear = null, worldEra = "";                     // era clock
let npcs = [], myHp = null, myMaxHp = null;              // NPCs + combat
let cities = [], sites = [];                            // historical cities + ancient sites
const terrainCache = new Map();                          // chunk -> ImageData (land/water)
let toastT = 0;
function toast(text) {
  const el = document.getElementById("log");
  el.textContent = text; el.style.opacity = "1";
  clearTimeout(toastT); toastT = setTimeout(() => { el.style.opacity = "0"; }, 2500);
}

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

function viewRect() { // visible area in global tiles (centred on the camera)
  const hw = canvas.width / 2 / TILE, hh = canvas.height / 2 / TILE;
  return { x0: camX - hw, y0: camY - hh, x1: camX + hw, y1: camY + hh };
}

function update(dt) {
  if (spawned) {  // movement (Shift = run); realistic scale, "game-fast" for the demo
    const onWater = (myInv.boat || 0) > 0 && isWaterTile(px, py);
    const sp = 14 * (keys.has("shift") ? 60 : 1) * (onWater ? 0.5 : 1) * dt; // boats are slow
    let dx = 0, dy = 0;
    if (keys.has("w") || keys.has("arrowup")) dy -= 1;
    if (keys.has("s") || keys.has("arrowdown")) dy += 1;
    if (keys.has("a") || keys.has("arrowleft")) dx -= 1;
    if (keys.has("d") || keys.has("arrowright")) dx += 1;
    if (dx || dy) {
      followCam = true;  // moving snaps the camera back to you
      const m = Math.hypot(dx, dy) || 1;
      let nx = ((px + dx / m * sp) % man.src_w + man.src_w) % man.src_w;  // wrap E/W
      let ny = Math.max(0, Math.min(man.src_h - 1, py + dy / m * sp));    // poles = wall
      const boat = (myInv.boat || 0) > 0;                   // a boat crosses water
      if (boat || !isWaterTile(nx, ny)) { px = nx; py = ny; }
      else { if (!isWaterTile(nx, py)) px = nx; if (!isWaterTile(px, ny)) py = ny; }
    }
  }
  if (followCam) { camX = px; camY = py; }  // else the minimap "look" holds the camera
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
  const offX = Math.round(canvas.width / 2 - camX * TILE), offY = Math.round(canvas.height / 2 - camY * TILE);
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
  // ancient sites (date-gated) — gold diamonds
  for (const s of sites) {
    const t = lonlatToTile(s.lon, s.lat); let ox = t[0]; const dd = ox - px;
    if (dd > man.src_w / 2) ox -= man.src_w; else if (dd < -man.src_w / 2) ox += man.src_w;
    const sx = offX + ox * TILE, sy = offY + t[1] * TILE, r = TILE * 0.6;
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
    const t = lonlatToTile(c.lon, c.lat); let ox = t[0]; const dd = ox - px;
    if (dd > man.src_w / 2) ox -= man.src_w; else if (dd < -man.src_w / 2) ox += man.src_w;
    const sx = offX + ox * TILE, sy = offY + t[1] * TILE;
    if (sx < -80 || sy < -80 || sx > canvas.width + 80 || sy > canvas.height + 80) continue;
    const rad = TILE * (0.45 + 0.22 * c.stage);
    ctx.fillStyle = "#e8d3a0"; ctx.strokeStyle = "#5a4424"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(sx, sy, rad, 0, 7); ctx.fill(); ctx.stroke();
    ctx.fillStyle = "#fff"; ctx.font = "12px ui-monospace, monospace"; ctx.textAlign = "center";
    ctx.fillText(`${c.name} · ${sizeName[c.stage]}`, sx, sy - rad - 4); ctx.textAlign = "left";
  }

  // built structures
  for (const s of structures) {
    let ox = s.x; const d = ox - px;
    if (d > man.src_w / 2) ox -= man.src_w; else if (d < -man.src_w / 2) ox += man.src_w;
    const sx = offX + ox * TILE, sy = offY + s.y * TILE;
    if (sx < -TILE || sy < -TILE || sx > canvas.width || sy > canvas.height) continue;
    ctx.fillStyle = "#caa472"; ctx.fillRect(sx - TILE / 2, sy - TILE / 2, TILE, TILE);
    ctx.strokeStyle = "#3a2d18"; ctx.lineWidth = 2; ctx.strokeRect(sx - TILE / 2, sy - TILE / 2, TILE, TILE);
  }

  // ruins (decayed past-era structures — dig sites; press E on one)
  for (const s of ruins) {
    let ox = s.x; const d = ox - px;
    if (d > man.src_w / 2) ox -= man.src_w; else if (d < -man.src_w / 2) ox += man.src_w;
    const sx = offX + ox * TILE, sy = offY + s.y * TILE;
    if (sx < -TILE || sy < -TILE || sx > canvas.width || sy > canvas.height) continue;
    ctx.fillStyle = "#5a4a33"; ctx.fillRect(sx - TILE / 2, sy - TILE / 2, TILE, TILE);
    ctx.strokeStyle = "#241c10"; ctx.lineWidth = 1; ctx.strokeRect(sx - TILE / 2, sy - TILE / 2, TILE, TILE);
  }

  // NPCs (wander / hunt around you) — name, HP bar, hostile outline
  const npcColor = { wanderer: "#9aa3b0", merchant: "#6fd0c8", brigand: "#e08a3a", monster: "#d24a6a" };
  for (const n of npcs) {
    let ox = n.x; const d = ox - px;
    if (d > man.src_w / 2) ox -= man.src_w; else if (d < -man.src_w / 2) ox += man.src_w;
    const sx = offX + ox * TILE, sy = offY + n.y * TILE, rr = TILE * 0.42;
    if (sx < -TILE * 2 || sy < -TILE * 2 || sx > canvas.width + TILE || sy > canvas.height + TILE) continue;
    ctx.fillStyle = npcColor[n.kind] || "#aaa";
    ctx.beginPath(); ctx.arc(sx, sy, rr, 0, 7); ctx.fill();
    ctx.lineWidth = n.hostile ? 2.5 : 1; ctx.strokeStyle = n.hostile ? "#ff2a2a" : "#000"; ctx.stroke();
    if (n.hp < n.max_hp) {
      const bw = TILE * 0.9;
      ctx.fillStyle = "rgba(0,0,0,0.6)"; ctx.fillRect(sx - bw / 2, sy - rr - 6, bw, 3);
      ctx.fillStyle = "#5bd64a"; ctx.fillRect(sx - bw / 2, sy - rr - 6, bw * n.hp / n.max_hp, 3);
    }
    if (TILE >= 16 || n.hostile) {
      ctx.fillStyle = "#dfe6f0"; ctx.font = "10px ui-monospace, monospace"; ctx.textAlign = "center";
      ctx.fillText(n.name, sx, sy - rr - 9); ctx.textAlign = "left";
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

  // player marker (relative to the camera — centred when following, offset when looking)
  let pox = px; const pdd = pox - camX;
  if (pdd > man.src_w / 2) pox -= man.src_w; else if (pdd < -man.src_w / 2) pox += man.src_w;
  const cx = offX + pox * TILE, cy = offY + py * TILE;
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
      const t = lonlatToTile(c.lon, c.lat);
      ctx.fillStyle = "#e8d3a0";
      ctx.fillRect(mx + t[0] / man.src_w * mmW - 1, my + t[1] / man.src_h * mmH - 1, 2, 2);
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
  const bar = document.getElementById("hpbar");
  if (spawned && myHp != null) {
    bar.style.display = "";
    const pct = Math.max(0, Math.min(1, myHp / myMaxHp));
    const fill = document.getElementById("hpfill");
    fill.style.width = pct * 100 + "%";
    fill.style.background = pct > 0.6 ? "#4caf50" : pct > 0.3 ? "#d4a017" : "#d9433a";
    document.getElementById("hptext").textContent = `♥ ${myHp}/${myMaxHp}`;
  } else bar.style.display = "none";
  document.getElementById("vitals").textContent = spawned ? `${myInv.coin || 0} coin` : "";
  const items = Object.entries(myInv).filter(([k]) => k !== "coin").map(([k, v]) => `${k} ${v}`);
  document.getElementById("inv").textContent = spawned ? (items.join(" · ") || "(empty pack)") : "";
  document.getElementById("builds").innerHTML = spawned && Object.keys(builds).length ?
    "build " + Object.keys(builds).map((b, i) => `<b>${i + 1}</b>:${b}`).join("  ") : "";
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
  if (!spawned || !ws || ws.readyState !== 1) return;
  if (k === "g" || k === " ") ws.send(JSON.stringify({ action: "gather" }));
  if (k === "e") ws.send(JSON.stringify({ action: "dig" }));
  if (k === "r") ws.send(JSON.stringify({ action: "attack" }));
  if (k === "f") ws.send(JSON.stringify({ action: "trade" }));
  const bk = Object.keys(builds);            // 1..N build the listed structures
  if (/^[1-9]$/.test(k) && bk[+k - 1])
    ws.send(JSON.stringify({ action: "build", kind: bk[+k - 1] }));
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
canvas.addEventListener("mousedown", (e) => { if (minimapLook(e.clientX, e.clientY)) mmDragging = true; });
addEventListener("mousemove", (e) => { if (mmDragging) minimapLook(e.clientX, e.clientY); });
addEventListener("mouseup", () => { mmDragging = false; });

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
      myPid = m.pid; builds = m.builds || {};
      ws.send(JSON.stringify({ action: "spawn", x: Math.round(px), y: Math.round(py), city }));
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
    } else if (m.type === "inv") {
      myInv = m.inv || {};
      if (m.hp != null) { myHp = m.hp; myMaxHp = m.max_hp; }
    } else if (m.type === "respawn") {
      px = m.x; py = m.y; myHp = m.hp; toast("You were slain — back to your city.");
    } else if (m.type === "log") {
      toast(m.text);
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
