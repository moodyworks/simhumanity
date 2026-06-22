"""FastAPI app: the authoritative tick loop, the WebSocket, and static client.

Run with:  python -m server.main   (or ./run.sh)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .ai import MythEngine, make_provider
from .eventlog import EventLog
from .landmarks import km_per_tile
from .quests import build_claims
from .settings import SETTINGS
from .world import World

CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_tick_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        log.close()


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


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(CLIENT_DIR / "index.html")


app.mount("/static", StaticFiles(directory=CLIENT_DIR), name="static")


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
