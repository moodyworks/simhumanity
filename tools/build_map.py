"""Offline asset tool: bake the Mediterranean terrain grid from medsmall.jpg.

The game does NOT depend on Pillow or decode the JPG at runtime. This tool runs
once (or whenever the source image changes) and writes a compact char grid to
server/med_map.txt, which the server loads at boot. It also writes a colored
preview PNG so the classification can be eyeballed.

Two stages:
  1. Classify each pixel by colour into the *macro* geography the image actually
     shows — water, desert, land, or snow-capped mountain. This is where the real
     coastlines and the Sahara come from.
  2. Texture the generic "land" into clustered grass / forest / hills biomes with
     seeded value-noise, so gameplay terrain (forage / wood / stone) is balanced
     instead of a solid forest wall. Seeded → identical every run.

Run:  ./.venv/bin/python -m tools.build_map
Requires Pillow (dev-only): ./.venv/bin/pip install Pillow
"""
from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "medsmall.jpg"
OUT_GRID = ROOT / "server" / "med_map.txt"
OUT_PREVIEW = ROOT / "med_classified_preview.png"

# Terrain legend chars (must match server/mapdata.LEGEND).
#   ~ water  g grass  f forest  h hills  m stone/mountain  d desert
PREVIEW_COLORS = {
    "~": (43, 74, 111), "g": (74, 122, 58), "f": (47, 90, 42),
    "h": (122, 106, 74), "m": (150, 152, 160), "d": (194, 167, 102),
}

SEED = 20260620


def classify_base(r: int, g: int, b: int) -> str:
    """Macro geography from colour: ~ water, d desert, m mountain, L land."""
    # Water: blue clearly dominant.
    if b > r + 15 and b >= g - 5 and b > 55:
        return "~"
    # Snow-capped peaks: bright and neutral (sand is bright but warm).
    if min(r, g, b) > 165 and abs(r - b) < 25:
        return "m"
    # Desert / arid: warm, red well above blue.
    if r > b + 28 and r >= g - 12:
        return "d"
    return "L"  # generic land, textured into biomes below


def _noise(x: int, y: int, seed: int, freq: float) -> float:
    """Spatially-correlated value noise in ~[0,1] so biomes form patches."""
    v = (math.sin(x * freq) + math.cos(y * freq)
         + math.sin((x + y) * freq * 0.7) + math.cos((x - y) * freq * 1.3))
    v = (v + 4) / 8
    jitter = random.Random((x * 92821) ^ (y * 68917) ^ seed).random()
    return 0.7 * v + 0.3 * jitter


def texture_land(x: int, y: int) -> str:
    """Pick a biome for a land tile: clustered forest / hills, else grass."""
    if _noise(x, y, SEED + 1, 0.16) > 0.60:
        return "f"
    if _noise(x, y, SEED + 2, 0.21) > 0.70:
        return "h"
    return "g"


def build() -> None:
    im = Image.open(SRC).convert("RGB")
    W, H = im.size
    px = im.load()

    def sample(x: int, y: int) -> tuple[int, int, int]:
        """3x3 box average to suppress JPEG speckle."""
        rs = gs = bs = n = 0
        for yy in range(max(0, y - 1), min(H, y + 2)):
            for xx in range(max(0, x - 1), min(W, x + 2)):
                r, g, b = px[xx, yy]
                rs += r; gs += g; bs += b; n += 1
        return rs // n, gs // n, bs // n

    rows: list[str] = []
    preview = Image.new("RGB", (W, H))
    ppx = preview.load()
    counts: dict[str, int] = {}
    for y in range(H):
        chars: list[str] = []
        for x in range(W):
            base = classify_base(*sample(x, y))
            if base == "L":
                ch = texture_land(x, y)
            elif base == "d":
                # Rocky outcrops break up the desert and give stone/flint.
                ch = "h" if _noise(x, y, SEED + 3, 0.3) > 0.82 else "d"
            else:
                ch = base
            chars.append(ch)
            counts[ch] = counts.get(ch, 0) + 1
            ppx[x, y] = PREVIEW_COLORS[ch]
        rows.append("".join(chars))

    OUT_GRID.write_text("\n".join(rows))
    preview.save(OUT_PREVIEW)
    total = W * H
    print(f"Source {SRC.name}: {W}x{H} ({total} tiles)")
    for ch in "~gfhmd":
        c = counts.get(ch, 0)
        print(f"  {ch}  {c:6d}  {100 * c / total:4.1f}%")
    print(f"Wrote {OUT_GRID.relative_to(ROOT)} and {OUT_PREVIEW.name}")


if __name__ == "__main__":
    build()
