"""Coarse global terrain for the world-map game: land/water, elevation and a
simple biome sampled at ~8 km from the GEBCO topo tiles + the Blue Marble
overview. Coarse on purpose — the *fine* land/water for smooth movement comes
from the client (it samples the rendered chunk colour); this drives what you can
**gather** and where you can **build**.

Built once in a background thread so server start isn't blocked; `ready` flips
true when done. Resources: stone (mountains), wood (vegetation), food (other
land), None on water (fishing needs a boat — later).
"""
from __future__ import annotations

import glob

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

CELL = 16  # world tiles per terrain cell (500 m * 16 = 8 km)

# Diverse resources by biome (like the test-map game): the staple repeats so it
# dominates, with rarer specials mixed in. Picked per tile, deterministically.
_WATER_RES = ["fish", "fish", "fish", "reeds", "clay"]
_PEAK_RES = ["ore", "ore", "obsidian", "flint", "stone"]
_HILL_RES = ["stone", "stone", "stone", "flint", "clay"]
_FOREST_RES = ["wood", "wood", "wood", "herbs", "mushrooms", "amber", "game"]
_GRASS_RES = ["food", "food", "olives", "grapes", "herbs", "flax", "game"]


class WorldTerrain:
    def __init__(self, world_w: int, world_h: int, topo_dir: str, marble_8km: str):
        self.W, self.H = world_w, world_h
        self.cw, self.ch = world_w // CELL, world_h // CELL  # 5400 x 2700
        self.topo_dir, self.marble_8km = topo_dir, marble_8km
        self.ready = False
        self.water = self.elev = self.veg = self.waterf = None

    def build(self) -> None:
        cw, ch = self.cw, self.ch
        qw, qh = cw // 4, ch // 2  # cells per GEBCO quadrant (1350 x 1350)
        elev = np.zeros((ch, cw), np.uint8)
        waterf = np.zeros((ch, cw), np.float32)
        for ri, rd in enumerate("12"):
            for ci, cl in enumerate("ABCD"):
                key = cl + rd
                hits = [f for f in glob.glob(self.topo_dir + "/*")
                        if f"_{key}_" in f and f.lower().endswith((".jpg", ".png"))]
                if not hits:
                    continue
                a = np.asarray(Image.open(hits[0]).convert("L"), dtype=np.uint8)
                ys, xs = slice(ri * qh, (ri + 1) * qh), slice(ci * qw, (ci + 1) * qw)
                elev[ys, xs] = np.asarray(Image.fromarray(a).resize((qw, qh), Image.BILINEAR))
                # water = sea-level (topo == 0); downsample the *mask* and take the
                # per-cell sea fraction, so low land isn't swallowed by averaging.
                sea = Image.fromarray((a == 0).astype(np.uint8) * 255)
                waterf[ys, xs] = np.asarray(sea.resize((qw, qh), Image.BILINEAR), np.float32) / 255.0
        m = np.asarray(Image.open(self.marble_8km).convert("RGB").resize((cw, ch)))
        R, G, B = (m[:, :, i].astype(int) for i in range(3))
        self.elev = elev
        self.waterf = waterf                          # per-cell sea fraction (0..1)
        self.water = waterf > 0.6                     # mostly-sea cells
        self.veg = (G > R) & (G >= B) & (G > 40) & ~self.water  # green vegetation
        self.ready = True

    def _cell(self, x: float, y: float) -> tuple[int, int]:
        return int(x) // CELL % self.cw, min(self.ch - 1, max(0, int(y) // CELL))

    def is_water(self, x: float, y: float) -> bool:
        if not self.ready:
            return False
        cx, cy = self._cell(x, y)
        return bool(self.water[cy, cx])

    def wet(self, x: float, y: float) -> bool:
        """Coast-aware: any meaningful sea fraction in the cell. Land NPCs avoid
        these so they don't wade onto the (coast-grabbed) shoreline."""
        if not self.ready:
            return False
        cx, cy = self._cell(x, y)
        return bool(self.waterf[cy, cx] > 0.3)

    def resource_at(self, x: float, y: float) -> str | None:
        if not self.ready:
            return None
        cx, cy = self._cell(x, y)
        if self.water[cy, cx]:
            pool = _WATER_RES                       # sea / coast (needs a boat)
        else:
            e = self.elev[cy, cx]
            if e > 170:
                pool = _PEAK_RES                    # high peaks — ore, obsidian
            elif e > 110:
                pool = _HILL_RES                    # mountains — stone, flint
            elif self.veg[cy, cx]:
                pool = _FOREST_RES                  # forest — wood, herbs, amber, game
            else:
                pool = _GRASS_RES                   # plains — food, olives, grapes, flax
        return pool[((int(x) * 73856093) ^ (int(y) * 19349663)) % len(pool)]
