"""The world model and the deterministic sim rules that run every tick.

This is the Python-authoritative core: terrain, resources, players, and the
actions they can take. No AI here — this is the cheap, deterministic bulk of
the game. The LLM layer (DeepSeek) sits above this and is called rarely.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum

from .eventlog import EventLog
from .landmarks import SITES, site_questions, to_tile
from .mapdata import LEGEND, build_terrain
from .plans import PLANS, RUIN_TEACHABLE, SITE_TEACHES, STARTING_PLANS, plan_public
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
ERA_ORDER = ["stone", "bronze"]

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


@dataclass
class Ruin:
    """What a Structure decays into once its era passes — diggable content."""
    original_type: str
    builder_pid: str
    builder_name: str
    era_built: str
    world_time_built: int
    excavated: bool = False


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
    found_by: set = field(default_factory=set)  # pids who've excavated it

    def to_public(self) -> dict:
        return {"name": self.name, "x": self.x, "y": self.y, "era": self.era}


@dataclass
class Player:
    pid: str
    name: str
    x: int
    y: int
    hp: int = 100
    lore: int = 0  # Loremaster renown — earned by judging legends correctly
    inventory: dict[str, int] = field(default_factory=dict)
    plans: set = field(default_factory=lambda: set(STARTING_PLANS))
    # Click-to-move: queued tiles to walk, advanced one per tick by the sim.
    path: list[tuple[int, int]] = field(default_factory=list)

    def to_public(self) -> dict:
        return {
            "pid": self.pid,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "hp": self.hp,
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
        # In-progress site quizzes, keyed by (pid, x, y) → {qid: question}.
        self._site_sessions: dict[tuple, dict] = {}

    @property
    def era(self) -> str:
        return ERA_ORDER[self.era_index]

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
            lm = Landmark(name=site["name"], x=x, y=y,
                          era=site["era"], note=site["note"])
            self.landmarks.append(lm)
            self.landmark_at[(x, y)] = lm
            self.tiles[y][x].item = None  # keep the site tile visually clean

    def landmarks_public(self) -> list[dict]:
        return [lm.to_public() for lm in self.landmarks]

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
                relic = f"relic of {lm.name}"
                p.inventory[relic] = p.inventory.get(relic, 0) + 1
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

    def move(self, pid: str, dx: int, dy: int) -> None:
        p = self.players.get(pid)
        if not p:
            return
        p.path = []  # a manual step cancels any click-to-move route
        nx, ny = p.x + dx, p.y + dy
        if not self._walkable(nx, ny):
            return
        p.x, p.y = nx, ny

    def set_goal(self, pid: str, tx: int, ty: int) -> None:
        """Click-to-move: compute a walkable path the tick loop will follow."""
        p = self.players.get(pid)
        if not p:
            return
        if not (0 <= tx < self.width and 0 <= ty < self.height):
            return
        # If the target itself isn't walkable (sea, off a coast), aim for the
        # nearest walkable tile to it instead.
        if not self._walkable(tx, ty):
            best = None
            for radius in range(1, 8):
                for yy in range(ty - radius, ty + radius + 1):
                    for xx in range(tx - radius, tx + radius + 1):
                        if self._walkable(xx, yy):
                            d = (xx - tx) ** 2 + (yy - ty) ** 2
                            if best is None or d < best[0]:
                                best = (d, xx, yy)
                if best:
                    break
            if not best:
                return
            tx, ty = best[1], best[2]
        p.path = self._bfs_path(p.x, p.y, tx, ty)

    def _bfs_path(self, sx: int, sy: int, tx: int, ty: int) -> list[tuple[int, int]]:
        """Shortest 4-dir path over walkable tiles, excluding the start tile."""
        if (sx, sy) == (tx, ty):
            return []
        from collections import deque
        prev: dict[tuple[int, int], tuple[int, int]] = {(sx, sy): (sx, sy)}
        q = deque([(sx, sy)])
        while q:
            x, y = q.popleft()
            if (x, y) == (tx, ty):
                break
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if (nx, ny) not in prev and self._walkable(nx, ny):
                    prev[(nx, ny)] = (x, y)
                    q.append((nx, ny))
        if (tx, ty) not in prev:
            return []
        path: list[tuple[int, int]] = []
        cur = (tx, ty)
        while cur != (sx, sy):
            path.append(cur)
            cur = prev[cur]
        path.reverse()
        return path

    def _advance_paths(self) -> None:
        """Move every player one step along their click-to-move route."""
        for p in self.players.values():
            if p.path:
                nx, ny = p.path.pop(0)
                if self._walkable(nx, ny):
                    p.x, p.y = nx, ny
                else:
                    p.path = []

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
        self.log.append(
            world_time=self.world_time, era=self.era, kind="build",
            actor=pid, x=p.x, y=p.y,
            data={"type": structure_type, "builder": p.name},
        )
        return f"You raise a {spec['label']}."

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
            return {"text": "You dig, but find nothing but dirt.",
                    "excavation": None}
        ruin.excavated = True
        # Artifacts seed the future cross-era artifact economy.
        p.inventory["artifact"] = p.inventory.get("artifact", 0) + 1
        # A chance to salvage raw material from what the structure was made of.
        salvage = STRUCTURES.get(ruin.original_type, {}).get("cost", {})
        recovered = ""
        if salvage and random.random() < 0.5:
            res = next(iter(salvage))
            p.inventory[res] = p.inventory.get(res, 0) + 1
            recovered = f" You also salvage 1 {res}."
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
                    )
                    tile.structure = None
                    ruined += 1
        self.log.append(
            world_time=self.world_time, era=self.era, kind="era_transition",
            data={"new_era": self.era, "ruins_created": ruined},
        )

    # ---- the tick ---------------------------------------------------------
    def tick(self) -> None:
        self.tick_count += 1
        self.world_time += self.minutes_per_tick
        self._advance_paths()  # click-to-move: one step per player per tick
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

    def snapshot(self) -> dict:
        structures, ruins = self._sparse_overlays()
        return {
            "type": "state",
            "era": self.era,
            "world_time": self.world_time,
            "tick": self.tick_count,
            "width": self.width,
            "height": self.height,
            "resource_changes": self.pop_resource_changes(),
            "item_changes": self.pop_item_changes(),
            "structures": structures,
            "ruins": ruins,
            "players": [p.to_public() for p in self.players.values()],
        }
