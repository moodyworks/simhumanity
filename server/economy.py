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
}


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
