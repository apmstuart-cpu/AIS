"""Forecast-model skill, measured on the server, and the weights it gives the blend.

Option 1 (wind verification, daily):
  For every fleet fix near 00/06/12/18 UTC, each model's forecast issued 1, 2 and 3 days
  earlier (Open-Meteo previous-runs API, `*_previous_dayN`) is compared with what actually
  happened. "What happened" is the consensus analysis: the vector mean of the models'
  day-0 (latest-run) wind at that hour, so no single model is favoured as the truth.
  The error is the vector wind error in knots |V_model - V_truth|.

  Weights per lead day are proportional to 1 / mean-squared error over the last
  `window_days`, kept within [0.6/n, 1.4/n] (15-35 % for four models) so no model is
  ever dropped. A model or lead day with fewer than `min_checks` checks keeps an equal
  share. Leads under a day use the day-1 weights, beyond three days the day-3 weights,
  and in between they are interpolated.

Option 2 (ETA log): every report stores each model's ETA for the course plus the
headline median (eta_log); arrivals are recorded when a yacht is first within 2 nm of
its destination (arrivals). `yachtspeed skill --show` compares the two.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from .geo import iso, parse_iso, utcnow
from .weather import MODELS, FetchError, QuotaExceeded

SCHEMA = """
CREATE TABLE IF NOT EXISTS model_skill (
    valid_utc TEXT NOT NULL, mmsi TEXT NOT NULL, model TEXT NOT NULL, lead_day INTEGER NOT NULL,
    lat REAL, lon REAL, truth_kn REAL, truth_dir REAL, model_kn REAL, model_dir REAL,
    vec_err_kn REAL, speed_err_kn REAL, truth_models INTEGER, checked_utc TEXT,
    PRIMARY KEY (valid_utc, mmsi, model, lead_day)
);
CREATE TABLE IF NOT EXISTS eta_log (
    id INTEGER PRIMARY KEY, report_utc TEXT, mmsi TEXT, destination TEXT, fix_utc TEXT,
    lat REAL, lon REAL, to_go_nm REAL, model TEXT, eta_utc TEXT, hours REAL, weight REAL, basis TEXT
);
CREATE INDEX IF NOT EXISTS ix_eta_log ON eta_log(mmsi, destination, fix_utc);
CREATE TABLE IF NOT EXISTS arrivals (
    mmsi TEXT NOT NULL, destination TEXT NOT NULL, arrived_utc TEXT NOT NULL, recorded_utc TEXT,
    PRIMARY KEY (mmsi, destination, arrived_utc)
);
"""

LEAD_DAYS = (1, 2, 3, 5, 7)  # previous-runs API keeps forecasts issued up to 7 days earlier
HOURS = (0, 6, 12, 18)


def ensure_schema(conn):
    conn.executescript(SCHEMA)


def _uv(spd, frm):
    r = math.radians(frm)
    return -spd * math.sin(r), -spd * math.cos(r)


def _from_uv(u, v):
    return math.hypot(u, v), (math.degrees(math.atan2(-u, -v)) + 360) % 360


# ---------------------------------------------------------------- option 1: verification

def fixes_for_day(conn, mmsis, day: str, tol_min=90):
    """One fix per yacht nearest each of 00/06/12/18 UTC on `day` (within tol_min)."""
    out = []
    d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    for mmsi in mmsis:
        for h in HOURS:
            t = d0 + timedelta(hours=h)
            row = conn.execute(
                "SELECT provider_time_utc, lat, lon FROM ais_reports WHERE mmsi=? AND provider_time_utc BETWEEN ? AND ? "
                "ORDER BY abs(julianday(provider_time_utc) - julianday(?)) LIMIT 1",
                (mmsi, iso(t - timedelta(minutes=tol_min)), iso(t + timedelta(minutes=tol_min)), iso(t))).fetchone()
            if row:
                out.append({"mmsi": mmsi, "valid": t, "lat": round(row["lat"] * 4) / 4,
                            "lon": round(row["lon"] * 4) / 4})
    return out


def _fetch_prev(client, model_id, lat, lon, day, end_day=None):
    names = ["wind_speed_10m", "wind_direction_10m"]
    for n in LEAD_DAYS:
        names += [f"wind_speed_10m_previous_day{n}", f"wind_direction_10m_previous_day{n}"]
    body = client._get("om_prev", {"latitude": lat, "longitude": lon, "start_date": day, "end_date": end_day or day,
                                   "hourly": ",".join(names), "models": model_id, "wind_speed_unit": "kn",
                                   "timezone": "GMT"})
    h = body.get("hourly") or {}
    times = [t + ":00Z" if len(t) == 16 else t for t in h.get("time") or []]
    out = {}
    for i, t in enumerate(times):
        rec = {}
        for lead, (sk, dk) in [(0, ("wind_speed_10m", "wind_direction_10m"))] + [
                (n, (f"wind_speed_10m_previous_day{n}", f"wind_direction_10m_previous_day{n}")) for n in LEAD_DAYS]:
            s, d = (h.get(sk) or [None] * len(times))[i], (h.get(dk) or [None] * len(times))[i]
            if s is not None and d is not None:
                rec[lead] = (float(s), float(d))
        out[t] = rec
    return out


def verify_day(conn, client, cfg, day: str, models=None) -> dict:
    """Score every model at every fleet fix on `day`. Returns counts; raises QuotaExceeded."""
    ensure_schema(conn)
    models = models or list(MODELS)
    pts = fixes_for_day(conn, cfg.mmsis, day)
    stats = {"day": day, "points": len(pts), "checks": 0, "errors": 0}
    cache = {}
    for p in pts:
        key = (p["lat"], p["lon"])
        if key not in cache:
            cache[key] = {}
            for m in models:
                try:
                    cache[key][m] = _fetch_prev(client, MODELS[m], p["lat"], p["lon"], day)
                except FetchError:
                    stats["errors"] += 1
                    cache[key][m] = {}
        data = cache[key]
        t = iso(p["valid"])
        day0 = {m: data[m].get(t, {}).get(0) for m in models}
        truth = [v for v in day0.values() if v]
        if len(truth) < 3:
            continue
        uvs = [_uv(*v) for v in truth]
        tu, tv = sum(a for a, _ in uvs) / len(uvs), sum(b for _, b in uvs) / len(uvs)
        tspd, tdir = _from_uv(tu, tv)
        for m in models:
            for n in LEAD_DAYS:
                f = data[m].get(t, {}).get(n)
                if not f:
                    continue
                mu, mv = _uv(*f)
                conn.execute(
                    "INSERT OR REPLACE INTO model_skill VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t, p["mmsi"], m, n, p["lat"], p["lon"], round(tspd, 2), round(tdir), f[0], f[1],
                     round(math.hypot(mu - tu, mv - tv), 2), round(f[0] - tspd, 2), len(truth), iso(utcnow())))
                stats["checks"] += 1
    return stats


def corridor_points(start, dest, n=12, trim=0.04):
    """Points spread along a passage (straight in lat/lon), a little away from both ends' coasts."""
    out = []
    for i in range(n):
        f = trim + (1 - 2 * trim) * i / (n - 1)
        out.append((round(start[0] + (dest[0] - start[0]) * f, 2), round(start[1] + (dest[1] - start[1]) * f, 2)))
    return out


def verify_corridor(conn, client, points, days=30, models=None, progress=None) -> dict:
    """Score each model's past forecasts (1-7 days ahead) along a passage corridor over the last `days`
    days: one request per point and model covers the whole period. Truth is the consensus of the models'
    latest-run analyses, as for the fleet checks. Raises QuotaExceeded."""
    ensure_schema(conn)
    models = models or list(MODELS)
    end = (utcnow() - timedelta(days=1)).date()
    start = end - timedelta(days=days - 1)
    stats = {"points": len(points), "checks": 0, "errors": 0, "from": start.isoformat(), "to": end.isoformat()}
    for la, lo in points:
        data = {}
        for m in models:
            try:
                data[m] = _fetch_prev(client, MODELS[m], la, lo, start.isoformat(), end.isoformat())
            except FetchError:
                stats["errors"] += 1
                data[m] = {}
        tag = f"corridor:{la:.2f},{lo:.2f}"
        times = sorted({t for d in data.values() for t in d if parse_iso(t).hour in HOURS})
        conn.execute("BEGIN")
        for t in times:
            truth = [data[m].get(t, {}).get(0) for m in models]
            truth = [v for v in truth if v]
            if len(truth) < 3:
                continue
            uvs = [_uv(*v) for v in truth]
            tu, tv = sum(a for a, _ in uvs) / len(uvs), sum(b for _, b in uvs) / len(uvs)
            tspd, tdir = _from_uv(tu, tv)
            for m in models:
                for n in LEAD_DAYS:
                    f = data[m].get(t, {}).get(n)
                    if not f:
                        continue
                    mu, mv = _uv(*f)
                    conn.execute("INSERT OR REPLACE INTO model_skill VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (t, tag, m, n, la, lo, round(tspd, 2), round(tdir), f[0], f[1],
                                  round(math.hypot(mu - tu, mv - tv), 2), round(f[0] - tspd, 2), len(truth),
                                  iso(utcnow())))
                    stats["checks"] += 1
        conn.execute("COMMIT")
        if progress:
            progress(f"{la:.1f},{lo:.1f}: {stats['checks']} checks so far")
    return stats


def run_verification(conn, client, cfg, back_days=14, models=None) -> list:
    """Verify each complete UTC day in the last `back_days` that has not been verified yet."""
    from . import db

    ensure_schema(conn)
    today = utcnow().date()
    done = []
    for k in range(back_days, 0, -1):
        day = (today - timedelta(days=k)).isoformat()
        if db.kv_get(conn, f"skill_done:{day}"):
            continue
        try:
            st = verify_day(conn, client, cfg, day, models)
        except QuotaExceeded as e:
            done.append({"day": day, "stopped": str(e)})
            break
        if st["errors"] == 0:
            db.kv_set(conn, f"skill_done:{day}", iso(utcnow()))
        done.append(st)
    return done


def scores(conn, window_days=30, models=None):
    """{lead_day: {model: (n, rmse_kn, mean_speed_bias_kn)}} over the window."""
    ensure_schema(conn)
    models = models or list(MODELS)
    since = iso(utcnow() - timedelta(days=window_days))
    out = {n: {} for n in LEAD_DAYS}
    for r in conn.execute("SELECT lead_day, model, count(*) n, avg(vec_err_kn*vec_err_kn) mse, avg(speed_err_kn) bias "
                          "FROM model_skill WHERE valid_utc >= ? GROUP BY lead_day, model", (since,)):
        if r["lead_day"] in out and r["model"] in models:
            out[r["lead_day"]][r["model"]] = (r["n"], math.sqrt(r["mse"]), r["bias"])
    return out


def _bucket_weights(sc, models, min_checks):
    n = len(models)
    lo, hi = 0.6 / n, 1.4 / n
    scored = {m: sc[m] for m in models if m in sc and sc[m][0] >= min_checks}
    w = {m: 1.0 / n for m in models}
    if len(scored) >= 2:
        share = len(scored) / n
        inv = {m: 1.0 / max(s[1], 0.1) ** 2 for m, s in scored.items()}
        tot = sum(inv.values())
        for m in scored:
            w[m] = share * inv[m] / tot
    for _ in range(20):  # clip to the band and renormalise
        w = {m: min(hi, max(lo, x)) for m, x in w.items()}
        s = sum(w.values())
        w = {m: x / s for m, x in w.items()}
        if all(lo - 1e-9 <= x <= hi + 1e-9 for x in w.values()):
            break
    return w


class Weights:
    """Model weights as a function of forecast lead time (hours after now)."""

    def __init__(self, conn, models, window_days=30, min_checks=12, enabled=True):
        self.models = list(models)
        self.sc = scores(conn, window_days, self.models) if enabled else {n: {} for n in LEAD_DAYS}
        self.by_day = {n: _bucket_weights(self.sc[n], self.models, min_checks) for n in LEAD_DAYS}
        self.checks = sum(s[0] for d in self.sc.values() for s in d.values())
        self.equal = all(abs(x - 1 / len(self.models)) < 1e-6 for d in self.by_day.values() for x in d.values())
        self.now = utcnow()

    def at_lead(self, lead_h: float) -> dict:
        """Weights at a lead time, interpolated between the verified lead days (held beyond the ends)."""
        days = sorted(self.by_day)
        d = min(days[-1], max(days[0], lead_h / 24.0))
        a = max(x for x in days if x <= d)
        b = min(x for x in days if x >= d)
        f = 0.0 if b == a else (d - a) / (b - a)
        return {m: self.by_day[a][m] * (1 - f) + self.by_day[b][m] * f for m in self.models}

    def at(self, t: datetime) -> dict:
        return self.at_lead((t - self.now).total_seconds() / 3600)

    def over(self, t0: datetime, t1: datetime) -> dict:
        """Mean weights over a passage (sampled every 3 h)."""
        n = max(1, int((t1 - t0).total_seconds() / 3600 / 3))
        acc = {m: 0.0 for m in self.models}
        for i in range(n + 1):
            for m, x in self.at(t0 + (t1 - t0) * (i / n)).items():
                acc[m] += x / (n + 1)
        return acc


def weighted_median_time(items):
    """items: [(datetime, weight)] -> weighted median (midpoint when the 50 % mark falls on a boundary)."""
    items = sorted(items)
    tot = sum(w for _, w in items)
    cum = 0.0
    for i, (t, w) in enumerate(items):
        cum += w / tot
        if abs(cum - 0.5) < 1e-9 and i + 1 < len(items):
            return t + (items[i + 1][0] - t) / 2
        if cum > 0.5:
            return t
    return items[-1][0]


# ---------------------------------------------------------------- option 2: ETA log

def log_etas(conn, y_out: dict, report_utc: str):
    ensure_schema(conn)
    e, f = y_out.get("eta"), y_out.get("fix") or {}
    if not e:
        return
    rows = [("median", e["median_utc"], None, None)]
    rt = next((r for r in y_out.get("routed") or [] if r.get("course_under_models")), None)
    wts = e.get("weights") or {}
    if rt and e.get("basis", "").startswith("routed"):
        rows += [(c["model"], c["eta_utc"], c.get("hours"), wts.get(c["model"]))
                 for c in rt["course_under_models"] if c.get("eta_utc")]
    else:
        rows += [(m["model"], m["eta_utc"], m.get("hours"), None) for m in y_out.get("models") or []
                 if m.get("eta_utc")]
    for model, eta, hours, w in rows:
        conn.execute("INSERT INTO eta_log(report_utc, mmsi, destination, fix_utc, lat, lon, to_go_nm, model, "
                     "eta_utc, hours, weight, basis) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (report_utc, y_out["mmsi"], y_out.get("destination"), f.get("time_utc"), f.get("lat"),
                      f.get("lon"), y_out.get("to_go_nm"), model, eta, hours, w, e.get("basis")))


def record_arrival(conn, mmsi, destination, dest_latlon, arrived_nm=2.0):
    """First fix within arrived_nm of the destination after the first logged ETA for it."""
    from .geo import haversine_nm

    ensure_schema(conn)
    first = conn.execute("SELECT min(fix_utc) FROM eta_log WHERE mmsi=? AND destination=?",
                         (mmsi, destination)).fetchone()[0]
    if not first:
        return None
    done = conn.execute("SELECT arrived_utc FROM arrivals WHERE mmsi=? AND destination=? AND arrived_utc>=?",
                        (mmsi, destination, first)).fetchone()
    if done:
        return done[0]
    for r in conn.execute("SELECT provider_time_utc, lat, lon FROM ais_reports WHERE mmsi=? AND provider_time_utc>=? "
                          "ORDER BY provider_time_utc", (mmsi, first)):
        if haversine_nm(r["lat"], r["lon"], *dest_latlon) <= arrived_nm:
            conn.execute("INSERT OR IGNORE INTO arrivals VALUES (?,?,?,?)",
                         (mmsi, destination, r["provider_time_utc"], iso(utcnow())))
            return r["provider_time_utc"]
    return None


def eta_skill(conn):
    """Per arrival: mean absolute ETA error (h) per model, by hours-before-arrival bucket."""
    ensure_schema(conn)
    out = []
    for a in conn.execute("SELECT * FROM arrivals ORDER BY arrived_utc"):
        arr = parse_iso(a["arrived_utc"])
        rows = conn.execute("SELECT model, fix_utc, eta_utc FROM eta_log WHERE mmsi=? AND destination=? "
                            "AND fix_utc < ?", (a["mmsi"], a["destination"], a["arrived_utc"])).fetchall()
        acc = {}
        for r in rows:
            before = (arr - parse_iso(r["fix_utc"])).total_seconds() / 3600
            b = "0-12h" if before < 12 else "12-24h" if before < 24 else "24-48h" if before < 48 else "48h+"
            err = abs((parse_iso(r["eta_utc"]) - arr).total_seconds() / 3600)
            acc.setdefault(r["model"], {}).setdefault(b, []).append(err)
        out.append({"mmsi": a["mmsi"], "destination": a["destination"], "arrived_utc": a["arrived_utc"],
                    "mae_h": {m: {b: (round(sum(v) / len(v), 2), len(v)) for b, v in d.items()}
                              for m, d in acc.items()}})
    return out
