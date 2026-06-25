"""Slice a full-resolution equirectangular world into game chunks.

NO downsampling — **one source pixel becomes one game tile** (a square), rendered
in its real colour. The game keeps a 3x3 ring of chunks (the one you're in + 8
neighbours) loaded, so there's always data and never a loading screen.

Two input modes:

  * Single image:   ./.venv/bin/python -m tools.tile_world world.jpg 600
  * NASA 500m grid: ./.venv/bin/python -m tools.tile_world highres 600
        a directory holding the 8 Blue Marble 500m tiles named
        ...A1.jpg ... D2.jpg  (cols A-D = lon west->east, rows 1-2 = north/south).
        Assembled that's 86400x43200 = 3.7e9 tiles; they're tiled one source tile
        at a time so the full image is never held in memory.

chunk_px is in pixels == game tiles. In NASA mode it must divide the source tile
size (21600) so chunks align to the global grid — e.g. 540, 600, 675, 720, 800, 1080.

Writes world_tiles/c{col}_r{row}.<ext> (global chunk coords) and a manifest.json
(grid + equirectangular bounds, so lat/lon -> tile -> chunk is exact).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

Image.MAX_IMAGE_PIXELS = None  # the source exceeds PIL's decompression-bomb guard

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "world_tiles"

# A full equirectangular image spans the whole globe.
LON_W, LON_E, LAT_N, LAT_S = -180.0, 180.0, 90.0, -90.0
NASA_COLS = "ABCD"  # longitude, west -> east
NASA_ROWS = "12"    # latitude, north (1) -> south (2)
CRISP = os.environ.get("CRISP", "1") != "0"  # repaint seas for a defined coastline


def _dilate(mask: np.ndarray, it: int) -> np.ndarray:
    """Binary dilation by `it` px (4-connectivity), numpy-only."""
    m = mask
    for _ in range(it):
        d = m.copy()
        d[1:, :] |= m[:-1, :]; d[:-1, :] |= m[1:, :]
        d[:, 1:] |= m[:, :-1]; d[:, :-1] |= m[:, 1:]
        m = d
    return m


def crisp_water(im: Image.Image, band: int = 2048, pad: int = 3) -> Image.Image:
    """Give the anti-aliased coastline a defined edge: classify the (near-black)
    ocean, grab the dark anti-aliased fringe just around it, and repaint the lot a
    subtly depth-shaded sea colour — land stays photographic. Banded with overlap
    so the fringe grab leaves no seams, bounding memory on huge tiles."""
    base = np.array([52, 112, 162], np.float32)  # main sea colour
    W, H = im.size
    for y0 in range(0, H, band):
        y1 = min(y0 + band, H)
        ey0, ey1 = max(0, y0 - pad), min(H, y1 + pad)  # extended for the dilation
        a = np.asarray(im.crop((0, ey0, W, ey1)))
        R, G, B = (a[:, :, i].astype(np.int16) for i in range(3))
        s = R + G + B
        sea = (s < 55) & (B >= R)              # near-black open water
        water = sea | (_dilate(sea, pad) & (s < 100))  # + dark anti-aliased coast
        depth = np.clip((55 - s) / 55.0, 0, 1)[..., None]       # 0 shallow .. 1 deep
        shade = (base * (1.0 - 0.18 * depth)).astype(np.uint8)  # deeper = only a touch darker
        out = a.copy()
        out[water] = shade[water]
        core = out[y0 - ey0: y0 - ey0 + (y1 - y0)]  # drop the overlap pad
        im.paste(Image.fromarray(core, "RGB"), (0, y0))
    return im


def _find(d: Path, key: str) -> Path | None:
    for pat in (f"*_{key}_*", f"*{key}*"):
        m = [p for p in d.glob(pat) if p.suffix.lower() in (".jpg", ".jpeg", ".png")]
        if m:
            return m[0]
    return None


def composite_sea(im: Image.Image, topo_path: Path, bath_path: Path,
                  band: int = 2048, grab: int = 12) -> Image.Image:
    """Paint the sea from authoritative GEBCO data instead of guessing from colour:
    water = land-elevation 0 (topo), depth-shaded by bathymetry. Gives clean coasts
    and full polar oceans (no colour mis-classification), smooth real depth, and the
    basis for ice-age sea levels. Land keeps the satellite colour. Rivers/lakes above
    sea level aren't in this mask — those come from the rivers overlay. GEBCO is
    half-res (1km), nearest-upsampled 2x to the 500m grid; `band` must be even."""
    W, H = im.size
    topo = np.asarray(Image.open(topo_path).convert("L"))
    bath = np.asarray(Image.open(bath_path).convert("L"))
    base = np.array([60, 120, 168], np.float32)  # main sea colour
    pad = grab + 2 - (grab % 2)  # even, >= grab — keeps the 2x GEBCO upsample aligned
    for y0 in range(0, H, band):
        y1 = min(y0 + band, H)
        ey0, ey1 = max(0, y0 - pad), min(H, y1 + pad)  # extended band for the grab
        a = np.asarray(im.crop((0, ey0, W, ey1)))
        R, G, B = (a[:, :, i].astype(np.int16) for i in range(3))
        s = R + G + B
        g0, g1 = ey0 // 2, -(-ey1 // 2)
        wg = np.repeat(np.repeat(topo[g0:g1] == 0, 2, 0), 2, 1)[:ey1 - ey0, :W]
        deepg = np.clip((255 - bath[g0:g1].astype(np.int16)) / 120.0, 0, 1)
        deepg = np.repeat(np.repeat(deepg, 2, 0), 2, 1)[:ey1 - ey0, :W]
        # GEBCO's 1km coast leaves near-black satellite sea mislabelled land; grab it
        # (any near-black hue, not just blue — murky/sediment shallows count too)
        coastal = (s < 60) & _dilate(wg, grab)
        water = wg | coastal
        deep = np.where(wg, deepg, 0.0)  # grabbed coastal pixels are shallow
        shade = (base * (1.0 - 0.20 * deep[..., None])).astype(np.uint8)  # subtle depth
        out = a.copy()
        out[water] = shade[water]
        # lift any near-black shoreline pixels GEBCO+grab still missed, toward their
        # own hue, so the coast has no black holes (no flooding — land stays land)
        dark = (~water) & (s < 75) & _dilate(wg, grab + 8)
        if dark.any():
            lift = np.clip(a.astype(np.float32) * 2.2 + 34, 0, 255).astype(np.uint8)
            out[dark] = lift[dark]
        core = out[y0 - ey0: y0 - ey0 + (y1 - y0)]
        im.paste(Image.fromarray(core, "RGB"), (0, y0))
    return im


def draw_rivers(im: Image.Image, features: list, x_off: int, y_off: int,
                world_w: int, world_h: int, color=(60, 120, 168)) -> Image.Image:
    """Draw Natural Earth river/lake centerlines onto a quadrant tile. Each vertex
    is equirectangular lon/lat -> global pixel -> local tile pixel; PIL clips lines
    that run off the tile. Major rivers (low scalerank) are drawn a touch wider."""
    draw = ImageDraw.Draw(im)
    W, H = im.size

    def proj(lon, lat):
        return ((lon + 180.0) / 360.0 * world_w - x_off,
                (90.0 - lat) / 180.0 * world_h - y_off)

    for feat in features:
        sr = feat["properties"].get("scalerank")
        width = 2 if (sr is not None and sr <= 4) else 1
        geom = feat["geometry"]
        segs = (geom["coordinates"] if geom["type"] == "MultiLineString"
                else [geom["coordinates"]])
        for seg in segs:
            pts = [proj(c[0], c[1]) for c in seg]
            if all(x < -8 or x > W + 8 or y < -8 or y > H + 8 for x, y in pts):
                continue  # wholly off this tile
            if len(pts) >= 2:
                draw.line(pts, fill=color, width=width, joint="curve")
    return im


def draw_lakes(im: Image.Image, features: list, x_off: int, y_off: int,
               world_w: int, world_h: int, color=(60, 120, 168)) -> Image.Image:
    """Fill Natural Earth lake polygons (the ones above sea level that GEBCO's
    sea mask misses — Great Lakes, Victoria, Baikal…). Same projection as rivers;
    holes (islands in lakes) are ignored."""
    draw = ImageDraw.Draw(im)
    W, H = im.size

    def proj(lon, lat):
        return ((lon + 180.0) / 360.0 * world_w - x_off,
                (90.0 - lat) / 180.0 * world_h - y_off)

    for feat in features:
        geom = feat["geometry"]
        polys = (geom["coordinates"] if geom["type"] == "MultiPolygon"
                 else [geom["coordinates"]])
        for poly in polys:
            pts = [proj(c[0], c[1]) for c in poly[0]]  # outer ring
            if all(x < -8 or x > W + 8 or y < -8 or y > H + 8 for x, y in pts):
                continue
            if len(pts) >= 3:
                draw.polygon(pts, fill=color)
    return im


def _save_kw(ext: str) -> dict:
    return {"quality": 92} if ext in ("jpg", "jpeg") else {}


def _write_manifest(W: int, H: int, chunk: int, cols: int, rows: int,
                    n: int, ext: str, source: str) -> None:
    (OUT / "manifest.json").write_text(json.dumps({
        "source": source,
        "version": int(time.time()),            # cache-buster for re-tiled chunks
        "src_w": W, "src_h": H,                 # == world size in game tiles
        "chunk_px": chunk, "cols": cols, "rows": rows, "count": n,
        "ext": ext,
        "tile_is_pixel": True,                  # 1 source pixel == 1 game tile
        "projection": "equirectangular",
        "bounds": {"lon_w": LON_W, "lon_e": LON_E, "lat_n": LAT_N, "lat_s": LAT_S},
        # lat/lon -> tile:  tx = (lon - lon_w)/(lon_e-lon_w)*src_w
        #                   ty = (lat_n - lat)/(lat_n-lat_s)*src_h
        # tile -> chunk:    (tx // chunk_px, ty // chunk_px)
    }, indent=2))


def tile_single(src: Path, chunk: int, ext: str) -> None:
    im = Image.open(src).convert("RGB")
    W, H = im.size
    cols, rows = -(-W // chunk), -(-H // chunk)  # ceil
    OUT.mkdir(exist_ok=True)
    kw, n = _save_kw(ext), 0
    for r in range(rows):
        for c in range(cols):
            x0, y0 = c * chunk, r * chunk
            box = (x0, y0, min(x0 + chunk, W), min(y0 + chunk, H))
            im.crop(box).save(OUT / f"c{c}_r{r}.{ext}", **kw)
            n += 1
        print(f"  row {r + 1}/{rows}", end="\r", flush=True)
    _write_manifest(W, H, chunk, cols, rows, n, ext, src.name)
    print(f"\n{src.name}  {W}x{H}px  ->  {n} chunks of {chunk}px ({cols}x{rows})")
    print(f"world = {W} x {H} game tiles ({W * H / 1e6:.0f}M tiles)")


def tile_nasa(src_dir: Path, chunk: int, ext: str, only: str | None = None) -> None:
    # Locate the 8 tiles by their A1..D2 suffix.
    paths: dict[str, Path] = {}
    for r in NASA_ROWS:
        for c in NASA_COLS:
            k = c + r
            cand = [p for p in src_dir.glob(f"*.{k}.*")
                    if "(1)" not in p.name and p.suffix.lower() in (".jpg", ".jpeg", ".png")]
            if not cand:
                sys.exit(f"missing NASA 500m tile '{k}' in {src_dir}")
            paths[k] = cand[0]
    s = Image.open(paths["A1"]).size[0]
    if Image.open(paths["A1"]).size != (s, s):
        sys.exit("NASA tiles must be square")
    if s % chunk:
        ok = [d for d in (216, 270, 360, 432, 540, 600, 675, 720, 800, 1080) if s % d == 0]
        sys.exit(f"chunk_px must divide the source tile size {s}; try one of {ok}")
    cpt = s // chunk                      # chunks per source-tile side
    cols, rows = len(NASA_COLS) * cpt, len(NASA_ROWS) * cpt
    W, H = len(NASA_COLS) * s, len(NASA_ROWS) * s
    OUT.mkdir(exist_ok=True)
    rivers = lakes = None
    if CRISP:
        rv, lk = src_dir.parent / "rivers.geojson", src_dir.parent / "lakes.geojson"
        if rv.exists():
            rivers = json.loads(rv.read_text())["features"]
        if lk.exists():
            lakes = json.loads(lk.read_text())["features"]
    kw, n = _save_kw(ext), 0
    for ri, rdig in enumerate(NASA_ROWS):
        for ci, cl in enumerate(NASA_COLS):
            if only and (cl + rdig) != only:
                continue
            im = Image.open(paths[cl + rdig]).convert("RGB")
            if CRISP:
                tp = _find(src_dir.parent / "topo", cl + rdig)
                bp = _find(src_dir.parent / "bath", cl + rdig)
                composite_sea(im, tp, bp) if (tp and bp) else crisp_water(im)
                if lakes:
                    draw_lakes(im, lakes, ci * s, ri * s, W, H)
                if rivers:
                    draw_rivers(im, rivers, ci * s, ri * s, W, H)
            for lr in range(cpt):
                for lc in range(cpt):
                    box = (lc * chunk, lr * chunk, (lc + 1) * chunk, (lr + 1) * chunk)
                    gc, gr = ci * cpt + lc, ri * cpt + lr
                    im.crop(box).save(OUT / f"c{gc}_r{gr}.{ext}", **kw)
                    n += 1
            im.close()
            print(f"  tiled {cl}{rdig}: chunks c{ci*cpt}..{ci*cpt+cpt-1} r{ri*cpt}..{ri*cpt+cpt-1}")
    _write_manifest(W, H, chunk, cols, rows, n, ext, src_dir.name)
    print(f"\nNASA 500m grid  {W}x{H}px  ->  {n} chunks of {chunk}px ({cols}x{rows})")
    print(f"world = {W} x {H} game tiles ({W * H / 1e9:.2f}B tiles)")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: tile_world.py <image-or-dir> [chunk_px=600] [ext=jpg]")
    src = Path(sys.argv[1])
    chunk = int(sys.argv[2]) if len(sys.argv) > 2 else 600
    ext = (sys.argv[3] if len(sys.argv) > 3 else "jpg").lower()
    only = sys.argv[4] if len(sys.argv) > 4 else None  # e.g. C1 — tile one quadrant
    if not src.exists():
        sys.exit(f"not found: {src}")
    if src.is_dir():
        tile_nasa(src, chunk, ext, only)
    else:
        tile_single(src, chunk, ext)


if __name__ == "__main__":
    main()
