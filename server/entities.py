"""Non-player entities: wandering folk, merchants, and roaming brigands.

The Entity model is shared; behaviour (wander / barter / hunt) is driven by the
World each tick. Brigands spot players within a range, give chase, and fight.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

# Flavor names so the world feels peopled.
_FIRST = ["Aelia", "Doru", "Kemsa", "Nuri", "Tariq", "Selka", "Boran", "Yara",
          "Hesper", "Mela", "Cadmus", "Razi", "Sabo", "Ona", "Pirin", "Galla"]
WANDERER_LINES = [
    "The passes are cruel this season — keep your wits on the heights.",
    "I traded amber to a man in the south for a song he swore was older than the sea.",
    "They say the old stones remember every name ever spoken to them.",
    "Brigands haunt the wild country. Travel by day, friend.",
    "I am bound for the coast, to take ship before the storms.",
    "Dig where the ground forgets to grow — that's where the past is buried.",
]
BRIGAND_NAMES = ["Cutthroat", "Reaver", "Marauder", "Footpad", "Raider", "Rogue"]
MONSTER_NAMES = ["Kraken", "Leviathan", "Giant Squid", "Scylla", "Hydra",
                 "Sea Serpent", "Cetus", "Charybdis"]


@dataclass
class Entity:
    eid: str
    kind: str             # "wanderer" | "merchant" | "brigand"
    name: str
    x: int
    y: int
    hp: int = 20
    max_hp: int = 20
    atk: int = 0
    spot: int = 0         # tiles at which a hunter notices a player
    cooldown: int = 0     # ticks until it can act/attack again
    speed: float = 1.0    # tiles per tick when chasing (randomized per mob)
    move_accum: float = 0.0
    target_pid: str | None = None
    path: list = field(default_factory=list)
    data: dict = field(default_factory=dict)  # wares / line / loot

    def to_public(self) -> dict:
        out = {"eid": self.eid, "kind": self.kind, "name": self.name,
               "x": self.x, "y": self.y, "hp": self.hp, "max_hp": self.max_hp}
        if self.kind == "brigand":
            out["hostile"] = self.target_pid is not None
        return out


def make_wanderer(eid: str, x: int, y: int, rng: random.Random) -> Entity:
    return Entity(eid, "wanderer", rng.choice(_FIRST), x, y,
                  data={"line": rng.choice(WANDERER_LINES)})


def make_merchant(eid: str, x: int, y: int, rng: random.Random) -> Entity:
    return Entity(eid, "merchant", f"{rng.choice(_FIRST)} the Trader", x, y,
                  data={"line": "Wares to sell, coin for your goods — come, look."})


def _roll_speed(rng: random.Random, base: float) -> float:
    """~60% slightly slower than the player's pace (evadable), ~40% faster
    (forces a fight). `base` is the player's flee speed for that medium."""
    if rng.random() < 0.6:
        return round(base * rng.uniform(0.7, 0.95), 2)
    return round(base * rng.uniform(1.05, 1.35), 2)


def make_brigand(eid: str, x: int, y: int, rng: random.Random) -> Entity:
    hp = rng.randint(16, 28)
    return Entity(eid, "brigand", rng.choice(BRIGAND_NAMES), x, y,
                  hp=hp, max_hp=hp, atk=rng.randint(4, 8), spot=6,
                  speed=_roll_speed(rng, 1.0),  # vs a walking player
                  data={"loot_coin": rng.randint(3, 12)})


def make_monster(eid: str, x: int, y: int, rng: random.Random) -> Entity:
    """A mythological sea beast — tougher than any brigand, and only a threat to
    those who venture onto the water."""
    hp = rng.randint(40, 75)
    return Entity(eid, "monster", rng.choice(MONSTER_NAMES), x, y,
                  hp=hp, max_hp=hp, atk=rng.randint(8, 15), spot=7,
                  speed=_roll_speed(rng, 0.5),  # vs a boat (water speed 0.5)
                  data={"loot_coin": rng.randint(10, 30)})
