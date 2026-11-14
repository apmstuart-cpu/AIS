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

    _print(asyncio.run(check_stream(cfg, a.seconds, use_filters=not a.all)))


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
    if a.depart:
        return parse_iso(a.depart)
    return max(at, utcnow().replace(second=0, microsecond=0)) if a.from_ is None else at


def cmd_passage(cfg, a):
    from .forecast import passage_forecast
    from .speedtable import DESTINATIONS, destination_route

    conn = _conn(cfg)
    mmsi = _mmsi(cfg, a.yacht)
    model, _ = _model(conn, cfg, mmsi)
    if a.to:
        if a.to not in DESTINATIONS:
            sys.exit(f"unknown destination {a.to!r}; known: {', '.join(DESTINATIONS)} (or give waypoints)")
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
    table = None if a.speed_table == "none" else a.speed_table
    try:
        r = passage_forecast(conn, cfg, _client(conn, cfg), model, mmsi, wps, depart, a.step_nm,
                             wind_provider=a.wind_provider, store=not a.no_store, speed_table=table,
                             motor_below_kn=a.motor_below, motor_speed_kn=a.motor_speed,
                             polar_factor=a.polar_factor, use_current=not a.no_current)
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
          + ("; current ignored" if a.no_current else "; incl. along-course current"))
    last = None
    for s_ in r["steps"]:
        t = parse_iso(s_["start_utc"])
        if a.steps or last is None or (t - last).total_seconds() >= 6 * 3600 - 60:
            tws = f"{s_['tws_kn']:.1f}" if s_["tws_kn"] is not None else "  ? "
            twa = f"{s_['twa_deg']:.0f}" if s_["twa_deg"] is not None else "?"
            cur = f"{s_['current_kn']:+.1f}" if s_.get("current_kn") is not None else "  ? "
            print(f"  {t:%a %d %H:%MZ}  {s_['lat']:7.3f} {s_['lon']:8.3f}  wind {tws} kn TWA {twa:>3}  "
                  f"current {cur}  SOG {s_['speed_kn']:4.1f} kn [{s_['speed_source']}] "
                  f"{' '.join(f for f in s_['flags'] if f not in ('speed_table', 'weak_support'))}")
            last = t
    print(f"  {parse_iso(r['arrival_utc']):%a %d %H:%MZ}  arrive")


def cmd_wind_ahead(cfg, a):
    from .forecast import wind_ahead

    conn = _conn(cfg)
    mmsi = _mmsi(cfg, a.yacht)
    lat, lon, at, course, sog, src = _start_point(conn, a, mmsi)
    if course is None or sog is None:
        sys.exit("course/speed unknown; pass --course and --speed")
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
    s.set_defaults(fn=cmd_check_stream)
    s = sub.add_parser("samples", help="show stored raw AIS payloads next to the parsed values")
    s.add_argument("--limit", type=int, default=5)
    s.set_defaults(fn=cmd_samples)
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
    s.add_argument("--motor-below", type=float, help="motor when sailing speed is below this (default: never)")
    s.add_argument("--motor-speed", type=float, default=7.0)
    s.add_argument("--wind-provider")
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
