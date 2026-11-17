"""Land mask for routing, built for the area of each route.

Sources (all Natural Earth, public domain):
  * data/land_world50m.json      1:50m land, worldwide (bundled, ~1 MB)
  * data/land_eatlantic.json     1:10m land clipped to 20-46N 32W-2E (bundled: Canaries,
                                 Madeira, Morocco, Iberia in more detail)
  * /var/lib/yachtspeed/land_10m.json  optional worldwide 1:10m, if the server downloaded it
    (`yachtspeed fetch-land`); used instead of the 1:50m file when present.

Coarse by design (~2 km cells): keeps a route off islands and coasts for ETA estimates;
not for coastal navigation.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).with_name("data")
WORLD_50M = DATA_DIR / "land_world50m.json"
EATL_10M = DATA_DIR / "land_eatlantic.json"
WORLD_10M = Path("/var/lib/yachtspeed/land_10m.json")
NE_10M_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_land.geojson"


OFFSHORE_NM = 3.0  # routes keep at least this far off every coast...
HARBOUR_ZONE_NM = 12.0  # ...except within this distance of the start and finish, where only land itself counts


class LandMask:
    def __init__(self, rings, bbox, res=0.02, offshore_nm=0.0):
        self.lat0, self.lon0, self.lat1, self.lon1 = bbox
        self.res = res
        self.nrows = max(1, int(math.ceil((self.lat1 - self.lat0) / res)))
        self.ncols = max(1, int(math.ceil((self.lon1 - self.lon0) / res)))
        self.rows = [bytearray(self.ncols) for _ in range(self.nrows)]
        for ring in rings:
            self._fill(ring)
        self.offshore_nm = offshore_nm
        self.core = None
        if offshore_nm > 0:
            self.core = self.rows
            self._grow(offshore_nm)

    def _grow(self, nm):
        """Widen the land by `nm` nautical miles in every direction, so a route stays that far off."""
        import re

        mid = math.radians((self.lat0 + self.lat1) / 2)
        kr = max(1, int(math.ceil(nm / 60 / self.res)))
        kc = max(1, int(math.ceil(nm / 60 / max(0.2, math.cos(mid)) / self.res)))
        wide = []
        for row in self.rows:  # sideways
            out = bytearray(row)
            for m in re.finditer(rb"\x01+", bytes(row)):
                a, b = max(0, m.start() - kc), min(self.ncols, m.end() + kc)
                out[a:b] = b"\x01" * (b - a)
            wide.append(int.from_bytes(out, "big"))
        rows = []
        for r in range(self.nrows):  # up and down
            acc = 0
            for rr in range(max(0, r - kr), min(self.nrows, r + kr + 1)):
                acc |= wide[rr]
            rows.append(bytearray(acc.to_bytes(self.ncols, "big")))
        self.rows = rows

    def _fill(self, ring):
        """Scanline polygon fill (even-odd) of one ring into the raster."""
        ys = [p[1] for p in ring]
        r0 = max(0, int((min(ys) - self.lat0) / self.res))
        r1 = min(self.nrows - 1, int((max(ys) - self.lat0) / self.res))
        n = len(ring)
        for r in range(r0, r1 + 1):
            y = self.lat0 + (r + 0.5) * self.res
            xs = []
            for i in range(n):
                x1, y1 = ring[i - 1]
                x2, y2 = ring[i]
                if (y1 <= y < y2) or (y2 <= y < y1):
                    xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
            xs.sort()
            row = self.rows[r]
            for a, b in zip(xs[0::2], xs[1::2]):
                c0 = max(0, int(math.ceil((a - self.lon0) / self.res - 0.5)))
                c1 = min(self.ncols - 1, int(math.floor((b - self.lon0) / self.res - 0.5)))
                if c1 >= c0:
                    row[c0:c1 + 1] = b"\x01" * (c1 - c0 + 1)

    def is_land(self, lat, lon, core=False) -> bool:
        """On land, or within the offshore margin of it (core=True: on land itself)."""
        r = int((lat - self.lat0) / self.res)
        c = int((lon - self.lon0) / self.res)
        if r < 0 or c < 0 or r >= self.nrows or c >= self.ncols:
            return False  # outside the mapped area: treated as sea
        rows = self.core if core and self.core is not None else self.rows
        return bool(rows[r][c])

    def segment_clear(self, lat1, lon1, lat2, lon2, step_deg=None, skip_near=None, skip_nm=0.0) -> bool:
        """True if no sampled point along the segment is on land. Points within `skip_nm`
        of `skip_near` (lat, lon) are ignored, so a marina on the coastline can be reached."""
        from .geo import haversine_nm

        step_deg = step_deg or max(0.75 * self.res, 0.01)
        n = max(1, int(max(abs(lat2 - lat1), abs(lon2 - lon1)) / step_deg))
        for i in range(n + 1):
            f = i / n
            la, lo = lat1 + (lat2 - lat1) * f, lon1 + (lon2 - lon1) * f
            if self.is_land(la, lo):
                near = skip_near if isinstance(skip_near, list) else ([skip_near] if skip_near else [])
                dist = min((haversine_nm(la, lo, *pt) for pt in near), default=1e9)
                if dist <= skip_nm:
                    continue  # marina/harbour on the coastline
                if self.core is not None and dist <= HARBOUR_ZONE_NM and not self.is_land(la, lo, core=True):
                    continue  # leaving or approaching harbour: inside the margin is fine, land is not
                return False
        return True


@lru_cache(maxsize=4)
def _load(path: str):
    d = json.loads(Path(path).read_text())
    rings = d["rings"] if "rings" in d else _geojson_rings(d)
    return [(r, min(p[1] for p in r), max(p[1] for p in r), min(p[0] for p in r), max(p[0] for p in r))
            for r in rings]


def _geojson_rings(d):
    out = []
    for f in d["features"]:
        g = f["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        for poly in polys:
            out.append([[p[0], p[1]] for p in poly[0]])
    return out


def _intersects(item, bbox):
    _, ymin, ymax, xmin, xmax = item
    lat0, lon0, lat1, lon1 = bbox
    return not (ymax < lat0 or ymin > lat1 or xmax < lon0 or xmin > lon1)


@lru_cache(maxsize=8)
def mask_for_bbox(bbox: tuple, offshore_nm: float = OFFSHORE_NM) -> LandMask:
    """Land mask covering bbox = (lat0, lon0, lat1, lon1), at ~2 km (coarser for huge areas)."""
    lat0, lon0, lat1, lon1 = bbox
    span = max(lat1 - lat0, lon1 - lon0)
    res = max(0.02, span / 2500)
    world = str(WORLD_10M) if WORLD_10M.exists() else str(WORLD_50M)
    rings = [it[0] for it in _load(world) if _intersects(it, bbox)]
    if world == str(WORLD_50M):
        rings += [it[0] for it in _load(str(EATL_10M)) if _intersects(it, bbox)]
    return LandMask(rings, bbox, res, offshore_nm)


def default_mask() -> LandMask:
    """The Eastern Atlantic area (kept for callers that do not pass a bbox)."""
    return mask_for_bbox((20.0, -32.0, 46.0, 2.0))


def fetch_world_10m(path: Path = WORLD_10M) -> Path:
    """Download Natural Earth 1:10m land (worldwide, ~10 MB) for more detailed routing."""
    import requests

    r = requests.get(NE_10M_URL, timeout=300)
    r.raise_for_status()
    rings = _geojson_rings(r.json())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"source": "Natural Earth 1:10m land", "rings": rings}, separators=(",", ":")))
    return path
