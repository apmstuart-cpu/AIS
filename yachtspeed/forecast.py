"""Single-point and passage SOG forecasts using live (or archived) weather.

Passage forecasts take ordered waypoints and a departure time, split each leg into
steps of at most `step_nm`, look up the forecast at each step's projected arrival hour,
and propagate travel time forward. Every step is stored as a prediction row with its
issue time and valid time. Route choice and navigational advice are out of scope.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from .geo import along_course_current, haversine_nm, initial_bearing, intermediate_point, iso, utcnow, wind_angle_to_course
from .model import MODEL_VERSION, SogModel
from .weather import FetchError, point_weather


def point_forecast(conn, cfg, client, model: SogModel, mmsi, lat, lon, when: datetime, course,
                   distance_nm=None, issue: datetime | None = None, wind_provider=None,
                   passage_id=None, segment_idx=None, store=True) -> dict:
    issue = issue or utcnow()
    res = {"mmsi": mmsi, "lat": lat, "lon": lon, "valid_utc": iso(when), "issue_utc": iso(issue),
           "course_deg": course, "lead_hours": round((when - issue).total_seconds() / 3600, 2)}
    try:
        wx, wprov, mprov = point_weather(conn, client, cfg, lat, lon, when, wind_provider)
    except FetchError as e:
        res.update(status="no_forecast", error=str(e))
        wx, wprov, mprov = {}, None, None
    if "wind_speed_kn" in wx and "wind_dir_from_deg" in wx:
        twa = wind_angle_to_course(wx["wind_dir_from_deg"], course)
        cur = along_course_current(wx.get("current_speed_kn"), wx.get("current_dir_to_deg"), course)
        p = model.predict(mmsi, wx["wind_speed_kn"], twa, wx.get("wave_height_m"), cur)
        res.update(
            status=p.status, n_matches=p.n, wind_speed_kn=wx["wind_speed_kn"],
            wind_dir_from_deg=wx["wind_dir_from_deg"], twa_deg=round(twa, 1),
            wave_height_m=wx.get("wave_height_m"), along_current_kn=round(cur, 2) if cur is not None else None,
            sog_median_kn=p.median, sog_p10_kn=p.p10, sog_p25_kn=p.p25, sog_p75_kn=p.p75, sog_p90_kn=p.p90,
            wind_provider=wprov, marine_provider=mprov if "wave_height_m" in wx else None,
        )
        if p.status == "ok" and distance_nm:
            res["distance_nm"] = distance_nm
            res["hours_at_speed"] = round(distance_nm / p.median, 2) if p.median > 0 else None
    elif "status" not in res:
        res.update(status="no_forecast", error="wind not available")
    if store:
        cols = ["issue_utc", "valid_utc", "lead_hours", "mmsi", "lat", "lon", "course_deg", "wind_provider",
                "marine_provider", "wind_speed_kn", "wind_dir_from_deg", "twa_deg", "wave_height_m",
                "along_current_kn", "status", "n_matches", "sog_median_kn", "sog_p25_kn", "sog_p75_kn",
                "sog_p10_kn", "sog_p90_kn", "distance_nm", "hours_at_speed"]
        conn.execute(
            f"INSERT INTO predictions(created_utc, {', '.join(cols)}, passage_id, segment_idx, model_version) "
            f"VALUES (?, {', '.join('?' * len(cols))}, ?, ?, ?)",
            (iso(utcnow()), *[res.get(c) for c in cols], passage_id, segment_idx, MODEL_VERSION),
        )
    return res


def passage_forecast(conn, cfg, client, model: SogModel, mmsi, waypoints, departure: datetime,
                     step_nm=10.0, issue=None, wind_provider=None, store=True, speed_table=None,
                     motor_below_kn=None, motor_speed_kn=None, polar_factor=1.0,
                     use_current=True) -> dict:
    """Propagate a passage step by step.

    Speed per step, in order of preference: the yacht's SOG model; else `speed_table`
    (a generic polar, see speedtable.py) if given; else the yacht's median SOG.
    If `motor_below_kn` is set, steps slower than that are motored at `motor_speed_kn`.
    Leave it unset for a yacht that cannot motor.
    """
    from .speedtable import table_speed

    if len(waypoints) < 2:
        raise ValueError("need at least two waypoints")
    pid = uuid.uuid4().hex[:12]
    fallback = model.yacht_median(mmsi)
    t = departure
    steps, idx = [], 0
    for leg, ((la1, lo1), (la2, lo2)) in enumerate(zip(waypoints, waypoints[1:])):
        leg_nm = haversine_nm(la1, lo1, la2, lo2)
        n = max(1, int(-(-leg_nm // step_nm)))
        for i in range(n):
            a = intermediate_point(la1, lo1, la2, lo2, i / n)
            b = intermediate_point(la1, lo1, la2, lo2, (i + 1) / n)
            dist = haversine_nm(*a, *b)
            course = initial_bearing(*a, *b)
            mid = intermediate_point(*a, *b, 0.5)
            r = point_forecast(conn, cfg, client, model, mmsi, mid[0], mid[1], t, course, dist, issue,
                               wind_provider, pid, idx, store)
            flags = []
            speed = r.get("sog_median_kn") if r["status"] == "ok" else None
            if r["status"] == "no_forecast":
                flags.append("forecast_unavailable")
            elif r["status"] == "insufficient_support":
                flags.append("weak_support")
            elif r.get("n_matches", 0) < 2 * cfg.min_matches:
                flags.append("thin_support")
            source = "model" if speed else None
            if (speed is None or speed <= 0.5) and speed_table and r.get("wind_speed_kn") is not None:
                stw = table_speed(r["wind_speed_kn"], r["twa_deg"], speed_table, polar_factor)
                # the table gives speed through the water; add the along-course current
                cur = r.get("along_current_kn") if use_current else None
                speed, source = stw + (cur or 0.0), "speed_table"
                flags.append("speed_table")
                if use_current and cur is None:
                    flags.append("no_current_data")
            if (speed is None or speed <= 0.5) and source != "speed_table" and fallback:
                speed, source = fallback, "yacht_median"
                flags.append("fallback_yacht_median")
            if speed is None:
                raise ValueError(f"no history or speed table for {mmsi} and no forecast at step {idx}; "
                                 "cannot propagate passage")
            if motor_below_kn and speed < motor_below_kn:
                speed, source = motor_speed_kn, "motor"
                flags.append("motoring")
            if speed < 1.0:
                flags.append("becalmed")
            speed = max(speed, 0.3)
            hours = dist / speed
            steps.append({"idx": idx, "leg": leg, "start_utc": iso(t), "lat": round(mid[0], 4),
                          "lon": round(mid[1], 4), "course_deg": round(course, 1), "distance_nm": round(dist, 2),
                          "speed_kn": round(speed, 2), "speed_source": source, "hours": round(hours, 2),
                          "flags": flags,
                          "tws_kn": r.get("wind_speed_kn"), "twa_deg": r.get("twa_deg"),
                          "wave_m": r.get("wave_height_m"), "current_kn": r.get("along_current_kn"),
                          "n_matches": r.get("n_matches")})
            t = t + timedelta(hours=hours)
            idx += 1
    total_nm = sum(s["distance_nm"] for s in steps)
    flagged = [s for s in steps if s["flags"]]
    return {
        "passage_id": pid, "mmsi": mmsi, "departure_utc": iso(departure), "arrival_utc": iso(t),
        "duration_hours": round((t - departure).total_seconds() / 3600, 2), "distance_nm": round(total_nm, 1),
        "steps": steps, "flagged_steps": len(flagged),
        "hours_by_speed_source": {k: round(sum(s["hours"] for s in steps if s["speed_source"] == k), 1)
                                  for k in {s["speed_source"] for s in steps}},
        "flagged_distance_share": round(sum(s["distance_nm"] for s in flagged) / total_nm, 3) if total_nm else 0,
    }


def backtest_voyages(conn, cfg, client, obs, min_hours=6.0, step_nm=10.0, max_voyages=20) -> list[dict]:
    """Compare predicted vs actual durations on tracked voyages, using archived weather
    and a model trained without the voyage being tested."""
    from collections import defaultdict
    from .geo import parse_iso

    by_v = defaultdict(list)
    for o in obs:
        by_v[o.voyage].append(o)
    out = []
    for vid, vo in list(by_v.items())[-max_voyages:]:
        t0, t1 = parse_iso(vo[0].time_utc), parse_iso(vo[-1].time_utc)
        hours = (t1 - t0).total_seconds() / 3600
        if hours < min_hours:
            continue
        model = SogModel(cfg, [o for o in obs if o.voyage != vid])
        # waypoints: thin the track to roughly one point per 3 hours
        wps, last = [(vo[0].lat, vo[0].lon)], t0
        for o in vo[1:-1]:
            t = parse_iso(o.time_utc)
            if (t - last).total_seconds() >= 3 * 3600:
                wps.append((o.lat, o.lon))
                last = t
        wps.append((vo[-1].lat, vo[-1].lon))
        try:
            p = passage_forecast(conn, cfg, client, model, vo[0].mmsi, wps, t0, step_nm, store=False)
        except ValueError as e:
            out.append({"voyage": vid, "error": str(e)})
            continue
        out.append({"voyage": vid, "mmsi": vo[0].mmsi, "start_utc": vo[0].time_utc, "actual_hours": round(hours, 2),
                    "predicted_hours": p["duration_hours"],
                    "error_hours": round(p["duration_hours"] - hours, 2),
                    "flagged_distance_share": p["flagged_distance_share"]})
    return out


def wind_ahead(conn, cfg, client, lat, lon, course, sog, start: datetime, threshold=10.0, hours=72,
               step_h=1.0, below=False) -> dict:
    """Dead-reckon from a position at constant course/speed and report the forecast wind
    along the way, and the first time it reaches `threshold` (or drops below it if `below`)."""
    from .geo import EARTH_RADIUS_NM
    import math

    def dest(la, lo, brg, nm):
        d, b = nm / EARTH_RADIUS_NM, math.radians(brg)
        p1, l1 = math.radians(la), math.radians(lo)
        p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
        l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
        return math.degrees(p2), math.degrees(l2)

    track, first = [], None
    n = int(hours / step_h)
    for i in range(n + 1):
        t = start + timedelta(hours=i * step_h)
        la, lo = dest(lat, lon, course, sog * i * step_h)
        try:
            wx, prov, _ = point_weather(conn, client, cfg, la, lo, t)
        except FetchError:
            track.append({"time_utc": iso(t), "lat": round(la, 3), "lon": round(lo, 3), "status": "no_forecast"})
            continue
        ws, wd = wx.get("wind_speed_kn"), wx.get("wind_dir_from_deg")
        row = {"time_utc": iso(t), "lat": round(la, 3), "lon": round(lo, 3),
               "wind_kn": round(ws, 1) if ws is not None else None, "wind_from_deg": wd,
               "wave_m": wx.get("wave_height_m"), "provider": prov}
        track.append(row)
        if first is None and ws is not None and ((ws < threshold) if below else (ws >= threshold)):
            first = row
    return {"start_utc": iso(start), "from": [lat, lon], "course_deg": course, "sog_kn": sog,
            "threshold_kn": threshold, "condition": "below" if below else "at_or_above",
            "first": first, "track": track}
