"""Read the finished world_tiles chunks to test a tile's *rendered* colour.

The 8 km WorldTerrain is too coarse to know whether a single tile draws as land or
sea, and our coastline doesn't match GEBCO exactly — so a city projected from real
lon/lat can land on a rendered-sea tile (e.g. Memphis). This samples the actual
chunk pixels (same sea-blue test the client uses) and nudges a place to the nearest
rendered-land tile. Chunks are gitignored/user-generated; if absent we no-op.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image


class RenderedTiles:
    def __init__(self, tiles_dir, manifest: dict) -> None:
        self.dir = Path(tiles_dir)
        self.cp = int(manifest["chunk_px"])
        self.W = int(manifest["src_w"])
        self.H = int(manifest["src_h"])
        self.ext = manifest.get("ext", "jpg")
        self._cache: dict[tuple[int, int], np.ndarray | None] = {}

    def _chunk(self, col: int, row: int):
        key = (col, row)
        if key not in self._cache:
            try:
                self._cache[key] = np.asarray(
                    Image.open(self.dir / f"c{col}_r{row}.{self.ext}").convert("RGB"))
            except Exception:
                self._cache[key] = None
        return self._cache[key]

    def available(self) -> bool:
        return self._chunk(0, 0) is not None

    def is_water(self, tx: float, ty: float) -> bool:
        ix, iy = int(tx) % self.W, max(0, min(self.H - 1, int(ty)))
        arr = self._chunk(ix // self.cp, iy // self.cp)
        if arr is None:
            return False
        r, g, b = (int(v) for v in arr[iy % self.cp, ix % self.cp][:3])
        return b > r + 20 and b > g and b > 100  # the client's sea-blue test

    def nearest_land(self, tx: float, ty: float, max_r: int = 300) -> tuple[int, int]:
        """The nearest non-water rendered tile by expanding rings (or the original)."""
        ix, iy = int(tx) % self.W, int(ty)
        if not self.is_water(ix, iy):
            return ix, iy
        for r in range(1, max_r):
            for dx in range(-r, r + 1):  # top & bottom edges of the ring
                for dy in (-r, r):
                    if 0 <= iy + dy < self.H and not self.is_water(ix + dx, iy + dy):
                        return (ix + dx) % self.W, iy + dy
            for dy in range(-r + 1, r):  # left & right edges
                for dx in (-r, r):
                    if 0 <= iy + dy < self.H and not self.is_water(ix + dx, iy + dy):
                        return (ix + dx) % self.W, iy + dy
        return ix, iy
