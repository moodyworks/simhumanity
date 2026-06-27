"""Read the finished world_tiles chunks to test a tile's *rendered* colour.

The 8 km WorldTerrain is too coarse to know whether a single tile draws as land or
sea, and our coastline doesn't match GEBCO exactly — so a city, a fish node, or a
sea monster can end up on a tile that actually renders the other way. This samples
the real chunk pixels (the same sea-blue test the client uses), caching a compact
per-chunk water mask (bounded, LRU) so it's cheap to call every tick. Chunks are
gitignored/user-generated; if absent we no-op and callers fall back to the coarse
terrain.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

import numpy as np
from PIL import Image

# Biome read straight from the rendered tile colour, so resources match what you see.
BIOMES = ["water", "desert", "forest", "grass", "mountain", "snow"]


def _classify(a: np.ndarray) -> np.ndarray:
    """RGB chunk -> per-pixel biome index (uint8), from satellite colour."""
    r, g, b = a[:, :, 0].astype(np.int16), a[:, :, 1].astype(np.int16), a[:, :, 2].astype(np.int16)
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    # Water == blue-DOMINANT (blue clearly the top channel), down to near-black so
    # even the very dark polar/deep sea counts (e.g. the [2,5,20] water off
    # Antarctica). Blue-dominance is what separates water from every dark *land*
    # type: forest/jungle is green-dominant and tundra/shadow is neutral, so neither
    # can qualify — only genuine sea does. Thresholds are small (b>r+12, b>18) so
    # they fire even at near-black, where absolute channel gaps shrink. Must stay
    # identical to client isWaterTile().
    water = (b > r + 12) & (b > g) & (b > 18)            # painted sea-blue (client's test)
    snow = (mn >= 180) & (mx - mn <= 40)                # bright & grey -> ice/snow
    forest = (g > r) & (g >= b)                          # green -> trees/foliage
    warm = (r >= g) & (g >= b - 10)                      # warm/dry (R>=G>=~B)
    desert = warm & (mx > 175)                           # bright warm -> sand
    gray = (mx - mn <= 30)                               # low saturation -> rock/mountain
    return np.select([water, snow, forest, desert, gray, warm],
                     [0, 5, 2, 1, 4, 3], default=3).astype(np.uint8)


class RenderedTiles:
    def __init__(self, tiles_dir, manifest: dict, cap: int = 400) -> None:
        self.dir = Path(tiles_dir)
        self.cp = int(manifest["chunk_px"])
        self.W = int(manifest["src_w"])
        self.H = int(manifest["src_h"])
        self.ext = manifest.get("ext", "jpg")
        self._cap = cap
        self._biomes: OrderedDict[tuple[int, int], np.ndarray | None] = OrderedDict()

    def _chunk(self, col: int, row: int):
        key = (col, row)
        if key in self._biomes:
            self._biomes.move_to_end(key)
            return self._biomes[key]
        try:
            arr = _classify(np.asarray(Image.open(self.dir / f"c{col}_r{row}.{self.ext}").convert("RGB")))
        except Exception:
            arr = None
        self._biomes[key] = arr
        if len(self._biomes) > self._cap:
            self._biomes.popitem(last=False)
        return arr

    def available(self) -> bool:
        return self._chunk(0, 0) is not None

    def _idx(self, tx: float, ty: float):
        ix, iy = int(tx) % self.W, max(0, min(self.H - 1, int(ty)))
        arr = self._chunk(ix // self.cp, iy // self.cp)
        return None if arr is None else int(arr[iy % self.cp, ix % self.cp])

    def is_water(self, tx: float, ty: float) -> bool:
        return self._idx(tx, ty) == 0

    def water_state(self, tx: float, ty: float) -> bool | None:
        """Tri-state: True = rendered sea-blue, False = land, None = unknown (the
        chunk is untiled or unreadable, so we can't see the colour). Callers that
        place mobs treat None as "not appropriate" — never drift onto a tile whose
        colour we can't verify against what the player sees."""
        i = self._idx(tx, ty)
        return None if i is None else (i == 0)

    def biome(self, tx: float, ty: float) -> str:
        i = self._idx(tx, ty)
        return "grass" if i is None else BIOMES[i]

    def water_frac(self, cx: int, cy: int, r: int) -> float:
        """Fraction of water in the (2r+1)x(2r+1) block — for telling solid ground /
        open sea from a 1-tile coastal sliver."""
        n = tot = 0
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                tot += 1
                if self.is_water(cx + dx, cy + dy):
                    n += 1
        return n / tot

    def is_open_water(self, tx: float, ty: float) -> bool:
        ix, iy = int(tx) % self.W, max(0, min(self.H - 1, int(ty)))
        return self.is_water(ix, iy) and self.water_frac(ix, iy, 3) >= 0.6

    def _nearest(self, ix: int, iy: int, ok, max_r: int):
        if ok(ix, iy):
            return ix, iy
        for r in range(1, max_r):
            for dx in range(-r, r + 1):  # top & bottom edges of the ring
                for dy in (-r, r):
                    if 0 <= iy + dy < self.H and ok(ix + dx, iy + dy):
                        return (ix + dx) % self.W, iy + dy
            for dy in range(-r + 1, r):  # left & right edges
                for dx in (-r, r):
                    if 0 <= iy + dy < self.H and ok(ix + dx, iy + dy):
                        return (ix + dx) % self.W, iy + dy
        return None

    def nearest_land(self, tx: float, ty: float, max_r: int = 400) -> tuple[int, int]:
        """Nearest *solid* land tile: first the nearest dry tile, then push inland a
        little to ground whose 3x3 is mostly land — so a marker doesn't sit on a
        1-tile coastal sliver (which still reads as sea)."""
        ix, iy = int(tx) % self.W, int(ty)
        base = self._nearest(ix, iy, lambda x, y: not self.is_water(x, y), max_r)
        if base is None:
            return ix, iy
        solid = self._nearest(base[0], base[1],
                              lambda x, y: not self.is_water(x, y) and self.water_frac(x, y, 1) <= 0.12,
                              60)
        return solid or base
