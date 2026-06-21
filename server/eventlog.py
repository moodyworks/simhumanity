"""Append-only event log — the backbone of the whole game.

Every meaningful thing that happens (a tile gathered, a structure built, a
player dying defending a stone circle) is appended here as an immutable event.
This single log is what later powers:
  - archaeology (past structures become diggable ruins),
  - the Myth Engine (the AI Historian reads/distorts these into legends),
  - replay / debugging / save-load.

SQLite now; the schema is plain enough to port to Postgres on the VPS later.
Lives on the native Linux FS (see settings.data_dir) — never on /mnt/.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable


class EventLog:
    def __init__(self, db_path: Path):
        # check_same_thread=False: the tick loop and request handlers share it;
        # we serialize access through the single asyncio loop so this is safe.
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                real_ts    REAL    NOT NULL,   -- wall-clock seconds
                world_time INTEGER NOT NULL,   -- in-world minutes since epoch
                era        TEXT    NOT NULL,
                kind       TEXT    NOT NULL,    -- e.g. 'gather', 'build', 'death'
                actor      TEXT,                -- player/npc id, nullable
                x          INTEGER,             -- where it happened (nullable)
                y          INTEGER,
                data       TEXT    NOT NULL     -- JSON payload
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_loc ON events(x, y)"
        )
        self._db.commit()

    def append(
        self,
        *,
        world_time: int,
        era: str,
        kind: str,
        actor: str | None = None,
        x: int | None = None,
        y: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> int:
        cur = self._db.execute(
            "INSERT INTO events (real_ts, world_time, era, kind, actor, x, y, data)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                world_time,
                era,
                kind,
                actor,
                x,
                y,
                json.dumps(data or {}),
            ),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def query(
        self, *, kind: str | None = None, limit: int = 100
    ) -> Iterable[dict[str, Any]]:
        sql = "SELECT * FROM events"
        params: list[Any] = []
        if kind is not None:
            sql += " WHERE kind = ?"
            params.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        for row in self._db.execute(sql, params):
            d = dict(row)
            d["data"] = json.loads(d["data"])
            yield d

    def by_actor(self, actor: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """All logged events for one actor — the raw material for their myth."""
        out: list[dict[str, Any]] = []
        for row in self._db.execute(
            "SELECT * FROM events WHERE actor = ? ORDER BY id ASC LIMIT ?",
            (actor, limit),
        ):
            d = dict(row)
            d["data"] = json.loads(d["data"])
            out.append(d)
        return out

    def close(self) -> None:
        self._db.close()
