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

from . import economy
from .cities import CITIES, city_stage
from .entities import (BRIGAND_NAMES, MONSTER_NAMES, WANDERER_LINES, _FIRST, _roll_speed)
from .landmarks import SITES, founded_year
from .world import ERA_DATES, ERA_ORDER

# NPCs spawn around players (the world is too big to simulate globally). Stats are
# rolled per mob (ranges) to match the test-map game's variety. Sea **monsters**
# live on the water and only threaten boaters; the shore is safe.
NPC_KINDS = {
    "wanderer": {"hp": (18, 24), "atk": 0,       "spot": 0, "speed": 6,  "water": False},
    "merchant": {"hp": (18, 22), "atk": 0,       "spot": 0, "speed": 5,  "water": False},
    "brigand":  {"hp": (16, 28), "atk": (4, 8),  "spot": 8, "speed": 14, "water": False,
                 "coin": (3, 12), "relic": economy.RELIC_DROP_CHANCE},
    "monster":  {"hp": (40, 75), "atk": (8, 15), "spot": 9, "speed": 9,  "water": True,
                 "coin": (10, 30), "relic": economy.MONSTER_RELIC_CHANCE},
}
SPAWN_KINDS, SPAWN_W = zip(("wanderer", 4), ("merchant", 2), ("brigand", 3), ("monster", 2))
NEAR = 100           # keep/spawn NPCs within this of a player
LEASH = 14           # a hunter gives up if its target gets this far (or leaves its medium)
MAX_PER_PLAYER = 6
ATTACK_RANGE = 2.0   # melee reach (player <-> NPC)
PLAYER_DMG = 6
PLAYER_HP = 20
WATER_SPEED = 0.5    # a boat moves at half pace (so sea monsters can catch you)

# Resource nodes scattered around players, so resources are visible/populated and
# finite per node (they re-seed as you move). Gather harvests the nearest one.
RESOURCE_AMOUNT = 5
MAX_NODES_PER_PLAYER = 14
NODE_NEAR = 80
GATHER_RANGE = 2.5
TALK_RANGE = 3.0

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
PRICES = {"wood": 2, "stone": 3, "food": 1, "fish": 2, "ore": 8, "artifact": 25}
TRADE_RANGE = 4.0


@dataclass
class NPC:
    nid: int
    kind: str
    name: str
    x: float
    y: float
    hp: int
    max_hp: int
    atk: int = 0
    spot: int = 0
    speed: float = 8.0
    water: bool = False        # lives on water (sea monster) vs land
    target: str | None = None  # pid being hunted
    cd: float = 0.0            # attack cooldown (s)
    head: float = 0.0         # wander heading (rad), kept across ticks (no jitter)
    line: str = ""            # what a wanderer/merchant says when you talk


@dataclass
class ResourceNode:
    rid: int
    kind: str    # wood / stone / food / fish / ore
    x: float
    y: float
    amount: int


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
        self.resources: dict[int, ResourceNode] = {}
        self.events: list[dict] = []  # per-player notices (respawn) drained by main
        self._nid = 0
        self._rid = 0
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
            self._update_resources(terrain)

    # --- NPCs + combat ------------------------------------------------------
    def _step(self, n: NPC, dx: float, dy: float, dist: float, terrain) -> bool:
        nx = (n.x + dx * dist) % terrain.W
        ny = min(terrain.H - 1, max(0.0, n.y + dy * dist))
        if terrain.is_water(nx, ny) == n.water:  # stay on your own medium
            n.x, n.y = nx, ny
            return True
        return False

    def _wander(self, n: NPC, dt: float, terrain) -> None:
        """Smooth idle drift: hold a heading, re-pick only occasionally or when
        blocked — so mobs meander instead of jittering every tick."""
        moved = self._step(n, math.cos(n.head), math.sin(n.head), n.speed * dt * 0.35, terrain)
        if not moved or random.random() < 0.03:
            n.head = random.random() * math.tau

    def _spawn_near(self, p: WorldPlayer, terrain) -> None:
        rng = random
        kind = rng.choices(SPAWN_KINDS, SPAWN_W)[0]
        spec = NPC_KINDS[kind]
        for _ in range(8):
            a, r = rng.random() * math.tau, 30 + rng.random() * (NEAR - 30)
            x, y = (p.x + math.cos(a) * r) % terrain.W, p.y + math.sin(a) * r
            if not (0 <= y < terrain.H) or terrain.is_water(x, y) != spec["water"]:
                continue  # sea beasts on water, land mobs on land
            hp = rng.randint(*spec["hp"])
            name = (rng.choice(BRIGAND_NAMES) if kind == "brigand"
                    else rng.choice(MONSTER_NAMES) if kind == "monster"
                    else rng.choice(_FIRST) + (" the Trader" if kind == "merchant" else ""))
            line = ("Wares to sell, coin for your goods — come, look." if kind == "merchant"
                    else rng.choice(WANDERER_LINES) if kind == "wanderer" else "")
            self._nid += 1
            self.npcs[self._nid] = NPC(
                self._nid, kind, name, x, y, hp, hp,
                atk=rng.randint(*spec["atk"]) if spec["atk"] else 0,
                spot=spec["spot"], speed=_roll_speed(rng, spec["speed"]),
                water=spec["water"], head=rng.random() * math.tau, line=line)
            return

    def _respawn(self, p: WorldPlayer) -> None:
        p.hp, p.x, p.y = p.max_hp, p.sx, p.sy
        for k in list(p.inv):
            p.inv[k] //= 2  # lose half your goods when slain
        self.events.append({"pid": p.pid, "kind": "respawn", "x": p.x, "y": p.y, "hp": p.hp})

    def _eligible(self, n: NPC, p: WorldPlayer, terrain) -> bool:
        """A sea monster only hunts players on the water (the shore is safe); a land
        hunter only hunts players on land."""
        return terrain.is_water(p.x, p.y) == n.water

    def _hunt(self, n: NPC, dt: float, terrain) -> None:
        tgt = self.players.get(n.target) if n.target else None
        if (tgt is None or math.hypot(tgt.x - n.x, tgt.y - n.y) > LEASH
                or not self._eligible(n, tgt, terrain)):
            n.target = None  # lost the trail — re-acquire within spot range
            tgt = min((p for p in self.players.values() if self._eligible(n, p, terrain)
                       and math.hypot(p.x - n.x, p.y - n.y) <= n.spot),
                      key=lambda p: math.hypot(p.x - n.x, p.y - n.y), default=None)
            n.target = tgt.pid if tgt else None
        if tgt is None:
            self._wander(n, dt, terrain)
            return
        d = math.hypot(tgt.x - n.x, tgt.y - n.y) or 1.0
        if d <= ATTACK_RANGE:
            if n.cd <= 0:
                tgt.hp -= n.atk
                n.cd = 1.0
                if tgt.hp <= 0:
                    self._respawn(tgt)
        else:
            self._step(n, (tgt.x - n.x) / d, (tgt.y - n.y) / d, n.speed * dt, terrain)

    def _update_npcs(self, dt: float, terrain) -> None:
        players = list(self.players.values())
        if not players:
            self.npcs.clear()
            return
        for nid, n in list(self.npcs.items()):  # despawn the lonely (hysteresis vs spawn)
            if not any(abs(n.x - p.x) < NEAR * 1.6 and abs(n.y - p.y) < NEAR * 1.6 for p in players):
                del self.npcs[nid]
        for p in players:  # top up the neighbourhood
            near = sum(1 for n in self.npcs.values()
                       if abs(n.x - p.x) < NEAR and abs(n.y - p.y) < NEAR)
            for _ in range(MAX_PER_PLAYER - near):
                self._spawn_near(p, terrain)
        for n in self.npcs.values():
            n.cd = max(0.0, n.cd - dt)
            if n.atk:  # a hunter (brigand / sea monster)
                self._hunt(n, dt, terrain)
            else:      # wanderer / merchant — idle drift
                self._wander(n, dt, terrain)

    def attack(self, pid: str) -> str | None:
        """Strike the nearest NPC in reach; a kill drops coin + weighted goods + a
        chance of a relic. Hitting a hunter makes it turn on you."""
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
        best.hp -= PLAYER_DMG  # + best weapon, later
        if best.hp > 0:
            if best.atk and best.target is None:
                best.target = pid
            return f"hit:{best.name}"
        self.npcs.pop(best.nid, None)
        spec, rng = NPC_KINDS[best.kind], random
        if "coin" in spec:
            p.inv["coin"] = p.inv.get("coin", 0) + rng.randint(*spec["coin"])
        if best.kind == "monster":
            for it in economy.roll_sea_loot(rng, rng.randint(1, 3)):
                p.inv[it] = p.inv.get(it, 0) + 1
        elif best.kind == "brigand":
            for it in economy.roll_loot(rng, rng.randint(1, 2)):
                p.inv[it] = p.inv.get(it, 0) + 1
        if rng.random() < spec.get("relic", 0):
            p.inv["relic"] = p.inv.get("relic", 0) + 1
        return f"killed:{best.name}"

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

    def gather(self, pid: str) -> str | None:
        """Harvest the nearest resource node in reach; depletes it."""
        p = self.players.get(pid)
        if not p:
            return None
        best, bd = None, GATHER_RANGE
        for nd in self.resources.values():
            d = math.hypot(nd.x - p.x, nd.y - p.y)
            if d <= bd:
                best, bd = nd, d
        if not best:
            return None
        p.inv[best.kind] = p.inv.get(best.kind, 0) + 1
        best.amount -= 1
        if best.amount <= 0:
            self.resources.pop(best.rid, None)
        return best.kind

    def _update_resources(self, terrain) -> None:
        players = list(self.players.values())
        if not players:
            self.resources.clear()
            return
        for rid, nd in list(self.resources.items()):
            if not any(abs(nd.x - p.x) < NODE_NEAR * 1.6 and abs(nd.y - p.y) < NODE_NEAR * 1.6
                       for p in players):
                del self.resources[rid]
        for p in players:
            near = sum(1 for nd in self.resources.values()
                       if abs(nd.x - p.x) < NODE_NEAR and abs(nd.y - p.y) < NODE_NEAR)
            for _ in range(MAX_NODES_PER_PLAYER - near):
                self._spawn_node(p, terrain)

    def _spawn_node(self, p: WorldPlayer, terrain) -> None:
        for _ in range(8):
            a, r = random.random() * math.tau, 8 + random.random() * (NODE_NEAR - 8)
            x, y = (p.x + math.cos(a) * r) % terrain.W, p.y + math.sin(a) * r
            if not (0 <= y < terrain.H):
                continue
            kind = terrain.resource_at(x, y)
            if kind:
                self._rid += 1
                self.resources[self._rid] = ResourceNode(self._rid, kind, x, y, RESOURCE_AMOUNT)
                return

    def talk(self, pid: str) -> dict | None:
        """Talk to the nearest wanderer/merchant: a wanderer shares a rumour; a
        merchant buys your sellable goods for coin."""
        p = self.players.get(pid)
        if not p:
            return None
        best, bd = None, TALK_RANGE
        for n in self.npcs.values():
            if n.kind in ("wanderer", "merchant"):
                d = math.hypot(n.x - p.x, n.y - p.y)
                if d <= bd:
                    best, bd = n, d
        if best is None:
            return None
        if best.kind == "merchant":
            earned = sum(p.inv.get(i, 0) * pr for i, pr in PRICES.items())
            for i in PRICES:
                p.inv[i] = 0
            p.inv["coin"] = p.inv.get("coin", 0) + earned
            text = (f'{best.name}: "A fair price!"  You sell your goods for {earned} coin.'
                    if earned else f'{best.name}: "Come back with goods to sell."')
            return {"text": text, "traded": True}
        return {"text": f'{best.name}: "{best.line}"', "traded": False}

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
        yr = self.year
        return {
            "players": [{"pid": p.pid, "name": p.name, "x": round(p.x, 1),
                         "y": round(p.y, 1), "city": p.city, "hp": p.hp, "max_hp": p.max_hp}
                        for p in self.players.values()],
            "structures": [{"x": x, "y": y, "kind": s["kind"]}
                           for (x, y), s in self.structures.items()],
            "ruins": [{"x": x, "y": y, "kind": r["kind"]}
                      for (x, y), r in self.ruins.items()],
            "npcs": [{"id": n.nid, "kind": n.kind, "name": n.name,
                      "x": round(n.x, 1), "y": round(n.y, 1), "hp": n.hp,
                      "max_hp": n.max_hp, "hostile": n.target is not None}
                     for n in self.npcs.values()],
            "resources": [{"id": r.rid, "kind": r.kind, "x": round(r.x, 1), "y": round(r.y, 1)}
                          for r in self.resources.values()],
            # real cities rising/falling on their timeline, and the famous ancient
            # sites once they've been founded — the world's living history
            "cities": [{"name": c["name"], "lon": c["lon"], "lat": c["lat"], "stage": st}
                       for c in CITIES if (st := city_stage(c["timeline"], yr)) > 0],
            "sites": [{"name": s["name"], "lon": s["lon"], "lat": s["lat"]}
                      for s in SITES if yr >= founded_year(s["era"])],
            "year": yr,
            "era": self.era_name(),
        }
