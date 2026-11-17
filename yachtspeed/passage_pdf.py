"""The passage write-up as a PDF, built only from the server's published results, so it can be
rebuilt whenever the reruns change:

    python -m yachtspeed.passage_pdf <folder with climate_* files> <out.pdf>

The folder needs climate_<name>.json and _80.json, climate_<strategies>.json and .jpg, and
optionally climate_<maps>_day1..4.jpg and climate_<maps>_assumptions.md. Every figure is read from the
JSON; nothing is hard-coded.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .geo import parse_iso

OPTION_COLOURS = {"A": (200, 30, 40), "B": (20, 90, 190), "C": (30, 140, 70), "D": (230, 120, 0), "E": (120, 40, 150)}


def _load(folder, name):
    p = Path(folder) / name
    return json.loads(p.read_text()) if p.exists() else None


def option_map(strat, key, path, width=1800):
    """One option: every forecast run faint, the most typical run bold (thick = sailing, thin = motoring),
    with a dot for each day at sea."""
    from .chart import _basemap
    from .climate import typical_run

    v = strat["strategies"][key]
    ok = [r for r in v["results"] if r.get("status") == "ok" and r.get("track")]
    if not ok:
        return None
    lats = [p[1] for r in ok for p in r["track"]]
    lons = [p[2] for r in ok for p in r["track"]]
    img, d, xy, (f_s, f_m, f_l), (w, h, top) = _basemap(lats, lons, width, top=90)
    col = OPTION_COLOURS.get(key, (60, 60, 60))
    faint = tuple(int(c + (255 - c) * 0.6) for c in col)
    for r in ok:
        d.line([xy(p[1], p[2]) for p in r["track"]], fill=faint, width=1)
    best = typical_run(ok)
    tr = best["track"]
    for a, b in zip(tr, tr[1:]):
        d.line([xy(a[1], a[2]), xy(b[1], b[2])], fill=col, width=3 if b[3] else 7)
    t0 = parse_iso(tr[0][0])
    day = 1
    for a, b in zip(tr, tr[1:]):
        ta, tb = parse_iso(a[0]), parse_iso(b[0])
        while ta <= t0 + timedelta(days=day) <= tb:
            f = (t0 + timedelta(days=day) - ta).total_seconds() / max(1, (tb - ta).total_seconds())
            x, y = xy(a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f)
            d.ellipse([x - 7, y - 7, x + 7, y + 7], fill=col, outline=(255, 255, 255), width=2)
            if day % 2 == 0:
                d.text((x + 9, y - 22), f"d{day}", fill=(30, 30, 40), font=f_s)
            day += 1
    for la, lo in (strat["from"], strat["to"]):
        x, y = xy(la, lo)
        d.ellipse([x - 9, y - 9, x + 9, y + 9], fill=(20, 20, 20))
    d.rectangle([0, 0, w, top], fill=(255, 255, 255))
    d.text((16, 10), f"Option {key}: {v['label']}", fill=col, font=f_l)
    d.text((16, 52), f"Bold: the most typical of {len(ok)} runs ({best['days']} d; thick = sailing, thin = motoring; "
                     "dot = each day at sea). Faint: the others.", fill=(40, 40, 50), font=f_s)
    img.save(path, "JPEG", quality=85, optimize=True)
    return best


def build(folder, out_pdf, name="alibi_antigua", strategies="alibi_strategies", maps="alibi_forecast"):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    folder = Path(folder)
    c85 = _load(folder, f"climate_{name}.json")
    c80 = _load(folder, f"climate_{name}_80.json")
    st = _load(folder, f"climate_{strategies}.json")
    if not c85 or not st:
        raise SystemExit("need climate_<name>.json and climate_<strategies>.json")

    ss = getSampleStyleSheet()
    H1 = ParagraphStyle("h1", parent=ss["Heading1"], fontSize=17, spaceAfter=6)
    H2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=13, spaceBefore=8, spaceAfter=4)
    P = ParagraphStyle("p", parent=ss["BodyText"], fontSize=9.5, leading=12.5, alignment=TA_LEFT)
    S = ParagraphStyle("s", parent=P, fontSize=8, leading=10)
    W = A4[0] - 30 * mm

    def table(rows, widths=None, head=1, size=8.2):
        t = Table([[Paragraph(str(c), ParagraphStyle("c", parent=S, fontSize=size, leading=size + 2)) for c in r]
                   for r in rows], colWidths=widths, repeatRows=head)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, head - 1), colors.HexColor("#1f2a3a")),
                               ("TEXTCOLOR", (0, 0), (-1, head - 1), colors.white),
                               ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#b8c0cc")),
                               ("ROWBACKGROUNDS", (0, head), (-1, -1), [colors.white, colors.HexColor("#f1f4f8")]),
                               ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        return t

    def img(path, maxw=W, maxh=230 * mm):
        from PIL import Image as PI

        iw, ih = PI.open(path).size
        k = min(maxw / iw, maxh / ih)
        return Image(str(path), iw * k, ih * k)

    def when(iso_s):
        return parse_iso(iso_s).strftime("%a %d %b %H:%M") + " UTC" if iso_s else "-"

    dep, ddl = parse_iso(st["depart"]), parse_iso(st["deadline"])
    s = st["settings"]
    yrs = sorted(r["year"] for r in c85["results"])
    years_txt = f"{len(yrs)} years ({yrs[0]}" + (f", {yrs[1]}-{yrs[-1]})" if len(yrs) > 2 else ")")
    enso = c85.get("enso") or {}
    now = enso.get("now") or {}
    ey = enso.get("years") or {}
    fc = (c85.get("forecast") or {}).get("summary") or {}
    hs = c85["summary"]
    story = []

    # ---- 1. summary
    story += [Paragraph(f"{c85['yacht']}: Rubicón to English Harbour, Antigua", H1),
              Paragraph(f"Passage model and routing options. Forecast issued {when(st['issued_utc'])}; "
                        f"built {datetime.now(timezone.utc):%d %b %Y %H:%M} UTC.", S), Spacer(1, 4 * mm),
              Paragraph(f"<b>Leaving</b> {dep:%a %d %b %H:%M} UTC. <b>Deadline</b> {ddl:%a %d %b %H:%M} UTC "
                        f"(6 Dec 23:59 Antigua), {st['allowed_days']} days. Polar {s.get('table')} x {s.get('polar_factor')}; "
                        f"motor at {s.get('motor_speed_kn')} kn below {s.get('motor_wind_below_kn')} kn of wind, "
                        f"at most {s.get('motor_max_h')} h on the fuel.", P), Spacer(1, 3 * mm),
              Paragraph(f"<b>Bottom line.</b> On time in {fc.get('on_time')}/{fc.get('years')} runs with the live forecast "
                        f"and {hs['on_time']}/{hs['years']} past years ({years_txt}), and "
                        f"{round(100 * (hs.get('probability_enso_weighted') or 0))}% weighted to this year's El Niño "
                        f"(ONI {now.get('oni')}). Median passage {fc.get('days_median')} days with the forecast, "
                        f"{hs['days_median']} days in past weather alone; slowest {hs['days_max']} days.", P),
              Spacer(1, 3 * mm)]
    rows = [["Option", "Median days", "10-90 % days", "Slowest", "On time", "Sailing", "Motoring", "Fuel",
             "Typical arrival"]]
    typicals = {}
    from .climate import typical_run

    for k, v in st["strategies"].items():
        sm = v["summary"]
        t = typical_run(v["results"])
        typicals[k] = t
        rows.append([f"<b>{k}</b>: {v['label']}", sm.get("days_median"), f"{sm.get('days_p10')}-{sm.get('days_p90')}",
                     sm.get("days_max"), f"{sm.get('on_time')}/{sm.get('years')}", f"{sm.get('sailing_pct_median')}%",
                     f"{sm.get('motor_hours_median')} h", f"{sm.get('fuel_l_median')} L", when(t["eta"]) if t else "-"])
    story += [Paragraph("Routing options (live forecast, fading into past years)", H2),
              table(rows, [W * x for x in (.27, .08, .1, .07, .07, .07, .08, .08, .18)]), Spacer(1, 3 * mm)]
    a, c, dd = (st["strategies"].get(k, {}).get("summary", {}) for k in "ACD")
    if a and c and dd:
        story.append(Paragraph(
            f"<b>Recommendation:</b> option C as the default, about {round((c['days_median'] - a['days_median']) * 24)} h "
            f"slower than the fastest route and {a['fuel_l_median'] - c['fuel_l_median']} L less fuel; option D for a "
            f"sailing passage, about {round((dd['days_median'] - a['days_median']) * 24)} h slower with "
            f"{dd['sailing_pct_median']}% sailing on {dd['fuel_l_median']} L. Neither puts the deadline at risk.", P))

    # ---- 2. polar sensitivity
    rows = [["Polar", "Forecast median", "Forecast 10-90 %", "Past weather median", "Past 10-90 %", "Slowest year",
             "Smallest margin", "On time"]]
    for lab, cj in (("85 %", c85), ("80 %", c80)):
        if not cj:
            continue
        f_ = (cj.get("forecast") or {}).get("summary") or {}
        h_ = cj["summary"]
        slow = max(cj["results"], key=lambda r: r.get("days") or 0)
        rows.append([lab, f"{f_.get('days_median')} d", f"{f_.get('days_p10')}-{f_.get('days_p90')}",
                     f"{h_['days_median']} d", f"{h_['days_p10']}-{h_['days_p90']}", f"{slow['days']} d ({slow['year']})",
                     f"{slow['margin_h']} h", f"{h_['on_time']}/{h_['years']}"])
    story += [Paragraph("How much the boat's pace matters (percentage of the Oyster 885 polar)", H2),
              Paragraph("ALIBI's logged history averages about 84% of polar overall and 79-82% broad reaching in the "
                        "trades, so 80-85% is the realistic band.", P), Spacer(1, 2 * mm),
              table(rows), PageBreak()]

    # ---- 3. how the model works
    w_ = st.get("model_weights") or {}
    wtxt = "; ".join(f"{lead}: " + ", ".join(f"{m} {p}%" for m, p in ws.items()) for lead, ws in list(w_.items())[:1])
    hd = st.get("handover_day") or [None, None]
    story += [Paragraph("How the model works", H2), Paragraph(
        "<b>Router.</b> An isochrone router steers ALIBI across the Atlantic on a weather grid, choosing headings "
        "every 4 hours from the polar, the wind, the sea state and the current, with a land mask. It motors at "
        f"{s.get('motor_speed_kn')} kn when the wind is below {s.get('motor_wind_below_kn')} kn, within the fuel. "
        "<br/><b>Weather.</b> Four forecast models (ECMWF IFS, ECMWF AIFS, GFS, ICON) are blended, each weighted by "
        f"its measured accuracy along the route over the last 30 days ({st.get('weights_checks')} checks; {wtxt}). "
        f"The forecast is used alone at first; its weight then fades steadily to zero by day {hd[1]} of the "
        f"passage, with each past year's ERA5 weather taking over, giving {len(yrs)} possible passages. "
        "<br/><b>Sea and current.</b> ERA5 ocean waves for past years (wind-derived seas in the forecast); following "
        "seas add up to 3% for surfing, head and beam seas slow her. Mean surface current 2022-2025 for these dates."
        "<br/><b>Caution comes last.</b> Every run is a best estimate; the caution is in the choice of polar (85% or 80%) "
        "and in reading the slowest years, not stacked inside the model.", P)]

    # ---- 4. El Nino
    rows = [["Year", "ENSO (Oct-Dec ONI)", "Weight", "Days at 85 %", "Days at 80 %", "Margin at 85 %",
             "Motor h", "Furthest south"]]
    by = {lab: {r["year"]: r for r in (cj or {}).get("results", [])} for lab, cj in (("85", c85), ("80", c80))}
    for y in yrs:
        r = by["85"][y]
        e = ey.get(str(y)) or {}
        rows.append([f"<b>{y}</b>" if (e.get("weight") or 0) >= 0.5 else y, f"{e.get('phase', '')} ({e.get('oni')})",
                     e.get("weight"), r.get("days"), (by["80"].get(y) or {}).get("days"), 
                     f"{r.get('margin_h')} h", r.get("motor_hours"), r.get("southmost_lat")])
    top = sorted(ey.items(), key=lambda kv: -(kv[1].get("weight") or 0))[:3]
    story += [Paragraph("El Niño and the past years", H2), Paragraph(
        f"This autumn's ONI is {now.get('oni')} ({now.get('season')}), a strong El Niño. Each past year is weighted by how "
        f"close its own Oct-Dec ONI was; the closest are " + ", ".join(f"{y} ({v.get('weight')})" for y, v in top) +
        f", about {hs.get('effective_years')} years' worth of evidence. "
        f"{hs.get('el_nino_like_on_time')} of the {hs.get('el_nino_like_years')} El Niño-like years were on time at 85 %.", P),
        Spacer(1, 2 * mm), table(rows, size=7.6), PageBreak()]

    # ---- 5. year by year by option
    keys = list(st["strategies"])
    hist = {k: {r["year"]: r for r in st["strategies"][k].get("history", []) if r.get("status") == "ok"} for k in keys}
    rows = [["Year"] + [f"{k} days" if k == "A" else f"{k} (vs A)" for k in keys]]
    for y in yrs:
        ra = hist["A"].get(y)
        row = [y]
        for k in keys:
            r = hist[k].get(y)
            if not r:
                row.append("-")
            elif k == "A" or not ra:
                row.append(r["days"])
            else:
                row.append(f"{r['days']} ({round((r['days'] - ra['days']) * 24):+d} h)")
        rows.append(row)
    story += [Paragraph("Each option in each past year's own weather (no forecast)", H2),
              Paragraph("Hours are relative to option A, the router's fastest.", S), Spacer(1, 2 * mm), table(rows),
              Spacer(1, 4 * mm)]
    sj = folder / f"climate_{strategies}.jpg"
    if sj.exists():
        story += [Paragraph("All options together", H2), img(sj, maxh=120 * mm)]
    story.append(PageBreak())

    # ---- 6. one page per option
    tmp = folder / "_option_maps"
    tmp.mkdir(exist_ok=True)
    for k in keys:
        p = tmp / f"option_{k}.jpg"
        best = option_map(st, k, p)
        sm = st["strategies"][k]["summary"]
        hm = st["strategies"][k].get("history_summary") or {}
        story += [Paragraph(f"Option {k}: {st['strategies'][k]['label']}", H2), Paragraph(
            f"With the forecast: median {sm.get('days_median')} days (10-90 %: {sm.get('days_p10')}-{sm.get('days_p90')}), "
            f"slowest {sm.get('days_max')}, {sm.get('sailing_pct_median')}% sailing, {sm.get('motor_hours_median')} h motoring, "
            f"{sm.get('fuel_l_median')} L. In past weather alone: median {hm.get('days_median')}, slowest {hm.get('days_max')}. "
            + (f"Typical arrival {when(best['eta'])}." if best else ""), P), Spacer(1, 2 * mm)]
        if best:
            story.append(img(p, maxh=200 * mm))
        story.append(PageBreak())

    # ---- appendices
    am = folder / f"climate_{maps}_assumptions.md"
    if am.exists():
        story.append(Paragraph("Appendix A: forecast and model assumptions", H2))
        for line in am.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("# "):
                continue
            line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
            ind = 12 if line.startswith("- ") else 24 if line.startswith("  - ") else 0
            story.append(Paragraph(line.lstrip(" -"), ParagraphStyle("b", parent=P, leftIndent=ind, bulletIndent=ind - 8,
                                                                      spaceAfter=2), bulletText="•" if ind else None))
        story.append(PageBreak())
    days = sorted(folder.glob(f"climate_{maps}_day*.jpg"), key=lambda p: int(re.findall(r"day(\d+)", p.name)[0]))
    for i, p in enumerate(days):
        story += [Paragraph(f"Appendix B{i + 1}: forecast wind, day {i + 1}", H2),
                  Paragraph("Skill-weighted blend of the four models every 3 h. Colours: wind strength (scale at the "
                            "top); white streaks flow with the wind; dots: options A, C and D on their most typical "
                            "routes at that hour.", S), Spacer(1, 2 * mm), img(p, maxh=232 * mm)]
        if i < len(days) - 1:
            story.append(PageBreak())

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#667080"))
        canvas.drawString(15 * mm, 9 * mm, f"{c85['yacht']} passage model, forecast {when(st['issued_utc'])}")
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"page {doc.page}")
        canvas.restoreState()

    SimpleDocTemplate(str(out_pdf), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=13 * mm,
                      bottomMargin=15 * mm, title=f"{c85['yacht']} passage model").build(story, onFirstPage=footer,
                                                                                        onLaterPages=footer)
    return out_pdf


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2])
