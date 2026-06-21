"""Currency and prices — the backbone of trade and barter.

Players hold `coin`. Merchants sell their wares above base value and buy the
player's goods below it (the spread is the economy's friction). Relics and
artifacts carry the cross-era premium that makes digging pay.
"""
from __future__ import annotations

BASE_PRICES: dict[str, int] = {
    "wood": 2, "stone": 3, "forage": 1,
    "olives": 3, "grapes": 4, "herbs": 3, "mushrooms": 2,
    "amber": 9, "flint": 4, "obsidian": 8,
    "shells": 3, "clay": 3, "reeds": 2, "bones": 2,
    "artifact": 15, "boat": 6,
    # supplies, tools, weapons, armour (looted from brigands).
    "dried_fish": 3, "hide": 4, "rope": 3, "grain": 3,
    "flint_knife": 6, "stone_axe": 7, "sickle": 6,
    "dagger": 10, "club": 8, "spear": 14, "bronze_sword": 22,
    "leather_jerkin": 9, "hide_shield": 11, "bronze_vest": 24,
}

# Weapons add to your attack; armour subtracts from damage taken. The player
# automatically uses the best they carry (no equip step in v1).
WEAPON_ATK = {"flint_knife": 1, "dagger": 3, "club": 4, "spear": 6,
              "bronze_sword": 9}
ARMOUR_DEF = {"hide": 1, "leather_jerkin": 2, "hide_shield": 3, "bronze_vest": 5}

# Weighted loot table for brigand kills (common foraged/supplies → rare gear).
_LOOT: list[tuple[str, int]] = [
    ("olives", 6), ("grapes", 6), ("herbs", 5), ("mushrooms", 4),
    ("flint", 5), ("dried_fish", 5), ("hide", 5), ("rope", 4), ("grain", 5),
    ("flint_knife", 3), ("stone_axe", 2), ("sickle", 2),
    ("dagger", 3), ("club", 2), ("spear", 2), ("bronze_sword", 1),
    ("leather_jerkin", 2), ("hide_shield", 2), ("bronze_vest", 1),
]
RELIC_DROP_CHANCE = 0.15  # a brigand sometimes carries a stolen site relic


def roll_loot(rng, n: int = 1) -> list[str]:
    items, weights = zip(*_LOOT)
    return rng.choices(items, weights=weights, k=max(0, n))


def base_value(item: str) -> int:
    if item.startswith("relic of"):
        return 40  # famous-site relics are prized
    return BASE_PRICES.get(item, 5)


def buy_price(item: str) -> int:
    """What the player pays a merchant for one (merchant sells dear)."""
    return max(1, round(base_value(item) * 1.3))


def sell_price(item: str) -> int:
    """What a merchant pays the player for one (merchant buys cheap)."""
    return max(1, round(base_value(item) * 0.6))


# What a merchant stocks to sell you (resources/tools handy for building).
MERCHANT_WARES = ["wood", "stone", "flint", "olives", "grapes", "herbs"]
