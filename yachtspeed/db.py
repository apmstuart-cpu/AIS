"""SQLite storage. WAL mode so the collector and enricher can write concurrently."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .geo import iso, utcnow

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS vessels (
    mmsi TEXT PRIMARY KEY,
    name TEXT, model TEXT, verified INTEGER DEFAULT 0, notes TEXT,
    static_name TEXT, static_callsign TEXT, static_imo TEXT, static_ship_type INTEGER,
    static_length_m REAL, static_beam_m REAL, static_updated_utc TEXT
);

-- One row per vessel, source and 10-minute UTC bucket (first valid report wins).
CREATE TABLE IF NOT EXISTS ais_reports (
    id INTEGER PRIMARY KEY,
    mmsi TEXT NOT NULL,
    source TEXT NOT NULL,              -- 'aisstream' or 'csv:<file>'
    bucket_utc TEXT NOT NULL,          -- start of 10-minute bucket
    provider_time_utc TEXT NOT NULL,   -- provider's receive timestamp (AISStream MetaData.time_utc)
    ingested_utc TEXT NOT NULL,        -- when this service stored it
    ais_second INTEGER,                -- AIS 'Timestamp' field: UTC second of report, NULL if 60-63
    msg_type TEXT,                     -- PositionReport / StandardClassBPositionReport / ...
    ais_class TEXT,                    -- 'A' or 'B'
    lat REAL NOT NULL, lon REAL NOT NULL,
    sog_kn REAL, cog_deg REAL, heading_deg REAL,
    nav_status INTEGER,                -- Class A only; NULL for Class B
    position_accuracy INTEGER,
    UNIQUE (mmsi, source, bucket_utc)
);
CREATE INDEX IF NOT EXISTS ix_reports_time ON ais_reports(provider_time_utc);
CREATE INDEX IF NOT EXISTS ix_reports_mmsi_time ON ais_reports(mmsi, provider_time_utc);

-- A capped sample of raw provider payloads, kept for parser verification.
CREATE TABLE IF NOT EXISTS raw_samples (
    id INTEGER PRIMARY KEY, ingested_utc TEXT, day TEXT, msg_type TEXT, mmsi TEXT, payload TEXT
);
CREATE INDEX IF NOT EXISTS ix_raw_day_type ON raw_samples(day, msg_type);

-- Hourly weather by grid cell. kind: 'wind' or 'marine'.
CREATE TABLE IF NOT EXISTS weather_cache (
    provider TEXT NOT NULL, kind TEXT NOT NULL,
    cell_lat REAL NOT NULL, cell_lon REAL NOT NULL, time_utc TEXT NOT NULL,
    grid_lat REAL, grid_lon REAL, fetched_utc TEXT,
    wind_speed_kn REAL, wind_dir_from_deg REAL, wind_gust_kn REAL,
    wave_height_m REAL, wave_dir_from_deg REAL, wave_period_s REAL,
    current_speed_kn REAL, current_dir_to_deg REAL,
    PRIMARY KEY (provider, kind, cell_lat, cell_lon, time_utc)
);

-- One row per (provider, kind, cell, UTC date) request, with retry state.
CREATE TABLE IF NOT EXISTS weather_fetches (
    provider TEXT NOT NULL, kind TEXT NOT NULL,
    cell_lat REAL NOT NULL, cell_lon REAL NOT NULL, date TEXT NOT NULL,
    status TEXT NOT NULL,              -- ok / retry / failed
    attempts INTEGER DEFAULT 0, last_error TEXT, updated_utc TEXT, next_retry_utc TEXT,
    PRIMARY KEY (provider, kind, cell_lat, cell_lon, date)
);

CREATE TABLE IF NOT EXISTS enrichment (
    report_id INTEGER PRIMARY KEY REFERENCES ais_reports(id),
    status TEXT NOT NULL,              -- matched / partial / missing
    weather_hour_utc TEXT, cell_lat REAL, cell_lon REAL,
    wind_provider TEXT, marine_provider TEXT,
    wind_speed_kn REAL, wind_dir_from_deg REAL, wind_gust_kn REAL,
    wave_height_m REAL, wave_dir_from_deg REAL, wave_period_s REAL,
    current_speed_kn REAL, current_dir_to_deg REAL,
    missing TEXT,                      -- comma-separated list of missing fields
    matched_utc TEXT
);

CREATE TABLE IF NOT EXISTS service_events (
    id INTEGER PRIMARY KEY, ts_utc TEXT, service TEXT, event TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_events ON service_events(service, event, ts_utc);

CREATE TABLE IF NOT EXISTS heartbeats (service TEXT PRIMARY KEY, ts_utc TEXT, detail TEXT);

CREATE TABLE IF NOT EXISTS api_usage (
    host TEXT, period TEXT, calls INTEGER DEFAULT 0, PRIMARY KEY (host, period)
);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

-- Live predictions keep issue and valid time so lead-time accuracy can be measured.
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY,
    created_utc TEXT, issue_utc TEXT, valid_utc TEXT, lead_hours REAL,
    mmsi TEXT, lat REAL, lon REAL, course_deg REAL,
    wind_provider TEXT, marine_provider TEXT,
    wind_speed_kn REAL, wind_dir_from_deg REAL, twa_deg REAL,
    wave_height_m REAL, along_current_kn REAL,
    status TEXT,                       -- ok / insufficient_support / no_forecast
    n_matches INTEGER, sog_median_kn REAL, sog_p25_kn REAL, sog_p75_kn REAL,
    sog_p10_kn REAL, sog_p90_kn REAL,
    distance_nm REAL, hours_at_speed REAL,
    passage_id TEXT, segment_idx INTEGER,
    model_version TEXT
);
CREATE INDEX IF NOT EXISTS ix_pred_mmsi_valid ON predictions(mmsi, valid_utc);
"""


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
    )
    return conn


def sync_fleet(conn, fleet) -> None:
    for y in fleet:
        if not y.mmsi:
            continue
        conn.execute(
            """INSERT INTO vessels(mmsi, name, model, verified, notes) VALUES (?,?,?,?,?)
               ON CONFLICT(mmsi) DO UPDATE SET name=excluded.name, model=excluded.model,
               verified=excluded.verified, notes=excluded.notes""",
            (y.mmsi, y.name, y.model, int(y.verified), y.notes),
        )


def log_event(conn, service: str, event: str, detail=None) -> None:
    conn.execute(
        "INSERT INTO service_events(ts_utc, service, event, detail) VALUES (?,?,?,?)",
        (iso(utcnow()), service, event, json.dumps(detail) if detail is not None else None),
    )


def heartbeat(conn, service: str, detail=None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO heartbeats(service, ts_utc, detail) VALUES (?,?,?)",
        (service, iso(utcnow()), json.dumps(detail) if detail is not None else None),
    )


def kv_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def kv_set(conn, key, value) -> None:
    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?,?)", (key, value))
