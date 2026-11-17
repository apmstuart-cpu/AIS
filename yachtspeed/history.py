"""A yacht's own track history (e.g. a TimeZero/plotter GPX export) against her polar.

import-gpx   streams a GPX file of any size, keeps one point per `every_s` seconds (speed and course
             from the position change over that interval) in `track_points`, apart from AIS data.
polar-history  adds the historical wind (ERA5) and the sea state and current (Open-Meteo marine
             archive) at each point, keeps only clear sailing, and reports % of polar by wind
             strength, wind angle and season.

There is no engine data in a plain GPX track, so motoring is inferred and left out: a point counts
as sailing only with at least `min_tws` of wind, at least `min_twa` off the wind, a speed of at
least 2 kn and no more than 130 % of the polar (faster than that in light air is the engine).
Runs on the server; weather is fetched per 0.5 deg cell and month and stored.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .geo import haversine_nm, initial_bearing, iso, parse_iso, utcnow
from .logutil import log

logger = logging.getLogger("history")

SQL = """
CREATE TABLE IF NOT EXISTS track_points (
    mmsi TEXT NOT NULL, time_utc TEXT NOT NULL, lat REAL, lon REAL, sog_kn REAL, cog_deg REAL, source TEXT,
    PRIMARY KEY (mmsi, time_utc)
);
CREATE TABLE IF NOT EXISTS hist_wx (
    kind TEXT NOT NULL, cell_lat REAL NOT NULL, cell_lon REAL NOT NULL, time_utc TEXT NOT NULL,
    a REAL, b REAL, c REAL, d REAL, e REAL,
    PRIMARY KEY (kind, cell_lat, cell_lon, time_utc)
);
CREATE TABLE IF NOT EXISTS hist_wx_done (
    kind TEXT NOT NULL, cell_lat REAL NOT NULL, cell_lon REAL NOT NULL, month TEXT NOT NULL,
    PRIMARY KEY (kind, cell_lat, cell_lon, month)
);
"""
CELL = 0.5


def import_gpx(conn, path, mmsi, every_s=60, min_sog=1.0, source="gpx", progress=None) -> dict:
    """Stream a GPX file; keep a point every `every_s` seconds when moving at least `min_sog`."""
    import xml.etree.ElementTree as ET

    conn.executescript(SQL)
    ns_t = None
    last = None  # (t, lat, lon) of the last kept point
    n_read = n_kept = 0
    batch = []
    for ev, el in ET.iterparse(path, events=("end",)):
        tag = el.tag.rsplit("}", 1)[-1]
        if tag != "trkpt":
            if tag in ("trkseg", "trk"):
                el.clear()
            continue
        n_read += 1
        tt = None
        for ch in el:
            if ch.tag.rsplit("}", 1)[-1] == "time":
                tt = ch.text
        if tt:
            try:
                t = datetime.fromisoformat(tt.strip().replace("Z", "+00:00")[:26] + "+00:00"
                                           if "." in tt else tt.strip().replace("Z", "+00:00"))
            except ValueError:
                t = None
            if t is not None:
                t = t.astimezone(timezone.utc) if t.tzinfo else t.replace(tzinfo=timezone.utc)
                lat, lon = float(el.get("lat")), float(el.get("lon"))
                if last is None or (t - last[0]).total_seconds() >= every_s:
                    if last is not None and 0 < (t - last[0]).total_seconds() <= 4 * every_s:
                        h = (t - last[0]).total_seconds() / 3600
                        d = haversine_nm(last[1], last[2], lat, lon)
                        sog = d / h
                        if min_sog <= sog <= 30:
                            batch.append((mmsi, iso(t), round(lat, 5), round(lon, 5), round(sog, 2),
                                          round(initial_bearing(last[1], last[2], lat, lon), 1), source))
                            n_kept += 1
                    last = (t, lat, lon)
        el.clear()
        if len(batch) >= 5000:
            conn.executemany("INSERT OR REPLACE INTO track_points VALUES (?,?,?,?,?,?,?)", batch)
            batch.clear()
            if progress:
                progress(f"read {n_read} points, kept {n_kept} under way")
    if batch:
        conn.executemany("INSERT OR REPLACE INTO track_points VALUES (?,?,?,?,?,?,?)", batch)
    return {"read": n_read, "kept_under_way": n_kept}


def _cell(x):
    return round(math.floor(x / CELL) * CELL + CELL / 2, 3)


def fetch_weather(conn, client, mmsi, since=None, progress=None):
    """ERA5 wind and the marine archive (waves, current) for every 0.5 deg cell and month the track
    passes through. Raises QuotaExceeded when the budget is spent (rerun to resume)."""
    conn.executescript(SQL)
    q = "SELECT time_utc, lat, lon FROM track_points WHERE mmsi=?" + (" AND time_utc>=?" if since else "")
    need = {}
    for r in conn.execute(q, (mmsi, since) if since else (mmsi,)):
        need.setdefault((_cell(r[1]), _cell(r[2])), set()).add(r[0][:7])
    done = {(k, round(a, 3), round(b, 3), m) for k, a, b, m in conn.execute("SELECT * FROM hist_wx_done")}
    jobs = [(kind, c, m) for c, months in need.items() for m in sorted(months) for kind in ("wind", "marine")
            if (kind, c[0], c[1], m) not in done]
    if progress:
        progress(f"{len(need)} cells, {len(jobs)} cell-months still to fetch")
    for i, (kind, (la, lo), month) in enumerate(jobs):
        y, mo = int(month[:4]), int(month[5:7])
        start = datetime(y, mo, 1)
        end = (datetime(y + (mo == 12), mo % 12 + 1, 1) - timedelta(days=1))
        end = min(end, (utcnow() - timedelta(days=6)).replace(tzinfo=None)) if kind == "wind" else end
        rows = []
        if end >= start:
            from .weather import FetchError

            try:
                rows = client.fetch("om_era5" if kind == "wind" else "om_marine", kind, la, lo,
                                    start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
            except FetchError as e:
                log(logger, logging.WARNING, "hist_fetch_failed", kind=kind, lat=la, lon=lo, month=month, error=str(e)[:100])
        conn.execute("BEGIN")
        for r in rows:
            if kind == "wind":
                vals = (r.get("wind_speed_kn"), r.get("wind_dir_from_deg"), None, None, None)
            else:
                vals = (r.get("wave_height_m"), r.get("wave_dir_from_deg"), r.get("wave_period_s"),
                        r.get("current_speed_kn"), r.get("current_dir_to_deg"))
            if all(v is None for v in vals):
                continue
            conn.execute("INSERT OR REPLACE INTO hist_wx VALUES (?,?,?,?,?,?,?,?,?)", (kind, la, lo, r["time_utc"], *vals))
        conn.execute("INSERT OR REPLACE INTO hist_wx_done VALUES (?,?,?,?)", (kind, la, lo, month))
        conn.execute("COMMIT")
        if progress and (i + 1) % 50 == 0:
            progress(f"fetched {i + 1}/{len(jobs)}")


def analyse(conn, y, since=None, min_tws=8.0, min_twa=40.0, max_ratio=1.3, every_min=10) -> dict:
    """% of polar for clear-sailing points, one per `every_min` minutes."""
    from .geo import along_course_current, wind_angle_to_course
    from .speedtable import table_speed, wave_factor

    conn.executescript(SQL)
    q = ("SELECT time_utc, lat, lon, sog_kn, cog_deg FROM track_points WHERE mmsi=? AND sog_kn >= 2"
         + (" AND time_utc>=?" if since else "") + " ORDER BY time_utc")
    rows = conn.execute(q, (y.mmsi, since) if since else (y.mmsi,)).fetchall()
    cache = {}

    def wx(kind, la, lo, t):
        key = (kind, _cell(la), _cell(lo), t.strftime("%Y-%m-%dT%H:00:00Z"))
        if key not in cache:
            cache[key] = conn.execute("SELECT a,b,c,d,e FROM hist_wx WHERE kind=? AND cell_lat=? AND cell_lon=? "
                                      "AND time_utc=?", key).fetchone()
        return cache[key]

    pts, last_t = [], None
    counts = {"under_way": 0, "no_wind": 0, "light_or_upwind (likely motoring)": 0, "too_fast (engine)": 0}
    for r in rows:
        t = parse_iso(r["time_utc"])
        if last_t is not None and (t - last_t).total_seconds() < every_min * 60 - 1:
            continue
        last_t = t
        counts["under_way"] += 1
        tr = t + timedelta(minutes=30)
        w = wx("wind", r["lat"], r["lon"], tr.replace(minute=0))
        if not w or w[0] is None or w[1] is None:
            counts["no_wind"] += 1
            continue
        tws, wdir = w[0], w[1]
        twa = wind_angle_to_course(wdir, r["cog_deg"])
        if tws < min_tws or twa < min_twa:
            counts["light_or_upwind (likely motoring)"] += 1
            continue
        m = wx("marine", r["lat"], r["lon"], tr.replace(minute=0)) or (None,) * 5
        cur = along_course_current(m[3], m[4], r["cog_deg"]) or 0.0
        wf, _ = wave_factor(m[0], m[1], r["cog_deg"], m[2])
        polar = table_speed(tws, twa, y.speed_table, 1.0) * wf
        if polar <= 0.5:
            continue
        ratio = (r["sog_kn"] - cur) / polar
        if ratio > max_ratio:
            counts["too_fast (engine)"] += 1
            continue
        pts.append({"t": t, "tws": tws, "twa": twa, "ratio": ratio})

    def summ(sel):
        rs = sorted(p["ratio"] for p in sel)
        if not rs:
            return None
        return {"points": len(rs), "hours": round(len(rs) * every_min / 60, 1),
                "median_pct": round(100 * statistics.median(rs)), "p25_pct": round(100 * rs[len(rs) // 4]),
                "p75_pct": round(100 * rs[(3 * len(rs)) // 4])}

    tws_bands = [(8, 12), (12, 16), (16, 20), (20, 40)]
    twa_bands = [(40, 70), (70, 110), (110, 150), (150, 181)]
    grid = {f"{a}-{b} kn": {f"{c}-{min(d, 180)} deg": summ([p for p in pts if a <= p["tws"] < b and c <= p["twa"] < d])
                            for c, d in twa_bands} for a, b in tws_bands}
    seasons = {}
    for p in pts:
        seasons.setdefault(p["t"].strftime("%Y-%m"), []).append(p)
    return {"yacht": y.name, "table": y.speed_table, "configured_factor_pct": round(100 * y.polar_factor),
            "filters": {"min_tws_kn": min_tws, "min_twa_deg": min_twa, "max_ratio": max_ratio, "every_min": every_min},
            "counts": counts, "overall": summ(pts), "by_wind_and_angle": grid,
            "by_angle": {f"{c}-{min(d, 180)} deg": summ([p for p in pts if c <= p["twa"] < d]) for c, d in twa_bands},
            "by_wind": {f"{a}-{b} kn": summ([p for p in pts if a <= p["tws"] < b]) for a, b in tws_bands},
            "by_month": {k: summ(v) for k, v in sorted(seasons.items())}}


def markdown(res) -> str:
    def cell(s):
        return f"{s['median_pct']}% ({s['hours']} h)" if s else "-"

    o = res.get("overall") or {}
    lines = [f"# {res['yacht']}: sailing history against the polar ({res['table']})", "",
             f"Clear sailing only (wind at least {res['filters']['min_tws_kn']} kn, at least "
             f"{res['filters']['min_twa_deg']} deg off the wind, not faster than {round(100 * res['filters']['max_ratio'])}% "
             f"of the polar). Points considered: {res['counts']}.", ""]
    if o:
        lines += [f"**Overall: {o['median_pct']}% of polar** (middle half {o['p25_pct']}-{o['p75_pct']}%) over "
                  f"{o['hours']} hours of sailing; configured {res['configured_factor_pct']}%.", ""]
    angles = list(next(iter(res["by_wind_and_angle"].values())).keys())
    lines += ["| True wind | " + " | ".join(f"TWA {a}" for a in angles) + " | All angles |",
              "|---|" + "---|" * (len(angles) + 1)]
    for w, row in res["by_wind_and_angle"].items():
        lines.append(f"| {w} | " + " | ".join(cell(row[a]) for a in angles) + f" | {cell(res['by_wind'][w])} |")
    lines.append("| all winds | " + " | ".join(cell(res["by_angle"][a]) for a in angles) + f" | {cell(o or None)} |")
    lines += ["", "| Month | % of polar | Hours sailing |", "|---|---|---|"]
    for m, s in res["by_month"].items():
        if s:
            lines.append(f"| {m} | {s['median_pct']}% ({s['p25_pct']}-{s['p75_pct']}) | {s['hours']} |")
    return "\n".join(lines) + "\n"
