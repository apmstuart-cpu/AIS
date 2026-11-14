"""Generic boat-speed tables used when a yacht has too little history for the SOG model.

These are rough ASSUMPTIONS for planning, not measured polars. Every step that uses
one is flagged 'speed_table' in passage output. Upwind of 45 degrees TWA the yacht is
assumed to tack, and progress along the course is reduced accordingly.
"""

from __future__ import annotations

import math

TWS = [4, 6, 8, 10, 12, 14, 16, 20, 25, 30]

TABLES = {
    # ~27 m performance cruiser (e.g. Oyster 885 class), boat speed in knots
    "cruiser27": {
        45: [2.8, 4.2, 5.3, 6.2, 6.9, 7.4, 7.7, 8.0, 8.0, 7.8],
        60: [3.4, 4.8, 6.0, 7.0, 7.8, 8.4, 8.8, 9.3, 9.5, 9.4],
        90: [3.6, 5.0, 6.3, 7.4, 8.3, 9.0, 9.6, 10.4, 11.0, 11.2],
        120: [3.2, 4.6, 5.9, 7.0, 8.0, 8.8, 9.5, 10.4, 11.0, 11.3],
        150: [2.4, 3.6, 4.9, 6.1, 7.2, 8.1, 8.9, 10.0, 10.8, 11.2],
        180: [1.9, 3.0, 4.2, 5.4, 6.5, 7.4, 8.3, 9.6, 10.5, 11.0],
    },
}
DEFAULT_TABLE = "cruiser27"


def _interp(x, xs, ys):
    if x <= xs[0]:
        return ys[0] * max(x, 0) / xs[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(len(xs) - 1):
        if xs[i] <= x <= xs[i + 1]:
            return ys[i] + (ys[i + 1] - ys[i]) * (x - xs[i]) / (xs[i + 1] - xs[i])


def table_speed(tws_kn: float, twa_deg: float, table: str = DEFAULT_TABLE) -> float:
    """Speed made good along the course (kn) for true wind speed/angle."""
    t = TABLES[table]
    twa = max(0.0, min(180.0, twa_deg))
    if twa < 45:
        return _interp(tws_kn, TWS, t[45]) * math.cos(math.radians(45 - twa)) * 0.75
    angles = sorted(t)
    for a, b in zip(angles, angles[1:]):
        if a <= twa <= b:
            sa, sb = _interp(tws_kn, TWS, t[a]), _interp(tws_kn, TWS, t[b])
            return sa + (sb - sa) * (twa - a) / (b - a)
    return _interp(tws_kn, TWS, t[180])


# Named destinations with an approach route. Waypoints are illustrative planning
# points only (not navigational advice): they keep the straight-line model off land.
DESTINATIONS = {
    "rubicon": {
        "name": "Marina Rubicon, Playa Blanca, Lanzarote",
        "point": (28.857, -13.817),
        # from the north/east: pass ~8 nm off Lanzarote's east coast, then the Bocaina strait
        "via_if_north_east_of": (29.3, -13.3),
        "via": [(29.05, -13.35), (28.80, -13.72)],
    },
    "las-palmas": {
        "name": "Muelle Deportivo, Las Palmas de Gran Canaria",
        "point": (28.130, -15.418),
        "via_if_north_east_of": None,
        "via": [],
    },
}


def destination_route(name: str, start_lat: float, start_lon: float) -> list[tuple[float, float]]:
    d = DESTINATIONS[name]
    pts = []
    cond = d.get("via_if_north_east_of")
    if d["via"] and cond and start_lat > cond[0] - 0.5 and start_lon > cond[1] - 0.5:
        pts.extend(d["via"])
    pts.append(d["point"])
    return pts
