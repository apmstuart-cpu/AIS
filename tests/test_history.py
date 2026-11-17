from datetime import datetime, timedelta, timezone

from yachtspeed import history, weather
from yachtspeed.config import Yacht
from yachtspeed.db import connect


def _gpx(path, start, n, lat0=36.0, lon0=-10.0, kn=7.0, course_deg=225.0):
    import math

    lines = ['<?xml version="1.0"?>', '<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">',
             "<trk><name>Own Ship</name><trkseg>"]
    for i in range(n):
        t = start + timedelta(seconds=2 * i)
        d = kn * (2 * i) / 3600 / 60
        la = lat0 + d * math.cos(math.radians(course_deg))
        lo = lon0 + d * math.sin(math.radians(course_deg)) / math.cos(math.radians(lat0))
        lines.append(f'<trkpt lat="{la}" lon="{lo}"><time>{t.strftime("%Y-%m-%dT%H:%M:%S")}.6923197Z</time></trkpt>')
    lines.append("</trkseg></trk></gpx>")
    path.write_text("\n".join(lines))


def test_import_and_analyse_history(tmp_path, monkeypatch):
    start = datetime(2025, 8, 10, 6, tzinfo=timezone.utc)
    gpx = tmp_path / "t.gpx"
    _gpx(gpx, start, 6 * 1800)  # 6 hours at 2 s, 7 kn on 225 deg
    conn = connect(":memory:")
    st = history.import_gpx(conn, str(gpx), "1", every_s=60)
    assert st["read"] == 6 * 1800 and 340 < st["kept_under_way"] < 362
    sog = conn.execute("SELECT avg(sog_kn) FROM track_points").fetchone()[0]
    assert abs(sog - 7.0) < 0.2

    def fake_fetch(self, provider, kind, lat, lon, s, e):
        t0 = datetime.fromisoformat(s + "T00:00:00+00:00")
        n = int((datetime.fromisoformat(e + "T00:00:00+00:00") - t0).total_seconds() // 3600) + 24
        ts = [(t0 + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:00Z") for h in range(n)]
        if kind == "wind":
            return [{"time_utc": t, "wind_speed_kn": 14.0, "wind_dir_from_deg": 30.0} for t in ts]
        return [{"time_utc": t, "wave_height_m": None, "wave_dir_from_deg": None, "wave_period_s": None,
                 "current_speed_kn": 0.0, "current_dir_to_deg": 0.0} for t in ts]

    monkeypatch.setattr(weather.OpenMeteo, "fetch", fake_fetch)
    client = weather.OpenMeteo(weather.Budget(conn, 10 ** 6, 10 ** 6, 10 ** 6))
    history.fetch_weather(conn, client, "1")
    y = Yacht(name="X", mmsi="1", speed_table="oyster885_std", polar_factor=0.85)
    res = history.analyse(conn, y)
    o = res["overall"]
    assert o and o["points"] >= 30 and 50 < o["median_pct"] < 100  # 7 kn in 14 kn at TWA ~165
    assert "| 12-16 kn |" in history.markdown(res)


def test_jobs_fetched_elsewhere_then_merged(tmp_path, monkeypatch):
    from yachtspeed import climate

    start = datetime(2025, 8, 10, 6, tzinfo=timezone.utc)
    gpx = tmp_path / "t.gpx"
    _gpx(gpx, start, 1800)
    server = connect(str(tmp_path / "server.db"))
    history.import_gpx(server, str(gpx), "1")
    jobs, ncells = history.needed_jobs(server, "1")
    assert jobs and all(j[0] in ("wind", "marine") for j in jobs)

    def fake_fetch(self, provider, kind, lat, lon, s, e):
        return [{"time_utc": s + "T06:00:00Z", "wind_speed_kn": 12.0, "wind_dir_from_deg": 40.0,
                 "wave_height_m": 1.0, "wave_dir_from_deg": 40.0, "wave_period_s": 7.0,
                 "current_speed_kn": 0.1, "current_dir_to_deg": 200.0}]

    monkeypatch.setattr(weather.OpenMeteo, "fetch", fake_fetch)
    mac = connect(str(tmp_path / "mac.db"))
    history.fetch_jobs(mac, weather.OpenMeteo(weather.Budget(mac, 10 ** 6, 10 ** 6, 10 ** 6)), jobs)
    added = climate.merge(server, str(tmp_path / "mac.db"))
    assert added["hist_wx_done"] == len(jobs)
    assert history.needed_jobs(server, "1")[0] == []
