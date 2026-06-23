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
from PIL import Image

Image.MAX_IMAGE_PIXELS = None  # the source exceeds PIL's decompression-bomb guard

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "world_tiles"

# A full equirectangular image spans the whole globe.
LON_W, LON_E, LAT_N, LAT_S = -180.0, 180.0, 90.0, -90.0
NASA_COLS = "ABCD"  # longitude, west -> east
NASA_ROWS = "12"    # latitude, north (1) -> south (2)
CRISP = os.environ.get("CRISP", "1") != "0"  # repaint seas for a defined coastline


def crisp_water(im: Image.Image, band: int = 2048) -> Image.Image:
    """Give the anti-aliased coastline a defined edge: classify ocean-blue pixels
    and repaint them flat sea colours, so land and water separate cleanly. Land is
    left photographic. Done in horizontal bands to bound memory on huge tiles."""
    W, H = im.size
    for y0 in range(0, H, band):
        a = np.asarray(im.crop((0, y0, W, min(y0 + band, H))))
        R, G, B = (a[:, :, i].astype(np.int16) for i in range(3))
        s = R + G + B
        water = (s < 55) & (B >= R)  # this Blue Marble renders oceans near-black
        out = a.copy()
        out[water & (s < 18)] = (24, 64, 120)     # deep
        out[water & (s >= 18)] = (52, 112, 162)   # coastal / shallow
        im.paste(Image.fromarray(out, "RGB"), (0, y0))
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
    kw, n = _save_kw(ext), 0
    for ri, rdig in enumerate(NASA_ROWS):
        for ci, cl in enumerate(NASA_COLS):
            if only and (cl + rdig) != only:
                continue
            im = Image.open(paths[cl + rdig]).convert("RGB")
            if CRISP:
                crisp_water(im)
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
