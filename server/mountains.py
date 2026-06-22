"""Real Mediterranean mountain ranges, stamped onto the baked terrain grid.

Color-classifying mountains from the satellite image is unreliable, so instead
we stamp the major ranges from geography: each range is a polyline of (lon, lat)
waypoints with a half-width; the body is impassable 'mountain' (M), the highest
cores are 'glacier'/snow (G, also impassable), and famous historical passes (P)
punch walkable gaps so there's always a way through — matching real routes.

Pure data + a stamping function over a char grid; used by tools/build_map.py at
bake time (no runtime cost). Tile mapping is shared with landmarks.to_tile.
"""
from __future__ import annotations

import random

# Each range: a center polyline, a half-width in tiles, glacier cores, and the
# real passes that cross it.
RANGES: list[dict] = [
    {"name": "Alps",
     "line": [(6.5, 45.1), (7.0, 45.9), (8.6, 46.5), (10.5, 46.6),
              (12.4, 47.0), (14.5, 46.9), (16.2, 47.0)],
     "width": 2,
     "glaciers": [(6.86, 45.83), (7.87, 45.94), (8.0, 46.5), (12.0, 47.0)],
     "passes": [(7.17, 45.87), (6.9, 45.22), (8.57, 46.55), (11.5, 47.0)]},
    {"name": "Pyrenees",
     "line": [(-1.6, 43.0), (0.6, 42.6), (3.1, 42.4)],
     "width": 1,
     "glaciers": [(0.65, 42.63)],
     "passes": [(-1.32, 43.01), (0.5, 42.7)]},
    {"name": "Apennines",
     "line": [(9.5, 44.2), (11.0, 43.5), (13.0, 42.5), (14.0, 41.4),
              (16.0, 40.1)],
     "width": 1,
     "glaciers": [(13.57, 42.47)],
     "passes": [(11.0, 44.0), (13.7, 42.4)]},
    {"name": "Atlas",
     "line": [(-8.0, 31.0), (-5.0, 32.0), (-1.0, 33.0), (3.0, 34.5),
              (6.5, 35.6)],
     "width": 1,
     "glaciers": [(-7.92, 31.06)],
     "passes": [(-7.4, 31.3), (2.0, 34.0)]},
    {"name": "Dinaric Alps",
     "line": [(14.5, 45.5), (16.5, 43.5), (18.5, 42.5), (20.0, 41.4)],
     "width": 1,
     "glaciers": [],
     "passes": [(17.0, 43.2)]},
    {"name": "Taurus",
     "line": [(29.5, 37.0), (32.0, 37.2), (34.5, 37.3), (36.6, 37.2)],
     "width": 1,
     "glaciers": [],
     "passes": [(34.8, 37.35)]},  # the Cilician Gates
]


def _disc(grid, cx, cy, r, ch, only_land=True):
    H = len(grid)
    W = len(grid[0])
    for y in range(max(0, cy - r), min(H, cy + r + 1)):
        for x in range(max(0, cx - r), min(W, cx + r + 1)):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                if only_land and grid[y][x] == "~":
                    continue  # don't stamp mountains onto the sea
                grid[y][x] = ch


def stamp(grid: list[list[str]], to_tile, width: int, height: int) -> None:
    """Overwrite terrain chars in-place with M (mountain), G (glacier), P (pass)."""
    for rng in RANGES:
        w = rng["width"]
        pts = [to_tile(lon, lat, width, height) for lon, lat in rng["line"]]
        # Walk each segment, stamping a band of mountain.
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            steps = max(abs(x1 - x0), abs(y1 - y0), 1)
            for i in range(steps + 1):
                cx = round(x0 + (x1 - x0) * i / steps)
                cy = round(y0 + (y1 - y0) * i / steps)
                _disc(grid, cx, cy, w, "M")
        for lon, lat in rng["glaciers"]:
            gx, gy = to_tile(lon, lat, width, height)
            _disc(grid, gx, gy, 1, "G")
        # Punch passes last so they always win — a walkable gap through the wall.
        for lon, lat in rng["passes"]:
            px, py = to_tile(lon, lat, width, height)
            _disc(grid, px, py, w + 1, "P", only_land=False)
            # A pass only over land/mountain, never carving sea:
            for y in range(max(0, py - w - 1), min(height, py + w + 2)):
                for x in range(max(0, px - w - 1), min(width, px + w + 2)):
                    if grid[y][x] == "P" and _was_sea(grid, x, y):
                        grid[y][x] = "~"

    # Rocky foothills: land beside the ranges turns to hills/stone, so stone is
    # plentiful near the mountains (and flint/obsidian scatter there too).
    rngf = random.Random(424242)
    foot = []
    for y in range(height):
        for x in range(width):
            if grid[y][x] in "gfd" and _near_char(grid, x, y, "M", 2):
                foot.append((x, y))
    for x, y in foot:
        grid[y][x] = "m" if rngf.random() < 0.35 else "h"


def _near_char(grid, x, y, ch, r) -> bool:
    H = len(grid); W = len(grid[0])
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            nx, ny = x + dx, y + dy
            if 0 <= nx < W and 0 <= ny < H and grid[ny][nx] == ch:
                return True
    return False


def _was_sea(grid, x, y) -> bool:
    # Heuristic: a pass tile fully surrounded by sea was mis-carved over water.
    H = len(grid); W = len(grid[0])
    sea = 0
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if 0 <= nx < W and 0 <= ny < H and grid[ny][nx] == "~":
            sea += 1
    return sea >= 3
