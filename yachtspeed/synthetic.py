"""Synthetic data for smoke tests: a yacht with a known speed function and fake weather."""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from .geo import along_course_current, bucket_start, iso, iso_ms, wind_angle_to_course


def true_stw(tws, twa, wave):
    """Toy 26 m cruiser: boat speed through water (kn)."""
    angle_factor = {0: 0.0, 30: 0.25, 45: 0.55, 60: 0.7, 90: 0.8, 120: 0.78, 150: 0.65, 180: 0.55}
    keys = sorted(angle_factor)
    for a, b in zip(keys, keys[1:]):
        if a <= twa <= b:
            f = angle_factor[a] + (angle_factor[b] - angle_factor[a]) * (twa - a) / (b - a)
            break
    else:
        f = 0.55
    return max(0.0, min(11.5, f * 2.2 * tws ** 0.75) - 0.4 * (wave or 0))


def generate(conn, mmsi="999000001", voyages=24, hours_per_voyage=30, seed=1, start=None) -> int:
    rnd = random.Random(seed)
    t = start or datetime(2025, 5, 1, tzinfo=timezone.utc)
    conn.execute("INSERT OR IGNORE INTO vessels(mmsi, name, model) VALUES (?, 'SYNTHETIC', 'test')", (mmsi,))
    n = 0
    lat, lon = 39.5, 2.6
    for v in range(voyages):
        course = rnd.uniform(0, 360)
        wind_from = rnd.uniform(0, 360)
        tws = rnd.uniform(4, 26)
        for step in range(hours_per_voyage * 6):
            tws = min(35, max(2, tws + rnd.gauss(0, 0.4)))
            wind_from = (wind_from + rnd.gauss(0, 2)) % 360
            wave = max(0.1, 0.08 * tws + rnd.gauss(0, 0.2))
            cur_speed, cur_to = abs(rnd.gauss(0.3, 0.2)), rnd.uniform(0, 360)
            twa = wind_angle_to_course(wind_from, course)
            stw = true_stw(tws, twa, wave)
            if stw < 3:  # crew motors when there is no useful wind
                stw = 6.5
            sog = max(0.0, stw + along_course_current(cur_speed, cur_to, course) + rnd.gauss(0, 0.35))
            cur = conn.execute(
                """INSERT OR IGNORE INTO ais_reports(mmsi, source, bucket_utc, provider_time_utc, ingested_utc,
                   msg_type, ais_class, lat, lon, sog_kn, cog_deg, heading_deg, nav_status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mmsi, "synthetic", iso(bucket_start(t)), iso_ms(t), iso_ms(t), "synthetic", "A",
                 lat, lon, round(sog, 1), round(course, 1), round(course), 8),
            )
            conn.execute(
                """INSERT OR REPLACE INTO enrichment(report_id, status, weather_hour_utc, wind_provider,
                   marine_provider, wind_speed_kn, wind_dir_from_deg, wave_height_m, current_speed_kn,
                   current_dir_to_deg, matched_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (cur.lastrowid, "matched", iso(t.replace(minute=0, second=0)), "synthetic", "synthetic",
                 tws, wind_from, wave, cur_speed, cur_to, iso(t)),
            )
            d = sog / 6 / 60  # degrees per 10 minutes, roughly
            lat += d * math.cos(math.radians(course))
            lon += d * math.sin(math.radians(course)) / math.cos(math.radians(lat))
            t += timedelta(minutes=10)
            n += 1
        t += timedelta(hours=rnd.uniform(12, 96))
        lat, lon = 39.5 + rnd.gauss(0, 1), 2.6 + rnd.gauss(0, 2)
    return n
