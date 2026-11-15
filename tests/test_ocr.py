from datetime import datetime, timedelta, timezone

import pytest

from yachtspeed.ocr import (OcrFix, _match_yacht, parse_ago, parse_app_coords, parse_app_time, parse_clock,
                            parse_decimal_coords, parse_label)

NAMES = ["ALIBI OF LONDON", "SORCERESS", "DAKOTA"]


def test_parse_label_and_ago():
    assert parse_label("DAKOTA [JM] at 9.1 kn / 241°") == ("DAKOTA", 9.1, 241.0)
    assert parse_label("SORCERESS [MH] at 4.3 kn/ 188°")[1:] == (4.3, 188.0)
    assert parse_ago("Position received: 1 hour, 49 minutes ago") == timedelta(hours=1, minutes=49)
    assert parse_ago("Position received: 1 hor, 19 minutes ag0") == timedelta(hours=1, minutes=19)
    assert parse_ago("Position received: 9 minutes ago") == timedelta(minutes=9)
    assert parse_ago("no time here") is None


def test_parse_coords():
    assert parse_decimal_coords("(32.003418, -11.274719)") == (32.003418, -11.274719)
    assert parse_decimal_coords("(0.0, 0.0)") is None
    assert parse_app_coords("32.9172° N, 9.4957° W") == (32.9172, -9.4957)


def test_parse_times():
    near = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
    assert parse_clock("Sun Nov 15 11:51 AM", near) == datetime(2026, 11, 15, 11, 51, tzinfo=timezone.utc)
    assert parse_clock("Sat Nov 14 9:57 PM", datetime(2026, 11, 14, 22, 0, tzinfo=timezone.utc)).hour == 21
    assert parse_clock("Sun Nov 15 12:53 AM", datetime(2026, 11, 15, 1, 0, tzinfo=timezone.utc)).minute == 53
    assert parse_clock("23:57", datetime(2026, 11, 16, 0, 0, tzinfo=timezone.utc)).day == 15  # rolls back a day
    assert parse_clock("07:10", near) is None  # too far from the file-name time
    assert parse_app_time("Data received 15 Nov 12:27 UTC from Roaming", 2026) == \
        datetime(2026, 11, 15, 12, 27, tzinfo=timezone.utc)


def test_match_yacht():
    assert _match_yacht("Alibi of London", NAMES) == "ALIBI OF LONDON"
    assert _match_yacht("‘ALIBI OF LONDON [6st", NAMES) == "ALIBI OF LONDON"
    assert _match_yacht("Sorceress", NAMES) == "SORCERESS"
    assert _match_yacht("Unknown boat", NAMES) is None


def test_screenshot_pipeline_stores_dedups_and_flags(conn, cfg, tmp_path, monkeypatch):
    from yachtspeed import ocr, screenshots

    cfg.screenshots_url = "https://example.invalid/folder?dl=0"
    t = datetime(2026, 11, 15, 11, 50, tzinfo=timezone.utc)
    names = ["a.png", "b.png", "c.png"]
    results = {
        "a.png": OcrFix("a.png", "ok", "TESTBOAT", t, 32.0, -11.27, 9.1, 241.0),
        "b.png": OcrFix("b.png", "ok", "TESTBOAT", t + timedelta(minutes=2), 32.001, -11.271, 9.1, 241.0),  # same fix
        "c.png": OcrFix("c.png", "review", "TESTBOAT", None, None, None, 9.1, 241.0, ["coordinates not read"]),
    }

    def fake_download(url, dest):
        assert "dl=1" in screenshots._zip_url(url)
        out = []
        for n in names:
            (dest / n).write_bytes(b"x")
            out.append(dest / n)
        return out

    monkeypatch.setattr(screenshots, "download", fake_download)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "read_screenshot", lambda path, fleet: results[path.rsplit("/", 1)[-1]])
    r = screenshots.run(conn, cfg)
    assert (r["new"], r["stored"], r["duplicates"], r["review"]) == (3, 1, 1, 1)
    assert screenshots.run(conn, cfg)["new"] == 0  # already processed
    assert [x["file"] for x in screenshots.review_list(conn)] == ["c.png"]
    row = conn.execute("SELECT source, sog_kn FROM ais_reports").fetchone()
    assert tuple(row) == ("ocr:marinetraffic", 9.1)


def test_parse_absolute_received_time():
    from yachtspeed.ocr import parse_received_abs

    assert parse_received_abs("Position received: 2026-11-14 00:53 UTC zx)") == \
        datetime(2026, 11, 14, 0, 53, tzinfo=timezone.utc)
    assert parse_received_abs("Received: 2026-11-14 00:53 UTC (AIS s") is not None
    assert parse_received_abs("Position received: 9 minutes ago") is None
