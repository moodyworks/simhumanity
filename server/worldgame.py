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
RESOURCE_AMOUNT = 6
MAX_NODES_PER_PLAYER = 40   # plenty scattered around you
NODE_NEAR = 110
GATHER_RANGE = 2.5
TALK_RANGE = 3.0
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
PRICES = {"wood": 2, "stone": 3, "food": 1, "fish": 2, "ore": 8, "artifact": 25}
TRADE_RANGE = 4.0


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
    target: str | None = None  # pid being hunted
    cd: float = 0.0            # attack cooldown (s)
    head: float = 0.0         # wander heading (rad), kept across ticks (no jitter)
    line: str = ""            # what a wanderer/merchant says when you talk
    dest: tuple | None = None  # tile centre currently stepping toward (grid-locked)


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
        self.resources: dict[int, ResourceNode] = {}
        self.events: list[dict] = []  # per-player notices (respawn) drained by main
        self._nid = 0
        self._rid = 0
        self._city_xy: dict[str, tuple] = {}  # cities/sites snapped onto land
        self._site_xy: dict[str, tuple] = {}
        self._snapped = False
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
        self._city_xy = {c["name"]: self._snap_land(terrain, c["lon"], c["lat"]) for c in CITIES}
        self._site_xy = {s["name"]: self._snap_land(terrain, s["lon"], s["lat"]) for s in SITES}
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
        """Debug: relocate a city/site to a tile and persist it (survives restart)."""
        x, y = int(x), int(y)
        if not (0 <= x < WORLD_W and 0 <= y < WORLD_H):
            return None
        tgt = self._city_xy if kind == "city" else self._site_xy if kind == "site" else None
        if tgt is None or name not in tgt:
            return None
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

    def _set_step(self, n: NPC, sdx: int, sdy: int, terrain) -> bool:
        """Aim n.dest at an adjacent tile centre (diagonal then axis fallbacks) if
        it's on this mob's medium and in bounds."""
        cx, cy = math.floor(n.x), math.floor(n.y)
        for ax, ay in ((sdx, sdy), (sdx, 0), (0, sdy)):
            if not ax and not ay:
                continue
            tx, ty = (cx + ax) % terrain.W, cy + ay
            if 0 <= ty < terrain.H and terrain.is_water(tx + 0.5, ty + 0.5) == n.water:
                n.dest = (tx + 0.5, ty + 0.5)
                return True
        return False

    def _wander(self, n: NPC, dt: float, terrain) -> None:
        """Tile-step meander: hold a heading, step tile-to-tile, turn when blocked."""
        if self._advance(n, dt, terrain):  # at a tile centre — choose the next step
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
                self._nid, kind, name, int(x) + 0.5, int(y) + 0.5, hp, hp,
                atk=rng.randint(*spec["atk"]) if spec["atk"] else 0,
                spot=spec["spot"], speed=_roll_speed(rng, spec["speed"]),
                water=spec["water"], head=rng.random() * math.tau, line=line)
            return

    def _respawn(self, p: WorldPlayer) -> None:
        p.hp = p.max_hp
        p.x, p.y = int(p.sx) + 0.5, int(p.sy) + 0.5  # back to your city, tile-centred
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
                "bases": [{"text": q["text"], "truth": q["truth"], "basis": q["basis"]}
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
        self.players[pid] = WorldPlayer(pid, name, float(x), float(y), city,
                                        sx=float(x), sy=float(y),
                                        plans=set(STARTING_PLANS))

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
                self._rid += 1  # snap nodes to tile centres
                self.resources[self._rid] = ResourceNode(
                    self._rid, kind, int(x) + 0.5, int(y) + 0.5, RESOURCE_AMOUNT)
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
                      "max_hp": n.max_hp, "hostile": n.target is not None}
                     for n in self.npcs.values()],
            "resources": [{"id": r.rid, "kind": r.kind, "x": round(r.x, 1), "y": round(r.y, 1)}
                          for r in self.resources.values()],
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
