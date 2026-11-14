import asyncio
import json
from datetime import timedelta

import pytest

from yachtspeed import db
from yachtspeed.backup import backup, restore
from yachtspeed.evaluate import evaluate, splits
from yachtspeed.forecast import passage_forecast, point_forecast
from yachtspeed.geo import utcnow
from yachtspeed.io_csv import export_csv, import_csv
from yachtspeed.collector import Store
from yachtspeed.model import SogModel, load_observations
from yachtspeed.status import format_text, status
from yachtspeed.synthetic import generate
from yachtspeed.weather import Budget, OpenMeteo

from conftest import FakeSession, om_body


@pytest.fixture
def synth(conn, cfg):
    generate(conn, mmsi="235000001", voyages=12, hours_per_voyage=20)
    return load_observations(conn, cfg, "235000001")


def test_abstains_below_min_matches(cfg, synth):
    m = SogModel(cfg, synth[:5])
    assert m.predict("235000001", 15, 90, 1.0).status == "insufficient_support"
    assert SogModel(cfg, synth).predict("235000001", 15, 90, 1.0).status == "ok"


def test_current_is_removed_and_reapplied(cfg, synth):
    m = SogModel(cfg, synth)
    a = m.predict("235000001", 15, 90, 1.0, 0.0)
    b = m.predict("235000001", 15, 90, 1.0, 1.5)
    assert b.median - a.median == pytest.approx(1.5)


def test_splits_hold_out_whole_voyages(synth):
    for train, test in splits(synth, "voyage", k=4):
        assert not ({o.voyage for o in train} & {o.voyage for o in test})
    train, test = next(splits(synth, "chrono"))
    assert max(o.time_utc for o in train) < min(o.time_utc for o in test)


def test_evaluate_beats_baseline_on_synthetic(cfg, synth):
    r = evaluate(cfg, synth, "voyage", k=4)
    assert r["scored_rows"] > 0 and r["mae_model_kn"] < r["mae_baseline_kn"]
    assert 0 < r["share_scored"] <= 1


def _client(conn):
    return OpenMeteo(Budget(conn, 100, 1000, 5000), FakeSession(lambda u, p: (200, om_body(p))))


def test_point_forecast_stores_issue_and_valid_time(conn, cfg, synth):
    when = (utcnow() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    # wind from 270, course 0 -> TWA 90; current to 90 -> along-course 0
    r = point_forecast(conn, cfg, _client(conn), SogModel(cfg, synth), "235000001", 39.5, 2.6, when, 0.0, 40)
    assert r["status"] == "ok" and r["twa_deg"] == 90 and r["hours_at_speed"] > 0
    assert 47 < r["lead_hours"] <= 48
    p = conn.execute("SELECT * FROM predictions").fetchone()
    assert p["issue_utc"] < p["valid_utc"] and p["model_version"]


def test_point_forecast_reports_insufficient_support(conn, cfg, synth):
    r = point_forecast(conn, cfg, _client(conn), SogModel(cfg, synth[:3]), "235000001", 39.5, 2.6,
                       utcnow() + timedelta(hours=5), 0.0)
    assert r["status"] == "insufficient_support"


def test_passage_propagates_time(conn, cfg, synth):
    dep = utcnow() + timedelta(hours=6)
    r = passage_forecast(conn, cfg, _client(conn), SogModel(cfg, synth), "235000001",
                         [(39.5, 2.6), (40.0, 2.6), (40.0, 3.2)], dep, step_nm=10)
    assert r["distance_nm"] == pytest.approx(30 + 27.6, abs=1.5)
    starts = [s["start_utc"] for s in r["steps"]]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)
    assert r["duration_hours"] == pytest.approx(sum(s["hours"] for s in r["steps"]), abs=0.05)


def test_csv_roundtrip(conn, cfg, tmp_path):
    src = tmp_path / "hist.csv"
    src.write_text("mmsi,timestamp_utc,lat,lon,sog_kn,cog_deg,heading_deg,nav_status\n"
                   "235000001,2025-07-01T10:01:00Z,39.5,2.6,6.5,90,511,8\n"
                   "235000001,2025-07-01T10:05:00Z,39.5,2.61,6.6,90,,8\n"
                   "235000001,2025-07-01T10:12:00,39.5,2.62,102.3,360,,\n"
                   "235000001,bad,39.5,2.62,1,1,1,1\n")
    s = import_csv(Store(conn, cfg), str(src))
    assert s == {"rows": 4, "stored": 2, "duplicate_bucket": 1, "rejected": 1}
    out = tmp_path / "out.csv"
    assert export_csv(conn, str(out), "unmatched") == 2
    lines = out.read_text().splitlines()
    assert lines[2].split(",")[12] == ""  # SOG sentinel exported as empty, not 102.3


def test_backup_restore(conn, cfg, synth, tmp_path):
    n = conn.execute("SELECT COUNT(*) FROM ais_reports").fetchone()[0]
    path = backup(conn, cfg.db_path, cfg.backup_dir, keep=2)
    conn.execute("DELETE FROM enrichment WHERE report_id > 10")
    conn.execute("DELETE FROM ais_reports WHERE id > 10")
    conn.close()
    restore(str(path), cfg.db_path)
    c2 = db.connect(cfg.db_path)
    assert c2.execute("SELECT COUNT(*) FROM ais_reports").fetchone()[0] == n


def test_status_text(conn, cfg, synth):
    db.heartbeat(conn, "collector", {"messages": 3})
    txt = format_text(status(conn, cfg))
    assert "TESTBOAT 235000001" in txt and "heartbeat" in txt


def test_collector_reconnects_after_server_drop(conn, cfg, monkeypatch):
    """A local websocket server sends one report then drops; the collector must reconnect."""
    import websockets
    from yachtspeed import collector
    from test_ais import CLASS_A, CLASS_B

    monkeypatch.setenv("AISSTREAM_API_KEY", "dummy")
    connections, subs = [], []

    async def handler(ws):
        subs.append(json.loads(await ws.recv()))
        connections.append(1)
        await ws.send(json.dumps(CLASS_A if len(connections) == 1 else CLASS_B))
        if len(connections) == 1:
            await ws.close()
        else:
            await asyncio.sleep(0.2)
            stop.set()
            await asyncio.sleep(5)

    async def run():
        global stop
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setattr(collector, "AISSTREAM_URL", f"ws://127.0.0.1:{port}")
            monkeypatch.setattr(collector.random, "random", lambda: 0.0)
            await asyncio.wait_for(collector.run_collector(cfg, conn, stop), timeout=20)

    global stop
    stop = asyncio.Event()
    asyncio.run(run())
    assert len(connections) == 2
    assert subs[0]["FiltersShipMMSI"] == ["235000001"] and subs[0]["APIKey"] == "dummy"
    assert conn.execute("SELECT COUNT(*) FROM ais_reports").fetchone()[0] == 2
    events = [r[0] for r in conn.execute("SELECT event FROM service_events WHERE service='collector'")]
    assert events.count("connected") == 2 and "disconnect" in events
