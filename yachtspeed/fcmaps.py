"""Forecast wind maps for the first days of a passage, every few hours, one page per day, plus a
written summary of the forecast assumptions. Drawn on the server with Pillow; published with the
report as climate_<name>_day<N>.jpg and climate_<name>_assumptions.md.

Each panel shows the skill-weighted blend of the forecast models (colour = wind strength, arrows =
direction) and, if a strategies result is given, the median routed course of the chosen options with
the yacht's position at that hour.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .geo import iso, parse_iso, utcnow

BANDS = [(0, (205, 225, 245)), (6, (150, 205, 235)), (10, (120, 200, 140)), (14, (200, 225, 100)),
         (18, (250, 210, 80)), (22, (245, 150, 60)), (26, (220, 70, 60)), (32, (150, 50, 140))]
TRACK_COLOURS = {"A": (200, 30, 40), "B": (20, 90, 190), "C": (30, 140, 70), "D": (230, 120, 0), "E": (120, 40, 150)}


def colour(kn):
    c = BANDS[0][1]
    for lim, col in BANDS:
        if kn >= lim:
            c = col
    return c


def _font(size):
    from .chart import _font as f

    return f(size)


def median_tracks(strategies_json, keys):
    """{key: [(t, lat, lon)]} for the median-time forecast run of each chosen option."""
    out = {}
    if not strategies_json or not Path(strategies_json).exists():
        return out
    o = json.loads(Path(strategies_json).read_text())
    for k in keys:
        v = (o.get("strategies") or {}).get(k)
        if not v:
            continue
        ok = sorted((r for r in v.get("results", []) if r.get("status") == "ok"), key=lambda r: r["days"])
        if ok:
            med = ok[len(ok) // 2]
            out[k] = [(parse_iso(p[0]), p[1], p[2]) for p in med["track"]]
    return out


def position_at(track, t):
    for (t0, a0, b0), (t1, a1, b1) in zip(track, track[1:]):
        if t0 <= t <= t1:
            f = (t - t0).total_seconds() / max(1.0, (t1 - t0).total_seconds())
            return a0 + (a1 - a0) * f, b0 + (b1 - b0) * f
    return None


def panel(grid, bbox, t, tracks, land_rings, w=880, h=560, label=""):
    from PIL import Image, ImageDraw

    lat0, lon0, lat1, lon1 = bbox
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    top = 30
    ph = h - top

    def xy(la, lo):
        return (lo - lon0) / (lon1 - lon0) * w, top + (lat1 - la) / (lat1 - lat0) * ph

    step = 8  # pixels per colour block
    for py in range(top, h, step):
        la = lat1 - (py - top + step / 2) / ph * (lat1 - lat0)
        for px in range(0, w, step):
            lo = lon0 + (px + step / 2) / w * (lon1 - lon0)
            s = grid.sample(la, lo, t)
            if s:
                d.rectangle([px, py, px + step, py + step], fill=colour(s["wind_speed_kn"]))
    for ring in land_rings:
        pts = [xy(p[1], p[0]) for p in ring]
        if len(pts) > 2:
            d.polygon(pts, fill=(236, 228, 205), outline=(150, 140, 110))
    f_s, f_t = _font(15), _font(19)
    la = math.ceil(lat0 / 2) * 2
    while la < lat1:  # light gridlines every 2 deg
        y = xy(la, lon0)[1]
        d.line([(0, y), (w, y)], fill=(255, 255, 255), width=1)
        d.text((3, y + 1), f"{abs(la):g}°N", fill=(60, 60, 70), font=f_s)
        la += 2
    lo = math.ceil(lon0 / 4) * 4
    while lo < lon1:
        x = xy(lat0, lo)[0]
        d.line([(x, top), (x, h)], fill=(255, 255, 255), width=1)
        d.text((x + 3, h - 18), f"{abs(lo):g}°W", fill=(60, 60, 70), font=f_s)
        lo += 4
    dstep = 1.5  # wind arrows (pointing downwind) every 1.5 deg
    a = lat0 + dstep / 2
    while a < lat1:
        b = lon0 + dstep / 2
        while b < lon1:
            s = grid.sample(a, b, t)
            if s:
                x, y = xy(a, b)
                r = math.radians(s["wind_dir_from_deg"])
                ux, uy = -math.sin(r), math.cos(r)  # screen: y grows downwards
                ln = 10 + min(s["wind_speed_kn"], 30) * 0.6
                x0, y0, x1, y1 = x - ux * ln / 2, y - uy * ln / 2, x + ux * ln / 2, y + uy * ln / 2
                d.line([(x0, y0), (x1, y1)], fill=(40, 40, 50), width=2)
                hx, hy = -uy, ux
                d.polygon([(x1, y1), (x1 - ux * 7 + hx * 4, y1 - uy * 7 + hy * 4),
                           (x1 - ux * 7 - hx * 4, y1 - uy * 7 - hy * 4)], fill=(40, 40, 50))
            b += dstep
        a += dstep
    for k, tr in tracks.items():
        col = TRACK_COLOURS.get(k, (0, 0, 0))
        pts = [xy(p[1], p[2]) for p in tr if lat0 <= p[1] <= lat1 and lon0 <= p[2] <= lon1]
        if len(pts) > 1:
            d.line(pts, fill=col, width=3)
        pos = position_at(tr, t)
        if pos:
            x, y = xy(*pos)
            d.ellipse([x - 8, y - 8, x + 8, y + 8], fill=col, outline=(255, 255, 255), width=2)
            d.text((x + 10, y - 8), k, fill=col, font=f_t)
    d.rectangle([0, 0, w, top], fill=(255, 255, 255))
    d.text((8, 5), label, fill=(20, 20, 30), font=f_t)
    return img


def legend(w, tracks):
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, 60), (255, 255, 255))
    d = ImageDraw.Draw(img)
    f = _font(17)
    x = 10
    d.text((x, 4), "True wind (kn):", fill=(20, 20, 30), font=f)
    x += 140
    for i, (lim, col) in enumerate(BANDS):
        nxt = BANDS[i + 1][0] if i + 1 < len(BANDS) else None
        lab = f"{lim}-{nxt}" if nxt else f"{lim}+"
        d.rectangle([x, 6, x + 26, 22], fill=col, outline=(90, 90, 90))
        d.text((x + 30, 4), lab, fill=(20, 20, 30), font=f)
        x += 95
    x = 10
    d.text((x, 32), "Arrows point downwind.  Routed course and position at that hour:", fill=(20, 20, 30), font=f)
    x += 560
    for k in tracks:
        d.ellipse([x, 34, x + 16, 50], fill=TRACK_COLOURS.get(k, (0, 0, 0)))
        d.text((x + 22, 32), k, fill=TRACK_COLOURS.get(k, (0, 0, 0)), font=f)
        x += 60
    return img


def build(conn, cfg, client, start, depart: datetime, out_dir, name, days=4, step_h=3, res=1.0,
          models=("ecmwf", "aifs", "gfs", "icon"), strategies_json=None, keys=("A", "D"), bbox=None) -> dict:
    from PIL import Image, ImageDraw

    from .land import EATL_10M, WORLD_10M, WORLD_50M, _intersects, _load
    from .router import BlendGrid, WeatherGrid
    from .weather import model_provider

    tracks = median_tracks(strategies_json, keys)
    if bbox is None:  # the area the first `days` of the routes cover, with a margin
        lats, lons = [start[0]], [start[1]]
        for tr in tracks.values():
            for t, la, lo in tr:
                if t <= depart + timedelta(days=days):
                    lats.append(la)
                    lons.append(lo)
        bbox = (math.floor(min(lats) - 3), math.floor(min(lons) - 4), math.ceil(max(lats) + 3), math.ceil(max(lons) + 3))
    t_end = depart + timedelta(days=days, hours=3)
    grids, used = [], []
    for m in models:
        g = WeatherGrid(conn, client, bbox, depart - timedelta(hours=3), t_end, model_provider(m), res=res,
                        use_marine=False).load()
        if g.wind:
            grids.append(g)
            used.append(m)
    if not grids:
        return {"status": "no forecast"}
    weights = None
    if getattr(cfg, "skill_weighting", False):
        from .skill import Weights

        weights = Weights(conn, used, cfg.skill_window_days, cfg.skill_min_checks)
    blend = BlendGrid(grids, used, weights)
    world = str(WORLD_10M) if WORLD_10M.exists() else str(WORLD_50M)
    lb = (bbox[0], bbox[1], bbox[2], bbox[3])
    rings = [it[0] for it in _load(world) if _intersects(it, lb)]
    if world == str(WORLD_50M):
        rings += [it[0] for it in _load(str(EATL_10M)) if _intersects(it, lb)]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = []
    pw, ph = 880, 560
    for day in range(days):
        times = [depart + timedelta(days=day, hours=h) for h in range(0, 24, step_h)]
        cols, rows = 2, math.ceil(len(times) / 2)
        sheet = Image.new("RGB", (cols * pw + 30, rows * ph + 150), (255, 255, 255))
        dd = ImageDraw.Draw(sheet)
        dd.text((15, 10), f"Forecast wind, day {day + 1} of the passage: {times[0]:%a %d %b} "
                          f"(blend of {', '.join(used)}; issued about {iso(utcnow())[:16]}Z)", fill=(20, 20, 30),
                font=_font(26))
        sheet.paste(legend(cols * pw, tracks), (15, 48))
        for i, t in enumerate(times):
            p = panel(blend, bbox, t, tracks, rings, pw, ph,
                      f"{t:%a %d %b %H:%M} UTC  (+{int((t - depart).total_seconds() // 3600)} h)")
            sheet.paste(p, (15 + (i % 2) * pw, 120 + (i // 2) * ph))
        path = out / f"climate_{name}_day{day + 1}.jpg"
        sheet.save(path, "JPEG", quality=85, optimize=True)
        files.append(str(path))
    return {"status": "ok", "files": files, "bbox": bbox, "models": used,
            "weights": ({f"{n} d ahead": {m: round(100 * x) for m, x in w.items()} for n, w in weights.by_day.items()}
                        if weights is not None else None)}


def assumptions_md(info, cfg, strategies_json=None, climate_json=None) -> str:
    """The forecast and modelling assumptions behind a run, as text for a report appendix."""
    lines = ["# Forecast and model assumptions", ""]
    st = json.loads(Path(strategies_json).read_text()) if strategies_json and Path(strategies_json).exists() else {}
    cl = json.loads(Path(climate_json).read_text()) if climate_json and Path(climate_json).exists() else {}
    issued = st.get("issued_utc") or (cl.get("forecast") or {}).get("issued_utc")
    lines += [f"- **Forecast models:** {', '.join(info.get('models') or st.get('models_used') or [])}, blended "
              f"(wind averaged as vectors), issued {issued or '?'}.",
              "- **Model weights** from each model's past accuracy along the route (wind error against the "
              "models' consensus analysis, last 30 days), kept within 15-35 % each:"]
    w = info.get("weights") or st.get("model_weights") or {}
    for lead, ws in w.items():
        lines.append(f"  - {lead}: " + ", ".join(f"{m} {p}%" for m, p in ws.items()))
    hd = st.get("handover_day") or (cl.get("forecast") or {}).get("handover_day")
    lines += [f"- **Forecast into history:** the forecast's weight fades steadily from 2 days after issue to zero "
              f"at 15 days; for this run from day {hd[0] if hd else '?'} to day {hd[1] if hd else '?'} of the "
              "passage. Each of 20 past years' weather (ERA5, 2006-2025) then takes over: 20 possible endings.",
              ]
    en = cl.get("enso") or {}
    if en.get("now"):
        lines.append(f"- **El Nino weighting:** ONI {en['now'].get('oni')} ({en['now'].get('season')}); past years "
                     "weighted by how close their Oct-Dec ONI was (2015 and 2023 most).")
    s = st.get("settings") or cl.get("settings") or {}
    lines += [f"- **Yacht:** polar {s.get('table')} x {s.get('polar_factor')}; 3 min per tack or gybe; motor at "
              f"{s.get('motor_speed_kn')} kn below {s.get('motor_wind_below_kn')} kn of wind (option D: below 7 kn), "
              f"at most {s.get('motor_max_h')} h.",
              "- **Sea state:** ERA5 ocean waves for past years and wind-derived seas in the forecast; following seas "
              "add up to +3 % (surfing), head and beam seas slow her.",
              "- **Current:** mean surface current for these dates over 2022-2025.",
              "- **Not modelled:** squalls, gusts, crew fatigue, breakages, sail changes beyond the polar.",
              "", f"Wind maps: every 3 h for the first 4 days, area {info.get('bbox')}, colours by wind strength, "
              "arrows downwind; the coloured dots are the median routed course of each option at that hour."]
    return "\n".join(lines) + "\n"
