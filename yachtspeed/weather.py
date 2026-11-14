"""Open-Meteo clients with a quota guard.

Providers (each value is the provenance stored with every weather row):
  wind / 'om_forecast'      api.open-meteo.com/v1/forecast              recent days and future
  wind / 'om_hist_forecast' historical-forecast-api.open-meteo.com       archived forecasts (2022+)
  wind / 'om_prev_dayN'     previous-runs-api.open-meteo.com             forecast issued N days earlier
  marine / 'om_marine'      marine-api.open-meteo.com/v1/marine          waves + modelled currents

Units are read from each response's `hourly_units` and converted to knots, never assumed.
Conventions: wind_direction and wave_direction are FROM; ocean_current_direction is TOWARDS.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from . import db
from .geo import iso, to_knots, utcnow
from .logutil import log

logger = logging.getLogger("weather")

ENDPOINTS = {
    "om_forecast": "https://api.open-meteo.com/v1/forecast",
    "om_hist_forecast": "https://historical-forecast-api.open-meteo.com/v1/forecast",
    "om_prev": "https://previous-runs-api.open-meteo.com/v1/forecast",
    "om_marine": "https://marine-api.open-meteo.com/v1/marine",
}
WIND_VARS = ["wind_speed_10m", "wind_direction_10m", "wind_gusts_10m"]
MARINE_VARS = ["wave_height", "wave_direction", "wave_period", "ocean_current_velocity",
               "ocean_current_direction"]


class QuotaExceeded(Exception):
    """Local budget or provider rate limit reached; stop fetching until `resume_at`."""

    def __init__(self, msg, resume_at: datetime):
        super().__init__(msg)
        self.resume_at = resume_at


class FetchError(Exception):
    pass


class Budget:
    """Counts calls per host per minute/hour/day in SQLite (shared across processes)."""

    def __init__(self, conn, per_minute, per_hour, per_day):
        self.conn = conn
        self.limits = {"m": per_minute, "h": per_hour, "d": per_day}

    @staticmethod
    def _periods(now):
        return {"m": now.strftime("m%Y%m%d%H%M"), "h": now.strftime("h%Y%m%d%H"), "d": now.strftime("d%Y%m%d")}

    def check_and_count(self, host: str) -> None:
        now = utcnow()
        paused = db.kv_get(self.conn, f"pause:{host}")
        if paused and paused > iso(now):
            raise QuotaExceeded(f"{host} paused", datetime.fromisoformat(paused.replace("Z", "+00:00")))
        periods = self._periods(now)
        for k, period in periods.items():
            row = self.conn.execute(
                "SELECT calls FROM api_usage WHERE host=? AND period=?", (host, period)
            ).fetchone()
            if row and row[0] >= self.limits[k]:
                resume = {"m": now.replace(second=0) + timedelta(minutes=1),
                          "h": now.replace(minute=0, second=0) + timedelta(hours=1),
                          "d": now.replace(hour=0, minute=0, second=0) + timedelta(days=1)}[k]
                raise QuotaExceeded(f"local {k} budget reached for {host}", resume)
        for period in periods.values():
            self.conn.execute(
                "INSERT INTO api_usage(host, period, calls) VALUES (?,?,1) "
                "ON CONFLICT(host, period) DO UPDATE SET calls=calls+1", (host, period)
            )

    def pause(self, host: str, until: datetime) -> None:
        db.kv_set(self.conn, f"pause:{host}", iso(until))

    def usage_today(self) -> dict:
        day = utcnow().strftime("d%Y%m%d")
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT host, calls FROM api_usage WHERE period=?", (day,))}

    def prune(self) -> None:
        cutoff = (utcnow() - timedelta(days=3)).strftime("%Y%m%d")
        self.conn.execute("DELETE FROM api_usage WHERE substr(period, 2, 8) < ?", (cutoff,))


class OpenMeteo:
    def __init__(self, budget: Budget, session: requests.Session | None = None, timeout=30):
        self.budget = budget
        self.http = session or requests.Session()
        self.http.headers["User-Agent"] = "yachtspeed/0.2"
        self.timeout = timeout

    def _get(self, provider_key: str, params: dict) -> dict:
        url = ENDPOINTS[provider_key]
        host = url.split("/")[2]
        self.budget.check_and_count(host)
        try:
            r = self.http.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise FetchError(f"network: {type(e).__name__}: {e}") from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        reason = str(body.get("reason", r.text[:200]))
        if r.status_code == 429 or "limit exceeded" in reason.lower():
            now = utcnow()
            if "daily" in reason.lower():
                until = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1, minutes=5)
            elif "hourly" in reason.lower():
                until = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1, minutes=1)
            else:
                until = now + timedelta(minutes=2)
            self.budget.pause(host, until)
            raise QuotaExceeded(f"{host}: {reason}", until)
        if r.status_code != 200 or body.get("error"):
            raise FetchError(f"HTTP {r.status_code}: {reason}")
        return body

    @staticmethod
    def _hourly(body: dict, var: str, unit_kind: str | None = None):
        vals = body.get("hourly", {}).get(var)
        if vals is None:
            return None
        if unit_kind == "speed":
            unit = body.get("hourly_units", {}).get(var, "")
            return [to_knots(v, unit) for v in vals]
        return vals

    def fetch(self, provider: str, kind: str, lat: float, lon: float, start: str, end: str) -> list[dict]:
        """Return hourly rows for [start, end] (UTC dates, YYYY-MM-DD)."""
        params = {"latitude": lat, "longitude": lon, "start_date": start, "end_date": end,
                  "timezone": "GMT", "wind_speed_unit": "kn"}
        if kind == "marine":
            params["hourly"] = ",".join(MARINE_VARS)
            params["cell_selection"] = "sea"
            body = self._get("om_marine", params)
            cols = {
                "wave_height_m": self._hourly(body, "wave_height"),
                "wave_dir_from_deg": self._hourly(body, "wave_direction"),
                "wave_period_s": self._hourly(body, "wave_period"),
                "current_speed_kn": self._hourly(body, "ocean_current_velocity", "speed"),
                "current_dir_to_deg": self._hourly(body, "ocean_current_direction"),
            }
        else:
            if provider.startswith("om_prev_day"):
                n = int(provider.removeprefix("om_prev_day"))
                names = [f"{v}_previous_day{n}" for v in WIND_VARS]
                key = "om_prev"
            else:
                names, key = WIND_VARS, provider
            params["hourly"] = ",".join(names)
            body = self._get(key, params)
            cols = {
                "wind_speed_kn": self._hourly(body, names[0], "speed"),
                "wind_dir_from_deg": self._hourly(body, names[1]),
                "wind_gust_kn": self._hourly(body, names[2], "speed"),
            }
        times = body.get("hourly", {}).get("time") or []
        if not times:
            raise FetchError("response has no hourly times")
        rows = []
        for i, t in enumerate(times):
            row = {"time_utc": t + ":00Z" if len(t) == 16 else t,
                   "grid_lat": body.get("latitude"), "grid_lon": body.get("longitude")}
            for col, vals in cols.items():
                row[col] = vals[i] if vals is not None and i < len(vals) else None
            rows.append(row)
        return rows


def wind_provider_for(date_str: str, recent_days: int, now: datetime | None = None) -> str:
    now = now or utcnow()
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return "om_forecast" if d >= (now - timedelta(days=recent_days)).replace(
        hour=0, minute=0, second=0, microsecond=0) else "om_hist_forecast"


WEATHER_COLS = ["wind_speed_kn", "wind_dir_from_deg", "wind_gust_kn", "wave_height_m",
                "wave_dir_from_deg", "wave_period_s", "current_speed_kn", "current_dir_to_deg"]


def store_rows(conn, provider, kind, cell_lat, cell_lon, rows) -> None:
    now = iso(utcnow())
    for r in rows:
        conn.execute(
            f"""INSERT OR REPLACE INTO weather_cache(provider, kind, cell_lat, cell_lon, time_utc,
                grid_lat, grid_lon, fetched_utc, {", ".join(WEATHER_COLS)})
                VALUES (?,?,?,?,?,?,?,?,{",".join("?" * len(WEATHER_COLS))})""",
            (provider, kind, cell_lat, cell_lon, r["time_utc"], r["grid_lat"], r["grid_lon"], now,
             *[r.get(c) for c in WEATHER_COLS]),
        )


def ensure_cell_day(conn, client: OpenMeteo, provider, kind, cell_lat, cell_lon, date, max_attempts=8,
                    force=False) -> str:
    """Fetch one (provider, kind, cell, day) if not cached and due. Returns status.

    Failures are recorded with exponential back-off so they never block other cells.
    QuotaExceeded propagates so the caller can stop the whole cycle.
    """
    row = conn.execute(
        "SELECT status, attempts, next_retry_utc FROM weather_fetches WHERE provider=? AND kind=? "
        "AND cell_lat=? AND cell_lon=? AND date=?", (provider, kind, cell_lat, cell_lon, date),
    ).fetchone()
    now = utcnow()
    if row and not force:
        if row["status"] == "ok":
            return "ok"
        if row["status"] == "failed":
            return "failed"
        if row["next_retry_utc"] and row["next_retry_utc"] > iso(now):
            return "waiting"
    attempts = (row["attempts"] if row else 0) + 1
    try:
        rows = client.fetch(provider, kind, cell_lat, cell_lon, date, date)
    except FetchError as e:
        status = "failed" if attempts >= max_attempts else "retry"
        delay = min(300 * 2 ** (attempts - 1), 12 * 3600)
        conn.execute(
            "INSERT OR REPLACE INTO weather_fetches VALUES (?,?,?,?,?,?,?,?,?,?)",
            (provider, kind, cell_lat, cell_lon, date, status, attempts, str(e)[:500], iso(now),
             iso(now + timedelta(seconds=delay))),
        )
        log(logger, logging.WARNING, "weather fetch failed", provider=provider, kind=kind,
            cell=[cell_lat, cell_lon], date=date, attempts=attempts, error=str(e))
        return status
    store_rows(conn, provider, kind, cell_lat, cell_lon, rows)
    conn.execute(
        "INSERT OR REPLACE INTO weather_fetches VALUES (?,?,?,?,?,'ok',?,NULL,?,NULL)",
        (provider, kind, cell_lat, cell_lon, date, attempts, iso(now)),
    )
    return "ok"


def lookup(conn, provider, kind, cell_lat, cell_lon, hour_iso):
    return conn.execute(
        "SELECT * FROM weather_cache WHERE provider=? AND kind=? AND cell_lat=? AND cell_lon=? AND time_utc=?",
        (provider, kind, cell_lat, cell_lon, hour_iso),
    ).fetchone()


METNO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"


def fetch_metno(client: "OpenMeteo", lat: float, lon: float) -> list[dict]:
    """MET Norway locationforecast (ECMWF-based, global): hourly to ~60 h, then 6-hourly.

    Used only as a fallback for future wind when Open-Meteo is unavailable. Free; their
    terms require an identifying User-Agent and modest request rates.
    """
    client.budget.check_and_count("api.met.no")
    try:
        r = client.http.get(METNO_URL, params={"lat": f"{lat:.2f}", "lon": f"{lon:.2f}"}, timeout=client.timeout,
                            headers={"User-Agent": "yachtspeed/0.2 personal yacht research"})
    except requests.RequestException as e:
        raise FetchError(f"met.no network: {e}") from e
    if r.status_code != 200:
        raise FetchError(f"met.no HTTP {r.status_code}")
    rows = []
    for ts in r.json().get("properties", {}).get("timeseries", []):
        d = ts.get("data", {}).get("instant", {}).get("details", {})
        if "wind_speed" in d:
            rows.append({"time_utc": ts["time"], "grid_lat": lat, "grid_lon": lon,
                         "wind_speed_kn": to_knots(d["wind_speed"], "m/s"),
                         "wind_dir_from_deg": d.get("wind_from_direction"), "wind_gust_kn": None})
    if not rows:
        raise FetchError("met.no returned no wind data")
    return rows


def metno_wind(conn, client, clat, clon, hour: datetime):
    """Nearest MET Norway wind row within 3 h of `hour`, refreshing the cache when >3 h old."""
    def nearest():
        return conn.execute(
            "SELECT * FROM weather_cache WHERE provider='metno' AND kind='wind' AND cell_lat=? AND cell_lon=? "
            "AND ABS(julianday(time_utc) - julianday(?)) * 24 <= 3 "
            "ORDER BY ABS(julianday(time_utc) - julianday(?)) LIMIT 1",
            (clat, clon, iso(hour), iso(hour))).fetchone()

    row = nearest()
    if row is None or row["fetched_utc"] < iso(utcnow() - timedelta(hours=3)):
        try:
            store_rows(conn, "metno", "wind", clat, clon, fetch_metno(client, clat, clon))
        except (FetchError, QuotaExceeded) as e:
            log(logger, logging.WARNING, "met.no fallback failed", error=str(e))
        row = nearest()
    return row


def point_weather(conn, client, cfg, lat, lon, when: datetime, wind_provider: str | None = None):
    """Weather at a point/time, fetching (and caching) as needed.

    Returns (values, wind_provider_used, marine_provider) or raises FetchError if no wind
    is available. For live forecasts (no explicit provider, time not in the past), wind
    falls back to MET Norway when Open-Meteo fails or its quota is exhausted.
    """
    from .geo import nearest_hour, round_cell

    hour = nearest_hour(when)
    date = hour.strftime("%Y-%m-%d")
    clat, clon = round_cell(lat, cfg.cell_deg), round_cell(lon, cfg.cell_deg)
    wprov = wind_provider or wind_provider_for(date, cfg.recent_days)
    out, used_wind, problem = {}, None, None
    for prov, kind in ((wprov, "wind"), ("om_marine", "marine")):
        cached = lookup(conn, prov, kind, clat, clon, iso(hour))
        # live forecasts change with each model run, so refresh anything fetched >3h ago
        stale = cached is None or (
            prov == "om_forecast" and hour > utcnow()
            and cached["fetched_utc"] < iso(utcnow() - timedelta(hours=3))
        )
        if stale:
            try:
                st = ensure_cell_day(conn, client, prov, kind, clat, clon, date, cfg.max_fetch_attempts, force=True)
            except QuotaExceeded as e:
                st, problem = "quota", str(e)
            fresh = lookup(conn, prov, kind, clat, clon, iso(hour)) if st == "ok" else None
            cached = fresh or cached
            if st != "ok" and kind == "wind" and not problem:
                problem = f"{prov} unavailable for {date}"
        if cached:
            out.update({c: cached[c] for c in WEATHER_COLS if cached[c] is not None})
            if kind == "wind":
                used_wind = prov
        elif kind == "wind" and wind_provider is None and hour >= utcnow() - timedelta(hours=3):
            row = metno_wind(conn, client, clat, clon, hour)
            if row is not None:
                out.update({c: row[c] for c in ("wind_speed_kn", "wind_dir_from_deg") if row[c] is not None})
                used_wind = "metno"
    if used_wind is None:
        raise FetchError(problem or f"no wind for {date}")
    return out, used_wind, "om_marine"


def sleep_until(t: datetime, max_s: float = 3600) -> None:
    time.sleep(max(0.0, min(max_s, (t - utcnow()).total_seconds())))
