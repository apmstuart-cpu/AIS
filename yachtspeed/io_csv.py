"""CSV import (historical AIS) and export (enriched / unmatched rows).

Import format (header row required; extra columns ignored):
    mmsi,timestamp_utc,lat,lon,sog_kn,cog_deg,heading_deg,nav_status
timestamp_utc is ISO-8601 (Z or offset; naive values are taken as UTC). Empty cells and
AIS sentinels (SOG 102.3, COG 360, heading 511) become NULL. Rows are bucketed and
de-duplicated exactly like live data, under source 'csv:<label>'.
"""

from __future__ import annotations

import csv
from pathlib import Path

from .ais import Position, clean_cog, clean_heading, clean_sog, valid_latlon
from .geo import parse_iso

EXPORT_COLS = [
    "report_id", "mmsi", "vessel_name", "source", "provider_time_utc", "ingested_utc", "ais_second",
    "bucket_utc", "msg_type", "ais_class", "lat", "lon", "sog_kn", "cog_deg", "heading_deg", "nav_status",
    "enrichment_status", "weather_hour_utc", "wind_provider", "marine_provider", "wind_speed_kn",
    "wind_dir_from_deg", "wind_gust_kn", "wave_height_m", "wave_dir_from_deg", "wave_period_s",
    "current_speed_kn", "current_dir_to_deg", "missing_fields",
]


def _f(v):
    v = (v or "").strip()
    return float(v) if v else None


def import_csv(store, path: str, label: str | None = None) -> dict:
    source = f"csv:{label or Path(path).name}"
    stats = {"rows": 0, "stored": 0, "duplicate_bucket": 0, "rejected": 0}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            stats["rows"] += 1
            try:
                lat, lon = _f(row.get("lat")), _f(row.get("lon"))
                if not valid_latlon(lat, lon):
                    raise ValueError("bad position")
                nav = _f(row.get("nav_status"))
                p = Position(
                    mmsi=str(int(float(row["mmsi"]))).zfill(9), msg_type="csv", ais_class=row.get("ais_class") or None,
                    provider_time=parse_iso(row["timestamp_utc"]), lat=lat, lon=lon,
                    sog_kn=clean_sog(_f(row.get("sog_kn"))), cog_deg=clean_cog(_f(row.get("cog_deg"))),
                    heading_deg=clean_heading(_f(row.get("heading_deg"))),
                    nav_status=int(nav) if nav is not None else None, ais_second=None, position_accuracy=None,
                )
            except (KeyError, ValueError, TypeError):
                stats["rejected"] += 1
                continue
            if store.store_position(p, source=source):
                stats["stored"] += 1
            else:
                stats["duplicate_bucket"] += 1
    return stats


def export_csv(conn, path: str, which="enriched", mmsi=None, since=None, until=None) -> int:
    """which: 'enriched' (matched+partial), 'unmatched' (missing or not yet enriched), 'all'."""
    q = """SELECT r.id AS report_id, r.mmsi, v.name AS vessel_name, r.source, r.provider_time_utc,
              r.ingested_utc, r.ais_second, r.bucket_utc, r.msg_type, r.ais_class, r.lat, r.lon, r.sog_kn,
              r.cog_deg, r.heading_deg, r.nav_status, COALESCE(e.status, 'pending') AS enrichment_status,
              e.weather_hour_utc, e.wind_provider, e.marine_provider, e.wind_speed_kn, e.wind_dir_from_deg,
              e.wind_gust_kn, e.wave_height_m, e.wave_dir_from_deg, e.wave_period_s, e.current_speed_kn,
              e.current_dir_to_deg, e.missing AS missing_fields
           FROM ais_reports r LEFT JOIN enrichment e ON e.report_id = r.id
           LEFT JOIN vessels v ON v.mmsi = r.mmsi WHERE 1=1"""
    args = []
    if which == "enriched":
        q += " AND e.status IN ('matched','partial')"
    elif which == "unmatched":
        q += " AND (e.status IS NULL OR e.status = 'missing')"
    if mmsi:
        q += " AND r.mmsi = ?"
        args.append(mmsi)
    if since:
        q += " AND r.provider_time_utc >= ?"
        args.append(since)
    if until:
        q += " AND r.provider_time_utc < ?"
        args.append(until)
    q += " ORDER BY r.mmsi, r.provider_time_utc"
    n = 0
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(EXPORT_COLS)
        for r in conn.execute(q, args):
            w.writerow(["" if r[c] is None else r[c] for c in EXPORT_COLS])
            n += 1
    return n
