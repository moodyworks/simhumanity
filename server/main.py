"""FastAPI app: the authoritative tick loop, the WebSocket, and static client.

Run with:  python -m server.main   (or ./run.sh)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .ai import MythEngine, make_provider
from .cities import CITIES, city_stage
from .eventlog import EventLog
from .landmarks import km_per_tile
from .plans import plan_public
from .quests import build_claims
from .settings import SETTINGS
from .world import World
from .worldgame import BUILDS, WorldGame
from .worldterrain import WorldTerrain

CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"
WORLD_TILES_DIR = Path(__file__).resolve().parent.parent / "world_tiles"
WORLD_TILES_DIR.mkdir(exist_ok=True)  # so the static mount never fails on a fresh checkout
_HIGHRES = Path(__file__).resolve().parent.parent / "highres"


def _world_dims() -> tuple[int, int]:
    try:
        m = json.loads((WORLD_TILES_DIR / "manifest.json").read_text())
        return int(m["src_w"]), int(m["src_h"])
    except Exception:
        return 86400, 43200


WORLD_W, WORLD_H = _world_dims()
world_terrain = WorldTerrain(WORLD_W, WORLD_H, str(_HIGHRES / "topo"),
                             str(_HIGHRES / "world.200408.3x5400x2700.jpg"))

# For now, every server (re)start is a completely fresh game: wipe the persisted
# event log so nothing carries over a hard reset. (Real persistence is future.)
for _suffix in ("", "-wal", "-shm"):
    _f = Path(str(SETTINGS.db_path) + _suffix)
    if _f.exists():
        _f.unlink()

log = EventLog(SETTINGS.db_path)
def _new_world() -> World:
    return World(
        log,
        minutes_per_tick=SETTINGS.minutes_per_tick,
        ticks_per_era=SETTINGS.ticks_per_era,
    )


world = _new_world()
myth_engine = MythEngine(make_provider())

# Connected websockets, so the tick loop can broadcast to everyone.
_clients: set[WebSocket] = set()
# pid -> websocket, so the tick loop can route per-player notices.
_ws_by_pid: dict[str, WebSocket] = {}

# World-map game (separate from the test-map World): presence broadcast to its
# own clients so players see each other on the real Earth.
world_game = WorldGame()
_world_clients: dict[WebSocket, str] = {}  # ws -> pid


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_tick_loop())
    wtask = asyncio.create_task(_world_loop())
    asyncio.create_task(_build_terrain())  # background; flips world_terrain.ready
    try:
        yield
    finally:
        for t in (task, wtask):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        log.close()


async def _build_terrain() -> None:
    """Build the coarse world terrain off-thread so startup isn't blocked. If the
    GEBCO/Blue Marble sources are absent, gather/build just stay unavailable."""
    try:
        await asyncio.to_thread(world_terrain.build)
    except Exception as exc:
        print("world terrain unavailable:", exc)


async def _world_loop() -> None:
    """Tick the world (era clock + NPCs) and broadcast presence at ~8 Hz."""
    last = time.monotonic()
    while True:
        await asyncio.sleep(0.125)
        now = time.monotonic()
        dt, last = now - last, now
        world_game.tick(dt, world_terrain)
        if world_game.events:  # per-player notices (e.g. respawn on death)
            pid_ws = {p: w for w, p in _world_clients.items()}
            for ev in world_game.events:
                w = pid_ws.get(ev["pid"])
                if w is None:
                    continue
                with contextlib.suppress(Exception):
                    if ev["kind"] == "respawn":
                        await w.send_text(json.dumps({"type": "respawn", "x": ev["x"],
                            "y": ev["y"], "hp": ev["hp"]}))
                        await w.send_text(json.dumps({"type": "log",
                            "text": "You were slain — back to your city."}))
            world_game.events.clear()
        if not _world_clients:
            continue
        msg = json.dumps({"type": "presence", **world_game.snapshot()})
        for ws in list(_world_clients):
            try:
                await ws.send_text(msg)
            except Exception:
                pid = _world_clients.pop(ws, None)
                if pid:
                    world_game.leave(pid)


app = FastAPI(title="simhumanity", lifespan=lifespan)


@app.middleware("http")
async def _no_cache(request, call_next):
    """Dev convenience: never let the browser serve a stale client. (In
    production you'd cache static assets with a content hash instead.)"""
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def _tick_loop() -> None:
    """The heartbeat: advance the world and broadcast at TICK_HZ."""
    interval = 1.0 / SETTINGS.tick_hz
    while True:
        prev_era = world.era
        world.tick()
        messages = [json.dumps(world.snapshot())]
        # Announce an era transition once, to everyone.
        if world.era != prev_era:
            messages.append(json.dumps({
                "type": "event",
                "text": (
                    f"The {world.era.title()} Age dawns; the works of the "
                    f"{prev_era.title()} Age crumble into ruin."
                ),
            }))
        dead: list[WebSocket] = []
        for ws in _clients:
            try:
                for m in messages:
                    await ws.send_text(m)
            except Exception:
                dead.append(ws)
        for ws in dead:
            _clients.discard(ws)
        # Per-player notices (e.g. death) routed to the matching client.
        for n in world.pop_notices():
            target = _ws_by_pid.get(n["pid"])
            if target:
                try:
                    await target.send_text(json.dumps({"type": "log", "text": n["text"]}))
                except Exception:
                    pass
        await asyncio.sleep(interval)


async def _handle_dig(ws: WebSocket, pid: str) -> None:
    """Excavate, report the true record, then deliver the (cached) legend.

    The deterministic part (dig) runs in the sim; the LLM call runs here in the
    async layer. We await it inline — a dig is a deliberate pause, and the
    legend for any given ruin is generated once then cached for everyone.
    """
    # Standing on a famous ancient site? Excavating opens its study quiz; the
    # relic is granted only once it's answered (handled in _handle_site_answer).
    site = world.excavate_landmark(pid)
    if site is not None:
        await ws.send_text(json.dumps({"type": "landmark", **site}))
        return

    result = world.dig(pid)
    if not result:
        return
    await ws.send_text(json.dumps({"type": "log", "text": result["text"]}))
    if result.get("learned"):
        await ws.send_text(json.dumps({"type": "plans", "plans": world.known_plans(pid)}))
    if result.get("relic"):
        await ws.send_text(json.dumps({"type": "relics", "relics": world.relics(pid)}))
    exc = result["excavation"]
    if not exc:
        return
    x, y = exc["x"], exc["y"]

    # Derive the truth-vs-myth quest once per ruin, grounded in the real log.
    deeds: list[dict] | None = None
    if world.get_quest(x, y) is None:
        deeds = world.log.by_actor(exc["builder_pid"], limit=50)
        world.set_quest(x, y, exc["builder"], build_claims(exc["builder"], deeds, x, y))

    myth = world.get_myth(x, y)
    if myth is None:
        # Tell the client a legend is being recalled, so the ~few-second AI
        # call doesn't feel like nothing happened.
        await ws.send_text(json.dumps({"type": "myth_pending"}))
        if deeds is None:
            deeds = world.log.by_actor(exc["builder_pid"], limit=50)
        myth = await myth_engine.generate(
            builder=exc["builder"],
            structure_type=exc["original_type"],
            era_built=exc["era_built"],
            deeds=deeds,
        )
        world.set_myth(x, y, myth)

    await ws.send_text(json.dumps({
        "type": "myth",
        "text": myth,
        "builder": exc["builder"],
        "x": x, "y": y,
        "claims": world.public_claims(x, y),
    }))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    # Each game is fresh (for now): when someone joins an empty world, start a
    # brand-new one so prior excavations/ruins don't carry over.
    global world
    if not world.players:
        world = _new_world()
    pid = uuid.uuid4().hex[:8]
    name = f"Wanderer-{pid[:4]}"
    player = world.add_player(pid, name)

    # Send the one-time map + identity FIRST, then join the broadcast set — so a
    # tick can't race a `state` frame in front of `init` (the client needs the
    # terrain/map from `init` before any state makes sense).
    await ws.send_text(json.dumps({
        "type": "init",
        "pid": pid,
        "era": world.era,
        "width": world.width,
        "height": world.height,
        "terrain": world.terrain_rows(),      # compact: one char per tile
        # No resource grid: every tile starts full, so the client derives it from
        # terrain caps (see RESOURCE_CAP) and applies per-tick deltas thereafter.
        "items": world.items_list(),          # full once; deltas follow per tick
        "landmarks": world.landmarks_public(),  # famous ancient sites (static)
        "km_per_tile": km_per_tile(world.width, world.height),
    }))
    await ws.send_text(json.dumps({"type": "plans", "plans": world.known_plans(pid)}))
    await ws.send_text(json.dumps({"type": "relics", "relics": world.relics(pid)}))
    _clients.add(ws)
    _ws_by_pid[pid] = ws

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            action = msg.get("action")
            if action == "move":
                world.move(pid, int(msg.get("dx", 0)), int(msg.get("dy", 0)))
            elif action == "run":
                world.set_running(pid, bool(msg.get("on")))
            elif action == "set_year":  # debug: jump the clock
                world.set_year(int(msg.get("year", 0)))
            elif action == "move_place":  # debug: relocate a city/site
                v = world.move_place(
                    str(msg.get("kind", "")), str(msg.get("name", "")),
                    int(msg.get("x", -1)), int(msg.get("y", -1)))
                if v:
                    await ws.send_text(json.dumps({"type": "log",
                        "text": f"Moved {v['name']} to ({v['x']},{v['y']}). Saved."}))
                    if v["kind"] == "site":  # cities update via the snapshot
                        await ws.send_text(json.dumps(
                            {"type": "landmarks", "landmarks": world.landmarks_public()}))
            elif action == "goto":
                world.set_goal(pid, int(msg.get("x", -1)), int(msg.get("y", -1)))
            elif action == "attack":
                r = world.attack(pid)
                if r:
                    await ws.send_text(json.dumps({"type": "log", "text": r["text"]}))
                    if r.get("relic"):
                        await ws.send_text(json.dumps(
                            {"type": "relics", "relics": world.relics(pid)}))
            elif action == "interact":
                v = world.interact(pid)
                if v is None:
                    await ws.send_text(json.dumps({"type": "log",
                        "text": "No one is here to speak with."}))
                elif v["kind"] == "merchant":
                    await ws.send_text(json.dumps({"type": "merchant", **v}))
                else:
                    await ws.send_text(json.dumps({"type": "npc", **v}))
            elif action == "barter":
                v = world.barter(pid, str(msg.get("eid", "")),
                                 str(msg.get("trade", "")), str(msg.get("item", "")),
                                 int(msg.get("qty", 1)))
                if v and "error" in v:
                    await ws.send_text(json.dumps({"type": "log", "text": v["error"]}))
                elif v:
                    await ws.send_text(json.dumps({"type": "merchant", **v}))
                    if v.get("learned"):
                        await ws.send_text(json.dumps(
                            {"type": "plans", "plans": world.known_plans(pid)}))
            elif action == "gather":
                picked = world.gather(pid)
                if picked:  # only ground-item pickups send feedback
                    await ws.send_text(
                        json.dumps({"type": "log", "text": picked})
                    )
            elif action == "build":
                result = world.build(pid, str(msg.get("type", "")))
                if result:  # per-actor feedback; tick broadcast stays global
                    await ws.send_text(
                        json.dumps({"type": "log", "text": result})
                    )
            elif action == "dig":
                await _handle_dig(ws, pid)
            elif action == "site_answer":
                v = world.answer_site(
                    pid, int(msg.get("x", -1)), int(msg.get("y", -1)),
                    int(msg.get("q", -1)), bool(msg.get("guess")),
                )
                if v and "error" in v:
                    await ws.send_text(json.dumps({"type": "log", "text": v["error"]}))
                elif v:
                    await ws.send_text(json.dumps({"type": "site_response", **v}))
                    if v.get("learned"):
                        await ws.send_text(json.dumps(
                            {"type": "plans", "plans": world.known_plans(pid)}))
                    if v.get("relic"):
                        await ws.send_text(json.dumps(
                            {"type": "relics", "relics": world.relics(pid)}))
            elif action == "site_abandon":
                world.abandon_site(pid, int(msg.get("x", -1)), int(msg.get("y", -1)))
            elif action == "investigate":
                v = world.investigate(
                    pid,
                    int(msg.get("x", -1)),
                    int(msg.get("y", -1)),
                    int(msg.get("claim", -1)),
                    msg.get("guess"),  # True/False for a judge, None for a hoard
                )
                if v is not None and "error" in v:
                    await ws.send_text(
                        json.dumps({"type": "log", "text": v["error"]})
                    )
                elif v is not None:
                    await ws.send_text(json.dumps({
                        "type": "verdict",
                        "x": int(msg.get("x", -1)),
                        "y": int(msg.get("y", -1)),
                        **v,
                    }))
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)
        _ws_by_pid.pop(pid, None)
        world.remove_player(pid)
        # When the last player leaves, start a fresh world so the next game
        # doesn't inherit this one's excavations/ruins/cities (each game fresh).
        if not _clients:
            world = _new_world()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(CLIENT_DIR / "index.html")


@app.get("/world")
async def world_viewer() -> FileResponse:
    """Standalone real-Earth chunk viewer (the world-map vertical slice)."""
    return FileResponse(CLIENT_DIR / "world.html")


@app.get("/world/spawns")
async def world_spawns(year: int = -2000) -> dict:
    """Cities of the age to spawn into — those that exist (stage > 0) in `year`.
    Returns lon/lat; the client projects to a world tile via the manifest. This
    clusters players together in real settlements instead of an empty planet."""
    out = [{"name": c["name"], "lon": c["lon"], "lat": c["lat"],
            "stage": city_stage(c["timeline"], year)} for c in CITIES]
    out = sorted((c for c in out if c["stage"] > 0), key=lambda c: -c["stage"])
    return {"year": year, "spawns": out}


async def _send_inv(ws: WebSocket, pid: str) -> None:
    p = world_game.players.get(pid)
    if p is not None:
        await ws.send_text(json.dumps({"type": "inv", "inv": p.inv,
            "hp": p.hp, "max_hp": p.max_hp, "relics": p.relics,
            "plans": [plan_public(k) for k in sorted(p.plans)]}))


async def _mythologize(ws: WebSocket, key, builder: str, kind: str, era: str) -> None:
    """Have the Myth Engine garble the true dig record into a legend, cache it on the
    ruin (so later diggers get the myth), and whisper it to this digger."""
    legend = await myth_engine.generate(builder=builder, structure_type=kind,
                                        era_built=era, deeds=[{"kind": "build"}])
    world_game.set_legend(key, legend)
    with contextlib.suppress(Exception):
        await ws.send_text(json.dumps({"type": "log", "text": "Legend: " + legend}))


@app.websocket("/world/ws")
async def world_ws(ws: WebSocket) -> None:
    """World-map multiplayer: the client spawns, streams its position, and gathers
    / builds; the server is authoritative for inventory and structures, and
    (via _world_loop) broadcasts everyone's positions + structures."""
    await ws.accept()
    pid = uuid.uuid4().hex[:8]
    await ws.send_text(json.dumps({"type": "welcome", "pid": pid}))
    _world_clients[ws] = pid
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            action = msg.get("action")
            if action == "spawn":
                name = msg.get("name") or f"Wanderer-{pid[:4]}"
                world_game.join(pid, name, msg.get("x", 0), msg.get("y", 0),
                                msg.get("city", ""))
                await _send_inv(ws, pid)
            elif action == "move":
                world_game.move(pid, msg.get("x", 0), msg.get("y", 0))
            elif action == "gather":
                res = world_game.gather(pid)
                await ws.send_text(json.dumps({"type": "log",
                    "text": f"Gathered {res}." if res else "Nothing in reach to gather."}))
                await _send_inv(ws, pid)
            elif action == "build":
                r = world_game.build(pid, str(msg.get("kind", "")), world_terrain)
                note = {"water": "Can't build on water.", "occupied": "Something's already here.",
                        "cost": "Not enough materials.", "bad": "Unknown structure.",
                        "unknown": "You haven't learned that plan yet."}
                await ws.send_text(json.dumps({"type": "log",
                    "text": f"Built a {r}." if r in BUILDS else note.get(r, "Can't build.")}))
                await _send_inv(ws, pid)
            elif action == "dig":
                r = world_game.dig(pid)
                st = r.get("status")
                if st in ("truth", "myth", "again"):
                    await ws.send_text(json.dumps({"type": "log", "text": r["text"]}))
                    await _send_inv(ws, pid)
                    if st == "truth":  # spin the legend (DeepSeek), cache + echo it
                        asyncio.create_task(_mythologize(ws, r["key"], r["builder"],
                                                         r["kind"], r["era"]))
                else:
                    await ws.send_text(json.dumps({"type": "log", "text": "Nothing buried here."}))
            elif action == "talk":
                r = world_game.talk(pid)
                if not r:
                    await ws.send_text(json.dumps({"type": "log", "text": "No-one nearby to talk to."}))
                else:
                    await ws.send_text(json.dumps({"type": "log", "text": r["text"]}))
                    if r.get("traded"):
                        await _send_inv(ws, pid)
            elif action == "attack":
                r = world_game.attack(pid)
                if r:
                    kind = r.split(":")[1]
                    await ws.send_text(json.dumps({"type": "log",
                        "text": f"You slay the {kind}!" if r.startswith("killed")
                        else f"You strike the {kind}."}))
                    await _send_inv(ws, pid)
                else:
                    await ws.send_text(json.dumps({"type": "log", "text": "Nothing in reach."}))
    except WebSocketDisconnect:
        pass
    finally:
        _world_clients.pop(ws, None)
        world_game.leave(pid)


app.mount("/static", StaticFiles(directory=CLIENT_DIR), name="static")
# Generated world chunks (gitignored). Served pixel-for-pixel to the world viewer.
app.mount("/tiles", StaticFiles(directory=WORLD_TILES_DIR), name="tiles")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "server.main:app",
        host=SETTINGS.host,
        port=SETTINGS.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
