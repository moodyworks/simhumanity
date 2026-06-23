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
  if (c < 0 || r < 0 || c >= man.cols || r >= man.rows) return;
  const k = c + "_" + r;
  if (chunks.has(k)) return;
  const img = new Image();
  const e = { img, loaded: false, missing: false };
  img.onload = () => { e.loaded = true; };
  img.onerror = () => { e.missing = true; }; // region not tiled yet
  img.src = `/tiles/c${c}_r${r}.${man.ext}`;
  chunks.set(k, e);
}

function viewRect() { // visible area in global tiles
  const hw = canvas.width / 2 / TILE, hh = canvas.height / 2 / TILE;
  return { x0: px - hw, y0: py - hh, x1: px + hw, y1: py + hh };
}

function update(dt) {
  // movement (Shift = run); realistic scale, so this is "game-fast" for the demo
  const run = keys.has("shift") ? 6 : 1;
  const sp = 14 * run * dt; // tiles/sec
  let dx = 0, dy = 0;
  if (keys.has("w") || keys.has("arrowup")) dy -= 1;
  if (keys.has("s") || keys.has("arrowdown")) dy += 1;
  if (keys.has("a") || keys.has("arrowleft")) dx -= 1;
  if (keys.has("d") || keys.has("arrowright")) dx += 1;
  if (dx || dy) { const m = Math.hypot(dx, dy) || 1; px += dx / m * sp; py += dy / m * sp; }
  px = Math.max(0, Math.min(man.src_w - 1, px));
  py = Math.max(0, Math.min(man.src_h - 1, py));
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
  const offX = canvas.width / 2 - px * TILE, offY = canvas.height / 2 - py * TILE;
  const v = viewRect();
  for (let r = Math.floor(v.y0 / cp); r <= Math.floor(v.y1 / cp); r++) {
    for (let c = Math.floor(v.x0 / cp); c <= Math.floor(v.x1 / cp); c++) {
      const e = chunks.get(c + "_" + r);
      if (!e || !e.loaded) continue;
      const gx = c * cp, gy = r * cp;                       // chunk's global tile origin
      const ix0 = Math.max(gx, Math.floor(v.x0)), iy0 = Math.max(gy, Math.floor(v.y0));
      const ix1 = Math.min(gx + cp, Math.ceil(v.x1)), iy1 = Math.min(gy + cp, Math.ceil(v.y1));
      if (ix1 <= ix0 || iy1 <= iy0) continue;
      ctx.drawImage(e.img, ix0 - gx, iy0 - gy, ix1 - ix0, iy1 - iy0,
                    offX + ix0 * TILE, offY + iy0 * TILE, (ix1 - ix0) * TILE, (iy1 - iy0) * TILE);
    }
  }
  // player marker
  const cx = canvas.width / 2, cy = canvas.height / 2;
  ctx.fillStyle = "#ff3b3b";
  ctx.fillRect(cx - TILE / 2, cy - TILE / 2, TILE, TILE);
  ctx.strokeStyle = "#fff"; ctx.lineWidth = 2;
  ctx.strokeRect(cx - TILE / 2, cy - TILE / 2, TILE, TILE);

  const [lon, lat] = tileToLonLat(px, py);
  hud.innerHTML =
    `simhumanity — <b>real Earth</b> (C1 quadrant tiled)\n` +
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

fetch("/tiles/manifest.json").then((r) => r.json()).then((m) => {
  man = m;
  [px, py] = lonlatToTile(45.6, 31.3); // spawn: Uruk, Sumer — the first cities
  requestAnimationFrame(frame);
}).catch(() => { hud.textContent = "no world tiles yet — run tools/tile_world.py"; });
