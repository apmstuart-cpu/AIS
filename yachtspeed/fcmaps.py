"""Forecast wind maps for the first days of a passage, every few hours, one page per day, plus a
written summary of the forecast assumptions. Drawn on the server with Pillow; published with the
report as climate_<name>_day<N>.jpg and climate_<name>_assumptions.md.

Each panel shows the skill-weighted blend of the forecast models (colour = wind strength, arrows =
direction, as white streaks) and, if a strategies result is given, the most typical routed course of the chosen options with
the yacht's position at that hour.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .geo import iso, parse_iso, utcnow

# Continuous wind colour scale in the style of the popular weather-map sites: deep blue in calms,
# through teal and green in the trades, to yellow, red and purple in gales. (knots, RGB)
SCALE = [(0, (98, 113, 183)), (2, (57, 97, 159)), (6, (74, 148, 169)), (10, (77, 141, 123)), (14, (83, 165, 83)),
         (17.5, (53, 159, 53)), (21, (167, 157, 81)), (25, (159, 127, 58)), (29, (161, 108, 92)), (33, (129, 58, 78)),
         (37, (175, 80, 136)), (41, (117, 74, 147)), (47, (109, 97, 163)), (60, (90, 90, 110))]
LEGEND_TICKS = [0, 5, 10, 20, 30, 40, 60]  # evenly spaced on the colour bar, as on the weather apps
BANDS = SCALE  # kept for older callers
PLACES = [("Las Palmas", 28.13, -15.42), ("Santa Cruz", 28.47, -16.25),
          ("Funchal", 32.65, -16.91), ("Mindelo", 16.89, -24.98), ("Dakhla", 23.70, -15.93),
          ("Nouadhibou", 20.93, -17.03), ("Praia", 14.92, -23.51), ("English Harbour", 17.00, -61.76),
          ("Barbados", 13.10, -59.62), ("Martinique", 14.60, -61.07), ("Azores", 37.74, -25.67)]
TRACK_COLOURS = {"A": (255, 70, 80), "B": (80, 160, 255), "C": (60, 230, 120), "D": (255, 170, 30), "E": (210, 120, 255), "F": (40, 230, 230)}


def colour(kn):
    """Wind colour for `kn`, interpolated smoothly along SCALE."""
    if kn is None:
        return (60, 60, 70)
    if kn <= SCALE[0][0]:
        return SCALE[0][1]
    for (k0, c0), (k1, c1) in zip(SCALE, SCALE[1:]):
        if kn <= k1:
            f = (kn - k0) / (k1 - k0)
            return tuple(int(round(a + (b - a) * f)) for a, b in zip(c0, c1))
    return SCALE[-1][1]


def _font(size):
    from .chart import _font as f

    return f(size)


def median_tracks(strategies_json, keys):
    """{key: [(t, lat, lon)]} for the most typical forecast run of each chosen option (the run whose
    track stays closest to the others; see climate.typical_run)."""
    from .climate import typical_run

    out = {}
    if not strategies_json or not Path(strategies_json).exists():
        return out
    o = json.loads(Path(strategies_json).read_text())
    for k in keys:
        v = (o.get("strategies") or {}).get(k)
        if not v:
            continue
        med = typical_run(v.get("results", []))
        if med:
            out[k] = [(parse_iso(p[0]), p[1], p[2]) for p in med["track"]]
    return out


def position_at(track, t):
    for (t0, a0, b0), (t1, a1, b1) in zip(track, track[1:]):
        if t0 <= t <= t1:
            f = (t - t0).total_seconds() / max(1.0, (t1 - t0).total_seconds())
            return a0 + (a1 - a0) * f, b0 + (b1 - b0) * f
    return None


class _Field:
    """The wind on a regular lat/lon lattice at one time, sampled once from the grid, with bilinear lookup."""

    def __init__(self, grid, bbox, t, d=0.25):
        lat0, lon0, lat1, lon1 = bbox
        self.lat0, self.lon0, self.d = lat0, lon0, d
        self.ny, self.nx = int(round((lat1 - lat0) / d)) + 1, int(round((lon1 - lon0) / d)) + 1
        self.u = [[None] * self.nx for _ in range(self.ny)]
        self.v = [[None] * self.nx for _ in range(self.ny)]
        self.s = [[None] * self.nx for _ in range(self.ny)]
        for j in range(self.ny):
            for i in range(self.nx):
                x = grid.sample(lat0 + j * d, lon0 + i * d, t)
                if x and x.get("wind_speed_kn") is not None:
                    r = math.radians(x["wind_dir_from_deg"])
                    sp = x["wind_speed_kn"]
                    self.s[j][i] = sp
                    self.u[j][i], self.v[j][i] = -sp * math.sin(r), -sp * math.cos(r)  # towards east, north

    def at(self, la, lo, arr=None):
        arr = arr or self.s
        fy, fx = (la - self.lat0) / self.d, (lo - self.lon0) / self.d
        j, i = int(math.floor(fy)), int(math.floor(fx))
        if not (0 <= j < self.ny - 1 and 0 <= i < self.nx - 1):
            return None
        fy, fx = fy - j, fx - i
        acc = wsum = 0.0
        for dj, di, w in ((0, 0, (1 - fy) * (1 - fx)), (0, 1, (1 - fy) * fx), (1, 0, fy * (1 - fx)), (1, 1, fy * fx)):
            val = arr[j + dj][i + di]
            if val is not None and w > 0:
                acc += val * w
                wsum += w
        return acc / wsum if wsum else None

    def uv(self, la, lo):
        u, v = self.at(la, lo, self.u), self.at(la, lo, self.v)
        return (u, v) if u is not None and v is not None else None


def panel(grid, bbox, t, tracks, land_rings, w=880, h=560, label=""):
    """One map: smooth wind colours, white streamlines along the wind, darkened land with white coasts,
    and each option's routed course with the yacht's position at time `t`."""
    from PIL import Image, ImageDraw, ImageFilter

    lat0, lon0, lat1, lon1 = bbox
    top = 34
    ph = h - top
    field = _Field(grid, bbox, t)

    def xy(la, lo):
        return (lo - lon0) / (lon1 - lon0) * w, top + (lat1 - la) / (lat1 - lat0) * ph

    # 1. wind colours: one pixel per lattice point, smoothly enlarged
    small = Image.new("RGB", (field.nx, field.ny))
    sp = small.load()
    for j in range(field.ny):
        for i in range(field.nx):
            sp[i, field.ny - 1 - j] = colour(field.s[j][i])
    base = small.resize((w, ph), Image.BICUBIC).filter(ImageFilter.GaussianBlur(2))
    img = Image.new("RGB", (w, h), (24, 26, 34))
    img.paste(base, (0, top))

    # 2. land keeps its wind colour, slightly shaded, with a thin dark coastline
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    rings = []
    for ring in land_rings:
        pts = [xy(p[1], p[0]) for p in ring]
        if len(pts) > 2:
            md.polygon(pts, fill=45)
            rings.append(pts)
    img.paste(Image.new("RGB", (w, h), (10, 10, 20)), (0, 0), mask)
    d = ImageDraw.Draw(img, "RGBA")
    for pts in rings:
        d.line(pts + [pts[0]], fill=(25, 25, 35, 230), width=2)
    # 3. graticule
    f_s, f_t = _font(14), _font(19)
    la = math.ceil(lat0 / 2) * 2
    while la < lat1:
        y = xy(la, lon0)[1]
        d.line([(0, y), (w, y)], fill=(255, 255, 255, 45), width=1)
        d.text((4, y + 1), f"{abs(la):g}°N", fill=(255, 255, 255, 190), font=f_s)
        la += 2
    lo = math.ceil(lon0 / 4) * 4
    while lo < lon1:
        x = xy(lat0, lo)[0]
        d.line([(x, top), (x, h)], fill=(255, 255, 255, 45), width=1)
        d.text((x + 4, h - 18), f"{abs(lo):g}°W", fill=(255, 255, 255, 190), font=f_s)
        lo += 4

    # 4. wind particles: many short, thin white streaks along the wind, brightest at the head
    import random

    rnd = random.Random(7)  # same scatter on every panel, so the eye follows the wind, not the noise
    over = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(over)
    dpp = (lon1 - lon0) / w  # degrees per pixel
    for _ in range(int(w * ph / 650)):
        px, py = rnd.uniform(0, w), rnd.uniform(top, h)
        la_ = lat1 - (py - top) / ph * (lat1 - lat0)
        lo_ = lon0 + px / w * (lon1 - lon0)
        pts = [(la_, lo_)]
        for _ in range(5):
            uvv = field.uv(*pts[-1])
            if not uvv:
                break
            u, v = uvv
            spd = math.hypot(u, v)
            if spd < 0.5:
                break
            k = dpp * (1.0 + 0.09 * min(spd, 35))  # 5-20 px streaks: longer in stronger wind
            pts.append((pts[-1][0] + v / spd * k,
                        pts[-1][1] + u / spd * k / max(0.3, math.cos(math.radians(pts[-1][0])))))
        if len(pts) < 3:
            continue
        xs = [xy(a, b) for a, b in pts]
        n = len(xs) - 1
        for q in range(n):
            od.line([xs[q], xs[q + 1]], fill=(255, 255, 255, int(20 + 170 * (q + 1) / n)), width=2)
    img.paste(over, (0, 0), over)
    d = ImageDraw.Draw(img, "RGBA")
    f_p = _font(15)
    for name, pla, plo in PLACES:
        if lat0 < pla < lat1 and lon0 < plo < lon1:
            x, y = xy(pla, plo)
            d.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(255, 255, 255, 230))
            d.text((x + 5, y - 9), name, fill=(255, 255, 255, 235), font=f_p, stroke_width=2, stroke_fill=(30, 30, 40))

    # 5. routes and positions
    for k, tr in tracks.items():
        col = TRACK_COLOURS.get(k, (255, 255, 255))
        pts = [xy(p[1], p[2]) for p in tr if lat0 - 1 <= p[1] <= lat1 + 1 and lon0 - 1 <= p[2] <= lon1 + 1]
        if len(pts) > 1:
            d.line(pts, fill=(0, 0, 0, 150), width=6)
            d.line(pts, fill=col + (255,), width=3)
        pos = position_at(tr, t)
        if pos:
            x, y = xy(*pos)
            d.ellipse([x - 9, y - 9, x + 9, y + 9], fill=col + (255,), outline=(255, 255, 255, 255), width=3)
            d.text((x + 12, y - 11), k, fill=(255, 255, 255, 255), font=f_t, stroke_width=3, stroke_fill=(0, 0, 0))

    # 6. title bar
    d.rectangle([0, 0, w, top], fill=(24, 26, 34, 255))
    d.text((10, 6), label, fill=(255, 255, 255), font=f_t)
    d.rectangle([0, 0, w - 1, h - 1], outline=(24, 26, 34, 255), width=2)
    return img


def legend(w, tracks):
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, 66), (24, 26, 34))
    d = ImageDraw.Draw(img)
    f = _font(17)
    x0, x1, top = 10, min(w - 20, 1000), 6
    d.rounded_rectangle([x0 - 4, top - 2, x1 + 4, top + 26], radius=13, fill=(60, 62, 72))
    seg = (x1 - x0 - 80) / (len(LEGEND_TICKS) - 1)
    xs0 = x0 + 40
    d.text((x0 + 6, top + 3), "kt", fill=(255, 255, 255), font=_font(16))
    for x in range(int(xs0), x1):
        i = min(int((x - xs0) / seg), len(LEGEND_TICKS) - 2)
        fr = min(1.0, (x - xs0 - i * seg) / seg)
        kn = LEGEND_TICKS[i] + (LEGEND_TICKS[i + 1] - LEGEND_TICKS[i]) * fr
        d.line([(x, top), (x, top + 24)], fill=colour(kn))
    for i, kn in enumerate(LEGEND_TICKS):
        x = xs0 + i * seg
        d.text((x - 4 * len(str(kn)), top + 3), str(kn), fill=(255, 255, 255), font=_font(16), stroke_width=2, stroke_fill=(40, 40, 50))
    x = 10
    d.text((x, 38), "White streaks flow with the wind.  Routed course and position at that hour:", fill=(255, 255, 255), font=f)
    x += 640
    for k in tracks:
        d.ellipse([x, 40, x + 16, 56], fill=TRACK_COLOURS.get(k, (255, 255, 255)), outline=(255, 255, 255), width=2)
        d.text((x + 22, 38), k, fill=(255, 255, 255), font=f)
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
    pw = 880  # panel height follows the map's shape, so a wide ocean area is not stretched
    kx = math.cos(math.radians((bbox[0] + bbox[2]) / 2))
    ph = max(360, min(700, int(pw * (bbox[2] - bbox[0]) / ((bbox[3] - bbox[1]) * kx)) + 34))
    for day in range(days):
        times = [depart + timedelta(days=day, hours=h) for h in range(0, 24, step_h)]
        cols, rows = 2, math.ceil(len(times) / 2)
        sheet = Image.new("RGB", (cols * pw + 30, rows * ph + 150), (24, 26, 34))
        dd = ImageDraw.Draw(sheet)
        dd.text((15, 10), f"Day {day + 1}, {times[0]:%a %d %b}: our model's forecast wind (skill-weighted blend of "
                          f"{', '.join(used)}; not Windy). Drawn {iso(utcnow())[:16]}Z", fill=(255, 255, 255),
                font=_font(26))
        sheet.paste(legend(cols * pw, tracks), (15, 48))
        for i, t in enumerate(times):
            p = panel(blend, bbox, t, tracks, rings, pw, ph,
                      f"{t:%a %d %b %H:%M} UTC  (+{int((t - depart).total_seconds() // 3600)} h)")
            sheet.paste(p, (15 + (i % 2) * pw, 120 + (i // 2) * ph))
        path = out / f"climate_{name}_day{day + 1}.jpg"
        sheet.save(path, "JPEG", quality=85, optimize=True)
        files.append(str(path))
    return {"status": "ok", "files": files, "bbox": bbox, "models": used, "days": days, "step_h": step_h,
            "weights": ({f"{n} d ahead": {m: round(100 * x) for m, x in w.items()} for n, w in weights.by_day.items()}
                        if weights is not None else None)}


def _years_text(yrs):
    """[1997, 2006, 2007, ..., 2025] -> '1997, 2006-2025'."""
    out, run = [], []
    for y in yrs:
        if run and y != run[-1] + 1:
            out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
            run = []
        run.append(y)
    if run:
        out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return ", ".join(out) or "?"


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
    yrs = sorted(int(y) for y in (cl.get("years") or st.get("years") or []))
    hd = st.get("handover_day") or (cl.get("forecast") or {}).get("handover_day")
    lines += [f"- **Forecast into history:** the forecast's weight fades steadily from 2 days after issue to zero "
              f"at 15 days; for this run from day {hd[0] if hd else '?'} to day {hd[1] if hd else '?'} of the "
              f"passage. Each of {len(yrs) or '?'} past years' weather (ERA5, {_years_text(yrs)}) then takes over: "
              f"{len(yrs) or '?'} possible endings.",
              ]
    en = cl.get("enso") or {}
    ranked = sorted((en.get("years") or {}).items(), key=lambda kv: -kv[1].get("weight", 0))
    top = ", ".join(f"{y} ({v.get('weight')})" for y, v in ranked[:3] if v.get("weight", 0) >= 0.5)
    if en.get("now"):
        lines.append(f"- **El Nino weighting:** ONI {en['now'].get('oni')} ({en['now'].get('season')}); past years "
                     "weighted by how close their Oct-Dec ONI was" + (f" ({top} most)." if top else "."))
    s = st.get("settings") or cl.get("settings") or {}
    lines += [f"- **Yacht:** polar {s.get('table')} x {s.get('polar_factor')}; 3 min per tack or gybe; motor at "
              f"{s.get('motor_speed_kn')} kn below {s.get('motor_wind_below_kn')} kn of wind (option D: below 7 kn), "
              f"at most {s.get('motor_max_h')} h of motoring within the fuel consumption limitation.",
              "- **Sea state:** ERA5 ocean waves for past years and wind-derived seas in the forecast; following seas "
              "add up to +3 % (surfing), head and beam seas slow her.",
              "- **Current:** mean surface current for these dates over 2022-2025.",
              "- **Not modelled:** squalls, gusts, crew fatigue, breakages, sail changes beyond the polar.",
              "", "**Wind maps: drawn by this model from its own skill-weighted forecast blend, not taken from Windy.** "
              f"Every {info.get('step_h', 3)} h for the first {info.get('days', 4)} days, area {info.get('bbox')}, "
              "colours by wind strength, "
              "white streaks flowing with the wind; the coloured dots are each option's most typical routed course at that hour."]
    return "\n".join(lines) + "\n"
