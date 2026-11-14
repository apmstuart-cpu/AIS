import struct

import pytest

from yachtspeed.collector import Store
from yachtspeed.yb import decode_positions, find_team, import_team, sync_configured, with_derived_motion


def encode(teams, ref=1_700_000_000, flags=2):
    """Inverse of decode_positions for tests: teams = {id: [(epoch, lat, lon), ...] oldest first}."""
    out = bytearray(struct.pack(">BI", flags, ref))
    for tid, pts in teams.items():
        pts = list(reversed(pts))  # newest first on the wire
        out += struct.pack(">HH", tid, len(pts))
        prev = None
        for at, lat, lon in pts:
            la, lo = round(lat * 1e5), round(lon * 1e5)
            if prev and prev[0] - at < 0x8000 and abs(la - prev[1]) < 32000 and abs(lo - prev[2]) < 32000:
                out += struct.pack(">Hhh", 0x8000 | (prev[0] - at), la - prev[1], lo - prev[2]) + struct.pack(">h", 0)
            else:
                out += struct.pack(">Iii", at - ref, la, lo) + struct.pack(">i", 0)
            prev = (at, la, lo)
    return bytes(out)


TRACK = [(1_700_000_000 + i * 3600, 28.1 - i * 0.1, -15.4 - i * 0.1) for i in range(6)]


def test_roundtrip_absolute_and_delta():
    d = decode_positions(encode({7: TRACK, 9: TRACK[:2]}))
    assert list(d) == [7, 9] and len(d[7]) == 6
    assert d[7][0]["at"] == TRACK[0][0] and d[7][-1]["lat"] == pytest.approx(27.6)


def test_derived_motion():
    pts = with_derived_motion(decode_positions(encode({1: TRACK}))[1])
    assert pts[0]["sog_kn"] is None
    assert 7.8 < pts[1]["sog_kn"] < 8.2  # 6 nm south + 5.3 nm west = ~8.0 nm in 1 h
    assert 220 < pts[1]["cog_deg"] < 230  # heading SW


def test_long_gaps_give_no_speed():
    pts = with_derived_motion([{"at": 0, "lat": 0, "lon": 0}, {"at": 7 * 3600, "lat": 1, "lon": 0}])
    assert pts[1]["sog_kn"] is None


def test_find_team_is_forgiving():
    teams = [{"id": 1, "name": "Sorceress"}, {"id": 2, "name": "Sorcerer II"}, {"id": 3, "name": "Alibi of London"}]
    assert find_team(teams, "SORCERESS")["id"] == 1
    assert find_team(teams, "alibi-of-london")["id"] == 3
    assert find_team(teams, "Sorc") is None  # ambiguous


def test_import_and_sync(conn, cfg):
    cfg.fleet[0].yb_race = "arcX"

    class Fake:
        def __init__(self, teams):
            self._teams = teams

        def teams(self, race):
            return self._teams

        def positions(self, race):
            return decode_positions(encode({5: TRACK}))

    r = sync_configured(conn, cfg, Fake([{"id": 1, "name": ".Waiting for teams"}]))
    assert r["TESTBOAT"]["status"] == "team_not_listed_yet"
    r = sync_configured(conn, cfg, Fake([{"id": 5, "name": "TestBoat"}]))
    assert r["TESTBOAT"]["stored"] == 6
    again = sync_configured(conn, cfg, Fake([{"id": 5, "name": "TestBoat"}]))
    assert again["TESTBOAT"]["stored"] == 0 and again["TESTBOAT"]["duplicate_bucket"] == 6
    row = conn.execute("SELECT source, msg_type, sog_kn FROM ais_reports ORDER BY provider_time_utc DESC").fetchone()
    assert row["source"] == "yb:arcX" and row["msg_type"] == "yb" and row["sog_kn"] > 7


def test_dense_fixes_measured_against_fix_10_min_back():
    pts = [{"at": i * 120, "lat": 28.0 + i * 0.002, "lon": -15.0} for i in range(12)]  # every 2 min
    out = with_derived_motion(pts)
    assert all(p["sog_kn"] is None for p in out[:5])  # nothing 10 min back yet
    assert all(p["sog_kn"] is not None and 3.4 < p["sog_kn"] < 3.8 for p in out[5:])  # 0.12 nm per 2 min = 3.6 kn
