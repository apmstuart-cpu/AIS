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
                     step_nm=10.0, issue=None, wind_provider=None, store=True) -> dict:
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
            if speed is None or speed <= 0.5:
                speed = fallback
                flags.append("fallback_yacht_median")
            if not speed:
                raise ValueError(f"no historical data for {mmsi}; cannot propagate passage")
            hours = dist / speed
            steps.append({"idx": idx, "leg": leg, "start_utc": iso(t), "lat": round(mid[0], 4),
                          "lon": round(mid[1], 4), "course_deg": round(course, 1), "distance_nm": round(dist, 2),
                          "speed_kn": round(speed, 2), "hours": round(hours, 2), "flags": flags,
                          "tws_kn": r.get("wind_speed_kn"), "twa_deg": r.get("twa_deg"),
                          "wave_m": r.get("wave_height_m"), "n_matches": r.get("n_matches")})
            t = t + timedelta(hours=hours)
            idx += 1
    total_nm = sum(s["distance_nm"] for s in steps)
    flagged = [s for s in steps if s["flags"]]
    return {
        "passage_id": pid, "mmsi": mmsi, "departure_utc": iso(departure), "arrival_utc": iso(t),
        "duration_hours": round((t - departure).total_seconds() / 3600, 2), "distance_nm": round(total_nm, 1),
        "steps": steps, "flagged_steps": len(flagged),
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
