# simhumanity — Living Design Document

> **This is a living document. Update it whenever a feature is added or changed.**
> Add a dated entry to the [Changelog](#changelog) and edit the relevant section.

A multiplayer sim that carries players through the ages of human history
(Stone → Space). Real history fires as scheduled **anchor events** and an
era/year clock; everything between emerges from players, NPCs, and AI.

The signature mechanic — **Living History / Myth Engine**: a player's real
actions become the **ruins** later players dig up and the **distorted myths**
they inherit and argue about. The past literally becomes the future's content.

> **Current version: the real-world Earth map** (`/world` — `client/world.js`,
> `server/worldgame.py`). It is the active game and all current work lands here.
> The image-derived **Mediterranean map** (`/` — `client/game.js`, `server/world.py`)
> is the original test bed that the world map was ported from; it still runs but is
> no longer the focus. Sections below that describe "the test map" predate the port —
> the world map mirrors those systems (terrain, fog, boats, economy, NPCs, combat,
> archaeology) on real Earth geography. See the [Changelog](#changelog) for the port
> history and the latest land/water work.

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
  all mobs visible); turns the **age/year label into a control — click it to type
  a year** and jump the world clock there (assets repopulate); and shows a
  **place-tool dropdown** — pick a city/site, then **click the map to relocate
  it**. Relocations are **permanent** (saved to `place_overrides.json`, applied
  over the defaults on every world build). This is the reliable way to accurize
  placements on the stylized test map (human-in-the-loop, since lon/lat→tile is
  unreliable here).

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
- **NPCs respect their medium (world map).** Every mob has a medium — **land**,
  **water**, or **air** (eagles/rocs go anywhere). Spawn *and* every movement step
  pass through one check (`worldgame._medium_ok`) that reads the **exact rendered
  tile colour** the player sees (`rendered.water_state`, kept byte-identical to the
  client's `isWaterTile`): a land mob needs dry land, a **sea mob needs genuine open
  ocean** (`is_open_water`, ≥60% water in a 7×7 — so it never sits on the baked
  rivers/lakes/coastal slivers that read as "on land"), and an **unknown** tile
  (untiled/unreadable) is never valid. An **end-of-tick sweep** despawns any mob that
  still ends up off its medium (self-healing chunk-load flips and PIL-vs-browser JPEG
  decode drift). Water classification is **blue-dominance** down to near-black
  (`B > R+4 & B > G & B > 6`) so even the darkest polar/deep sea counts while dark
  *land* (green-dominant forest, neutral tundra) and pure-black void never do.
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

**Each game is fresh (for now):** every server (re)start **wipes the persisted
event-log DB**, and the world also resets when a player joins an empty world or
the last player leaves — so nothing carries over a hard reset. (Real persistence
+ chunked loading is in the Roadmap.)

## Placing real locations (cities & sites) — how to accurize

Cities/sites are placed by converting real `(lon, lat)` to a map tile via a
**linear transform** with hand-estimated border coordinates (`LON_W/E`,
`LAT_N/S` in `landmarks.py`). That estimate is imperfect, so some placements
drift (worst in the east), landing in the sea and snapping to the wrong land. To
correct a placement:
1. **Hand-pin** it: add a `"tile": (x, y)` to the entry (cities.py / landmarks.py),
   verified against a gridded render of `medsmall.jpg` (overlay a tile grid, read
   off the right tile). This is what's done for the few the transform misplaces.
2. Or **recalibrate the transform** globally: pin a few unambiguous features
   (Gibraltar, the Nile delta, the Bosphorus) to their true pixels and solve for
   accurate border coordinates — fixes everything at once.

For the **real world map** (Roadmap), bake accurate coordinates in from the start
(a proper projection, or author placements directly on the new map).

## Changelog

- **2026-06-27 (c)** — **Dark/deep blue sea now counts as water (incl. near-black
  polar).** The water test required `B > 100`, so shadowed/deep blue sea read as
  **land** — letting land mobs wander into dark water. Now `B > R+4 & B > G & B > 6`:
  kept **blue-dominance** (what separates sea from every dark *land* type —
  forest/jungle is green-dominant, tundra/shadow neutral) but pushed the thresholds
  to the black end so even the darkest polar/deep sea off Antarctica (`[2,5,20]`)
  classifies as water. `B > 6` still rejects pure-black void/poles and `B > R+4`
  rejects neutral shadow, so nothing land-side leaks (verified:
  rainforest/boreal/tundra/Sahara flat at ~0–3%; Antarctic-edge and southern ocean
  rescued to ~70–80%). Changed identically in the server classifier (`rendered.py
  _classify`) and the client (`world.js isWaterTile`) — the two must match.
- **2026-06-27 (b)** — **Sea mobs stay in open ocean (not rivers/lakes/coast).** A
  sea serpent kept appearing "on land". Cause: `tools/tile_world.py` **bakes**
  Natural Earth **rivers/lakes** and the GEBCO sea into the world tiles, so the
  displayed map carries thin water lines through land that aren't in the raw
  satellite source. The server reads the same tiles and correctly saw a river pixel
  as blue — but a mob on a 1-tile river/lake/coastal-sliver tile reads as "on land"
  to the eye. Fix: a sea mob now requires **genuine open ocean** (`is_open_water`,
  ≥60% water in a 7×7) to *stand*, not just to spawn — rivers, lakes and shoreline
  slivers never qualify, and the wide neighbourhood vote is robust to PIL-vs-browser
  JPEG-decode drift on thin features. The end-of-tick sweep culls any sea mob no
  longer in open water.
- **2026-06-27** — **NPCs respect land/water (world map).** Land mobs were drifting
  into water and sea mobs onto land. Root cause: the rendered-tile water test
  reported an **unknown** tile (untiled or unreadable chunk) as land, so a land mob
  treated any colour it couldn't read as walkable, and nothing re-validated a mob
  after chunks loaded. Fixes: `rendered.py` gains `water_state()` (tri-state
  water/land/**unknown**); `worldgame.py` replaces `_passable` with one
  `_medium_ok(air, water, …)` used by **both** spawn and every movement step,
  testing the exact sea-blue pixel the player sees and treating **unknown as never
  appropriate** (a land/water mob won't step onto or spawn on a tile whose colour
  can't be verified; air is unrestricted). An **end-of-tick sweep** despawns any mob
  that still ends up off its medium (self-heals a chunk loading under a standing
  mob, a transient read, or a PIL-vs-browser JPEG threshold flip); the top-up
  re-seeds a valid one. Coarse 8 km terrain still backs the brief startup window
  before chunks are readable, and the sweep cleans up once they come online.
- **2026-06-24** — **Feel/parity round + grid-lock.** Click-to-move; smooth NPC
  wander (held heading); **populated resource nodes** (gatherable pips that deplete
  & re-seed); **talk (F)** to wanderers (rumours) / merchants (sell); **weapons &
  armour** auto-applied; **plans tech-tree** (learn build recipes by digging) with
  a **build dropdown (B)**; **relics** as objects + panel (I); cities/sites **snap
  onto land**; and **grid-locked movement** — player + NPCs + resources + structures
  + cities are all **constrained to tile squares**, moving smoothly between and
  always landing centred (positions are tile centres; player/NPCs tile-step).
  Then **site excavation quizzes** (dig a famous site → true/false claims → a relic
  + renown + the site's lost build plan), **fog of war** (see a radius around you;
  explored ground stays dimly known; toggle O), and **truth-vs-myth quests** (dig a
  ruin → judge its legend's claims True/Embellished + dig a rumoured hoard for
  renown/loot). Still open: debug tools (year-jump, place-city), fog on the minimap.
- **2026-06-23 (j)** — **Parity pass on the three flagged areas: NPCs, HUD, minimap.**
  **NPCs** now match the test map: per-mob rolled hp/atk/speed (`_roll_speed`),
  **sea monsters live on water and only hunt boaters (shore is safe)**, separate
  aggro (spot) vs give-up leash, fight-back, and weighted **coin/goods/gear/relic**
  loot; flavor names + HP bars + hostile outline; boats move at half pace. **HUD**
  rebuilt as structured DOM with a **graphical green→amber→red HP bar**, era/year,
  coin, inventory, and build list (was a plain text blob). **Minimap** is now
  **interactive** — click/drag to "look" (camera decouples from the player; moving
  re-attaches) with a viewport rectangle. Remaining: relics/learnable plans, site
  excavation quizzes, fog-of-war, weapons/armour equip, quests, debug tools.
- **2026-06-23 (i)** — **Living history + richer resources on the world.** The real
  **cities** (Athens, Rome, Carthage, Memphis…) now **rise and fall on the map** on
  their historical timeline — drawn sized hamlet→metropolis from the era clock —
  and the famous **ancient sites** (Göbekli Tepe, Jericho, Troy…) appear
  **date-gated** once founded (snapshot `cities`/`sites`, reusing `CITIES`/`city_stage`
  + `SITES`/`founded_year`). **Resources** enriched: **fish** (sail out to water),
  **ore** (high peaks), plus stone/wood/food by biome. Both show on the minimap.
  Remaining test-map extras: fog-of-war, weapons/armour, relics/plans, site
  excavation quizzes, quests, sea monsters, debug tools.
- **2026-06-23 (h)** — **Trading + Myth Engine + boats — world-map parity reached.**
  **Trade (F)** sells your goods to a nearby merchant for **coin**. **Boats** are
  crafted (8 wood) and let you **cross water**. **Dig** now drives the **Myth
  Engine**: the *first* excavator gets the true record *and* the DeepSeek Historian
  spins it into a distorted **legend** cached on the ruin — later diggers get the
  myth, not the truth ("a hut raised by Bob" → "Bob the Sky-Father split the
  mountains"). The real-Earth world map is now a full multiplayer game matching the
  test map's systems. (Reuses `ai.MythEngine`; stub/DeepSeek/ollama providers.)
- **2026-06-23 (g)** — **NPCs + combat on the world.** NPCs (wanderer / merchant /
  brigand / monster) spawn **around players** (the planet's too big to simulate
  globally), wander or chase; brigands & monsters attack you. **Attack (R)** the
  nearest NPC in reach — killing drops loot. Players have **HP**; death respawns
  you at your spawn city and drops half your goods. NPCs render by kind + on the
  minimap; HP shows in the HUD. Verified: spawn, monster kill → respawn, brigand
  kill → loot. Still to port: economy/trading, the DeepSeek Myth Engine, boats.
- **2026-06-23 (f)** — **Eras + archaeology on the world (the Living-History core).**
  A world clock advances through `ERA_DATES` (`WORLD_YEARS_PER_SEC`, default 4).
  When the era turns, structures built in a **prior** era **decay into ruins**;
  **dig (E)** a ruin to recover its materials + an **artifact** and reveal who built
  it and when (the true record, before the Myth Engine distorts it). HUD shows the
  age/year; ruins draw as mounds. Verified: bronze hut → feudal era → ruin → dig.
  Still to port: NPCs, combat, economy/trading, the DeepSeek Myth Engine, boats.
- **2026-06-23 (e)** — **Land/water movement + gather/build on the world.** Server
  terrain (`worldterrain.py`): land/water + a coarse biome from GEBCO topo + the
  Blue Marble overview (8 km cells, water = sea-level fraction). You **can't walk
  on water** (the client reads the rendered sea-blue; boats later). **Gather (G)**
  yields wood (forest), stone (mountains) or food (plains) by biome; **build
  (1/2/3)** places hut / cairn / granary — server-authoritative inventory +
  **shared structures** (everyone sees them). Toward test-map parity, still to
  port: eras/season clock, archaeology (dig), NPCs, combat, economy, Myth Engine.
- **2026-06-23 (d)** — **World map becomes a multiplayer game (port begins).** Whole
  planet tiled (16,200 chunks). `/world` now: pick a **city of the age** to spawn
  (`/world/spawns?year=`), connect over **`/world/ws`**, and **see other players**
  (markers + names, minimap dots, online count). Server tracks presence
  (`worldgame.py`) and broadcasts at 8 Hz, separate from the test-map game. The
  world **wraps E/W** (circumnavigate) with the **poles as walls**. Next:
  land/water movement, gather/build, eras, NPCs on the real Earth.
- **2026-06-23 (c)** — **Sea painted from GEBCO, not colour.** Colour-classifying
  water failed on rivers, polar seas and anti-aliased coasts. Now `tile_world.py`
  composites the sea from authoritative **GEBCO**: water = topo elevation 0,
  depth-shaded by bathymetry (1km, 2× upsampled to the 500m grid). Clean coasts +
  full polar oceans + smooth real depth, and the basis for ice-age sea levels.
  Land keeps the satellite colour. **Rivers/lakes above sea level still pending —
  next is a Natural Earth rivers overlay.** Viewer also gained a minimap, plain
  Blue Marble colour, pixel-snapped chunks, and 10× run.
- **2026-06-23 (b)** — **World-map vertical slice is walkable.** New `/world` page
  (`client/world.html` + `world.js`) streams the real-Earth chunks and lets you
  **walk a pixel-for-pixel patch of Earth** (1 source pixel = 1 tile, real colours,
  3×3-ring chunk loading, untiled regions stay dark). Spawns at **Uruk** (the HUD's
  lat/lon confirms the equirectangular placement is exact). Server serves chunks at
  `/tiles` + the viewer at `/world`. Colour source = the combined relief+bathymetry
  500m tiles (`world.topo.bathy.*`). So far only the **C1 quadrant** (Fertile
  Crescent / Mediterranean / India-NW) is tiled. Next: tile the rest, era-city
  spawn, fold into the game, topo→mountains + bath→sea level.
- **2026-06-23** — **World-map pipeline started (will replace the test map).**
  `tools/tile_world.py` slices a full-res equirectangular source into **1px=1tile**
  game chunks (`world_tiles/c{col}_r{row}` + `manifest.json`) for 3×3-ring chunk
  loading. Supports the NASA Blue Marble **500m** 8-tile grid (A1–D2 →
  86400×43200 = **3.7 billion tiles**), tiled one source tile at a time (memory-safe)
  with correct global chunk coords. Minimap baked from the **8km/px** Blue Marble →
  `client/minimap.jpg`. Ground will render **real pixel colours** (not the 9-class
  palette). Public-domain imagery only (not Google). Next: chunk-streaming loader +
  colour→terrain classification. Memory: [[simhumanity-world-map]].
- **2026-06-22 (latest+2)** — Ancient sites are **date-gated** (hidden/undiggable
  before their founding year — e.g. Memphis & Giza won't appear in 4500 BCE);
  best-effort re-placement of Carthage (Tunisia landmass) and the Egypt cluster.
  Known limitation: this stylized map's coastline is fragmented and not a true
  projection, so precise lon/lat placement is unreliable — see "Placing real
  locations". Server-orphan pileup fixed (run.sh stops the old server first).
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
