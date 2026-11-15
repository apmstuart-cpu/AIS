"""Isochrone weather routing: fastest track from a position to a destination using a
yacht's polar and a gridded forecast. Used to estimate ETAs (a model output, not
navigational advice).

Method (as in common routing software):
  * from every frontier point, try headings every `heading_step` degrees for `step_h`;
    boat speed from the raw polar at that true wind angle (zero inside the no-go zone),
    scaled by the polar factor and a sea-state factor, plus the ocean current vector;
  * a change of tack or gybe costs `maneuver_min` minutes;
  * reject moves whose track crosses land (Natural Earth mask, ~2 km);
  * prune: keep the point furthest from the start in each `sector_deg` bearing sector;
  * stop when the destination can be reached directly within a step; trace back.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .geo import haversine_nm, initial_bearing, iso, utcnow
from .speedtable import boat_speed, wave_factor


def _uv_from(speed, dir_from):  # meteorological: direction the wind comes FROM
    r = math.radians(dir_from)
    return -speed * math.sin(r), -speed * math.cos(r)


def _uv_to(speed, dir_to):  # oceanographic: direction the current flows TO
    r = math.radians(dir_to)
    return speed * math.sin(r), speed * math.cos(r)


class WeatherGrid:
    """Hourly forecast on a regular lat/lon grid, fetched per cell for a date range and
    cached in the DB (re-used while fresher than `max_age_h`). Sampled with bilinear
    interpolation in space and linear interpolation in time; directions as vectors."""

    def __init__(self, conn, client, bbox, t_start: datetime, t_end: datetime, wind_provider: str,
                 res=0.5, use_marine=True, max_age_h=6.0):
        self.conn, self.client = conn, client
        self.lat0, self.lon0, self.lat1, self.lon1 = bbox
        self.res, self.wind_provider, self.use_marine = res, wind_provider, use_marine
        self.t0 = t_start.replace(minute=0, second=0, microsecond=0)
        self.t_end = t_end
        self.max_age_h = max_age_h
        self.lats = [round(self.lat0 + i * res, 4) for i in range(int(round((self.lat1 - self.lat0) / res)) + 1)]
        self.lons = [round(self.lon0 + j * res, 4) for j in range(int(round((self.lon1 - self.lon0) / res)) + 1)]
        self.wind: dict = {}  # (i, j) -> {hour_index: (u, v, speed)}
        self.marine: dict = {}  # (i, j) -> {hour_index: (hs, wave_u, wave_v, period, cur_u, cur_v)}
        self.fetches = 0
        self.missing_cells = 0

    def _hidx(self, t: datetime) -> float:
        return (t - self.t0).total_seconds() / 3600

    def _rows(self, provider, kind, lat, lon):
        from .weather import FetchError, QuotaExceeded, fetch_metno, store_rows

        start, end = self.t0.strftime("%Y-%m-%d"), self.t_end.strftime("%Y-%m-%d")
        fresh = iso(utcnow() - timedelta(hours=self.max_age_h))
        rows = self.conn.execute(
            "SELECT * FROM weather_cache WHERE provider=? AND kind=? AND cell_lat=? AND cell_lon=? "
            "AND time_utc >= ? AND time_utc <= ? AND fetched_utc >= ? ORDER BY time_utc",
            (provider, kind, lat, lon, iso(self.t0), iso(self.t_end + timedelta(hours=1)), fresh)).fetchall()
        expected = int(self._hidx(self.t_end)) + 1
        if rows and (len(rows) >= expected * 0.9 or provider == "metno"):
            return rows  # fresh enough (MET Norway is 6-hourly after ~60 h, so fewer rows)
        try:
            if provider == "metno":
                fetched = fetch_metno(self.client, lat, lon)
            else:
                fetched = self.client.fetch(provider, kind, lat, lon, start, end)
            self.fetches += 1
            store_rows(self.conn, provider, kind, lat, lon, fetched)
        except (FetchError, QuotaExceeded):
            self.missing_cells += 1
            return rows
        return self.conn.execute(
            "SELECT * FROM weather_cache WHERE provider=? AND kind=? AND cell_lat=? AND cell_lon=? "
            "AND time_utc >= ? AND time_utc <= ? ORDER BY time_utc",
            (provider, kind, lat, lon, iso(self.t0 - timedelta(hours=6)), iso(self.t_end + timedelta(hours=7)))).fetchall()

    def load(self):
        for i, la in enumerate(self.lats):
            for j, lo in enumerate(self.lons):
                w = {}
                for r in self._rows(self.wind_provider, "wind", la, lo):
                    if r["wind_speed_kn"] is None or r["wind_dir_from_deg"] is None:
                        continue
                    t = datetime.fromisoformat(r["time_utc"].replace("Z", "+00:00"))
                    u, v = _uv_from(r["wind_speed_kn"], r["wind_dir_from_deg"])
                    w[self._hidx(t)] = (u, v, r["wind_speed_kn"])
                if w:
                    self.wind[(i, j)] = w
                if self.use_marine:
                    m = {}
                    for r in self._rows("om_marine", "marine", la, lo):
                        t = datetime.fromisoformat(r["time_utc"].replace("Z", "+00:00"))
                        hs = r["wave_height_m"]
                        wu, wv = _uv_from(hs or 0.0, r["wave_dir_from_deg"] or 0.0)
                        cu, cv = _uv_to(r["current_speed_kn"] or 0.0, r["current_dir_to_deg"] or 0.0)
                        m[self._hidx(t)] = (hs, wu, wv, r["wave_period_s"], cu, cv)
                    if m:
                        self.marine[(i, j)] = m
        ends = [max(v) for v in self.wind.values() if v]
        self.data_end_h = sorted(ends)[len(ends) // 2] if ends else 0.0
        return self

    hold_beyond_h = 6.0  # set large to persist the last forecast hour beyond the forecast end

    def _time_interp(self, series: dict, h: float):
        if not series:
            return None
        keys = sorted(series)
        if h <= keys[0]:
            return series[keys[0]] if keys[0] - h <= 6 else None
        if h >= keys[-1]:
            return series[keys[-1]] if h - keys[-1] <= self.hold_beyond_h else None
        lo = max(k for k in keys if k <= h)
        hi = min(k for k in keys if k >= h)
        if hi == lo:
            return series[lo]
        f = (h - lo) / (hi - lo)
        a, b = series[lo], series[hi]
        return tuple((x if y is None else y if x is None else x + (y - x) * f) for x, y in zip(a, b))

    def _corners(self, lat, lon):
        fi = (lat - self.lat0) / self.res
        fj = (lon - self.lon0) / self.res
        i0, j0 = int(math.floor(fi)), int(math.floor(fj))
        di, dj = fi - i0, fj - j0
        return [((i0, j0), (1 - di) * (1 - dj)), ((i0 + 1, j0), di * (1 - dj)),
                ((i0, j0 + 1), (1 - di) * dj), ((i0 + 1, j0 + 1), di * dj)]

    def sample(self, lat, lon, t: datetime):
        h = self._hidx(t)
        out = {}
        acc, wsum = [0.0, 0.0, 0.0], 0.0
        for key, wt in self._corners(lat, lon):
            v = self._time_interp(self.wind.get(key, {}), h)
            if v is not None and wt > 0:
                acc = [acc[0] + wt * v[0], acc[1] + wt * v[1], acc[2] + wt * v[2]]
                wsum += wt
        if wsum <= 0:
            return None
        u, v, spd = acc[0] / wsum, acc[1] / wsum, acc[2] / wsum
        out["wind_speed_kn"] = spd
        out["wind_dir_from_deg"] = (math.degrees(math.atan2(-u, -v)) + 360) % 360
        acc, wsum = [0.0] * 6, 0.0
        for key, wt in self._corners(lat, lon):
            v = self._time_interp(self.marine.get(key, {}), h)
            if v is not None and v[0] is not None and wt > 0:
                acc = [a + wt * (x or 0.0) for a, x in zip(acc, v)]
                wsum += wt
        if wsum > 0:
            hs, wu, wv, per, cu, cv = (a / wsum for a in acc)
            out["wave_height_m"] = hs
            out["wave_dir_from_deg"] = (math.degrees(math.atan2(-wu, -wv)) + 360) % 360
            out["wave_period_s"] = per or None
            out["current_u"], out["current_v"] = cu, cv
        return out


@dataclass
class Node:
    lat: float
    lon: float
    t: datetime
    parent: "Node | None" = None
    heading: float | None = None
    side: int = 0  # +1 wind from starboard, -1 from port, 0 at start
    sog: float = 0.0
    tws: float | None = None
    twa: float | None = None
    maneuvers: int = 0
    dist_start: float = 0.0
    extra: dict = field(default_factory=dict)


def _move(lat, lon, ve_kn, vn_kn, hours):
    dlat = vn_kn * hours / 60.0
    dlon = ve_kn * hours / (60.0 * max(0.05, math.cos(math.radians(lat))))
    return lat + dlat, lon + dlon


def route(grid: WeatherGrid, land, start, t0: datetime, dest, table, factor=1.0, step_h=1.0,
          heading_step=5, sector_deg=2.0, maneuver_min=3.0, max_hours=240, use_waves=True,
          use_current=True, arrive_nm=1.0, harbour_nm=2.0) -> dict:
    """Isochrone routing. Returns dict with ETA, track and summary, or status 'no_route'."""
    s_lat, s_lon = start
    brg_sd = initial_bearing(s_lat, s_lon, *dest)
    d_total = haversine_nm(s_lat, s_lon, *dest)
    root = Node(s_lat, s_lon, t0)
    frontier = [root]
    best = None  # (arrival time, node, final leg info)
    steps = int(max_hours / step_h)
    # within this distance a direct finish is always evaluated as a candidate (the search front
    # can otherwise step past the marina when the direct heading is slow, e.g. dead downwind)
    finish_nm = max(15.0, 12.0 * step_h)

    def velocity(node, w, heading):
        twa_signed = ((w["wind_dir_from_deg"] - heading + 540) % 360) - 180
        twa = abs(twa_signed)
        stw = boat_speed(w["wind_speed_kn"], twa, table, factor)
        if stw <= 0.2:
            return None
        if use_waves:
            stw *= wave_factor(w.get("wave_height_m"), w.get("wave_dir_from_deg"), heading,
                               w.get("wave_period_s"))[0]
        ve, vn = stw * math.sin(math.radians(heading)), stw * math.cos(math.radians(heading))
        if use_current and "current_u" in w:
            ve += w["current_u"]
            vn += w["current_v"]
        side = 1 if twa_signed > 0 else -1
        return ve, vn, side, twa

    for _ in range(steps):
        new = {}
        for node in frontier:
            w = grid.sample(node.lat, node.lon, node.t)
            if w is None:
                continue
            # can we reach the destination directly within this step?
            d = haversine_nm(node.lat, node.lon, *dest)
            if d <= arrive_nm:
                cand = (node.t, node, None)
                best = cand if best is None or cand[0] < best[0] else best
                continue
            h_dest = initial_bearing(node.lat, node.lon, *dest)
            vel = velocity(node, w, h_dest)
            if vel:
                ve, vn, side, twa = vel
                sog_along = ve * math.sin(math.radians(h_dest)) + vn * math.cos(math.radians(h_dest))
                pen = maneuver_min / 60 if node.side and side != node.side else 0.0
                if sog_along > 0.5 and d <= finish_nm and \
                        land.segment_clear(node.lat, node.lon, *dest, skip_near=[dest, start], skip_nm=harbour_nm):
                    arr = node.t + timedelta(hours=d / sog_along + pen)
                    if best is None or arr < best[0]:
                        best = (arr, node, {"heading": h_dest, "sog": sog_along, "side": side, "twa": twa})
            for k in range(0, 360, heading_step):
                vel = velocity(node, w, k)
                if not vel:
                    continue
                ve, vn, side, twa = vel
                hours = step_h
                man = 0
                if node.side and side != node.side:
                    hours -= maneuver_min / 60
                    man = 1
                lat, lon = _move(node.lat, node.lon, ve, vn, hours)
                if not land.segment_clear(node.lat, node.lon, lat, lon, skip_near=[dest, start], skip_nm=harbour_nm):
                    continue
                ds = haversine_nm(s_lat, s_lon, lat, lon)
                b = initial_bearing(s_lat, s_lon, lat, lon) if ds > 0.01 else brg_sd
                off = ((b - brg_sd + 540) % 360) - 180
                if abs(off) > 110:
                    continue
                key = int((off + 180) // sector_deg)
                # prune: furthest from start per sector, tie-broken by closeness to destination
                score = ds - 0.3 * haversine_nm(lat, lon, *dest)
                cur = new.get(key)
                if cur is None or score > cur.extra["score"]:
                    new[key] = Node(lat, lon, node.t + timedelta(hours=step_h), node, k, side,
                                    math.hypot(ve, vn), w["wind_speed_kn"], twa, node.maneuvers + man, ds,
                                    {"score": score, "wind_from": round(w["wind_dir_from_deg"]),
                                     "wave_m": round(w["wave_height_m"], 1) if w.get("wave_height_m") is not None else None})
        if best is not None and (not new or best[0] <= min(n.t for n in new.values())):
            break
        frontier = list(new.values())
        if not frontier:
            break
    if best is None:
        return {"status": "no_route", "direct_nm": round(d_total, 1)}

    arr, node, fin = best
    track = []
    n = node
    while n is not None:
        track.append(n)
        n = n.parent
    track.reverse()
    pts = [{"time_utc": iso(x.t), "lat": round(x.lat, 4), "lon": round(x.lon, 4),
            "heading": x.heading, "sog_kn": round(x.sog, 2) if x.sog else None,
            "tws_kn": round(x.tws, 1) if x.tws is not None else None,
            "twa_deg": round(x.twa) if x.twa is not None else None, "side": x.side or None,
            "wind_from_deg": x.extra.get("wind_from"), "wave_m": x.extra.get("wave_m")} for x in track]
    maneuvers = node.maneuvers
    if fin:
        if node.side and fin["side"] != node.side:
            maneuvers += 1
        pts.append({"time_utc": iso(arr), "lat": dest[0], "lon": dest[1], "heading": round(fin["heading"]),
                    "sog_kn": round(fin["sog"], 2), "tws_kn": None, "twa_deg": round(fin["twa"])})
    sailed = sum(haversine_nm(a["lat"], a["lon"], b["lat"], b["lon"]) for a, b in zip(pts, pts[1:]))
    hours = (arr - t0).total_seconds() / 3600
    fc_end = grid.t0 + timedelta(hours=getattr(grid, "data_end_h", hours))
    beyond = max(0.0, (arr - fc_end).total_seconds() / 3600)
    return {"status": "ok", "forecast_ends_utc": iso(fc_end), "hours_beyond_forecast": round(beyond, 1), "departure_utc": iso(t0), "eta_utc": iso(arr), "hours": round(hours, 2),
            "direct_nm": round(d_total, 1), "sailed_nm": round(sailed, 1),
            "avg_sog_kn": round(sailed / hours, 2) if hours else None,
            "maneuvers": maneuvers, "track": pts}


def grid_bbox(start, dest, res=0.5, margin=None):
    span = max(abs(start[0] - dest[0]), abs(start[1] - dest[1]))
    margin = margin if margin is not None else max(1.5, 0.2 * span)
    lat0 = max(-80.0, math.floor((min(start[0], dest[0]) - margin) / res) * res)
    lat1 = min(80.0, math.ceil((max(start[0], dest[0]) + margin) / res) * res)
    lon0 = math.floor((min(start[1], dest[1]) - margin) / res) * res
    lon1 = math.ceil((max(start[1], dest[1]) + margin) / res) * res
    return lat0, lon0, lat1, lon1


def auto_settings(start, dest, res=None, step_h=None):
    """Grid spacing and time step scaled to the passage: ~0.5 deg / 1 h for a few hundred
    miles, up to ~1.5 deg / 3-4 h for an ocean crossing (keeps forecast calls and run time
    reasonable; ocean wind fields are smooth at that scale)."""
    d = haversine_nm(*start, *dest)
    span = max(abs(start[0] - dest[0]), abs(start[1] - dest[1]))
    if res is None:
        res = 0.5 if span <= 8 else (1.0 if span <= 20 else 1.5)
    if step_h is None:
        step_h = 1.0 if d <= 500 else (2.0 if d <= 1200 else (3.0 if d <= 2500 else 4.0))
    return res, step_h


def route_yacht(conn, cfg, client, y, start, t0, dest, wind_provider, hours_guess=None, res=None,
                step_h=None, max_days=21, **kw):
    """Build the forecast grid and land mask around start/destination and route.

    Beyond the end of the forecast (about 10-16 days depending on the model) the last
    forecast hour is held; `hours_beyond_forecast` says how much of the ETA rests on that."""
    from .land import mask_for_bbox

    d = haversine_nm(*start, *dest)
    res, step_h = auto_settings(start, dest, res, step_h)
    horizon = hours_guess or min(max_days * 24.0, max(24.0, d / 3.0))
    bbox = grid_bbox(start, dest, res=res)
    grid = WeatherGrid(conn, client, bbox, t0, t0 + timedelta(hours=horizon + 6), wind_provider, res=res).load()
    grid.hold_beyond_h = max_days * 24.0
    land = mask_for_bbox(tuple(round(x, 3) for x in bbox))
    r = route(grid, land, start, t0, dest, y.speed_table or "cruiser27", y.polar_factor,
              step_h=step_h, max_hours=horizon + 6, **kw)
    r["grid"] = {"cells": len(grid.lats) * len(grid.lons), "fetches": grid.fetches, "missing": grid.missing_cells,
                 "res_deg": res, "step_h": step_h, "wind_provider": wind_provider}
    return r


def _now():
    return datetime.now(timezone.utc)
