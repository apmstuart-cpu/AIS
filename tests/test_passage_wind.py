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
    assert table_speed(12, 20) < table_speed(12, 45)  # tacking penalty
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
