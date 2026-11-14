"""Hold-out evaluation of the SOG model against a per-yacht median baseline.

Splits hold out whole voyages, never individual reports, so neighbouring 10-minute
reports from the same passage cannot leak into training:
  * 'chrono': train on the earliest voyages, test on the last `test_frac` of voyages
  * 'voyage': k-fold over voyages (each voyage tested once by a model trained on the rest)
Baseline = per-yacht median SOG of the training rows, scored on exactly the same rows
the model scored.
"""

from __future__ import annotations

import statistics
from collections import defaultdict

from .model import SogModel

TWS_BINS = [(0, 6), (6, 12), (12, 18), (18, 25), (25, None)]
TWA_BINS = [(0, 60), (60, 100), (100, 140), (140, 181)]
WAVE_BINS = [(0, 0.5), (0.5, 1.5), (1.5, 2.5), (2.5, None)]


def _bin(v, bins):
    if v is None:
        return "unknown"
    for lo, hi in bins:
        if lo <= v and (hi is None or v < hi):
            return f"{lo}+" if hi is None else f"{lo}-{hi}"
    return "other"


def _score(cfg, train, test):
    model = SogModel(cfg, train)
    rows = []
    for o in test:
        p = model.predict(o.mmsi, o.tws, o.twa, o.wave, o.current if o.current_known else None)
        base = model.yacht_median(o.mmsi)
        rows.append((o, p, base))
    return rows


def splits(obs, method="chrono", test_frac=0.25, k=5):
    voyages = sorted({o.voyage for o in obs})
    if len(voyages) < 2:
        raise ValueError("need at least two voyages to hold one out")
    if method == "chrono":
        # order voyages by start time across the whole fleet
        start = {}
        for o in obs:
            start.setdefault(o.voyage, o.time_utc)
        ordered = sorted(voyages, key=lambda v: start[v])
        n_test = max(1, round(len(ordered) * test_frac))
        test_v = set(ordered[-n_test:])
        yield [o for o in obs if o.voyage not in test_v], [o for o in obs if o.voyage in test_v]
    elif method == "voyage":
        folds = [set(voyages[i::k]) for i in range(min(k, len(voyages)))]
        for f in folds:
            yield [o for o in obs if o.voyage not in f], [o for o in obs if o.voyage in f]
    else:
        raise ValueError(method)


def evaluate(cfg, obs, method="chrono", test_frac=0.25, k=5) -> dict:
    rows = []
    for train, test in splits(obs, method, test_frac, k):
        rows.extend(_score(cfg, train, test))
    scored = [(o, p, b) for o, p, b in rows if p.status == "ok" and b is not None]
    report = {
        "method": method,
        "test_rows": len(rows),
        "scored_rows": len(scored),
        "share_scored": round(len(scored) / len(rows), 3) if rows else 0,
        "mae_model_kn": None,
        "mae_baseline_kn": None,
        "mae_baseline_all_test_rows_kn": None,
        "by": {},
    }
    base_all = [abs(o.sog - b) for o, p, b in rows if b is not None]
    if base_all:
        report["mae_baseline_all_test_rows_kn"] = round(statistics.mean(base_all), 3)
    if not scored:
        return report
    report["mae_model_kn"] = round(statistics.mean(abs(o.sog - p.median) for o, p, b in scored), 3)
    report["mae_baseline_kn"] = round(statistics.mean(abs(o.sog - b) for o, p, b in scored), 3)
    inside = sum(1 for o, p, b in scored if p.p25 <= o.sog <= p.p75)
    report["share_inside_p25_p75"] = round(inside / len(scored), 3)
    inside = sum(1 for o, p, b in scored if p.p10 <= o.sog <= p.p90)
    report["share_inside_p10_p90"] = round(inside / len(scored), 3)
    for name, fn in (("tws_kn", lambda o: _bin(o.tws, TWS_BINS)),
                     ("twa_deg", lambda o: _bin(o.twa, TWA_BINS)),
                     ("wave_m", lambda o: _bin(o.wave, WAVE_BINS))):
        groups = defaultdict(lambda: {"all": 0, "m": [], "b": []})
        for o, p, b in rows:
            g = groups[fn(o)]
            g["all"] += 1
            if p.status == "ok" and b is not None:
                g["m"].append(abs(o.sog - p.median))
                g["b"].append(abs(o.sog - b))
        report["by"][name] = {
            key: {
                "test_rows": g["all"], "scored": len(g["m"]),
                "mae_model_kn": round(statistics.mean(g["m"]), 3) if g["m"] else None,
                "mae_baseline_kn": round(statistics.mean(g["b"]), 3) if g["b"] else None,
            }
            for key, g in sorted(groups.items())
        }
    return report


def evaluate_predictions(conn, tolerance_min=10) -> dict:
    """Score stored live predictions against AIS reports near their valid time.

    Matches a prediction to the same yacht's report within `tolerance_min` of valid_utc.
    Grouped by lead time so accuracy can be tracked as forecasts get older.
    """
    q = """SELECT p.id, p.lead_hours, p.sog_median_kn, p.status,
                  (SELECT r.sog_kn FROM ais_reports r WHERE r.mmsi = p.mmsi
                     AND ABS(julianday(r.provider_time_utc) - julianday(p.valid_utc)) * 1440 <= ?
                     ORDER BY ABS(julianday(r.provider_time_utc) - julianday(p.valid_utc)) LIMIT 1) AS actual
           FROM predictions p WHERE p.valid_utc <= strftime('%Y-%m-%dT%H:%M:%SZ','now')"""
    groups = defaultdict(lambda: {"n": 0, "with_actual": 0, "scored": 0, "err": []})
    for r in conn.execute(q, (tolerance_min,)):
        lh = r["lead_hours"] or 0
        key = "0-6h" if lh < 6 else "6-24h" if lh < 24 else "1-3d" if lh < 72 else "3d+"
        g = groups[key]
        g["n"] += 1
        if r["actual"] is not None:
            g["with_actual"] += 1
            if r["status"] == "ok":
                g["scored"] += 1
                g["err"].append(abs(r["actual"] - r["sog_median_kn"]))
    return {k: {"predictions": g["n"], "with_actual": g["with_actual"], "scored": g["scored"],
                "mae_kn": round(statistics.mean(g["err"]), 3) if g["err"] else None}
            for k, g in sorted(groups.items())}
