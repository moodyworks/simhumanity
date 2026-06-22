# simhumanity — Living Design Document

> **This is a living document. Update it whenever a feature is added or changed.**
> Add a dated entry to the [Changelog](#changelog) and edit the relevant section.

A multiplayer sim that carries players through the ages of human history
(Stone → Space). Real history fires as scheduled **anchor events** and an
era/year clock; everything between emerges from players, NPCs, and AI.

The signature mechanic — **Living History / Myth Engine**: a player's real
actions become the **ruins** later players dig up and the **distorted myths**
they inherit and argue about. The past literally becomes the future's content.

---

## 1. Architecture

- **Python authoritative tick server** (`server/`), FastAPI + WebSocket. The
  deterministic sim runs every tick: terrain, resources, movement, economy,
  combat, cities, pathfinding. The client only draws and sends intent.
- **DeepSeek LLM** (`server/ai.py`), called *rarely* for high-value content
  (the Myth Engine, lore). Provider is swappable (`SIMHUMANITY_AI_PROVIDER`:
  `deepseek` | `ollama` | `stub`). Never per-tick.
- **2D top-down tile client** (`client/`), plain HTML5 canvas + vanilla JS, no
  build step.
- **Append-only event log** (`server/eventlog.py`) — the backbone that powers
  archaeology, myth propagation, and (later) persistence.

### Wire protocol (WebSocket)
- `init` (once): identity, map size, terrain (compact one-char-per-tile rows),
  items, landmarks, `km_per_tile`. Followed by `plans` and `relics`.
- `state` (every tick): era, year, tick, sparse `resource_changes` /
  `item_changes`, `structures`, `ruins`, `cities`, `entities`, `combat`,
  `players`. Steady-state is small (deltas, not full grids).
- Server→client one-offs: `log`, `event`, `myth_pending`, `myth`, `verdict`,
  `landmark`, `site_response`, `merchant`, `npc`, `plans`, `relics`.
- Client→server actions: `move` (set heading), `run`, `goto`, `gather`, `build`,
  `dig`, `interact`, `barter`, `attack`, `site_answer`, `site_abandon`,
  `investigate`.

### Repo layout
| Path | Purpose |
|---|---|
| `server/main.py` | FastAPI app, tick loop, WebSocket, action handlers |
| `server/world.py` | The World: tiles, players, movement, combat, cities, snapshot |
| `server/settings.py` | Env/`.env` config |
| `server/eventlog.py` | Append-only event log (SQLite) |
| `server/mapdata.py` | Loads the baked terrain grid `med_map.txt` |
| `server/mountains.py` | Stamps real mountain ranges + passes (build-time) |
| `server/landmarks.py` | Famous ancient sites + lon/lat→tile + km/tile |
| `server/cities.py` | Cities and their rise/fall timelines |
| `server/plans.py` | Buildable plans (tech tree) + prices |
| `server/economy.py` | Prices, loot tables, weapon/armour stats |
| `server/entities.py` | NPCs, brigands, sea monsters |
| `server/quests.py` | Truth-vs-myth claim generation |
| `server/ai.py` | LLM provider seam + Myth Engine |
| `tools/build_map.py` | Bakes `med_map.txt` from `medsmall.jpg` (Pillow, dev-only) |
| `tools/preview_landmarks.py` | Calibration overlay for landmark placement |
| `client/game.js` | Renderer, input, all UI |

---

## 2. World & map (current test map)

- **Mediterranean**, 300×219 tiles, derived from a satellite image
  (`medsmall.jpg`) by `tools/build_map.py` → baked to `server/med_map.txt`. The
  server never decodes the image at runtime.
- **Scale:** ~15.45 km per tile (~4,600 km across). A km/mile **scale bar** is
  drawn in the corner from `km_per_tile` (sent in `init`).
- **Terrain:** water, grass, forest, hills, stone, desert, **mountain**
  (impassable), **glacier** (impassable), **pass** (walkable). Mountains/glaciers
  block movement *and* line of sight; historical passes (Great St Bernard,
  Brenner, Cilician Gates, …) punch walkable gaps. Map stays ~94% connected.
  Ranges are ringed by **rocky foothills** (hills/stone), so **stone is plentiful
  near the mountains**.
- **Ground items** scattered by region (olives/grapes on grass, flint/obsidian
  in hills, shells/clay/reeds on coasts, **bones** in desert…).
- **Minimap** (top-right) shows the basin, discovered sites/cities, players, and
  the viewport — all subject to fog.

---

## 3. Time, eras & the year

- **8 eras**: stone, bronze, iron, classical, feudal, industrial, atomic, space.
- **In-world year** spans **50,000 BCE → 5,000 AD**, interpolated through each
  era's date range (`ERA_DATES` in `world.py`) and shown beside the age.
- Each era lasts `SIMHUMANITY_TICKS_PER_ERA` ticks. At the default 1350 ticks and
  `TICK_HZ=3`, the **whole arc takes ~1 hour**. Years-per-tick varies by era
  (prehistory blasts by; recent eras are detailed) — deliberate time dilation.
- At each era boundary, **standing works decay into diggable ruins** (the Living
  History mechanic).

---

## 4. Movement & camera

- **Tick-paced**, per-player. Set a heading with WASD/arrows; **click the map**
  to auto-travel there; **click/drag the minimap** to pan the view only.
- **Auto-travel finds the *fastest* (least-time) route**, not the straightest:
  a Dijkstra weights each tile by its crossing time (water is slow — a boat is
  half speed), so it routes around the sea via faster land instead of cutting
  straight across, and through mountain passes rather than into impassable rock.
- **Speeds:** walk 1.0 tiles/tick; **run (hold Shift) 2.0**; **boat 0.5** on
  water (slower than walking). Sub-tile accumulator.
- **Boats:** carry one (built by the coast/dock or bought from a shipwright) to
  cross water; pathfinding is per-player.
- **Camera deadzone:** the view does **not** scroll while you move through the
  central region; only when you come within **25% of a screen edge** does it pan
  to keep you inside the deadzone. When you **stop**, the camera eases to centre
  on you. (Minimap panning detaches the camera until you move again.) Driven by a
  `requestAnimationFrame` loop for smoothness between ticks.

---

## 5. Fog of war & line of sight

- Tiles are hidden until seen; explored-but-out-of-sight dims; the area in
  **current line of sight** is clear. LOS uses **raycast occlusion** — mountains
  and glaciers block the view (not a bare radius).
- **Enemies and NPCs are only drawn within current line of sight.**
- Fog also applies to the **minimap** (and to discovered sites/cities/players).
- **Debug mode** (**O** key / **Debug** button): reveals the whole map (fog off,
  all mobs visible) and turns the **age/year label into a control — click it to
  type a year** and jump the world clock there; assets repopulate for that year
  (cities rise/fall to their stage, era updates). Great for testing the timeline.

---

## 6. Building & the plan tree

- **Discoverable plans** (`server/plans.py`): start knowing hut + cache; learn
  more by excavating ruins (chance), completing ancient-site quizzes (each site
  teaches a fitting plan), or **buying from coastal "shipwright" merchants** (the
  boat/dock plans — not all vendors stock them).
- **Build menu** (B): lists known, affordable plans.
- **Building purposes:**
  - **Hut** — your home: respawn there on death, heal faster nearby.
  - **Stone circle** — a monument: earns its builder renown over time.
  - **Cache** — a strongbox: on death you lose only 25% (vs 50%), and that coin
    is **stashed in the cache**; when the cache later decays into a ruin and
    someone excavates it, they recover the buried hoard (an archaeology-economy
    loop — your death becomes a future player's find).
  - **Boat** — carried; lets you cross water.
  - Others: wall, workshop, market stall, granary, dock.

---

## 7. Archaeology, myths & relics

- **Ruins:** at each era transition, structures decay into buried ruins. `dig`
  (E) excavates them.
- **Myth Engine:** excavating a ruin sends the builder's logged deeds to DeepSeek,
  which returns a *distorted legend*; cached per ruin.
- **Truth-vs-myth quests:** legends carry claims (some true per the log, some
  embellished); judge them for **Loremaster renown** and loot.
- **Ancient sites** (`landmarks.py`): 12 famous real sites (Göbekli Tepe,
  Çatalhöyük, Jericho, Troy, Knossos, …). Excavating opens a **study quiz**; the
  relic is granted only when answered (walk away and it stays buried).
- **Relics** are site/excavation-specific objects with **clues**. A **relic
  inventory** (I) lets you click each to read its clue. Sources: ancient sites,
  ruin digs, and rare drops from brigands (stolen) and sea monsters (swallowed).
- **Bone sites:** digging on a bones item has a <50% chance to unearth buried loot.

---

## 8. Cities that rise and fall

- 18 major cities (`server/cities.py`): Athens, Rome, Carthage, Alexandria,
  Byzantium, Memphis, Troy, Knossos, Byblos, Jericho, Syracuse, Massalia, Gades,
  Neapolis, Corduba, Venetia, Tyre, Tarraco.
- Each has a **(year, stage)** timeline (stage 0 ruins … 4 metropolis). The
  current stage is interpolated from the in-world year, so settlements grow and
  decline at roughly the right dates (Athens peaks ~450 BC, dwindles to a village
  by ~1600 AD, booms again modern; Carthage is razed in 146 BC; …).
- Rendered client-side as scaled settlements with walls and **ruins in the former
  extent** as they shrink. Shown on map + minimap, subject to fog. Cities not yet
  founded (future cities at stage 0 with no history) stay hidden until founding.
- *Not yet:* city ruins aren't separately diggable, and cities don't yet spawn
  their own NPCs/markets (see Roadmap → city interiors).

---

## 9. Economy, NPCs & combat

- **Currency** (coin). **Merchants** barter (buy/sell with a spread; F or click);
  coastal **shipwrights** also sell plans. Relics/artifacts carry a premium.
- **Wandering NPCs** with dialogue.
- **Brigands** roam land; **sea monsters** (kraken, leviathan, giant squid, …)
  roam water and hunt only players out in boats (the shore is safe). Both have
  **random speeds** (≈60% evadable / 40% catch you), and **give up the chase once
  you're out of their sight** (beyond your vision radius, `VISION_TILES`).
- **Combat:** attack adjacent hostiles (R or click). Damage = 6 + best weapon
  carried; armour blunts incoming damage. Kills drop coin + random loot
  (foraged / supplies / tools / weapons / armour / rare relics); sea monsters
  drop briny treasure. Death loses some coin and respawns you (at your hut if you
  have one). HP regenerates slowly out of combat; an **HP bar** is in the HUD.

---

## 10. Controls

| Input | Action |
|---|---|
| Click map | Walk there (pathfind) |
| Click neighbour | Talk/trade (NPC) or attack (hostile) |
| Click/drag minimap | Pan the view (does not move you) |
| WASD / arrows | Move (set heading) |
| Shift (hold) | Run (2×) |
| Space | Gather / pick up |
| B | Build menu |
| E | Dig / excavate |
| F | Talk / trade |
| R | Attack |
| I | Relic inventory (click a relic for its clue) |
| O | Toggle fog of war (debug) |
| Esc | Close dialogue |

---

## 11. Configuration (`.env`)

| Key | Default | Meaning |
|---|---|---|
| `SIMHUMANITY_DATA_DIR` | `~/.local/share/simhumanity` | Runtime data (keep off `/mnt` on WSL2) |
| `SIMHUMANITY_HOST` / `_PORT` | `127.0.0.1` / `8000` | Bind |
| `SIMHUMANITY_TICK_HZ` | `3` | Ticks/sec (the "pause between ticks") |
| `SIMHUMANITY_MINUTES_PER_TICK` | `30` | Internal fine clock (event timestamps) |
| `SIMHUMANITY_TICKS_PER_ERA` | `1350` | Ticks per era (~1 hr full arc at 3 Hz) |
| `SIMHUMANITY_AI_PROVIDER` | `auto` | `deepseek` / `ollama` / `stub` |
| `SIMHUMANITY_DEEPSEEK_*` | — | Key/model/base URL (key only in `.env`, never committed) |

---

## 12. Roadmap / planned features

### A. Real world map (replaces the test Mediterranean map)
The current map is a **test bed**. A full world map is coming. All location-keyed
systems (landmarks, cities, mob/spawn placement, the lon/lat→tile transform) must
generalize to it. A new map is started in the repo (`1-1.jpg`…`2-6.jpg`).

### B. Chunked "circle of influence" loading + resource DB *(required for the world map)*
The world map is too large to hold fully in memory or ship at connect. Plan:
- Back the world with a **resource file / database** (the event log already
  points this way; likely SQLite → Postgres on the VPS).
- **Load and unload** terrain chunks, cities, NPCs/mobs, relics, and structures
  **within a circle of influence** around each active player; evict what's far
  away. Persist changes back to the store.
- Keep everything **location-keyed** now (it already is) so a spatial index /
  chunk grid drops in cleanly. The `init` full-map send must become chunked
  streaming as the player moves.

### C. City interiors — enter a city/region and wander it *(start with Egypt)*
A new layer: **walk into a city/region** and explore an interior sub-map with
homes and businesses appropriate to the era — visit a **smith**, **trade**,
**eat**, **sleep**, **repair**, **heal**, **buy armour/better weapons/plans**.
- **Start with Egypt**, mapped in the same tile style as the overworld, using a
  reference such as the King-of-Maps ancient-Egypt poster as a guide.
- Architecturally a **sub-map** loaded on entry (ties into B's load/unload):
  entering transitions to the interior; services are NPC buildings; leaving
  returns to the overworld at the city tile.
- Interiors should reflect the **era** (a Bronze-Age vs modern Egypt differ).

### D. Smaller / known follow-ups
- Make city ruins separately diggable; cities spawn their own NPCs/markets.
- Pre-bake myths at era transitions so a culture *inherits* legends.
- Anchor events (e.g. Younger Dryas). Fame/notability so deeds become *your* legend.
- Running stamina/cost so it isn't a free escape.
- Move the project off `/mnt/e` onto the native Linux FS for DB performance.

---

## Sessions / persistence

**Each game is fresh (for now):** when a player joins an empty world, the server
starts a brand-new World, so prior excavations/ruins/cities don't carry over. The
event log persists to SQLite but the World does not yet load from it. (Real
persistence + chunked loading is in the Roadmap.)

## Changelog

- **2026-06-22 (latest+1)** — Debug mode (O): reveal-all + click the age to jump
  to any year (assets repopulate); stone now plentiful near mountains (rocky
  foothills); world also resets when the last player leaves (robust fresh games);
  fixed nameless landmark stars.
- **2026-06-22 (latest)** — Cache death-stash → buried treasure recovered by
  excavators; fastest-route (time-weighted) auto-travel; fixed misplaced cities
  (Memphis/Alexandria/Carthage/Knossos); settlements no longer spill into water;
  colocated site+city shows one label (no duplicate "Carthage").
- **2026-06-22 (later)** — Hunters give up the chase once you're out of sight
  (`VISION_TILES` leash); camera deadzone (no scroll until 25% from an edge, then
  follow; ease-to-centre on stop) via a rAF render loop; each game starts fresh
  (reset world on first join); future cities hidden until founded.
- **2026-06-22** — Timeline extended to 50k BCE–5k AD (~1-hour arc); 18 cities
  that rise and fall on real timelines (with ruins); random mob speeds
  (60% evade / 40% fight); mountain line-of-sight occlusion; bone-site dig loot;
  map scale bar; HP bar. Added this living design doc. Recorded the world-map,
  chunked-loading, and Egypt city-interior plans.
- **2026-06-21** — Sea monsters; coastal shipwrights sell the boat plan; in-world
  date display; building purposes (hut/circle/cache); walk/run/boat speeds; kill
  loot + weapons/armour; relics with clickable clues; fog toggle; eras beyond
  Bronze; entities hidden out of vision.
- **2026-06-20** — Camera/input refactor; quiz-gated relics; real mountain ranges
  + passes; fog of war; discoverable build plans; boats; economy + NPCs +
  brigand combat; tick-pause + minimap fog.
- **2026-06-19/20** — Foundation: authoritative tick world, multiplayer, eras
  (Stone→Bronze), archaeology, DeepSeek Myth Engine, truth-vs-myth quests, the
  image-derived Mediterranean map, click-to-move, minimap, ancient sites.
  Pushed to GitHub (`moodyworks/simhumanity`, public).
