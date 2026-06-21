"""Dev tool: overlay the lon/lat graticule + ancient sites on medsmall.jpg to
visually calibrate server/landmarks.py bounds. Run, then open the PNG.

  ./.venv/bin/python -m tools.preview_landmarks
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from server.landmarks import LAT_N, LAT_S, LON_E, LON_W, SITES, to_tile

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "medsmall.jpg"
OUT = ROOT / "med_landmarks_preview.png"


def main() -> None:
    im = Image.open(SRC).convert("RGB")
    W, H = im.size
    # Upscale 3x so labels are readable.
    scale = 3
    im = im.resize((W * scale, H * scale), Image.NEAREST)
    d = ImageDraw.Draw(im)

    def px(lon: float, lat: float) -> tuple[int, int]:
        x, y = to_tile(lon, lat, W, H)
        return x * scale, y * scale

    # Graticule every 5 degrees.
    lon = -10
    while lon <= 45:
        x0, y0 = px(lon, LAT_N)
        x1, y1 = px(lon, LAT_S)
        d.line([(x0, y0), (x1, y1)], fill=(255, 255, 255, 80), width=1)
        d.text((x0 + 2, 2), f"{lon}E", fill=(255, 255, 0))
        lon += 5
    lat = 20
    while lat <= 50:
        x0, y0 = px(LON_W, lat)
        x1, y1 = px(LON_E, lat)
        d.line([(x0, y0), (x1, y1)], fill=(255, 255, 255, 80), width=1)
        d.text((2, y0 + 1), f"{lat}N", fill=(255, 255, 0))
        lat += 5

    # Sites.
    for s in SITES:
        x, y = px(s["lon"], s["lat"])
        r = 4
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 60, 60),
                  outline=(0, 0, 0))
        d.text((x + 6, y - 5), s["name"], fill=(255, 255, 255))

    im.save(OUT)
    print(f"Bounds: lon [{LON_W}, {LON_E}]  lat [{LAT_S}, {LAT_N}]")
    print(f"Wrote {OUT.name} ({im.size[0]}x{im.size[1]})")


if __name__ == "__main__":
    main()
