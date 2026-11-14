"""Per-yacht SOG baseline: median SOG of comparable historical observations.

This is a speed-over-ground (SOG) model, not a sailing polar. AIS SOG includes current
and may include motoring or motor-sailing; the filters below (low-speed cut-off and AIS
navigational status) are only a first pass at isolating sailing.

Comparable observation: same yacht, and
    |TWS - TWS*| <= max(tws_tol_kn, tws_tol_frac * TWS*)
    |TWA - TWA*| <= twa_tol_deg           (wind angle relative to course, 0..180)
    |Hs  - Hs*|  <= wave_tol_m            (only when both wave heights are known)
Prediction = median(SOG_i - current_i) + current*, where current is the along-course
component; i.e. current is removed from history and the target current is added back.
With fewer than `min_matches` comparables the model abstains.
The p10..p90 spread is descriptive, not a calibrated forecast interval.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from .geo import along_course_current, wind_angle_to_course

MODEL_VERSION = "sog-knn-median-v1"


@dataclass
class Obs:
    report_id: int
    mmsi: str
    time_utc: str
    lat: float
    lon: float
    sog: float
    course: float
    tws: float
    twa: float
    wave: float | None
    current: float  # along-course, 0 when unknown
    current_known: bool
    voyage: int = -1


@dataclass
class Prediction:
    status: str  # ok / insufficient_support
    n: int
    median: float | None = None
    p10: float | None = None
    p25: float | None = None
    p75: float | None = None
    p90: float | None = None
    inputs: dict = field(default_factory=dict)


def quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def load_observations(conn, cfg, mmsi: str | None = None, since=None, until=None) -> list[Obs]:
    q = """SELECT r.id, r.mmsi, r.provider_time_utc, r.lat, r.lon, r.sog_kn, r.cog_deg, r.nav_status,
                  e.wind_speed_kn, e.wind_dir_from_deg, e.wave_height_m, e.current_speed_kn,
                  e.current_dir_to_deg
           FROM ais_reports r JOIN enrichment e ON e.report_id = r.id
           WHERE e.status IN ('matched','partial') AND e.wind_speed_kn IS NOT NULL
             AND e.wind_dir_from_deg IS NOT NULL AND r.sog_kn IS NOT NULL AND r.cog_deg IS NOT NULL
             AND r.sog_kn >= ? AND r.sog_kn <= ?"""
    args = [cfg.min_sog_kn, cfg.max_sog_kn]
    if mmsi:
        q += " AND r.mmsi = ?"
        args.append(mmsi)
    if since:
        q += " AND r.provider_time_utc >= ?"
        args.append(since)
    if until:
        q += " AND r.provider_time_utc < ?"
        args.append(until)
    q += " ORDER BY r.mmsi, r.provider_time_utc"
    out = []
    excluded = set(cfg.exclude_nav_status)
    for r in conn.execute(q, args):
        if r["nav_status"] is not None and r["nav_status"] in excluded:
            continue
        cur = along_course_current(r["current_speed_kn"], r["current_dir_to_deg"], r["cog_deg"])
        out.append(Obs(
            report_id=r["id"], mmsi=r["mmsi"], time_utc=r["provider_time_utc"], lat=r["lat"], lon=r["lon"],
            sog=r["sog_kn"], course=r["cog_deg"], tws=r["wind_speed_kn"],
            twa=wind_angle_to_course(r["wind_dir_from_deg"], r["cog_deg"]),
            wave=r["wave_height_m"], current=cur or 0.0, current_known=cur is not None,
        ))
    assign_voyages(out, cfg.voyage_gap_hours)
    return out


def assign_voyages(obs: list[Obs], gap_hours: float) -> None:
    """Split each yacht's track into voyages at gaps longer than `gap_hours`."""
    from .geo import parse_iso

    vid, prev = -1, None
    for o in obs:
        t = parse_iso(o.time_utc)
        if prev is None or prev[0] != o.mmsi or (t - prev[1]).total_seconds() > gap_hours * 3600:
            vid += 1
        o.voyage = vid
        prev = (o.mmsi, t)


class SogModel:
    def __init__(self, cfg, observations: list[Obs]):
        self.cfg = cfg
        self.by_mmsi: dict[str, list[Obs]] = {}
        for o in observations:
            self.by_mmsi.setdefault(o.mmsi, []).append(o)

    def yacht_median(self, mmsi: str) -> float | None:
        obs = self.by_mmsi.get(mmsi) or []
        return statistics.median(o.sog for o in obs) if obs else None

    def comparables(self, mmsi, tws, twa, wave):
        c = self.cfg
        tol = max(c.tws_tol_kn, c.tws_tol_frac * tws)
        out = []
        for o in self.by_mmsi.get(mmsi, ()):
            if abs(o.tws - tws) > tol or abs(o.twa - twa) > c.twa_tol_deg:
                continue
            if wave is not None and o.wave is not None and abs(o.wave - wave) > c.wave_tol_m:
                continue
            out.append(o)
        return out

    def predict(self, mmsi, tws, twa, wave=None, along_current=None) -> Prediction:
        comps = self.comparables(mmsi, tws, twa, wave)
        inputs = {"tws_kn": tws, "twa_deg": twa, "wave_m": wave, "along_current_kn": along_current}
        if len(comps) < self.cfg.min_matches:
            return Prediction("insufficient_support", len(comps), inputs=inputs)
        cur = along_current or 0.0
        vals = sorted(o.sog - o.current + cur for o in comps)
        return Prediction(
            "ok", len(comps), median=statistics.median(vals), p10=quantile(vals, 0.1),
            p25=quantile(vals, 0.25), p75=quantile(vals, 0.75), p90=quantile(vals, 0.9), inputs=inputs,
        )
