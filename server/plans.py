"""Build plans — the discoverable 'tech tree' of things you can construct.

Players start knowing a couple of basics and discover the rest by excavating
ruins and ancient sites (each famous site teaches a fitting plan). The build
menu lists the plans you know and can afford; world.build() enforces both.
"""
from __future__ import annotations

# type -> {label, desc, cost{resource:amount}}. `boat` is special-cased in
# world.build (it grants a sailing item rather than placing a structure).
PLANS: dict[str, dict] = {
    "hut": {"label": "Hut", "desc": "basic shelter",
            "cost": {"wood": 5}},
    "cache": {"label": "Cache", "desc": "a small store of goods",
              "cost": {"wood": 2}},
    "stone_circle": {"label": "Stone Circle", "desc": "a ritual monument",
                     "cost": {"stone": 10}},
    "wall": {"label": "Stone Wall", "desc": "a defensive barrier",
             "cost": {"stone": 6}},
    "workshop": {"label": "Workshop",
                 "desc": "craft goods — the seed of a business",
                 "cost": {"wood": 8, "stone": 4}},
    "market_stall": {"label": "Market Stall",
                     "desc": "sell your goods to passers-by",
                     "cost": {"wood": 6}},
    "granary": {"label": "Granary", "desc": "store food against hard times",
                "cost": {"wood": 6, "stone": 2}},
    "dock": {"label": "Dock", "desc": "a harbor — build boats by the sea",
             "cost": {"wood": 10}},
    "boat": {"label": "Boat", "desc": "a small craft to cross the water",
             "cost": {"wood": 8}},
}

# What you can build before discovering anything.
STARTING_PLANS = ["hut", "cache"]

# Completing a famous site's study quiz teaches its fitting plan.
SITE_TEACHES = {
    "Göbekli Tepe": "stone_circle", "Ġgantija": "stone_circle",
    "Çatalhöyük": "workshop", "Knossos": "workshop",
    "Jericho": "wall", "Troy": "wall", "Mycenae": "wall",
    "Byblos": "market_stall", "Carthage": "dock", "Gadir": "boat",
    "Akrotiri": "boat", "Memphis & Giza": "granary",
}

# Plans a player-made ruin can teach when excavated (random pick).
RUIN_TEACHABLE = ["wall", "workshop", "granary", "market_stall", "dock"]

# Plans some merchants will sell, and for how much coin. Only merchants who
# spawn by the sea stock the boat/dock plans (set in World._spawn_entities).
PLAN_PRICE = {"boat": 25, "dock": 40, "workshop": 35, "granary": 20}
COASTAL_PLANS = ["boat", "dock"]  # sold only by shoreside traders


def plan_public(plan_type: str) -> dict:
    p = PLANS[plan_type]
    return {"type": plan_type, "label": p["label"], "desc": p["desc"],
            "cost": p["cost"]}
