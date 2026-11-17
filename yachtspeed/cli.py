"""Command-line entry point: `yachtspeed <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import __version__, db, logutil
from .config import load_config
from .geo import iso, parse_iso, utcnow


def _conn(cfg):
    conn = db.connect(cfg.db_path)
    db.sync_fleet(conn, cfg.fleet)
    return conn


def _client(conn, cfg):
    from .weather import Budget, OpenMeteo

    return OpenMeteo(Budget(conn, cfg.calls_per_minute, cfg.calls_per_hour, cfg.calls_per_day))


def _mmsi(cfg, value):
    """Accept an MMSI or a configured yacht name."""
    for y in cfg.fleet:
        if value and value.lower() in (y.name.lower(), y.mmsi):
            if not y.mmsi:
                sys.exit(f"{y.name} has no MMSI configured")
            return y.mmsi
    return value


def _print(obj):
    print(json.dumps(obj, indent=2, default=str))


def cmd_collect(cfg, a):
    from . import collector

    collector.main(cfg, _conn(cfg))


def cmd_enrich(cfg, a):
    from . import enricher

    conn = _conn(cfg)
    if a.retry_failed:
        print(f"reset {enricher.retry_failed(conn)} failed fetches/rows for retry")
    if a.backfill_since:
        from .weather import Budget, OpenMeteo

        client = OpenMeteo(Budget(conn, cfg.calls_per_minute, cfg.calls_per_hour, cfg.calls_per_day))
        while True:
            s = enricher.enrich_once(conn, cfg, client, limit=5000, since=a.backfill_since)
            _print(s)
            if not s["candidates"] or s["quota_paused_until"] or s["waiting"] == s["candidates"]:
                break
        return
    enricher.main(cfg, conn, once=a.once)


def cmd_status(cfg, a):
    from .status import format_text, status

    s = status(_conn(cfg), cfg)
    print(json.dumps(s, indent=2, default=str) if a.json else format_text(s))


def cmd_discover(cfg, a):
    from .collector import discover

    _print(asyncio.run(discover(cfg, _conn(cfg), a.name, a.minutes)))


def cmd_check_stream(cfg, a):
    from .collector import check_stream

    if a.box:
        la1, lo1, la2, lo2 = map(float, a.box.split(","))
        cfg.bounding_boxes = [[[min(la1, la2), min(lo1, lo2)], [max(la1, la2), max(lo1, lo2)]]]
        a.all = True
    r = asyncio.run(check_stream(cfg, a.seconds, use_filters=not a.all))
    if a.box:
        r["box"] = cfg.bounding_boxes[0]
    _print(r)


def cmd_samples(cfg, a):
    conn = _conn(cfg)
    from .ais import parse_message

    rows = conn.execute("SELECT msg_type, payload FROM raw_samples ORDER BY id DESC LIMIT ?", (a.limit,)).fetchall()
    for r in rows:
        msg = json.loads(r["payload"])
        try:
            parsed = parse_message(msg)
            parsed = parsed.__dict__ if parsed else None
        except Exception as e:  # show the reason a sample was rejected
            parsed = {"rejected": str(e)}
        _print({"msg_type": r["msg_type"], "raw": msg, "parsed": parsed})


def cmd_add_fix(cfg, a):
    """Record a position you read yourself (e.g. from the MarineTraffic app or the crew)."""
    import re as _re
    from datetime import timedelta

    from .ais import Position, clean_cog, clean_sog, valid_latlon
    from .collector import Store

    conn = _conn(cfg)
    mmsi = _mmsi(cfg, a.yacht)
    lat, lon = map(float, a.pos.split(","))
    if not valid_latlon(lat, lon):
        sys.exit("invalid position")
    m = _re.fullmatch(r"(\d+)\s*(m|min|h)\s*ago", (a.time or "").strip())
    when = utcnow() - (timedelta(minutes=int(m[1])) if m[2] in ("m", "min") else timedelta(hours=int(m[1]))) \
        if m else (parse_iso(a.time) if a.time else utcnow())
    p = Position(mmsi=mmsi, msg_type="manual", ais_class=None, provider_time=when, lat=lat, lon=lon,
                 sog_kn=clean_sog(a.sog), cog_deg=clean_cog(a.cog), heading_deg=None, nav_status=None,
                 ais_second=None, position_accuracy=None)
    src = f"manual:{a.source}"
    stored = Store(conn, cfg).store_position(p, source=src)
    print(f"{'stored' if stored else 'already have a fix in that 10-minute slot for'} {a.yacht} "
          f"{lat:.4f},{lon:.4f} at {iso(when)} SOG {p.sog_kn} COG {p.cog_deg} [{src}]")


def cmd_import(cfg, a):
    from .collector import Store
    from .io_csv import import_csv

    _print(import_csv(Store(_conn(cfg), cfg), a.path, a.label))


def cmd_export(cfg, a):
    from .io_csv import export_csv

    n = export_csv(_conn(cfg), a.out, a.which, _mmsi(cfg, a.yacht), a.since, a.until)
    print(f"wrote {n} rows to {a.out}")


def _model(conn, cfg, mmsi, until=None):
    from .model import SogModel, load_observations

    obs = load_observations(conn, cfg, mmsi, until=until)
    return SogModel(cfg, obs), obs


def cmd_forecast(cfg, a):
    from .forecast import point_forecast

    conn = _conn(cfg)
    mmsi = _mmsi(cfg, a.yacht)
    model, obs = _model(conn, cfg, mmsi)
    when = parse_iso(a.time)
    r = point_forecast(conn, cfg, _client(conn, cfg), model, mmsi, a.lat, a.lon, when, a.course,
                       a.distance, wind_provider=a.wind_provider, store=not a.no_store)
    r["history_rows"] = len(obs)
    _print(r)


def _latest(conn, mmsi):
    return conn.execute(
        "SELECT provider_time_utc, lat, lon, sog_kn, cog_deg, source FROM ais_reports WHERE mmsi=? "
        "ORDER BY provider_time_utc DESC LIMIT 1", (mmsi,)).fetchone()


def _start_point(conn, a, mmsi):
    """--from LAT,LON [--course --speed --at], else the yacht's latest stored report."""
    last = _latest(conn, mmsi)
    if a.from_:
        lat, lon = map(float, a.from_.split(","))
        at = parse_iso(a.at) if a.at else utcnow()
        course, sog, src = a.course, a.speed, "given"
    elif last:
        lat, lon, at = last["lat"], last["lon"], parse_iso(last["provider_time_utc"])
        course = a.course if a.course is not None else last["cog_deg"]
        sog = a.speed if a.speed is not None else last["sog_kn"]
        src = f"latest report {last['provider_time_utc']} ({last['source']})"
        age_h = (utcnow() - at).total_seconds() / 3600
        if age_h > 6:
            print(f"warning: latest report is {age_h:.0f} h old; pass --from LAT,LON for a fresher start",
                  file=sys.stderr)
    else:
        sys.exit("no stored position for this yacht; pass --from LAT,LON (and --course/--speed for wind-ahead)")
    return lat, lon, at, course, sog, src


def _depart(a, at):
    """Departure for a passage: --depart if given, else the time of the start fix, so a
    passage from an older fix is projected forward from when the yacht was actually there."""
    if a.depart:
        return parse_iso(a.depart)
    return at


def cmd_passage(cfg, a):
    from .forecast import passage_forecast
    from .speedtable import DESTINATIONS, destination_route

    conn = _conn(cfg)
    mmsi = _mmsi(cfg, a.yacht)
    model, _ = _model(conn, cfg, mmsi)
    if a.to:
        from .speedtable import parse_destination
        try:
            parse_destination(a.to)
        except KeyError as e:
            sys.exit(str(e))
        lat, lon, at, _, _, src = _start_point(conn, a, mmsi)
        wps = [(lat, lon)] + destination_route(a.to, lat, lon) + [tuple(map(float, w.split(","))) for w in a.waypoints]
        depart = _depart(a, at)
    else:
        if len(a.waypoints) < 2 or not a.depart:
            sys.exit("give --to NAME, or --depart plus at least two lat,lon waypoints")
        wps = [tuple(map(float, w.split(","))) for w in a.waypoints]
        depart, src = parse_iso(a.depart), "waypoints"
    y = next((y for y in cfg.fleet if y.mmsi == mmsi), None)
    a.speed_table = a.speed_table or (y.speed_table if y and y.speed_table else "cruiser27")
    a.polar_factor = a.polar_factor if a.polar_factor is not None else (y.polar_factor if y else 1.0)
    if a.motor_below is None and y and y.motor_below_kn and not a.no_motor:
        a.motor_below = y.motor_below_kn
        a.motor_speed = a.motor_speed if a.motor_speed is not None else (y.motor_speed_kn or 7.0)
    if a.motor_speed is None:
        a.motor_speed = 7.0
    table = None if a.speed_table == "none" else a.speed_table
    if a.models:
        return _passage_models(conn, cfg, a, model, mmsi, wps, depart, table)
    try:
        r = passage_forecast(conn, cfg, _client(conn, cfg), model, mmsi, wps, depart, a.step_nm,
                             wind_provider=a.wind_provider, store=not a.no_store, speed_table=table,
                             motor_below_kn=a.motor_below, motor_speed_kn=a.motor_speed,
                             polar_factor=a.polar_factor, use_current=not a.no_current,
                             use_waves=not a.no_waves, maneuver_loss_min=a.gybe_loss_min,
                             maneuver_every_h=a.gybe_every_h)
    except ValueError as e:
        sys.exit(f"cannot forecast passage: {e}")
    r["start"] = src
    r["route"] = [[round(x, 3), round(y, 3)] for x, y in wps]
    if a.json:
        if not a.steps:
            r["steps"] = [{k: s[k] for k in ("idx", "start_utc", "distance_nm", "speed_kn", "speed_source", "flags")}
                          for s in r["steps"]]
        _print(r)
        return
    print(f"{a.yacht}: {r['distance_nm']} nm, depart {r['departure_utc']} -> ETA {r['arrival_utc']} "
          f"({r['duration_hours']} h, avg {r['distance_nm'] / r['duration_hours']:.1f} kn)")
    print(f"start: {src}; route: " + " -> ".join(f"{x},{y}" for x, y in r["route"]))
    print(f"hours by speed source: {r['hours_by_speed_source']}"
          + ("" if a.motor_below else "  (no motoring)")
          + (f"; speed table {table} x{a.polar_factor:g}" if table else "")
          + ("; current ignored" if a.no_current else "; incl. along-course current")
          + ("; waves ignored" if a.no_waves else "; incl. sea state (wave angle: 0 = head seas)"))
    if r.get("maneuvers"):
        print(f"tacks/gybes: ~{r['maneuvers']:.0f} (every {a.gybe_every_h:g} h when needed, {a.gybe_loss_min:g} min each "
              f"= {r['maneuver_hours'] * 60:.0f} min); hours by mode {r['hours_by_sail_mode']}")
    last = None
    for s_ in r["steps"]:
        t = parse_iso(s_["start_utc"])
        if a.steps or last is None or (t - last).total_seconds() >= 6 * 3600 - 60:
            tws = f"{s_['tws_kn']:.1f}" if s_["tws_kn"] is not None else "  ? "
            twa = f"{s_['twa_deg']:.0f}" if s_["twa_deg"] is not None else "?"
            cur = f"{s_['current_kn']:+.1f}" if s_.get("current_kn") is not None else "  ? "
            wv = (f"{s_['wave_m']:.1f}m@{s_['wave_rel_deg']:.0f}" if s_.get("wave_rel_deg") is not None
                  else (f"{s_['wave_m']:.1f}m" if s_.get("wave_m") is not None else "  ? "))
            print(f"  {t:%a %d %H:%MZ}  {s_['lat']:7.3f} {s_['lon']:8.3f}  wind {tws} kn TWA {twa:>3}  "
                  f"waves {wv}  current {cur}  SOG {s_['speed_kn']:4.1f} kn [{s_['speed_source']}] "
                  f"{' '.join(f for f in s_['flags'] if f not in ('speed_table', 'weak_support'))}")
            last = t
    print(f"  {parse_iso(r['arrival_utc']):%a %d %H:%MZ}  arrive")


def _passage_models(conn, cfg, a, model, mmsi, wps, depart, table):
    """Run the same passage on several forecast models and show the spread of ETAs."""
    from .forecast import passage_forecast
    from .weather import model_provider

    names = [m.strip() for m in a.models.split(",") if m.strip()]
    client = _client(conn, cfg)
    rows = []
    for name in names:
        try:
            r = passage_forecast(conn, cfg, client, model, mmsi, wps, depart, a.step_nm,
                                 wind_provider=model_provider(name), store=False, speed_table=table,
                                 motor_below_kn=a.motor_below, motor_speed_kn=a.motor_speed,
                                 polar_factor=a.polar_factor, use_current=not a.no_current,
                                 use_waves=not a.no_waves, maneuver_loss_min=a.gybe_loss_min,
                             maneuver_every_h=a.gybe_every_h)
        except ValueError as e:
            rows.append({"model": name, "error": str(e)})
            continue
        winds = [s_["tws_kn"] for s_ in r["steps"] if s_["tws_kn"] is not None]
        rows.append({"model": name, "eta_utc": r["arrival_utc"], "hours": r["duration_hours"],
                     "gybes": round(r.get("maneuvers", 0)), "gybe_min": round(r.get("maneuver_hours", 0) * 60),
                     "avg_kn": round(r["distance_nm"] / r["duration_hours"], 1),
                     "wind_min_kn": round(min(winds), 1) if winds else None,
                     "wind_max_kn": round(max(winds), 1) if winds else None,
                     "becalmed_steps": sum("becalmed" in s_["flags"] for s_ in r["steps"]),
                     "distance_nm": r["distance_nm"]})
    ok = [x for x in rows if "eta_utc" in x]
    spread = None
    if ok:
        etas = sorted(parse_iso(x["eta_utc"]) for x in ok)
        spread = {"earliest_utc": iso(etas[0]), "latest_utc": iso(etas[-1]),
                  "median_utc": iso(etas[len(etas) // 2]),
                  "spread_hours": round((etas[-1] - etas[0]).total_seconds() / 3600, 1)}
    if a.json:
        _print({"departure_utc": iso(depart), "route": [[round(x, 3), round(y, 3)] for x, y in wps],
                "models": rows, "spread": spread})
        return
    print(f"{a.yacht}: depart {iso(depart)}, {ok[0]['distance_nm'] if ok else '?'} nm; "
          f"speed table {table} x{a.polar_factor:g}" + ("" if a.motor_below else ", no motoring"))
    print(f"  {'model':8s} {'ETA (UTC)':17s} {'hours':>6s} {'avg kn':>6s} {'wind kn':>10s} becalmed")
    for x in rows:
        if "error" in x:
            print(f"  {x['model']:8s} unavailable: {x['error'][:70]}")
        else:
            print(f"  {x['model']:8s} {parse_iso(x['eta_utc']):%a %d %H:%MZ}   {x['hours']:6.1f} {x['avg_kn']:6.1f} "
                  f"{x['wind_min_kn']:4.1f}-{x['wind_max_kn']:<5.1f} {x['becalmed_steps']}")
    if spread:
        print(f"  spread: {parse_iso(spread['earliest_utc']):%a %d %H:%MZ} to {parse_iso(spread['latest_utc']):%a %d %H:%MZ}"
              f" ({spread['spread_hours']} h); median {parse_iso(spread['median_utc']):%a %d %H:%MZ}")


def cmd_wind_ahead(cfg, a):
    from .forecast import wind_ahead

    conn = _conn(cfg)
    mmsi = _mmsi(cfg, a.yacht)
    lat, lon, at, course, sog, src = _start_point(conn, a, mmsi)
    if course is None or sog is None:
        sys.exit("course/speed unknown; pass --course and --speed")
    if a.models:
        from .forecast import wind_ahead as _wa
        from .weather import model_provider

        cond = "below" if a.below else "at or above"
        print(f"{a.yacht}: from {lat:.3f},{lon:.3f} at {iso(at)}, {course:.0f} deg at {sog:.1f} kn; "
              f"first wind {cond} {a.threshold:g} kn:")
        for name in [m.strip() for m in a.models.split(",") if m.strip()]:
            r = _wa(conn, cfg, _client(conn, cfg), lat, lon, course, sog, at, a.threshold, a.hours, 1.0, a.below,
                    wind_provider=model_provider(name))
            f = r["first"]
            print(f"  {name:8s} " + (f"{f['time_utc']} at {f['lat']},{f['lon']}: {f['wind_kn']} kn from "
                                      f"{f['wind_from_deg']:.0f}" if f else f"not within {a.hours} h"))
        return
    r = wind_ahead(conn, cfg, _client(conn, cfg), lat, lon, course, sog, at, a.threshold, a.hours, 1.0, a.below)
    r["start"] = src
    if a.json:
        _print(r)
        return
    cond = "below" if a.below else "at or above"
    print(f"{a.yacht}: from {lat:.3f},{lon:.3f} at {iso(at)}, {course:.0f} deg at {sog:.1f} kn ({src})")
    f = r["first"]
    print(f"first forecast wind {cond} {a.threshold:g} kn: " +
          (f"{f['time_utc']} at {f['lat']},{f['lon']}: {f['wind_kn']} kn from {f['wind_from_deg']:.0f}"
           if f else f"not within {a.hours} h"))
    for row in r["track"][::3]:
        if row.get("wind_kn") is not None:
            print(f"  {row['time_utc']}  {row['lat']:7.3f} {row['lon']:8.3f}  {row['wind_kn']:4.1f} kn from "
                  f"{row['wind_from_deg']:3.0f}" + (f"  waves {row['wave_m']} m" if row.get('wave_m') is not None else ""))


def cmd_evaluate(cfg, a):
    from .evaluate import evaluate
    from .model import load_observations

    obs = load_observations(_conn(cfg), cfg, _mmsi(cfg, a.yacht))
    print(f"{len(obs)} usable observations, {len({o.voyage for o in obs})} voyages", file=sys.stderr)
    _print(evaluate(cfg, obs, a.method, a.test_frac, a.k))


def cmd_evaluate_live(cfg, a):
    from .evaluate import evaluate_predictions

    _print(evaluate_predictions(_conn(cfg), a.tolerance_min))


def cmd_backtest_leadtime(cfg, a):
    """Score held-out observations using wind forecasts issued N days before (Previous Runs API)."""
    import statistics

    from .geo import along_course_current, wind_angle_to_course
    from .model import SogModel, load_observations
    from .evaluate import splits
    from .weather import FetchError, QuotaExceeded, point_weather

    conn = _conn(cfg)
    client = _client(conn, cfg)
    obs = load_observations(conn, cfg, _mmsi(cfg, a.yacht))
    train, test = next(splits(obs, "chrono", a.test_frac))
    model = SogModel(cfg, train)
    test = test[:: max(1, len(test) // a.max_rows)]
    out = {}
    for days in a.days:
        errs, base, n, nofc = [], [], 0, 0
        for o in test:
            try:
                wx, _, _ = point_weather(conn, client, cfg, o.lat, o.lon, parse_iso(o.time_utc), f"om_prev_day{days}")
            except (FetchError, QuotaExceeded):
                nofc += 1
                continue
            n += 1
            if "wind_speed_kn" not in wx:
                nofc += 1
                continue
            cur = along_course_current(wx.get("current_speed_kn"), wx.get("current_dir_to_deg"), o.course)
            p = model.predict(o.mmsi, wx["wind_speed_kn"], wind_angle_to_course(wx["wind_dir_from_deg"], o.course),
                              wx.get("wave_height_m"), cur)
            if p.status == "ok":
                errs.append(abs(o.sog - p.median))
                base.append(abs(o.sog - model.yacht_median(o.mmsi)))
        out[f"lead_{days}d"] = {"test_rows": len(test), "no_forecast": nofc, "scored": len(errs),
                                "mae_model_kn": round(statistics.mean(errs), 3) if errs else None,
                                "mae_baseline_kn": round(statistics.mean(base), 3) if base else None}
    out["note"] = "wind from forecasts issued N days earlier; waves/currents are archived (marine has no previous runs)"
    _print(out)


def cmd_backtest_passages(cfg, a):
    from .forecast import backtest_voyages
    from .model import load_observations

    conn = _conn(cfg)
    obs = load_observations(conn, cfg, _mmsi(cfg, a.yacht))
    _print(backtest_voyages(conn, cfg, _client(conn, cfg), obs, a.min_hours, a.step_nm, a.max_voyages))


def cmd_import_yb(cfg, a):
    from .collector import Store
    from .yb import YBClient, find_team, import_team, sync_configured

    conn = _conn(cfg)
    client = YBClient()
    if a.list:
        teams = client.teams(a.list)
        q = (a.search or "").upper()
        _print(sorted(t["name"] for t in teams if q in t["name"].upper()))
        return
    if a.race:
        if not a.yacht:
            sys.exit("--race needs --yacht (a configured yacht name or MMSI)")
        mmsi = _mmsi(cfg, a.yacht)
        team = find_team(client.teams(a.race), a.team or a.yacht)
        if not team:
            sys.exit(f"no unique team matching {a.team or a.yacht!r} in {a.race}; use --list {a.race}")
        pts = client.positions(a.race).get(team["id"], [])
        _print({"team": team["name"], **import_team(Store(conn, cfg), a.race, team["id"], mmsi, pts)})
        return
    res = sync_configured(conn, cfg, client)
    db.heartbeat(conn, "yb", res)
    _print(res or "no yachts have yb_race set in config")


def cmd_polar_check(cfg, a):
    """Score recorded fixes against a speed table: speed through the water (SOG minus the
    along-course current) as a share of the polar speed (incl. sea state) for the forecast
    wind at the fix. Builds the evidence for a yacht's polar_factor."""
    import csv
    import statistics

    from .geo import along_course_current, wind_angle_to_course
    from .speedtable import table_speed, wave_factor
    from .weather import FetchError, point_weather

    conn = _conn(cfg)
    mmsi = _mmsi(cfg, a.yacht)
    y = next((y for y in cfg.fleet if y.mmsi == mmsi), None)
    table = a.speed_table or (y.speed_table if y and y.speed_table else "cruiser27")
    client = _client(conn, cfg)
    rows = []
    if a.csv:
        with open(a.csv, newline="") as f:
            fixes = [r for r in csv.DictReader(f) if r["mmsi"].strip() == mmsi]
    else:  # every stored report: AIS, imported screenshot fixes, rally tracker
        fixes = [{"timestamp_utc": r["provider_time_utc"], "lat": r["lat"], "lon": r["lon"], "sog_kn": r["sog_kn"],
                  "cog_deg": r["cog_deg"], "source": r["source"]}
                 for r in conn.execute("SELECT provider_time_utc, lat, lon, sog_kn, cog_deg, source FROM ais_reports "
                                       "WHERE mmsi=? AND sog_kn IS NOT NULL AND cog_deg IS NOT NULL "
                                       "ORDER BY provider_time_utc", (mmsi,))]
        if a.since:
            fixes = [r for r in fixes if r["timestamp_utc"] >= a.since]
    if y is not None and not a.include_approach:  # the last 12 h before arriving: often slowed on purpose
        from .report import arrival_times, in_final_approach

        arr = arrival_times(conn, y)
        n0 = len(fixes)
        fixes = [r for r in fixes if not in_final_approach(parse_iso(r["timestamp_utc"]), arr)]
        if n0 != len(fixes):
            print(f"  ({n0 - len(fixes)} fixes in the last 12 h before arriving left out; --include-approach keeps them)")
    last_t = None
    for r in fixes:
        t_fix = parse_iso(r["timestamp_utc"])
        if last_t is not None and abs((t_fix - last_t).total_seconds()) < 600:
            continue  # same fix from two sources
        last_t = t_fix
        try:
            sog, cog = float(r["sog_kn"]), float(r["cog_deg"])
        except (ValueError, KeyError, TypeError):
            continue
        if sog < a.min_sog:
            continue
        t = parse_iso(r["timestamp_utc"])
        try:
            wx, prov, _ = point_weather(conn, client, cfg, float(r["lat"]), float(r["lon"]), t)
        except FetchError as e:
            rows.append({"time_utc": iso(t), "error": str(e)[:60]})
            continue
        tws, twd = wx.get("wind_speed_kn"), wx.get("wind_dir_from_deg")
        twa = wind_angle_to_course(twd, cog)
        cur = along_course_current(wx.get("current_speed_kn"), wx.get("current_dir_to_deg"), cog) or 0.0
        wf, _ = wave_factor(wx.get("wave_height_m"), wx.get("wave_dir_from_deg"), cog, wx.get("wave_period_s"))
        polar = table_speed(tws, twa, table, 1.0) * wf
        stw = sog - cur
        rows.append({"time_utc": iso(t), "tws_kn": round(tws, 1), "twa_deg": round(twa), "wave_m": wx.get("wave_height_m"),
                     "current_kn": round(cur, 1), "polar_kn": round(polar, 1), "stw_kn": round(stw, 1),
                     "ratio": round(stw / polar, 3) if polar > 0.5 else None, "wind_provider": prov})
    ratios = [x["ratio"] for x in rows if x.get("ratio")]
    summary = {"yacht": a.yacht, "table": table, "fixes_scored": len(ratios),
               "median_ratio": round(statistics.median(ratios), 3) if ratios else None,
               "mean_ratio": round(statistics.mean(ratios), 3) if ratios else None,
               "configured_polar_factor": y.polar_factor if y else None}
    if a.json:
        _print({"summary": summary, "fixes": rows})
        return
    print(f"{a.yacht} vs {table} (100%): speed through water / polar speed incl. sea state")
    for x in rows:
        if "error" in x:
            print(f"  {x['time_utc']}  no wind: {x['error']}")
        else:
            print(f"  {x['time_utc']}  wind {x['tws_kn']:4.1f} kn TWA {x['twa_deg']:3d}  polar {x['polar_kn']:4.1f}  "
                  f"actual {x['stw_kn']:4.1f}  {x['ratio']:.0%}  [{x['wind_provider']}]")
    print(f"  median {summary['median_ratio']:.0%} over {len(ratios)} fixes (configured factor "
          f"{summary['configured_polar_factor']})" if ratios else "  no fixes scored")


def cmd_route(cfg, a):
    """Isochrone-routed ETA (fastest track through the forecast with the yacht's polar)."""
    from .router import route_yacht
    from .speedtable import DESTINATIONS
    from .weather import model_provider

    conn = _conn(cfg)
    mmsi = _mmsi(cfg, a.yacht)
    y = next((y for y in cfg.fleet if y.mmsi == mmsi), None)
    if y is None:
        sys.exit("yacht not in config")
    if a.speed_table:
        y.speed_table = a.speed_table
    if a.polar_factor is not None:
        y.polar_factor = a.polar_factor
    if not y.speed_table:
        sys.exit("this yacht has no speed_table; the router needs a polar (e.g. --speed-table cruiser27)")
    from .speedtable import parse_destination

    try:
        to, dest = parse_destination(a.to or y.destination)
    except KeyError as e:
        sys.exit(str(e))
    lat, lon, at, _, _, src = _start_point(conn, a, mmsi)
    client = _client(conn, cfg)
    models = [m.strip() for m in a.models.split(",")] if a.models else [a.provider]
    results = []
    for m in models:
        prov = "metno" if m == "metno" else ("blend" if m in ("blend", "combined") else model_provider(m))
        mot = {}
        if a.motor_below is not None:
            mot = {"motor_wind_below_kn": a.motor_below, "motor_speed_kn": a.motor_speed}
        if a.motor_max_h is not None:
            mot["motor_max_h"] = a.motor_max_h
        r = route_yacht(conn, cfg, client, y, (lat, lon), at, dest, prov, res=a.grid_res,
                        step_h=a.step_h, heading_step=a.heading_step, maneuver_min=a.gybe_loss_min, **mot)
        if r.get("status") == "ok" and r.get("hours_beyond_forecast", 0) > 0:
            r["note"] = f"forecast ends {r['forecast_ends_utc']}; last {r['hours_beyond_forecast']} h assume it holds"
        r["model"] = m
        results.append(r)
    if a.json:
        _print({"yacht": y.name, "to": to, "start": src, "routes": results})
        return
    print(f"{y.name} -> {to}: routed from {lat:.3f},{lon:.3f} at {iso(at)} ({src}); "
          f"{y.speed_table} x{y.polar_factor:g}, sail only, {a.gybe_loss_min:g} min per tack/gybe")
    print(f"  {'model':8s} {'ETA (UTC)':17s} {'hours':>6s} {'sailed nm':>9s} {'direct nm':>9s} {'avg kn':>6s} {'gybes/tacks':>11s}")
    for r in results:
        if r["status"] != "ok":
            print(f"  {r['model']:8s} {r['status']} (grid {r.get('grid')})"
                  + (f"\n           reason: {r['error']}" if r.get("error") else "")
                  + (f"\n           diagnostics: {r['diagnostics']}" if r.get("diagnostics") else ""))
            continue
        print(f"  {r['model']:8s} {parse_iso(r['eta_utc']):%a %d %H:%MZ}   {r['hours']:6.1f} {r['sailed_nm']:9.1f} "
              f"{r['direct_nm']:9.1f} {r['avg_sog_kn']:6.1f} {r['maneuvers']:11d}"
              + (f"  motor {r['motor_hours']:g} h" if r.get("motor_hours") else "")
              + (f"   ({r['note']})" if r.get("note") else ""))
    ok = [r for r in results if r["status"] == "ok"]
    for r in results:
        cu = r.get("course_under_models")
        if cu:
            print(f"  {r['model']} course sailed through each model's weather:")
            for e in cu:
                print(f"    {e['model']:8s} " + (f"{parse_iso(e['eta_utc']):%a %d %H:%MZ}  {e['hours']:6.1f} h"
                                                 if e.get("eta_utc") else str(e.get("status", "n/a"))))
            if r.get("course_eta_median_utc"):
                lo, hi = r["course_eta_range_utc"]
                print(f"  ETA (median of the course under {len([e for e in cu if e.get('eta_utc')])} models): "
                      f"{parse_iso(r['course_eta_median_utc']):%a %d %H:%MZ}  "
                      f"(range {parse_iso(lo):%a %d %H:%MZ} to {parse_iso(hi):%a %d %H:%MZ})")
    if len(ok) > 1 and not any(r.get("course_under_models") for r in ok):
        etas = sorted(parse_iso(r["eta_utc"]) for r in ok)
        print(f"  spread {etas[0]:%a %d %H:%MZ} to {etas[-1]:%a %d %H:%MZ}")
    if a.track and ok:
        r = ok[0]
        print(f"  track ({r['model']}), every {a.track_every:g} h:")
        last = None
        for p in r["track"]:
            t = parse_iso(p["time_utc"])
            if last is None or (t - last).total_seconds() >= a.track_every * 3600 - 60 or p is r["track"][-1]:
                print(f"    {t:%a %d %H:%MZ} {p['lat']:7.3f} {p['lon']:8.3f}  hdg {p['heading'] if p['heading'] is not None else '-':>3}"
                      f"  wind {p['tws_kn'] if p['tws_kn'] is not None else '-':>4} TWA {p['twa_deg'] if p['twa_deg'] is not None else '-':>3}"
                      f"  SOG {p['sog_kn'] if p['sog_kn'] is not None else '-'}")
                last = t


def cmd_skill(cfg, a):
    """Option 1: verify each model's past wind forecasts at the fleet's fixes (server, daily)."""
    from . import skill

    conn = _conn(cfg)
    if a.corridor:
        from .speedtable import parse_destination

        frm, to = a.corridor.split(":")
        pts = skill.corridor_points(parse_destination(frm)[1], parse_destination(to)[1], n=a.points)
        print(skill.verify_corridor(conn, _client(conn, cfg), pts, days=a.corridor_days,
                                    progress=lambda m: print(" ", m, flush=True)))
    elif not a.show:
        for st in skill.run_verification(conn, _client(conn, cfg), cfg, back_days=a.days):
            print(st)
    sc = skill.scores(conn, cfg.skill_window_days)
    from .weather import MODELS

    w = skill.Weights(conn, list(MODELS), cfg.skill_window_days, cfg.skill_min_checks, cfg.skill_weighting)
    print(f"wind skill, last {cfg.skill_window_days} d (vector error vs consensus analysis):")
    for n in skill.LEAD_DAYS:
        row = "  ".join(f"{m} {sc[n][m][1]:.2f} kn/{sc[n][m][0]}" if m in sc[n] else f"{m} -" for m in MODELS)
        wt = "  ".join(f"{m} {w.by_day[n][m]:.0%}" for m in MODELS)
        print(f"  day {n}: {row}\n         weights: {wt}")
    for r in skill.eta_skill(conn):
        print(f"arrival {r['mmsi']} {r['destination']} {r['arrived_utc']}: ETA mean abs error h (n) {r['mae_h']}")


def cmd_climate_route(cfg, a):
    """Passage odds from past weather (ERA5): route the yacht through the same dates in past years."""
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _P

    from . import climate
    from .speedtable import parse_destination

    conn = _conn(cfg)
    mmsi = _mmsi(cfg, a.yacht)
    y = next((y for y in cfg.fleet if y.mmsi == mmsi), None)
    if y is None or not y.speed_table:
        sys.exit("yacht not in config, or it has no speed_table (polar)")
    if a.polar_factor is not None:
        y.polar_factor = a.polar_factor
    _, start = parse_destination(a.frm)
    _, dest = parse_destination(a.to)
    depart = _dt.fromisoformat(a.depart).astimezone(_tz.utc)
    deadline = _dt.fromisoformat(a.by).astimezone(_tz.utc)
    years = _years(a.years)
    fuel_note = None
    if a.motor_max_h is None and a.fuel_l:
        # usable fuel after the reserve and the generator, at the motoring burn rate
        allow_days = (deadline - depart).total_seconds() / 86400
        usable = a.fuel_l * (1 - a.fuel_reserve) - a.genset_lph * 24 * allow_days
        a.motor_max_h = max(0.0, round(usable / a.burn_lph))
        fuel_note = (f"{a.fuel_l:g} L, {round(a.fuel_reserve * 100)}% reserve, generator {a.genset_lph:g} L/h, "
                     f"{a.burn_lph:g} L/h motoring")
    out_dir = _P(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"climate_{a.name}"
    out = climate.run(conn, cfg, y, start, dest, depart, deadline, years, res=a.res,
                      step_h=a.step_h, motor_wind_below_kn=a.motor_below, motor_speed_kn=a.motor_speed,
                      motor_max_h=a.motor_max_h, maneuver_min=a.gybe_loss_min, per_day=a.max_calls_per_day,
                      wait=not a.no_wait, out_path=str(base) + ".json", name=a.name,
                      forecast_models=[m.strip() for m in a.forecast_models.split(",") if m.strip()],
                      fade=(a.fade_from, a.fade_to), enso_sigma=a.enso_sigma, currents=not a.no_current,
                      fuel_note=fuel_note, per_model=not a.no_per_model, waves=not a.no_waves,
                      real_waves=not a.wind_waves_only, forecast_only=a.forecast_only)
    (_P(str(base) + ".md")).write_text(climate.markdown(out))
    climate.tracks_jpeg(str(base) + ".jpg", out)
    print(climate.markdown(out))


def _years(spec):
    """'2006-2025', '1997', or a mix: '1997,2006-2025'."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = (int(x) for x in part.split("-"))
            out += list(range(a, b + 1))
        elif part:
            out.append(int(part))
    return sorted(set(out))


def cmd_climate_fetch(cfg, a):
    """Download passage history on another machine (its own API allowance), for climate-merge."""
    from datetime import datetime as _dt, timezone as _tz

    from . import climate
    from .speedtable import parse_destination

    conn = db.connect(a.db)
    _, start = parse_destination(a.frm)
    _, dest = parse_destination(a.to)
    years = _years(a.years)
    climate.fetch_only(conn, start, dest, _dt.fromisoformat(a.depart).astimezone(_tz.utc), years,
                       res=a.res, waves=not a.no_waves, wind=not a.waves_only, per_day=a.max_calls_per_day,
                       progress=lambda m: print(m, flush=True))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print(f"saved to {a.db}: copy it to the server and run `yachtspeed climate-merge <file>` there")


def cmd_strategies(cfg, a):
    """Compare routing strategies through the live forecast and past years (history must be stored)."""
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _P

    from . import climate
    from .speedtable import parse_destination

    conn = _conn(cfg)
    mmsi = _mmsi(cfg, a.yacht)
    y = next((y for y in cfg.fleet if y.mmsi == mmsi), None)
    if y is None or not y.speed_table:
        sys.exit("yacht not in config, or it has no speed_table (polar)")
    if a.polar_factor is not None:
        y.polar_factor = a.polar_factor
    _, start = parse_destination(a.frm)
    _, dest = parse_destination(a.to)
    depart = _dt.fromisoformat(a.depart).astimezone(_tz.utc)
    deadline = _dt.fromisoformat(a.by).astimezone(_tz.utc)
    years = _years(a.years)
    motor_max_h = a.motor_max_h
    if motor_max_h is None and a.fuel_l:
        usable = a.fuel_l * (1 - a.fuel_reserve) - a.genset_lph * 24 * (deadline - depart).total_seconds() / 86400
        motor_max_h = max(0.0, round(usable / a.burn_lph))
    strategies = dict(climate.DEFAULT_STRATEGIES)
    if a.via:
        la, lo = (float(x) for x in a.via.split(","))
        strategies["B"] = {"label": f"classic: south-west to {la:g}N {abs(lo):g}W first", "via": (la, lo)}
    _P(a.out_dir).mkdir(parents=True, exist_ok=True)
    out = climate.run_strategies(conn, cfg, y, start, dest, depart, deadline, years,
                                 motor_wind_below_kn=a.motor_below, motor_speed_kn=a.motor_speed,
                                 motor_max_h=motor_max_h, strategies=strategies, burn_lph=a.burn_lph,
                                 fade=(a.fade_from, a.fade_to),
                                 out_path=str(_P(a.out_dir) / f"climate_{a.name}.json"))
    print(climate.strategies_markdown(out))


def cmd_import_gpx(cfg, a):
    from . import history

    conn = _conn(cfg)
    mmsi = _mmsi(cfg, a.yacht)
    print(history.import_gpx(conn, a.file, mmsi, every_s=a.every, source=a.source,
                             progress=lambda m: print(" ", m, flush=True)))


def cmd_history_jobs(cfg, a):
    """Write the weather the track history still needs, for history-fetch on another machine."""
    from . import history

    conn = _conn(cfg)
    jobs, ncells = history.needed_jobs(conn, _mmsi(cfg, a.yacht), a.since)
    with open(a.out, "w") as f:
        json.dump(jobs, f)
    print(f"{len(jobs)} cell-months over {ncells} cells written to {a.out}")


def cmd_history_fetch(cfg, a):
    """Fetch a jobs file's weather into a database file (run where there is API allowance), then
    climate-merge it on the server."""
    from . import history
    from .weather import Budget, OpenMeteo, QuotaExceeded

    conn = db.connect(a.db)
    with open(a.jobs) as f:
        jobs = json.load(f)
    client = OpenMeteo(Budget(conn, 150, 2500, a.max_calls_per_day))
    print(f"{len(jobs)} cell-months to fetch", flush=True)
    try:
        history.fetch_jobs(conn, client, jobs, progress=lambda m: print(" ", m, flush=True))
    except QuotaExceeded as e:
        print(f"stopped at the API allowance ({e}); copy what is done, or rerun later to continue", flush=True)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print(f"saved to {a.db}: copy it to the server and run `yachtspeed climate-merge <file>` there")


def cmd_polar_history(cfg, a):
    """The yacht's imported track history against her polar, with historical wind, waves and current."""
    from pathlib import Path as _P

    from . import history
    from .weather import Budget, OpenMeteo, QuotaExceeded

    conn = _conn(cfg)
    mmsi = _mmsi(cfg, a.yacht)
    y = next((y for y in cfg.fleet if y.mmsi == mmsi), None)
    if y is None or not y.speed_table:
        sys.exit("yacht not in config, or it has no speed_table")
    if a.speed_table:
        y.speed_table = a.speed_table
    if not a.no_fetch:
        client = OpenMeteo(Budget(conn, 120, 1000, a.max_calls_per_day))
        while True:
            try:
                history.fetch_weather(conn, client, mmsi, since=a.since, progress=lambda m: print(" ", m, flush=True))
                break
            except QuotaExceeded as e:
                wait = (e.resume_at - utcnow()).total_seconds()
                print(f"  weather allowance reached; waiting until {iso(e.resume_at)}", flush=True)
                if wait > 26 * 3600:
                    sys.exit("stopped: rerun later to resume")
                import time as _time

                _time.sleep(max(5, wait + 5))
    res = history.analyse(conn, y, since=a.since, min_tws=a.min_tws, min_twa=a.min_twa)
    md = history.markdown(res)
    out = _P(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    slug = "".join(c if c.isalnum() else "_" for c in y.name.lower())
    (out / f"climate_{slug}_polar_history.json").write_text(json.dumps(res, indent=1, default=str))
    (out / f"climate_{slug}_polar_history.md").write_text(md)
    print(md)


def cmd_forecast_maps(cfg, a):
    """Forecast wind maps every few hours for the first days of a passage, plus the assumptions text."""
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _P

    from . import fcmaps
    from .speedtable import parse_destination

    conn = _conn(cfg)
    _, start = parse_destination(a.frm)
    depart = _dt.fromisoformat(a.depart).astimezone(_tz.utc)
    info = fcmaps.build(conn, cfg, _client(conn, cfg), start, depart, a.out_dir, a.name, days=a.days,
                        step_h=a.step_h, res=a.res or (1.0 if a.days <= 4 else 2.5), strategies_json=a.strategies,
                        keys=tuple(k.strip() for k in a.options.split(",") if k.strip()))
    md = fcmaps.assumptions_md(info, cfg, a.strategies, a.climate)
    (_P(a.out_dir) / f"climate_{a.name}_assumptions.md").write_text(md)
    print(json.dumps({k: v for k, v in info.items() if k != "weights"}, indent=1))
    print(md)


def cmd_climate_merge(cfg, a):
    from . import climate

    print(climate.merge(_conn(cfg), a.file))


def cmd_screenshots(cfg, a):
    from . import screenshots

    conn = _conn(cfg)
    if a.review:
        for r in screenshots.review_list(conn, a.hours):
            print(f"  {r['file']}: {r['status']} ({r['yacht'] or '?'}) {r['notes'] or ''}")
        return
    if a.reprocess:
        conn.execute("DELETE FROM ocr_files WHERE status != 'ok'")
    if a.reread_hours:  # re-read recent screenshots too (positions already stored are skipped as duplicates)
        from .geo import iso as _iso, utcnow as _now
        from datetime import timedelta as _td

        conn.execute("DELETE FROM ocr_files WHERE processed_utc >= ?", (_iso(_now() - _td(hours=a.reread_hours)),))
    res = screenshots.run(conn, cfg)
    db.heartbeat(conn, "screenshots", res)
    _print(res)


def cmd_destinations(cfg, a):
    from .speedtable import DESTINATIONS

    for k, v in sorted(DESTINATIONS.items()):
        print(f"  {k:18s} {v['point'][0]:8.3f} {v['point'][1]:9.3f}  {v['name']}")


def cmd_fetch_land(cfg, a):
    from .land import fetch_world_10m

    print(f"saved {fetch_world_10m()} (worldwide 1:10m land; the router now uses it)")


def cmd_report(cfg, a):
    from . import report

    conn = _conn(cfg)
    rep = report.build(conn, cfg, [m.strip() for m in a.models.split(",")] if a.models else None)
    if a.out:
        for f in report.write(rep, a.out):
            print(f)
    else:
        print(report.markdown(rep))


def cmd_backup(cfg, a):
    from .backup import backup

    print(backup(_conn(cfg), cfg.db_path, a.dest or cfg.backup_dir, a.keep or cfg.backup_keep))


def cmd_restore(cfg, a):
    from .backup import restore

    restore(a.archive, cfg.db_path)
    print(f"restored {a.archive} -> {cfg.db_path} (previous files kept as *.pre-restore)")


def cmd_smoke(cfg, a):
    """End-to-end check on synthetic data in a throwaway database."""
    import os
    import tempfile

    from .evaluate import evaluate
    from .model import SogModel, load_observations
    from .synthetic import generate

    cfg.db_path = os.path.join(tempfile.mkdtemp(), "smoke.db")
    conn = db.connect(cfg.db_path)
    n = generate(conn)
    obs = load_observations(conn, cfg, "999000001")
    rep = evaluate(cfg, obs, "chrono")
    m = SogModel(cfg, obs)
    _print({"generated": n, "usable": len(obs), "evaluation": rep,
            "example_beam_reach_15kn": m.predict("999000001", 15, 90, 1.2, 0.0).__dict__,
            "example_no_support": m.predict("999000001", 45, 10, 6.0, 0.0).status})


def build_parser():
    p = argparse.ArgumentParser(prog="yachtspeed", description=__doc__)
    p.add_argument("--config", help="config.toml (default /etc/yachtspeed/config.toml or ./config.toml)")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--plain-logs", action="store_true", help="human-readable logs instead of JSON")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("collect", help="run the AISStream collector service").set_defaults(fn=cmd_collect)
    s = sub.add_parser("enrich", help="run the weather enrichment service")
    s.add_argument("--once", action="store_true")
    s.add_argument("--retry-failed", action="store_true", help="reset failed fetches before running")
    s.add_argument("--backfill-since", help="ISO date: enrich everything since then, then exit")
    s.set_defaults(fn=cmd_enrich)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("discover", help="find MMSIs broadcasting a name (for verification)")
    s.add_argument("name")
    s.add_argument("--minutes", type=float, default=30)
    s.set_defaults(fn=cmd_discover)
    s = sub.add_parser("check-stream", help="test the AISStream key and connection")
    s.add_argument("--seconds", type=float, default=30)
    s.add_argument("--all", action="store_true", help="no MMSI filter: any vessel in the boxes")
    s.add_argument("--box", metavar="LAT1,LON1,LAT2,LON2",
                   help="test coverage in this area only (implies --all), e.g. 32,-10,35,-7 off Casablanca")
    s.set_defaults(fn=cmd_check_stream)
    s = sub.add_parser("samples", help="show stored raw AIS payloads next to the parsed values")
    s.add_argument("--limit", type=int, default=5)
    s.set_defaults(fn=cmd_samples)
    s = sub.add_parser("add-fix", help="record a position you read yourself (MarineTraffic app, crew, ...)")
    s.add_argument("--yacht", required=True)
    s.add_argument("--pos", required=True, metavar="LAT,LON", help="decimal degrees, e.g. 33.62,-8.38")
    s.add_argument("--time", help="ISO UTC (2026-11-14T18:03Z) or '6m ago' / '2h ago' (default now)")
    s.add_argument("--sog", type=float, help="knots")
    s.add_argument("--cog", type=float, help="degrees true")
    s.add_argument("--source", default="marinetraffic", help="label stored with the fix")
    s.set_defaults(fn=cmd_add_fix)
    s = sub.add_parser("import-csv")
    s.add_argument("path")
    s.add_argument("--label")
    s.set_defaults(fn=cmd_import)
    s = sub.add_parser("export")
    s.add_argument("out")
    s.add_argument("--which", choices=["enriched", "unmatched", "all"], default="enriched")
    s.add_argument("--yacht")
    s.add_argument("--since")
    s.add_argument("--until")
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser("forecast", help="SOG forecast at a future time and place")
    s.add_argument("--yacht", required=True, help="name or MMSI")
    s.add_argument("--lat", type=float, required=True)
    s.add_argument("--lon", type=float, required=True)
    s.add_argument("--time", required=True, help="ISO UTC valid time")
    s.add_argument("--course", type=float, required=True, help="intended course, degrees true")
    s.add_argument("--distance", type=float, help="nm; also report hours at the forecast speed")
    s.add_argument("--wind-provider", help="override, e.g. om_hist_forecast or om_prev_day2")
    s.add_argument("--no-store", action="store_true")
    s.set_defaults(fn=cmd_forecast)
    def start_args(sp):
        sp.add_argument("--from", dest="from_", metavar="LAT,LON", help="start position (default: latest report)")
        sp.add_argument("--at", help="time of --from position, ISO UTC (default now)")
        sp.add_argument("--course", type=float, help="degrees true (default: latest report COG)")
        sp.add_argument("--speed", type=float, help="knots (default: latest report SOG)")
        sp.add_argument("--json", action="store_true")

    s = sub.add_parser("passage", help="passage ETA: --to NAME from the latest position, or explicit waypoints")
    s.add_argument("--yacht", required=True)
    s.add_argument("--to", help="named destination, e.g. rubicon, las-palmas")
    s.add_argument("--depart", help="ISO UTC departure (default: now / time of --from)")
    s.add_argument("--step-nm", type=float, default=10.0)
    s.add_argument("--speed-table", help="fallback polar when history is thin: cruiser27, oyster885gt_orc, "
                   "or 'none' (default: the yacht's speed_table in config, else cruiser27)")
    s.add_argument("--polar-factor", type=float, help="scale the speed table, e.g. 0.85 (default: config, else 1)")
    s.add_argument("--no-current", action="store_true", help="ignore ocean current")
    s.add_argument("--no-waves", action="store_true", help="ignore sea state (wave height/direction)")
    s.add_argument("--gybe-loss-min", type=float, default=3.0, help="minutes lost per gybe/tack (default 3)")
    s.add_argument("--gybe-every-h", type=float, default=4.0,
                   help="hours between gybes/tacks when the course needs them (default 4; 0 = ignore)")
    s.add_argument("--motor-below", type=float, help="motor when sailing speed is below this (default: never)")
    s.add_argument("--motor-speed", type=float, help="motoring speed (default: config, else 7)")
    s.add_argument("--no-motor", action="store_true", help="ignore the yacht's configured motoring")
    s.add_argument("--wind-provider")
    s.add_argument("--models", help="compare forecast models, e.g. ecmwf,aifs,gfs,icon (shows the ETA spread)")
    s.add_argument("--steps", action="store_true", help="show every step")
    s.add_argument("--no-store", action="store_true")
    s.add_argument("waypoints", nargs="*", help="lat,lon ... (with --to: extra points before the destination)")
    start_args(s)
    s.set_defaults(fn=cmd_passage)
    s = sub.add_parser("wind-ahead", help="when will the yacht reach a given wind strength on its current course")
    s.add_argument("--yacht", required=True)
    s.add_argument("--threshold", type=float, default=10.0)
    s.add_argument("--below", action="store_true", help="find when wind drops below the threshold instead")
    s.add_argument("--hours", type=int, default=72)
    s.add_argument("--models", help="compare forecast models, e.g. ecmwf,aifs,gfs,icon")
    start_args(s)
    s.set_defaults(fn=cmd_wind_ahead)
    s = sub.add_parser("evaluate", help="hold-out evaluation vs per-yacht median")
    s.add_argument("--yacht")
    s.add_argument("--method", choices=["chrono", "voyage"], default="chrono")
    s.add_argument("--test-frac", type=float, default=0.25)
    s.add_argument("-k", type=int, default=5)
    s.set_defaults(fn=cmd_evaluate)
    s = sub.add_parser("evaluate-live", help="score stored live predictions by lead time")
    s.add_argument("--tolerance-min", type=int, default=10)
    s.set_defaults(fn=cmd_evaluate_live)
    s = sub.add_parser("backtest-leadtime", help="hold-out scoring with N-day-old wind forecasts")
    s.add_argument("--yacht")
    s.add_argument("--days", type=int, nargs="+", default=[1, 3, 5])
    s.add_argument("--test-frac", type=float, default=0.25)
    s.add_argument("--max-rows", type=int, default=200)
    s.set_defaults(fn=cmd_backtest_leadtime)
    s = sub.add_parser("backtest-passages", help="predicted vs actual voyage durations")
    s.add_argument("--yacht")
    s.add_argument("--min-hours", type=float, default=6)
    s.add_argument("--step-nm", type=float, default=10)
    s.add_argument("--max-voyages", type=int, default=20)
    s.set_defaults(fn=cmd_backtest_passages)
    s = sub.add_parser("import-yb", help="import YB Tracking (rally satellite tracker) positions")
    s.add_argument("--list", metavar="RACE", help="list team names on a race, e.g. arc2026")
    s.add_argument("--search", help="with --list: filter names")
    s.add_argument("--race", help="one-off import from this race (default: all yachts with yb_race)")
    s.add_argument("--yacht", help="with --race: configured yacht to attach positions to")
    s.add_argument("--team", help="with --race: team name on the tracker if different")
    s.set_defaults(fn=cmd_import_yb)
    s = sub.add_parser("polar-check", help="score recorded fixes against the polar (evidence for polar_factor)")
    s.add_argument("--yacht", required=True)
    s.add_argument("--csv", help="score fixes from this CSV instead of the database")
    s.add_argument("--since", help="ISO date: only fixes from this time (database mode)")
    s.add_argument("--speed-table")
    s.add_argument("--min-sog", type=float, default=2.0)
    s.add_argument("--include-approach", action="store_true",
                   help="keep fixes from the last 12 h before arriving (left out by default)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_polar_check)
    s = sub.add_parser("route", help="isochrone-routed ETA: fastest track through the forecast using the polar")
    s.add_argument("--yacht", required=True)
    s.add_argument("--to", help="named destination (see `yachtspeed destinations`) or LAT,LON "
                                "(default: the yacht's configured destination)")
    s.add_argument("--models", help="one route per forecast, e.g. blend,ecmwf,aifs,gfs,icon")
    s.add_argument("--provider", default="blend",
                   help="forecast if --models not given: blend (average of ecmwf, aifs, gfs, icon; default), "
                        "a single model, or 'metno'")
    s.add_argument("--speed-table")
    s.add_argument("--polar-factor", type=float)
    s.add_argument("--gybe-loss-min", type=float, default=3.0)
    s.add_argument("--step-h", type=float, help="isochrone time step in hours (default: by passage length)")
    s.add_argument("--heading-step", type=int, default=5, help="degrees between headings tried")
    s.add_argument("--grid-res", type=float, help="forecast grid spacing in degrees (default: by passage size)")
    s.add_argument("--track", action="store_true", help="print the routed track")
    s.add_argument("--track-every", type=float, default=3.0)
    s.add_argument("--motor-below", type=float, help="motor when true wind is below this (kn)")
    s.add_argument("--motor-speed", type=float, default=8.0)
    s.add_argument("--motor-max-h", type=float, help="fuel: most hours of motoring")
    start_args(s)
    s.set_defaults(fn=cmd_route)
    s = sub.add_parser("skill", help="verify the models' past wind forecasts; prints skill and blend weights")
    s.add_argument("--days", type=int, default=14, help="verify up to this many past days not yet done")
    s.add_argument("--show", action="store_true", help="only print scores and weights (no API calls)")
    s.add_argument("--corridor", help="verify along a passage instead of at fleet fixes, e.g. rubicon:english-harbour")
    s.add_argument("--corridor-days", type=int, default=30, help="how many past days to verify along the corridor")
    s.add_argument("--points", type=int, default=12, help="points along the corridor")
    s.set_defaults(fn=cmd_skill)
    s = sub.add_parser("climate-fetch", help="download passage history into a file (run on another machine)")
    s.add_argument("--from", dest="frm", required=True)
    s.add_argument("--to", required=True)
    s.add_argument("--depart", required=True)
    s.add_argument("--years", default="2006-2025")
    s.add_argument("--res", type=float, default=2.5)
    s.add_argument("--db", default="climate.db", help="output file")
    s.add_argument("--no-waves", action="store_true")
    s.add_argument("--waves-only", action="store_true", help="only the ERA5 waves (wind already on the server)")
    s.add_argument("--max-calls-per-day", type=int, default=3000)
    s.set_defaults(fn=cmd_climate_fetch)
    s = sub.add_parser("strategies", help="compare routing strategies (fastest / classic south / fuel-saving)")
    s.add_argument("--yacht", required=True)
    s.add_argument("--from", dest="frm", required=True)
    s.add_argument("--to", required=True)
    s.add_argument("--depart", required=True)
    s.add_argument("--by", required=True)
    s.add_argument("--years", default="2006-2025")
    s.add_argument("--via", help="waypoint for the classic route, LAT,LON (default 22,-30)")
    s.add_argument("--motor-below", type=float, default=None)
    s.add_argument("--motor-speed", type=float, default=8.0)
    s.add_argument("--motor-max-h", type=float, default=None)
    s.add_argument("--fuel-l", type=float, default=None)
    s.add_argument("--burn-lph", type=float, default=25.0)
    s.add_argument("--genset-lph", type=float, default=2.0)
    s.add_argument("--fuel-reserve", type=float, default=0.10)
    s.add_argument("--polar-factor", type=float, default=None)
    s.add_argument("--fade-from", type=float, default=2.0, help="forecast weight starts fading (days after issue)")
    s.add_argument("--fade-to", type=float, default=15.0, help="history only from this many days after issue")
    s.add_argument("--name", default="strategies")
    s.add_argument("--out-dir", default="/var/lib/yachtspeed/report")
    s.set_defaults(fn=cmd_strategies)
    s = sub.add_parser("import-gpx", help="import a plotter/TimeZero GPX track history (any size) for polar analysis")
    s.add_argument("file")
    s.add_argument("--yacht", required=True)
    s.add_argument("--every", type=int, default=60, help="keep one point per this many seconds")
    s.add_argument("--source", default="gpx")
    s.set_defaults(fn=cmd_import_gpx)
    s = sub.add_parser("history-jobs", help="list the weather the track history still needs (for history-fetch)")
    s.add_argument("--yacht", required=True)
    s.add_argument("--since")
    s.add_argument("--out", default="/tmp/history_jobs.json")
    s.set_defaults(fn=cmd_history_jobs)
    s = sub.add_parser("history-fetch", help="fetch a history-jobs file's weather into a database file (another machine)")
    s.add_argument("--jobs", required=True)
    s.add_argument("--db", default="history.db")
    s.add_argument("--max-calls-per-day", type=int, default=3000)
    s.set_defaults(fn=cmd_history_fetch)
    s = sub.add_parser("polar-history", help="the imported track history against the polar (historical weather)")
    s.add_argument("--yacht", required=True)
    s.add_argument("--since", help="only from this date (YYYY-MM-DD)")
    s.add_argument("--speed-table")
    s.add_argument("--min-tws", type=float, default=8.0)
    s.add_argument("--min-twa", type=float, default=40.0)
    s.add_argument("--no-fetch", action="store_true", help="use only the weather already stored")
    s.add_argument("--max-calls-per-day", type=int, default=1500)
    s.add_argument("--out-dir", default="/var/lib/yachtspeed/report")
    s.set_defaults(fn=cmd_polar_history)
    s = sub.add_parser("forecast-maps", help="forecast wind maps every few hours for the first days, plus assumptions")
    s.add_argument("--from", dest="frm", required=True)
    s.add_argument("--depart", required=True)
    s.add_argument("--days", type=int, default=4)
    s.add_argument("--step-h", type=int, default=3)
    s.add_argument("--res", type=float, default=None, help="grid degrees (default 1 up to 4 days, else 2.5)")
    s.add_argument("--strategies", default="/var/lib/yachtspeed/report/climate_alibi_strategies.json")
    s.add_argument("--climate", default="/var/lib/yachtspeed/report/climate_alibi_antigua.json")
    s.add_argument("--options", default="A,B,C,D", help="which options' routes to draw")
    s.add_argument("--name", default="alibi_forecast")
    s.add_argument("--out-dir", default="/var/lib/yachtspeed/report")
    s.set_defaults(fn=cmd_forecast_maps)
    s = sub.add_parser("climate-merge", help="add a climate-fetch file's history to this server's store")
    s.add_argument("file")
    s.set_defaults(fn=cmd_climate_merge)
    s = sub.add_parser("climate-route", help="passage odds: route through the same dates in past years (ERA5)")
    s.add_argument("--yacht", required=True)
    s.add_argument("--from", dest="frm", required=True, help="named place or lat,lon")
    s.add_argument("--to", required=True, help="named place or lat,lon")
    s.add_argument("--depart", required=True, help="e.g. 2026-11-20T10:00+00:00 (month/day/time reused each year)")
    s.add_argument("--by", required=True, help="deadline, e.g. 2026-12-06T23:59-04:00 (Antigua local)")
    s.add_argument("--years", default="2006-2025")
    s.add_argument("--res", type=float, default=2.5, help="grid spacing in degrees")
    s.add_argument("--step-h", type=float, default=4.0)
    s.add_argument("--motor-below", type=float, default=None, help="motor when true wind is below this (kn)")
    s.add_argument("--motor-speed", type=float, default=8.0)
    s.add_argument("--motor-max-h", type=float, default=None, help="fuel: most hours of motoring")
    s.add_argument("--polar-factor", type=float, default=None)
    s.add_argument("--gybe-loss-min", type=float, default=3.0)
    s.add_argument("--max-calls-per-day", type=int, default=1500,
                   help="archive API calls per day (Open-Meteo counts each as ~3; leave room for the reports)")
    s.add_argument("--name", default="passage")
    s.add_argument("--out-dir", default="/var/lib/yachtspeed/report")
    s.add_argument("--no-wait", action="store_true", help="stop at the API's daily limit instead of waiting")
    s.add_argument("--forecast-models", default="ecmwf,aifs,gfs,icon",
                   help="live forecast for the first part of the passage ('' = past weather only)")
    s.add_argument("--fade-from", type=float, default=2.0,
                   help="forecast weight starts fading this many days after it was issued")
    s.add_argument("--fade-to", type=float, default=15.0,
                   help="...and reaches zero (history only) this many days after it was issued")
    s.add_argument("--enso-sigma", type=float, default=1.0, help="El Nino weighting width (ONI units)")
    s.add_argument("--fuel-l", type=float, default=None, help="fuel on board (litres); sets --motor-max-h")
    s.add_argument("--burn-lph", type=float, default=25.0, help="litres per hour when motoring")
    s.add_argument("--genset-lph", type=float, default=2.0, help="average generator use over the passage")
    s.add_argument("--fuel-reserve", type=float, default=0.10, help="share of fuel kept in reserve")
    s.add_argument("--no-current", action="store_true", help="leave out the ocean current")
    s.add_argument("--no-per-model", action="store_true", help="skip the runs with each model's own forecast")
    s.add_argument("--no-waves", action="store_true", help="leave out the sea state (surfing gain, sea losses)")
    s.add_argument("--forecast-only", action="store_true",
                   help="run only the live-forecast part, on the years of history already stored (no history downloads)")
    s.add_argument("--wind-waves-only", action="store_true",
                   help="derive the sea from the wind only (no ERA5 wave downloads)")
    s.set_defaults(fn=cmd_climate_route)
    s = sub.add_parser("screenshots", help="read new MarineTraffic screenshots (Dropbox folder) by OCR")
    s.add_argument("--reread-hours", type=float, default=0,
                   help="also re-read screenshots processed in the last N hours (e.g. to pick up port calls)")
    s.add_argument("--review", action="store_true", help="list screenshots that could not be read")
    s.add_argument("--hours", type=int, default=48)
    s.add_argument("--reprocess", action="store_true", help="retry screenshots that failed before")
    s.set_defaults(fn=cmd_screenshots)
    sub.add_parser("destinations", help="list named destinations").set_defaults(fn=cmd_destinations)
    sub.add_parser("fetch-land", help="download worldwide 1:10m land for more detailed routing (~10 MB)").set_defaults(
        fn=cmd_fetch_land)
    s = sub.add_parser("report", help="status + multi-model ETAs + polar evidence (JSON and Markdown)")
    s.add_argument("--out", help="write report.json and report.md to this directory (default: print Markdown)")
    s.add_argument("--models", help="default ecmwf,aifs,gfs,icon")
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("backup")
    s.add_argument("--dest")
    s.add_argument("--keep", type=int)
    s.set_defaults(fn=cmd_backup)
    s = sub.add_parser("restore", help="restore a backup (stop services first)")
    s.add_argument("archive")
    s.set_defaults(fn=cmd_restore)
    sub.add_parser("smoke-test", help="synthetic end-to-end check").set_defaults(fn=cmd_smoke)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    logutil.setup(a.log_level, json_logs=not a.plain_logs)
    cfg = load_config(a.config)
    a.fn(cfg, a)


if __name__ == "__main__":
    main()
