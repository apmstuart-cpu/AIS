"""Read MarineTraffic screenshots on the server with Tesseract OCR (no cloud model needed).

Handles the two layouts the owner captures:
  * web map (desktop): hover label "<NAME> [XX] at 9.1 kn / 241°" + "Position received: 1 hour,
    49 minutes ago", and the cursor coordinate box "(32.003418, -11.274719)" bottom right.
    The capture time comes from the file name "YYYY-MM-DD HHMM <Yacht>.png" (UTC).
  * app "Current voyage/position" tab: "Data received 15 Nov 12:27 UTC", "32.3367° N, 9.5176° W",
    "Course 242°" and the speed bubble "8.8 kn".
Anything that does not parse cleanly is returned with status 'review' and is never guessed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

FNAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:\s+at\s+|[ _T]?)(\d{2})[:.]?(\d{2})(?:[:.]\d{2})?\s*(.*?)\.(png|jpe?g|webp)$",
                      re.I)  # "2026-11-15 1000 Alibi.png" or macOS "Screenshot 2026-11-16 at 07.00.20.png" (UK time = UTC in winter)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"], 1)}


@dataclass
class OcrFix:
    file: str
    status: str  # ok | review
    yacht: str | None = None
    fix_utc: datetime | None = None
    lat: float | None = None
    lon: float | None = None
    sog_kn: float | None = None
    cog_deg: float | None = None
    notes: list = field(default_factory=list)
    port_calls: list = field(default_factory=list)  # [{port, arrival_utc, departure_utc}] from the app's list


def available() -> bool:
    return shutil.which("tesseract") is not None


def _tess(img, psm=6) -> str:
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    p = subprocess.run(["tesseract", "stdin", "stdout", "--psm", str(psm)], input=buf.getvalue(),
                       capture_output=True, timeout=60)
    return p.stdout.decode("utf-8", "replace")


def _prep(img, box, scale=3):
    from PIL import Image, ImageOps

    w, h = img.size
    x0, y0, x1, y1 = (int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h))
    crop = img.crop((x0, y0, x1, y1)).convert("L")
    crop = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
    return ImageOps.autocontrast(crop)


def _num(s):
    return float(s.replace(",", "."))


def parse_ago(text: str):
    """'1 hour, 49 minutes ago' (OCR-tolerant: 'hor', 'ag0', 'min') -> timedelta or None."""
    t = text.lower().replace("ag0", "ago").replace("hor", "hour").replace("hr", "hour")
    m = re.search(r"received:?\s*(.*?)\bago\b", t) or re.search(r"((?:\d+\s*\w+[, ]*){1,3})\s*ago", t)
    if not m:
        return None
    seg = m.group(1)
    total = timedelta(0)
    found = False
    for n, unit in re.findall(r"(\d+)\s*(day|hour|min|sec)", seg):
        found = True
        total += {"day": timedelta(days=int(n)), "hour": timedelta(hours=int(n)),
                  "min": timedelta(minutes=int(n)), "sec": timedelta(seconds=int(n))}[unit]
    if not found and re.search(r"just now|now", seg):
        return timedelta(0)
    return total if found else None


def parse_received_abs(text: str):
    """'Position received: 2026-11-14 00:53 UTC' (shown once a fix is about a day old)."""
    m = re.search(r"[Rr]eceived:?\s*(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})\s*UTC", text)
    if not m:
        return None
    y, mo, d, h, mi = map(int, m.groups())
    try:
        return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_label(text: str):
    """'<NAME> [XX] at 9.1 kn / 241°' -> (name, sog, cog) (any may be None)."""
    t = text.replace("|", " ")
    m = re.search(r"at\s*([0-9]+[.,]?[0-9]*)\s*kn\s*/?\s*([0-9]{1,3})", t)
    sog = cog = None
    if m:
        sog, cog = _num(m.group(1)), float(m.group(2))
        if sog > 40 and "." not in m.group(1):  # '75kn' for 7.5
            sog = sog / 10
    name = None
    n = re.search(r"([A-Z][A-Z0-9 '\-]{2,40}?)\s*\[[A-Z]{2}\]", text)
    if n:
        name = n.group(1).strip(" '‘")
    return name, sog, cog


def parse_decimal_coords(text: str):
    m = re.search(r"\(\s*(-?\d{1,2}\.\d{2,6})\s*[,;]\s*(-?\d{1,3}\.\d{2,6})\s*\)", text) or \
        re.search(r"(-?\d{1,2}\.\d{4,6})\s*[,;]\s*(-?\d{1,3}\.\d{4,6})", text)
    if not m:
        return parse_dm_coords(text)
    lat, lon = float(m.group(1)), float(m.group(2))
    if abs(lat) > 90 or abs(lon) > 180 or (lat == 0 and lon == 0):
        return None
    return lat, lon


def parse_dm_coords(text: str):
    """Degrees and decimal minutes: "32° 38.64 N" / "9° 32.00 W" (OCR may read ° as o, 0 or *)."""
    la = re.search(r"(\d{1,2})\s*[°o*º]?\s+(\d{1,2}\.\d{1,3})\s*'?\s*([NS])\b", text)
    lo = re.search(r"(\d{1,3})\s*[°o*º]?\s+(\d{1,2}\.\d{1,3})\s*'?\s*([EW])\b", text)
    if not la or not lo:
        return None
    lat = (int(la.group(1)) + float(la.group(2)) / 60) * (1 if la.group(3) == "N" else -1)
    lon = (int(lo.group(1)) + float(lo.group(2)) / 60) * (1 if lo.group(3) == "E" else -1)
    if abs(lat) > 90 or abs(lon) > 180 or float(la.group(2)) >= 60 or float(lo.group(2)) >= 60:
        return None
    return round(lat, 6), round(lon, 6)


def parse_clock(text: str, near: datetime | None):
    """Taskbar clock like '11:51 AM' or '23:57' -> datetime on the file-name date, if within
    30 min of the file-name time (handles the date rolling over at midnight)."""
    if near is None:
        return None
    for h, mnt, ap in re.findall(r"\b(\d{1,2}):(\d{2})\s*(AM|PM)?\b", text, re.I):
        h, mnt = int(h), int(mnt)
        if ap:
            h = (h % 12) + (12 if ap.upper() == "PM" else 0)
        if h > 23 or mnt > 59:
            continue
        for day in (0, -1, 1):
            cand = near.replace(hour=h, minute=mnt) + timedelta(days=day)
            if abs((cand - near).total_seconds()) <= 1800:
                return cand
    return None


def parse_app_coords(text: str):
    """'32.3367° N, 9.5176° W' -> (lat, lon)."""
    m = re.search(r"(\d{1,2}\.\d{2,6})\s*°?\s*([NS])\s*[,;]?\s*(\d{1,3}\.\d{2,6})\s*°?\s*([EW])", text)
    if not m:
        return None
    lat = float(m.group(1)) * (1 if m.group(2) == "N" else -1)
    lon = float(m.group(3)) * (1 if m.group(4) == "E" else -1)
    return lat, lon


def parse_app_time(text: str, year: int):
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\w*\s+(\d{1,2}):(\d{2})\s*UTC", text)
    if not m or m.group(2).lower() not in MONTHS:
        return None
    return datetime(year, MONTHS[m.group(2).lower()], int(m.group(1)), int(m.group(3)), int(m.group(4)),
                    tzinfo=timezone.utc)


_PC_TIME = r"(\d{2})/(\d{2})/(\d{4})\s+(\d{1,2}):(\d{2})\s*UTC"


def parse_port_calls(text: str) -> list:
    """The app's 'Latest port calls' list: a port line ('GIBRALTAR WEST ANCH, GI') followed by
    'Arrival : 13/11/2026 07:33 UTC' and 'Departure : 13/11/2026 08:07 UTC' lines."""
    def when(m):
        d, mo, y, h, mi = map(int, m.groups())
        try:
            return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)
        except ValueError:
            return None

    calls, cur = [], None
    for line in text.splitlines():
        a = re.search(r"[Aa]rriv\w*\s*:?\s*" + _PC_TIME, line)
        dep = re.search(r"[Dd]epart\w*\s*:?\s*" + _PC_TIME, line)
        if a and cur is not None:
            cur["arrival_utc"] = when(a)
        elif dep and cur is not None:
            cur["departure_utc"] = when(dep)
        else:
            m = re.search(r"([A-Z][A-Z0-9 .'\-]{2,40}?)\s*,+\s*([A-Z][A-Za-z])\b", line)
            if m and "PORT CALLS" not in line.upper():
                cur = {"port": f"{m.group(1).strip()}, {m.group(2).upper()}", "arrival_utc": None, "departure_utc": None}
                calls.append(cur)
    return [c for c in calls if c["arrival_utc"] or c["departure_utc"]]


def _scan_app_coords(img):
    """The app's coordinate label sits on the map, where a whole-page read misses it: read
    overlapping strips of the right half at higher magnification until it turns up."""
    for i in range(16):
        y0 = 0.30 + i * 0.035
        c = parse_app_coords(_tess(_prep(img, (0.45, y0, 1.0, min(1.0, y0 + 0.06)), scale=3), psm=6))
        if c:
            return c
    return None


def _scan_app_speed(img):
    """The speed bubble on the app's compass, e.g. '9.1 kn', read in strips across the map."""
    for i in range(14):
        y0 = 0.30 + i * 0.03
        m = re.search(r"\b([0-9]{1,2}[.,][0-9])\s*kn", _tess(_prep(img, (0.3, y0, 0.7, min(1.0, y0 + 0.05)), scale=3), psm=6))
        if m:
            return _num(m.group(1))
    return None


def read_screenshot(path: str, fleet_names: list[str]) -> OcrFix:
    from PIL import Image

    fname = path.rsplit("/", 1)[-1]
    fx = OcrFix(file=fname, status="review")
    m = FNAME_RE.search(fname)
    capture = None
    if m:
        capture = datetime.strptime(f"{m.group(1)} {m.group(2)}{m.group(3)}", "%Y-%m-%d %H%M").replace(
            tzinfo=timezone.utc)
        fx.yacht = _match_yacht(m.group(4), fleet_names)
    img = Image.open(path)
    full = _tess(_prep(img, (0, 0, 1, 1), scale=2), psm=11)
    # the PC's taskbar clock, when visible, is more exact than the rounded file-name time
    clock = parse_clock(_tess(_prep(img, (0.8, 0.9, 1.0, 1.0), scale=3), psm=6) + "\n" + full, capture)
    if clock is not None:
        capture = clock
    # app layout ('Data received ... UTC' and N/W coordinates)
    app_t = parse_app_time(full, (capture or datetime.now(timezone.utc)).year)
    app_c = parse_app_coords(full)
    if app_t and not app_c:
        app_c = _scan_app_coords(img)
    if app_t and app_c:
        fx.fix_utc, (fx.lat, fx.lon) = app_t, app_c
        c = re.search(r"Course\s*([0-9]{1,3})", full)
        s = re.search(r"([0-9]+[.,][0-9])\s*kn", full)
        fx.cog_deg = float(c.group(1)) if c else None
        fx.sog_kn = _num(s.group(1)) if s else _scan_app_speed(img)
        fx.yacht = fx.yacht or _match_yacht(full, fleet_names)
    else:
        # web layout: hover label in the middle third, coordinate box bottom right
        label = _tess(_prep(img, (0.25, 0.3, 0.75, 0.7)), psm=6)
        coords_txt = _tess(_prep(img, (0.75, 0.75, 1.0, 1.0), scale=4), psm=6)
        name, sog, cog = parse_label(label)
        if sog is None:
            name, sog, cog = parse_label(full)
        ago = parse_ago(label) or parse_ago(full)
        c = parse_decimal_coords(coords_txt) or parse_decimal_coords(full)
        fx.sog_kn, fx.cog_deg = sog, cog
        fx.yacht = fx.yacht or _match_yacht(name or "", fleet_names)
        if c:
            fx.lat, fx.lon = c
        else:
            fx.notes.append("coordinates not read")
        absolute = parse_received_abs(label) or parse_received_abs(full)
        if absolute is not None:
            fx.fix_utc = absolute
        elif capture and ago is not None:
            fx.fix_utc = (capture - ago).replace(second=0, microsecond=0)
        else:
            fx.notes.append("fix time not read" if capture else "no capture time in file name")
    if re.search(r"port\s*calls", full, re.I):
        fx.port_calls = parse_port_calls(_tess(_prep(img, (0, 0.6, 1, 1), scale=2), psm=6))
    if fx.sog_kn is None:
        fx.notes.append("speed not read")
    if fx.cog_deg is None:
        fx.notes.append("course not read")
    if fx.yacht is None:
        fx.notes.append("yacht not identified")
    ok = fx.yacht and fx.fix_utc and fx.lat is not None and fx.sog_kn is not None and fx.cog_deg is not None
    if ok and not (0 <= fx.sog_kn <= 40 and 0 <= fx.cog_deg < 360):
        ok = False
        fx.notes.append("implausible speed/course")
    fx.status = "ok" if ok else "review"
    return fx


def _match_yacht(text: str, fleet_names: list[str]):
    t = re.sub(r"[^A-Z0-9]", "", (text or "").upper())
    for n in sorted(fleet_names, key=len, reverse=True):
        if re.sub(r"[^A-Z0-9]", "", n.upper()) in t or (t and t in re.sub(r"[^A-Z0-9]", "", n.upper()) and len(t) >= 5):
            return n
    return None
