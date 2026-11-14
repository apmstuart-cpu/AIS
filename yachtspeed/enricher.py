"""Weather enrichment service: matches stored AIS reports to Open-Meteo weather.

Each cycle:
  1. pick reports that are old enough to have settled weather and lack enrichment,
     newest first, so a backlog of old/failed rows never starves new data;
  2. group them by (grid cell, UTC date) and fetch one day of hourly data per group;
  3. write an enrichment row per report: matched (all fields), partial (some fields
     null, e.g. marine model has no data near the coast) or missing (fetch gave up).
"""

from __future__ import annotations

import logging
import signal
import time
from collections import defaultdict
from datetime import timedelta

from . import db
from .geo import iso, nearest_hour, parse_iso, round_cell, utcnow
from .logutil import log
from .weather import WEATHER_COLS, Budget, OpenMeteo, QuotaExceeded, ensure_cell_day, lookup, wind_provider_for

logger = logging.getLogger("enricher")
KEY_FIELDS = ["wind_speed_kn", "wind_dir_from_deg", "wave_height_m", "current_speed_kn", "current_dir_to_deg"]


def pending_reports(conn, cfg, limit=2000, since=None):
    cutoff = iso(utcnow() - timedelta(minutes=cfg.settle_minutes))
    q = """SELECT r.id, r.lat, r.lon, r.provider_time_utc FROM ais_reports r
           LEFT JOIN enrichment e ON e.report_id = r.id
           WHERE e.report_id IS NULL AND r.provider_time_utc <= ?"""
    args = [cutoff]
    if since:
        q += " AND r.provider_time_utc >= ?"
        args.append(since)
    q += " ORDER BY r.provider_time_utc DESC LIMIT ?"
    args.append(limit)
    return conn.execute(q, args).fetchall()


def enrich_once(conn, cfg, client: OpenMeteo, limit=2000, since=None) -> dict:
    stats = {"candidates": 0, "matched": 0, "partial": 0, "missing": 0, "waiting": 0}
    rows = pending_reports(conn, cfg, limit, since)
    stats["candidates"] = len(rows)
    groups = defaultdict(list)
    for r in rows:
        hour = nearest_hour(parse_iso(r["provider_time_utc"]))
        key = (round_cell(r["lat"], cfg.cell_deg), round_cell(r["lon"], cfg.cell_deg), hour.strftime("%Y-%m-%d"))
        groups[key].append((r["id"], iso(hour)))

    quota_hit = None
    for (clat, clon, date), members in groups.items():
        wprov = wind_provider_for(date, cfg.recent_days)
        status = {}
        for prov, kind in ((wprov, "wind"), ("om_marine", "marine")):
            if quota_hit:
                status[kind] = "waiting"
                continue
            try:
                status[kind] = ensure_cell_day(conn, client, prov, kind, clat, clon, date, cfg.max_fetch_attempts)
            except QuotaExceeded as e:
                quota_hit = e
                status[kind] = "waiting"
                log(logger, logging.WARNING, "weather quota reached", reason=str(e), resume=iso(e.resume_at))
        if "waiting" in status.values() or "retry" in status.values():
            stats["waiting"] += len(members)
            continue
        for report_id, hour in members:
            vals, missing = {}, []
            w = lookup(conn, wprov, "wind", clat, clon, hour) if status["wind"] == "ok" else None
            m = lookup(conn, "om_marine", "marine", clat, clon, hour) if status["marine"] == "ok" else None
            for col in WEATHER_COLS:
                src = w if col.startswith("wind") else m
                vals[col] = src[col] if src is not None else None
            missing = [c for c in KEY_FIELDS if vals[c] is None]
            if vals["wind_speed_kn"] is None and vals["wave_height_m"] is None:
                st = "missing"
            else:
                st = "partial" if missing else "matched"
            stats[st] += 1
            conn.execute(
                f"""INSERT OR REPLACE INTO enrichment(report_id, status, weather_hour_utc, cell_lat, cell_lon,
                    wind_provider, marine_provider, {", ".join(WEATHER_COLS)}, missing, matched_utc)
                    VALUES (?,?,?,?,?,?,?,{",".join("?" * len(WEATHER_COLS))},?,?)""",
                (report_id, st, hour, clat, clon, wprov if w else None, "om_marine" if m else None,
                 *[vals[c] for c in WEATHER_COLS], ",".join(missing) or None, iso(utcnow())),
            )
    stats["quota_paused_until"] = iso(quota_hit.resume_at) if quota_hit else None
    return stats


def retry_failed(conn, missing_too=True) -> int:
    """Reset permanently failed fetches (and 'missing' enrichments) so they are retried."""
    n = conn.execute("UPDATE weather_fetches SET status='retry', attempts=0, next_retry_utc=NULL "
                     "WHERE status IN ('failed','retry')").rowcount
    if missing_too:
        n += conn.execute("DELETE FROM enrichment WHERE status='missing'").rowcount
    return n


def main(cfg, conn, once=False) -> None:
    budget = Budget(conn, cfg.calls_per_minute, cfg.calls_per_hour, cfg.calls_per_day)
    client = OpenMeteo(budget)
    stopping = {"flag": False}

    def _stop(*_):
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    db.log_event(conn, "enricher", "start")
    while not stopping["flag"]:
        t0 = time.monotonic()
        try:
            stats = enrich_once(conn, cfg, client)
            budget.prune()
            log(logger, logging.INFO, "enrich cycle", **stats, seconds=round(time.monotonic() - t0, 1))
            db.heartbeat(conn, "enricher", stats)
        except Exception as e:  # keep the service alive; systemd restarts on crash anyway
            logger.exception("enrich cycle failed")
            db.log_event(conn, "enricher", "error", f"{type(e).__name__}: {e}")
        if once:
            break
        for _ in range(cfg.enrich_interval_s):
            if stopping["flag"]:
                break
            time.sleep(1)
    db.log_event(conn, "enricher", "stop")
