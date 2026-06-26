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


class RenderedTiles:
    def __init__(self, tiles_dir, manifest: dict, cap: int = 400) -> None:
        self.dir = Path(tiles_dir)
        self.cp = int(manifest["chunk_px"])
        self.W = int(manifest["src_w"])
        self.H = int(manifest["src_h"])
        self.ext = manifest.get("ext", "jpg")
        self._cap = cap
        self._masks: OrderedDict[tuple[int, int], np.ndarray | None] = OrderedDict()

    def _mask(self, col: int, row: int):
        key = (col, row)
        if key in self._masks:
            self._masks.move_to_end(key)
            return self._masks[key]
        try:
            a = np.asarray(Image.open(self.dir / f"c{col}_r{row}.{self.ext}").convert("RGB"))
            r, g, b = a[:, :, 0].astype(np.int16), a[:, :, 1].astype(np.int16), a[:, :, 2].astype(np.int16)
            mask = (b > r + 20) & (b > g) & (b > 100)  # the client's sea-blue test
        except Exception:
            mask = None
        self._masks[key] = mask
        if len(self._masks) > self._cap:
            self._masks.popitem(last=False)
        return mask

    def available(self) -> bool:
        return self._mask(0, 0) is not None

    def is_water(self, tx: float, ty: float) -> bool:
        ix, iy = int(tx) % self.W, max(0, min(self.H - 1, int(ty)))
        m = self._mask(ix // self.cp, iy // self.cp)
        return False if m is None else bool(m[iy % self.cp, ix % self.cp])

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
