# simhumanity

A multiplayer sim that takes players through the ages of human history
(Stone Age → Space Age). Real history fires as scheduled **anchor events**;
everything between them emerges from players, NPCs, and AI.

The signature mechanic — **Living History / Myth Engine**: a player's real
actions are turned by AI into the **ruins** you dig up and the **distorted
myths** later-era players inherit and argue about. The past literally becomes
the future's content.

## Architecture

- **Python authoritative tick server** runs the deterministic sim every tick
  (terrain, resources, economy, combat, pathfinding). This is the bulk of the game.
- **DeepSeek LLM** is called *rarely* for high-value content (NPC dialogue,
  the Myth Engine, naming, lore, anchor-event narration). Never per-tick.
- **2D top-down tile** client in the browser (HTML5 canvas, no build step).
- **Append-only event log** (`server/eventlog.py`) is the backbone that powers
  archaeology, myth propagation, and save/load.

## Run it

```bash
./run.sh
```

Then open http://127.0.0.1:8000 . First run creates `.venv` and copies
`.env.example` → `.env`. WASD/arrows to move, Space to gather.

## Config (`.env`)

All environment-specific settings (paths, ports, pacing, the DeepSeek key) live
in `.env` so moving from this WSL2 workstation to the Debian VPS is trivial.
**On WSL2, keep `SIMHUMANITY_DATA_DIR` on the native Linux FS** (not `/mnt/...`)
— cross-boundary I/O is slow and bad for the SQLite DB.

`SIMHUMANITY_TICK_HZ` and `SIMHUMANITY_MINUTES_PER_TICK` are the pacing knobs —
turn them up for the fast early-game feel, down for long multi-week arcs.

## Status / roadmap

**Done:**
- Authoritative tick loop, multiplayer over WebSocket, move/gather, event-log backbone.
- **Real Mediterranean map** (**300×219**) derived from a satellite image
  (`medsmall.jpg`). The offline tool `tools/build_map.py` classifies the image's
  pixels into macro geography (water / desert / land / snow), then textures the
  land into clustered grass / forest / hills biomes, and bakes the result to
  `server/med_map.txt`. The server just loads that grid — **no image decoding or
  Pillow dependency at runtime**. Re-bake only if the source image changes.
- **Ground items** scattered by region (olives/grapes on grass, flint/obsidian in
  hills, shells/clay/reeds on coasts, bones in the desert…), picked up by gathering.
- **Famous ancient sites** (`server/landmarks.py`) placed at their real
  coordinates — Göbekli Tepe, Çatalhöyük, Jericho, Troy, Knossos, Mycenae,
  Byblos, Memphis & Giza, Carthage, Ġgantija, Akrotiri, Gadir. Shown as gold
  stars; excavating one (`dig` while standing on it) reveals its true history
  and, the first time, grants a unique relic + Loremaster renown.
- **Click-to-move**: click the map or the minimap to travel. The server
  pathfinds (BFS over land) and walks you there one tile per tick; a manual WASD
  step cancels the route. (Landmasses separated by sea are only reachable the
  long way around until boats exist — a known limitation.)
- **Minimap** (top-right) showing the whole basin, sites, players and viewport.
- **Bandwidth**: terrain + items are sent once at connect (`init`, ~177 KB, terrain
  as compact one-char-per-tile rows); the resource grid isn't sent at all (the
  client derives it from terrain caps). Per tick only *deltas* stream — the
  steady-state message is ~285 bytes even on the 65k-tile map.

### Regenerating the map

```bash
./.venv/bin/pip install -r tools/requirements-dev.txt   # Pillow, build-only
./.venv/bin/python -m tools.build_map                   # writes server/med_map.txt
```

`tools/build_map.py` also writes `med_classified_preview.png` to eyeball the
classification. Tweak the colour thresholds / biome noise there.
- **Eras + season clock** (Stone → Bronze) with an automatic era transition.
- **Building** (hut / stone circle / cache) and **archaeology**: at the era
  transition the prior age's structures decay into buried ruins, which players
  `dig` to recover artifacts and the true record of who built what.
- **Myth Engine** (`server/ai.py`): when a ruin is excavated, the AI Historian
  reads the builder's logged deeds and writes a *distorted legend* — the true
  record decaying into myth. Provider is swappable (`SIMHUMANITY_AI_PROVIDER`):
  **DeepSeek** (default), local **Ollama**, or an offline **stub**. Legends are
  cached per ruin and logged as `myth` events.
- **Truth-vs-myth quests** (`server/quests.py`): each excavated legend carries
  structured **claims** — some true (grounded in the event log), some
  embellished (inflated numbers, pure myth). The player **judges** each claim
  true/embellished and **digs** for rumored buried hoards. Correct judgements
  and found hoards build **Loremaster renown** and loot; resolved via the
  `investigate` action and logged as `verify` events. Truth is checked against
  the log, not the AI, so it's grounded.

**Next:**
1. Economy: ownable assets, crafting, NPC-driven markets; relic *provenance*
   (an artifact tied to a famous legend is worth more).
2. Pre-bake myths at the era transition so a culture *inherits* legends about
   notable figures (vs. only generating them on dig).
3. Fame: track each player's notability so grand deeds become *their* legend.
4. Separate per-era player cohorts; bronze-specific tech/resources.
5. Anchor events (e.g. Younger Dryas).
