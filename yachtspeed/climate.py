"""Passage odds from past weather: route a yacht through the same calendar dates in each of
many past years (ERA5 reanalysis via Open-Meteo's archive) and count how often she arrives
in time. The standard way to plan beyond the 10-16 day forecast range.

Runs on the server only. Wind is fetched once per grid cell and year (3-hourly kept) and
stored permanently, so a run interrupted by the API's daily limit resumes where it stopped.

Not modelled: ocean current (the westgoing North Equatorial Current helps ~0.3-0.5 kn, so
the result is slightly pessimistic), sea state, squalls, crew fatigue and breakages.
"""

from __future__ import annotations

import json
import logging
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
CREATE TABLE IF NOT EXISTS climate_cells (
    year INTEGER NOT NULL, cell_lat REAL NOT NULL, cell_lon REAL NOT NULL, start_date TEXT, end_date TEXT,
    fetched_utc TEXT, PRIMARY KEY (year, cell_lat, cell_lon, start_date, end_date)
);
"""


def cells_for(bbox, res):
    lat0, lon0, lat1, lon1 = bbox
    lats = [round(lat0 + i * res, 4) for i in range(int(round((lat1 - lat0) / res)) + 1)]
    lons = [round(lon0 + j * res, 4) for j in range(int(round((lon1 - lon0) / res)) + 1)]
    return [(la, lo) for la in lats for lo in lons]


def fetch_year(conn, client, year, cells, start_date, end_date, thin_h=3, progress=None) -> int:
    """Fetch ERA5 wind for every cell not yet stored; raises QuotaExceeded when the budget is spent."""
    conn.executescript(SQL)
    done = {(r[0], r[1]) for r in conn.execute(
        "SELECT cell_lat, cell_lon FROM climate_cells WHERE year=? AND start_date=? AND end_date=?",
        (year, start_date, end_date))}
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


def run(conn, cfg, y, start, dest, depart: datetime, deadline: datetime, years, res=2.5, margin=4.0,
        max_days=28, step_h=4.0, heading_step=5, motor_wind_below_kn=None, motor_speed_kn=7.0, motor_max_h=None,
        maneuver_min=3.0, per_day=2500, per_hour=1200, per_minute=120, wait=True, out_path=None,
        name="climate") -> dict:
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
                        "motor_max_h": motor_max_h,
                        "current": "not modelled", "waves": "not modelled"},
           "results": [], "status": "running", "updated_utc": iso(utcnow())}
    if out_path and Path(out_path).exists():  # resume: keep finished years
        try:
            prev = json.loads(Path(out_path).read_text())
            if prev.get("depart") == out["depart"] and prev.get("settings") == out["settings"]:
                out["results"] = [r for r in prev.get("results", []) if r.get("status") in ("ok", "no_route")]
        except (OSError, ValueError):
            pass
    done_years = {r["year"] for r in out["results"]}

    def save():
        out["updated_utc"] = iso(utcnow())
        out["summary"] = summarise(out["results"], allow_h)
        if out_path:  # results so far, so progress is visible in the published report
            Path(out_path).write_text(json.dumps(out, indent=1))
            base = str(out_path)[:-5] if str(out_path).endswith(".json") else str(out_path)
            Path(base + ".md").write_text(markdown(out))
            try:
                tracks_jpeg(base + ".jpg", out)
            except Exception as e:  # a chart failure must not stop the run
                log(logger, logging.WARNING, "climate_chart_failed", error=str(e)[:120])

    for yr in years:
        if yr in done_years:
            continue
        d0 = depart.replace(year=yr)
        t_end = d0 + timedelta(days=max_days)
        s_date, e_date = d0.strftime("%Y-%m-%d"), (t_end + timedelta(days=1)).strftime("%Y-%m-%d")
        while True:
            try:
                fetch_year(conn, client, yr, cells, s_date, e_date)
                break
            except QuotaExceeded as e:
                pause = (e.resume_at - utcnow()).total_seconds()
                out["status"] = f"waiting for weather quota until {iso(e.resume_at)} (year {yr})"
                save()
                if not wait or pause > 26 * 3600:
                    return out
                log(logger, logging.INFO, "climate_quota_wait", until=iso(e.resume_at), year=yr)
                time.sleep(max(5.0, pause + 5))
        grid = ClimateGrid(conn, yr, bbox, d0, t_end, res).load()
        if len(grid.wind) < 0.9 * len(cells):
            out["results"].append({"year": yr, "status": "missing_weather", "cells": len(grid.wind)})
            save()
            continue
        t_run = time.time()
        r = route(grid, land, start, d0, dest, y.speed_table or "cruiser27", y.polar_factor, step_h=step_h,
                  heading_step=heading_step, maneuver_min=maneuver_min, max_hours=max_days * 24,
                  use_waves=False, use_current=False, motor_wind_below_kn=motor_wind_below_kn,
                  motor_speed_kn=motor_speed_kn, motor_max_h=motor_max_h)
        if r.get("status") != "ok":
            res_y = {"year": yr, "status": "no_route", "on_time": False}
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
        res_y["route_seconds"] = round(time.time() - t_run)
        out["results"].append(res_y)
        log(logger, logging.INFO, "climate_year", year=yr, days=res_y.get("days"), on_time=res_y.get("on_time"))
        save()
    out["status"] = "complete"
    save()
    return out


def summarise(results, allow_h) -> dict:
    ok = [r for r in results if r.get("status") == "ok"]
    counted = [r for r in results if r.get("status") in ("ok", "no_route")]
    if not counted:
        return {"years": 0}
    days = sorted(r["days"] for r in ok)

    def pct(p):
        if not days:
            return None
        k = (len(days) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(days) - 1)
        return round(days[lo] + (days[hi] - days[lo]) * (k - lo), 2)

    on = sum(1 for r in counted if r.get("on_time"))
    return {"years": len(counted), "on_time": on, "probability": round(on / len(counted), 2),
            "allowed_days": round(allow_h / 24, 2), "days_median": pct(0.5), "days_p10": pct(0.1),
            "days_p90": pct(0.9), "days_min": days[0] if days else None, "days_max": days[-1] if days else None,
            "motor_hours_median": round(statistics.median(r["motor_hours"] for r in ok), 1) if ok else None,
            "sailed_nm_median": round(statistics.median(r["sailed_nm"] for r in ok)) if ok else None}


def markdown(out) -> str:
    s = out.get("summary") or {}
    st = out["settings"]
    lines = [f"# {out['yacht']}: passage odds from past weather", "",
             f"Leaving {parse_iso(out['depart']):%d %b %H:%M} UTC, deadline {parse_iso(out['deadline']):%d %b %H:%M} UTC "
             f"({out['allowed_days']} days). Polar {st['table']} x{st['polar_factor']}, gybe {st['gybe_min']} min, "
             f"motor at {st['motor_speed_kn']} kn below {st['motor_wind_below_kn']} kn of wind, "
             f"at most {st.get('motor_max_h')} h (fuel). "
             f"Current and waves not modelled. Status: {out['status']}.", ""]
    if s.get("years"):
        lines += [f"**On time in {s['on_time']} of {s['years']} years ({round(100 * s['probability'])}%).** "
                  f"Passage time: median {s['days_median']} days (10-90 %: {s['days_p10']}-{s['days_p90']}); "
                  f"median {s['sailed_nm_median']} nm sailed, {s['motor_hours_median']} h motoring.", "",
                  "| Year | Arrives (same calendar) | Days | On time | Margin (h) | Sailed nm | Avg kn | Motor h | Median wind kn | Furthest south |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for r in sorted(out["results"], key=lambda r: r["year"]):
            if r.get("status") != "ok":
                lines.append(f"| {r['year']} | {r.get('status')} | | {'no' if r.get('status') == 'no_route' else '-'} | | | | | | |")
                continue
            lines.append(f"| {r['year']} | {parse_iso(r['eta']):%a %d %b %H:%M} | {r['days']} | "
                         f"{'yes' if r['on_time'] else 'no'} | {r['margin_h']:+} | {r['sailed_nm']} | {r['avg_kn']} | "
                         f"{r['motor_hours']} | {r['wind_median_kn']} | {r['southmost_lat']}N |")
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
