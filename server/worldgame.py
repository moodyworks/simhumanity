"""Lightweight multiplayer state for the world map (the chunked real-Earth game).

Distinct from the test-map `World`: a player's position is a **global tile
coordinate** on the 86400x43200 world. Tracks presence, inventory, gathered
resources and built structures so players share a world. Terrain (what you can
gather / where you can build) comes from `WorldTerrain`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

# Simple starter build set (the fuller plan/era system is ported later).
BUILDS: dict[str, dict[str, int]] = {
    "hut": {"wood": 5},
    "cairn": {"stone": 5},
    "granary": {"wood": 4, "food": 3},
}


@dataclass
class WorldPlayer:
    pid: str
    name: str
    x: float
    y: float
    city: str = ""
    inv: dict = field(default_factory=dict)
    seen: float = field(default_factory=time.time)


class WorldGame:
    def __init__(self) -> None:
        self.players: dict[str, WorldPlayer] = {}
        self.structures: dict[tuple[int, int], dict] = {}

    def join(self, pid: str, name: str, x: float, y: float, city: str) -> None:
        self.players[pid] = WorldPlayer(pid, name, float(x), float(y), city)

    def move(self, pid: str, x: float, y: float) -> None:
        p = self.players.get(pid)
        if p:
            p.x, p.y, p.seen = float(x), float(y), time.time()

    def leave(self, pid: str) -> None:
        self.players.pop(pid, None)

    def gather(self, pid: str, terrain) -> str | None:
        """Gather the resource under the player; returns the item or None."""
        p = self.players.get(pid)
        if not p:
            return None
        res = terrain.resource_at(p.x, p.y)
        if res:
            p.inv[res] = p.inv.get(res, 0) + 1
        return res

    def build(self, pid: str, kind: str, terrain) -> str:
        """Place a structure at the player's tile; returns the kind or an error
        code (water / occupied / cost / bad)."""
        p = self.players.get(pid)
        recipe = BUILDS.get(kind)
        if not p or not recipe:
            return "bad"
        tx, ty = round(p.x), round(p.y)
        if terrain.is_water(tx, ty):
            return "water"
        if (tx, ty) in self.structures:
            return "occupied"
        if any(p.inv.get(k, 0) < v for k, v in recipe.items()):
            return "cost"
        for k, v in recipe.items():
            p.inv[k] -= v
        self.structures[(tx, ty)] = {"kind": kind, "pid": pid, "name": p.name}
        return kind

    def snapshot(self) -> dict:
        return {
            "players": [{"pid": p.pid, "name": p.name, "x": round(p.x, 1),
                         "y": round(p.y, 1), "city": p.city} for p in self.players.values()],
            "structures": [{"x": x, "y": y, "kind": s["kind"]}
                           for (x, y), s in self.structures.items()],
        }
