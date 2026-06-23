"""Slice a full-resolution equirectangular world image into game chunks.

NO downsampling — **one source pixel becomes one game tile** (a square), rendered
in its real colour. The game keeps a 3x3 ring of chunks (the one you're in + 8
neighbours) loaded, so there's always data and never a loading screen.

The chunk size is in pixels == game tiles. Pick it bigger than the on-screen
tile span (so a 3x3 ring always covers the view with buffer) but small enough
that a ring loads fast.

Run:
  ./.venv/bin/python -m tools.tile_world world_src.jpg            # default 256px chunks
  ./.venv/bin/python -m tools.tile_world world_src.jpg 512        # 512px chunks
  ./.venv/bin/python -m tools.tile_world world_src.jpg 256 png    # lossless PNG chunks

Writes world_tiles/c{col}_r{row}.<ext> and world_tiles/manifest.json (grid +
equirectangular bounds, so lat/lon -> tile -> chunk is exact).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image

Image.MAX_IMAGE_PIXELS = None  # the source exceeds PIL's decompression-bomb guard

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "world_tiles"

# A full equirectangular image spans the whole globe.
LON_W, LON_E, LAT_N, LAT_S = -180.0, 180.0, 90.0, -90.0


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: tile_world.py <source-image> [chunk_px=256] [ext=jpg]")
    src = Path(sys.argv[1])
    chunk = int(sys.argv[2]) if len(sys.argv) > 2 else 256
    ext = (sys.argv[3] if len(sys.argv) > 3 else "jpg").lower()
    if not src.exists():
        sys.exit(f"source image not found: {src}")

    im = Image.open(src).convert("RGB")
    W, H = im.size
    cols = -(-W // chunk)  # ceil division
    rows = -(-H // chunk)
    OUT.mkdir(exist_ok=True)

    save_kw = {"quality": 92} if ext in ("jpg", "jpeg") else {}
    n = 0
    for r in range(rows):
        for c in range(cols):
            x0, y0 = c * chunk, r * chunk
            box = (x0, y0, min(x0 + chunk, W), min(y0 + chunk, H))
            im.crop(box).save(OUT / f"c{c}_r{r}.{ext}", **save_kw)
            n += 1
        print(f"  row {r + 1}/{rows}", end="\r", flush=True)

    (OUT / "manifest.json").write_text(json.dumps({
        "source": src.name,
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

    print(f"\n{src.name}  {W}x{H}px  ->  {n} chunks of {chunk}px "
          f"({cols} cols x {rows} rows)  in {OUT}")
    print(f"world = {W} x {H} game tiles ({W * H / 1e6:.0f}M tiles)")


if __name__ == "__main__":
    main()
