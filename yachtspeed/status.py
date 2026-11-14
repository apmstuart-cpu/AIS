"""Operational status: last report per yacht, reconnects, weather backlog, match rate."""

from __future__ import annotations

import json
import os
from datetime import timedelta

from .geo import iso, parse_iso, utcnow


def _age_min(ts):
    return round((utcnow() - parse_iso(ts)).total_seconds() / 60, 1) if ts else None


def status(conn, cfg) -> dict:
    now = utcnow()
    d1, d7 = iso(now - timedelta(days=1)), iso(now - timedelta(days=7))
    yachts = []
    configured = {y.mmsi: y for y in cfg.fleet if y.mmsi}
    for y in cfg.fleet:
        if not y.mmsi:
            yachts.append({"name": y.name, "mmsi": None, "verified": False, "note": "MMSI not set"})
            continue
        last = conn.execute(
            "SELECT provider_time_utc, ingested_utc, lat, lon, sog_kn, cog_deg, nav_status, ais_class, source "
            "FROM ais_reports WHERE mmsi=? ORDER BY provider_time_utc DESC LIMIT 1", (y.mmsi,)).fetchone()
        v = conn.execute("SELECT static_name, static_callsign, static_length_m FROM vessels WHERE mmsi=?",
                         (y.mmsi,)).fetchone()
        n24 = conn.execute("SELECT COUNT(*) FROM ais_reports WHERE mmsi=? AND provider_time_utc>=?",
                           (y.mmsi, d1)).fetchone()[0]
        yachts.append({
            "name": y.name, "mmsi": y.mmsi, "verified": y.verified,
            "broadcast_name": v["static_name"] if v else None,
            "broadcast_callsign": v["static_callsign"] if v else None,
            "last_report": dict(last) if last else None,
            "last_report_age_min": _age_min(last["provider_time_utc"]) if last else None,
            "reports_24h": n24,
        })
    hb = {r["service"]: {"ts_utc": r["ts_utc"], "age_min": _age_min(r["ts_utc"]),
                         "detail": json.loads(r["detail"]) if r["detail"] else None}
          for r in conn.execute("SELECT * FROM heartbeats")}
    ev = lambda e, since: conn.execute(  # noqa: E731
        "SELECT COUNT(*) FROM service_events WHERE event=? AND ts_utc>=?", (e, since)).fetchone()[0]
    enr = {r[0]: r[1] for r in conn.execute(
        "SELECT e.status, COUNT(*) FROM enrichment e JOIN ais_reports r ON r.id=e.report_id "
        "WHERE r.provider_time_utc>=? GROUP BY e.status", (d7,))}
    pending = conn.execute(
        "SELECT COUNT(*), MIN(r.provider_time_utc) FROM ais_reports r LEFT JOIN enrichment e "
        "ON e.report_id=r.id WHERE e.report_id IS NULL").fetchone()
    fetch = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM weather_fetches GROUP BY status")}
    total7 = sum(enr.values())
    usage = {r[0]: r[1] for r in conn.execute(
        "SELECT host, calls FROM api_usage WHERE period=?", (now.strftime("d%Y%m%d"),))}
    pauses = {r[0][6:]: r[1] for r in conn.execute("SELECT key, value FROM kv WHERE key LIKE 'pause:%'")
              if r[1] > iso(now)}
    last_backup = conn.execute("SELECT value FROM kv WHERE key='last_backup'").fetchone()
    size = os.path.getsize(cfg.db_path) if cfg.db_path != ":memory:" and os.path.exists(cfg.db_path) else None
    return {
        "now_utc": iso(now),
        "yachts": yachts,
        "unconfigured_mmsis_seen": [r[0] for r in conn.execute(
            "SELECT DISTINCT mmsi FROM ais_reports") if r[0] not in configured],
        "services": hb,
        "collector": {"connects_24h": ev("connected", d1), "disconnects_24h": ev("disconnect", d1),
                      "provider_errors_24h": ev("provider_error", d1)},
        "weather": {
            "backlog_reports": pending[0], "oldest_pending_utc": pending[1],
            "enrichment_7d": enr,
            "match_rate_7d": round(enr.get("matched", 0) / total7, 3) if total7 else None,
            "usable_rate_7d": round((enr.get("matched", 0) + enr.get("partial", 0)) / total7, 3) if total7 else None,
            "fetches": fetch, "api_calls_today": usage, "paused": pauses,
        },
        "predictions": conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0],
        "db_bytes": size,
        "last_backup": last_backup[0] if last_backup else None,
    }


def format_text(s: dict) -> str:
    lines = [f"Status at {s['now_utc']}"]
    for y in s["yachts"]:
        if not y.get("mmsi"):
            lines.append(f"  {y['name']}: MMSI not set")
            continue
        lr = y["last_report"]
        flag = "" if y["verified"] else " (MMSI UNVERIFIED)"
        if lr:
            lines.append(f"  {y['name']} {y['mmsi']}{flag}: last {lr['provider_time_utc']} "
                         f"({y['last_report_age_min']} min ago) {lr['lat']:.4f},{lr['lon']:.4f} "
                         f"SOG {lr['sog_kn']} kn; {y['reports_24h']} reports/24h")
        else:
            lines.append(f"  {y['name']} {y['mmsi']}{flag}: no reports yet")
        if y.get("broadcast_name"):
            lines.append(f"      broadcast name {y['broadcast_name']!r} callsign {y['broadcast_callsign']!r}")
    for name, h in s["services"].items():
        lines.append(f"  service {name}: heartbeat {h['age_min']} min ago")
    c, w = s["collector"], s["weather"]
    lines.append(f"  collector 24h: {c['connects_24h']} connects, {c['disconnects_24h']} disconnects, "
                 f"{c['provider_errors_24h']} provider errors")
    lines.append(f"  weather backlog: {w['backlog_reports']} reports (oldest {w['oldest_pending_utc']}); "
                 f"7d match rate {w['match_rate_7d']} usable {w['usable_rate_7d']} {w['enrichment_7d']}")
    lines.append(f"  weather fetches {w['fetches']}; API calls today {w['api_calls_today']}"
                 + (f"; PAUSED {w['paused']}" if w["paused"] else ""))
    lines.append(f"  predictions stored: {s['predictions']}; db {s['db_bytes']} bytes; last backup {s['last_backup']}")
    return "\n".join(lines)
