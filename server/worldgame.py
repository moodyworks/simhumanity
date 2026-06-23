"""Lightweight multiplayer state for the world map (the chunked real-Earth game).

Distinct from the test-map `World`: a player's position is a **global tile
coordinate** on the 86400x43200 world. Tracks presence, inventory, gathered
resources and built structures so players share a world. Terrain (what you can
gather / where you can build) comes from `WorldTerrain`.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from .world import ERA_DATES, ERA_ORDER

START_YEAR = -2000  # the spawn era (matches the spawn-city picker default)
YEARS_PER_SEC = float(os.environ.get("WORLD_YEARS_PER_SEC", "4"))


def era_index_for(year: int) -> int:
    for i, era in enumerate(ERA_ORDER):
        if year < ERA_DATES[era][1]:
            return i
    return len(ERA_ORDER) - 1

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
        self.ruins: dict[tuple[int, int], dict] = {}
        self.t0 = time.time()
        self.era = era_index_for(START_YEAR)

    @property
    def year(self) -> int:
        return min(5000, int(START_YEAR + (time.time() - self.t0) * YEARS_PER_SEC))

    def era_name(self) -> str:
        return ERA_ORDER[self.era]

    def tick(self) -> None:
        """Advance the clock; when the era turns, prior-era structures crumble into
        ruins — the Living-History loop: your works become the next age's dig sites."""
        ei = era_index_for(self.year)
        if ei > self.era:
            self.era = ei
            for xy, s in list(self.structures.items()):
                if s["era"] < self.era:
                    self.ruins[xy] = {"kind": s["kind"], "builder": s["name"],
                                      "era": s["era"], "found_by": set()}
                    del self.structures[xy]

    def dig(self, pid: str) -> str | None:
        """Excavate a ruin under the player: recover its materials + an artifact and
        reveal who built it (the true record, before the Myth Engine distorts it)."""
        p = self.players.get(pid)
        if not p:
            return None
        ruin = self.ruins.get((round(p.x), round(p.y)))
        if not ruin:
            return "nothing"
        if pid in ruin["found_by"]:
            return "again"
        ruin["found_by"].add(pid)
        for k, v in BUILDS.get(ruin["kind"], {}).items():
            p.inv[k] = p.inv.get(k, 0) + v
        p.inv["artifact"] = p.inv.get("artifact", 0) + 1
        return (f"You unearth a {ruin['kind']} raised by {ruin['builder']} in the "
                f"{ERA_ORDER[ruin['era']]} age.")

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
        self.structures[(tx, ty)] = {"kind": kind, "pid": pid, "name": p.name,
                                     "era": self.era}
        return kind

    def snapshot(self) -> dict:
        return {
            "players": [{"pid": p.pid, "name": p.name, "x": round(p.x, 1),
                         "y": round(p.y, 1), "city": p.city} for p in self.players.values()],
            "structures": [{"x": x, "y": y, "kind": s["kind"]}
                           for (x, y), s in self.structures.items()],
            "ruins": [{"x": x, "y": y, "kind": r["kind"]}
                      for (x, y), r in self.ruins.items()],
            "year": self.year,
            "era": self.era_name(),
        }
