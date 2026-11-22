import json
from datetime import datetime, timedelta, timezone

from yachtspeed import fcmaps, weather
from yachtspeed.config import Config
from yachtspeed.db import connect
from yachtspeed.geo import iso, utcnow


def test_forecast_maps_draw_four_days(tmp_path, monkeypatch):
    def fake_fetch(self, provider, kind, lat, lon, s, e):
        t0 = datetime.fromisoformat(s + "T00:00:00+00:00")
        n = int((datetime.fromisoformat(e + "T00:00:00+00:00") - t0).total_seconds() // 3600) + 24
        return [{"time_utc": (t0 + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:00Z"),
                 "wind_speed_kn": 6.0 + (28.9 - lat) * 1.5 + h % 5, "wind_dir_from_deg": 40.0,
                 "wind_gust_kn": None, "grid_lat": lat, "grid_lon": lon} for h in range(n)]

    monkeypatch.setattr(weather.OpenMeteo, "fetch", fake_fetch)
    conn = connect(":memory:")
    cfg = Config()
    cfg.skill_weighting = False
    client = weather.OpenMeteo(weather.Budget(conn, 10 ** 6, 10 ** 6, 10 ** 6))
    dep = (utcnow() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    track = [[iso(dep + timedelta(hours=6 * i)), 28.86 - 0.3 * i, -13.8 - 0.9 * i, False] for i in range(30)]
    sj = tmp_path / "s.json"
    sj.write_text(json.dumps({"strategies": {"A": {"results": [{"status": "ok", "days": 13.0, "track": track}]}},
                              "issued_utc": iso(utcnow()), "settings": {"table": "oyster885_std", "polar_factor": 0.85}}))
    info = fcmaps.build(conn, cfg, client, (28.857, -13.817), dep, tmp_path, "t", days=2, step_h=6, res=2.0,
                        strategies_json=str(sj), keys=("A",))
    assert info["status"] == "ok" and len(info["files"]) == 2
    md = fcmaps.assumptions_md(info, cfg, str(sj))
    assert "Forecast models" in md and "Wind maps" in md


def test_assumptions_name_the_years_and_top_enso_years(tmp_path):
    import json
    cl = tmp_path / "c.json"
    cl.write_text(json.dumps({"years": [1997] + list(range(2006, 2026)),
                              "enso": {"now": {"oni": 2.46, "season": "ASO 2026"},
                                       "years": {"1997": {"weight": 0.993}, "2015": {"weight": 0.96},
                                                 "2007": {"weight": 0.1}}}}))
    txt = fcmaps.assumptions_md({}, None, None, str(cl))
    assert "21 past years" in txt and "1997, 2006-2025" in txt and "1997 (0.993)" in txt and "2007" not in txt


def test_passage_pdf_needs_its_inputs(tmp_path):
    from yachtspeed import passage_pdf

    assert passage_pdf.build_if_stale(tmp_path) == []  # nothing published yet: nothing built
    assert passage_pdf.fmt_dt(datetime(2026, 12, 3, 10, 22), "es") == "jue 03 dic 10:22"


def test_rdp_keeps_a_closed_island_outline():
    ring = [(0, 0), (0.05, 0.01), (0.1, 0.0), (0.1, 0.1), (0.05, 0.12), (0, 0.1), (0, 0)]
    far = 3
    s = fcmaps._rdp(ring[:far + 1], 0.005)[:-1] + fcmaps._rdp(ring[far:], 0.005)
    assert len(s) >= 4 and s[0] == s[-1]


def test_wind_export_keeps_each_model_and_past_years(tmp_path, monkeypatch):
    """The viewer re-weights the forecast models and can drop the fade, so the export keeps each model's wind and
    the past-years mean alongside the blend, cell for cell."""
    def fake_fetch(self, provider, kind, lat, lon, s, e):
        t0 = datetime.fromisoformat(s + "T00:00:00+00:00")
        n = int((datetime.fromisoformat(e + "T00:00:00+00:00") - t0).total_seconds() // 3600) + 24
        base = 20.0 if "ecmwf" in provider else 10.0
        return [{"time_utc": (t0 + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:00Z"), "wind_speed_kn": base,
                 "wind_dir_from_deg": 60.0, "wind_gust_kn": None, "wave_height_m": 2.0, "wave_dir_from_deg": 60.0,
                 "wave_period_s": 8.0, "grid_lat": lat, "grid_lon": lon} for h in range(n)]

    monkeypatch.setattr(weather.OpenMeteo, "fetch", fake_fetch)
    conn = connect(":memory:")
    cfg = Config()
    cfg.skill_weighting = False
    client = weather.OpenMeteo(weather.Budget(conn, 10 ** 6, 10 ** 6, 10 ** 6))
    dep = (utcnow() + timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    out = tmp_path / "w.json"
    fcmaps.export_json(conn, cfg, client, (20.0, -30.0), (18.0, -40.0), dep, out, days=1, step_h=6,
                       models=("ecmwf", "gfs"), waves=False, progress=lambda m: None)
    d = json.loads(out.read_text())
    n = len(d["times"]) * len(d["lats"]) * len(d["lons"])
    assert set(d["model_wind"]) == {"ecmwf", "gfs"}
    assert len(d["model_wind"]["ecmwf"]["speed_kn"]) == n and len(d["hist_wind"]["speed_kn"]) == n
    assert d["model_wind"]["ecmwf"]["speed_kn"][0] == 20.0 and d["model_wind"]["gfs"]["speed_kn"][0] == 10.0
    assert d["speed_kn"][0] == 15.0  # the blend is their (equal-weight) mean
