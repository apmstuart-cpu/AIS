"""Server report: status, multi-model passage ETAs and polar evidence, as JSON + Markdown.

Built on the server (its own Open-Meteo quota) and published to the repo's
`server-reports` branch by deploy/publish-report.sh, so other sessions can read it.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from .geo import haversine_nm, iso, parse_iso, utcnow
from .model import SogModel, load_observations
from .speedtable import DESTINATIONS, destination_route
from .weather import Budget, OpenMeteo, model_provider

DEFAULT_MODELS = ["ecmwf", "aifs", "gfs", "icon"]
ARRIVED_NM = 2.0


def _latest(conn, mmsi):
    return conn.execute(
        "SELECT provider_time_utc, lat, lon, sog_kn, cog_deg, source FROM ais_reports WHERE mmsi=? "
        "ORDER BY provider_time_utc DESC LIMIT 1", (mmsi,)).fetchone()


def yacht_passage(conn, cfg, client, y, models, max_fix_age_h=24.0, step_nm=10.0) -> dict:
    from .forecast import passage_forecast

    out = {"yacht": y.name, "mmsi": y.mmsi, "destination": y.destination or None}
    last = _latest(conn, y.mmsi)
    if not last:
        out["status"] = "no_fix"
        return out
    fix_t = parse_iso(last["provider_time_utc"])
    out["fix"] = {"time_utc": iso(fix_t), "lat": last["lat"], "lon": last["lon"], "sog_kn": last["sog_kn"],
                  "cog_deg": last["cog_deg"], "source": last["source"],
                  "age_h": round((utcnow() - fix_t).total_seconds() / 3600, 1)}
    from .speedtable import parse_destination
    try:
        _, dest = parse_destination(y.destination) if y.destination else (None, None)
    except KeyError:
        dest = None
    if dest is None:
        out["status"] = "no_destination"
        return out
    out["to_go_nm"] = round(haversine_nm(last["lat"], last["lon"], *dest), 1)
    if out["to_go_nm"] <= ARRIVED_NM and (last["sog_kn"] or 0) < 1.5:
        out["status"] = "arrived"
        return out
    if out["fix"]["age_h"] > max_fix_age_h:
        out["status"] = "fix_too_old"
        return out
    wps = [(last["lat"], last["lon"])] + destination_route(y.destination, last["lat"], last["lon"])
    model = SogModel(cfg, load_observations(conn, cfg, y.mmsi))
    table = y.speed_table or "cruiser27"
    rows = []
    for name in models:
        try:
            r = passage_forecast(conn, cfg, client, model, y.mmsi, wps, fix_t, step_nm,
                                 wind_provider=model_provider(name), store=False, speed_table=table,
                                 motor_below_kn=y.motor_below_kn, motor_speed_kn=y.motor_speed_kn or 7.0,
                                 polar_factor=y.polar_factor)
        except ValueError as e:
            rows.append({"model": name, "error": str(e)[:120]})
            continue
        winds = [s["tws_kn"] for s in r["steps"] if s["tws_kn"] is not None]
        rows.append({"model": name, "eta_utc": r["arrival_utc"], "hours": r["duration_hours"],
                     "gybes": round(r.get("maneuvers", 0)), "gybe_min": round(r.get("maneuver_hours", 0) * 60),
                     "avg_kn": round(r["distance_nm"] / r["duration_hours"], 1),
                     "wind_min_kn": round(min(winds), 1) if winds else None,
                     "wind_max_kn": round(max(winds), 1) if winds else None,
                     "hours_wind_ge_10": round(sum(s["hours"] for s in r["steps"]
                                                   if s["tws_kn"] is not None and s["tws_kn"] >= 10), 1),
                     "distance_nm": r["distance_nm"], "speed_sources": r["hours_by_speed_source"]})
    # isochrone routing (needs a polar); the gridded forecast is cached for 6 h
    if y.speed_table and cfg.report_route_models:
        from .router import route_yacht

        out["routed"] = []
        for name in cfg.report_route_models:
            try:
                rr = route_yacht(conn, cfg, client, y, (last["lat"], last["lon"]), fix_t, dest,
                                 model_provider(name), res=max(cfg.report_route_res, 0.5),
                                 maneuver_min=3.0)
            except Exception as e:  # never let routing break the report
                rr = {"status": "error", "error": f"{type(e).__name__}: {e}"[:160]}
            out["routed"].append({"model": name, **{k: rr.get(k) for k in (
                "status", "eta_utc", "hours", "sailed_nm", "maneuvers", "hours_beyond_forecast", "error")}})
    ok = [x for x in rows if "eta_utc" in x]
    out["models"] = rows
    out["speed_table"] = table
    out["polar_factor"] = y.polar_factor
    out["motoring"] = bool(y.motor_below_kn)
    if ok:
        etas = sorted(parse_iso(x["eta_utc"]) for x in ok)
        out["eta"] = {"median_utc": iso(etas[len(etas) // 2]), "earliest_utc": iso(etas[0]),
                      "latest_utc": iso(etas[-1]),
                      "spread_h": round((etas[-1] - etas[0]).total_seconds() / 3600, 1)}
        out["status"] = "ok"
    else:
        out["status"] = "no_forecast"
    return out


def polar_evidence(conn, cfg, client, y, since=None) -> dict | None:
    """Median share of the polar for this yacht's stored fixes (speed through water / polar)."""
    from .geo import along_course_current, wind_angle_to_course
    from .speedtable import table_speed, wave_factor
    from .weather import FetchError, point_weather

    if not y.speed_table:
        return None
    q = ("SELECT provider_time_utc, lat, lon, sog_kn, cog_deg FROM ais_reports WHERE mmsi=? AND sog_kn >= 2 "
         "AND cog_deg IS NOT NULL" + (" AND provider_time_utc >= ?" if since else "") + " ORDER BY provider_time_utc")
    ratios, last_t = [], None
    for r in conn.execute(q, (y.mmsi, since) if since else (y.mmsi,)).fetchall():
        t = parse_iso(r["provider_time_utc"])
        if last_t is not None and (t - last_t).total_seconds() < 600:
            continue  # the same fix recorded from two sources (e.g. add-fix and the screenshot log)
        last_t = t
        try:
            wx, _, _ = point_weather(conn, client, cfg, r["lat"], r["lon"], parse_iso(r["provider_time_utc"]))
        except FetchError:
            continue
        if wx.get("wind_speed_kn") is None:
            continue
        twa = wind_angle_to_course(wx["wind_dir_from_deg"], r["cog_deg"])
        cur = along_course_current(wx.get("current_speed_kn"), wx.get("current_dir_to_deg"), r["cog_deg"]) or 0.0
        wf, _ = wave_factor(wx.get("wave_height_m"), wx.get("wave_dir_from_deg"), r["cog_deg"], wx.get("wave_period_s"))
        polar = table_speed(wx["wind_speed_kn"], twa, y.speed_table, 1.0) * wf
        if polar > 0.5:
            ratios.append((r["sog_kn"] - cur) / polar)
    if not ratios:
        return {"fixes": 0}
    return {"fixes": len(ratios), "median_pct": round(100 * statistics.median(ratios)),
            "p25_pct": round(100 * sorted(ratios)[len(ratios) // 4]),
            "p75_pct": round(100 * sorted(ratios)[(3 * len(ratios)) // 4]),
            "configured_factor_pct": round(100 * y.polar_factor), "table": y.speed_table}


def build(conn, cfg, models=None) -> dict:
    from .status import status

    client = OpenMeteo(Budget(conn, cfg.calls_per_minute, cfg.calls_per_hour, cfg.calls_per_day))
    models = models or DEFAULT_MODELS
    yachts = []
    for y in cfg.fleet:
        if not y.mmsi:
            continue
        p = yacht_passage(conn, cfg, client, y, models)
        p["polar"] = polar_evidence(conn, cfg, client, y)
        yachts.append(p)
    st = status(conn, cfg)
    from .screenshots import review_list
    return {"generated_utc": iso(utcnow()), "models": models, "yachts": yachts,
            "screenshots_review": review_list(conn, 24),
            "services": {k: v.get("age_min") for k, v in st["services"].items()},
            "collector": st["collector"], "weather": {k: st["weather"][k] for k in
                                                      ("backlog_reports", "match_rate_7d", "api_calls_today", "paused")}}


def markdown(rep: dict) -> str:
    def t(s):
        return parse_iso(s).strftime("%a %d %b %H:%M") if s else "-"

    lines = [f"# Yacht report — generated {t(rep['generated_utc'])} UTC", "",
             "| Yacht | Latest fix (UTC) | Age | Position | SOG / COG | To | To go | ETA median | Range (spread) | Routed ETA |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for y in rep["yachts"]:
        f = y.get("fix") or {}
        pos = f"{f['lat']:.3f}, {f['lon']:.3f}" if f else "-"
        sc = f"{f.get('sog_kn')} kn / {f.get('cog_deg')}°" if f else "-"
        e = y.get("eta") or {}
        eta = t(e.get("median_utc")) if e else y.get("status", "")
        rng = f"{t(e['earliest_utc'])} – {t(e['latest_utc'])} ({e['spread_h']} h)" if e else ""
        rt = "; ".join(f"{r['model']} {t(r['eta_utc'])} ({r['maneuvers']} gybes/tacks, {r['sailed_nm']} nm)"
                       if r.get("status") == "ok" else f"{r['model']} {r.get('status')}"
                       for r in (y.get("routed") or []))
        lines.append(f"| {y['yacht']} | {t(f.get('time_utc'))} | {f.get('age_h', '-')} h | {pos} | {sc} | "
                     f"{y.get('destination') or '-'} | {y.get('to_go_nm', '-')} nm | {eta} | {rng} | {rt or '-'} |")
    lines.append("")
    for y in rep["yachts"]:
        if not y.get("models"):
            continue
        how = "motoring" if y.get("motoring") else f"{y['speed_table']} x{y['polar_factor']:g}, sail only"
        lines += [f"## {y['yacht']} → {y['destination']} ({how})", "",
                  "| Model | ETA (UTC) | Hours | Avg kn | Wind kn (min–max) | Hours ≥10 kn | Gybes/tacks (time) |",
                  "|---|---|---|---|---|---|---|"]
        for m in y["models"]:
            if "error" in m:
                lines.append(f"| {m['model']} | unavailable | | | | | |")
            else:
                lines.append(f"| {m['model']} | {t(m['eta_utc'])} | {m['hours']} | {m['avg_kn']} | "
                             f"{m['wind_min_kn']}–{m['wind_max_kn']} | {m['hours_wind_ge_10']} | "
                             f"{m.get('gybes', 0)} ({m.get('gybe_min', 0)} min) |")
        p = y.get("polar")
        if p and p.get("fixes"):
            lines.append(f"\nPolar evidence: median **{p['median_pct']}%** of {p['table']} "
                         f"(IQR {p['p25_pct']}–{p['p75_pct']}%) over {p['fixes']} fixes; "
                         f"configured {p['configured_factor_pct']}%.")
        lines.append("")
    rv = rep.get("screenshots_review") or []
    if rv:
        lines.append("Screenshots needing review (last 24 h): " + "; ".join(
            f"{r['file']} ({r['notes'] or r['status']})" for r in rv) + "\n")
    lines.append(f"Services (heartbeat age, min): {rep['services']}; collector {rep['collector']}; "
                 f"weather {rep['weather']}")
    return "\n".join(lines) + "\n"


def write(rep: dict, out_dir: str) -> list[str]:
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps(rep, indent=2, default=str))
    (d / "report.md").write_text(markdown(rep))
    return [str(d / "report.json"), str(d / "report.md")]
