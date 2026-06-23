"""Lightweight multiplayer state for the world map (the chunked real-Earth game).

Distinct from the test-map `World`: a player's position is a **global tile
coordinate** on the 86400x43200 world. Tracks presence, inventory, gathered
resources and built structures so players share a world. Terrain (what you can
gather / where you can build) comes from `WorldTerrain`.
"""
from __future__ import annotations

import math
import os
import random
import time
from dataclasses import dataclass, field

from .world import ERA_DATES, ERA_ORDER

# NPCs spawn around players (the world is too big to simulate globally).
NPC_KINDS = {
    "wanderer": {"hp": 6, "speed": 9, "chase": False, "dmg": 0, "loot": {"food": 1}},
    "merchant": {"hp": 8, "speed": 7, "chase": False, "dmg": 0, "loot": {"food": 2}},
    "brigand": {"hp": 10, "speed": 13, "chase": True, "dmg": 2, "loot": {"artifact": 1}},
    "monster": {"hp": 14, "speed": 16, "chase": True, "dmg": 3, "loot": {"artifact": 1, "stone": 1}},
}
SPAWN_KINDS, SPAWN_W = zip(("wanderer", 4), ("merchant", 2), ("brigand", 3), ("monster", 2))
VISION = 18          # tiles a chaser sees/aggros within
NEAR = 100           # keep/spawn NPCs within this of a player
MAX_PER_PLAYER = 6
ATTACK_RANGE = 2.5   # melee reach (player <-> NPC)
PLAYER_DMG = 4
PLAYER_HP = 20

START_YEAR = -2000  # the spawn era (matches the spawn-city picker default)
YEARS_PER_SEC = float(os.environ.get("WORLD_YEARS_PER_SEC", "4"))


def era_index_for(year: int) -> int:
    for i, era in enumerate(ERA_ORDER):
        if year < ERA_DATES[era][1]:
            return i
    return len(ERA_ORDER) - 1

# Simple starter build set (the fuller plan/era system is ported later). "boat"
# is crafted to the inventory (it lets you cross water) rather than placed.
BUILDS: dict[str, dict[str, int]] = {
    "hut": {"wood": 5},
    "cairn": {"stone": 5},
    "granary": {"wood": 4, "food": 3},
    "boat": {"wood": 8},
}
PRICES = {"wood": 2, "stone": 3, "food": 1, "artifact": 25}  # a merchant buys these
TRADE_RANGE = 4.0


@dataclass
class NPC:
    nid: int
    kind: str
    x: float
    y: float
    hp: int
    cd: float = 0.0  # attack cooldown (s)


@dataclass
class WorldPlayer:
    pid: str
    name: str
    x: float
    y: float
    city: str = ""
    inv: dict = field(default_factory=dict)
    hp: int = PLAYER_HP
    max_hp: int = PLAYER_HP
    sx: float = 0.0  # spawn point (respawn here on death)
    sy: float = 0.0
    seen: float = field(default_factory=time.time)


class WorldGame:
    def __init__(self) -> None:
        self.players: dict[str, WorldPlayer] = {}
        self.structures: dict[tuple[int, int], dict] = {}
        self.ruins: dict[tuple[int, int], dict] = {}
        self.npcs: dict[int, NPC] = {}
        self.events: list[dict] = []  # per-player notices (respawn) drained by main
        self._nid = 0
        self.t0 = time.time()
        self.era = era_index_for(START_YEAR)

    @property
    def year(self) -> int:
        return min(5000, int(START_YEAR + (time.time() - self.t0) * YEARS_PER_SEC))

    def era_name(self) -> str:
        return ERA_ORDER[self.era]

    def tick(self, dt: float = 0.0, terrain=None) -> None:
        """Advance the clock; when the era turns, prior-era structures crumble into
        ruins — the Living-History loop: your works become the next age's dig sites.
        Then step the NPCs that live around players."""
        ei = era_index_for(self.year)
        if ei > self.era:
            self.era = ei
            for xy, s in list(self.structures.items()):
                if s["era"] < self.era:
                    self.ruins[xy] = {"kind": s["kind"], "builder": s["name"],
                                      "era": s["era"], "found_by": set()}
                    del self.structures[xy]
        if terrain is not None and getattr(terrain, "ready", False):
            self._update_npcs(dt, terrain)

    # --- NPCs + combat ------------------------------------------------------
    def _nearest_player(self, n: NPC):
        return min(self.players.values(),
                   key=lambda p: math.hypot(p.x - n.x, p.y - n.y), default=None)

    def _step(self, n: NPC, dx: float, dy: float, dist: float, terrain) -> None:
        nx = (n.x + dx * dist) % terrain.W
        ny = min(terrain.H - 1, max(0, n.y + dy * dist))
        if not terrain.is_water(nx, ny):
            n.x, n.y = nx, ny

    def _spawn_near(self, p: WorldPlayer, terrain) -> None:
        for _ in range(8):
            a, r = random.random() * math.tau, 30 + random.random() * (NEAR - 30)
            x, y = (p.x + math.cos(a) * r) % terrain.W, p.y + math.sin(a) * r
            if 0 <= y < terrain.H and not terrain.is_water(x, y):
                kind = random.choices(SPAWN_KINDS, SPAWN_W)[0]
                self._nid += 1
                self.npcs[self._nid] = NPC(self._nid, kind, x, y, NPC_KINDS[kind]["hp"])
                return

    def _respawn(self, p: WorldPlayer) -> None:
        p.hp, p.x, p.y = p.max_hp, p.sx, p.sy
        for k in list(p.inv):
            p.inv[k] //= 2  # lose half your goods when slain
        self.events.append({"pid": p.pid, "kind": "respawn", "x": p.x, "y": p.y, "hp": p.hp})

    def _update_npcs(self, dt: float, terrain) -> None:
        players = list(self.players.values())
        if not players:
            self.npcs.clear()
            return
        for nid, n in list(self.npcs.items()):  # despawn the lonely
            if not any(abs(n.x - p.x) < NEAR and abs(n.y - p.y) < NEAR for p in players):
                del self.npcs[nid]
        for p in players:  # top up the neighbourhood
            near = sum(1 for n in self.npcs.values()
                       if abs(n.x - p.x) < NEAR and abs(n.y - p.y) < NEAR)
            for _ in range(MAX_PER_PLAYER - near):
                self._spawn_near(p, terrain)
        for n in self.npcs.values():
            n.cd = max(0.0, n.cd - dt)
            spec = NPC_KINDS[n.kind]
            tgt = self._nearest_player(n) if spec["chase"] else None
            d = math.hypot(tgt.x - n.x, tgt.y - n.y) if tgt else 1e9
            if tgt and d < VISION:
                if d <= ATTACK_RANGE:
                    if n.cd <= 0 and spec["dmg"]:
                        tgt.hp -= spec["dmg"]
                        n.cd = 1.0
                        if tgt.hp <= 0:
                            self._respawn(tgt)
                else:
                    self._step(n, (tgt.x - n.x) / d, (tgt.y - n.y) / d, spec["speed"] * dt, terrain)
            else:  # idle wander
                a = random.random() * math.tau
                self._step(n, math.cos(a), math.sin(a), spec["speed"] * dt * 0.4, terrain)

    def attack(self, pid: str) -> str | None:
        """Strike the nearest NPC in melee reach; killing it drops loot."""
        p = self.players.get(pid)
        if not p:
            return None
        best, bd = None, ATTACK_RANGE
        for n in self.npcs.values():
            d = math.hypot(n.x - p.x, n.y - p.y)
            if d <= bd:
                best, bd = n, d
        if not best:
            return None
        best.hp -= PLAYER_DMG
        if best.hp <= 0:
            for k, v in NPC_KINDS[best.kind]["loot"].items():
                p.inv[k] = p.inv.get(k, 0) + v
            self.npcs.pop(best.nid, None)
            return f"killed:{best.kind}"
        return f"hit:{best.kind}"

    def dig(self, pid: str) -> dict:
        """Excavate the ruin under the player: recover materials + an artifact. The
        FIRST digger gets the true record (and the caller has the Myth Engine spin a
        legend, stored on the ruin); later diggers get the distorted legend."""
        p = self.players.get(pid)
        if not p:
            return {"status": "none"}
        key = (round(p.x), round(p.y))
        ruin = self.ruins.get(key)
        if not ruin:
            return {"status": "nothing"}
        if pid in ruin["found_by"]:
            return {"status": "again",
                    "text": ruin.get("legend") or "You've already excavated this ruin."}
        ruin["found_by"].add(pid)
        for k, v in BUILDS.get(ruin["kind"], {}).items():
            p.inv[k] = p.inv.get(k, 0) + v
        p.inv["artifact"] = p.inv.get("artifact", 0) + 1
        if ruin.get("legend"):
            return {"status": "myth", "text": ruin["legend"]}
        era = ERA_ORDER[ruin["era"]]
        return {"status": "truth", "key": key, "builder": ruin["builder"],
                "kind": ruin["kind"], "era": era,
                "text": (f"You unearth a {ruin['kind']} raised by {ruin['builder']} "
                         f"in the {era} age — its legend now stirs.")}

    def set_legend(self, key: tuple, legend: str) -> None:
        ruin = self.ruins.get(key)
        if ruin is not None:
            ruin["legend"] = legend

    def trade(self, pid: str) -> dict:
        """Sell your sellable goods to the nearest merchant for coin."""
        p = self.players.get(pid)
        if not p:
            return {"status": "none"}
        merch = min((n for n in self.npcs.values() if n.kind == "merchant"
                     and math.hypot(n.x - p.x, n.y - p.y) <= TRADE_RANGE),
                    key=lambda n: math.hypot(n.x - p.x, n.y - p.y), default=None)
        if merch is None:
            return {"status": "none"}
        earned = sum(p.inv.get(i, 0) * pr for i, pr in PRICES.items())
        for i in PRICES:
            p.inv[i] = 0
        p.inv["coin"] = p.inv.get("coin", 0) + earned
        return {"status": "ok", "earned": earned}

    def join(self, pid: str, name: str, x: float, y: float, city: str) -> None:
        self.players[pid] = WorldPlayer(pid, name, float(x), float(y), city,
                                        sx=float(x), sy=float(y))

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
        if any(p.inv.get(k, 0) < v for k, v in recipe.items()):
            return "cost"
        if kind == "boat":  # crafted to the inventory (lets you cross water)
            for k, v in recipe.items():
                p.inv[k] -= v
            p.inv["boat"] = p.inv.get("boat", 0) + 1
            return "boat"
        tx, ty = round(p.x), round(p.y)
        if terrain.is_water(tx, ty):
            return "water"
        if (tx, ty) in self.structures:
            return "occupied"
        for k, v in recipe.items():
            p.inv[k] -= v
        self.structures[(tx, ty)] = {"kind": kind, "pid": pid, "name": p.name,
                                     "era": self.era}
        return kind

    def snapshot(self) -> dict:
        return {
            "players": [{"pid": p.pid, "name": p.name, "x": round(p.x, 1),
                         "y": round(p.y, 1), "city": p.city, "hp": p.hp, "max_hp": p.max_hp}
                        for p in self.players.values()],
            "structures": [{"x": x, "y": y, "kind": s["kind"]}
                           for (x, y), s in self.structures.items()],
            "ruins": [{"x": x, "y": y, "kind": r["kind"]}
                      for (x, y), r in self.ruins.items()],
            "npcs": [{"id": n.nid, "kind": n.kind, "x": round(n.x, 1), "y": round(n.y, 1),
                      "hp": n.hp} for n in self.npcs.values()],
            "year": self.year,
            "era": self.era_name(),
        }
