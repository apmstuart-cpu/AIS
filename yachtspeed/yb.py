"""Import positions from YB Tracking (yb.tl), used by the ARC and other ocean rallies.

YB transmits over Iridium, so it covers the mid-ocean legs that terrestrial AIS cannot.
Public endpoints used by the yb.tl web viewer:
    https://cf.yb.tl/JSON/<race>/RaceSetup        teams (id, name, ...), latin-1 JSON
    https://cf.yb.tl/BIN/<race>/AllPositions3     all tracks, compact big-endian binary

AllPositions3 layout: u8 flags (1 altitude, 2 distance-to-finish, 4 lap, 8 percent),
u32 reference time, then per team: u16 team id, u16 point count, then points newest
first. A point whose first byte has the high bit set is a delta from the previous
(newer) point: u16 seconds-earlier (low 15 bits), i16 dlat, i16 dlon (1e-5 degrees);
otherwise it is absolute: u32 seconds after the reference time, i32 lat, i32 lon.

YB gives positions only. SOG and COG are derived from consecutive fixes, so they are
speed and course made good over the interval (typically 1-6 hours offshore) and will
read low when a yacht tacks or gybes within the interval. Rows are stored with
source 'yb:<race>' and msg_type 'yb' so they can be told apart from AIS.
"""

from __future__ import annotations

import json
import logging
import struct
from datetime import datetime, timezone

import requests

from .ais import Position
from .geo import haversine_nm, initial_bearing
from .logutil import log

logger = logging.getLogger("yb")
BASE = "https://cf.yb.tl"
MIN_INTERVAL_S = 10 * 60  # shorter gaps give noisy derived speeds
MAX_INTERVAL_S = 6 * 3600  # longer gaps say little about speed at the fix


def decode_positions(buf: bytes) -> dict[int, list[dict]]:
    """Decode AllPositions3 into {team_id: [{'at': epoch_s, 'lat': deg, 'lon': deg}, ...]} oldest first."""
    if len(buf) < 5:
        return {}
    flags = buf[0]
    has_alt, has_dtf, has_lap, has_pc = flags & 1, flags & 2, flags & 4, flags & 8
    ref = struct.unpack_from(">I", buf, 1)[0]
    pos, out = 5, {}
    while pos + 4 <= len(buf):
        tid, n = struct.unpack_from(">HH", buf, pos)
        pos += 4
        pts, prev = [], None
        for _ in range(n):
            if buf[pos] & 0x80:
                dt, dlat, dlon = struct.unpack_from(">Hhh", buf, pos)
                pos += 6
                m = {"at": prev["at"] - (dt & 0x7FFF), "lat": prev["lat"] + dlat, "lon": prev["lon"] + dlon}
                pos += 2 if has_alt else 0
                if has_dtf:
                    pos += 2 + (1 if has_lap else 0)
                pos += 2 if has_pc else 0
            else:
                t, lat, lon = struct.unpack_from(">Iii", buf, pos)
                pos += 12
                m = {"at": ref + t, "lat": lat, "lon": lon}
                pos += 2 if has_alt else 0
                if has_dtf:
                    pos += 4 + (1 if has_lap else 0)
                pos += 4 if has_pc else 0
            pts.append(m)
            prev = m
        out[tid] = [{"at": p["at"], "lat": p["lat"] / 1e5, "lon": p["lon"] / 1e5} for p in reversed(pts)]
    return out


def with_derived_motion(points: list[dict]) -> list[dict]:
    """Add sog_kn / cog_deg made good since the latest fix at least MIN_INTERVAL_S earlier.

    Using a reference fix >= 10 minutes back (rather than the immediately preceding fix)
    keeps speeds defined where the tracker reports every few minutes, e.g. near a start.
    No speed is given when that reference is more than MAX_INTERVAL_S back.
    """
    out, j = [], -1
    for i, p in enumerate(points):
        while j + 1 < i and points[j + 1]["at"] <= p["at"] - MIN_INTERVAL_S:
            j += 1
        q = dict(p, sog_kn=None, cog_deg=None, interval_s=None)
        if j >= 0 and points[j]["at"] <= p["at"] - MIN_INTERVAL_S:
            ref = points[j]
            dt = p["at"] - ref["at"]
            q["interval_s"] = dt
            if dt <= MAX_INTERVAL_S:
                dist = haversine_nm(ref["lat"], ref["lon"], p["lat"], p["lon"])
                q["sog_kn"] = round(dist / (dt / 3600), 2)
                if dist > 0.05:
                    q["cog_deg"] = round(initial_bearing(ref["lat"], ref["lon"], p["lat"], p["lon"]), 1)
        out.append(q)
    return out


class YBClient:
    def __init__(self, session: requests.Session | None = None, timeout=60):
        self.http = session or requests.Session()
        self.http.headers["User-Agent"] = "yachtspeed/0.2 (personal yacht performance research)"
        self.timeout = timeout

    def _get(self, path, attempts=3):
        import time

        for i in range(attempts):
            try:
                r = self.http.get(f"{BASE}/{path}", timeout=self.timeout)
            except requests.RequestException as e:
                err = f"{type(e).__name__}: {e}"
            else:
                if r.status_code == 200:
                    return r.content
                err = f"HTTP {r.status_code}"
                if r.status_code < 500 and r.status_code != 429:
                    break
            if i < attempts - 1:
                time.sleep(10 * (i + 1))
        raise RuntimeError(f"YB {path}: {err}")

    def teams(self, race: str) -> list[dict]:
        setup = json.loads(self._get(f"JSON/{race}/RaceSetup").decode("latin-1"))
        return setup.get("teams") or []

    def positions(self, race: str) -> dict[int, list[dict]]:
        return decode_positions(self._get(f"BIN/{race}/AllPositions3"))


def _norm(name: str) -> str:
    return "".join(ch for ch in (name or "").upper() if ch.isalnum())


def find_team(teams: list[dict], name: str) -> dict | None:
    want = _norm(name)
    exact = [t for t in teams if _norm(t.get("name")) == want]
    if exact:
        return exact[0]
    partial = [t for t in teams if want and want in _norm(t.get("name"))]
    return partial[0] if len(partial) == 1 else None


def import_team(store, race: str, team_id: int, mmsi: str, points: list[dict]) -> dict:
    stats = {"points": len(points), "stored": 0, "duplicate_bucket": 0, "no_motion": 0}
    for p in with_derived_motion(points):
        if p["sog_kn"] is None:
            stats["no_motion"] += 1
        pos = Position(
            mmsi=mmsi, msg_type="yb", ais_class=None,
            provider_time=datetime.fromtimestamp(p["at"], timezone.utc),
            lat=round(p["lat"], 5), lon=round(p["lon"], 5), sog_kn=p["sog_kn"], cog_deg=p["cog_deg"],
            heading_deg=None, nav_status=None, ais_second=None, position_accuracy=None,
        )
        if store.store_position(pos, source=f"yb:{race}"):
            stats["stored"] += 1
        else:
            stats["duplicate_bucket"] += 1
    return stats


def sync_configured(conn, cfg, client: YBClient | None = None) -> dict:
    """Import every configured yacht that has a `yb_race`. Safe to re-run (de-duplicated)."""
    from .collector import Store

    client = client or YBClient()
    store = Store(conn, cfg)
    results, cache = {}, {}
    for y in cfg.fleet:
        if not (y.yb_race and y.mmsi):
            continue
        try:
            if y.yb_race not in cache:
                cache[y.yb_race] = (client.teams(y.yb_race), client.positions(y.yb_race))
            teams, tracks = cache[y.yb_race]
            team = find_team(teams, y.yb_team or y.name)
            if not team:
                results[y.name] = {"race": y.yb_race, "status": "team_not_listed_yet",
                                   "teams_listed": len(teams)}
                continue
            pts = tracks.get(team["id"], [])
            results[y.name] = {"race": y.yb_race, "team": team["name"], "team_id": team["id"],
                               **import_team(store, y.yb_race, team["id"], y.mmsi, pts)}
        except Exception as e:  # one race failing must not stop the others
            results[y.name] = {"race": y.yb_race, "status": "error", "error": f"{type(e).__name__}: {e}"}
        log(logger, logging.INFO, "yb sync", yacht=y.name, **results[y.name])
    return results
