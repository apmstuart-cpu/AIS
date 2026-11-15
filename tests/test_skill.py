from datetime import datetime, timedelta, timezone

from yachtspeed import skill
from yachtspeed.db import connect
from yachtspeed.geo import iso, utcnow


def test_weighted_median_equal_is_true_median():
    t = datetime(2026, 11, 17, tzinfo=timezone.utc)
    items = [(t + timedelta(hours=h), 0.25) for h in (0, 1, 3, 10)]
    assert skill.weighted_median_time(items) == t + timedelta(hours=2)
    items = [(t, 0.1), (t + timedelta(hours=1), 0.1), (t + timedelta(hours=5), 0.8)]
    assert skill.weighted_median_time(items) == t + timedelta(hours=5)


def test_bucket_weights_band_and_fallback():
    models = ["ecmwf", "aifs", "gfs", "icon"]
    w = skill._bucket_weights({"ecmwf": (50, 2.0), "aifs": (50, 2.2), "gfs": (50, 6.0), "icon": (50, 3.0)},
                              models, 12)
    assert abs(sum(w.values()) - 1) < 1e-9
    assert all(0.15 - 1e-9 <= x <= 0.35 + 1e-9 for x in w.values())
    assert w["ecmwf"] > w["icon"] > w["gfs"]
    eq = skill._bucket_weights({"ecmwf": (3, 2.0)}, models, 12)  # too few checks: equal
    assert all(abs(x - 0.25) < 1e-9 for x in eq.values())


class FakeClient:
    """Previous-runs API stub: truth 10 kn from 000; gfs is 4 kn too strong, others exact."""

    def _get(self, key, params):
        day = params["start_date"]
        times = [f"{day}T{h:02d}:00" for h in range(24)]
        bias = 4.0 if params["models"].startswith("gfs") else 0.0
        h = {"time": times, "wind_speed_10m": [10.0] * 24, "wind_direction_10m": [0.0] * 24}
        for n in (1, 2, 3):
            h[f"wind_speed_10m_previous_day{n}"] = [10.0 + bias] * 24
            h[f"wind_direction_10m_previous_day{n}"] = [0.0] * 24
        return {"hourly": h}


def test_verify_day_scores_and_weights(cfg):
    conn = connect(":memory:")
    day = (utcnow() - timedelta(days=2)).date().isoformat()
    for hh in (0, 6, 12, 18):
        conn.execute("INSERT INTO ais_reports(mmsi, source, bucket_utc, provider_time_utc, ingested_utc, lat, lon) "
                     "VALUES (?,?,?,?,?,?,?)", (cfg.mmsis[0], "t", f"{day}T{hh:02d}:00:00Z", f"{day}T{hh:02d}:05:00Z",
                                                 iso(utcnow()), 30.1, -12.0))
    st = skill.verify_day(conn, FakeClient(), cfg, day)
    assert st["points"] == 4 and st["checks"] == 4 * 4 * 3
    sc = skill.scores(conn)
    assert sc[1]["gfs"][1] > 2.5 and sc[1]["ecmwf"][1] < 1.5
    w = skill.Weights(conn, ["ecmwf", "aifs", "gfs", "icon"], min_checks=4)
    assert not w.equal and w.at_lead(30)["gfs"] < 0.25 < w.at_lead(30)["ecmwf"]


def test_review_list_skips_screenshots_captured_by_hand(tmp_path, monkeypatch):
    from yachtspeed import screenshots

    conn = connect(":memory:")
    now = iso(utcnow())
    for f in ("a.png", "b.png"):
        conn.execute("INSERT INTO ocr_files(file, processed_utc, status, notes) VALUES (?,?,?,?)",
                     (f, now, "review", "coordinates not read"))
    csvp = tmp_path / "fixes.csv"
    csvp.write_text("mmsi,timestamp_utc,lat,lon,screenshot\n1,2026-11-14T00:00:00Z,1,2,a.png\n")
    monkeypatch.setattr(screenshots, "MANUAL_CSV", csvp)
    monkeypatch.setattr(screenshots.manual_files, "__defaults__", (csvp,))
    assert [r["file"] for r in screenshots.review_list(conn, 24)] == ["b.png"]


def test_ocr_reads_three_decimal_and_degree_minute_coords():
    from yachtspeed.ocr import parse_decimal_coords

    assert parse_decimal_coords("32° 38.64 N\n9° 32.00 W\n(32.644, -9.533386)") == (32.644, -9.533386)
    assert parse_decimal_coords("32° 38.64 N\n9° 32.00 W") == (32.644, -9.533333)
