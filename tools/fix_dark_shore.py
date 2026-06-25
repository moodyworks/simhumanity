"""Lift near-black "dark shore" pixels in already-generated world_tiles.

The coastal sea-grab only repaints water it can identify; a few near-black pixels
(murky shallows, cliff shadow, sediment) can survive at shorelines as ugly black
dots. This scans the finished chunk JPGs and brightens any near-black pixel toward
its OWN hue (so dark blue stays sea, dark brown stays shore) — no source data or
re-tiling needed. Safe because real sea is depth-shaded well above near-black.

    python tools/fix_dark_shore.py [world_tiles_dir]      # default: world_tiles
    DARK_THRESH=66 python tools/fix_dark_shore.py         # tune the R+G+B cutoff
"""
import glob
import os
import sys

import numpy as np
from PIL import Image

TILES = sys.argv[1] if len(sys.argv) > 1 else "world_tiles"
THRESH = int(os.environ.get("DARK_THRESH", "66"))  # R+G+B below this = near-black


def fix(path: str) -> int:
    im = Image.open(path).convert("RGB")
    a = np.asarray(im)
    dark = a.astype(np.int16).sum(2) < THRESH
    n = int(dark.sum())
    if not n:
        return 0
    lift = np.clip(a.astype(np.float32) * 2.2 + 34, 0, 255).astype(np.uint8)
    out = a.copy()
    out[dark] = lift[dark]
    Image.fromarray(out, "RGB").save(path, quality=95)
    return n


def main() -> None:
    files = sorted(glob.glob(os.path.join(TILES, "*.jpg")))
    if not files:
        sys.exit(f"no .jpg tiles found in {TILES!r}")
    print(f"scanning {len(files)} tiles in {TILES} (lift R+G+B < {THRESH})…")
    total = tiles = 0
    for i, f in enumerate(files):
        n = fix(f)
        if n:
            tiles += 1
            total += n
        if (i + 1) % 1000 == 0:
            print(f"  {i + 1}/{len(files)}…")
    print(f"done — lifted {total:,} dark pixels across {tiles} tiles")


if __name__ == "__main__":
    main()
