"""AISStream websocket collector: a long-running supervised service."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import signal

from . import db
from .ais import SUBSCRIBE_TYPES, STATIC_TYPES, ParseError, Position, Static, parse_message
from .geo import bucket_start, iso, iso_ms, utcnow
from .logutil import log

AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"
EX_CONFIG = 78
logger = logging.getLogger("collector")


class Store:
    """Writes parsed messages to SQLite and keeps running counters."""

    def __init__(self, conn, cfg, source="aisstream"):
        self.conn, self.cfg, self.source = conn, cfg, source
        self.counts = {"messages": 0, "stored": 0, "duplicate_bucket": 0, "rejected": 0, "static": 0}
        self._sample_counts: dict = {}

    def sample_raw(self, raw: str, mtype: str, mmsi: str | None) -> None:
        day = iso(utcnow())[:10]
        key = (day, mtype)
        if key not in self._sample_counts:
            self._sample_counts[key] = self.conn.execute(
                "SELECT COUNT(*) FROM raw_samples WHERE day=? AND msg_type=?", key
            ).fetchone()[0]
        if self._sample_counts[key] < self.cfg.raw_samples_per_type_per_day:
            self.conn.execute(
                "INSERT INTO raw_samples(ingested_utc, day, msg_type, mmsi, payload) VALUES (?,?,?,?,?)",
                (iso(utcnow()), day, mtype, mmsi, raw),
            )
            self._sample_counts[key] += 1

    def handle(self, raw: str) -> Position | Static | None:
        self.counts["messages"] += 1
        msg = json.loads(raw)
        mtype = msg.get("MessageType", "unknown")
        mmsi = str((msg.get("MetaData") or {}).get("MMSI") or "") or None
        self.sample_raw(raw, mtype, mmsi)
        try:
            rec = parse_message(msg)
        except ParseError as e:
            self.counts["rejected"] += 1
            log(logger, logging.INFO, "rejected message", reason=str(e), msg_type=mtype, mmsi=mmsi)
            if "provider error" in str(e):
                raise
            return None
        if isinstance(rec, Position):
            self.store_position(rec)
        elif isinstance(rec, Static):
            self.store_static(rec)
        return rec

    def store_position(self, p: Position, source: str | None = None) -> bool:
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO ais_reports(mmsi, source, bucket_utc, provider_time_utc,
               ingested_utc, ais_second, msg_type, ais_class, lat, lon, sog_kn, cog_deg,
               heading_deg, nav_status, position_accuracy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                p.mmsi, source or self.source, iso(bucket_start(p.provider_time, self.cfg.bucket_minutes)),
                iso_ms(p.provider_time), iso_ms(utcnow()), p.ais_second, p.msg_type, p.ais_class,
                p.lat, p.lon, p.sog_kn, p.cog_deg, p.heading_deg, p.nav_status, p.position_accuracy,
            ),
        )
        if cur.rowcount:
            self.counts["stored"] += 1
            log(logger, logging.INFO, "stored position", mmsi=p.mmsi, lat=p.lat, lon=p.lon,
                sog_kn=p.sog_kn, provider_time=iso_ms(p.provider_time))
            return True
        self.counts["duplicate_bucket"] += 1
        return False

    def store_static(self, s: Static) -> None:
        self.counts["static"] += 1
        self.conn.execute("INSERT OR IGNORE INTO vessels(mmsi) VALUES (?)", (s.mmsi,))
        self.conn.execute(
            """UPDATE vessels SET static_name=COALESCE(?, static_name),
               static_callsign=COALESCE(?, static_callsign), static_imo=COALESCE(?, static_imo),
               static_ship_type=COALESCE(?, static_ship_type), static_length_m=COALESCE(?, static_length_m),
               static_beam_m=COALESCE(?, static_beam_m), static_updated_utc=? WHERE mmsi=?""",
            (s.name, s.callsign, s.imo, s.ship_type, s.length_m, s.beam_m, iso(s.provider_time), s.mmsi),
        )


def subscription(cfg, key: str, mmsis: list[str] | None, types=None) -> dict:
    sub = {
        "APIKey": key,
        "BoundingBoxes": cfg.bounding_boxes,
        "FilterMessageTypes": types or SUBSCRIBE_TYPES,
    }
    if mmsis:
        if len(mmsis) > 50:
            raise ValueError("AISStream allows at most 50 MMSIs per subscription")
        sub["FiltersShipMMSI"] = mmsis
    return sub


async def run_collector(cfg, conn, stop: asyncio.Event | None = None) -> None:
    import websockets

    key = cfg.aisstream_key
    mmsis = cfg.mmsis
    problem = ("AISSTREAM_API_KEY is not set (put it in /etc/yachtspeed/env)" if not key
               else "no fleet MMSIs configured; set mmsi for a [[yacht]] in config.toml" if not mmsis else None)
    if problem:
        log(logger, logging.ERROR, problem)
        db.log_event(conn, "collector", "config_error", problem)
        raise SystemExit(EX_CONFIG)  # systemd: RestartPreventExitStatus stops a restart loop
    db.sync_fleet(conn, cfg.fleet)
    stop = stop or asyncio.Event()
    store = Store(conn, cfg)
    backoff = 1.0
    db.log_event(conn, "collector", "start", {"mmsis": mmsis, "boxes": cfg.bounding_boxes})

    async def heartbeat_loop():
        while not stop.is_set():
            db.heartbeat(conn, "collector", store.counts)
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    hb = asyncio.create_task(heartbeat_loop())
    try:
        while not stop.is_set():
            try:
                async with websockets.connect(
                    AISSTREAM_URL, ping_interval=20, ping_timeout=30, close_timeout=5, max_size=2**20
                ) as ws:
                    await ws.send(json.dumps(subscription(cfg, key, mmsis)))
                    db.log_event(conn, "collector", "connected")
                    log(logger, logging.INFO, "connected", mmsis=mmsis)
                    backoff = 1.0
                    async for raw in ws:
                        if stop.is_set():
                            break
                        store.handle(raw)
                    if not stop.is_set():
                        db.log_event(conn, "collector", "disconnect", f"server closed: {ws.close_code}")
                        log(logger, logging.WARNING, "server closed connection", code=ws.close_code)
            except asyncio.CancelledError:
                raise
            except ParseError as e:  # provider-level error, e.g. bad key
                db.log_event(conn, "collector", "provider_error", str(e))
                log(logger, logging.ERROR, "provider error", error=str(e))
                backoff = max(backoff, 60.0)
            except Exception as e:  # network errors, closed connections
                db.log_event(conn, "collector", "disconnect", f"{type(e).__name__}: {e}")
                log(logger, logging.WARNING, "disconnected", error=f"{type(e).__name__}: {e}")
            if stop.is_set():
                break
            delay = backoff * (0.5 + random.random())
            db.log_event(conn, "collector", "reconnect", {"delay_s": round(delay, 1)})
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 300.0)
    finally:
        stop.set()
        await hb
        db.heartbeat(conn, "collector", {**store.counts, "stopped": True})
        db.log_event(conn, "collector", "stop", store.counts)


async def discover(cfg, conn, name_fragment: str, minutes: float) -> dict:
    """Listen to static-data messages in the bounding boxes (no MMSI filter) and
    report vessels whose broadcast name contains `name_fragment`. Use to verify MMSIs."""
    import websockets

    key = cfg.aisstream_key
    if not key:
        raise SystemExit("AISSTREAM_API_KEY is not set")
    found: dict = {}
    frag = name_fragment.upper()
    store = Store(conn, cfg)
    deadline = asyncio.get_event_loop().time() + minutes * 60
    async with websockets.connect(AISSTREAM_URL, ping_interval=20) as ws:
        await ws.send(json.dumps(subscription(cfg, key, None, list(STATIC_TYPES))))
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(1, deadline - asyncio.get_event_loop().time()))
            except asyncio.TimeoutError:
                break
            rec = parse_message(json.loads(raw))
            if isinstance(rec, Static) and rec.name and frag in rec.name.upper():
                store.store_static(rec)
                found[rec.mmsi] = {
                    "name": rec.name, "callsign": rec.callsign, "ship_type": rec.ship_type,
                    "length_m": rec.length_m, "seen_utc": iso(rec.provider_time),
                }
                log(logger, logging.INFO, "candidate", mmsi=rec.mmsi, **found[rec.mmsi])
    return found


def main(cfg, conn) -> None:
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    loop.run_until_complete(run_collector(cfg, conn, stop))
