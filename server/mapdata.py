"""Loads the baked Mediterranean terrain grid (server/med_map.txt).

The grid is derived from medsmall.jpg by the offline tool tools/build_map.py —
the real coastlines and the Sahara come from the image's pixels; grass/forest/
hills biomes are seeded within the land. The server only reads the baked text
file here (no image decoding, no Pillow dependency at runtime).

build_terrain() returns grid[y][x] of terrain-name strings.
"""
from __future__ import annotations

from pathlib import Path

MAP_FILE = Path(__file__).resolve().parent / "med_map.txt"

LEGEND = {
    "~": "water", "g": "grass", "f": "forest",
    "h": "hills", "m": "stone", "d": "desert",
}


def _load_chars() -> list[str]:
    if not MAP_FILE.exists():
        raise FileNotFoundError(
            f"{MAP_FILE.name} is missing. Regenerate it from the source image "
            f"with:  ./.venv/bin/python -m tools.build_map"
        )
    rows = MAP_FILE.read_text().splitlines()
    if not rows:
        raise ValueError(f"{MAP_FILE.name} is empty.")
    width = len(rows[0])
    for i, r in enumerate(rows):
        if len(r) != width:
            raise ValueError(f"{MAP_FILE.name} row {i} has width {len(r)} != {width}")
    return rows


# Dimensions are whatever the baked file is (currently the image's 300x219).
_CHARS = _load_chars()
H = len(_CHARS)
W = len(_CHARS[0])


def build_terrain() -> list[list[str]]:
    """Grid of single-char terrain codes; World maps them via LEGEND."""
    return [list(row) for row in _CHARS]


def render_ascii(step: int = 2) -> str:
    """Downsampled preview (every `step`th tile) so the wide map fits a terminal."""
    return "\n".join(
        "".join(_CHARS[y][x] for x in range(0, W, step))
        for y in range(0, H, step)
    )


if __name__ == "__main__":
    import sys
    print(render_ascii(int(sys.argv[1]) if len(sys.argv) > 1 else 2))
