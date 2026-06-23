"""Famous super-ancient sites placed at their real Mediterranean coordinates.

Each site has a real (lon, lat); to_tile() maps that onto the game grid via an
equirectangular transform whose bounds are calibrated to medsmall.jpg (verify
with tools/preview_landmarks.py). The World snaps each site to the nearest land
tile and turns it into a named, excavatable ruin from the dawn of the world.

Pure data + math — no image/Pillow dependency, safe to import at runtime.
"""
from __future__ import annotations

import math
import re

# Geographic bounds of medsmall.jpg, in degrees. Calibrated visually so the
# graticule and known features (Gibraltar, the Nile delta, the Bosphorus,
# Crete, Cyprus) line up with the image. x grows east, y grows south.
LON_W, LON_E = -9.5, 40.5
LAT_N, LAT_S = 46.8, 21.0

# name, lon, lat, era, short factual note (the "true record" a dig reveals).
SITES: list[dict] = [
    {"name": "Göbekli Tepe", "lon": 38.92, "lat": 37.22, "era": "c. 9500 BC",
     "note": "The oldest known monumental temple — ringed stone pillars raised "
             "by hunter-gatherers, millennia before farming or writing."},
    {"name": "Çatalhöyük", "lon": 32.83, "lat": 37.67, "era": "c. 7400 BC",
     "tile": (254, 83),  # inland on the Anatolian highland, not the coast
     "note": "One of the earliest proto-cities: a honeycomb of mud-brick houses "
             "entered through the roof, home to thousands."},
    {"name": "Jericho", "lon": 35.44, "lat": 31.87, "era": "c. 9000 BC",
     "note": "Among the oldest continuously inhabited towns, girdled by a stone "
             "wall and tower older than the Pyramids by 5,000 years."},
    {"name": "Troy", "lon": 26.24, "lat": 39.96, "era": "c. 3000 BC",
     "note": "A great citadel guarding the Dardanelles, rebuilt nine times — the "
             "Troy of later legend and the Trojan War."},
    {"name": "Knossos", "lon": 25.16, "lat": 35.30, "era": "c. 1900 BC",
     # Crete isn't a distinct island in the downsampled coastline; pin to the
     # small south-Aegean island that sits at Crete's latitude/longitude.
     "tile": (212, 105),
     "note": "The labyrinthine palace of Minoan Crete, Europe's first great "
             "civilization, with running water and bull-leaping frescoes."},
    {"name": "Mycenae", "lon": 22.76, "lat": 37.73, "era": "c. 1600 BC",
     "note": "The fortress of Bronze Age Greece — Cyclopean walls and the gold "
             "death-masks of its warrior kings."},
    {"name": "Byblos", "lon": 35.65, "lat": 34.12, "era": "c. 8800 BC",
     "note": "An ancient Phoenician port, one of the oldest cities on Earth; our "
             "word 'Bible' descends from its name."},
    {"name": "Memphis & Giza", "lon": 31.13, "lat": 29.98, "era": "c. 2600 BC",
     "tile": (252, 173),  # inland on the Egyptian mainland (south of the delta)
     "note": "The capital of the Old Kingdom and its pyramids — the last "
             "surviving wonder of the ancient world."},
    {"name": "Carthage", "lon": 10.32, "lat": 36.85, "era": "c. 814 BC",
     # NW Africa is fragmented on this map; pin to the Tunisia landmass.
     "tile": (110, 92),
     "note": "The Phoenician sea-empire that rose to rival Rome across the "
             "western Mediterranean."},
    {"name": "Ġgantija", "lon": 14.27, "lat": 36.05, "era": "c. 3600 BC",
     "note": "Malta's megalithic temples — among the oldest free-standing "
             "structures anywhere, named for the giants said to have built them."},
    {"name": "Akrotiri", "lon": 25.40, "lat": 36.35, "era": "c. 1600 BC",
     "tile": (209, 90),  # placed via the in-game debug tool
     "note": "A Minoan town on Thera, buried and preserved by the colossal "
             "eruption that may have seeded the Atlantis legend."},
    {"name": "Gadir", "lon": -6.29, "lat": 36.53, "era": "c. 1100 BC",
     "note": "Founded by Phoenicians beyond the known sea — often called the "
             "oldest city in western Europe (modern Cádiz)."},
]


def to_tile(lon: float, lat: float, width: int, height: int) -> tuple[int, int]:
    x = (lon - LON_W) / (LON_E - LON_W) * (width - 1)
    y = (LAT_N - lat) / (LAT_N - LAT_S) * (height - 1)
    return round(x), round(y)


def founded_year(era: str) -> int:
    """Parse a site's 'era' string (e.g. 'c. 2600 BC') into a numeric year so the
    site only exists in-world from then on."""
    m = re.search(r"(\d+)\s*(BC|AD)", era)
    if not m:
        return -50000
    n = int(m.group(1))
    return -n if m.group(2) == "BC" else n


def km_per_tile(width: int, height: int) -> float:
    """Real-world kilometres each map tile spans (horizontal, at mid-latitude)."""
    mid_lat = (LAT_N + LAT_S) / 2
    km_per_deg_lon = 111.32 * math.cos(math.radians(mid_lat))
    return (LON_E - LON_W) * km_per_deg_lon / max(1, width - 1)


# False claims rotated through the third quiz question, for variety.
_FALSE_CLAIMS = [
    ("was raised by Norse settlers from Scandinavia.",
     "No — it belongs to the ancient Mediterranean world."),
    ("was a fortress built during the 1800s.",
     "No — it is thousands of years old."),
    ("lies deep in the Americas, far across the ocean.",
     "No — it stands in the Mediterranean."),
    ("was first constructed of steel and concrete.",
     "No — its builders had no such materials."),
]


def site_questions(site: dict, seed: int) -> list[dict]:
    """A short true/false quiz about a site — answer it to claim the relic.

    Returns dicts {id, text, truth, basis}; the server hides `truth`/`basis`
    from the client until each is answered.
    """
    name = site["name"]
    era = site["era"]
    fc = _FALSE_CLAIMS[seed % len(_FALSE_CLAIMS)]
    raw = [
        (f"{name} dates to roughly {era}.", True,
         f"True — it belongs to {era}."),
        (f"{name} was founded by the Roman Empire.", False,
         "False — it predates Roman power (Rome's empire began ~27 BC)."),
        (f"{name} {fc[0]}", False, fc[1]),
    ]
    return [{"id": i, "text": t, "truth": tr, "basis": b}
            for i, (t, tr, b) in enumerate(raw)]
