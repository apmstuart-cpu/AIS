"""Server job: download the owner's MarineTraffic screenshot folder (Dropbox shared link),
read new screenshots with OCR, and store the fixes. Replaces the cloud screenshot job.

Treats the download as untrusted: only image files are extracted (by base name, size-capped)
into a private temp directory, which is deleted afterwards.
"""

from __future__ import annotations

import io
import logging
import shutil
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path

import requests

from .ais import Position
from .geo import haversine_nm, iso, utcnow
from .logutil import log

logger = logging.getLogger("screenshots")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
MAX_FILE = 25 * 1024 * 1024
MAX_FILES = 500


def _zip_url(url: str) -> str:
    url = url.strip()
    if "dl=0" in url:
        return url.replace("dl=0", "dl=1")
    return url + ("&" if "?" in url else "?") + "dl=1"


def download(url: str, dest: Path) -> list[Path]:
    r = requests.get(_zip_url(url), timeout=180, headers={"User-Agent": "yachtspeed/0.2"})
    r.raise_for_status()
    out = []
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for info in z.infolist()[:MAX_FILES]:
            name = Path(info.filename).name
            if info.is_dir() or not name.lower().endswith(IMAGE_EXT) or info.file_size > MAX_FILE:
                continue
            p = dest / name
            with z.open(info) as src, open(p, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            out.append(p)
    return out


def _duplicate(conn, mmsi, fx) -> bool:
    lo, hi = iso(fx.fix_utc - timedelta(minutes=10)), iso(fx.fix_utc + timedelta(minutes=10))
    for r in conn.execute("SELECT lat, lon FROM ais_reports WHERE mmsi=? AND provider_time_utc BETWEEN ? AND ?",
                          (mmsi, lo, hi)):
        if haversine_nm(r["lat"], r["lon"], fx.lat, fx.lon) <= 1.0:
            return True
    return False


def run(conn, cfg) -> dict:
    from .collector import Store
    from .ocr import available, read_screenshot

    if not cfg.screenshots_url:
        return {"status": "not_configured"}
    if not available():
        return {"status": "tesseract_missing"}
    names = {y.name: y.mmsi for y in cfg.fleet if y.mmsi}
    done = {r[0] for r in conn.execute("SELECT file FROM ocr_files")}
    stats = {"files": 0, "new": 0, "stored": 0, "duplicates": 0, "review": 0}
    tmp = Path(tempfile.mkdtemp(prefix="yachtspeed-ocr-"))
    try:
        files = download(cfg.screenshots_url, tmp)
        stats["files"] = len(files)
        store = Store(conn, cfg)
        for p in sorted(files):
            if p.name in done:
                continue
            stats["new"] += 1
            try:
                fx = read_screenshot(str(p), list(names))
            except Exception as e:  # a bad image must not stop the rest
                conn.execute("INSERT OR REPLACE INTO ocr_files(file, processed_utc, status, notes) VALUES (?,?,?,?)",
                             (p.name, iso(utcnow()), "error", f"{type(e).__name__}: {e}"[:200]))
                stats["review"] += 1
                continue
            stored = 0
            if fx.status == "ok":
                mmsi = names[fx.yacht]
                if _duplicate(conn, mmsi, fx):
                    stats["duplicates"] += 1
                else:
                    stored = int(store.store_position(Position(
                        mmsi=mmsi, msg_type="ocr", ais_class=None, provider_time=fx.fix_utc, lat=fx.lat, lon=fx.lon,
                        sog_kn=fx.sog_kn, cog_deg=fx.cog_deg, heading_deg=None, nav_status=None, ais_second=None,
                        position_accuracy=None), source="ocr:marinetraffic"))
                    stats["stored"] += stored
            else:
                stats["review"] += 1
            conn.execute(
                "INSERT OR REPLACE INTO ocr_files VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (p.name, iso(utcnow()), fx.status, fx.yacht, iso(fx.fix_utc) if fx.fix_utc else None, fx.lat,
                 fx.lon, fx.sog_kn, fx.cog_deg, stored, "; ".join(fx.notes) or None))
            log(logger, logging.INFO, "screenshot", file=p.name, status=fx.status, yacht=fx.yacht, stored=stored)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    stats["status"] = "ok"
    return stats


def review_list(conn, hours=48) -> list[dict]:
    since = iso(utcnow() - timedelta(hours=hours))
    return [dict(r) for r in conn.execute(
        "SELECT file, status, yacht, notes FROM ocr_files WHERE status != 'ok' AND processed_utc >= ? ORDER BY file",
        (since,))]
