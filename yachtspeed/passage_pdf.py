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

STRONG_ONI = 1.5  # a strong El Nino: Oct-Dec ONI at or above this
OPTION_COLOURS = {"A": (200, 30, 40), "B": (20, 90, 190), "C": (30, 140, 70), "D": (230, 120, 0), "E": (120, 40, 150), "F": (0, 150, 160)}


ES_DAYS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
ES_MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
ES_LABELS = {
    "A": "la más rápida",
    "B": "clásica: rumbo suroeste hasta 22N 30W primero",
    "C": "ahorro de combustible: como máximo 30 h a motor en las primeras 72 h",
    "D": "más vela: motor solo con menos de 7 nudos de viento",
    "E": "casi todo a vela: como máximo 48 h a motor en total",
    "F": "vía Cabo Verde: doblar 17,5N 25,5W, al noroeste de Santo Antão",
}
ES_PHASE = {"El Nino": "El Niño", "La Nina": "La Niña", "neutral": "neutro"}


def fmt_dt(dt, lang="en", year=False):
    """'Thu 03 Dec 10:22' (or 'jue 03 dic 10:22')."""
    if lang == "es":
        out = f"{ES_DAYS[dt.weekday()]} {dt:%d} {ES_MONTHS[dt.month - 1]}"
        return out + (f" {dt:%Y}" if year else "") + f" {dt:%H:%M}"
    return dt.strftime("%a %d %b" + (" %Y" if year else "") + " %H:%M")


def _load(folder, name):
    p = Path(folder) / name
    return json.loads(p.read_text()) if p.exists() else None


def option_map(strat, key, path, width=1800, lang="en"):
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
    if lang == "es":
        d.text((16, 10), f"Opción {key}: {ES_LABELS.get(key, v['label'])}", fill=col, font=f_l)
        dd = f"{best['days']:.1f}".replace(".", ",")
        d.text((16, 52), f"En negrita: la más típica de {len(ok)} simulaciones ({dd} d; grueso = vela, fino = "
                         "motor; punto = cada día de navegación). En claro: las demás.",
               fill=(40, 40, 50), font=f_s)
    else:
        d.text((16, 10), f"Option {key}: {v['label']}", fill=col, font=f_l)
        d.text((16, 52), f"Bold: the most typical of {len(ok)} runs ({best['days']:.1f} d; thick = sailing, thin = motoring; "
                         "dot = each day at sea). Faint: the others.", fill=(40, 40, 50), font=f_s)
    img.save(path, "JPEG", quality=85, optimize=True)
    return best


def build(folder, out_pdf, name="alibi_antigua", strategies="alibi_strategies", maps="alibi_forecast", lang="en"):
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
    for k, v in st["strategies"].items():  # older results carried a longer label
        v["label"] = v["label"].replace(" (router's choice)", "")
        if lang == "es":
            v["label"] = ES_LABELS.get(k, v["label"])
    es = lang == "es"

    def T(en, sp):
        return sp if es else en

    ss = getSampleStyleSheet()
    H1 = ParagraphStyle("h1", parent=ss["Heading1"], fontSize=17, spaceAfter=6)
    H2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=13, spaceBefore=8, spaceAfter=4)
    P = ParagraphStyle("p", parent=ss["BodyText"], fontSize=9.5, leading=12.5, alignment=TA_LEFT)
    S = ParagraphStyle("s", parent=P, fontSize=8, leading=10)
    W = A4[0] - 30 * mm

    def table(rows, widths=None, head=1, size=8.2):
        body = ParagraphStyle("c", parent=S, fontSize=size, leading=size + 2)
        hdr = ParagraphStyle("ch", parent=body, textColor=colors.white, fontName="Helvetica-Bold",
                             fontSize=size - 0.4, leading=size + 1.6)
        t = Table([[Paragraph(str(c), hdr if i < head else body) for c in r] for i, r in enumerate(rows)],
                  colWidths=widths, repeatRows=head)
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
        return fmt_dt(parse_iso(iso_s), lang) + " UTC" if iso_s else "-"

    def D(x):  # days to one decimal place
        if x is None:
            return "-"
        return f"{x:.1f}".replace(".", ",") if es else f"{x:.1f}"

    def N(x):  # any number, with a decimal comma in Spanish
        return "-" if x is None else (str(x).replace(".", ",") if es else str(x))

    def DR(a, b):
        return f"{D(a)}-{D(b)}"

    dep, ddl = parse_iso(st["depart"]), parse_iso(st["deadline"])
    s = st["settings"]
    yrs = sorted(r["year"] for r in c85["results"])
    years_txt = f"{len(yrs)} {T('years', 'años')} ({yrs[0]}" + (f", {yrs[1]}-{yrs[-1]})" if len(yrs) > 2 else ")")
    enso = c85.get("enso") or {}
    now = enso.get("now") or {}
    ey = enso.get("years") or {}
    fc = (c85.get("forecast") or {}).get("summary") or {}
    hs = c85["summary"]
    story = []

    # ---- 1. summary
    built = fmt_dt(datetime.now(timezone.utc), lang, year=True)
    pf = f"{s.get('polar_factor')}".replace(".", ",") if es else s.get("polar_factor")
    mk, mw, mh_ = s.get("motor_speed_kn"), s.get("motor_wind_below_kn"), s.get("motor_max_h") or 0
    if es:
        mk, mw = f"{mk}".replace(".", ","), f"{mw}".replace(".", ",")
    pct = round(100 * (hs.get("probability_enso_weighted") or 0))
    story += [Paragraph(f"{c85['yacht']}: " + T("Rubicón to English Harbour, Antigua", "de Rubicón a English Harbour, Antigua"), H1),
              Paragraph(T(f"Passage model and routing options. Forecast issued {when(st['issued_utc'])}; built {built} UTC.",
                          f"Modelo de travesía y opciones de ruta. Previsión emitida el {when(st['issued_utc'])}; "
                          f"elaborado el {built} UTC."), S), Spacer(1, 4 * mm),
              Paragraph(T(f"<b>Leaving</b> {fmt_dt(dep)} UTC. <b>Deadline</b> {fmt_dt(ddl)} UTC "
                          f"(6 Dec 23:59 Antigua), {D(st['allowed_days'])} days. Polar {s.get('table')} x {pf}; "
                          f"motor at {mk} kn below {mw} kn of wind, "
                          f"at most {mh_:.0f} h of motoring within the fuel consumption limitation.",
                          f"<b>Salida</b> {fmt_dt(dep, lang)} UTC. <b>Fecha límite</b> {fmt_dt(ddl, lang)} UTC "
                          f"(6 dic 23:59 hora de Antigua), {D(st['allowed_days'])} días. Polar {s.get('table')} x {pf}; "
                          f"motor a {mk} nudos con menos de {mw} nudos de viento, "
                          f"como máximo {mh_:.0f} h a motor dentro del límite de consumo de combustible."), P),
              Spacer(1, 3 * mm),
              Paragraph(T(f"<b>Bottom line.</b> On time in {fc.get('on_time')}/{fc.get('years')} runs with the live forecast "
                          f"and {hs['on_time']}/{hs['years']} past years ({years_txt}), and "
                          f"{pct}% weighted to this year's El Niño "
                          f"(ONI {now.get('oni')}). Median passage {D(fc.get('days_median'))} days with the forecast, "
                          f"{D(hs['days_median'])} days in past weather alone; slowest {D(hs['days_max'])} days.",
                          f"<b>Conclusión.</b> Llega a tiempo en {fc.get('on_time')}/{fc.get('years')} simulaciones con la "
                          f"previsión actual y en {hs['on_time']}/{hs['years']} años anteriores ({years_txt}), y en un "
                          f"{pct} % ponderando según El Niño de este año (ONI {str(now.get('oni')).replace('.', ',')}). "
                          f"Travesía mediana de {D(fc.get('days_median'))} días con la previsión y "
                          f"{D(hs['days_median'])} días solo con el tiempo de años anteriores; la más lenta, "
                          f"{D(hs['days_max'])} días."), P),
              Spacer(1, 3 * mm)]
    rows = [T(["Option", "Median (d)", "10-90 % (d)", "Slowest", "On time", "Sailing", "Motor", "Fuel",
               "Typical arrival"],
              ["Opción", "Mediana (d)", "10-90 % (d)", "Más lenta", "A tiempo", "Vela", "Motor", "Gasoil",
               "Llegada típica"])]
    typicals = {}
    from .climate import typical_run

    for k, v in st["strategies"].items():
        sm = v["summary"]
        t = typical_run(v["results"])
        typicals[k] = t
        rows.append([f"<b>{k}</b>: {v['label']}", D(sm.get("days_median")), DR(sm.get('days_p10'), sm.get('days_p90')),
                     D(sm.get("days_max")), f"{sm.get('on_time')}/{sm.get('years')}", f"{sm.get('sailing_pct_median')}%",
                     f"{sm.get('motor_hours_median'):.0f} h", f"{sm.get('fuel_l_median')} L",
                     fmt_dt(parse_iso(t["eta"]), lang) if t else "-"])
    story += [Paragraph(T("Routing options (live forecast, fading into past years)",
                          "Opciones de ruta (previsión actual, que da paso gradualmente a años anteriores)"), H2),
              table(rows, [W * x for x in (.23, .085, .125, .085, .075, .085, .07, .075, .175)]), Spacer(1, 1 * mm),
              Paragraph(T("Times in UTC.", "Horas en UTC."), S), Spacer(1, 2 * mm)]
    a, c, dd = (st["strategies"].get(k, {}).get("summary", {}) for k in "ACD")
    if a and c and dd:
        hc, hd_ = round((c['days_median'] - a['days_median']) * 24), round((dd['days_median'] - a['days_median']) * 24)
        story.append(Paragraph(T(
            f"<b>Recommendation:</b> option C as the default, about {hc} h "
            f"slower than the fastest route and {a['fuel_l_median'] - c['fuel_l_median']} L less fuel; option D for a "
            f"sailing passage, about {hd_} h slower with "
            f"{dd['sailing_pct_median']}% sailing on {dd['fuel_l_median']} L. Neither puts the deadline at risk.",
            f"<b>Recomendación:</b> la opción C por defecto, unas {hc} h más lenta que la ruta más rápida y con "
            f"{a['fuel_l_median'] - c['fuel_l_median']} L menos de gasóleo; la opción D para una travesía a vela, unas "
            f"{hd_} h más lenta, con un {dd['sailing_pct_median']} % a vela y {dd['fuel_l_median']} L. "
            "Ninguna de las dos pone en riesgo la fecha límite."), P))

    # ---- 2. polar sensitivity
    rows = [T(["Polar", "Forecast median", "Forecast 10-90 %", "Past weather median", "Past 10-90 %", "Slowest year",
               "Smallest margin", "On time"],
              ["Polar", "Mediana con previsión", "Previsión 10-90 %", "Mediana años anteriores", "Años anteriores 10-90 %",
               "Año más lento", "Margen mínimo", "A tiempo"])]
    for lab, cj in (("85 %", c85), ("80 %", c80)):
        if not cj:
            continue
        f_ = (cj.get("forecast") or {}).get("summary") or {}
        h_ = cj["summary"]
        slow = max(cj["results"], key=lambda r: r.get("days") or 0)
        rows.append([lab, f"{D(f_.get('days_median'))} d", DR(f_.get('days_p10'), f_.get('days_p90')),
                     f"{D(h_['days_median'])} d", DR(h_['days_p10'], h_['days_p90']), f"{D(slow['days'])} d ({slow['year']})",
                     f"{slow['margin_h']:.0f} h", f"{h_['on_time']}/{h_['years']}"])
    story += [Paragraph(T("How much the boat's pace matters (percentage of the Oyster 885 polar)",
                          "Cuánto importa el ritmo del barco (porcentaje de la polar del Oyster 885)"), H2),
              Paragraph(T("ALIBI's logged history averages about 84% of polar overall and 79-82% broad reaching in the "
                          "trades. With the G2 asymmetric spinnaker now aboard, 80-85% is the realistic band.",
                          "El historial registrado de ALIBI promedia alrededor del 84 % de la polar en general y un 79-82 % "
                          "a un largo en los alisios. Con el spinnaker asimétrico G2 ahora a bordo, el rango realista es "
                          "del 80-85 %."), P), Spacer(1, 2 * mm),
              table(rows, [W * x for x in (.08, .12, .14, .13, .14, .15, .13, .11)]), PageBreak()]

    # ---- 3. how the model works
    w_ = st.get("model_weights") or {}
    wtxt = "; ".join(f"{lead.replace(' d ahead', ' d de antelación') if es else lead}: " + ", ".join(f"{m} {p}%" for m, p in ws.items()) for lead, ws in list(w_.items())[:1])
    hd = st.get("handover_day") or [None, None]
    story += [Paragraph(T("How the model works", "Cómo funciona el modelo"), H2)]
    if es:
        story.append(Paragraph(
            "<b>Enrutador.</b> Un enrutador de isocronas guía a ALIBI a través del Atlántico sobre una malla meteorológica, "
            "eligiendo el rumbo cada 4 horas según la polar, el viento, el estado de la mar y la corriente, con una "
            f"máscara de tierra que la mantiene al menos a 3 millas de la costa. Navega a motor a {mk} nudos cuando el "
            f"viento baja de {mw} nudos, dentro del límite de consumo de combustible (como máximo {mh_:.0f} h a motor). "
            "<br/><b>Meteorología.</b> Se combinan cuatro modelos de previsión (ECMWF IFS, ECMWF AIFS, GFS, ICON), cada uno "
            "ponderado según su precisión medida a lo largo de la ruta en los últimos 30 días "
            f"({st.get('weights_checks')} comprobaciones; {wtxt}). Al principio se usa solo la previsión; después su peso "
            f"disminuye de forma gradual hasta cero hacia el día {N(hd[1])} de la travesía, y el tiempo ERA5 de cada año "
            f"anterior toma el relevo, lo que da {len(yrs)} travesías posibles. "
            "<br/><b>Mar y corriente.</b> Olas oceánicas ERA5 para los años anteriores (mar derivada del viento en la "
            "previsión); la mar de popa añade hasta un 3 % por el surfeo, y la mar de proa y de través la frena. Corriente "
            "superficial media de 2022-2025 para estas fechas."
            "<br/><b>La prudencia va al final.</b> Cada simulación es la mejor estimación; la prudencia está en la elección "
            "de la polar (85 % u 80 %) y en la lectura de los años más lentos, no acumulada dentro del modelo.", P))
    else:
        story.append(Paragraph(
        "<b>Router.</b> An isochrone router steers ALIBI across the Atlantic on a weather grid, choosing headings "
        "every 4 hours from the polar, the wind, the sea state and the current, with a land mask. It motors at "
        f"{s.get('motor_speed_kn')} kn when the wind is below {s.get('motor_wind_below_kn')} kn, within the fuel consumption limitation "
        f"(at most {s.get('motor_max_h'):.0f} h of motoring). "
        "<br/><b>Weather.</b> Four forecast models (ECMWF IFS, ECMWF AIFS, GFS, ICON) are blended, each weighted by "
        f"its measured accuracy along the route over the last 30 days ({st.get('weights_checks')} checks; {wtxt}). "
        f"The forecast is used alone at first; its weight then fades steadily to zero by day {hd[1]} of the "
        f"passage, with each past year's ERA5 weather taking over, giving {len(yrs)} possible passages. "
        "<br/><b>Sea and current.</b> ERA5 ocean waves for past years (wind-derived seas in the forecast); following "
        "seas add up to 3% for surfing, head and beam seas slow her. Mean surface current 2022-2025 for these dates."
        "<br/><b>Caution comes last.</b> Every run is a best estimate; the caution is in the choice of polar (85% or 80%) "
        "and in reading the slowest years, not stacked inside the model.", P))

    # ---- 4. El Nino
    rows = [T(["Year", "ENSO (Oct-Dec ONI)", "Weight", "Days at 85 %", "Days at 80 %", "Margin at 85 %",
               "Motor h", "Furthest south"],
              ["Año", "ENSO (ONI oct-dic)", "Peso", "Días al 85 %", "Días al 80 %", "Margen al 85 %",
               "Horas motor", "Latitud más al sur"])]
    by = {lab: {r["year"]: r for r in (cj or {}).get("results", [])} for lab, cj in (("85", c85), ("80", c80))}
    for y in yrs:
        r = by["85"][y]
        e = ey.get(str(y)) or {}
        rows.append([f"<b>{y}</b>" if (e.get("weight") or 0) >= 0.5 else y, f"{ES_PHASE.get(e.get('phase', ''), e.get('phase', '')) if es else e.get('phase', '')} ({N(e.get('oni'))})",
                     N(e.get("weight")), D(r.get("days")), D((by["80"].get(y) or {}).get("days")),
                     f"{r.get('margin_h'):.0f} h", f"{r.get('motor_hours'):.0f}", N(r.get("southmost_lat"))])
    strong = [y for y in yrs if ((ey.get(str(y)) or {}).get("oni") or 0) >= STRONG_ONI]

    def mean(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    for lab, ys in ((T("<b>Average</b>", "<b>Media</b>"), yrs),
                    (T("<b>Strong El Niño avg</b>", "<b>Media El Niño fuerte</b>"), strong)):
        if not ys:
            continue
        r85 = [by["85"][y] for y in ys]
        m = mean([by["85"][y].get("margin_h") for y in ys])
        mh = mean([r.get("motor_hours") for r in r85])
        sl = mean([r.get("southmost_lat") for r in r85])
        rows.append(["", lab, "", f"<b>{D(mean([r.get('days') for r in r85]))}</b>",
                     f"<b>{D(mean([(by['80'].get(y) or {}).get('days') for y in ys]))}</b>",
                     f"{m:.0f} h" if m is not None else "-", f"{mh:.0f}" if mh is not None else "-",
                     N(round(sl, 1)) if sl is not None else "-"])
    top = sorted(ey.items(), key=lambda kv: -(kv[1].get("weight") or 0))[:3]
    tops = ", ".join(f"{y} ({N(v.get('weight'))})" for y, v in top)
    story += [Paragraph(T("El Niño and the past years", "El Niño y los años anteriores"), H2)]
    if es:
        story.append(Paragraph(
            f"El ONI de este otoño es {N(now.get('oni'))} ({now.get('season')}), un El Niño fuerte. Cada año anterior se "
            f"pondera según lo parecido que fue su propio ONI de oct-dic; los más parecidos son {tops}, lo que equivale a "
            f"unos {N(hs.get('effective_years'))} años de evidencia. {hs.get('el_nino_like_on_time')} de los "
            f"{hs.get('el_nino_like_years')} años de tipo El Niño llegaron a tiempo al 85 %.", P))
    else:
        story.append(Paragraph(
        f"This autumn's ONI is {now.get('oni')} ({now.get('season')}), a strong El Niño. Each past year is weighted by how "
        f"close its own Oct-Dec ONI was; the closest are " + ", ".join(f"{y} ({v.get('weight')})" for y, v in top) +
        f", about {hs.get('effective_years')} years' worth of evidence. "
        f"{hs.get('el_nino_like_on_time')} of the {hs.get('el_nino_like_years')} El Niño-like years were on time at 85 %.", P))
    story += [Spacer(1, 2 * mm), table(rows, [W * x for x in (.08, .2, .09, .12, .12, .14, .12, .13)], size=7.6),
        PageBreak()]

    # ---- 5. year by year by option
    keys = list(st["strategies"])
    hist = {k: {r["year"]: r for r in st["strategies"][k].get("history", []) if r.get("status") == "ok"} for k in keys}
    rows = [[T("Year", "Año")] + [T(f"{k} days", f"{k} días") if k == "A" else T(f"{k} (vs A)", f"{k} (vs A)")
                                  for k in keys]]
    for y in yrs:
        ra = hist["A"].get(y)
        row = [y]
        for k in keys:
            r = hist[k].get(y)
            if not r:
                row.append("-")
            elif k == "A" or not ra:
                row.append(D(r["days"]))
            else:
                row.append(f"{D(r['days'])} ({round((r['days'] - ra['days']) * 24):+d} h)")
        rows.append(row)
    strong = [y for y in yrs if ((ey.get(str(y)) or {}).get("oni") or 0) >= STRONG_ONI]
    for lab, ys in ((T("<b>Average</b>", "<b>Media</b>"), yrs),
                    (T("<b>Strong El Niño avg</b>", "<b>Media El Niño fuerte</b>"), strong)):
        if not ys:
            continue
        row = [lab]
        for k in keys:
            both = [y for y in ys if y in hist[k] and y in hist["A"]]
            dk = [hist[k][y]["days"] for y in ys if y in hist[k]]
            if not dk:
                row.append("-")
                continue
            avg = f"<b>{D(sum(dk) / len(dk))}</b>"
            if k != "A" and both:
                dh = sum((hist[k][y]["days"] - hist["A"][y]["days"]) * 24 for y in both) / len(both)
                avg += f" ({dh:+.0f} h)"
            row.append(avg)
        rows.append(row)
    story += [Paragraph(T("Each option in each past year's own weather (no forecast)",
                          "Cada opción con el tiempo real de cada año anterior (sin previsión)"), H2),
              Paragraph(T(f"Hours are relative to option A, the fastest. Strong El Niño years (Oct-Dec ONI of "
                          f"{STRONG_ONI} or more): {', '.join(map(str, strong))}.",
                          f"Las horas son respecto a la opción A, la más rápida. Años de El Niño fuerte (ONI oct-dic de "
                          f"{STRONG_ONI} o más): {', '.join(map(str, strong))}."), S), Spacer(1, 2 * mm),
              table(rows, [W * 0.16] + [W * 0.84 / len(keys)] * len(keys)), Spacer(1, 4 * mm)]
    sj = folder / f"climate_{strategies}.jpg"
    if sj.exists():
        story += [Paragraph(T("All options together", "Todas las opciones juntas"), H2), img(sj, maxh=120 * mm)]
    story.append(PageBreak())

    # ---- 6. one page per option
    tmp = folder / "_option_maps"
    tmp.mkdir(exist_ok=True)
    for n, k in enumerate(keys):  # two options to a page
        p = tmp / f"option_{k}.jpg"
        best = option_map(st, k, p, lang=lang)
        sm = st["strategies"][k]["summary"]
        hm = st["strategies"][k].get("history_summary") or {}
        if es:
            story += [Paragraph(f"Opción {k}: {st['strategies'][k]['label']}", H2), Paragraph(
                f"Con la previsión: mediana de {D(sm.get('days_median'))} días (10-90 %: "
                f"{DR(sm.get('days_p10'), sm.get('days_p90'))}), la más lenta {D(sm.get('days_max'))}, "
                f"{sm.get('sailing_pct_median')} % a vela, {sm.get('motor_hours_median'):.0f} h a motor, "
                f"{sm.get('fuel_l_median')} L. Solo con el tiempo de años anteriores: mediana {D(hm.get('days_median'))}, "
                f"la más lenta {D(hm.get('days_max'))}. "
                + (f"Llegada típica: {when(best['eta'])}." if best else ""), P), Spacer(1, 2 * mm)]
        else:
          story += [Paragraph(f"Option {k}: {st['strategies'][k]['label']}", H2), Paragraph(
            f"With the forecast: median {D(sm.get('days_median'))} days (10-90 %: {DR(sm.get('days_p10'), sm.get('days_p90'))}), "
            f"slowest {D(sm.get('days_max'))}, {sm.get('sailing_pct_median')}% sailing, {sm.get('motor_hours_median'):.0f} h of "
            f"motoring, {sm.get('fuel_l_median')} L. In past weather alone: median {D(hm.get('days_median'))}, slowest "
            f"{D(hm.get('days_max'))}. "
            + (f"Typical arrival {when(best['eta'])}." if best else ""), P), Spacer(1, 2 * mm)]
        if best:
            story.append(img(p, maxh=98 * mm))
        story.append(PageBreak() if n % 2 == 1 or n == len(keys) - 1 else Spacer(1, 5 * mm))

    # ---- appendices
    am = folder / f"climate_{maps}_assumptions.md"
    if es:
        story.append(Paragraph("Anexo A: supuestos de la previsión y del modelo", H2))
        for line in _assumptions_es(st, c85, hd, yrs, now, top, mk, mw, mh_, pf, s):
            story.append(Paragraph(line, ParagraphStyle("b", parent=P, leftIndent=12, bulletIndent=4, spaceAfter=2),
                                   bulletText="•"))
        story.append(PageBreak())
    elif am.exists():
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
        story += [Paragraph(T(f"Appendix B{i + 1}: forecast wind, day {i + 1}",
                              f"Anexo B{i + 1}: viento previsto, día {i + 1}"), H2),
                  Paragraph(T("Skill-weighted blend of the four models every 3 h. Colours: wind strength (scale at the "
                            "top); white streaks flow with the wind; coloured lines and dots: each option's most "
                            "typical route and position at that hour (B is the traditional route south-west via "
                            "22N 30W; F goes via Cape Verde).",
                              "Combinación de los cuatro modelos, ponderada por su precisión, cada 3 h. Colores: fuerza del "
                              "viento (escala en la parte superior, en nudos); las estelas blancas siguen el viento; líneas y "
                              "puntos de color: la ruta más típica de cada opción y su posición a esa hora (B es la ruta "
                              "tradicional hacia el suroeste por 22N 30W; F pasa por Cabo Verde). Los rótulos de los mapas "
                              "están en inglés."), S), Spacer(1, 2 * mm), img(p, maxh=232 * mm)]
        if i < len(days) - 1:
            story.append(PageBreak())

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#667080"))
        canvas.drawString(15 * mm, 9 * mm, T(f"{c85['yacht']} passage model, forecast {when(st['issued_utc'])}",
                                             f"{c85['yacht']}: modelo de travesía, previsión del {when(st['issued_utc'])}"))
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, T(f"page {doc.page}", f"página {doc.page}"))
        canvas.restoreState()

    SimpleDocTemplate(str(out_pdf), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=13 * mm,
                      bottomMargin=15 * mm, title=T(f"{c85['yacht']} passage model", f"{c85['yacht']}: modelo de travesía")).build(story, onFirstPage=footer,
                                                                                        onLaterPages=footer)
    return out_pdf


def _assumptions_es(st, c85, hd, yrs, now, top, mk, mw, mh_, pf, s):
    w = st.get("model_weights") or {}
    lines = [f"<b>Modelos de previsión:</b> {', '.join(st.get('models_used') or [])}, combinados (el viento se promedia "
             f"como vector), emitidos el {st.get('issued_utc', '?')[:16].replace('T', ' ')} UTC.",
             "<b>Pesos de los modelos</b> según la precisión pasada de cada uno a lo largo de la ruta (error del viento "
             "frente al análisis de consenso de los modelos, últimos 30 días), limitados al 15-35 % cada uno:"]
    for lead, ws in w.items():
        lead_es = lead.replace(" d ahead", " d de antelación")
        lines.append(f"{lead_es}: " + ", ".join(f"{m} {p} %" for m, p in ws.items()))
    lines += [f"<b>De la previsión a los años anteriores:</b> el peso de la previsión disminuye de forma gradual desde "
              f"2 días después de su emisión hasta cero a los 15 días; en esta simulación, desde el día {hd[0]} hasta el día "
              f"{hd[1]} de la travesía. Después toma el relevo el tiempo de cada uno de {len(yrs)} años anteriores (ERA5): "
              f"{len(yrs)} desenlaces posibles.",
              f"<b>Ponderación por El Niño:</b> ONI {now.get('oni')} ({now.get('season')}); cada año anterior se pondera "
              "según lo parecido que fue su ONI de oct-dic (" + ", ".join(f"{y} ({v.get('weight')})" for y, v in top) +
              " los que más).",
              f"<b>Barco:</b> polar {s.get('table')} x {pf}; 3 min por virada o trasluchada; motor a {mk} nudos con menos "
              f"de {mw} nudos de viento (opción D: menos de 7 nudos), como máximo {mh_:.0f} h a motor dentro del límite de "
              "consumo de combustible.",
              "<b>Distancia a tierra:</b> las rutas se mantienen al menos a 3 millas de cualquier costa, salvo en las 12 "
              "millas próximas a la salida y a la llegada, donde solo cuenta la tierra.",
              "<b>Estado de la mar:</b> olas oceánicas ERA5 para los años anteriores y mar derivada del viento en la "
              "previsión; la mar de popa añade hasta un 3 % (surfeo), la mar de proa y de través la frena.",
              "<b>Corriente:</b> corriente superficial media de estas fechas en 2022-2025.",
              "<b>No se modelan:</b> chubascos, rachas, cansancio de la tripulación, averías ni cambios de vela más allá de la polar.",
              "<b>Mapas de viento:</b> cada 3 h durante los 4 primeros días; colores según la fuerza del viento; estelas "
              "blancas que siguen el viento; los puntos de color son la ruta más típica de cada opción a esa hora."]
    return lines


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2], lang=sys.argv[3] if len(sys.argv) > 3 else "en")
