"""Passage odds from past weather: route a yacht through the same calendar dates in each of
many past years (ERA5 reanalysis via Open-Meteo's archive) and count how often she arrives
in time. The standard way to plan beyond the 10-16 day forecast range.

Runs on the server only. Wind is fetched once per grid cell and year (3-hourly kept) and
stored permanently, so a run interrupted by the API's daily limit resumes where it stopped.

Ocean current: a mean field for the same calendar window over 2022-2025 (Open-Meteo's marine
archive starts in 2022), applied to every year. Sea state: derived from each hour's wind
(fully developed sea), so following seas give the router's surfing gain (up to +3 %). Not
modelled: squalls, crew fatigue and breakages (the caution belongs in reading the result,
e.g. the 75 % polar run, not inside the model).
"""

from __future__ import annotations

import json
import logging
import math
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .geo import haversine_nm, iso, parse_iso, utcnow
from .logutil import log
from .router import WeatherGrid, route
from .weather import Budget, FetchError, OpenMeteo, QuotaExceeded

logger = logging.getLogger("climate")

SQL = """
CREATE TABLE IF NOT EXISTS climate_wind (
    year INTEGER NOT NULL, cell_lat REAL NOT NULL, cell_lon REAL NOT NULL, time_utc TEXT NOT NULL,
    wind_speed_kn REAL, wind_dir_from_deg REAL,
    PRIMARY KEY (year, cell_lat, cell_lon, time_utc)
);
CREATE TABLE IF NOT EXISTS climate_current (
    year INTEGER NOT NULL, cell_lat REAL NOT NULL, cell_lon REAL NOT NULL, start_md TEXT NOT NULL,
    u_sum REAL, v_sum REAL, n INTEGER, PRIMARY KEY (year, cell_lat, cell_lon, start_md)
);
CREATE TABLE IF NOT EXISTS climate_wave (
    year INTEGER NOT NULL, cell_lat REAL NOT NULL, cell_lon REAL NOT NULL, time_utc TEXT NOT NULL,
    hs REAL, dir_from REAL, period REAL, PRIMARY KEY (year, cell_lat, cell_lon, time_utc)
);
CREATE TABLE IF NOT EXISTS climate_wave_cells (
    year INTEGER NOT NULL, cell_lat REAL NOT NULL, cell_lon REAL NOT NULL, start_date TEXT, end_date TEXT,
    PRIMARY KEY (year, cell_lat, cell_lon, start_date, end_date)
);
CREATE TABLE IF NOT EXISTS climate_cells (
    year INTEGER NOT NULL, cell_lat REAL NOT NULL, cell_lon REAL NOT NULL, start_date TEXT, end_date TEXT,
    fetched_utc TEXT, PRIMARY KEY (year, cell_lat, cell_lon, start_date, end_date)
);
"""


ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"


def oni_table(conn=None) -> dict:
    """NOAA CPC Oceanic Nino Index: {(season, year): anomaly}, e.g. ('OND', 2015): 2.45.
    Cached in the DB for 7 days."""
    import requests

    from . import db

    txt = None
    if conn is not None:
        cached = db.kv_get(conn, "oni_table")
        if cached:
            stamp, body = cached.split("\n", 1)
            if stamp >= iso(utcnow() - timedelta(days=7)):
                txt = body
    if txt is None:
        r = requests.get(ONI_URL, timeout=60, headers={"User-Agent": "yachtspeed/0.2"})
        r.raise_for_status()
        txt = r.text
        if conn is not None:
            db.kv_set(conn, "oni_table", iso(utcnow()) + "\n" + txt)
    out = {}
    for line in txt.splitlines():
        p = line.split()
        if len(p) == 4 and p[1].isdigit():
            try:
                out[(p[0], int(p[1]))] = float(p[3])
            except ValueError:
                pass
    return out


def enso_weights(oni, years, sigma=1.0, floor=0.1):
    """Weight each past year by how close its Oct-Dec ONI is to the latest ONI (this year's
    event): w = max(floor, exp(-(d/sigma)^2 / 2)). Returns (current, {year: (oni, weight)})."""
    import math

    latest = max(((y, s) for (s, y) in oni), key=lambda k: (k[0], ["DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ",
                                                                  "JJA", "JAS", "ASO", "SON", "OND", "NDJ"].index(k[1])))
    cur = oni[(latest[1], latest[0])]
    w = {}
    for yr in years:
        v = oni.get(("OND", yr))
        w[yr] = (v, 1.0 if v is None else max(floor, math.exp(-((v - cur) / sigma) ** 2 / 2)))
    return {"season": f"{latest[1]} {latest[0]}", "oni": cur, "sigma": sigma}, w


def phase(v):
    if v is None:
        return "-"
    return "El Nino" if v >= 0.5 else ("La Nina" if v <= -0.5 else "neutral")


class HybridGrid:
    """The live forecast, handing over gradually to a past year's weather (time-shifted onto this
    year): forecast only before `fade_start`, history only after `fade_end`, and in between a mix
    whose history share rises linearly (wind as vectors, speed as a weighted mean). Forecast skill
    fades with lead time, so the handover should too."""

    def __init__(self, forecast, clim, fade_start: datetime, fade_end: datetime, shift: timedelta):
        self.f, self.c, self.fs, self.fe, self.shift = forecast, clim, fade_start, fade_end, shift
        self.t0 = forecast.t0
        self.data_end_h = 10 ** 6
        self.lats, self.lons = getattr(forecast, "lats", []), getattr(forecast, "lons", [])

    def history_share(self, t):
        if t <= self.fs:
            return 0.0
        if t >= self.fe:
            return 1.0
        return (t - self.fs).total_seconds() / (self.fe - self.fs).total_seconds()

    def sample(self, lat, lon, t):
        a = self.history_share(t)
        f = self.f.sample(lat, lon, t) if a < 1.0 else None
        if f is None:  # beyond this forecast's range, or fully handed over
            return self.c.sample(lat, lon, t - self.shift)
        if a <= 0.0:
            return f
        c = self.c.sample(lat, lon, t - self.shift)
        if c is None:
            return f
        out = dict(f)
        fu = -f["wind_speed_kn"] * math.sin(math.radians(f["wind_dir_from_deg"]))
        fv = -f["wind_speed_kn"] * math.cos(math.radians(f["wind_dir_from_deg"]))
        cu = -c["wind_speed_kn"] * math.sin(math.radians(c["wind_dir_from_deg"]))
        cv = -c["wind_speed_kn"] * math.cos(math.radians(c["wind_dir_from_deg"]))
        u, v = fu * (1 - a) + cu * a, fv * (1 - a) + cv * a
        out["wind_speed_kn"] = f["wind_speed_kn"] * (1 - a) + c["wind_speed_kn"] * a
        out["wind_dir_from_deg"] = (math.degrees(math.atan2(-u, -v)) + 360) % 360
        return out


def fade_window(now, grid, depart, fade_from_days, fade_to_days):
    """Handover times for one forecast: from now+fade_from to now+fade_to, never beyond the end of
    that forecast's data (a short-range model hands over earlier)."""
    end = depart + timedelta(hours=getattr(grid, "data_end_h", 0))
    fe = min(now + timedelta(days=fade_to_days), end)
    fs = min(now + timedelta(days=fade_from_days), fe - timedelta(days=1))
    return fs, fe


def cells_for(bbox, res):
    lat0, lon0, lat1, lon1 = bbox
    lats = [round(lat0 + i * res, 4) for i in range(int(round((lat1 - lat0) / res)) + 1)]
    lons = [round(lon0 + j * res, 4) for j in range(int(round((lon1 - lon0) / res)) + 1)]
    return [(la, lo) for la in lats for lo in lons]


def fetch_year(conn, client, year, cells, start_date, end_date, thin_h=3, progress=None, enough=1.0) -> int:
    """Fetch ERA5 wind for every cell not yet stored; raises QuotaExceeded when the budget is spent.
    With `enough` < 1, a year that already has that share of its cells is left as it is (a few points
    that failed to download are filled by interpolation rather than waiting for the next day's quota)."""
    conn.executescript(SQL)
    done = {(r[0], r[1]) for r in conn.execute(
        "SELECT cell_lat, cell_lon FROM climate_cells WHERE year=? AND start_date=? AND end_date=?",
        (year, start_date, end_date))}
    if len(done) >= enough * len(cells) and enough < 1.0:
        return 0
    n = 0
    for la, lo in cells:
        if (la, lo) in done:
            continue
        try:
            rows = client.fetch("om_era5", "wind", la, lo, start_date, end_date)
        except FetchError as e:
            log(logger, logging.WARNING, "era5_fetch_failed", year=year, lat=la, lon=lo, error=str(e)[:120])
            continue
        conn.execute("BEGIN")
        for r in rows:
            t = parse_iso(r["time_utc"])
            if t.hour % thin_h:
                continue
            conn.execute("INSERT OR REPLACE INTO climate_wind VALUES (?,?,?,?,?,?)",
                         (year, la, lo, iso(t), r["wind_speed_kn"], r["wind_dir_from_deg"]))
        conn.execute("INSERT OR REPLACE INTO climate_cells VALUES (?,?,?,?,?,?)",
                     (year, la, lo, start_date, end_date, iso(utcnow())))
        conn.execute("COMMIT")
        n += 1
        if progress:
            progress(year, n)
    return n


CURRENT_YEARS = (2022, 2023, 2024, 2025)  # Open-Meteo's marine archive has currents from mid-2022


class CurrentField:
    """Mean surface current (knots, east/north components) over the same calendar window in
    several recent years, on a coarse grid; currents on ocean passages are steady and large-scale."""

    def __init__(self, conn, bbox, res, start_md):
        self.lat0, self.lon0 = bbox[0], bbox[1]
        self.res = res
        self.uv = {}
        for r in conn.execute("SELECT cell_lat, cell_lon, sum(u_sum), sum(v_sum), sum(n) FROM climate_current "
                              "WHERE start_md=? GROUP BY cell_lat, cell_lon", (start_md,)):
            if r[4]:
                self.uv[(round(r[0], 4), round(r[1], 4))] = (r[2] / r[4], r[3] / r[4])

    def at(self, lat, lon):
        fi, fj = (lat - self.lat0) / self.res, (lon - self.lon0) / self.res
        i0, j0 = int(fi // 1), int(fj // 1)
        di, dj = fi - i0, fj - j0
        acc_u = acc_v = ws = 0.0
        for (i, j), w in (((i0, j0), (1 - di) * (1 - dj)), ((i0 + 1, j0), di * (1 - dj)),
                          ((i0, j0 + 1), (1 - di) * dj), ((i0 + 1, j0 + 1), di * dj)):
            k = (round(self.lat0 + i * self.res, 4), round(self.lon0 + j * self.res, 4))
            if k in self.uv and w > 0:
                acc_u += w * self.uv[k][0]
                acc_v += w * self.uv[k][1]
                ws += w
        return (acc_u / ws, acc_v / ws) if ws > 0 else (0.0, 0.0)


class WithCurrent:
    """Adds the mean current to whatever wind grid it wraps."""

    def __init__(self, grid, field):
        self.g, self.field = grid, field
        self.t0 = grid.t0
        self.data_end_h = getattr(grid, "data_end_h", 10 ** 6)
        self.lats, self.lons = getattr(grid, "lats", []), getattr(grid, "lons", [])

    def sample(self, lat, lon, t):
        w = self.g.sample(lat, lon, t)
        if w is not None and self.field is not None and self.field.uv:
            w = dict(w)
            w["current_u"], w["current_v"] = self.field.at(lat, lon)
        return w


class WithSea:
    """Adds the sea state: a past year's ERA5 waves where given and available, otherwise one
    derived from the local wind (fully developed sea, Pierson-Moskowitz):
    Hs = 0.0246 U10^2 m and peak period 0.785 U10 s (U10 in m/s), from the wind direction.
    Open-ocean trade-wind seas are mostly wind-made; this lets the router apply its sea-state
    factor (following seas: up to +3 % surfing gain; head and beam seas: losses) without
    downloading wave history."""

    def __init__(self, grid, field=None):
        self.g = grid
        self.field = field  # (WaveField, time shift onto its year, use_from) or None
        self.t0 = grid.t0
        self.data_end_h = getattr(grid, "data_end_h", 10 ** 6)
        self.lats, self.lons = getattr(grid, "lats", []), getattr(grid, "lons", [])

    def sample(self, lat, lon, t):
        w = self.g.sample(lat, lon, t)
        if w is None or w.get("wave_height_m") is not None:
            return w
        if self.field is not None:
            wf, shift, use_from = self.field
            if use_from is None or t >= use_from:
                x = wf.at(lat, lon, t - shift)
                if x is not None:
                    w = dict(w)
                    w["wave_height_m"], w["wave_dir_from_deg"], w["wave_period_s"] = x
                    return w
        u = w["wind_speed_kn"] * 0.5144
        w = dict(w)
        w["wave_height_m"] = min(6.0, 0.0246 * u * u)
        w["wave_dir_from_deg"] = w["wind_dir_from_deg"]
        w["wave_period_s"] = max(3.0, 0.785 * u)
        return w


def sea(grid, waves=True, field=None):
    return WithSea(grid, field) if waves else grid


WAVE_RES = 7.5  # ocean waves are smooth over the open ocean; a coarse grid keeps downloads down


def wave_bbox(bbox, res=WAVE_RES):
    return (math.floor(bbox[0] / res) * res, math.floor(bbox[1] / res) * res,
            math.ceil(bbox[2] / res) * res, math.ceil(bbox[3] / res) * res)


def fetch_waves_year(conn, client, year, cells, start_date, end_date, thin_h=3, enough=1.0):
    """ERA5 ocean waves (height, direction, period) per cell for one year; raises QuotaExceeded."""
    conn.executescript(SQL)
    done = {(round(r[0], 4), round(r[1], 4)) for r in conn.execute(
        "SELECT cell_lat, cell_lon FROM climate_wave_cells WHERE year=? AND start_date=? AND end_date=?",
        (year, start_date, end_date))}
    if len(done) >= enough * len(cells) and enough < 1.0:
        return
    for la, lo in cells:
        if (la, lo) in done:
            continue
        try:
            rows = client.fetch("om_era5_ocean", "marine", la, lo, start_date, end_date)
        except FetchError as e:
            log(logger, logging.WARNING, "era5_wave_fetch_failed", year=year, lat=la, lon=lo, error=str(e)[:120])
            rows = []
        conn.execute("BEGIN")
        for r in rows:
            t = parse_iso(r["time_utc"])
            if t.hour % thin_h or r.get("wave_height_m") is None:
                continue
            conn.execute("INSERT OR REPLACE INTO climate_wave VALUES (?,?,?,?,?,?,?)",
                         (year, la, lo, iso(t), r["wave_height_m"], r.get("wave_dir_from_deg"), r.get("wave_period_s")))
        conn.execute("INSERT OR REPLACE INTO climate_wave_cells VALUES (?,?,?,?,?)", (year, la, lo, start_date, end_date))
        conn.execute("COMMIT")


def waves_complete(conn, year, cells, start_date, end_date):
    conn.executescript(SQL)
    n = conn.execute("SELECT count(*) FROM climate_wave_cells WHERE year=? AND start_date=? AND end_date=?",
                     (year, start_date, end_date)).fetchone()[0]
    return n >= 0.9 * len(cells)


class WaveField:
    """One past year's ERA5 waves on the coarse grid, sampled at a time on that year's calendar
    (bilinear in space, nearest 3-hourly step in time; directions as vectors)."""

    def __init__(self, conn, year, bbox, res=WAVE_RES):
        self.lat0, self.lon0, self.res = bbox[0], bbox[1], res
        self.data = {}
        for r in conn.execute("SELECT cell_lat, cell_lon, time_utc, hs, dir_from, period FROM climate_wave "
                              "WHERE year=?", (year,)):
            if r[3] is None:
                continue
            self.data[(round(r[0], 4), round(r[1], 4), r[2])] = (r[3], r[4] or 0.0, r[5])

    def at(self, lat, lon, t):
        if not self.data:
            return None
        key_t = iso(t.replace(hour=(t.hour // 3) * 3, minute=0, second=0, microsecond=0))
        fi, fj = (lat - self.lat0) / self.res, (lon - self.lon0) / self.res
        i0, j0 = int(fi // 1), int(fj // 1)
        di, dj = fi - i0, fj - j0
        hs = u = v = per = ws = 0.0
        for (i, j), w in (((i0, j0), (1 - di) * (1 - dj)), ((i0 + 1, j0), di * (1 - dj)),
                          ((i0, j0 + 1), (1 - di) * dj), ((i0 + 1, j0 + 1), di * dj)):
            k = (round(self.lat0 + i * self.res, 4), round(self.lon0 + j * self.res, 4), key_t)
            x = self.data.get(k)
            if x is None or w <= 0:
                continue
            hs += w * x[0]
            u += w * math.sin(math.radians(x[1]))
            v += w * math.cos(math.radians(x[1]))
            per += w * (x[2] or 0.0)
            ws += w
        if ws <= 0:
            return None
        return hs / ws, (math.degrees(math.atan2(u, v)) + 360) % 360, (per / ws) or None


def fetch_currents(conn, client, bbox, res, depart, max_days, years=CURRENT_YEARS):
    """Mean current per cell and year for the passage's calendar window (raises QuotaExceeded)."""
    import math

    conn.executescript(SQL)
    md = depart.strftime("%m-%d")
    done = {(r[0], round(r[1], 4), round(r[2], 4)) for r in conn.execute(
        "SELECT year, cell_lat, cell_lon FROM climate_current WHERE start_md=?", (md,))}
    for yr in years:
        d0 = depart.replace(year=yr)
        s_date, e_date = d0.strftime("%Y-%m-%d"), (d0 + timedelta(days=max_days)).strftime("%Y-%m-%d")
        for la, lo in cells_for(bbox, res):
            if (yr, la, lo) in done:
                continue
            try:
                rows = client.fetch("om_marine", "marine", la, lo, s_date, e_date)
            except FetchError:
                rows = []
            u = v = 0.0
            n = 0
            for r in rows:
                spd, d = r.get("current_speed_kn"), r.get("current_dir_to_deg")
                if spd is None or d is None:
                    continue
                u += spd * math.sin(math.radians(d))
                v += spd * math.cos(math.radians(d))
                n += 1
            conn.execute("INSERT OR REPLACE INTO climate_current VALUES (?,?,?,?,?,?,?)", (yr, la, lo, md, u, v, n))


class ClimateGrid(WeatherGrid):
    """A WeatherGrid read from the stored ERA5 wind of one past year (no fetching, no sea state)."""

    def __init__(self, conn, year, bbox, t_start, t_end, res):
        super().__init__(conn, None, bbox, t_start, t_end, "om_era5", res=res, use_marine=False)
        self.year = year
        self.hold_beyond_h = 6.0

    def _rows(self, provider, kind, lat, lon):
        return self.conn.execute(
            "SELECT time_utc, wind_speed_kn, wind_dir_from_deg FROM climate_wind WHERE year=? AND cell_lat=? "
            "AND cell_lon=? AND time_utc >= ? AND time_utc <= ? ORDER BY time_utc",
            (self.year, lat, lon, iso(self.t0 - timedelta(hours=3)), iso(self.t_end + timedelta(hours=3)))).fetchall()


def _thin(track, every_h=6.0):
    out, last = [], None
    for p in track:
        t = parse_iso(p["time_utc"])
        if last is None or (t - last).total_seconds() >= every_h * 3600 - 60 or p is track[-1]:
            out.append([p["time_utc"], p["lat"], p["lon"], bool(p.get("motor"))])
            last = t
    return out


def fetch_only(conn, start, dest, depart: datetime, years, res=2.5, margin=4.0, max_days=28, waves=True,
               per_day=3000, per_hour=1500, per_minute=150, progress=print, wind=True):
    """Download history (wind, and ERA5 waves) for `years` into `conn`, exactly as `run` would, so it
    can be fetched from another machine (its own API allowance) and merged on the server."""
    from .router import grid_bbox

    conn.executescript(SQL)
    bbox = grid_bbox(start, dest, res=res, margin=margin)
    cells = cells_for(bbox, res)
    wcells = cells_for(wave_bbox(bbox), WAVE_RES)
    client = OpenMeteo(Budget(conn, per_minute, per_hour, per_day))
    for yr in years:
        d0 = depart.replace(year=yr)
        t_end = d0 + timedelta(days=max_days)
        s_date, e_date = d0.strftime("%Y-%m-%d"), (t_end + timedelta(days=1)).strftime("%Y-%m-%d")
        for what, fn, n in (("wind", lambda: fetch_year(conn, client, yr, cells, s_date, e_date), len(cells)),
                            ("waves", lambda: fetch_waves_year(conn, client, yr, wcells, s_date, e_date), len(wcells))):
            if (what == "waves" and not waves) or (what == "wind" and not wind):
                continue
            while True:
                try:
                    fn()
                    progress(f"{yr} {what}: done ({n} grid points)")
                    break
                except QuotaExceeded as e:
                    pause = (e.resume_at - utcnow()).total_seconds()
                    progress(f"{yr} {what}: API limit, waiting until {iso(e.resume_at)}")
                    if pause > 26 * 3600:
                        return
                    time.sleep(max(5.0, pause + 5))


MERGE_TABLES = ("climate_wind", "climate_cells", "climate_wave", "climate_wave_cells", "hist_wx", "hist_wx_done")


def merge(conn, path) -> dict:
    """Add another machine's downloaded history (a climate-fetch database) to this one."""
    from .history import SQL as HSQL

    conn.executescript(SQL)
    conn.executescript(HSQL)
    conn.execute("ATTACH DATABASE ? AS other", (path,))
    out = {}
    try:
        for t in MERGE_TABLES:
            have = conn.execute("SELECT name FROM other.sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
            if not have:
                continue
            before = conn.execute(f"SELECT count(*) FROM main.{t}").fetchone()[0]
            conn.execute(f"INSERT OR IGNORE INTO main.{t} SELECT * FROM other.{t}")
            out[t] = conn.execute(f"SELECT count(*) FROM main.{t}").fetchone()[0] - before
    finally:
        conn.execute("DETACH DATABASE other")
    return out


def run(conn, cfg, y, start, dest, depart: datetime, deadline: datetime, years, res=2.5, margin=4.0,
        max_days=28, step_h=4.0, heading_step=5, motor_wind_below_kn=None, motor_speed_kn=7.0, motor_max_h=None,
        maneuver_min=3.0, per_day=2500, per_hour=1200, per_minute=120, wait=True, out_path=None,
        name="climate", forecast_models=None, fade=(2.0, 15.0), enso_sigma=1.0, currents=True,
        current_res=5.0, fuel_note=None, per_model=True, waves=True, real_waves=True,
        forecast_only=False) -> dict:
    """Route `y` from `start` to `dest` leaving on `depart`'s month/day/time in each of `years`.
    `deadline` is in the same year as `depart`; the same interval is applied to every year."""
    from .land import mask_for_bbox
    from .router import grid_bbox

    conn.executescript(SQL)
    bbox = grid_bbox(start, dest, res=res, margin=margin)
    cells = cells_for(bbox, res)
    allow_h = (deadline - depart).total_seconds() / 3600
    client = OpenMeteo(Budget(conn, per_minute, per_hour, per_day))
    land = mask_for_bbox(tuple(round(x, 3) for x in bbox))
    out = {"name": name, "yacht": y.name, "from": list(start), "to": list(dest), "depart": iso(depart),
           "deadline": iso(deadline), "allowed_days": round(allow_h / 24, 2), "years": list(years),
           "grid": {"bbox": bbox, "res_deg": res, "cells": len(cells), "step_h": step_h},
           "settings": {"table": y.speed_table, "polar_factor": y.polar_factor, "gybe_min": maneuver_min,
                        "motor_wind_below_kn": motor_wind_below_kn, "motor_speed_kn": motor_speed_kn,
                        "motor_max_h": motor_max_h, "fuel": fuel_note,
                        "current": f"mean of {CURRENT_YEARS[0]}-{CURRENT_YEARS[-1]} for these dates" if currents
                        else "not modelled",
                        "waves": ("ERA5 ocean waves (wind-derived until downloaded)" if real_waves else
                                  "from the wind (fully developed sea)") + "; following seas up to +3 %" if waves
                        else "not modelled"},
           "results": [], "status": "running", "updated_utc": iso(utcnow())}
    if out_path and Path(out_path).exists():  # resume: keep finished years
        try:
            prev = json.loads(Path(out_path).read_text())
            if prev.get("depart") == out["depart"] and prev.get("settings") == out["settings"]:
                out["results"] = [r for r in prev.get("results", []) if r.get("status") in ("ok", "no_route")]
        except (OSError, ValueError):
            pass
    done_years = {r["year"] for r in out["results"]}
    try:
        cur, wts = enso_weights(oni_table(conn), years, sigma=enso_sigma)
        out["enso"] = {"now": cur, "years": {str(k): {"oni": v[0], "phase": phase(v[0]), "weight": round(v[1], 3)}
                                             for k, v in wts.items()}}
    except Exception as e:  # index unavailable: unweighted only
        out["enso"] = {"error": f"{type(e).__name__}: {e}"[:120]}
        wts = {}
    weights = {k: v[1] for k, v in wts.items()}
    field = None
    if currents:
        cbbox = (math.floor(bbox[0] / current_res) * current_res, math.floor(bbox[1] / current_res) * current_res,
                 math.ceil(bbox[2] / current_res) * current_res, math.ceil(bbox[3] / current_res) * current_res)
        while True:
            try:
                fetch_currents(conn, client, cbbox, current_res, depart, max_days)
                break
            except QuotaExceeded as e:
                pause = (e.resume_at - utcnow()).total_seconds()
                out["status"] = f"waiting for weather quota until {iso(e.resume_at)} (currents)"
                if out_path:
                    Path(out_path).write_text(json.dumps(out, indent=1))
                if not wait or pause > 26 * 3600:
                    return out
                time.sleep(max(5.0, pause + 5))
        field = CurrentField(conn, cbbox, current_res, depart.strftime("%m-%d"))
        out["current_cells"] = len(field.uv)

    def save():
        out["updated_utc"] = iso(utcnow())
        out["summary"] = summarise(out["results"], allow_h, weights)
        if out.get("forecast"):
            out["forecast"]["summary"] = summarise(out["forecast"]["results"], allow_h, weights)
        if out_path:  # results so far, so progress is visible in the published report
            Path(out_path).write_text(json.dumps(out, indent=1))
            base = str(out_path)[:-5] if str(out_path).endswith(".json") else str(out_path)
            Path(base + ".md").write_text(markdown(out))
            try:
                tracks_jpeg(base + ".jpg", out)
            except Exception as e:  # a chart failure must not stop the run
                log(logger, logging.WARNING, "climate_chart_failed", error=str(e)[:120])

    wcells = cells_for(wave_bbox(bbox), WAVE_RES)

    def window(yr):
        d0 = depart.replace(year=yr)
        t_end = d0 + timedelta(days=max_days)
        return d0, t_end, d0.strftime("%Y-%m-%d"), (t_end + timedelta(days=1)).strftime("%Y-%m-%d")

    def with_quota(fn, label):
        """Run fn(), waiting out the API budget; returns False if we must stop (no-wait / long pause)."""
        while True:
            try:
                fn()
                return True
            except QuotaExceeded as e:
                pause = (e.resume_at - utcnow()).total_seconds()
                out["status"] = f"waiting for weather quota until {iso(e.resume_at)} ({label})"
                save()
                if not wait or pause > 26 * 3600:
                    return False
                log(logger, logging.INFO, "climate_quota_wait", until=iso(e.resume_at), what=label)
                time.sleep(max(5.0, pause + 5))

    def route_year(yr):
        d0, t_end, s_date, e_date = window(yr)
        grid = ClimateGrid(conn, yr, bbox, d0, t_end, res).load()
        if len(grid.wind) < 0.9 * len(cells):
            return {"year": yr, "status": "missing_weather", "cells": len(grid.wind)}
        real = real_waves and waves and waves_complete(conn, yr, wcells, s_date, e_date)
        wf = (WaveField(conn, yr, wave_bbox(bbox)), timedelta(0), None) if real else None
        t_run = time.time()
        r = route(sea(WithCurrent(grid, field), waves, wf), land, start, d0, dest, y.speed_table or "cruiser27",
                  y.polar_factor, step_h=step_h, heading_step=heading_step, maneuver_min=maneuver_min,
                  max_hours=max_days * 24, use_waves=waves, use_current=True, motor_wind_below_kn=motor_wind_below_kn,
                  motor_speed_kn=motor_speed_kn, motor_max_h=motor_max_h)
        if r.get("status") != "ok":
            res_y = {"year": yr, "status": "no_route", "on_time": False, "diagnostics": r.get("diagnostics")}
        else:
            hrs = r["hours"]
            winds = [p["tws_kn"] for p in r["track"] if p.get("tws_kn") is not None]
            res_y = {"year": yr, "status": "ok", "eta": r["eta_utc"], "days": round(hrs / 24, 2),
                     "on_time": hrs <= allow_h, "margin_h": round(allow_h - hrs, 1),
                     "sailed_nm": r["sailed_nm"], "avg_kn": r["avg_sog_kn"], "gybes": r["maneuvers"],
                     "motor_hours": r.get("motor_hours", 0.0),
                     "wind_median_kn": round(statistics.median(winds), 1) if winds else None,
                     "southmost_lat": round(min(p["lat"] for p in r["track"]), 1),
                     "track": _thin(r["track"])}
        res_y["waves"] = "era5" if real else ("wind" if waves else "none")
        res_y["route_seconds"] = round(time.time() - t_run)
        return res_y

    def put(res_y):
        out["results"] = [r for r in out["results"] if r["year"] != res_y["year"]] + [res_y]
        log(logger, logging.INFO, "climate_year", year=res_y["year"], days=res_y.get("days"),
            on_time=res_y.get("on_time"), waves=res_y.get("waves"))
        save()

    if forecast_only:  # only the years whose history is already stored; no history downloads
        stored = {r[0]: r[1] for r in conn.execute(
            "SELECT year, count(*) FROM climate_cells GROUP BY year")}
        years = [yr for yr in years if stored.get(yr, 0) >= 0.9 * len(cells)]
        out["years"] = years
        out["status"] = f"forecast only, on the {len(years)} years of history stored so far"
    # pass 1: every year's wind and a first route (real waves where already stored, else from the wind)
    for yr in ([] if forecast_only else years):
        if yr in done_years:
            continue
        d0, t_end, s_date, e_date = window(yr)
        if not with_quota(lambda: fetch_year(conn, client, yr, cells, s_date, e_date, enough=0.95), f"wind {yr}"):
            return out
        put(route_year(yr))
    # pass 2: real ERA5 ocean waves, then re-route the years that used the wind-derived sea
    if real_waves and waves and not forecast_only:
        for yr in years:
            prev = next((r for r in out["results"] if r["year"] == yr), None)
            if prev and prev.get("waves") == "era5":
                continue
            d0, t_end, s_date, e_date = window(yr)
            if not with_quota(lambda: fetch_waves_year(conn, client, yr, wcells, s_date, e_date, enough=0.9),
                              f"waves {yr}"):
                return out
            put(route_year(yr))
    if forecast_models:
        out["status"] = "running the live forecast"
        save()
        out["forecast"] = run_forecast(conn, cfg, y, start, dest, depart, bbox, res, land, years, allow_h, max_days,
                                       step_h, heading_step, maneuver_min, motor_wind_below_kn, motor_speed_kn,
                                       motor_max_h, forecast_models, fade, save_cb=save, holder=out,
                                       field=field, per_model=per_model, enso_w=weights, waves=waves,
                                       real_waves=real_waves)
    out["status"] = "complete"
    save()
    return out


def run_forecast(conn, cfg, y, start, dest, depart, bbox, res, land, years, allow_h, max_days, step_h, heading_step,
                 maneuver_min, motor_wind_below_kn, motor_speed_kn, motor_max_h, models, fade,
                 save_cb=None, holder=None, field=None, per_model=True, enso_w=None, waves=True,
                 real_waves=True) -> dict:
    """The live forecast (blend of `models`, skill-weighted), handing over between `fade` days after now, to each past
    year's weather for the rest of the passage."""
    from .router import BlendGrid
    from .weather import model_provider

    now = utcnow()
    f_end = min(depart + timedelta(days=max_days), now + timedelta(days=15))
    fc = {"issued_utc": iso(now), "models": list(models), "results": []}
    if f_end <= depart + timedelta(hours=12):
        fc["status"] = "departure is beyond the forecast range"
        return fc
    client = OpenMeteo(Budget(conn, cfg.calls_per_minute, cfg.calls_per_hour, cfg.calls_per_day))
    grids, used = [], []
    for m in models:
        g = WeatherGrid(conn, client, bbox, depart, f_end, model_provider(m), res=res, use_marine=False).load()
        if len(g.wind) >= 0.9 * len(g.lats) * len(g.lons):
            grids.append(g)
            used.append(m)
    if not grids:
        fc["status"] = "forecast unavailable (weather quota or API error)"
        return fc
    weights = None
    if getattr(cfg, "skill_weighting", False):
        from .skill import Weights

        weights = Weights(conn, used, cfg.skill_window_days, cfg.skill_min_checks)
    blend = BlendGrid(grids, used, weights)
    blend.data_end_h = max(g.data_end_h for g in grids)  # a member drops out of the blend when its run ends
    fs, fe = fade_window(now, blend, depart, *fade)
    windows = {m: fade_window(now, g, depart, *fade) for m, g in zip(used, grids)}

    def day(t):
        return round(max(0.0, (t - depart).total_seconds() / 86400), 1)

    fc.update({"models_used": used, "handover_utc": [iso(fs), iso(fe)], "handover_day": [day(fs), day(fe)],
               "model_handover_day": {m: [day(a), day(b)] for m, (a, b) in windows.items()}})
    if holder is not None:
        holder["forecast"] = fc
    variants = [("blend", blend)] + ([(m, g) for m, g in zip(used, grids)] if per_model and len(used) > 1 else [])
    if len(variants) > 1:
        fc["per_model"] = {m: {"results": []} for m, _ in variants[1:]}
    for yr in years:
        d0 = depart.replace(year=yr)
        clim = ClimateGrid(conn, yr, bbox, d0, d0 + timedelta(days=max_days), res).load()
        if len(clim.wind) < 0.9 * len(clim.lats) * len(clim.lons):
            continue
        s_date, e_date = d0.strftime("%Y-%m-%d"), (d0 + timedelta(days=max_days + 1)).strftime("%Y-%m-%d")
        wcells = cells_for(wave_bbox(bbox), WAVE_RES)
        wfield = WaveField(conn, yr, wave_bbox(bbox)) if real_waves and waves_complete(
            conn, yr, wcells, s_date, e_date) else None

        def wf(handover_end):  # the past year's real waves once its weather has taken over
            return (wfield, depart - d0, handover_end) if wfield is not None else None

        for m, g in variants[1:]:  # the same past year, after each model's own forecast
            r = route(sea(WithCurrent(HybridGrid(g, clim, *windows[m], depart - d0), field), waves,
                          wf(windows[m][1])), land, start,
                      depart, dest, y.speed_table or "cruiser27", y.polar_factor, step_h=step_h,
                      heading_step=heading_step, maneuver_min=maneuver_min, max_hours=max_days * 24,
                      use_waves=waves, use_current=True,
                      motor_wind_below_kn=motor_wind_below_kn, motor_speed_kn=motor_speed_kn, motor_max_h=motor_max_h)
            pm = fc["per_model"][m]
            if r.get("status") != "ok":
                pm["results"].append({"year": yr, "status": "no_route", "on_time": False, "diagnostics": r.get("diagnostics")})
            else:
                pm["results"].append({"year": yr, "status": "ok", "days": round(r["hours"] / 24, 2),
                                      "on_time": r["hours"] <= allow_h, "margin_h": round(allow_h - r["hours"], 1),
                                      "sailed_nm": r["sailed_nm"], "motor_hours": r.get("motor_hours", 0.0)})
            pm["summary"] = summarise(pm["results"], allow_h, enso_w)
        hg = sea(WithCurrent(HybridGrid(blend, clim, fs, fe, depart - d0), field), waves, wf(fe))
        r = route(hg, land, start, depart, dest, y.speed_table or "cruiser27", y.polar_factor, step_h=step_h,
                  heading_step=heading_step, maneuver_min=maneuver_min, max_hours=max_days * 24, use_waves=waves,
                  use_current=True, motor_wind_below_kn=motor_wind_below_kn, motor_speed_kn=motor_speed_kn,
                  motor_max_h=motor_max_h)
        if r.get("status") != "ok":
            fc["results"].append({"year": yr, "status": "no_route", "on_time": False, "diagnostics": r.get("diagnostics")})
        else:
            fc["results"].append({"year": yr, "status": "ok", "eta": r["eta_utc"], "days": round(r["hours"] / 24, 2),
                                  "on_time": r["hours"] <= allow_h, "margin_h": round(allow_h - r["hours"], 1),
                                  "sailed_nm": r["sailed_nm"], "avg_kn": r["avg_sog_kn"],
                                  "motor_hours": r.get("motor_hours", 0.0),
                                  "southmost_lat": round(min(p["lat"] for p in r["track"]), 1),
                                  "track": _thin(r["track"])})
        if save_cb:
            save_cb()
    fc["status"] = "complete"
    return fc


DEFAULT_STRATEGIES = {
    "A": {"label": "fastest (router's choice)"},
    "B": {"label": "classic: south-west to 22N 30W first", "via": (22.0, -30.0)},
    "C": {"label": "fuel-saving: at most 30 h motoring in the first 72 h", "motor_early": (72.0, 30.0)},
    "D": {"label": "more sailing: motor only below 7 kn of wind", "motor_below": 7.0},
    "E": {"label": "mostly sailing: at most 48 h motoring in all", "motor_max_h": 48.0},
}


def run_strategies(conn, cfg, y, start, dest, depart, deadline, years, res=2.5, margin=4.0, max_days=28, step_h=4.0,
                   heading_step=5, maneuver_min=3.0, motor_wind_below_kn=None, motor_speed_kn=7.0, motor_max_h=None,
                   models=("ecmwf", "aifs", "gfs", "icon"), fade=(2.0, 15.0), strategies=None, burn_lph=None,
                   waves=True, out_path=None, enso_sigma=1.0) -> dict:
    """Compare routing strategies through the live forecast (skill-weighted blend, handing over to each past
    year's weather), using the history already stored. A waypoint strategy routes start -> waypoint -> destination,
    carrying the remaining fuel into the second leg."""
    from .land import mask_for_bbox
    from .router import BlendGrid, grid_bbox
    from .weather import model_provider

    strategies = strategies or DEFAULT_STRATEGIES
    conn.executescript(SQL)
    bbox = grid_bbox(start, dest, res=res, margin=margin)
    cells = cells_for(bbox, res)
    land = mask_for_bbox(tuple(round(x, 3) for x in bbox))
    allow_h = (deadline - depart).total_seconds() / 3600
    now = utcnow()
    out = {"name": "strategies", "yacht": y.name, "from": list(start), "to": list(dest), "depart": iso(depart),
           "deadline": iso(deadline), "allowed_days": round(allow_h / 24, 2), "issued_utc": iso(now),
           "settings": {"table": y.speed_table, "polar_factor": y.polar_factor, "motor_wind_below_kn": motor_wind_below_kn,
                        "motor_speed_kn": motor_speed_kn, "motor_max_h": motor_max_h, "burn_lph": burn_lph},
           "strategies": {k: {"label": v["label"], "via": v.get("via"), "motor_early": v.get("motor_early"),
                              "motor_below": v.get("motor_below"), "motor_max_h": v.get("motor_max_h"),
                              "results": [], "history": []} for k, v in strategies.items()},
           "status": "running"}
    try:
        cur, wts = enso_weights(oni_table(conn), years, sigma=enso_sigma)
        weights = {k: v[1] for k, v in wts.items()}
        out["enso_now"] = cur
    except Exception:
        weights = {}

    def save():
        for v in out["strategies"].values():
            for key, src in (("summary", "results"), ("history_summary", "history")):
                v[key] = summarise(v[src], allow_h, weights)
                sail = [r["sailing_pct"] for r in v[src] if r.get("sailing_pct") is not None]
                if sail:
                    v[key]["sailing_pct_median"] = round(statistics.median(sail))
                if burn_lph and v[key].get("motor_hours_median") is not None:
                    v[key]["fuel_l_median"] = round(v[key]["motor_hours_median"] * burn_lph)
        if out_path:
            Path(out_path).write_text(json.dumps(out, indent=1))
            base = str(out_path)[:-5]
            Path(base + ".md").write_text(strategies_markdown(out))
            try:
                strategies_jpeg(base + ".jpg", out)
            except Exception as e:
                log(logger, logging.WARNING, "strategies_chart_failed", error=str(e)[:120])

    f_end = min(depart + timedelta(days=max_days), now + timedelta(days=15))
    client = OpenMeteo(Budget(conn, cfg.calls_per_minute, cfg.calls_per_hour, cfg.calls_per_day))
    grids, used = [], []
    for m in models:
        g = WeatherGrid(conn, client, bbox, depart, f_end, model_provider(m), res=res, use_marine=False).load()
        if len(g.wind) >= 0.9 * len(g.lats) * len(g.lons):
            grids.append(g)
            used.append(m)
    if not grids:
        out["status"] = "forecast unavailable (weather quota or API error)"
        save()
        return out
    wobj = None
    if getattr(cfg, "skill_weighting", False):
        from .skill import Weights

        wobj = Weights(conn, used, cfg.skill_window_days, cfg.skill_min_checks)
    blend = BlendGrid(grids, used, wobj)
    blend.data_end_h = max(g.data_end_h for g in grids)
    fs, fe = fade_window(now, blend, depart, *fade)
    out["models_used"] = used
    if wobj is not None:
        out["model_weights"] = {f"{n} d ahead": {m: round(x * 100) for m, x in w.items()} for n, w in wobj.by_day.items()}
        out["weights_checks"] = wobj.checks
    out["handover_day"] = [round((fs - depart).total_seconds() / 86400, 1), round((fe - depart).total_seconds() / 86400, 1)]
    md = depart.strftime("%m-%d")
    cfield = CurrentField(conn, wave_bbox(bbox, 5.0), 5.0, md)
    stored = {r[0]: r[1] for r in conn.execute("SELECT year, count(*) FROM climate_cells GROUP BY year")}
    for yr in years:
        if stored.get(yr, 0) < 0.9 * len(cells):
            continue
        d0 = depart.replace(year=yr)
        clim = ClimateGrid(conn, yr, bbox, d0, d0 + timedelta(days=max_days), res).load()
        s_date, e_date = d0.strftime("%Y-%m-%d"), (d0 + timedelta(days=max_days + 1)).strftime("%Y-%m-%d")
        wcells = cells_for(wave_bbox(bbox), WAVE_RES)
        wfield = WaveField(conn, yr, wave_bbox(bbox)) if waves_complete(conn, yr, wcells, s_date, e_date) else None
        fgrid = sea(WithCurrent(HybridGrid(blend, clim, fs, fe, depart - d0), cfield), waves,
                    (wfield, depart - d0, fe) if wfield is not None else None)
        hgrid = sea(WithCurrent(clim, cfield), waves, (wfield, timedelta(0), None) if wfield is not None else None)
        kw = dict(step_h=step_h, heading_step=heading_step, maneuver_min=maneuver_min, use_waves=waves,
                  use_current=True, motor_wind_below_kn=motor_wind_below_kn, motor_speed_kn=motor_speed_kn)
        for key, st, (grid, t0, bucket) in [(k, v, b) for k, v in strategies.items()
                                             for b in ((fgrid, depart, "results"), (hgrid, d0, "history"))]:
            kw["motor_wind_below_kn"] = st.get("motor_below", motor_wind_below_kn)
            cap = motor_max_h if st.get("motor_max_h") is None else (
                st["motor_max_h"] if motor_max_h is None else min(motor_max_h, st["motor_max_h"]))
            if st.get("via"):
                r1 = route(grid, land, start, t0, st["via"], y.speed_table or "cruiser27", y.polar_factor,
                           max_hours=max_days * 24, motor_max_h=cap, **kw)
                if r1.get("status") != "ok":
                    r = r1
                else:
                    left = None if cap is None else max(0.0, cap - r1.get("motor_hours", 0.0))
                    t1 = parse_iso(r1["eta_utc"])
                    r2 = route(grid, land, tuple(st["via"]), t1, dest, y.speed_table or "cruiser27", y.polar_factor,
                               max_hours=max_days * 24 - r1["hours"], motor_max_h=left, **kw)
                    r = r2 if r2.get("status") != "ok" else {
                        "status": "ok", "eta_utc": r2["eta_utc"], "hours": r1["hours"] + r2["hours"],
                        "sailed_nm": round(r1["sailed_nm"] + r2["sailed_nm"], 1),
                        "motor_hours": r1.get("motor_hours", 0.0) + r2.get("motor_hours", 0.0),
                        "track": r1["track"] + r2["track"][1:]}
            else:
                r = route(grid, land, start, t0, dest, y.speed_table or "cruiser27", y.polar_factor,
                          max_hours=max_days * 24, motor_max_h=cap, motor_early=st.get("motor_early"), **kw)
            if r.get("status") != "ok":
                res_y = {"year": yr, "status": "no_route", "on_time": False, "diagnostics": r.get("diagnostics")}
            else:
                hrs = r["hours"]
                res_y = {"year": yr, "status": "ok", "eta": r["eta_utc"], "days": round(hrs / 24, 2),
                         "on_time": hrs <= allow_h, "margin_h": round(allow_h - hrs, 1), "sailed_nm": r["sailed_nm"],
                         "motor_hours": round(r.get("motor_hours", 0.0), 1),
                         "sailing_pct": round(100 * (1 - r.get("motor_hours", 0.0) / hrs)) if hrs else None,
                         "track": _thin(r["track"])}
            out["strategies"][key][bucket].append(res_y)
            log(logger, logging.INFO, "strategy_year", strategy=key, basis=bucket, year=yr, days=res_y.get("days"))
        save()
    out["status"] = "complete"
    save()
    return out


def typical_run(results, every_h=12.0):
    """The run whose track stays closest, on average, to all the others (positions compared every
    `every_h` hours from departure): a better 'suggested route' than the median-time run, which can be
    an outlier in a different part of the ocean."""
    from .geo import haversine_nm

    ok = [r for r in results if r.get("status") == "ok" and r.get("track")]
    if len(ok) < 3:
        return sorted(ok, key=lambda r: r["days"])[len(ok) // 2] if ok else None

    def pos(track, h):
        t0 = parse_iso(track[0][0])
        prev = None
        for p in track:
            dt = (parse_iso(p[0]) - t0).total_seconds() / 3600
            if dt >= h:
                if prev is None:
                    return p[1], p[2]
                pdt = (parse_iso(prev[0]) - t0).total_seconds() / 3600
                f = (h - pdt) / max(1e-6, dt - pdt)
                return prev[1] + (p[1] - prev[1]) * f, prev[2] + (p[2] - prev[2]) * f
            prev = p
        return track[-1][1], track[-1][2]

    hours = [every_h * i for i in range(int(min(r["days"] for r in ok) * 24 / every_h) + 1)]
    P = [[pos(r["track"], h) for h in hours] for r in ok]
    best, best_d = None, None
    for i, r in enumerate(ok):
        d = sum(haversine_nm(*P[i][k], *P[j][k]) for j in range(len(ok)) if j != i for k in range(len(hours)))
        if best_d is None or d < best_d:
            best, best_d = r, d
    return best


def strategies_markdown(out) -> str:
    st = out["settings"]
    lines = [f"# {out['yacht']}: routing strategies", "",
             f"Leaving {parse_iso(out['depart']):%a %d %b %H:%M} UTC, deadline {parse_iso(out['deadline']):%d %b %H:%M} UTC. "
             f"Forecast issued {parse_iso(out['issued_utc']):%d %b %H:%M} UTC ({', '.join(out.get('models_used', []))}, "
             f"blended), its weight fading steadily from day {out.get('handover_day', ['?'])[0]} of the passage to "
             f"zero by day {out.get('handover_day', ['?', '?'])[1]}, with each past year's weather taking over. Polar {st['table']} x{st['polar_factor']}, motor at "
             f"{st['motor_speed_kn']} kn below {st['motor_wind_below_kn']} kn of wind, at most {st['motor_max_h']} h. "
             f"Status: {out['status']}.", "",
             *( [f"Forecast model weights from past accuracy ({out.get('weights_checks')} checks): " + "; ".join(
                 f"{lead}: " + ", ".join(f"{m} {p}%" for m, p in w.items()) for lead, w in out["model_weights"].items()), ""]
                if out.get("model_weights") else []),
             "| Strategy | On time | Probability | El Nino-weighted | Median days | 10-90 % days | Slowest | Sailing | Median motoring | Median fuel |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for k, v in out["strategies"].items():
        s = v.get("summary") or {}
        if not s.get("years"):
            lines.append(f"| {k}: {v['label']} | - | | | | | | | | |")
            continue
        lines.append(f"| {k}: {v['label']} | {s['on_time']}/{s['years']} | {round(100 * s['probability'])}% | "
                     f"{round(100 * (s.get('probability_enso_weighted') or 0))}% | {s['days_median']} | "
                     f"{s['days_p10']}-{s['days_p90']} | {s['days_max']} | {s.get('sailing_pct_median', '-')}% | "
                     f"{s['motor_hours_median']} h | {s.get('fuel_l_median', '-')} L |")
    lines += ["", "## In past years' own weather (no forecast)", "",
              "| Strategy | On time | Median days | 10-90 % days | Slowest | Sailing | Median motoring | Median fuel |",
              "|---|---|---|---|---|---|---|---|"]
    for k, v in out["strategies"].items():
        s = v.get("history_summary") or {}
        if s.get("years"):
            lines.append(f"| {k}: {v['label']} | {s['on_time']}/{s['years']} | {s['days_median']} | "
                         f"{s['days_p10']}-{s['days_p90']} | {s['days_max']} | {s.get('sailing_pct_median', '-')}% | "
                         f"{s['motor_hours_median']} h | {s.get('fuel_l_median', '-')} L |")
    keys = list(out["strategies"])
    if "A" in keys and len(keys) > 1:
        byyear = {k: {r["year"]: r for r in out["strategies"][k]["history"] if r.get("status") == "ok"} for k in keys}
        others = [k for k in keys if k != "A"]
        lines += ["", "Year by year in past weather (days; + = slower than A, the router's fastest):", "",
                  "| Year | A days | " + " | ".join(f"{k} days | {k} vs A" for k in others) + " |",
                  "|---|---|" + "---|---|" * len(others)]
        for yr in sorted(byyear["A"]):
            a = byyear["A"][yr]["days"]
            cells = []
            for k in others:
                r = byyear[k].get(yr)
                cells += [f"{r['days']}" if r else "-", f"{(r['days'] - a) * 24:+.0f} h" if r else "-"]
            lines.append(f"| {yr} | {a} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


STRATEGY_COLOURS = {"A": (200, 40, 40), "B": (20, 90, 190), "C": (30, 140, 70), "D": (210, 120, 0),
                    "E": (120, 40, 150)}


def strategies_jpeg(path, out, width=1800):
    from .chart import _basemap

    allr = [(k, r) for k, v in out["strategies"].items() for r in v["results"] if r.get("status") == "ok"]
    if not allr:
        return None
    lats = [p[1] for _, r in allr for p in r["track"]] + [out["to"][0]]
    lons = [p[2] for _, r in allr for p in r["track"]] + [out["to"][1]]
    img, d, xy, (f_s, f_m, f_l), (w, h, top) = _basemap(lats, lons, width, top=130)
    for k, r in allr:
        col = STRATEGY_COLOURS.get(k, (90, 90, 90))
        d.line([xy(p[1], p[2]) for p in r["track"]], fill=col, width=1)
    for k, v in out["strategies"].items():  # the most typical run of each strategy, drawn bold
        med = typical_run(v["results"])
        if med:
            d.line([xy(p[1], p[2]) for p in med["track"]], fill=STRATEGY_COLOURS.get(k, (90, 90, 90)), width=5)
    for la, lo in (out["from"], out["to"]):
        x, yy = xy(la, lo)
        d.ellipse([x - 8, yy - 8, x + 8, yy + 8], fill=(20, 20, 20))
    d.rectangle([0, 0, w, top], fill=(255, 255, 255))
    d.text((16, 10), f"{out['yacht']}: routing strategies, leaving {parse_iso(out['depart']):%a %d %b} "
                     f"(forecast {parse_iso(out['issued_utc']):%d %b %H:%M}Z, then past years)", fill=(30, 30, 40), font=f_l)
    x, yy = 16, 52
    for k, v in out["strategies"].items():
        s = v.get("summary") or {}
        lab = (f"{k}: {v['label']} - median {s.get('days_median')} d, sailing {s.get('sailing_pct_median', '-')}%, "
               f"{s.get('on_time')}/{s.get('years')} on time")
        wlab = int(d.textlength(lab, font=f_s)) if hasattr(d, "textlength") else 9 * len(lab)
        if x + wlab > w - 16:
            x, yy = 16, yy + 24
        d.text((x, yy), lab, fill=STRATEGY_COLOURS.get(k, (90, 90, 90)), font=f_s)
        x += wlab + 30
    img.save(path, "JPEG", quality=88, optimize=True)
    return path


def summarise(results, allow_h, weights=None) -> dict:
    ok = [r for r in results if r.get("status") == "ok"]
    # a route the router could not complete is a model failure, not a slow crossing: reported apart
    counted = ok
    failed = sum(1 for r in results if r.get("status") == "no_route")
    if not counted:
        return {"years": 0, "router_failures": failed}
    days = sorted(r["days"] for r in ok)

    def pct(p):
        if not days:
            return None
        k = (len(days) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(days) - 1)
        return round(days[lo] + (days[hi] - days[lo]) * (k - lo), 2)

    on = sum(1 for r in counted if r.get("on_time"))
    extra = {}
    if weights:
        w = [(weights.get(r["year"], 1.0), bool(r.get("on_time"))) for r in counted]
        tw = sum(x for x, _ in w)
        extra["probability_enso_weighted"] = round(sum(x for x, o in w if o) / tw, 2) if tw else None
        extra["effective_years"] = round(tw ** 2 / sum(x * x for x, _ in w), 1) if tw else None
        en = [r for r in counted if weights.get(r["year"], 0) >= 0.2]
        if en:
            extra["el_nino_like_years"] = len(en)
            extra["el_nino_like_on_time"] = sum(1 for r in en if r.get("on_time"))
    return {**extra, "router_failures": failed, "years": len(counted), "on_time": on,
            "probability": round(on / len(counted), 2),
            "allowed_days": round(allow_h / 24, 2), "days_median": pct(0.5), "days_p10": pct(0.1),
            "days_p90": pct(0.9), "days_min": days[0] if days else None, "days_max": days[-1] if days else None,
            "motor_hours_median": round(statistics.median(r["motor_hours"] for r in ok), 1) if ok else None,
            "sailed_nm_median": round(statistics.median(r["sailed_nm"] for r in ok)) if ok else None}


def markdown(out) -> str:
    s = out.get("summary") or {}
    st = out["settings"]
    en = out.get("enso") or {}
    ey = en.get("years") or {}
    now = en.get("now") or {}
    lines = [f"# {out['yacht']}: passage odds", "",
             f"Leaving {parse_iso(out['depart']):%d %b %H:%M} UTC, deadline {parse_iso(out['deadline']):%d %b %H:%M} UTC "
             f"({out['allowed_days']} days). Polar {st['table']} x{st['polar_factor']}, gybe {st['gybe_min']} min, "
             f"motor at {st['motor_speed_kn']} kn below {st['motor_wind_below_kn']} kn of wind, "
             f"at most {st.get('motor_max_h')} h (fuel{', ' + st['fuel'] if st.get('fuel') else ''}). "
             f"Current: {st.get('current', 'not modelled')}. Waves: {st.get('waves', 'not modelled')}. "
             f"Status: {out['status']}.", ""]
    fc = out.get("forecast") or {}
    fs = fc.get("summary") or {}
    if fs.get("years"):
        lines += [f"## With the live forecast (issued {parse_iso(fc['issued_utc']):%d %b %H:%M} UTC)", "",
                  f"Forecast ({', '.join(fc.get('models_used', []))}, skill-weighted blend), its weight fading steadily "
                  f"from day {(fc.get('handover_day') or ['?'])[0]} of the passage to zero by day "
                  f"{(fc.get('handover_day') or ['?', '?'])[1]}, with each past year's weather taking over.", "",
                  f"**On time in {fs['on_time']} of {fs['years']} ({round(100 * fs['probability'])}%); "
                  f"El Nino-weighted {round(100 * (fs.get('probability_enso_weighted') or 0))}%.** "
                  f"Median {fs['days_median']} days (10-90 %: {fs['days_p10']}-{fs['days_p90']}), "
                  f"{fs['motor_hours_median']} h motoring."
                  + (f" ({fs['router_failures']} run(s) the router could not complete are left out.)"
                     if fs.get("router_failures") else ""), ""]
        pm = fc.get("per_model") or {}
        if pm:
            lines += ["Each model's own forecast, then the same past years (how much the answer depends on "
                      "which model is right):", "",
                      "| Forecast | On time | Probability | El Nino-weighted | Median days | 10-90 % days | Median motor h |",
                      "|---|---|---|---|---|---|---|",
                      f"| blend | {fs['on_time']}/{fs['years']} | {round(100 * fs['probability'])}% | "
                      f"{round(100 * (fs.get('probability_enso_weighted') or 0))}% | {fs['days_median']} | "
                      f"{fs['days_p10']}-{fs['days_p90']} | {fs['motor_hours_median']} |"]
            for m, v in pm.items():
                ms = v.get("summary") or {}
                if ms.get("years"):
                    lines.append(f"| {m} | {ms['on_time']}/{ms['years']} | {round(100 * ms['probability'])}% | "
                                 f"{round(100 * (ms.get('probability_enso_weighted') or 0))}% | {ms['days_median']} | "
                                 f"{ms['days_p10']}-{ms['days_p90']} | {ms['motor_hours_median']} |")
            lines.append("")
    elif fc.get("status"):
        lines += [f"Live forecast: {fc['status']}.", ""]
    if s.get("years"):
        lines += ["## From past weather alone", "",
                  f"**On time in {s['on_time']} of {s['years']} years ({round(100 * s['probability'])}%).** "
                  + (f"Weighted to this year's El Nino (ONI {now.get('oni')} in {now.get('season')}): "
                     f"**{round(100 * (s.get('probability_enso_weighted') or 0))}%** "
                     f"(about {s.get('effective_years')} years' worth of evidence); in the "
                     f"{s.get('el_nino_like_years')} El Nino-like years: {s.get('el_nino_like_on_time')} on time. "
                     if s.get("probability_enso_weighted") is not None else "")
                  + f"Passage time: median {s['days_median']} days (10-90 %: {s['days_p10']}-{s['days_p90']}); "
                  f"median {s['sailed_nm_median']} nm sailed, {s['motor_hours_median']} h motoring.", "",
                  "| Year | ENSO (Oct-Dec ONI) | Weight | Arrives (this year) | Days | On time | Margin (h) | Sailed nm | Avg kn | Motor h | Median wind kn | Furthest south | Sea state |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in sorted(out["results"], key=lambda r: r["year"]):
            e = ey.get(str(r["year"]), {})
            ens = f"{e.get('phase', '-')} ({e.get('oni')})" if e else "-"
            wt = e.get("weight", "-")
            if r.get("status") != "ok":
                lines.append(f"| {r['year']} | {ens} | {wt} | {r.get('status')} | | {'no' if r.get('status') == 'no_route' else '-'} | | | | | | | |")
                continue
            dep = parse_iso(out["depart"])
            eta_now = parse_iso(r["eta"]) + (dep - dep.replace(year=r["year"]))  # on this year's calendar
            lines.append(f"| {r['year']} | {ens} | {wt} | {eta_now:%a %d %b %H:%M} | {r['days']} | "
                         f"{'yes' if r['on_time'] else 'no'} | {r['margin_h']:+} | {r['sailed_nm']} | {r['avg_kn']} | "
                         f"{r['motor_hours']} | {r['wind_median_kn']} | {r['southmost_lat']}N | "
                         f"{ {'era5': 'ERA5 waves', 'wind': 'from wind', 'none': '-'}.get(r.get('waves'), '-') } |")
    return "\n".join(lines) + "\n"


def tracks_jpeg(path, out, width=1800):
    from .chart import _basemap

    ok = [r for r in out["results"] if r.get("status") == "ok"]
    if not ok:
        return None
    lats = [p[1] for r in ok for p in r["track"]] + [out["to"][0]]
    lons = [p[2] for r in ok for p in r["track"]] + [out["to"][1]]
    img, d, xy, (f_s, f_m, f_l), (w, h, top) = _basemap(lats, lons, width)
    for r in sorted(ok, key=lambda r: r["on_time"], reverse=True):
        col = (40, 150, 70) if r["on_time"] else (200, 40, 40)
        pts = [xy(p[1], p[2]) for p in r["track"]]
        d.line(pts, fill=col, width=2)
        x, yy = pts[len(pts) // 2]
        d.text((x + 4, yy - 16), str(r["year"]), fill=col, font=f_s)
    for (la, lo) in (out["from"], out["to"]):
        x, yy = xy(la, lo)
        d.ellipse([x - 8, yy - 8, x + 8, yy + 8], fill=(20, 60, 160))
    s = out.get("summary") or {}
    d.rectangle([0, 0, w, top], fill=(255, 255, 255))
    d.text((16, 12), f"{out['yacht']}: leave {parse_iso(out['depart']):%d %b}, deadline {parse_iso(out['deadline']):%d %b} "
                     f"- on time {s.get('on_time')}/{s.get('years')} years ({round(100 * (s.get('probability') or 0))}%)",
           fill=(30, 30, 40), font=f_l)
    d.text((16, 60), f"Median {s.get('days_median')} days (10-90 %: {s.get('days_p10')}-{s.get('days_p90')}). "
                     "Green: arrives in time; red: late. Routed through each year's actual weather (ERA5).",
           fill=(30, 30, 40), font=f_m)
    img.save(path, "JPEG", quality=88, optimize=True)
    return path
