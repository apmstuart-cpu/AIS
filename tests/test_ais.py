import json

import pytest

from yachtspeed.ais import ParseError, Position, Static, parse_message
from yachtspeed.collector import Store
from yachtspeed.geo import parse_aisstream_time

# Shapes follow the AISStream documentation examples.
CLASS_A = {
    "MessageType": "PositionReport",
    "MetaData": {"MMSI": 235000001, "MMSI_String": 235000001, "ShipName": "TESTBOAT    ",
                 "latitude": 39.51, "longitude": 2.62, "time_utc": "2026-06-01 12:34:56.987654321 +0000 UTC"},
    "Message": {"PositionReport": {
        "Cog": 181.3, "CommunicationState": 0, "Latitude": 39.51, "Longitude": 2.62, "MessageID": 1,
        "NavigationalStatus": 8, "PositionAccuracy": True, "Raim": False, "RateOfTurn": 0,
        "RepeatIndicator": 0, "Sog": 7.4, "Spare": 0, "SpecialManoeuvreIndicator": 0,
        "Timestamp": 55, "TrueHeading": 178, "UserID": 235000001, "Valid": True}},
}
CLASS_B = {
    "MessageType": "StandardClassBPositionReport",
    "MetaData": {"MMSI": 235000001, "latitude": 39.6, "longitude": 2.7,
                 "time_utc": "2026-06-01 12:44:00.1 +0000 UTC"},
    "Message": {"StandardClassBPositionReport": {
        "Cog": 360, "Latitude": 39.6, "Longitude": 2.7, "MessageID": 18, "Sog": 102.3,
        "Timestamp": 60, "TrueHeading": 511, "UserID": 235000001, "Valid": True, "PositionAccuracy": False}},
}
STATIC_24 = {
    "MessageType": "StaticDataReport",
    "MetaData": {"MMSI": 235000001, "ShipName": "ALIBI", "time_utc": "2026-06-01 12:00:00 +0000 UTC"},
    "Message": {"StaticDataReport": {
        "PartNumber": False, "ReportA": {"Name": "ALIBI@@@@", "Valid": True},
        "ReportB": {"CallSign": "", "ShipType": 0, "Dimension": {"A": 0, "B": 0, "C": 0, "D": 0}, "Valid": False},
        "UserID": 235000001, "Valid": True}},
}


def test_time_parsing_truncates_nanoseconds_and_is_utc():
    t = parse_aisstream_time("2026-06-01 12:34:56.987654321 +0000 UTC")
    assert t.isoformat() == "2026-06-01T12:34:56.987654+00:00"
    assert parse_aisstream_time("2026-06-01 14:00:00 +0200 CEST").hour == 12


def test_class_a():
    p = parse_message(CLASS_A)
    assert isinstance(p, Position)
    assert (p.mmsi, p.ais_class, p.sog_kn, p.cog_deg, p.heading_deg, p.nav_status, p.ais_second) == (
        "235000001", "A", 7.4, 181.3, 178, 8, 55)


def test_class_b_sentinels_become_none():
    p = parse_message(CLASS_B)
    assert p.ais_class == "B"
    assert p.sog_kn is None and p.cog_deg is None and p.heading_deg is None
    assert p.ais_second is None and p.nav_status is None


def test_invalid_position_rejected():
    bad = json.loads(json.dumps(CLASS_A))
    bad["Message"]["PositionReport"]["Latitude"] = 91
    with pytest.raises(ParseError):
        parse_message(bad)


def test_provider_error():
    with pytest.raises(ParseError):
        parse_message({"error": "Api Key Is Not Valid"})


def test_static_report_name_cleaned():
    s = parse_message(STATIC_24)
    assert isinstance(s, Static) and s.name == "ALIBI" and s.callsign is None


def test_store_dedups_by_10_minute_bucket_and_keeps_both_timestamps(conn, cfg):
    store = Store(conn, cfg)
    store.handle(json.dumps(CLASS_A))
    later = json.loads(json.dumps(CLASS_A))
    later["MetaData"]["time_utc"] = "2026-06-01 12:38:00 +0000 UTC"  # same 12:30 bucket
    store.handle(json.dumps(later))
    store.handle(json.dumps(CLASS_B))  # 12:40 bucket
    rows = conn.execute("SELECT * FROM ais_reports ORDER BY provider_time_utc").fetchall()
    assert [r["bucket_utc"] for r in rows] == ["2026-06-01T12:30:00Z", "2026-06-01T12:40:00Z"]
    assert rows[0]["provider_time_utc"] == "2026-06-01T12:34:56.987Z"  # first report wins, ms precision
    assert rows[0]["ingested_utc"] != rows[0]["provider_time_utc"]
    assert store.counts["duplicate_bucket"] == 1
    assert conn.execute("SELECT COUNT(*) FROM raw_samples").fetchone()[0] == 3
