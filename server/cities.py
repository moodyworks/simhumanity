"""Major Mediterranean cities that rise and fall on their real historical
timeline. Each city has a list of (year, stage) milestones; the current stage
(0 = ruins/none … 4 = metropolis) is interpolated from the in-world year, so
settlements grow into cities and crumble into ruins at roughly the right dates
— Athens swells around 450 BC and dwindles to a village by 1600 AD, Carthage
is razed in 146 BC, and so on.

Pure data + a stage function (no Pillow / runtime image work). Placed on tiles
via landmarks.to_tile, snapped to land by the World.
"""
from __future__ import annotations

# stage scale: 0 ruins/none · 1 hamlet · 2 town · 3 city · 4 metropolis
CITIES: list[dict] = [
    {"name": "Athens", "lon": 23.73, "lat": 37.98,
     "timeline": [(-4000, 1), (-1600, 2), (-700, 3), (-450, 4), (-100, 3),
                  (530, 2), (1200, 1), (1456, 1), (1834, 2), (1950, 4)]},
    {"name": "Rome", "lon": 12.48, "lat": 41.89,
     "timeline": [(-1000, 1), (-753, 2), (-400, 3), (-100, 4), (150, 4),
                  (400, 3), (550, 1), (900, 1), (1300, 2), (1870, 3), (1950, 4)]},
    {"name": "Carthage", "lon": 10.32, "lat": 36.85,
     "timeline": [(-814, 2), (-400, 4), (-200, 4), (-146, 0), (-29, 2),
                  (200, 3), (439, 3), (698, 0), (1900, 2)]},
    {"name": "Alexandria", "lon": 29.92, "lat": 31.20,
     "timeline": [(-331, 2), (-200, 4), (100, 4), (400, 3), (640, 2),
                  (1000, 1), (1800, 1), (1900, 3), (1950, 4)]},
    {"name": "Byzantium", "lon": 28.98, "lat": 41.01,
     "timeline": [(-660, 2), (196, 2), (330, 4), (1000, 4), (1204, 2),
                  (1453, 3), (1600, 4), (1950, 4)]},
    {"name": "Memphis", "lon": 31.25, "lat": 29.85,
     "timeline": [(-3100, 3), (-2500, 4), (-1300, 3), (-300, 2), (640, 1),
                  (1000, 0)]},
    {"name": "Troy", "lon": 26.24, "lat": 39.96,
     "timeline": [(-3000, 1), (-1700, 2), (-1250, 3), (-1180, 0), (-700, 1),
                  (-85, 1), (500, 0)]},
    {"name": "Knossos", "lon": 25.16, "lat": 35.30,
     "timeline": [(-2000, 2), (-1700, 3), (-1450, 4), (-1370, 0), (-1000, 1),
                  (-67, 1), (400, 0)]},
    {"name": "Byblos", "lon": 35.65, "lat": 34.12,
     "timeline": [(-5000, 1), (-3000, 2), (-1200, 3), (-300, 2), (1100, 1),
                  (1900, 2)]},
    {"name": "Jericho", "lon": 35.44, "lat": 31.87,
     "timeline": [(-9000, 1), (-7000, 2), (-1550, 1), (-100, 2), (700, 1),
                  (1900, 1)]},
    {"name": "Syracuse", "lon": 15.29, "lat": 37.07,
     "timeline": [(-734, 2), (-400, 4), (-211, 3), (500, 2), (878, 1),
                  (1700, 2), (1950, 2)]},
    {"name": "Massalia", "lon": 5.37, "lat": 43.30,
     "timeline": [(-600, 2), (-200, 3), (100, 2), (500, 1), (1200, 2),
                  (1800, 3), (1950, 4)]},
    {"name": "Gades", "lon": -6.29, "lat": 36.53,
     "timeline": [(-1100, 2), (-500, 2), (100, 2), (500, 1), (1500, 2),
                  (1800, 3), (1950, 3)]},
    {"name": "Neapolis", "lon": 14.25, "lat": 40.85,
     "timeline": [(-600, 2), (-300, 3), (100, 3), (500, 2), (1300, 3),
                  (1600, 4), (1950, 4)]},
    {"name": "Corduba", "lon": -4.78, "lat": 37.89,
     "timeline": [(-150, 2), (200, 2), (756, 4), (1000, 4), (1236, 2),
                  (1500, 1), (1800, 2), (1950, 3)]},
    {"name": "Venetia", "lon": 12.34, "lat": 45.44,
     "timeline": [(450, 1), (800, 3), (1200, 4), (1500, 4), (1800, 3),
                  (1950, 3)]},
    {"name": "Tyre", "lon": 35.20, "lat": 33.27,
     "timeline": [(-2750, 2), (-1000, 3), (-700, 4), (-332, 2), (100, 2),
                  (1124, 1), (1300, 0), (1900, 1)]},
    {"name": "Tarraco", "lon": 1.25, "lat": 41.12,
     "timeline": [(-500, 1), (-218, 3), (100, 3), (400, 2), (713, 1),
                  (1100, 2), (1800, 2), (1950, 3)]},
]


def city_stage(timeline: list[tuple[int, int]], year: int) -> int:
    """Interpolate a city's development stage (0–4) for a given year."""
    if year < timeline[0][0]:
        return 0  # not yet founded
    for (y0, s0), (y1, s1) in zip(timeline, timeline[1:]):
        if y0 <= year <= y1:
            t = (year - y0) / (y1 - y0) if y1 != y0 else 1.0
            return max(0, round(s0 + (s1 - s0) * t))
    return timeline[-1][1]  # hold the last stage thereafter
