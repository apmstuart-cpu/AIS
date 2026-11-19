from datetime import timedelta
from types import SimpleNamespace

from yachtspeed import cli
from yachtspeed.geo import iso, utcnow


def _add_fix(conn, lat, lon, t):
    conn.execute("INSERT INTO ais_reports (mmsi, source, bucket_utc, provider_time_utc, ingested_utc, lat, lon) "
                 "VALUES ('235000001', 'test', ?, ?, ?, ?, ?)", (iso(t), iso(t), iso(t), lat, lon))
    conn.commit()


def _run(cfg, capsys, planned):
    a = SimpleNamespace(yacht="TESTBOAT", frm="rubicon", to="english-harbour", depart=planned.isoformat(),
                        max_age_h=12.0, harbour_nm=5.0, arrived_nm=10.0)
    cli.cmd_passage_start(cfg, a)
    return dict(line.split("=", 1) for line in capsys.readouterr().out.strip().splitlines())


def test_before_departure_routes_from_the_planned_port(cfg, conn, capsys):
    planned = (utcnow() + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    out = _run(cfg, capsys, planned)
    assert out["MODE"] == "planned" and out["FROM"] == "'rubicon'" and out["DEPART"].startswith(planned.strftime("%Y-%m-%dT%H"))


def test_under_way_routes_from_her_latest_position(cfg, conn, capsys):
    now = utcnow()
    _add_fix(conn, 28.86, -13.82, now - timedelta(hours=30))   # in Rubicón, before leaving
    _add_fix(conn, 24.5, -30.25, now - timedelta(hours=2))     # at sea
    out = _run(cfg, capsys, now - timedelta(days=2))
    assert out["MODE"] == "underway" and out["FROM"] == "'24.500,-30.250'"


def test_still_in_harbour_after_the_planned_time_waits(cfg, conn, capsys):
    now = utcnow()
    _add_fix(conn, 28.86, -13.82, now - timedelta(minutes=30))
    out = _run(cfg, capsys, now - timedelta(hours=3))
    assert out["MODE"] == "planned" and out["FROM"] == "'rubicon'"


def test_arrived_does_nothing(cfg, conn, capsys):
    now = utcnow()
    _add_fix(conn, 17.0, -61.76, now - timedelta(hours=1))
    assert _run(cfg, capsys, now - timedelta(days=14))["MODE"] == "arrived"
