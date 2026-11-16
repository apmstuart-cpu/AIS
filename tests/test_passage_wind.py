import json
import math
from datetime import timedelta

import pytest

from yachtspeed.forecast import passage_forecast, wind_ahead
from yachtspeed.geo import utcnow
from yachtspeed.model import SogModel
from yachtspeed.speedtable import destination_route, table_speed
from yachtspeed.weather import Budget, OpenMeteo

from conftest import FakeSession, om_body


def client(conn, handler=None):
    return OpenMeteo(Budget(conn, 1000, 10000, 50000), FakeSession(handler or (lambda u, p: (200, om_body(p)))))


def test_speed_table_shape():
    assert table_speed(12, 90) > table_speed(12, 180) > 0
    assert table_speed(12, 20) < table_speed(12, 45)  # tacking: progress below the close-hauled speed
    assert table_speed(2, 90) < 2


def test_rubicon_route_goes_round_lanzarote_from_the_north_east():
    r = destination_route("rubicon", 33.62, -8.38)
    assert r[0] == (29.05, -13.35) and r[-1] == (28.857, -13.817)
    assert destination_route("rubicon", 28.2, -15.4) == [(28.857, -13.817)]  # from Gran Canaria: direct


def test_passage_without_history_uses_speed_table_and_no_motor(conn, cfg):
    dep = (utcnow() + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    wps = [(33.62, -8.38)] + destination_route("rubicon", 33.62, -8.38)
    empty = SogModel(cfg, [])
    r = passage_forecast(conn, cfg, client(conn), empty, "235000001", wps, dep, step_nm=20,
                         store=False, speed_table="cruiser27")
    assert set(r["hours_by_speed_source"]) == {"speed_table"}
    assert 380 < r["distance_nm"] < 430
    assert r["duration_hours"] == pytest.approx(r["distance_nm"] / (r["distance_nm"] / r["duration_hours"]))
    with pytest.raises(ValueError):
        passage_forecast(conn, cfg, client(conn), empty, "235000001", wps, dep, step_nm=20, store=False)


def test_motoring_only_when_allowed(conn, cfg):
    calm = lambda u, p: (200, om_body(p, {"wind_speed_10m": [0.5] * 24}))  # noqa: E731
    dep = (utcnow() + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    wps = [(33.0, -9.0), (32.5, -9.5)]
    empty = SogModel(cfg, [])
    sail = passage_forecast(conn, cfg, client(conn, calm), empty, "235000001", wps, dep, store=False,
                            speed_table="cruiser27")
    motor = passage_forecast(conn, cfg, client(conn, calm), empty, "235000001", wps, dep, store=False,
                             speed_table="cruiser27", motor_below_kn=5, motor_speed_kn=7)
    assert any("becalmed" in s["flags"] for s in sail["steps"])
    assert set(motor["hours_by_speed_source"]) == {"motor"} and motor["duration_hours"] < sail["duration_hours"]


def test_wind_ahead_finds_first_hour_over_threshold(conn, cfg):
    start = (utcnow() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    def ramp(u, p):  # wind rises 1 kn per hour from 4 kn at 00Z
        return 200, om_body(p, {"wind_speed_10m": [4.0 + h for h in range(24)]} if "wind" in p["hourly"] else None)

    r = wind_ahead(conn, cfg, client(conn, ramp), 33.62, -8.38, 238, 4.5, start, threshold=10, hours=30)
    f = r["first"]
    assert f is not None and f["wind_kn"] >= 10 and f["time_utc"][11:13] == "06"
    below = wind_ahead(conn, cfg, client(conn, ramp), 33.62, -8.38, 238, 4.5, start, threshold=10, hours=30,
                       below=True)
    assert below["first"]["wind_kn"] < 10


def test_future_wind_falls_back_to_metno_when_open_meteo_quota_hit(conn, cfg):
    from yachtspeed.weather import point_weather

    def handler(url, p):
        if "api.met.no" in url:
            t0 = (utcnow() + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            series = [{"time": (t0 + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "data": {"instant": {"details": {"wind_speed": 5.144, "wind_from_direction": 45.0}}}}
                      for h in range(48)]
            return 200, {"properties": {"timeseries": series}}
        if "marine" in url:
            return 200, om_body(p)
        return 429, {"error": True, "reason": "Daily API request limit exceeded."}

    when = utcnow() + timedelta(hours=5)
    wx, prov, _ = point_weather(conn, client(conn, handler), cfg, 33.3, -8.9, when)
    assert prov == "metno" and wx["wind_speed_kn"] == pytest.approx(10.0, abs=0.01)
    assert wx["wave_height_m"] == 1.0  # marine still from Open-Meteo


def test_past_wind_does_not_use_metno(conn, cfg):
    from yachtspeed.weather import FetchError, point_weather

    c = client(conn, lambda u, p: (429, {"error": True, "reason": "Daily API request limit exceeded."}))
    with pytest.raises(FetchError):
        point_weather(conn, c, cfg, 33.3, -8.9, utcnow() - timedelta(days=3))
    assert not any("met.no" in u for u, _ in c.http.calls)


def test_orc_885_table_matches_certificate_and_gybes_downwind():
    from yachtspeed.speedtable import TABLES

    assert table_speed(10, 90, "oyster885gt_orc") == pytest.approx(10.88)
    assert table_speed(10, 90, "oyster885gt_orc", 0.85) == pytest.approx(10.88 * 0.85)
    dead_run = table_speed(10, 180, "oyster885gt_orc")
    assert dead_run > TABLES["oyster885gt_orc"]["speeds"][180][3]  # gybing beats sailing dead downwind
    assert dead_run == pytest.approx(7.45, abs=0.5)  # close to the certificate's run VMG
    assert table_speed(10, 0, "oyster885gt_orc") == pytest.approx(6.39)  # dead upwind: beat VMG
    assert table_speed(10, 30, "oyster885gt_orc") == pytest.approx(6.39 / math.cos(math.radians(30)))
    beat_speed = 6.39 / math.cos(math.radians(41.4))
    assert beat_speed < table_speed(10, 46, "oyster885gt_orc") < 9.57  # between beat and 52 deg
    speeds = [table_speed(10, a, "oyster885gt_orc") for a in range(0, 181, 5)]
    assert all(abs(b - a) < 1.5 for a, b in zip(speeds, speeds[1:]))  # no jumps


def test_passage_adds_along_course_current(conn, cfg):
    # om_body: wind 12 kn from 270, current 1 kn towards 090
    dep = (utcnow() + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    east = [(33.0, -10.0), (33.0, -9.0)]  # course ~090: current fully helping
    empty = SogModel(cfg, [])
    with_c = passage_forecast(conn, cfg, client(conn), empty, "235000001", east, dep, store=False,
                              speed_table="oyster885gt_orc")
    without = passage_forecast(conn, cfg, client(conn), empty, "235000001", east, dep, store=False,
                               speed_table="oyster885gt_orc", use_current=False)
    s1, s0 = with_c["steps"][0], without["steps"][0]
    assert s1["current_kn"] == pytest.approx(1.0, abs=0.05)
    assert s1["speed_kn"] - s0["speed_kn"] == pytest.approx(1.0, abs=0.05)


def test_standard_885_is_slower_mostly_upwind():
    gt = lambda a: table_speed(12, a, "oyster885gt_orc")  # noqa: E731
    std = lambda a: table_speed(12, a, "oyster885_std")  # noqa: E731
    assert std(0) == pytest.approx(gt(0) * 0.93, rel=1e-3)
    assert std(90) == pytest.approx(gt(90) * 0.98, rel=1e-3)
    assert std(150) == pytest.approx(gt(150) * 0.99, rel=1e-3)
    assert (gt(0) - std(0)) / gt(0) > (gt(150) - std(150)) / gt(150)


def test_blend_directions_as_vectors():
    from yachtspeed.weather import blend

    r = blend({"wind_speed_kn": 10, "wind_dir_from_deg": 350}, {"wind_speed_kn": 20, "wind_dir_from_deg": 10}, 0.5)
    assert r["wind_speed_kn"] == pytest.approx(15) and min(r["wind_dir_from_deg"], 360 - r["wind_dir_from_deg"]) < 0.01
    r = blend({"wave_height_m": 1.0, "current_speed_kn": None}, {"wave_height_m": 2.0, "current_speed_kn": 0.5}, 0.25)
    assert r["wave_height_m"] == pytest.approx(1.25) and r["current_speed_kn"] == 0.5


def test_point_weather_blends_between_hours(conn, cfg):
    from yachtspeed.weather import point_weather

    def ramp(u, p):  # wind rises 1 kn per hour
        return 200, om_body(p, {"wind_speed_10m": [4.0 + h for h in range(24)]} if "wind" in p["hourly"] else None)

    day = (utcnow() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    wx, _, _ = point_weather(conn, client(conn, ramp), cfg, 33.3, -8.9, day + timedelta(hours=6, minutes=45))
    assert wx["wind_speed_kn"] == pytest.approx(10.75)


def test_metno_six_hourly_steps_are_blended(conn, cfg):
    from yachtspeed.weather import point_weather

    t0 = (utcnow() + timedelta(days=4)).replace(hour=0, minute=0, second=0, microsecond=0)

    def handler(url, p):
        if "api.met.no" in url:
            series = [{"time": (t0 + timedelta(hours=6 * k)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "data": {"instant": {"details": {"wind_speed": 5.144 * (1 + k), "wind_from_direction": 90.0}}}}
                      for k in range(4)]  # 10, 20, 30, 40 kn every 6 h
            return 200, {"properties": {"timeseries": series}}
        if "marine" in url:
            return 200, om_body(p)
        return 429, {"error": True, "reason": "Daily API request limit exceeded."}

    wx, prov, _ = point_weather(conn, client(conn, handler), cfg, 33.3, -8.9, t0 + timedelta(hours=1, minutes=30))
    assert prov == "metno" and wx["wind_speed_kn"] == pytest.approx(12.5, abs=0.01)  # 1.5 h of 6 h: 10 -> 20


def test_tenerife_route_rounds_anaga_from_the_north_east():
    r = destination_route("tenerife", 33.6, -8.9)
    assert r == [(28.62, -16.05), (28.465, -16.245)]


def test_recent_past_fix_uses_metno_held_back_when_open_meteo_unavailable(conn, cfg):
    from yachtspeed.weather import FetchError, point_weather

    t0 = utcnow().replace(minute=0, second=0, microsecond=0)

    def handler(url, p):
        if "api.met.no" in url:  # forecast starts at the current hour, like the real service
            series = [{"time": (t0 + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "data": {"instant": {"details": {"wind_speed": 5.144, "wind_from_direction": 30.0}}}}
                      for h in range(24)]
            return 200, {"properties": {"timeseries": series}}
        if "marine" in url:
            return 200, om_body(p)
        return 429, {"error": True, "reason": "Daily API request limit exceeded."}

    c = client(conn, handler)
    wx, prov, _ = point_weather(conn, c, cfg, 33.6, -8.4, utcnow() - timedelta(hours=4))
    assert prov == "metno" and wx["wind_speed_kn"] == pytest.approx(10.0, abs=0.01)
    with pytest.raises(FetchError):  # more than 12 h back: no fallback
        point_weather(conn, c, cfg, 33.6, -8.4, utcnow() - timedelta(hours=14))


def test_model_provider_requests_named_model(conn, cfg):
    from yachtspeed.weather import model_provider, point_weather

    def handler(url, p):
        if "marine" in url:
            return 200, om_body(p)
        speed = {"ecmwf_ifs025": 8.0, "gfs_seamless": 14.0}[p["models"]]
        return 200, om_body(p, {"wind_speed_10m": [speed] * 24})

    c = client(conn, handler)
    when = (utcnow() + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
    e, pe, _ = point_weather(conn, c, cfg, 33.3, -8.9, when, model_provider("ecmwf"))
    g, pg, _ = point_weather(conn, c, cfg, 33.3, -8.9, when, model_provider("gfs"))
    assert (e["wind_speed_kn"], g["wind_speed_kn"]) == (8.0, 14.0)
    assert pe == "om_model:ecmwf_ifs025" and pg == "om_model:gfs_seamless"


def test_passage_models_cli_shows_spread(conn, cfg, tmp_path, monkeypatch, capsys):
    from yachtspeed import cli, weather

    def handler(url, p):
        if "marine" in url:
            return 200, om_body(p)
        speed = {"ecmwf_ifs025": 8.0, "gfs_seamless": 16.0}.get(p.get("models"), 12.0)
        return 200, om_body(p, {"wind_speed_10m": [speed] * 24})

    real_init = weather.OpenMeteo.__init__
    monkeypatch.setattr(weather.OpenMeteo, "__init__",
                        lambda self, budget, session=None, timeout=30: real_init(self, budget, FakeSession(handler)))
    cfgfile = tmp_path / "c.toml"
    cfgfile.write_text(f'db_path = "{cfg.db_path}"\n[[yacht]]\nname = "TESTBOAT"\nmmsi = "235000001"\n')
    dep = (utcnow() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:00Z")
    cli.main(["--config", str(cfgfile), "--plain-logs", "passage", "--yacht", "TESTBOAT", "--from", "33.6,-8.4",
              "--at", dep, "--to", "rubicon", "--speed-table", "oyster885_std", "--models", "ecmwf,gfs", "--json"])
    out = json.loads(capsys.readouterr().out)
    by = {m["model"]: m for m in out["models"]}
    assert by["gfs"]["hours"] < by["ecmwf"]["hours"]  # more wind, faster
    assert out["spread"]["spread_hours"] > 0


def test_wave_factor_shapes():
    from yachtspeed.speedtable import wave_factor

    head, rel = wave_factor(2.0, 0, 0)
    beam, _ = wave_factor(2.0, 90, 0)
    follow, relf = wave_factor(2.0, 180, 0)
    assert rel == 0 and relf == 180
    assert head == pytest.approx(0.90) and head < beam < 1.0 < follow <= 1.03
    assert wave_factor(None, 0, 0) == (1.0, None)
    assert wave_factor(2.0, 0, 0, period_s=5)[0] < head  # short steep seas hurt more
    assert wave_factor(10.0, 0, 0)[0] == 0.65  # capped


def test_passage_applies_head_seas(conn, cfg):
    # om_body: wind 12 kn from 270; waves 1 m from... om_body has no wave_direction default -> set head seas
    def handler(u, p):
        if "marine" in u:
            return 200, om_body(p, {"wave_height": [2.5] * 24, "wave_direction": [90.0] * 24, "wave_period": [7.0] * 24})
        return 200, om_body(p)
    dep = (utcnow() + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    east = [(33.0, -10.0), (33.0, -9.5)]  # course ~090 into seas from 090
    empty = SogModel(cfg, [])
    w = passage_forecast(conn, cfg, client(conn, handler), empty, "235000001", east, dep, store=False,
                         speed_table="oyster885gt_orc", use_current=False)
    n = passage_forecast(conn, cfg, client(conn, handler), empty, "235000001", east, dep, store=False,
                         speed_table="oyster885gt_orc", use_current=False, use_waves=False)
    s = w["steps"][0]
    assert s["wave_rel_deg"] < 5 and s["wave_factor"] == pytest.approx(1 - 0.025 * 2.5 ** 2, abs=0.01)
    assert w["duration_hours"] > n["duration_hours"]


def test_polar_check_scores_fixes(conn, cfg, tmp_path, monkeypatch, capsys):
    from yachtspeed import cli, weather

    real_init = weather.OpenMeteo.__init__
    monkeypatch.setattr(weather.OpenMeteo, "__init__",
                        lambda self, budget, session=None, timeout=30: real_init(self, budget, FakeSession(
                            lambda u, p: (200, om_body(p, {"ocean_current_velocity": [0.0] * 24}
                                                          if "marine" in u else None)))))
    t = (utcnow() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:00:00Z")
    fx = tmp_path / "f.csv"
    # wind 12 kn from 270 (om_body); course 000 -> TWA 90; polar 885 GT at 12 kn/90 = 11.32
    fx.write_text("mmsi,timestamp_utc,lat,lon,sog_kn,cog_deg,heading_deg,nav_status,yacht,screenshot\n"
                  f"235000001,{t},33.0,-10.0,8.5,0,,,TESTBOAT,x\n")
    cfgfile = tmp_path / "c.toml"
    cfgfile.write_text(f'db_path = "{cfg.db_path}"\n[[yacht]]\nname = "TESTBOAT"\nmmsi = "235000001"\n')
    cli.main(["--config", str(cfgfile), "--plain-logs", "polar-check", "--yacht", "TESTBOAT", "--csv", str(fx),
              "--speed-table", "oyster885gt_orc", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["fixes_scored"] == 1
    assert out["fixes"][0]["ratio"] == pytest.approx(8.5 / (11.32 * out["fixes"][0]["polar_kn"] / out["fixes"][0]["polar_kn"]), rel=0.05)


def test_sw96_table_from_certificate():
    assert table_speed(12, 90, "sw96_orc") == pytest.approx(12.87)
    assert table_speed(12, 0, "sw96_orc") == pytest.approx(7.80)  # beat VMG
    assert table_speed(10, 90, "sw96_orc") > table_speed(10, 90, "oyster885gt_orc")  # bigger, faster boat


def test_passage_from_old_fix_departs_at_fix_time(conn, cfg, tmp_path, monkeypatch, capsys):
    from yachtspeed import cli, weather
    from yachtspeed.ais import Position
    from yachtspeed.collector import Store

    real_init = weather.OpenMeteo.__init__
    monkeypatch.setattr(weather.OpenMeteo, "__init__",
                        lambda self, budget, session=None, timeout=30: real_init(self, budget, FakeSession(
                            lambda u, p: (200, om_body(p)))))
    fix_time = (utcnow() - timedelta(hours=16)).replace(second=0, microsecond=0)
    Store(conn, cfg).store_position(Position("235000001", "csv", None, fix_time, 33.6, -9.0, 10.0, 240.0,
                                             None, None, None, None), source="csv:test")
    cfgfile = tmp_path / "c.toml"
    cfgfile.write_text(f'db_path = "{cfg.db_path}"\n[[yacht]]\nname = "TESTBOAT"\nmmsi = "235000001"\n')
    cli.main(["--config", str(cfgfile), "--plain-logs", "passage", "--yacht", "TESTBOAT", "--to", "tenerife",
              "--speed-table", "oyster885gt_orc", "--json", "--no-store"])
    out = json.loads(capsys.readouterr().out)
    assert out["departure_utc"] == fix_time.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_polar_check_reads_database_by_default(conn, cfg, tmp_path, monkeypatch, capsys):
    from yachtspeed import cli, weather
    from yachtspeed.ais import Position
    from yachtspeed.collector import Store

    real_init = weather.OpenMeteo.__init__
    monkeypatch.setattr(weather.OpenMeteo, "__init__",
                        lambda self, budget, session=None, timeout=30: real_init(self, budget, FakeSession(
                            lambda u, p: (200, om_body(p)))))
    t = (utcnow() - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    Store(conn, cfg).store_position(Position("235000001", "csv", None, t, 33.0, -10.0, 8.0, 0.0,
                                             None, None, None, None), source="csv:test")
    cfgfile = tmp_path / "c.toml"
    cfgfile.write_text(f'db_path = "{cfg.db_path}"\n[[yacht]]\nname = "TESTBOAT"\nmmsi = "235000001"\n')
    monkeypatch.chdir(tmp_path)  # no data/ folder here: must not need the CSV
    cli.main(["--config", str(cfgfile), "--plain-logs", "polar-check", "--yacht", "TESTBOAT",
              "--speed-table", "oyster885gt_orc", "--json"])
    assert json.loads(capsys.readouterr().out)["summary"]["fixes_scored"] == 1


def test_report_builds_json_and_markdown(conn, cfg, tmp_path, monkeypatch):
    from yachtspeed import report, weather
    from yachtspeed.ais import Position
    from yachtspeed.collector import Store

    real_init = weather.OpenMeteo.__init__
    monkeypatch.setattr(weather.OpenMeteo, "__init__",
                        lambda self, budget, session=None, timeout=30: real_init(self, budget, FakeSession(
                            lambda u, p: (200, om_body(p)))))
    cfg.calls_per_minute = 100000  # the blend grid makes several hundred (fake) calls
    cfg.fleet[0].destination = "rubicon"
    cfg.fleet[0].speed_table = "oyster885_std"
    cfg.fleet[0].polar_factor = 0.85
    t = (utcnow() - timedelta(hours=1)).replace(second=0, microsecond=0)
    Store(conn, cfg).store_position(Position("235000001", "csv", None, t, 32.34, -9.52, 8.8, 242.0,
                                             None, None, None, None), source="csv:test")
    rep = report.build(conn, cfg, ["ecmwf", "gfs"])
    y = rep["yachts"][0]
    assert y["status"] == "ok" and y["eta"]["median_utc"] and y["eta"]["basis"]
    # one basis only: the routed course's median, or the straight-line fallback, never both
    assert ("models" in y) != y["eta"]["basis"].startswith("routed")
    assert y["polar"]["fixes"] == 1
    files = report.write(rep, str(tmp_path / "out"))
    md = open(files[1]).read()
    assert "TESTBOAT" in md and "rubicon" in md and "Polar evidence" in md


def test_gybing_adds_maneuver_time(conn, cfg):
    from yachtspeed.speedtable import table_speed_mode

    assert table_speed_mode(12, 175, "oyster885_std")[1] == "gybing"
    assert table_speed_mode(12, 90, "oyster885_std")[1] == "direct"
    assert table_speed_mode(12, 20, "oyster885_std")[1] == "tacking"
    # om_body wind from 270: course 090 is dead downwind -> gybing
    dep = (utcnow() + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    wps = [(33.0, -10.0), (33.0, -8.5)]
    empty = SogModel(cfg, [])
    no = passage_forecast(conn, cfg, client(conn), empty, "235000001", wps, dep, store=False,
                          speed_table="oyster885_std", maneuver_every_h=0)
    yes = passage_forecast(conn, cfg, client(conn), empty, "235000001", wps, dep, store=False,
                           speed_table="oyster885_std", maneuver_loss_min=4, maneuver_every_h=4)
    assert yes["maneuvers"] > 0 and set(yes["hours_by_sail_mode"]) == {"gybing"}
    extra = yes["duration_hours"] - no["duration_hours"]
    assert extra == pytest.approx(yes["maneuver_hours"], rel=0.05)


def test_recording_only_yacht_gets_no_forecast(conn, cfg, monkeypatch):
    from yachtspeed import report, weather
    from yachtspeed.ais import Position
    from yachtspeed.collector import Store

    real_init = weather.OpenMeteo.__init__
    monkeypatch.setattr(weather.OpenMeteo, "__init__",
                        lambda self, budget, session=None, timeout=30: real_init(self, budget, FakeSession(
                            lambda u, p: (200, om_body(p)))))
    y = cfg.fleet[0]
    y.destination, y.speed_table, y.forecast = "rubicon", "oyster885_std", False
    Store(conn, cfg).store_position(Position("235000001", "csv", None, utcnow() - timedelta(hours=1), 32.3, -9.5,
                                             8.0, 240.0, None, None, None, None), source="csv:test")
    rep = report.build(conn, cfg, ["ecmwf"])
    r = rep["yachts"][0]
    assert r["status"] == "recording only" and "models" not in r and "routed" not in r and r["fix"]
    assert "recording only" in report.markdown(rep)


def test_sw96_sorceress_slower_in_light_air_than_liberty():
    light = table_speed(8, 90, "sw96_sorceress") / table_speed(8, 90, "sw96_orc")
    fresh = table_speed(20, 90, "sw96_sorceress") / table_speed(20, 90, "sw96_orc")
    assert light == pytest.approx(0.963, abs=0.002)
    assert fresh == pytest.approx(0.990, abs=0.002)
    assert table_speed(12, 140, "sw96_sorceress") > table_speed(12, 140, "oyster885_std")
