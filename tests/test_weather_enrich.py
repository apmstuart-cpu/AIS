from datetime import timedelta

import pytest

from yachtspeed.collector import Store
from yachtspeed.ais import Position
from yachtspeed.enricher import enrich_once, retry_failed
from yachtspeed.geo import iso, utcnow
from yachtspeed.weather import Budget, OpenMeteo, QuotaExceeded, point_weather

from conftest import FakeSession, om_body


def add_report(conn, cfg, when, lat=39.5, lon=2.6, mmsi="235000001"):
    Store(conn, cfg).store_position(Position(mmsi, "PositionReport", "A", when, lat, lon, 7.0, 90.0, None, 8, 0, 1))


def client(conn, handler, **limits):
    b = Budget(conn, limits.get("m", 100), limits.get("h", 1000), limits.get("d", 5000))
    return OpenMeteo(b, FakeSession(handler))


def test_units_converted_and_conventions(conn, cfg):
    when = (utcnow() - timedelta(days=5)).replace(minute=20)
    add_report(conn, cfg, when)
    c = client(conn, lambda url, p: (200, om_body(p)))
    s = enrich_once(conn, cfg, c)
    assert s["matched"] == 1
    e = conn.execute("SELECT * FROM enrichment").fetchone()
    assert e["current_speed_kn"] == pytest.approx(1.0)  # 1.852 km/h
    assert e["wind_speed_kn"] == 12.0 and e["wind_provider"] == "om_hist_forecast"
    assert e["weather_hour_utc"] == iso(when.replace(minute=0, second=0, microsecond=0))
    urls = [u for u, _ in c.http.calls]
    assert any("historical-forecast" in u for u in urls) and any("marine" in u for u in urls)
    # second run is served from cache: no more calls
    add_report(conn, cfg, when + timedelta(minutes=10))
    enrich_once(conn, cfg, c)
    assert len(c.http.calls) == 2


def test_recent_rows_use_forecast_api(conn, cfg):
    add_report(conn, cfg, utcnow() - timedelta(hours=3))
    c = client(conn, lambda url, p: (200, om_body(p)))
    enrich_once(conn, cfg, c)
    assert any(u.startswith("https://api.open-meteo.com") for u, _ in c.http.calls)


def test_null_marine_is_partial_with_missing_listed(conn, cfg):
    add_report(conn, cfg, utcnow() - timedelta(days=4))
    c = client(conn, lambda url, p: (200, om_body(p, {"ocean_current_velocity": [None] * 24,
                                                     "ocean_current_direction": [None] * 24})))
    enrich_once(conn, cfg, c)
    e = conn.execute("SELECT status, missing FROM enrichment").fetchone()
    assert e["status"] == "partial" and e["missing"] == "current_speed_kn,current_dir_to_deg"


def test_failure_backs_off_without_blocking_other_cells(conn, cfg):
    add_report(conn, cfg, utcnow() - timedelta(days=4), lat=36.0, lon=-5.0)  # failing cell
    add_report(conn, cfg, utcnow() - timedelta(days=4, hours=2), lat=40.0, lon=3.0)

    def handler(url, p):
        if abs(p["latitude"] - 36.0) < 0.01:
            return 500, {"error": True, "reason": "boom"}
        return 200, om_body(p)

    c = client(conn, handler)
    s = enrich_once(conn, cfg, c)
    assert s["matched"] == 1 and s["waiting"] == 1
    calls = len(c.http.calls)
    enrich_once(conn, cfg, c)  # failing cell is in back-off: not re-requested
    assert len(c.http.calls) == calls
    f = conn.execute("SELECT status, attempts, next_retry_utc FROM weather_fetches WHERE status!='ok'").fetchone()
    assert f["status"] == "retry" and f["attempts"] == 1 and f["next_retry_utc"] > iso(utcnow())
    assert retry_failed(conn) >= 1


def test_permanent_failure_marks_missing(conn, cfg):
    cfg.max_fetch_attempts = 1
    add_report(conn, cfg, utcnow() - timedelta(days=4))
    c = client(conn, lambda url, p: (400, {"error": True, "reason": "out of range"}))
    s = enrich_once(conn, cfg, c)
    assert s["missing"] == 1
    assert conn.execute("SELECT status FROM enrichment").fetchone()[0] == "missing"


def test_provider_rate_limit_pauses_host(conn, cfg):
    add_report(conn, cfg, utcnow() - timedelta(days=4))
    c = client(conn, lambda url, p: (429, {"error": True, "reason": "Daily API request limit exceeded."}))
    s = enrich_once(conn, cfg, c)
    assert s["quota_paused_until"] and s["waiting"] == 1
    n = len(c.http.calls)
    enrich_once(conn, cfg, c)
    assert len(c.http.calls) == n  # paused: no call made


def test_local_budget(conn, cfg):
    c = client(conn, lambda url, p: (200, om_body(p)), m=1)
    c.fetch("om_marine", "marine", 39.5, 2.6, "2026-01-01", "2026-01-01")
    with pytest.raises(QuotaExceeded):
        c.fetch("om_marine", "marine", 39.5, 2.6, "2026-01-02", "2026-01-02")


def test_point_weather_previous_runs(conn, cfg):
    c = client(conn, lambda url, p: (200, om_body(p)))
    wx, wprov, _ = point_weather(conn, c, cfg, 39.5, 2.6, utcnow() - timedelta(days=10), "om_prev_day3")
    assert wprov == "om_prev_day3" and wx["wind_speed_kn"] == 12.0
    url, params = [x for x in c.http.calls if "previous-runs" in x[0]][0]
    assert "wind_speed_10m_previous_day3" in params["hourly"]
