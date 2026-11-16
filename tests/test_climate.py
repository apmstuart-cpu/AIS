from datetime import datetime, timedelta, timezone

from yachtspeed import climate, weather
from yachtspeed.config import Yacht
from yachtspeed.db import connect
from yachtspeed.speedtable import parse_destination


def test_climate_run_counts_on_time_years(tmp_path, monkeypatch, cfg):
    calls = []

    def fake_fetch(self, provider, kind, lat, lon, start, end):
        assert provider == "om_era5"
        calls.append((lat, lon, start))
        year = int(start[:4])
        trade = 17.0 if year == 2020 else 9.5  # 2021: weak trades, mostly motoring until fuel runs out
        t0 = datetime.fromisoformat(start + "T00:00:00+00:00")
        n = int((datetime.fromisoformat(end + "T00:00:00+00:00") - t0).total_seconds() // 3600) + 24
        return [{"time_utc": (t0 + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:00Z"), "wind_speed_kn": trade,
                 "wind_dir_from_deg": 65.0, "wind_gust_kn": None} for h in range(n)]

    monkeypatch.setattr(weather.OpenMeteo, "fetch", fake_fetch)
    conn = connect(":memory:")
    y = Yacht(name="X", mmsi="1", speed_table="oyster885_std", polar_factor=0.85)
    _, s = parse_destination("rubicon")
    _, d = parse_destination("english-harbour")
    dep = datetime(2026, 11, 20, 10, tzinfo=timezone.utc)
    by = datetime(2026, 12, 7, 3, 59, tzinfo=timezone.utc)
    out = climate.run(conn, cfg, y, s, d, dep, by, [2020, 2021], res=5.0, motor_wind_below_kn=10,
                      motor_speed_kn=8, motor_max_h=60, out_path=str(tmp_path / "c.json"), name="t")
    res = {r["year"]: r for r in out["results"]}
    assert out["status"] == "complete" and res[2020]["on_time"] and not res[2021]["on_time"]
    assert out["summary"]["probability"] == 0.5 and res[2021]["motor_hours"] <= 64
    n_calls = len(calls)
    out2 = climate.run(conn, cfg, y, s, d, dep, by, [2020, 2021], res=5.0, motor_wind_below_kn=10,
                       motor_speed_kn=8, motor_max_h=60, out_path=str(tmp_path / "c.json"), name="t")
    assert len(calls) == n_calls and out2["summary"]["years"] == 2  # resumes from stored data and results
    assert "On time in 1 of 2 years" in climate.markdown(out2)
    assert climate.tracks_jpeg(str(tmp_path / "c.jpg"), out2)
