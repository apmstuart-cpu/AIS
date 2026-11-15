"""Boat-speed tables (polars) used when a yacht has too little history for the SOG model.

Each table gives boat speed through the water (kn) by true wind speed (TWS) and true
wind angle (TWA), plus optimum beat and run angles. Progress along a course that is
tighter than the beat angle or deeper than the gybe angle assumes the yacht tacks or
gybes at the optimum angle: progress = V(optimum) * cos(angle off course).

Every passage step that uses a table is flagged 'speed_table' in the output.
`factor` scales a table (e.g. 0.85 for a cruising yacht against a racing VPP).
"""

from __future__ import annotations

import math

# Oyster 885 GT "BABIANA": VPP from her ORC Club certificate (GBR, RefNo 038600051DB,
# CertNo 100701, issued 2026-10-07), public ORC data feed. ORC allowances (s/nm) converted
# to knots. This is a RACING prediction: race crew, clean hull, 624 m2 asymmetric spinnaker.
# A cruising 885 (e.g. a 2013 Series I with cruising sails, loaded, short-handed) will be
# slower; use a factor (0.8-0.9 is typical) until real data calibrates it.
_ORC_885GT = {
    "tws": [4, 6, 8, 10, 12, 14, 16, 20, 24],
    "speeds": {
        52: [5.10, 7.28, 8.64, 9.57, 10.10, 10.41, 10.61, 10.88, 11.00],
        60: [5.73, 7.94, 9.27, 10.04, 10.55, 10.83, 11.01, 11.24, 11.36],
        75: [6.44, 8.61, 9.94, 10.63, 10.98, 11.26, 11.45, 11.70, 11.86],
        90: [6.56, 8.69, 10.08, 10.88, 11.32, 11.58, 11.76, 12.08, 12.35],
        110: [5.94, 8.09, 9.59, 10.64, 11.28, 11.69, 12.02, 12.60, 13.03],
        120: [5.31, 7.50, 9.28, 10.58, 11.33, 11.80, 12.12, 12.80, 13.45],
        135: [4.60, 6.77, 8.58, 9.97, 10.95, 11.58, 12.08, 13.06, 14.05],
        150: [3.79, 5.62, 7.22, 8.60, 9.77, 10.72, 11.38, 12.32, 13.26],
        165: [2.61, 3.96, 5.29, 6.54, 7.72, 8.76, 9.68, 11.05, 11.90],
        180: [2.25, 3.40, 4.55, 5.68, 6.75, 7.74, 8.66, 10.24, 11.32],
    },
    "beat_vmg": [3.16, 4.60, 5.69, 6.39, 6.81, 7.09, 7.29, 7.57, 7.67],
    "beat_angle": [49.8, 44.9, 42.6, 41.4, 40.7, 40.1, 39.7, 39.1, 39.2],
    "run_vmg": [3.28, 4.87, 6.25, 7.45, 8.46, 9.30, 9.93, 10.83, 11.62],
    "gybe_angle": [138.1, 140.3, 141.2, 145.6, 148.9, 151.7, 153.4, 159.2, 160.4],
    "source": "ORC certificate, Oyster 885 GT BABIANA (GBR), 2026-10-07",
}

# Rough generic ~27 m cruiser (no spinnaker). An ASSUMPTION, not a measured polar.
_CRUISER27 = {
    "tws": [4, 6, 8, 10, 12, 14, 16, 20, 25, 30],
    "speeds": {
        45: [2.8, 4.2, 5.3, 6.2, 6.9, 7.4, 7.7, 8.0, 8.0, 7.8],
        60: [3.4, 4.8, 6.0, 7.0, 7.8, 8.4, 8.8, 9.3, 9.5, 9.4],
        90: [3.6, 5.0, 6.3, 7.4, 8.3, 9.0, 9.6, 10.4, 11.0, 11.2],
        120: [3.2, 4.6, 5.9, 7.0, 8.0, 8.8, 9.5, 10.4, 11.0, 11.3],
        150: [2.4, 3.6, 4.9, 6.1, 7.2, 8.1, 8.9, 10.0, 10.8, 11.2],
        180: [1.9, 3.0, 4.2, 5.4, 6.5, 7.4, 8.3, 9.6, 10.5, 11.0],
    },
    "beat_angle": [45] * 10,
    "gybe_angle": [180] * 10,  # no spinnaker: assume she sails the course directly downwind
    "source": "generic assumption",
}

def _derive(base, speed_scale_by_angle, beat_vmg_scale, beat_angle_add, source):
    """A sister-ship variant of a table: per-angle speed scale (interpolated) and a
    separate upwind adjustment."""
    angs = sorted(speed_scale_by_angle)

    def scale(a):
        if a <= angs[0]:
            return speed_scale_by_angle[angs[0]]
        if a >= angs[-1]:
            return speed_scale_by_angle[angs[-1]]
        for x, y in zip(angs, angs[1:]):
            if x <= a <= y:
                return speed_scale_by_angle[x] + (speed_scale_by_angle[y] - speed_scale_by_angle[x]) * (a - x) / (y - x)

    out = {k: v for k, v in base.items() if k not in ("speeds", "beat_vmg", "beat_angle", "run_vmg", "source")}
    out["speeds"] = {a: [round(v * scale(a), 3) for v in vals] for a, vals in base["speeds"].items()}
    out["beat_vmg"] = [round(v * beat_vmg_scale, 3) for v in base["beat_vmg"]]
    out["beat_angle"] = [round(v + beat_angle_add, 1) for v in base["beat_angle"]]
    out["run_vmg"] = [round(v * scale(180), 3) for v in base["run_vmg"]]
    out["source"] = source
    return out


# Standard Oyster 885 (e.g. ALIBI OF LONDON, 2013): same rig as BABIANA but a less efficient
# keel than the GT. Keel differences mostly cost pointing and upwind speed, fading off the
# wind. These scales are a JUDGEMENT, not measured; calibrate against her own track data.
_OYSTER885_STD = _derive(
    _ORC_885GT,
    speed_scale_by_angle={52: 0.95, 60: 0.95, 75: 0.97, 90: 0.98, 110: 0.99, 180: 0.99},
    beat_vmg_scale=0.93, beat_angle_add=2.0,
    source="derived from Oyster 885 GT BABIANA ORC VPP: upwind VMG -7%, beat angle +2 deg, "
           "-5% at 52-60 deg tapering to -1% from 110 deg (keel difference; judgement)",
)

TABLES = {"cruiser27": _CRUISER27, "oyster885gt_orc": _ORC_885GT, "oyster885_std": _OYSTER885_STD}
DEFAULT_TABLE = "cruiser27"


def _interp(x, xs, ys):
    if x <= xs[0]:
        return ys[0] * max(x, 0) / xs[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(len(xs) - 1):
        if xs[i] <= x <= xs[i + 1]:
            return ys[i] + (ys[i + 1] - ys[i]) * (x - xs[i]) / (xs[i + 1] - xs[i])


def _beat(t, tws):
    """(beat angle, VMG to windward) at this wind speed."""
    ang = _interp(tws, t["tws"], t["beat_angle"]) if tws > 0 else t["beat_angle"][0]
    if "beat_vmg" in t:
        vmg = _interp(tws, t["tws"], t["beat_vmg"])
    else:  # generic table: speed at its tightest angle, with an allowance for tacking losses
        vmg = _speed_at(t, tws, ang, beat=None) * math.cos(math.radians(ang)) * 0.85
    return ang, vmg


def _run(t, tws):
    """(gybe angle, VMG downwind) at this wind speed."""
    ang = _interp(tws, t["tws"], t["gybe_angle"]) if tws > 0 else t["gybe_angle"][0]
    if "run_vmg" in t:
        return ang, _interp(tws, t["tws"], t["run_vmg"])
    return ang, _speed_at(t, tws, ang, beat=None) * abs(math.cos(math.radians(ang)))


def _speed_at(t, tws, twa, beat="auto"):
    """Boat speed at a TWA from the table, with the optimum beat as an extra point below
    the table's tightest angle (so speeds between the beat angle and e.g. 52 deg are sane)."""
    pts = {a: _interp(tws, t["tws"], v) for a, v in t["speeds"].items()}
    if beat == "auto" and "beat_vmg" in t:
        b_ang, b_vmg = _beat(t, tws)
        if b_ang < min(pts):
            pts[b_ang] = b_vmg / math.cos(math.radians(b_ang))
    angs = sorted(pts)
    twa = max(angs[0], min(angs[-1], twa))
    for a, b in zip(angs, angs[1:]):
        if a <= twa <= b:
            return pts[a] + (pts[b] - pts[a]) * (twa - a) / (b - a)
    return pts[angs[-1]]


def table_speed(tws_kn: float, twa_deg: float, table: str = DEFAULT_TABLE, factor: float = 1.0) -> float:
    """Progress along the course (kn, through the water) for true wind speed/angle.

    Inside the beat angle the yacht tacks: progress = VMG_beat / cos(TWA).
    Deeper than the gybe angle it gybes: progress = VMG_run / |cos(TWA)|, or sails the
    course directly if that is faster.
    """
    t = TABLES[table]
    twa = max(0.0, min(180.0, twa_deg))
    b_ang, b_vmg = _beat(t, tws_kn)
    g_ang, g_vmg = _run(t, tws_kn)
    if twa < b_ang:
        v = b_vmg / math.cos(math.radians(twa))
    elif twa > g_ang:
        v = max(_speed_at(t, tws_kn, twa), g_vmg / abs(math.cos(math.radians(twa))))
    else:
        v = _speed_at(t, tws_kn, twa)
    return v * factor


def wave_factor(hs_m, wave_from_deg, course_deg, period_s=None):
    """Multiplier on speed through the water for sea state (polars assume flat water).

    A deliberately simple HEURISTIC until a yacht's own data can calibrate it:
      relative angle 0 = head seas, 180 = following seas
      head seas (0-60 deg):   loss 2.5 % per m^2 of significant wave height
      beam seas (60-120 deg): loss 1.2 % per m^2
      quartering/following (>120 deg): gain 1 % per m, capped at +3 % (surfing)
    Short, steep seas (period < 6 s) count 30 % more. Losses are capped at 35 %.
    Returns (factor, relative_angle) or (1.0, None) without wave data.
    """
    if hs_m is None or wave_from_deg is None or course_deg is None:
        return 1.0, None
    rel = abs((wave_from_deg - course_deg + 180) % 360 - 180)  # 0 = waves on the nose
    steep = 1.3 if (period_s is not None and period_s < 6) else 1.0
    hs = max(0.0, min(hs_m, 6.0))
    if rel <= 60:
        f = 1 - 0.025 * hs * hs * steep
    elif rel <= 120:
        w = (rel - 60) / 60  # blend head -> beam
        f = 1 - (0.025 * (1 - w) + 0.012 * w) * hs * hs * steep
    else:
        w = min(1.0, (rel - 120) / 30)  # beam -> following
        beam = 1 - 0.012 * hs * hs * steep
        follow = 1 + min(0.03, 0.01 * hs)
        f = beam * (1 - w) + follow * w
    return max(0.65, f), rel


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
    "tenerife": {
        "name": "Marina Santa Cruz de Tenerife",
        "point": (28.465, -16.245),
        # from the north-east: round Punta de Anaga, keeping off the north coast
        "via_if_north_east_of": (29.0, -16.0),
        "via": [(28.62, -16.05)],
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
