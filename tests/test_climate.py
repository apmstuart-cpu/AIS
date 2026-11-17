from datetime import datetime, timedelta, timezone

from yachtspeed import climate, weather
from yachtspeed.config import Yacht
from yachtspeed.db import connect
from yachtspeed.speedtable import parse_destination


def test_climate_run_counts_on_time_years(tmp_path, monkeypatch, cfg):
    calls = []

    def fake_fetch(self, provider, kind, lat, lon, start, end):
        if provider == "om_marine":  # a steady westgoing current of 0.5 kn
            return [{"time_utc": start + "T00:00:00Z", "current_speed_kn": 0.5, "current_dir_to_deg": 270.0}]
        if provider == "om_era5_ocean":  # a 2.5 m following sea from the ENE
            t0 = datetime.fromisoformat(start + "T00:00:00+00:00")
            return [{"time_utc": (t0 + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:00Z"), "wave_height_m": 2.5,
                     "wave_dir_from_deg": 65.0, "wave_period_s": 9.0} for h in range(0, 30 * 24, 3)]
        assert provider == "om_era5"
        calls.append((lat, lon, start))
        year = int(start[:4])
        trade = 17.0 if year == 2020 else 7.0  # 2021: very weak trades, motoring until fuel runs out
        t0 = datetime.fromisoformat(start + "T00:00:00+00:00")
        n = int((datetime.fromisoformat(end + "T00:00:00+00:00") - t0).total_seconds() // 3600) + 24
        return [{"time_utc": (t0 + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:00Z"), "wind_speed_kn": trade,
                 "wind_dir_from_deg": 65.0, "wind_gust_kn": None} for h in range(n)]

    monkeypatch.setattr(weather.OpenMeteo, "fetch", fake_fetch)
    monkeypatch.setattr(climate, "oni_table", lambda conn=None: {("OND", 2020): -1.11, ("OND", 2021): -0.91,
                                                                  ("ASO", 2026): 2.46})
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
    assert out["enso"]["now"]["oni"] == 2.46 and out["enso"]["years"]["2020"]["phase"] == "La Nina"
    assert all(r["waves"] == "era5" for r in out["results"])  # pass 2 re-routed with the real waves
    assert out["summary"]["probability_enso_weighted"] is not None
    n_calls = len(calls)
    out2 = climate.run(conn, cfg, y, s, d, dep, by, [2020, 2021], res=5.0, motor_wind_below_kn=10,
                       motor_speed_kn=8, motor_max_h=60, out_path=str(tmp_path / "c.json"), name="t")
    assert len(calls) == n_calls and out2["summary"]["years"] == 2  # resumes from stored data and results
    assert "On time in 1 of 2 years" in climate.markdown(out2)
    assert climate.tracks_jpeg(str(tmp_path / "c.jpg"), out2)
    no_cur = climate.run(conn, cfg, y, s, d, dep, by, [2020], res=5.0, motor_wind_below_kn=10, motor_speed_kn=8,
                         motor_max_h=60, currents=False)
    assert no_cur["results"][0]["days"] > res[2020]["days"]  # the westgoing current shortens the crossing


def test_enso_weights_favour_similar_years():
    oni = {("OND", 2015): 2.45, ("OND", 2010): -1.57, ("OND", 2018): 1.04, ("ASO", 2026): 2.46}
    cur, w = climate.enso_weights(oni, [2010, 2015, 2018])
    assert cur["oni"] == 2.46 and w[2015][1] > 0.99 and w[2010][1] == 0.1 and 0.3 < w[2018][1] < 0.45


def test_hybrid_grid_uses_forecast_then_past_year():
    from datetime import datetime, timedelta, timezone

    class G:
        def __init__(self, v):
            self.v, self.t0, self.lats, self.lons = v, datetime(2026, 11, 20, tzinfo=timezone.utc), [], []

        def sample(self, lat, lon, t):
            return {"wind_speed_kn": self.v, "t": t}

    class W(G):
        def sample(self, lat, lon, t):
            return {"wind_speed_kn": self.v, "wind_dir_from_deg": 60.0, "t": t}

    t0 = datetime(2026, 11, 20, tzinfo=timezone.utc)
    h = climate.HybridGrid(W(15), W(9), t0 + timedelta(days=6), t0 + timedelta(days=12), timedelta(days=365 * 6 + 2))
    assert h.sample(20, -30, t0 + timedelta(days=1))["wind_speed_kn"] == 15
    assert abs(h.sample(20, -30, t0 + timedelta(days=9))["wind_speed_kn"] - 12.0) < 1e-6  # halfway: half each
    late = h.sample(20, -30, t0 + timedelta(days=13))
    assert late["wind_speed_kn"] == 9 and late["t"].year == 2020


def test_forecast_then_history(tmp_path, monkeypatch, cfg):
    from yachtspeed.geo import utcnow

    def fake_fetch(self, provider, kind, lat, lon, start, end):
        if provider in ("om_marine", "om_era5_ocean"):
            return []
        trade = (20.0 if "ecmwf" in provider else 15.0) if provider.startswith("om_model") else 9.5
        t0 = datetime.fromisoformat(start + "T00:00:00+00:00")
        n = int((datetime.fromisoformat(end + "T00:00:00+00:00") - t0).total_seconds() // 3600) + 24
        return [{"time_utc": (t0 + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:00Z"), "wind_speed_kn": trade,
                 "wind_dir_from_deg": 65.0, "wind_gust_kn": None, "grid_lat": lat, "grid_lon": lon} for h in range(n)]

    monkeypatch.setattr(weather.OpenMeteo, "fetch", fake_fetch)
    monkeypatch.setattr(climate, "oni_table", lambda conn=None: {("OND", 2021): -0.91, ("ASO", 2026): 2.46})
    cfg.calls_per_minute = 100000
    cfg.skill_weighting = False
    conn = connect(":memory:")
    y = Yacht(name="X", mmsi="1", speed_table="oyster885_std", polar_factor=0.85)
    _, s = parse_destination("rubicon")
    _, d = parse_destination("english-harbour")
    dep = (utcnow() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    out = climate.run(conn, cfg, y, s, d, dep, dep + timedelta(days=16, hours=18), [2021], res=5.0,
                      motor_wind_below_kn=10, motor_speed_kn=8, motor_max_h=60, out_path=str(tmp_path / "f.json"),
                      forecast_models=["ecmwf", "gfs"], fade=(6, 12))
    hist = out["results"][0]
    fc = out["forecast"]["results"][0]
    assert out["forecast"]["status"] == "complete" and out["forecast"]["handover_day"][1] > 9
    assert fc["days"] < hist["days"]  # strong forecast trades for the first 8+ days beat the weak past year
    md = climate.markdown(out)
    assert "With the live forecast" in md and "| gfs |" in md
    pm = out["forecast"]["per_model"]
    assert pm["ecmwf"]["results"][0]["days"] < pm["gfs"]["results"][0]["days"]  # stronger forecast trades


def test_sea_from_wind_gives_a_following_sea_gain():
    from yachtspeed.speedtable import wave_factor

    class G:
        t0 = None

        def sample(self, lat, lon, t):
            return {"wind_speed_kn": 20.0, "wind_dir_from_deg": 60.0}

    w = climate.WithSea(G()).sample(20, -40, None)
    assert 2.3 < w["wave_height_m"] < 2.9 and w["wave_dir_from_deg"] == 60.0
    assert wave_factor(w["wave_height_m"], w["wave_dir_from_deg"], 240.0, w["wave_period_s"])[0] > 1.0  # running
    assert wave_factor(w["wave_height_m"], w["wave_dir_from_deg"], 60.0, w["wave_period_s"])[0] < 1.0  # into it


def test_forecast_only_uses_stored_history_without_downloading(tmp_path, monkeypatch, cfg):
    from yachtspeed.geo import utcnow

    kinds = []

    def fake_fetch(self, provider, kind, lat, lon, start, end):
        kinds.append(provider)
        if provider in ("om_marine", "om_era5_ocean"):
            return []
        t0 = datetime.fromisoformat(start + "T00:00:00+00:00")
        n = int((datetime.fromisoformat(end + "T00:00:00+00:00") - t0).total_seconds() // 3600) + 24
        trade = 16.0 if provider.startswith("om_model") else 14.0
        return [{"time_utc": (t0 + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:00Z"), "wind_speed_kn": trade,
                 "wind_dir_from_deg": 65.0, "wind_gust_kn": None, "grid_lat": lat, "grid_lon": lon} for h in range(n)]

    monkeypatch.setattr(weather.OpenMeteo, "fetch", fake_fetch)
    monkeypatch.setattr(climate, "oni_table", lambda conn=None: {("OND", 2021): -0.91, ("ASO", 2026): 2.46})
    cfg.calls_per_minute = 100000
    cfg.skill_weighting = False
    conn = connect(":memory:")
    y = Yacht(name="X", mmsi="1", speed_table="oyster885_std", polar_factor=0.85)
    _, s = parse_destination("rubicon")
    _, d = parse_destination("english-harbour")
    dep = (utcnow() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    climate.run(conn, cfg, y, s, d, dep, dep + timedelta(days=16, hours=18), [2021], res=5.0,
                motor_wind_below_kn=10, motor_speed_kn=8, motor_max_h=60, real_waves=False)  # stores 2021
    kinds.clear()
    out = climate.run(conn, cfg, y, s, d, dep, dep + timedelta(days=16, hours=18), [2021, 2022], res=5.0,
                      motor_wind_below_kn=10, motor_speed_kn=8, motor_max_h=60, forecast_models=["ecmwf"],
                      forecast_only=True, out_path=str(tmp_path / "fo.json"))
    assert "om_era5" not in kinds and out["years"] == [2021]
    assert out["forecast"]["status"] == "complete" and len(out["forecast"]["results"]) == 1


def test_fetch_elsewhere_then_merge(tmp_path, monkeypatch):
    from yachtspeed.db import connect as dbc

    def fake_fetch(self, provider, kind, lat, lon, start, end):
        t0 = datetime.fromisoformat(start + "T00:00:00+00:00")
        if provider == "om_era5_ocean":
            return [{"time_utc": (t0 + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:00Z"), "wave_height_m": 2.0,
                     "wave_dir_from_deg": 60.0, "wave_period_s": 8.0} for h in range(0, 48, 3)]
        return [{"time_utc": (t0 + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:00Z"), "wind_speed_kn": 15.0,
                 "wind_dir_from_deg": 60.0, "wind_gust_kn": None} for h in range(48)]

    monkeypatch.setattr(weather.OpenMeteo, "fetch", fake_fetch)
    _, s = parse_destination("rubicon")
    _, d = parse_destination("english-harbour")
    dep = datetime(2026, 11, 20, 10, tzinfo=timezone.utc)
    mac = dbc(str(tmp_path / "mac.db"))
    climate.fetch_only(mac, s, d, dep, [2017], res=5.0, progress=lambda m: None)
    server = dbc(str(tmp_path / "server.db"))
    added = climate.merge(server, str(tmp_path / "mac.db"))
    assert added["climate_wind"] > 0 and added["climate_cells"] > 0 and added["climate_wave_cells"] > 0
    assert climate.merge(server, str(tmp_path / "mac.db"))["climate_wind"] == 0  # idempotent


def test_strategies_compare_routes_in_forecast_and_past_weather(tmp_path, monkeypatch, cfg):
    from yachtspeed.geo import utcnow

    def fake_fetch(self, provider, kind, lat, lon, start, end):
        if provider in ("om_marine", "om_era5_ocean"):
            return []
        t0 = datetime.fromisoformat(start + "T00:00:00+00:00")
        n = int((datetime.fromisoformat(end + "T00:00:00+00:00") - t0).total_seconds() // 3600) + 24
        spd = (lambda la: 8.0 if la > 25 else 17.0)  # calm in the north, trades in the south
        return [{"time_utc": (t0 + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:00Z"), "wind_speed_kn": spd(lat),
                 "wind_dir_from_deg": 65.0, "wind_gust_kn": None, "grid_lat": lat, "grid_lon": lon} for h in range(n)]

    monkeypatch.setattr(weather.OpenMeteo, "fetch", fake_fetch)
    monkeypatch.setattr(climate, "oni_table", lambda conn=None: {("OND", 2021): -0.91, ("ASO", 2026): 2.46})
    cfg.calls_per_minute = 100000
    cfg.skill_weighting = False
    conn = connect(":memory:")
    y = Yacht(name="X", mmsi="1", speed_table="oyster885_std", polar_factor=0.85)
    _, s = parse_destination("rubicon")
    _, d = parse_destination("english-harbour")
    dep = (utcnow() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    by = dep + timedelta(days=16, hours=18)
    climate.run(conn, cfg, y, s, d, dep, by, [2021], res=5.0, motor_wind_below_kn=10, motor_speed_kn=8.5,
                motor_max_h=60, real_waves=False)  # store 2021's history
    out = climate.run_strategies(conn, cfg, y, s, d, dep, by, [2021], res=5.0, motor_wind_below_kn=10,
                                 motor_speed_kn=8.5, motor_max_h=60, burn_lph=15, models=("ecmwf",),
                                 out_path=str(tmp_path / "st.json"))
    assert out["status"] == "complete"
    for k in ("A", "B", "C", "D", "E"):
        assert out["strategies"][k]["results"] and out["strategies"][k]["history"]
        assert out["strategies"][k]["results"][0]["status"] == "ok"
    md = climate.strategies_markdown(out)
    assert "Year by year in past weather" in md and "| 2021 |" in md and "B vs A" in md and "E vs A" in md
    a, e = out["strategies"]["A"]["history"][0], out["strategies"]["E"]["history"][0]
    assert e["motor_hours"] <= 52 and e["sailing_pct"] >= a["sailing_pct"]
    assert (tmp_path / "st.jpg").exists()


def test_typical_run_ignores_a_southern_outlier():
    def run(days, lat_shift):
        t0 = datetime(2026, 11, 20, 10, tzinfo=timezone.utc)
        return {"status": "ok", "days": days,
                "track": [[(t0 + timedelta(hours=6 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"), 29 - 0.2 * i + lat_shift,
                           -14 - 0.9 * i, False] for i in range(50)]}

    runs = [run(13.5, 0.0), run(13.6, 0.3), run(13.4, -0.2), run(13.0, -6.0), run(13.8, 0.1)]
    assert climate.typical_run(runs)["days"] in (13.5, 13.8, 13.6)  # not the 13.0-day outlier
