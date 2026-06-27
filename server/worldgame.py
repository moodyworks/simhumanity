"""Lightweight multiplayer state for the world map (the chunked real-Earth game).

Distinct from the test-map `World`: a player's position is a **global tile
coordinate** on the 86400x43200 world. Tracks presence, inventory, gathered
resources and built structures so players share a world. Terrain (what you can
gather / where you can build) comes from `WorldTerrain`.
"""
from __future__ import annotations

import json
import math
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import economy
from .cities import CITIES, city_stage
from .entities import (BRIGAND_NAMES, MONSTER_NAMES, WANDERER_LINES, _FIRST, _roll_speed)
from .landmarks import SITES, founded_year, site_questions
from .plans import PLANS, RUIN_TEACHABLE, SITE_TEACHES, STARTING_PLANS, plan_public
from .quests import build_claims, public_claim
from .world import ERA_DATES, ERA_ORDER

# NPCs spawn around players (the world is too big to simulate globally). Stats are
# rolled per mob (ranges) to match the test-map game's variety. Sea **monsters**
# live on the water and only threaten boaters; the shore is safe.
# Three media: land (default), water (sea monsters), air (flies over both). Friendly
# wanderers/merchants are slow and pause a lot so you can actually reach them.
NPC_KINDS = {
    "wanderer": {"hp": (18, 24), "atk": 0,       "spot": 0, "speed": 5,  "water": False},
    "merchant": {"hp": (18, 22), "atk": 0,       "spot": 0, "speed": 4,  "water": False},
    "brigand":  {"hp": (16, 28), "atk": (4, 8),  "spot": 8, "speed": 12, "water": False,
                 "coin": (3, 12), "relic": economy.RELIC_DROP_CHANCE},
    "monster":  {"hp": (40, 75), "atk": (8, 15), "spot": 9, "speed": 9,  "water": True,
                 "coin": (10, 30), "relic": economy.MONSTER_RELIC_CHANCE},
    "eagle":    {"hp": (12, 18), "atk": 0,       "spot": 0, "speed": 7,  "air": True},
    "roc":      {"hp": (36, 60), "atk": (7, 12), "spot": 9, "speed": 10, "air": True,
                 "coin": (8, 24), "relic": economy.MONSTER_RELIC_CHANCE},
}
SPAWN_KINDS, SPAWN_W = zip(("wanderer", 4), ("merchant", 2), ("brigand", 3),
                           ("monster", 2), ("eagle", 2), ("roc", 1))
NEAR = 100           # keep/spawn NPCs within this of a player
LEASH = 14           # a hunter gives up if its target gets this far (or leaves its medium)
MAX_PER_PLAYER = 8
ATTACK_RANGE = 2.0   # melee reach (player <-> NPC)
PLAYER_DMG = 6
PLAYER_HP = 20
WATER_SPEED = 0.5    # a boat moves at half pace (so sea monsters can catch you)

# Resource nodes scattered around players, so resources are visible/populated and
# finite per node (they re-seed as you move). Gather harvests the nearest one.
# Resources are a DETERMINISTIC field, not random spawns: each grid cell holds one
# node at a fixed, hashed spot, so a place always has the same resources — they're
# already there when you arrive instead of generating as you walk.
RESOURCE_AMOUNT = 1   # one harvest per node, then it regrows
GRID = 20             # tiles per resource cell (one node per cell)
RES_RADIUS = 240      # how far out around you nodes are computed (square half-width)
RESOURCE_REGROW = 90  # seconds a harvested node takes to come back
GATHER_RANGE = 1.5    # must be on or right next to the node (1 square)
TALK_RANGE = 3.0


def _hash32(x: int, y: int) -> int:
    m = 0xFFFFFFFF
    h = (x * 374761393 + y * 668265263) & m
    h = ((h ^ (h >> 13)) * 1274126177) & m
    return (h ^ (h >> 16)) & m
WORLD_W, WORLD_H = 86400, 43200  # tiles (for projecting city/site lon-lat)
WORLD_OVERRIDES = Path(__file__).resolve().parent.parent / "world_place_overrides.json"

START_YEAR = -2000  # the spawn era (matches the spawn-city picker default)
YEARS_PER_SEC = float(os.environ.get("WORLD_YEARS_PER_SEC", "4"))


def era_index_for(year: int) -> int:
    for i, era in enumerate(ERA_ORDER):
        if year < ERA_DATES[era][1]:
            return i
    return len(ERA_ORDER) - 1

# Buildable costs, derived from the shared PLANS catalog (the discoverable tech
# tree). "boat" is crafted to the inventory (it lets you cross water) not placed.
BUILDS = {k: v["cost"] for k, v in PLANS.items()}
RELIC_SITES = ["Göbekli Tepe", "Jericho", "Troy", "Knossos", "Mycenae",
               "Memphis", "Carthage", "Byblos", "Çatalhöyük"]
PRICES = {  # what a merchant pays for goods
    "wood": 2, "stone": 3, "food": 1, "fish": 2, "ore": 8, "artifact": 25,
    "herbs": 3, "mushrooms": 2, "amber": 10, "game": 3, "obsidian": 7,
    "flint": 3, "clay": 2, "olives": 4, "grapes": 4, "flax": 3, "reeds": 1, "bones": 2,
}
# Resources keyed to the *rendered* biome (RenderedTiles.biome) so what you find
# matches the ground you see: sand things in deserts, timber/game in forests, etc.
# The staple repeats so it dominates; specials are rarer.
BIOME_RES = {  # stone & wood appear everywhere (basic materials), just rarer off-biome
    "water":    ["fish", "fish", "fish", "reeds", "clay"],
    "desert":   ["flint", "flint", "stone", "bones", "clay", "obsidian"],
    "forest":   ["wood", "wood", "wood", "stone", "herbs", "mushrooms", "amber", "game"],
    "grass":    ["food", "food", "stone", "wood", "olives", "grapes", "herbs", "flax", "game"],
    "mountain": ["stone", "stone", "stone", "ore", "flint", "obsidian", "wood"],
    "snow":     ["game", "stone", "flint", "wood"],
}
TRADE_RANGE = 4.0
WARE_POOL = ["wood", "stone", "flint", "herbs", "olives", "grapes", "clay",
             "food", "obsidian", "amber", "flax", "fish"]  # what merchants stock


def buy_price(item: str) -> int:
    return max(1, round(PRICES.get(item, 2) * 1.6))  # merchants sell dearer than they buy


def best_weapon(inv: dict) -> int:
    return max((economy.WEAPON_ATK[i] for i in inv if i in economy.WEAPON_ATK), default=0)


def best_armour(inv: dict) -> int:
    return max((economy.ARMOUR_DEF[i] for i in inv if i in economy.ARMOUR_DEF), default=0)


def _make_relic(source: str) -> dict:
    site = random.choice(RELIC_SITES)
    return {"name": f"Relic of {site}", "source": source,
            "clue": f"Its markings hint at {site}."}


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
    air: bool = False          # flies — crosses land and water alike
    target: str | None = None  # pid being hunted
    cd: float = 0.0            # attack cooldown (s)
    head: float = 0.0         # wander heading (rad), kept across ticks (no jitter)
    line: str = ""            # what a wanderer/merchant says when you talk
    dest: tuple | None = None  # tile centre currently stepping toward (grid-locked)
    pause: float = 0.0        # rest timer — friendlies stop-and-go so you can catch them
    stock: list = field(default_factory=list)  # goods a merchant sells


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
    plans: set = field(default_factory=set)  # build recipes you know (the tech tree)
    relics: list = field(default_factory=list)  # named relics with clues
    renown: int = 0  # scholar's renown, earned excavating famous sites
    dug_sites: set = field(default_factory=set)  # site names already studied
    seen: float = field(default_factory=time.time)


class WorldGame:
    def __init__(self) -> None:
        self.players: dict[str, WorldPlayer] = {}
        self.structures: dict[tuple[int, int], dict] = {}
        self.ruins: dict[tuple[int, int], dict] = {}
        self.npcs: dict[int, NPC] = {}
        self.resources: dict[tuple[int, int], ResourceNode] = {}  # deterministic, keyed by tile
        self._res_key = None          # which cells are covered (recompute on change)
        self._res_dirty = False       # resources changed -> broadcast
        self._depleted_until: dict[tuple[int, int], float] = {}  # harvested tile -> regrow time
        self.events: list[dict] = []  # per-player notices (respawn) drained by main
        self._nid = 0
        self._rid = 0
        self._city_xy: dict[str, tuple] = {}  # cities/sites snapped onto land
        self._site_xy: dict[str, tuple] = {}
        self._snapped = False
        self.rendered = None  # RenderedTiles (set by main) — nudges places off rendered sea
        self._site_sessions: dict[str, dict] = {}  # pid -> open excavation quiz
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
            if not self._snapped:
                self._snap_places(terrain)
            self._update_npcs(dt, terrain)
            self._update_resources(terrain)

    def _lonlat_tile(self, lon: float, lat: float) -> tuple[float, float]:
        return (lon + 180.0) / 360.0 * WORLD_W, (90.0 - lat) / 180.0 * WORLD_H

    def _snap_land(self, terrain, lon: float, lat: float) -> tuple[int, int]:
        """Project lon/lat to a tile, nudged to the nearest land if it lands at sea
        (cities are coastal and our coastline ate a little land)."""
        x, y = self._lonlat_tile(lon, lat)
        if not terrain.is_water(x, y):
            return int(x), int(y)
        step = 16 * 3  # ~24 km hops outward
        for ring in range(1, 12):
            for k in range(-ring, ring + 1):
                for dx, dy in ((k, -ring), (k, ring), (-ring, k), (ring, k)):
                    nx, ny = x + dx * step, y + dy * step
                    if 0 <= ny < terrain.H and not terrain.is_water(nx, ny):
                        return int(nx), int(ny)
        return int(x), int(y)

    def _snap_places(self, terrain) -> None:
        def snap(lon, lat):
            if self.rendered is not None and self.rendered.available():
                x, y = self._lonlat_tile(lon, lat)  # precise: nudge off rendered sea
                return self.rendered.nearest_land(x, y)
            return self._snap_land(terrain, lon, lat)  # coarse 8 km fallback
        self._city_xy = {c["name"]: snap(c["lon"], c["lat"]) for c in CITIES}
        self._site_xy = {s["name"]: snap(s["lon"], s["lat"]) for s in SITES}
        for key, xy in self._load_overrides().items():  # debug-placed positions win
            kind, _, nm = key.partition(":")
            tgt = self._city_xy if kind == "city" else self._site_xy
            if nm in tgt:
                tgt[nm] = (int(xy[0]), int(xy[1]))
        self._snapped = True

    @staticmethod
    def _load_overrides() -> dict:
        try:
            return json.loads(WORLD_OVERRIDES.read_text())
        except Exception:
            return {}

    def set_year(self, year: int) -> None:
        """Debug: jump the world clock. Re-bases t0 so `year` reads the target, and
        decays prior-era works into ruins if the jump crosses into a later age."""
        year = max(-50000, min(5000, int(year)))
        self.t0 = time.time() - (year - START_YEAR) / YEARS_PER_SEC
        new_era = era_index_for(year)
        if new_era > self.era:
            for xy, s in list(self.structures.items()):
                if s["era"] < new_era:
                    self.ruins[xy] = {"kind": s["kind"], "builder": s["name"],
                                      "era": s["era"], "found_by": set()}
                    del self.structures[xy]
        self.era = new_era

    def move_place(self, kind: str, name: str, x: int, y: int) -> dict | None:
        """Debug: relocate a city/site to a tile and persist it (survives restart).
        Snaps to the nearest solid land so a click near the coast still lands ashore."""
        x, y = int(x), int(y)
        if not (0 <= x < WORLD_W and 0 <= y < WORLD_H):
            return None
        tgt = self._city_xy if kind == "city" else self._site_xy if kind == "site" else None
        if tgt is None or name not in tgt:
            return None
        r = self._rendered()
        if r is not None:
            x, y = r.nearest_land(x, y)
        tgt[name] = (x, y)
        data = self._load_overrides()
        data[f"{kind}:{name}"] = [x, y]
        try:
            WORLD_OVERRIDES.write_text(json.dumps(data, indent=2, sort_keys=True))
        except Exception:
            pass
        return {"kind": kind, "name": name, "x": x, "y": y}

    def _place(self, name: str, lon: float, lat: float, snapped: dict) -> tuple[int, int]:
        xy = snapped.get(name)
        if xy:
            return xy
        x, y = self._lonlat_tile(lon, lat)
        return int(x), int(y)

    # --- NPCs + combat ------------------------------------------------------
    def _advance(self, n: NPC, dt: float, terrain) -> bool:
        """Move toward n.dest (a tile centre), snapping on arrival. Returns True when
        idle (no dest / just arrived) so the caller can choose the next tile step."""
        if n.dest is None:
            return True
        dx, dy = n.dest[0] - n.x, n.dest[1] - n.y
        if dx > terrain.W / 2:
            dx -= terrain.W
        elif dx < -terrain.W / 2:
            dx += terrain.W
        d = math.hypot(dx, dy)
        step = n.speed * dt
        if d <= step or d < 1e-6:
            n.x, n.y, n.dest = n.dest[0] % terrain.W, n.dest[1], None
            return True
        n.x = (n.x + dx / d * step) % terrain.W
        n.y += dy / d * step
        return False

    def _rendered(self):
        r = self.rendered
        return r if (r is not None and r.available()) else None

    def _water(self, tx: float, ty: float, terrain) -> bool:
        """Does this tile render as water? Use the real chunk colour when we have it
        (matches what the player sees), else the coarse 8 km terrain."""
        r = self._rendered()
        return r.is_water(tx, ty) if r else terrain.is_water(tx, ty)

    def _medium_ok(self, air: bool, water: bool, tx: int, ty: int, terrain) -> bool:
        """Is tile (tx,ty) appropriate ground for a mob of this medium? Air goes
        anywhere. With rendered tiles we test the *exact* pixel colour the player
        sees: a water mob needs open-ish sea (no creeping thin rivers onto land); a
        land mob needs dry land. An **unknown** tile (untiled / unreadable colour)
        is never appropriate — we won't place a mob where we can't verify the
        colour, which is what let land mobs drift into unmapped sea (and vice
        versa). Only with no rendered tiles at all do we fall back to coarse 8 km."""
        if air:
            return True
        r = self._rendered()
        if r is not None:
            st = r.water_state(tx + 0.5, ty + 0.5)  # True water / False land / None unknown
            if st is None:
                return False
            if water:
                # Genuine open ocean only (>=60% water in a 7x7) — NOT the thin rivers
                # and lakes the tile pipeline bakes through the land, nor 1-tile coastal
                # slivers. Those read as blue to the server but look like land on screen
                # (a sea mob sitting on a river line reads as "on land"). The wide
                # neighbourhood vote is also robust to PIL-vs-browser JPEG decode drift.
                return r.is_open_water(tx + 0.5, ty + 0.5)
            return not st
        if water:  # startup only — coarse fallback until chunks are readable
            return terrain.is_water(tx + 0.5, ty + 0.5)
        return not terrain.wet(tx + 0.5, ty + 0.5)

    def _passable(self, n: NPC, tx: int, ty: int, terrain) -> bool:
        """Can this mob stand on tile (tx,ty)?"""
        return self._medium_ok(n.air, n.water, tx, ty, terrain)

    def _set_step(self, n: NPC, sdx: int, sdy: int, terrain) -> bool:
        """Aim n.dest at an adjacent tile centre (diagonal then axis fallbacks) if
        it's on this mob's medium and in bounds."""
        cx, cy = math.floor(n.x), math.floor(n.y)
        for ax, ay in ((sdx, sdy), (sdx, 0), (0, sdy)):
            if not ax and not ay:
                continue
            tx, ty = (cx + ax) % terrain.W, cy + ay
            if 0 <= ty < terrain.H and self._passable(n, tx, ty, terrain):
                n.dest = (tx + 0.5, ty + 0.5)
                return True
        return False

    def _wander(self, n: NPC, dt: float, terrain) -> None:
        """Tile-step meander: hold a heading, step tile-to-tile, turn when blocked.
        Friendlies rest a beat between steps (stop-and-go) so you can catch them."""
        if n.pause > 0:
            n.pause = max(0.0, n.pause - dt)
            return
        if self._advance(n, dt, terrain):  # at a tile centre — choose the next step
            if not n.atk and random.random() < 0.45:  # the unarmed often pause
                n.pause = random.uniform(0.5, 2.2)
                return
            if random.random() < 0.12:
                n.head = random.random() * math.tau
            sdx, sdy = round(math.cos(n.head)), round(math.sin(n.head))
            if not sdx and not sdy:
                sdx = 1
            if self._set_step(n, sdx, sdy, terrain):
                self._advance(n, dt, terrain)
            else:
                n.head = random.random() * math.tau

    def _spawn_near(self, p: WorldPlayer, terrain) -> None:
        rng = random
        kind = rng.choices(SPAWN_KINDS, SPAWN_W)[0]
        spec = NPC_KINDS[kind]
        air, water = spec.get("air", False), spec.get("water", False)
        for _ in range(8):
            a, r = rng.random() * math.tau, 30 + rng.random() * (NEAR - 30)
            x, y = (p.x + math.cos(a) * r) % terrain.W, p.y + math.sin(a) * r
            if not (0 <= y < terrain.H):
                continue
            if not air:  # sea beasts in OPEN water; land mobs on solid rendered land
                r = self._rendered()
                if r is not None:
                    if water:
                        if not r.is_open_water(x, y):
                            continue
                    elif r.water_state(x, y) is not False:  # skip water AND unknown tiles
                        continue
                elif (not terrain.is_water(x, y)) if water else terrain.wet(x, y):
                    continue
            hp = rng.randint(*spec["hp"])
            name = (rng.choice(BRIGAND_NAMES) if kind == "brigand"
                    else rng.choice(MONSTER_NAMES) if kind in ("monster", "roc")
                    else "Great Eagle" if kind == "eagle"
                    else rng.choice(_FIRST) + (" the Trader" if kind == "merchant" else ""))
            line = ("Wares to sell, coin for your goods — come, look." if kind == "merchant"
                    else rng.choice(WANDERER_LINES) if kind == "wanderer" else "")
            self._nid += 1
            self.npcs[self._nid] = NPC(
                self._nid, kind, name, int(x) + 0.5, int(y) + 0.5, hp, hp,
                atk=rng.randint(*spec["atk"]) if spec.get("atk") else 0,
                spot=spec["spot"], speed=_roll_speed(rng, spec["speed"]),
                water=water, air=air, head=rng.random() * math.tau, line=line,
                stock=rng.sample(WARE_POOL, 4) if kind == "merchant" else [])
            return

    def _respawn(self, p: WorldPlayer) -> None:
        p.hp = p.max_hp
        p.x, p.y = int(p.sx) + 0.5, int(p.sy) + 0.5  # back to your city, tile-centred
        for k in list(p.inv):
            p.inv[k] //= 2  # lose half your goods when slain
        self.events.append({"pid": p.pid, "kind": "respawn", "x": p.x, "y": p.y, "hp": p.hp})

    def _eligible(self, n: NPC, p: WorldPlayer, terrain) -> bool:
        """A sea monster only hunts players on the water (the shore is safe); a land
        hunter only hunts players on land; a roc strikes from above, anywhere."""
        if n.air:
            return True
        return self._water(p.x, p.y, terrain) == n.water

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
        ddx = tgt.x - n.x
        if ddx > terrain.W / 2:
            ddx -= terrain.W
        elif ddx < -terrain.W / 2:
            ddx += terrain.W
        d = math.hypot(ddx, tgt.y - n.y)
        if d <= ATTACK_RANGE:
            n.dest = None
            if n.cd <= 0:
                tgt.hp -= max(1, n.atk - best_armour(tgt.inv))  # armour soaks the blow
                n.cd = 1.0
                if tgt.hp <= 0:
                    self._respawn(tgt)
        elif self._advance(n, dt, terrain):  # at a tile — step one tile toward target
            sdx = 0 if abs(ddx) < 0.6 else (1 if ddx > 0 else -1)
            sdy = 0 if abs(tgt.y - n.y) < 0.6 else (1 if tgt.y > n.y else -1)
            self._set_step(n, sdx, sdy, terrain)
            self._advance(n, dt, terrain)

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
        # Safety net: never leave a mob standing off its medium. Movement/spawn are
        # gated, but a chunk loading (unknown -> water), a transient read, or a
        # JPEG-decode threshold flip can still strand one — cull it and the top-up
        # above re-seeds a valid one next tick (air is unrestricted, never culled).
        for nid, n in list(self.npcs.items()):
            if not self._medium_ok(n.air, n.water, math.floor(n.x), math.floor(n.y), terrain):
                del self.npcs[nid]

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
        best.hp -= PLAYER_DMG + best_weapon(p.inv)  # your best blade adds bite
        if best.hp > 0:
            if not best.atk:  # a provoked friendly turns and fights back
                best.atk = random.randint(3, 6)
                best.spot = max(best.spot, 8)
                best.pause = 0.0
            if best.target is None:
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
            p.relics.append(_make_relic(f"looted from {best.name}"))
        return f"killed:{best.name}"

    def dig(self, pid: str) -> dict:
        """Excavate the ruin under the player: recover materials + an artifact. The
        FIRST digger gets the true record (and the caller has the Myth Engine spin a
        legend, stored on the ruin); later diggers get the distorted legend."""
        p = self.players.get(pid)
        if not p:
            return {"status": "none"}
        site = self.start_site(pid)  # standing on a famous site → an excavation quiz
        if site:
            return site
        key = (int(p.x), int(p.y))
        ruin = self.ruins.get(key)
        if not ruin:
            return {"status": "nothing"}
        q = self._ensure_quest(key, ruin)  # the judgeable legend of this ruin (a quest)
        claims = self._public_claims(q)
        if pid in ruin["found_by"]:
            return {"status": "again", "builder": ruin["builder"], "claims": claims,
                    "text": ruin.get("legend") or "You've already excavated this ruin."}
        ruin["found_by"].add(pid)
        for k, v in BUILDS.get(ruin["kind"], {}).items():
            p.inv[k] = p.inv.get(k, 0) + v
        p.inv["artifact"] = p.inv.get("artifact", 0) + 1
        extra = ""  # excavation sometimes reveals a lost building technique
        teachable = [pl for pl in RUIN_TEACHABLE if pl not in p.plans]
        if teachable and random.random() < 0.30:
            learned = random.choice(teachable)
            p.plans.add(learned)
            extra = f" You work out how to build a {PLANS[learned]['label']}!"
        if ruin.get("legend"):
            return {"status": "myth", "builder": ruin["builder"], "claims": claims,
                    "text": ruin["legend"] + extra}
        era = ERA_ORDER[ruin["era"]]
        return {"status": "truth", "key": key, "builder": ruin["builder"],
                "kind": ruin["kind"], "era": era, "claims": claims,
                "text": (f"You unearth a {ruin['kind']} raised by {ruin['builder']} "
                         f"in the {era} age — its legend now stirs.") + extra}

    def set_legend(self, key: tuple, legend: str) -> None:
        ruin = self.ruins.get(key)
        if ruin is not None:
            ruin["legend"] = legend

    # --- truth-vs-myth quests on excavated ruins ----------------------------
    def _ensure_quest(self, key: tuple, ruin: dict) -> dict:
        if "quest" not in ruin:
            ruin["quest"] = {"claims": build_claims(ruin["builder"], [], key[0], key[1]),
                             "resolved": {}}
        return ruin["quest"]

    def _public_claims(self, quest: dict) -> list[dict]:
        return [public_claim(c, quest["resolved"].get(c["id"])) for c in quest["claims"]]

    def investigate(self, pid: str, claim_id: int, guess) -> dict | None:
        """Resolve one legend-claim of the ruin under the player: judge a claim
        true/embellished, or dig a rumoured hoard. Right calls earn renown."""
        p = self.players.get(pid)
        if not p:
            return None
        ruin = self.ruins.get((int(p.x), int(p.y)))
        if not ruin or "quest" not in ruin:
            return {"error": "No legend to investigate here."}
        q = ruin["quest"]
        claim = next((c for c in q["claims"] if c["id"] == claim_id), None)
        if claim is None:
            return {"error": "No such claim."}
        if claim_id in q["resolved"]:
            return {"error": "That part of the legend is already settled."}
        truth = claim["truth"]
        verdict = {"id": claim_id, "truth": truth, "basis": claim["basis"]}
        if claim["mode"] == "hoard":
            if truth:
                for res, n in claim.get("reward", {}).items():
                    p.inv[res] = p.inv.get(res, 0) + n
                p.renown += 1
                amt = claim.get("reward", {}).get("artifact", 0)
                verdict["result_text"] = (f"The legend told true — you unearth the hoard! "
                                          f"(+{amt} artifacts, +1 renown)")
            else:
                p.renown += 1
                verdict["result_text"] = "You dig deep and find nothing — a fable. (+1 renown)"
        else:
            correct = guess is not None and bool(guess) == truth
            verdict["correct"] = correct
            if correct:
                p.renown += 1
                verdict["result_text"] = "Your judgement matches the record. (+1 renown)"
            else:
                verdict["result_text"] = "Your judgement was mistaken."
        q["resolved"][claim_id] = verdict
        return verdict

    # --- famous-site excavation quizzes -------------------------------------
    def _site_near(self, p: WorldPlayer):
        """The founded site the player is standing on (within 2 tiles), or None."""
        tx, ty = int(p.x), int(p.y)
        yr = self.year
        for s in SITES:
            if yr < founded_year(s["era"]):
                continue
            xy = self._site_xy.get(s["name"])
            if xy and abs(xy[0] - tx) <= 2 and abs(xy[1] - ty) <= 2:
                return s
        return None

    def start_site(self, pid: str):
        """Begin (or report) the excavation quiz for the site under the player."""
        p = self.players.get(pid)
        if not p:
            return None
        s = self._site_near(p)
        if not s:
            return None
        if s["name"] in p.dug_sites:
            return {"status": "site_done",
                    "text": f"You have already studied {s['name']}."}
        qs = site_questions(s, len(p.dug_sites) + int(p.x) + int(p.y))
        self._site_sessions[pid] = {"site": s["name"], "q": qs}
        return {"status": "site", "site": s["name"], "note": s.get("note", ""),
                "questions": [{"id": q["id"], "text": q["text"]} for q in qs]}

    def answer_site(self, pid: str, answers: dict):
        """Score the open quiz: claim a relic, renown, and the site's lost plan."""
        p = self.players.get(pid)
        sess = self._site_sessions.pop(pid, None)
        if not p or not sess:
            return None
        qs = sess["q"]
        correct = sum(1 for q in qs
                      if bool(answers.get(str(q["id"]), answers.get(q["id"]))) == q["truth"])
        name = sess["site"]
        p.dug_sites.add(name)
        p.renown += 2 + correct
        p.relics.append({"name": f"Relic of {name}", "source": f"excavated at {name}",
                         "clue": f"Recovered from the ruins of {name}."})
        learned = SITE_TEACHES.get(name)
        if learned and learned not in p.plans:
            p.plans.add(learned)
        else:
            learned = None
        return {"status": "site_result", "site": name, "correct": correct, "total": len(qs),
                "renown": p.renown, "learned": PLANS[learned]["label"] if learned else None,
                "bases": [{"text": q["text"], "truth": q["truth"], "basis": q["basis"],
                           "correct": bool(answers.get(str(q["id"]), answers.get(q["id"]))) == q["truth"]}
                          for q in qs]}

    def leave_site(self, pid: str) -> None:
        """Walk away mid-quiz → abandon it (the site stays re-diggable)."""
        self._site_sessions.pop(pid, None)

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
        # boat from the start — it's a water planet; you need to be able to cross it
        self.players[pid] = WorldPlayer(pid, name, float(x), float(y), city,
                                        sx=float(x), sy=float(y),
                                        plans=set(STARTING_PLANS) | {"boat"})

    def move(self, pid: str, x: float, y: float) -> None:
        p = self.players.get(pid)
        if p:
            p.x, p.y, p.seen = float(x), float(y), time.time()
            sess = self._site_sessions.get(pid)  # walked off the dig → abandon the quiz
            if sess:
                near = self._site_near(p)
                if not near or near["name"] != sess["site"]:
                    self.leave_site(pid)

    def leave(self, pid: str) -> None:
        self.players.pop(pid, None)

    def gather(self, pid: str) -> str | None:
        """Harvest the nearest node in reach; it then regrows after a while."""
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
        tile = (int(best.x), int(best.y))
        self._depleted_until[tile] = time.time() + RESOURCE_REGROW
        self.resources.pop(tile, None)
        self._res_key = None      # recompute so it stays gone until it regrows
        self._res_dirty = True
        return best.kind

    def _cell_node(self, cx: int, cy: int, terrain):
        """The one deterministic node for a grid cell, or None (empty / regrowing)."""
        h = _hash32(cx, cy)
        if h % 7 == 0:  # a few cells are bare, for irregularity
            return None
        nx = cx * GRID + (h % GRID) + 0.5
        ny = cy * GRID + ((h >> 8) % GRID) + 0.5
        if not (0 <= ny < terrain.H):
            return None
        if self._depleted_until.get((int(nx), int(ny)), 0) > time.time():
            return None
        rnd = self._rendered()
        if rnd is not None:
            pool = BIOME_RES.get(rnd.biome(nx, ny))
        else:
            k0 = terrain.resource_at(nx, ny)
            pool = [k0] if k0 else None
        if not pool:
            return None
        kind = pool[_hash32(int(nx), int(ny)) % len(pool)]
        return ResourceNode(0, kind, nx, ny, RESOURCE_AMOUNT)

    def _update_resources(self, terrain) -> None:
        """Recompute the deterministic node field — only when players cross into new
        cells (or a node was harvested), so it's stable as you wander."""
        players = list(self.players.values())
        if not players:
            if self.resources:
                self.resources, self._res_dirty = {}, True
            self._res_key = None
            return
        key = frozenset((int(p.x) // GRID, int(p.y) // GRID) for p in players)
        if key == self._res_key:
            return
        self._res_key = key
        cellr = RES_RADIUS // GRID
        now = time.time()
        self._depleted_until = {t: u for t, u in self._depleted_until.items() if u > now}
        want = {}
        for p in players:
            pcx, pcy = int(p.x) // GRID, int(p.y) // GRID
            for cy in range(pcy - cellr, pcy + cellr + 1):
                if not (0 <= cy * GRID < terrain.H):
                    continue
                for cx in range(pcx - cellr, pcx + cellr + 1):
                    nd = self._cell_node(cx, cy, terrain)
                    if nd is not None:
                        want[(int(nd.x), int(nd.y))] = nd
        self.resources = want
        self._res_dirty = True

    def talk(self, pid: str) -> dict | None:
        """Talk to the nearest wanderer/merchant: a wanderer shares a rumour; a
        merchant opens a buy/sell trade."""
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
            return {"trade": self.trade_info(pid)}
        return {"text": f'{best.name}: "{best.line}"'}

    def _merchant_near(self, p: WorldPlayer):
        return min((n for n in self.npcs.values() if n.kind == "merchant"
                    and math.hypot(n.x - p.x, n.y - p.y) <= TRADE_RANGE),
                   key=lambda n: math.hypot(n.x - p.x, n.y - p.y), default=None)

    def trade_info(self, pid: str) -> dict | None:
        """The buy/sell sheet for the nearest merchant: their wares + what you can
        sell, with prices and your coin."""
        p = self.players.get(pid)
        if not p:
            return None
        m = self._merchant_near(p)
        if m is None:
            return None
        sell = [{"item": i, "price": PRICES[i]} for i in sorted(p.inv)
                if i != "coin" and i in PRICES and p.inv.get(i, 0) > 0]
        buy = [{"item": i, "price": buy_price(i)} for i in m.stock]
        return {"who": m.name, "coin": p.inv.get("coin", 0), "buy": buy, "sell": sell}

    def buy(self, pid: str, item: str) -> dict | None:
        p = self.players.get(pid)
        if not p:
            return None
        m = self._merchant_near(p)
        if m is None:
            return {"text": "No merchant nearby."}
        if item not in m.stock:
            return {"text": "They don't stock that."}
        cost = buy_price(item)
        if p.inv.get("coin", 0) < cost:
            return {"text": f"You need {cost} coin for {item}."}
        p.inv["coin"] = p.inv.get("coin", 0) - cost
        p.inv[item] = p.inv.get(item, 0) + 1
        return {"text": f"Bought {item} for {cost} coin."}

    def sell(self, pid: str, item: str) -> dict | None:
        p = self.players.get(pid)
        if not p:
            return None
        if self._merchant_near(p) is None:
            return {"text": "No merchant nearby."}
        if p.inv.get(item, 0) <= 0 or item not in PRICES:
            return {"text": "Nothing to sell."}
        p.inv[item] -= 1
        if p.inv[item] <= 0:
            del p.inv[item]
        p.inv["coin"] = p.inv.get("coin", 0) + PRICES[item]
        return {"text": f"Sold {item} for {PRICES[item]} coin."}

    def build(self, pid: str, kind: str, terrain) -> str:
        """Place a structure at the player's tile; returns the kind or an error
        code (water / occupied / cost / bad)."""
        p = self.players.get(pid)
        recipe = BUILDS.get(kind)
        if not p or not recipe:
            return "bad"
        if kind not in p.plans:
            return "unknown"
        if any(p.inv.get(k, 0) < v for k, v in recipe.items()):
            return "cost"
        if kind == "boat":  # crafted to the inventory (lets you cross water)
            for k, v in recipe.items():
                p.inv[k] -= v
            p.inv["boat"] = p.inv.get("boat", 0) + 1
            return "boat"
        tx, ty = int(p.x), int(p.y)
        if terrain.is_water(tx, ty):
            return "water"
        if (tx, ty) in self.structures:
            return "occupied"
        for k, v in recipe.items():
            p.inv[k] -= v
        self.structures[(tx, ty)] = {"kind": kind, "pid": pid, "name": p.name,
                                     "era": self.era}
        return kind

    def resources_payload(self) -> dict:
        return {"type": "resources",
                "resources": [{"kind": r.kind, "x": r.x, "y": r.y}
                              for r in self.resources.values()]}

    def snapshot(self) -> dict:
        yr = self.year
        return {
            "players": [{"pid": p.pid, "name": p.name, "x": round(p.x, 1),
                         "y": round(p.y, 1), "city": p.city, "hp": p.hp, "max_hp": p.max_hp}
                        for p in self.players.values()],
            "structures": [{"x": x + 0.5, "y": y + 0.5, "kind": s["kind"]}
                           for (x, y), s in self.structures.items()],
            "ruins": [{"x": x + 0.5, "y": y + 0.5, "kind": r["kind"]}
                      for (x, y), r in self.ruins.items()],
            "npcs": [{"id": n.nid, "kind": n.kind, "name": n.name,
                      "x": round(n.x, 1), "y": round(n.y, 1), "hp": n.hp,
                      "max_hp": n.max_hp, "hostile": n.target is not None, "spot": n.spot}
                     for n in self.npcs.values()],
            # resources are sent separately (deterministic, only on change) via
            # resources_payload(), not every tick.
            # real cities rising/falling on their timeline, and the famous ancient
            # sites once they've been founded — the world's living history
            "cities": [{"name": c["name"], "stage": st,
                        "x": (cxy := self._place(c["name"], c["lon"], c["lat"], self._city_xy))[0] + 0.5,
                        "y": cxy[1] + 0.5}
                       for c in CITIES if (st := city_stage(c["timeline"], yr)) > 0],
            "sites": [{"name": s["name"],
                       "x": (sxy := self._place(s["name"], s["lon"], s["lat"], self._site_xy))[0] + 0.5,
                       "y": sxy[1] + 0.5}
                      for s in SITES if yr >= founded_year(s["era"])],
            "year": yr,
            "era": self.era_name(),
        }
