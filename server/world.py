"""The world model and the deterministic sim rules that run every tick.

This is the Python-authoritative core: terrain, resources, players, and the
actions they can take. No AI here — this is the cheap, deterministic bulk of
the game. The LLM layer (DeepSeek) sits above this and is called rarely.
"""
from __future__ import annotations

import heapq
import random
from dataclasses import dataclass, field
from enum import Enum

from . import economy
from .cities import CITIES, city_stage
from .entities import (Entity, make_brigand, make_merchant, make_monster,
                       make_wanderer)
from .eventlog import EventLog
from .landmarks import SITES, founded_year, site_questions, to_tile
from .mapdata import LEGEND, build_terrain
from .plans import (COASTAL_PLANS, PLAN_PRICE, PLANS, RUIN_TEACHABLE,
                    SITE_TEACHES, STARTING_PLANS, plan_public)
from .quests import public_claim


class Terrain(str, Enum):
    WATER = "water"
    GRASS = "grass"
    FOREST = "forest"
    HILLS = "hills"
    STONE = "stone"
    DESERT = "desert"
    MOUNTAIN = "mountain"   # impassable rock
    GLACIER = "glacier"     # impassable snow/ice
    PASS = "pass"           # walkable route through the mountains


# What you can harvest from each terrain, and the starting amount.
RESOURCE_BY_TERRAIN: dict[Terrain, tuple[str, int]] = {
    Terrain.FOREST: ("wood", 20),
    Terrain.HILLS: ("stone", 15),
    Terrain.STONE: ("stone", 30),
    Terrain.GRASS: ("forage", 10),
}

# Mountains and glaciers are impassable; passes are the way through.
WALKABLE = {Terrain.GRASS, Terrain.FOREST, Terrain.HILLS, Terrain.STONE,
            Terrain.DESERT, Terrain.PASS}

# The player's sight radius (mirrors the client VISION). A hunter gives up the
# chase once its quarry is beyond this — i.e. out of the player's view.
VISION_TILES = 8

# Single-char terrain codes for compact wire encoding (matches mapdata.LEGEND).
CHAR_BY_TERRAIN = {
    Terrain.WATER: "~", Terrain.GRASS: "g", Terrain.FOREST: "f",
    Terrain.HILLS: "h", Terrain.STONE: "m", Terrain.DESERT: "d",
    Terrain.MOUNTAIN: "M", Terrain.GLACIER: "G", Terrain.PASS: "P",
}

# Scatterable ground items, grouped by where they're found. Picked up by
# standing on the tile and gathering. Mediterranean-flavored.
ITEMS_BY_TERRAIN: dict[Terrain, list[str]] = {
    Terrain.GRASS: ["olives", "grapes", "herbs"],
    Terrain.FOREST: ["herbs", "amber", "mushrooms"],
    Terrain.HILLS: ["flint", "obsidian"],
    Terrain.STONE: ["flint", "obsidian"],
    Terrain.DESERT: ["bones", "flint"],
}
COAST_ITEMS = ["shells", "clay", "reeds"]  # found on land next to the sea

# The season's progression. The world advances through these in order; at each
# boundary the prior age's works decay into diggable ruins.
ERA_ORDER = ["stone", "bronze", "iron", "classical", "feudal",
             "industrial", "atomic", "space"]

# Approximate (start, end) calendar year per era (negative = BC). The in-world
# date moves through these as the season advances.
ERA_DATES = {
    "stone": (-50000, -3300),   # deep prehistory absorbs most of the timeline
    "bronze": (-3300, -1200),
    "iron": (-1200, -500),
    "classical": (-500, 500),
    "feudal": (500, 1500),
    "industrial": (1500, 1900),
    "atomic": (1900, 2000),
    "space": (2000, 5000),
}

# What players can build comes from the discoverable plans catalog.
STRUCTURES = PLANS  # alias: salvage/label lookups share the plan cost/label data


@dataclass
class Structure:
    """Something a player built, standing in the current era."""
    type: str
    builder_pid: str
    builder_name: str
    world_time_built: int
    era_built: str
    stored_coin: int = 0  # coin stashed in a cache (recovered when dug up)


@dataclass
class Ruin:
    """What a Structure decays into once its era passes — diggable content."""
    original_type: str
    builder_pid: str
    builder_name: str
    era_built: str
    world_time_built: int
    excavated: bool = False
    stored_coin: int = 0  # buried treasure (e.g. a cache's death-stash)


@dataclass
class Tile:
    terrain: Terrain
    resource: str | None = None
    amount: int = 0
    structure: Structure | None = None
    ruin: Ruin | None = None
    item: str | None = None  # a one-time ground item to pick up


@dataclass
class Landmark:
    """A real famous ancient site, placed at its true Mediterranean location."""
    name: str
    x: int
    y: int
    era: str
    note: str
    founded: int = -50000  # year the site exists from (hidden before then)
    found_by: set = field(default_factory=set)  # pids who've excavated it

    def to_public(self) -> dict:
        return {"name": self.name, "x": self.x, "y": self.y, "era": self.era,
                "founded": self.founded}


@dataclass
class Player:
    pid: str
    name: str
    x: int
    y: int
    hp: int = 100
    max_hp: int = 100
    coin: int = 25
    lore: int = 0  # Loremaster renown — earned by judging legends correctly
    inventory: dict[str, int] = field(default_factory=dict)
    plans: set = field(default_factory=lambda: set(STARTING_PLANS))
    relics: list = field(default_factory=list)  # site/dig relics with clues
    # Movement: click-to-move path, held heading, run state, sub-tile accumulator.
    path: list[tuple[int, int]] = field(default_factory=list)
    heading: tuple | None = None
    running: bool = False
    move_accum: float = 0.0

    def to_public(self) -> dict:
        return {
            "pid": self.pid,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "hp": self.hp,
            "max_hp": self.max_hp,
            "coin": self.coin,
            "lore": self.lore,
            "inventory": self.inventory,
        }


class World:
    def __init__(
        self,
        log: EventLog,
        *,
        width: int = 48,
        height: int = 48,
        minutes_per_tick: int = 30,
        ticks_per_era: int = 600,
        seed: int = 1,
    ):
        self.log = log
        self.width = width
        self.height = height
        self.minutes_per_tick = minutes_per_tick
        self.ticks_per_era = ticks_per_era
        self.seed = seed
        self.era_index = 0
        self.world_time = 0  # in-world minutes since the season epoch
        self.tick_count = 0
        self.players: dict[str, Player] = {}
        self.tiles: list[list[Tile]] = self._generate()
        # Cached legends keyed by ruin location, so a given ruin tells the same
        # tale to everyone who digs it. Populated by the AI layer via set_myth.
        self.myths: dict[tuple[int, int], str] = {}
        # Truth-vs-myth quests keyed by ruin location. Each entry:
        #   {"builder": str, "claims": [full claim dicts], "resolved": {id: verdict}}
        # Resolution is global per ruin — the culture learns the truth once.
        self.quests: dict[tuple[int, int], dict] = {}
        # Resource tiles changed since the last broadcast. The full grid goes out
        # once at connect (init); per tick we only send these deltas, so a big
        # map doesn't ship thousands of unchanged values every frame.
        self._res_dirty: set[tuple[int, int]] = set()
        # Ground-item changes since the last broadcast (pickups; future spawns).
        self._item_changes: list[dict] = []
        # Famous ancient sites at real coordinates (see landmarks.py).
        self.landmarks: list[Landmark] = []
        self.landmark_at: dict[tuple[int, int], Landmark] = {}
        self._place_landmarks()
        # Major cities that rise and fall on their real historical timeline.
        self.cities: list[dict] = []
        self._place_cities()
        self._update_cities()
        # In-progress site quizzes, keyed by (pid, x, y) → {qid: question}.
        self._site_sessions: dict[tuple, dict] = {}
        # Index of standing structures by tile, for fast ownership/benefit checks.
        self.structures: dict[tuple[int, int], Structure] = {}
        # Living world: wandering folk, merchants, and roaming brigands.
        self.entities: dict[str, Entity] = {}
        self._eid_seq = 0
        self._spawn_entities()
        # Per-tick buffers drained by the server: combat hits (for floating
        # damage) and per-player notices (e.g. death messages).
        self._combat_events: list[dict] = []
        self._notices: list[dict] = []

    @property
    def era(self) -> str:
        return ERA_ORDER[self.era_index]

    def era_year(self) -> int:
        """In-world calendar year (negative = BC), interpolated through the era."""
        start, end = ERA_DATES.get(self.era, (-10000, -3300))
        span = max(1, self.ticks_per_era)
        into = self.tick_count - self.era_index * span
        progress = min(1.0, max(0.0, into / span))
        return round(start + (end - start) * progress)

    def set_year(self, year: int) -> None:
        """Debug: jump the clock to a given year and repopulate time-based assets
        (city stages). Sets era_index + tick_count so era_year() matches."""
        lo = ERA_DATES[ERA_ORDER[0]][0]
        hi = ERA_DATES[ERA_ORDER[-1]][1]
        year = max(lo, min(hi, int(year)))
        span = max(1, self.ticks_per_era)
        for i, era in enumerate(ERA_ORDER):
            start, end = ERA_DATES[era]
            if start <= year <= end:
                self.era_index = i
                progress = (year - start) / (end - start) if end != start else 0.0
                self.tick_count = round(i * span + progress * span)
                break
        self._update_cities()

    # ---- world generation -------------------------------------------------
    def _generate(self) -> list[list[Tile]]:
        """Build the tile grid from the stylized Mediterranean map, then
        scatter ground items. Map dimensions drive the world size."""
        grid = build_terrain()
        self.height = len(grid)
        self.width = len(grid[0])
        tiles: list[list[Tile]] = []
        for row_chars in grid:
            row: list[Tile] = []
            for ch in row_chars:
                terrain = Terrain(LEGEND[ch])
                res = RESOURCE_BY_TERRAIN.get(terrain)
                if res:
                    row.append(Tile(terrain, res[0], res[1]))
                else:
                    row.append(Tile(terrain))
            tiles.append(row)
        self._scatter_items(tiles)
        return tiles

    def _near_water(self, tiles: list[list[Tile]], x: int, y: int) -> bool:
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < self.width and 0 <= ny < self.height:
                if tiles[ny][nx].terrain == Terrain.WATER:
                    return True
        return False

    def _scatter_items(self, tiles: list[list[Tile]]) -> None:
        rng = random.Random(self.seed ^ 0x17E45)
        for y in range(self.height):
            for x in range(self.width):
                tile = tiles[y][x]
                if tile.terrain not in WALKABLE:
                    continue  # no items on water, mountains or glaciers
                # Coastlines first — shells, clay, reeds by the sea.
                if self._near_water(tiles, x, y) and rng.random() < 0.10:
                    tile.item = rng.choice(COAST_ITEMS)
                    continue
                pool = ITEMS_BY_TERRAIN.get(tile.terrain)
                if pool and rng.random() < 0.06:
                    tile.item = rng.choice(pool)

    def _nearest_land(self, x: int, y: int, max_r: int = 14) -> tuple[int, int] | None:
        if 0 <= x < self.width and 0 <= y < self.height and self._walkable(x, y):
            return x, y
        best = None
        for r in range(1, max_r + 1):
            for yy in range(y - r, y + r + 1):
                for xx in range(x - r, x + r + 1):
                    if self._walkable(xx, yy):
                        d = (xx - x) ** 2 + (yy - y) ** 2
                        if best is None or d < best[0]:
                            best = (d, xx, yy)
            if best:
                return best[1], best[2]
        return None

    def _place_landmarks(self) -> None:
        """Map each real site to a tile, snapped to the nearest land, no dupes."""
        for site in SITES:
            if "tile" in site:  # hand-pinned where the stylized coast misleads
                tx, ty = site["tile"]
            else:
                tx, ty = to_tile(site["lon"], site["lat"], self.width, self.height)
            land = self._nearest_land(tx, ty)
            if not land:
                continue
            x, y = land
            # Avoid stacking two sites on one tile.
            if (x, y) in self.landmark_at:
                bumped = self._nearest_land(x + 2, y + 2)
                if not bumped or bumped in self.landmark_at:
                    continue
                x, y = bumped
            lm = Landmark(name=site["name"], x=x, y=y, era=site["era"],
                          note=site["note"], founded=founded_year(site["era"]))
            self.landmarks.append(lm)
            self.landmark_at[(x, y)] = lm
            self.tiles[y][x].item = None  # keep the site tile visually clean

    def landmarks_public(self) -> list[dict]:
        return [lm.to_public() for lm in self.landmarks]

    def _place_cities(self) -> None:
        for c in CITIES:
            if "tile" in c:  # hand-pinned where the stylized coast misleads
                land = self._nearest_land(*c["tile"], max_r=4) or c["tile"]
            else:
                tx, ty = to_tile(c["lon"], c["lat"], self.width, self.height)
                land = self._nearest_land(tx, ty, max_r=18)
            if not land:
                continue
            self.cities.append({"name": c["name"], "x": land[0], "y": land[1],
                                "timeline": c["timeline"], "stage": 0, "max": 0})

    def _update_cities(self) -> None:
        """Advance each city's stage to match the current in-world year."""
        year = self.era_year()
        for c in self.cities:
            c["stage"] = city_stage(c["timeline"], year)
            c["max"] = max(c["max"], c["stage"])

    def cities_public(self) -> list[dict]:
        return [{"x": c["x"], "y": c["y"], "name": c["name"],
                 "stage": c["stage"], "max": c["max"]} for c in self.cities]

    def excavate_landmark(self, pid: str) -> dict | None:
        """Standing on an ancient site opens its study quiz. The relic is only
        granted once the quiz is answered (see answer_site); abandoning it
        (abandon_site / walking away) leaves the site un-excavated. Returns the
        site + questions, or None if the player isn't on a landmark."""
        p = self.players.get(pid)
        if not p:
            return None
        lm = self.landmark_at.get((p.x, p.y))
        if not lm:
            return None
        if self.era_year() < lm.founded:
            return {"name": lm.name, "x": p.x, "y": p.y, "done": True,
                    "questions": [], "note": "There is nothing here yet — this "
                    "site has not been built in this age.", "era": lm.era}
        base = {"name": lm.name, "era": lm.era, "note": lm.note,
                "x": p.x, "y": p.y}
        if pid in lm.found_by:  # already studied — just re-read the placard
            return {**base, "done": True, "questions": []}
        # Start (or restart) a study session with the site's quiz.
        seed = (p.x * 31 + p.y) ^ (hash(pid) & 0xFFFF)
        qs = site_questions({"name": lm.name, "era": lm.era}, seed)
        self._site_sessions[(pid, p.x, p.y)] = {q["id"]: q for q in qs}
        public_qs = [{"id": q["id"], "text": q["text"], "resolved": False}
                     for q in qs]
        return {**base, "done": False, "questions": public_qs}

    def answer_site(self, pid: str, x: int, y: int, qid: int,
                    guess: bool) -> dict | None:
        p = self.players.get(pid)
        if not p:
            return None
        sess = self._site_sessions.get((pid, x, y))
        if not sess or qid not in sess:
            return {"error": "No study of this site is underway."}
        q = sess[qid]
        if "answered" in q:
            return {"error": "Already answered."}
        q["answered"] = (guess == q["truth"])
        out = {"id": qid, "x": x, "y": y, "correct": q["answered"],
               "basis": q["basis"], "complete": False}
        if all("answered" in qq for qq in sess.values()):
            out["complete"] = True
            lm = self.landmark_at.get((x, y))
            right = sum(1 for qq in sess.values() if qq["answered"])
            del self._site_sessions[(pid, x, y)]
            if lm and pid not in lm.found_by:
                lm.found_by.add(pid)
                self._grant_relic(
                    p, f"Relic of {lm.name}",
                    f"{lm.note} (Recovered at {lm.name}, {lm.era}.)", lm.name)
                out["relic"] = True
                bonus = right  # +1 renown per correct answer
                p.lore += 2 + bonus
                self.log.append(
                    world_time=self.world_time, era=self.era, kind="discover",
                    actor=pid, x=x, y=y,
                    data={"site": lm.name, "correct": right},
                )
                msg = (f"Relic of {lm.name} recovered! "
                       f"({right}/{len(sess)} correct, +{2 + bonus} renown)")
                # The site teaches its fitting build plan.
                taught = SITE_TEACHES.get(lm.name)
                if taught and self.teach_plan(pid, taught):
                    out["learned"] = taught
                    msg += f" You learn to build: {PLANS[taught]['label']}."
                out["result_text"] = msg
        return out

    def abandon_site(self, pid: str, x: int, y: int) -> None:
        """Player walked away mid-quiz — drop the session; site stays un-found."""
        self._site_sessions.pop((pid, x, y), None)

    def _spawn_point(self) -> tuple[int, int]:
        for _ in range(500):
            x = random.randrange(self.width)
            y = random.randrange(self.height)
            if self.tiles[y][x].terrain in WALKABLE:
                return x, y
        return 0, 0

    # ---- player lifecycle -------------------------------------------------
    def add_player(self, pid: str, name: str) -> Player:
        x, y = self._spawn_point()
        p = Player(pid=pid, name=name, x=x, y=y)
        self.players[pid] = p
        self.log.append(
            world_time=self.world_time, era=self.era, kind="spawn",
            actor=pid, x=x, y=y, data={"name": name},
        )
        return p

    def remove_player(self, pid: str) -> None:
        self.players.pop(pid, None)

    # ---- actions (authoritative; client only requests them) ---------------
    def _walkable(self, x: int, y: int) -> bool:
        return (0 <= x < self.width and 0 <= y < self.height
                and self.tiles[y][x].terrain in WALKABLE)

    def _can_enter(self, p: Player, x: int, y: int) -> bool:
        """Per-player passability: land for everyone; water if they have a boat.
        Mountains and glaciers are impassable for all."""
        if not (0 <= x < self.width and 0 <= y < self.height):
            return False
        t = self.tiles[y][x].terrain
        if t in WALKABLE:
            return True
        if t == Terrain.WATER and p.inventory.get("boat", 0) > 0:
            return True
        return False

    def move(self, pid: str, dx: int, dy: int) -> None:
        """Set the held heading; actual stepping is tick-paced (see speeds)."""
        p = self.players.get(pid)
        if not p:
            return
        p.path = []  # a manual heading cancels any click-to-move route
        p.heading = (dx, dy) if (dx or dy) else None

    def set_running(self, pid: str, on: bool) -> None:
        p = self.players.get(pid)
        if p:
            p.running = bool(on)

    def set_goal(self, pid: str, tx: int, ty: int) -> None:
        """Click-to-move: compute a path (per-player passability) for the loop."""
        p = self.players.get(pid)
        if not p:
            return
        if not (0 <= tx < self.width and 0 <= ty < self.height):
            return
        # If the target isn't enterable, aim for the nearest tile that is.
        if not self._can_enter(p, tx, ty):
            best = None
            for radius in range(1, 8):
                for yy in range(ty - radius, ty + radius + 1):
                    for xx in range(tx - radius, tx + radius + 1):
                        if self._can_enter(p, xx, yy):
                            d = (xx - tx) ** 2 + (yy - ty) ** 2
                            if best is None or d < best[0]:
                                best = (d, xx, yy)
                if best:
                    break
            if not best:
                return
            tx, ty = best[1], best[2]
        p.heading = None  # a destination overrides any held heading
        p.path = self._find_path(p, p.x, p.y, tx, ty)

    def _tile_time_cost(self, x: int, y: int) -> float:
        """Time to cross a tile = 1 / movement speed there. Water is slow (a boat
        is half speed), so the pathfinder prefers faster land routes."""
        return 2.0 if self.tiles[y][x].terrain == Terrain.WATER else 1.0

    def _find_path(self, p: Player, sx: int, sy: int, tx: int,
                   ty: int) -> list[tuple[int, int]]:
        """Fastest (least-time) 4-dir path this player can take — Dijkstra over
        tile crossing-times, so it routes around slow water instead of straight
        through it. Excludes the start tile."""
        if (sx, sy) == (tx, ty):
            return []
        dist: dict[tuple[int, int], float] = {(sx, sy): 0.0}
        prev: dict[tuple[int, int], tuple[int, int]] = {}
        pq: list[tuple[float, int, int]] = [(0.0, sx, sy)]
        while pq:
            d, x, y = heapq.heappop(pq)
            if (x, y) == (tx, ty):
                break
            if d > dist.get((x, y), float("inf")):
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if not self._can_enter(p, nx, ny):
                    continue
                nd = d + self._tile_time_cost(nx, ny)
                if nd < dist.get((nx, ny), float("inf")):
                    dist[(nx, ny)] = nd
                    prev[(nx, ny)] = (x, y)
                    heapq.heappush(pq, (nd, nx, ny))
        if (tx, ty) not in prev:
            return []
        path: list[tuple[int, int]] = []
        cur = (tx, ty)
        while cur != (sx, sy):
            path.append(cur)
            cur = prev[cur]
        path.reverse()
        return path

    def _player_speed(self, p: Player) -> float:
        """Tiles per tick. Boats are slow on water; running doubles land speed."""
        on_water = self.tiles[p.y][p.x].terrain == Terrain.WATER
        if on_water:
            return 0.5            # a boat is slower than walking
        return 2.0 if p.running else 1.0

    def _step_once(self, p: Player) -> bool:
        """Take a single tile step along the path or heading. False if blocked."""
        if p.path:
            nx, ny = p.path[0]
            if self._can_enter(p, nx, ny):
                p.x, p.y = nx, ny
                p.path.pop(0)
                return True
            p.path = []
            return False
        if p.heading:
            dx, dy = p.heading
            nx, ny = p.x + dx, p.y + dy
            if self._can_enter(p, nx, ny):
                p.x, p.y = nx, ny
                return True
            return False
        return False

    def _advance_paths(self) -> None:
        """Advance every player by their per-tick speed (walk / run / boat)."""
        for p in self.players.values():
            if not p.path and not p.heading:
                p.move_accum = 0.0
                continue
            p.move_accum += self._player_speed(p)
            while p.move_accum >= 1.0:
                p.move_accum -= 1.0
                if not self._step_once(p):
                    p.move_accum = 0.0
                    break

    # ---- entities: spawning, AI, combat, trade ----------------------------
    def _new_eid(self) -> str:
        self._eid_seq += 1
        return f"e{self._eid_seq}"

    def _rand_land(self, rng: random.Random) -> tuple[int, int]:
        for _ in range(300):
            x, y = rng.randrange(self.width), rng.randrange(self.height)
            if self.tiles[y][x].terrain in WALKABLE:
                return x, y
        return self._spawn_point()

    def _rand_water(self, rng: random.Random) -> tuple[int, int] | None:
        for _ in range(400):
            x, y = rng.randrange(self.width), rng.randrange(self.height)
            if self.tiles[y][x].terrain == Terrain.WATER:
                return x, y
        return None

    def _rand_coast(self, rng: random.Random) -> tuple[int, int] | None:
        for _ in range(400):
            x, y = rng.randrange(self.width), rng.randrange(self.height)
            if (self.tiles[y][x].terrain in WALKABLE
                    and self._near_water(self.tiles, x, y)):
                return x, y
        return None

    def _spawn_entities(self) -> None:
        rng = random.Random(self.seed ^ 0xBEEF)
        self._erng = random.Random(self.seed ^ 0xC0FFEE)
        for i in range(14):
            # Force ~5 of them onto the coast (shipwrights with boat plans);
            # the rest are ordinary inland traders.
            coastal = i < 5
            x, y = (self._rand_coast(rng) if coastal else None) or self._rand_land(rng)
            e = make_merchant(self._new_eid(), x, y, rng)
            e.data["wares"] = list(economy.MERCHANT_WARES)
            if coastal or self._near_water(self.tiles, x, y):
                e.data["plans_for_sale"] = list(COASTAL_PLANS)
                e.name = e.name.replace("the Trader", "the Shipwright")
            self.entities[e.eid] = e
        for _ in range(18):
            x, y = self._rand_land(rng)
            e = make_wanderer(self._new_eid(), x, y, rng)
            self.entities[e.eid] = e
        for _ in range(16):
            x, y = self._rand_land(rng)
            e = make_brigand(self._new_eid(), x, y, rng)
            self.entities[e.eid] = e
        for _ in range(12):  # mythological beasts of the deep
            wxy = self._rand_water(rng)
            if wxy:
                e = make_monster(self._new_eid(), wxy[0], wxy[1], rng)
                self.entities[e.eid] = e

    def _land_step(self, e: Entity, nx: int, ny: int) -> None:
        if (0 <= nx < self.width and 0 <= ny < self.height
                and self.tiles[ny][nx].terrain in WALKABLE):
            e.x, e.y = nx, ny

    def _wander(self, e: Entity) -> None:
        if self._erng.random() < 0.28:
            dx, dy = self._erng.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
            self._land_step(e, e.x + dx, e.y + dy)

    def _water_step(self, e: Entity, nx: int, ny: int) -> None:
        if (0 <= nx < self.width and 0 <= ny < self.height
                and self.tiles[ny][nx].terrain == Terrain.WATER):
            e.x, e.y = nx, ny

    def _wander_water(self, e: Entity) -> None:
        if self._erng.random() < 0.4:
            dx, dy = self._erng.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
            self._water_step(e, e.x + dx, e.y + dy)

    def _on_water(self, p: Player) -> bool:
        return self.tiles[p.y][p.x].terrain == Terrain.WATER

    def _nearest_player(self, x: int, y: int, maxd: int) -> Player | None:
        best = None
        for p in self.players.values():
            d = abs(p.x - x) + abs(p.y - y)
            if d <= maxd and (best is None or d < best[0]):
                best = (d, p)
        return best[1] if best else None

    def _update_entities(self) -> None:
        for e in list(self.entities.values()):
            if e is None:
                continue
            if e.cooldown > 0:
                e.cooldown -= 1
            if e.kind == "brigand":
                self._update_brigand(e)
            elif e.kind == "monster":
                self._update_monster(e)
            else:
                self._wander(e)

    def _chase(self, e: Entity, tgt: Player, stepfn) -> None:
        """Move e toward tgt at its own speed (tiles/tick), stopping when next
        to the target. Random per-mob speed makes some catchable and some not."""
        e.move_accum += e.speed
        while e.move_accum >= 1.0:
            e.move_accum -= 1.0
            if abs(tgt.x - e.x) + abs(tgt.y - e.y) <= 1:
                break  # adjacent — the attack is handled by the caller
            sx = (tgt.x > e.x) - (tgt.x < e.x)
            sy = (tgt.y > e.y) - (tgt.y < e.y)
            before = (e.x, e.y)
            if sx:
                stepfn(e, e.x + sx, e.y)
            if (e.x, e.y) == before and sy:
                stepfn(e, e.x, e.y + sy)
            if (e.x, e.y) == before:
                break  # blocked

    def _update_brigand(self, e: Entity) -> None:
        tgt = None
        if e.target_pid in self.players:
            p = self.players[e.target_pid]
            if abs(p.x - e.x) + abs(p.y - e.y) <= VISION_TILES:
                tgt = p
            else:
                e.target_pid = None  # lost from sight — give up the chase
        if tgt is None:
            p = self._nearest_player(e.x, e.y, e.spot)
            if p:
                e.target_pid = p.pid
                tgt = p
        if tgt is None:
            e.move_accum = 0.0
            self._wander(e)
            return
        if abs(tgt.x - e.x) + abs(tgt.y - e.y) <= 1:
            if e.cooldown == 0:
                self._entity_attacks(e, tgt)
                e.cooldown = 2
        else:
            self._chase(e, tgt, self._land_step)

    def _update_monster(self, e: Entity) -> None:
        """A sea beast hunts only players who are out on the water (in a boat),
        and never leaves the sea — so the shore is always safe from it."""
        tgt = None
        if e.target_pid in self.players:
            p = self.players[e.target_pid]
            if self._on_water(p) and abs(p.x - e.x) + abs(p.y - e.y) <= VISION_TILES:
                tgt = p
            else:
                e.target_pid = None  # out of sight (or reached land) — give up
        if tgt is None:
            best = None
            for p in self.players.values():
                if not self._on_water(p):
                    continue
                d = abs(p.x - e.x) + abs(p.y - e.y)
                if d <= e.spot and (best is None or d < best[0]):
                    best = (d, p)
            if best:
                e.target_pid = best[1].pid
                tgt = best[1]
        if tgt is None:
            e.move_accum = 0.0
            self._wander_water(e)
            return
        if abs(tgt.x - e.x) + abs(tgt.y - e.y) <= 1:
            if e.cooldown == 0:
                self._entity_attacks(e, tgt)
                e.cooldown = 2
        else:
            self._chase(e, tgt, self._water_step)

    def _best_weapon(self, p: Player) -> int:
        return max((economy.WEAPON_ATK[i] for i in p.inventory
                    if p.inventory[i] > 0 and i in economy.WEAPON_ATK), default=0)

    def _best_armour(self, p: Player) -> int:
        return max((economy.ARMOUR_DEF[i] for i in p.inventory
                    if p.inventory[i] > 0 and i in economy.ARMOUR_DEF), default=0)

    def _grant_relic(self, p: Player, name: str, clue: str, source: str) -> None:
        p.relics.append({"id": len(p.relics), "name": name,
                         "clue": clue, "source": source})

    def _entity_attacks(self, e: Entity, p: Player) -> None:
        dmg = max(1, e.atk - self._best_armour(p))  # armour blunts the blow
        p.hp -= dmg
        self._combat_events.append({"x": p.x, "y": p.y, "dmg": dmg})
        self.log.append(world_time=self.world_time, era=self.era, kind="combat",
                        actor=e.eid, x=p.x, y=p.y, data={"dmg": dmg, "vs": p.pid})
        if p.hp <= 0:
            self._player_dies(p, e)

    def _owned(self, pid: str, stype: str) -> list[tuple[int, int]]:
        return [xy for xy, s in self.structures.items()
                if s.builder_pid == pid and s.type == stype]

    def _near_own(self, p: Player, stype: str, r: int = 4) -> tuple[int, int] | None:
        for (x, y) in self._owned(p.pid, stype):
            if abs(x - p.x) + abs(y - p.y) <= r:
                return (x, y)
        return None

    def _player_dies(self, p: Player, killer: Entity | None) -> None:
        # With a cache you lose only 25% (vs 50%) — and that coin is stashed in
        # your nearest cache, to be unearthed by whoever digs the ruin later.
        caches = self._owned(p.pid, "cache")
        if caches:
            lost = p.coin // 4
            p.coin -= lost
            cx, cy = min(caches, key=lambda c: abs(c[0] - p.x) + abs(c[1] - p.y))
            self.structures[(cx, cy)].stored_coin += lost
        else:
            lost = p.coin // 2
            p.coin -= lost
            if killer:
                killer.data["loot_coin"] = killer.data.get("loot_coin", 0) + lost
        if killer:
            killer.target_pid = None
        p.hp = p.max_hp
        p.path = []
        p.heading = None
        # Respawn at your own hut (a home) if you have one, else far away.
        huts = self._owned(p.pid, "hut")
        if huts:
            p.x, p.y = min(huts, key=lambda h: abs(h[0] - p.x) + abs(h[1] - p.y))
            home = " You wake at your hut."
        else:
            p.x, p.y = self._spawn_point()
            home = " You wake far from danger."
        self.log.append(world_time=self.world_time, era=self.era, kind="death",
                        actor=p.pid, x=p.x, y=p.y, data={"lost": lost})
        who = killer.name if killer else "the wilds"
        self._notices.append({"pid": p.pid,
                              "text": f"You were struck down by {who} and lost "
                                      f"{lost} coin.{home}"})

    def attack(self, pid: str) -> dict | None:
        """Strike an adjacent brigand. Killing it drops random loot. Returns
        {"text": ..., "relic": bool} so the server can refresh the relic list."""
        p = self.players.get(pid)
        if not p:
            return None
        for e in list(self.entities.values()):
            if (e and e.kind in ("brigand", "monster")
                    and abs(e.x - p.x) + abs(e.y - p.y) <= 1):
                dmg = 6 + self._best_weapon(p)  # bare fists + best weapon held
                e.hp -= dmg
                self._combat_events.append({"x": e.x, "y": e.y, "dmg": dmg})
                if e.hp <= 0:
                    text, relic = self._kill_loot(p, e)
                    return {"text": text, "relic": relic}
                e.target_pid = pid  # now it fights back
                return {"text": f"You strike the {e.name} ({e.hp}/{e.max_hp})."}
        return {"text": "There is nothing to fight here."}

    def _kill_loot(self, p: Player, e: Entity) -> tuple[str, bool]:
        coin = e.data.get("loot_coin", 0)
        p.coin += coin
        is_monster = e.kind == "monster"
        if is_monster:
            drops = economy.roll_sea_loot(self._erng, self._erng.randint(1, 3))
            relic_chance = economy.MONSTER_RELIC_CHANCE
        else:
            drops = economy.roll_loot(self._erng, self._erng.randint(1, 2))
            relic_chance = economy.RELIC_DROP_CHANCE
        for it in drops:
            p.inventory[it] = p.inventory.get(it, 0) + 1
        gained_relic = False
        if self._erng.random() < relic_chance and self.landmarks:
            site = self._erng.choice(self.landmarks)
            if is_monster:
                name = f"Sea-swallowed relic of {site.name}"
                clue = (f"Cut from the belly of the {e.name}. {site.note} "
                        f"(Lost long ago off {site.name}.)")
            else:
                name = f"Stolen relic of {site.name}"
                clue = (f"Plundered from a grave-robber. {site.note} "
                        f"(Its rightful place is {site.name}.)")
            self._grant_relic(p, name, clue, f"looted:{site.name}")
            gained_relic = True
        del self.entities[e.eid]
        self.log.append(world_time=self.world_time, era=self.era, kind="kill",
                        actor=p.pid, x=e.x, y=e.y,
                        data={"name": e.name, "coin": coin, "drops": drops})
        parts = [f"+{coin} coin"] + drops + (["a relic!"] if gained_relic else [])
        return f"You slay the {e.name}! ({', '.join(parts)})", gained_relic

    # ---- interaction & trade ---------------------------------------------
    def _adjacent_entity(self, p: Player, kind: str | None = None) -> Entity | None:
        best = None
        for e in self.entities.values():
            if not e:
                continue
            if kind and e.kind != kind:
                continue
            d = abs(e.x - p.x) + abs(e.y - p.y)
            if d <= 1 and (best is None or d < best[1]):
                best = (e, d)
        return best[0] if best else None

    def _merchant_view(self, p: Player, e: Entity) -> dict:
        wares = [{"item": w, "price": economy.buy_price(w)}
                 for w in e.data.get("wares", [])]
        sell = [{"item": it, "qty": n, "price": economy.sell_price(it)}
                for it, n in sorted(p.inventory.items()) if n > 0]
        # Some shoreside traders also sell build plans (e.g. the boat plan).
        plans = [{"type": t, "label": PLANS[t]["label"],
                  "price": PLAN_PRICE.get(t, 30), "known": t in p.plans}
                 for t in e.data.get("plans_for_sale", []) if t in PLANS]
        return {"kind": "merchant", "eid": e.eid, "name": e.name,
                "line": e.data.get("line", ""), "wares": wares,
                "sell": sell, "plans": plans, "coin": p.coin}

    def interact(self, pid: str) -> dict | None:
        p = self.players.get(pid)
        if not p:
            return None
        e = self._adjacent_entity(p)
        if not e:
            return None
        if e.kind == "merchant":
            return self._merchant_view(p, e)
        if e.kind == "brigand":
            return {"kind": "brigand", "name": e.name,
                    "line": "The brigand bares steel — there will be no talk."}
        if e.kind == "monster":
            return {"kind": "brigand", "name": e.name,
                    "line": "The beast rises from the deep — flee or fight!"}
        return {"kind": "wanderer", "name": e.name, "line": e.data.get("line", "")}

    def barter(self, pid: str, eid: str, action: str, item: str,
               qty: int = 1) -> dict | None:
        p = self.players.get(pid)
        if not p:
            return None
        e = self.entities.get(eid)
        if not e or e.kind != "merchant" or abs(e.x - p.x) + abs(e.y - p.y) > 1:
            return {"error": "No merchant within reach."}
        qty = max(1, int(qty))
        if action == "buy":
            if item not in e.data.get("wares", []):
                return {"error": "That isn't for sale."}
            price = economy.buy_price(item) * qty
            if p.coin < price:
                return {"error": "Not enough coin."}
            p.coin -= price
            p.inventory[item] = p.inventory.get(item, 0) + qty
            text = f"Bought {qty} {item} for {price} coin."
        elif action == "sell":
            if p.inventory.get(item, 0) < qty:
                return {"error": f"You have no {item} to sell."}
            price = economy.sell_price(item) * qty
            p.inventory[item] -= qty
            if p.inventory[item] <= 0:
                del p.inventory[item]
            p.coin += price
            text = f"Sold {qty} {item} for {price} coin."
        elif action == "plan":
            if item not in e.data.get("plans_for_sale", []):
                return {"error": "This trader doesn't sell that plan."}
            if item in p.plans:
                return {"error": "You already know that plan."}
            price = PLAN_PRICE.get(item, 30)
            if p.coin < price:
                return {"error": "Not enough coin."}
            p.coin -= price
            self.teach_plan(pid, item)
            view = self._merchant_view(p, e)
            view["text"] = f"You buy the {PLANS[item]['label']} plan for {price} coin."
            view["learned"] = item
            return view
        else:
            return {"error": "?"}
        view = self._merchant_view(p, e)
        view["text"] = text
        return view

    def gather(self, pid: str) -> str | None:
        p = self.players.get(pid)
        if not p:
            return None
        tile = self.tiles[p.y][p.x]
        # A ground item takes priority — pick it up (one-time).
        if tile.item:
            item = tile.item
            tile.item = None
            self._item_changes.append({"x": p.x, "y": p.y, "type": None})
            p.inventory[item] = p.inventory.get(item, 0) + 1
            self.log.append(
                world_time=self.world_time, era=self.era, kind="pickup",
                actor=pid, x=p.x, y=p.y, data={"item": item},
            )
            return f"You pick up {item}."
        # Otherwise harvest the tile's renewable resource.
        if not tile.resource or tile.amount <= 0:
            return None
        tile.amount -= 1
        self._res_dirty.add((p.x, p.y))
        p.inventory[tile.resource] = p.inventory.get(tile.resource, 0) + 1
        self.log.append(
            world_time=self.world_time, era=self.era, kind="gather",
            actor=pid, x=p.x, y=p.y, data={"resource": tile.resource},
        )
        return None

    def known_plans(self, pid: str) -> list[dict]:
        p = self.players.get(pid)
        if not p:
            return []
        return [plan_public(t) for t in PLANS if t in p.plans]

    def relics(self, pid: str) -> list[dict]:
        p = self.players.get(pid)
        return list(p.relics) if p else []

    def teach_plan(self, pid: str, plan_type: str) -> bool:
        """Grant a plan; returns True if it was newly learned."""
        p = self.players.get(pid)
        if not p or plan_type not in PLANS or plan_type in p.plans:
            return False
        p.plans.add(plan_type)
        return True

    def build(self, pid: str, structure_type: str) -> str | None:
        p = self.players.get(pid)
        if not p:
            return None
        spec = PLANS.get(structure_type)
        if not spec:
            return "You don't know how to build that."
        if structure_type not in p.plans:
            return f"You haven't discovered the plan for a {spec['label']} yet."
        if structure_type == "boat":
            return self._build_boat(pid, spec)
        tile = self.tiles[p.y][p.x]
        if tile.structure or tile.ruin:
            return "Something already stands here."
        cost: dict[str, int] = spec["cost"]
        missing = [
            f"{need - p.inventory.get(res, 0)} {res}"
            for res, need in cost.items()
            if p.inventory.get(res, 0) < need
        ]
        if missing:
            return f"Not enough materials — need {', '.join(missing)} more."
        for res, need in cost.items():
            p.inventory[res] -= need
        tile.structure = Structure(
            type=structure_type,
            builder_pid=pid,
            builder_name=p.name,
            world_time_built=self.world_time,
            era_built=self.era,
        )
        self.structures[(p.x, p.y)] = tile.structure
        self.log.append(
            world_time=self.world_time, era=self.era, kind="build",
            actor=pid, x=p.x, y=p.y,
            data={"type": structure_type, "builder": p.name},
        )
        boon = {
            "hut": " A home — you'll respawn here and heal nearby.",
            "stone_circle": " A monument — it will earn you renown over time.",
            "cache": " A strongbox — coin you'd lose on death is stashed here, "
                     "for some future digger to unearth.",
        }.get(structure_type, "")
        return f"You raise a {spec['label']}.{boon}"

    def _build_boat(self, pid: str, spec: dict) -> str:
        """A boat is carried, not placed — it lets you cross water. Build by the
        sea (a coast tile or a dock)."""
        p = self.players[pid]
        on_dock = (self.tiles[p.y][p.x].structure
                   and self.tiles[p.y][p.x].structure.type == "dock")
        if not on_dock and not self._near_water(self.tiles, p.x, p.y):
            return "You must build a boat by the water (a coast or a dock)."
        for res, need in spec["cost"].items():
            if p.inventory.get(res, 0) < need:
                short = need - p.inventory.get(res, 0)
                return f"Not enough materials — need {short} more {res}."
        for res, need in spec["cost"].items():
            p.inventory[res] -= need
        p.inventory["boat"] = p.inventory.get("boat", 0) + 1
        self.log.append(
            world_time=self.world_time, era=self.era, kind="build",
            actor=pid, x=p.x, y=p.y, data={"type": "boat", "builder": p.name},
        )
        return "You build a boat — now you can put to sea."

    def dig(self, pid: str) -> dict | None:
        """Excavate the ruin under the player.

        Returns a dict: ``{"text": <true record>, "excavation": <info|None>}``.
        ``excavation`` (when present) carries everything the Myth Engine needs
        to spin a legend; the AI call itself happens in the async layer so the
        deterministic sim stays pure. Returns None only if the player is gone.
        """
        p = self.players.get(pid)
        if not p:
            return None
        tile = self.tiles[p.y][p.x]
        ruin = tile.ruin
        if not ruin or ruin.excavated:
            # Bone sites sometimes hide buried loot (consumes the bones).
            if tile.item == "bones":
                tile.item = None
                self._item_changes.append({"x": p.x, "y": p.y, "type": None})
                if random.random() < 0.4:  # less than even odds
                    drops = economy.roll_loot(self._erng, random.randint(1, 2))
                    for it in drops:
                        p.inventory[it] = p.inventory.get(it, 0) + 1
                    self.log.append(world_time=self.world_time, era=self.era,
                                    kind="dig_loot", actor=pid, x=p.x, y=p.y,
                                    data={"drops": drops})
                    return {"text": f"You dig beneath the bones and unearth "
                                    f"buried loot: {', '.join(drops)}!",
                            "excavation": None}
                return {"text": "You dig beneath the bones, but find only dust.",
                        "excavation": None}
            return {"text": "You dig, but find nothing but dirt.",
                    "excavation": None}
        ruin.excavated = True
        # The dig yields a relic with a clue about who built it and when.
        label0 = PLANS.get(ruin.original_type, {}).get("label", ruin.original_type)
        self._grant_relic(
            p, f"Artifact from a {label0}",
            f"Unearthed at ({p.x},{p.y}). It was raised by {ruin.builder_name} "
            f"in the {ruin.era_built.title()} Age — a clue to a buried life.",
            "excavation")
        # A chance to salvage raw material from what the structure was made of.
        salvage = STRUCTURES.get(ruin.original_type, {}).get("cost", {})
        recovered = ""
        if salvage and random.random() < 0.5:
            res = next(iter(salvage))
            p.inventory[res] = p.inventory.get(res, 0) + 1
            recovered = f" You also salvage 1 {res}."
        # A cache's buried death-stash pays out to whoever digs it up.
        if ruin.stored_coin:
            p.coin += ruin.stored_coin
            recovered += f" You unearth a hoard of {ruin.stored_coin} coin!"
            ruin.stored_coin = 0
        self.log.append(
            world_time=self.world_time, era=self.era, kind="excavate",
            actor=pid, x=p.x, y=p.y,
            data={
                "original_type": ruin.original_type,
                "builder": ruin.builder_name,
                "era_built": ruin.era_built,
            },
        )
        label = STRUCTURES.get(ruin.original_type, {}).get(
            "label", ruin.original_type
        )
        text = (
            f"You unearth a {label} raised by {ruin.builder_name} "
            f"in the {ruin.era_built.title()} Age.{recovered}"
        )
        # A ruin sometimes yields lost knowledge — a new build plan.
        learned = None
        if random.random() < 0.3:
            unknown = [t for t in RUIN_TEACHABLE if t not in p.plans]
            if unknown:
                learned = random.choice(unknown)
                self.teach_plan(pid, learned)
                text += f" Among the ruins you decipher how to build a {PLANS[learned]['label']}!"
        return {
            "text": text,
            "learned": learned,
            "relic": True,
            "excavation": {
                "x": p.x, "y": p.y,
                "original_type": ruin.original_type,
                "builder": ruin.builder_name,
                "builder_pid": ruin.builder_pid,
                "era_built": ruin.era_built,
            },
        }

    # ---- myths (legends generated by the AI layer, cached per ruin) -------
    def get_myth(self, x: int, y: int) -> str | None:
        return self.myths.get((x, y))

    def set_myth(self, x: int, y: int, text: str) -> None:
        self.myths[(x, y)] = text
        self.log.append(
            world_time=self.world_time, era=self.era, kind="myth",
            x=x, y=y, data={"legend": text},
        )

    # ---- truth-vs-myth quests --------------------------------------------
    def set_quest(self, x: int, y: int, builder: str, claims: list[dict]) -> None:
        self.quests[(x, y)] = {"builder": builder, "claims": claims, "resolved": {}}

    def get_quest(self, x: int, y: int) -> dict | None:
        return self.quests.get((x, y))

    def public_claims(self, x: int, y: int) -> list[dict]:
        """Claims as the client should see them (answers hidden until resolved)."""
        q = self.quests.get((x, y))
        if not q:
            return []
        return [public_claim(c, q["resolved"].get(c["id"])) for c in q["claims"]]

    def investigate(
        self, pid: str, x: int, y: int, claim_id: int, guess: bool | None
    ) -> dict | None:
        """Resolve one claim. ``guess`` is the player's true/false call for a
        'judge' claim, or None for a 'hoard' dig. Returns a verdict dict to send
        back, or an ``{"error": ...}`` message. None if the player is gone.
        """
        p = self.players.get(pid)
        if not p:
            return None
        if (p.x, p.y) != (x, y):
            return {"error": "Return to the ruin to investigate its legend."}
        q = self.quests.get((x, y))
        if not q:
            return {"error": "There is no legend to investigate here."}
        claim = next((c for c in q["claims"] if c["id"] == claim_id), None)
        if claim is None:
            return {"error": "No such claim."}
        if claim_id in q["resolved"]:
            return {"error": "That part of the legend is already settled."}

        truth = claim["truth"]
        verdict: dict = {"id": claim_id, "truth": truth, "basis": claim["basis"]}

        if claim["mode"] == "hoard":
            if truth:
                gained = claim.get("reward", {})
                for res, n in gained.items():
                    p.inventory[res] = p.inventory.get(res, 0) + n
                p.lore += 1
                amt = gained.get("artifact", 0)
                verdict["result_text"] = (
                    f"The legend told true — you unearth the buried hoard! "
                    f"(+{amt} artifacts, +1 renown)"
                )
            else:
                p.lore += 1
                verdict["result_text"] = (
                    "You dig deep and find nothing — the hoard was a fable. "
                    "You record the truth. (+1 renown)"
                )
        else:  # judge
            correct = (guess is not None) and (guess == truth)
            verdict["correct"] = correct
            if correct:
                p.lore += 1
                verdict["result_text"] = (
                    "Your judgement matches the record. (+1 renown)"
                )
            else:
                verdict["result_text"] = "Your judgement was mistaken."

        q["resolved"][claim_id] = verdict
        self.log.append(
            world_time=self.world_time, era=self.era, kind="verify",
            actor=pid, x=x, y=y,
            data={"claim": claim_id, "truth": truth,
                  "mode": claim["mode"], "correct": verdict.get("correct")},
        )
        return verdict

    # ---- era progression --------------------------------------------------
    def advance_era(self) -> None:
        """Cross into the next age: the prior era's works decay into ruins."""
        self.era_index += 1
        ruined = 0
        for row in self.tiles:
            for tile in row:
                if tile.structure:
                    s = tile.structure
                    tile.ruin = Ruin(
                        original_type=s.type,
                        builder_pid=s.builder_pid,
                        builder_name=s.builder_name,
                        era_built=s.era_built,
                        world_time_built=s.world_time_built,
                        stored_coin=s.stored_coin,  # cache hoards become buried
                    )
                    tile.structure = None
                    ruined += 1
        self.structures.clear()  # all standing works are now ruins
        self.log.append(
            world_time=self.world_time, era=self.era, kind="era_transition",
            data={"new_era": self.era, "ruins_created": ruined},
        )

    # ---- the tick ---------------------------------------------------------
    def tick(self) -> None:
        self.tick_count += 1
        self.world_time += self.minutes_per_tick
        self._advance_paths()  # click-to-move: one step per player per tick
        self._update_entities()  # wanderers, merchants, hunting brigands
        # Slow out-of-combat HP regen — faster near your own hut (a home).
        if self.tick_count % 10 == 0:
            for p in self.players.values():
                if p.hp < p.max_hp:
                    heal = 4 if self._near_own(p, "hut") else 1
                    p.hp = min(p.max_hp, p.hp + heal)
        # Cities rise and fall with the years.
        if self.tick_count % 10 == 0:
            self._update_cities()
        # Stone circles are monuments — they earn their builder renown over time.
        if self.tick_count % 30 == 0:
            for s in self.structures.values():
                if s.type == "stone_circle" and s.builder_pid in self.players:
                    self.players[s.builder_pid].lore += 1
        # Season clock: advance to the next age once this era's span elapses.
        if (
            self.era_index + 1 < len(ERA_ORDER)
            and self.tick_count >= (self.era_index + 1) * self.ticks_per_era
        ):
            self.advance_era()
        # Slow resource regrowth so the world isn't strip-mined permanently.
        if self.tick_count % 20 == 0:
            for y, row in enumerate(self.tiles):
                for x, tile in enumerate(row):
                    if tile.resource:
                        cap = RESOURCE_BY_TERRAIN[tile.terrain][1]
                        if tile.amount < cap:
                            tile.amount += 1
                            self._res_dirty.add((x, y))

    # ---- serialization for the client ------------------------------------
    def terrain_rows(self) -> list[str]:
        """Compact terrain: one row-string of single-char codes per row."""
        return ["".join(CHAR_BY_TERRAIN[t.terrain] for t in row)
                for row in self.tiles]

    def _sparse_overlays(self) -> tuple[list[dict], list[dict]]:
        """Structures and ruins are rare, so send them as sparse lists."""
        structures: list[dict] = []
        ruins: list[dict] = []
        for y, row in enumerate(self.tiles):
            for x, tile in enumerate(row):
                if tile.structure:
                    structures.append(
                        {"x": x, "y": y, "type": tile.structure.type}
                    )
                if tile.ruin:
                    ruins.append(
                        {"x": x, "y": y, "type": tile.ruin.original_type,
                         "excavated": tile.ruin.excavated}
                    )
        return structures, ruins

    def items_list(self) -> list[dict]:
        """Every ground item — sent in full once at connect (init)."""
        return [
            {"x": x, "y": y, "type": tile.item}
            for y, row in enumerate(self.tiles)
            for x, tile in enumerate(row) if tile.item
        ]

    def pop_item_changes(self) -> list[dict]:
        """Item tiles changed since the last call (type=None means removed)."""
        changes = self._item_changes
        self._item_changes = []
        return changes

    def pop_resource_changes(self) -> list[dict]:
        """Resource tiles changed since the last call; clears the dirty set."""
        changes = [
            {"x": x, "y": y, "amount": self.tiles[y][x].amount}
            for (x, y) in self._res_dirty
        ]
        self._res_dirty.clear()
        return changes

    def pop_combat_events(self) -> list[dict]:
        ev = self._combat_events
        self._combat_events = []
        return ev

    def pop_notices(self) -> list[dict]:
        n = self._notices
        self._notices = []
        return n

    def snapshot(self) -> dict:
        structures, ruins = self._sparse_overlays()
        return {
            "type": "state",
            "era": self.era,
            "year": self.era_year(),
            "world_time": self.world_time,
            "tick": self.tick_count,
            "width": self.width,
            "height": self.height,
            "resource_changes": self.pop_resource_changes(),
            "item_changes": self.pop_item_changes(),
            "structures": structures,
            "ruins": ruins,
            "cities": self.cities_public(),
            "entities": [e.to_public() for e in self.entities.values() if e],
            "combat": self.pop_combat_events(),
            "players": [p.to_public() for p in self.players.values()],
        }
