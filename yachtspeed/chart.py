"""Route chart as a JPEG, drawn on the server with Pillow (no cloud work, no extra packages)."""

from __future__ import annotations

import math

from .geo import parse_iso

SEA, LAND, COAST, GRID = (214, 230, 242), (236, 228, 205), (150, 140, 110), (190, 205, 220)
TRACK, TEXT, DEST, START = (200, 30, 40), (30, 30, 40), (20, 110, 40), (20, 60, 160)


def _font(size):
    from PIL import ImageFont

    for name in ("DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _basemap(lats, lons, width=1600, top=110):
    """Sea, land and a lat/lon grid around the points. Returns (img, draw, xy, fonts, (w, h, top))."""
    from PIL import Image, ImageDraw

    from .land import EATL_10M, WORLD_10M, WORLD_50M, _intersects, _load

    pad = max(0.4, 0.12 * max(max(lats) - min(lats), max(lons) - min(lons)))
    lat0, lat1, lon0, lon1 = min(lats) - pad, max(lats) + pad, min(lons) - pad, max(lons) + pad
    kx = math.cos(math.radians((lat0 + lat1) / 2))
    height = int(width * (lat1 - lat0) / ((lon1 - lon0) * kx)) + top
    height = max(600, min(height, 2400))
    sx = width / (lon1 - lon0)
    sy = (height - top) / (lat1 - lat0)

    def xy(la, lo):
        return (lo - lon0) * sx, top + (lat1 - la) * sy

    img = Image.new("RGB", (width, height), SEA)
    d = ImageDraw.Draw(img)
    bbox = (lat0, lon0, lat1, lon1)
    world = str(WORLD_10M) if WORLD_10M.exists() else str(WORLD_50M)
    rings = [it[0] for it in _load(world) if _intersects(it, bbox)]
    if world == str(WORLD_50M):
        rings += [it[0] for it in _load(str(EATL_10M)) if _intersects(it, bbox)]
    for ring in rings:
        pts = [xy(p[1], p[0]) for p in ring]
        if len(pts) > 2:
            d.polygon(pts, fill=LAND, outline=COAST)
    f_s, f_m, f_l = _font(18), _font(22), _font(30)
    span = max(lat1 - lat0, lon1 - lon0)
    step = 1.0 if span < 8 else 2.0 if span < 20 else 5.0
    la = math.ceil(lat0 / step) * step
    while la < lat1:
        y = xy(la, lon0)[1]
        d.line([(0, y), (width, y)], fill=GRID)
        d.text((4, y + 2), f"{abs(la):g}°{'N' if la >= 0 else 'S'}", fill=TEXT, font=f_s)
        la += step
    lo = math.ceil(lon0 / step) * step
    while lo < lon1:
        x = xy(lat0, lo)[0]
        d.line([(x, top), (x, height)], fill=GRID)
        d.text((x + 3, height - 24), f"{abs(lo):g}°{'E' if lo >= 0 else 'W'}", fill=TEXT, font=f_s)
        lo += step
    return img, d, xy, (f_s, f_m, f_l), (width, height, top)


FLEET_COLOURS = [(200, 30, 40), (20, 90, 190), (210, 120, 0), (120, 40, 150), (20, 130, 90)]


def tracks_jpeg(path, yachts: list, hours=72, width=1600, quality=88):
    """Actual tracks (stored fixes) of every yacht with a progress section, one colour each."""
    from .speedtable import parse_destination

    ys = [y for y in yachts if (y.get("progress") or {}).get("track")]
    if not ys:
        return None
    lats, lons = [], []
    for y in ys:
        lats += [p[1] for p in y["progress"]["track"]]
        lons += [p[2] for p in y["progress"]["track"]]
        try:
            dest = parse_destination(y["destination"])[1] if y.get("destination") else None
        except KeyError:
            dest = None
        y["_dest"] = dest
        if dest:
            lats.append(dest[0])
            lons.append(dest[1])
    img, d, xy, (f_s, f_m, f_l), (width, height, top) = _basemap(lats, lons, width)
    for i, y in enumerate(ys):
        col = FLEET_COLOURS[i % len(FLEET_COLOURS)]
        pts = [xy(p[1], p[2]) for p in y["progress"]["track"]]
        if len(pts) > 1:
            d.line(pts, fill=col, width=4, joint="curve")
        for x, yy in pts:
            d.ellipse([x - 3, yy - 3, x + 3, yy + 3], fill=col)
        last_day = None
        for p, (x, yy) in zip(y["progress"]["track"], pts):  # a label at each new UTC day
            day = p[0][:10]
            if day != last_day and last_day is not None:
                d.text((x + 8, yy - 20), f"{parse_iso(p[0]):%a %H:%M}", fill=col, font=f_s)
            last_day = day
        x, yy = pts[-1]
        d.rectangle([x - 8, yy - 8, x + 8, yy + 8], fill=col)
        last = y["progress"]["track"][-1]
        d.text((x + 12, yy + 4), f"{y['yacht']} {parse_iso(last[0]):%a %H:%M}", fill=col, font=f_m)
        if y.get("_dest"):
            dx, dy = xy(*y["_dest"])
            d.ellipse([dx - 9, dy - 9, dx + 9, dy + 9], outline=col, width=3)
            d.text((dx + 12, dy - 10), y["destination"], fill=col, font=f_s)
        y.pop("_dest", None)
    d.rectangle([0, 0, width, top], fill=(255, 255, 255))
    d.text((16, 12), f"Fleet tracks, last {hours} h (stored fixes: AIS, ARC tracker, screenshots)", fill=TEXT, font=f_l)
    x = 16
    for i, y in enumerate(ys):
        col = FLEET_COLOURS[i % len(FLEET_COLOURS)]
        g = (y["progress"].get("windows") or {}).get("24h") or {}
        lab = f"■ {y['yacht']}" + (f"  24 h: {g['sailed_nm']} nm, {g['avg_kn']} kn" if g.get("sailed_nm") else "")
        d.text((x, 60), lab, fill=col, font=f_m)
        x += int(d.textlength(lab, font=f_m) if hasattr(d, "textlength") else 12 * len(lab)) + 40
    img.save(path, "JPEG", quality=quality, optimize=True)
    return path


def route_jpeg(path, yacht: dict, width=1600, quality=88):
    """Draw yacht['routed'][0]['track'] with the fix, destination, ETA and wind labels."""
    from .speedtable import parse_destination

    rt = next((r for r in yacht.get("routed") or [] if r.get("track")), None)
    if not rt:
        return None
    trk = rt["track"]
    _, dest = parse_destination(yacht["destination"])
    lats = [p["lat"] for p in trk] + [dest[0]]
    lons = [p["lon"] for p in trk] + [dest[1]]
    img, d, xy, (f_s, f_m, f_l), (width, height, top) = _basemap(lats, lons, width)
    line = [xy(p["lat"], p["lon"]) for p in trk]
    d.line(line, fill=TRACK, width=4, joint="curve")
    t0 = parse_iso(trk[0]["time_utc"])
    last_lab = None
    for p, (x, y) in zip(trk, line):
        t = parse_iso(p["time_utc"])
        d.ellipse([x - 3, y - 3, x + 3, y + 3], fill=TRACK)
        if last_lab is None or (t - last_lab).total_seconds() >= 6 * 3600 - 60:
            last_lab = t
            d.ellipse([x - 6, y - 6, x + 6, y + 6], outline=TRACK, width=2)
            wind = ""
            if p.get("tws_kn") is not None:
                wind = f"  {p['tws_kn']:.0f} kn"
                if p.get("wind_from_deg") is not None:
                    wind += f" from {p['wind_from_deg']:03.0f}°"
                    a = math.radians(p["wind_from_deg"])  # arrow pointing downwind
                    ux, uy = -math.sin(a), math.cos(a)
                    cx, cy = x - 30, y - 30
                    x0, y0, x1, y1 = cx - ux * 18, cy - uy * 18, cx + ux * 18, cy + uy * 18
                    d.line([(x0, y0), (x1, y1)], fill=START, width=2)
                    hx, hy = -uy, ux
                    d.polygon([(x1, y1), (x1 - ux * 9 + hx * 5, y1 - uy * 9 + hy * 5),
                               (x1 - ux * 9 - hx * 5, y1 - uy * 9 - hy * 5)], fill=START)
            lab = f"{t:%a %H:%M}{wind}"
            tw = d.textlength(lab, font=f_s) if hasattr(d, "textlength") else 9 * len(lab)
            d.text((x + 12 if x + 12 + tw < width - 8 else x - 12 - tw, y + 4), lab, fill=TEXT, font=f_s)
    sx0, sy0 = line[0]
    d.rectangle([sx0 - 8, sy0 - 8, sx0 + 8, sy0 + 8], fill=START)
    dx, dy = xy(*dest)
    d.ellipse([dx - 10, dy - 10, dx + 10, dy + 10], fill=DEST)
    d.text((dx + 14, dy - 12), yacht["destination"], fill=DEST, font=f_m)
    e = yacht.get("eta") or {}
    d.rectangle([0, 0, width, top], fill=(255, 255, 255))
    title = f"{yacht['yacht']} → {yacht['destination']}"
    if e:
        title += f"   ETA {parse_iso(e['median_utc']):%a %d %b %H:%M} UTC (median)"
    d.text((16, 12), title, fill=TEXT, font=f_l)
    sub = f"from fix {t0:%a %d %b %H:%M} UTC · {rt.get('sailed_nm')} nm · {rt.get('maneuvers')} gybes/tacks"
    if e:
        sub += (f" · range {parse_iso(e['earliest_utc']):%a %H:%M}–{parse_iso(e['latest_utc']):%a %H:%M}"
                f" · {'equal weights' if not e.get('weights') or rt.get('weights_equal', True) else 'skill weights'}")
    d.text((16, 56), sub, fill=TEXT, font=f_m)
    d.text((16, 84), "Red: routed course through the combined forecast; labels every 6 h with wind; blue arrows show wind direction.",
           fill=(90, 90, 100), font=f_s)
    img.save(path, "JPEG", quality=quality, optimize=True)
    return path
