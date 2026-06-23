"""Lightweight multiplayer state for the world map (the chunked real-Earth game).

Distinct from the test-map `World`: here a player's position is a **global tile
coordinate** on the 86400x43200 world, and we just track presence so players see
each other. Gameplay (land/water movement, gather, build, eras, NPCs) layers on
top of this in later steps.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class WorldPlayer:
    pid: str
    name: str
    x: float
    y: float
    city: str = ""
    seen: float = field(default_factory=time.time)


class WorldGame:
    def __init__(self) -> None:
        self.players: dict[str, WorldPlayer] = {}

    def join(self, pid: str, name: str, x: float, y: float, city: str) -> None:
        self.players[pid] = WorldPlayer(pid, name, float(x), float(y), city)

    def move(self, pid: str, x: float, y: float) -> None:
        p = self.players.get(pid)
        if p:
            p.x, p.y, p.seen = float(x), float(y), time.time()

    def leave(self, pid: str) -> None:
        self.players.pop(pid, None)

    def snapshot(self) -> dict:
        return {"players": [
            {"pid": p.pid, "name": p.name, "x": round(p.x, 1), "y": round(p.y, 1),
             "city": p.city} for p in self.players.values()]}
