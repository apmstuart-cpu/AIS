"""Geodesy and time helpers. All times are UTC; all angles are degrees."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

EARTH_RADIUS_NM = 3440.065
KMH_TO_KN = 1 / 1.852
MS_TO_KN = 3600 / 1852
MPH_TO_KN = 1609.344 / 1852


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """Canonical storage format: 2026-11-14T14:38:01Z (second precision)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ms(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def parse_iso(s: str) -> datetime:
    """Parse ISO-8601 (with Z, offset or naive=UTC)."""
    s = s.strip().replace(" ", "T", 1) if "T" not in s else s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


_AISSTREAM_TIME = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(?:\.(\d+))? ([+-]\d{4})"
)


def parse_aisstream_time(s: str) -> datetime:
    """AISStream MetaData.time_utc, e.g. '2024-05-01 12:34:56.123456789 +0000 UTC'.

    Python only keeps microseconds, so nanoseconds are truncated.
    """
    m = _AISSTREAM_TIME.match(s.strip())
    if not m:
        return parse_iso(s)
    date, clock, frac, off = m.groups()
    frac = (frac or "0")[:6].ljust(6, "0")
    dt = datetime.strptime(f"{date} {clock} {off}", "%Y-%m-%d %H:%M:%S %z")
    return dt.replace(microsecond=int(frac)).astimezone(timezone.utc)


def bucket_start(dt: datetime, minutes: int = 10) -> datetime:
    dt = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return dt - timedelta(minutes=dt.minute % minutes)


def nearest_hour(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc)
    base = dt.replace(minute=0, second=0, microsecond=0)
    return base + timedelta(hours=1) if (dt - base) >= timedelta(minutes=30) else base


def haversine_nm(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def intermediate_point(lat1, lon1, lat2, lon2, frac: float):
    """Great-circle point at fraction `frac` from (lat1,lon1) to (lat2,lon2)."""
    d = haversine_nm(lat1, lon1, lat2, lon2) / EARTH_RADIUS_NM
    if d == 0:
        return lat1, lon1
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = math.sin((1 - frac) * d) / math.sin(d)
    b = math.sin(frac * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return (
        math.degrees(math.atan2(z, math.hypot(x, y))),
        math.degrees(math.atan2(y, x)),
    )


def angle_diff(a: float, b: float) -> float:
    """Smallest absolute difference between two bearings, 0..180."""
    d = abs((a - b) % 360)
    return 360 - d if d > 180 else d


def wind_angle_to_course(wind_from_deg: float, course_deg: float) -> float:
    """True wind angle relative to course, 0 (head to wind) .. 180 (dead run).

    Wind direction uses the meteorological convention (direction wind blows FROM).
    """
    return angle_diff(wind_from_deg, course_deg)


def along_course_current(speed_kn, dir_to_deg, course_deg):
    """Component of current along the course (positive = helping).

    Current direction uses the oceanographic convention (direction current flows TOWARDS),
    which is what Open-Meteo's ocean_current_direction reports.
    """
    if speed_kn is None or dir_to_deg is None:
        return None
    return speed_kn * math.cos(math.radians(dir_to_deg - course_deg))


def to_knots(value, unit: str):
    if value is None:
        return None
    u = (unit or "").strip().lower()
    if u in ("kn", "kt", "kts", "knots"):
        return value
    if u in ("km/h", "kmh", "kph"):
        return value * KMH_TO_KN
    if u in ("m/s", "ms"):
        return value * MS_TO_KN
    if u in ("mp/h", "mph"):
        return value * MPH_TO_KN
    raise ValueError(f"unknown speed unit {unit!r}")


def round_cell(value: float, cell_deg: float) -> float:
    return round(round(value / cell_deg) * cell_deg, 4)
