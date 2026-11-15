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
REVIEW_DIR = Path("/var/lib/yachtspeed/report/review")  # published with the report for a manual read
MANUAL_CSV = Path("/var/lib/yachtspeed/marinetraffic_fixes.csv")  # fixes typed in from screenshots


def manual_files(path: Path = MANUAL_CSV) -> set:
    """Screenshot names already captured by hand in the repo's fixes CSV."""
    import csv

    try:
        with open(path, newline="") as f:
            return {(r.get("screenshot") or "").strip() for r in csv.DictReader(f)} - {""}
    except OSError:
        return set()


def _review_copy(src: Path, review_dir: Path) -> None:
    """Downscaled JPEG of a screenshot that OCR could not read, for a manual read."""
    from PIL import Image

    review_dir.mkdir(parents=True, exist_ok=True)
    img = Image.open(src).convert("RGB")
    if img.width > 1800:
        img = img.resize((1800, int(img.height * 1800 / img.width)))
    img.save(review_dir / (src.stem + ".jpg"), "JPEG", quality=85)


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


def stale_repeat(conn, mmsi, t, lat, lon, sog_kn, exclude_source=None):
    """An earlier fix at (almost) the same place while the yacht is reported moving means the
    screenshot showed an old position and its received time was misread. Returns that time."""
    if sog_kn is None or sog_kn < 2:
        return None
    q = ("SELECT provider_time_utc, lat, lon, source FROM ais_reports WHERE mmsi=? AND provider_time_utc BETWEEN ? AND ?"
         " ORDER BY provider_time_utc DESC")
    for r in conn.execute(q, (mmsi, iso(t - timedelta(hours=12)), iso(t - timedelta(minutes=30)))):
        if exclude_source and r["source"] == exclude_source and r["provider_time_utc"] == iso(t):
            continue
        if haversine_nm(r["lat"], r["lon"], lat, lon) <= 0.3:
            return r["provider_time_utc"]
    return None


def purge_stale(conn, days=3) -> list:
    """Remove screenshot fixes already stored that are stale repeats (see stale_repeat)."""
    from .geo import parse_iso

    gone = []
    rows = conn.execute("SELECT id, mmsi, provider_time_utc, lat, lon, sog_kn FROM ais_reports WHERE source='ocr:marinetraffic'"
                        " AND provider_time_utc >= ?", (iso(utcnow() - timedelta(days=days)),)).fetchall()
    for r in rows:
        earlier = stale_repeat(conn, r["mmsi"], parse_iso(r["provider_time_utc"]), r["lat"], r["lon"], r["sog_kn"])
        if earlier:
            conn.execute("DELETE FROM enrichment WHERE report_id=?", (r["id"],))
            conn.execute("DELETE FROM ais_reports WHERE id=?", (r["id"],))
            conn.execute("UPDATE ocr_files SET status='review', stored=0, notes=? WHERE fix_utc=? "
                         "AND abs(lat - ?) < 0.001 AND abs(lon - ?) < 0.001",
                         (f"same position as the fix at {earlier}: received time probably misread",
                          r["provider_time_utc"], r["lat"], r["lon"]))
            gone.append((r["mmsi"], r["provider_time_utc"], earlier))
            log(logger, logging.WARNING, "stale_fix_removed", mmsi=r["mmsi"], time=r["provider_time_utc"], same_as=earlier)
    return gone


def run(conn, cfg, review_dir: Path = REVIEW_DIR) -> dict:
    from .collector import Store
    from .ocr import available, read_screenshot

    if not cfg.screenshots_url:
        return {"status": "not_configured"}
    if not available():
        return {"status": "tesseract_missing"}
    names = {y.name: y.mmsi for y in cfg.fleet if y.mmsi}
    stale = purge_stale(conn)  # before the review list, so a removed fix's screenshot is published
    done = {r[0] for r in conn.execute("SELECT file FROM ocr_files")}
    manual = manual_files()
    pending = {r[0] for r in conn.execute("SELECT file FROM ocr_files WHERE status != 'ok'")} - manual
    stats = {"files": 0, "new": 0, "stored": 0, "duplicates": 0, "review": 0,
             "stale_removed": len(stale)}
    tmp = Path(tempfile.mkdtemp(prefix="yachtspeed-ocr-"))
    try:
        files = download(cfg.screenshots_url, tmp)
        stats["files"] = len(files)
        store = Store(conn, cfg)
        for p in sorted(files):
            if p.name in pending and not (review_dir / (p.stem + ".jpg")).exists():
                try:
                    _review_copy(p, review_dir)
                except Exception as e:
                    log(logger, logging.WARNING, "review_copy_failed", file=p.name, error=str(e)[:120])
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
                earlier = stale_repeat(conn, names[fx.yacht], fx.fix_utc, fx.lat, fx.lon, fx.sog_kn)
                if earlier:
                    fx.status = "review"
                    fx.notes.append(f"same position as the fix at {earlier}: received time probably misread")
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
                if p.name not in manual:
                    try:
                        _review_copy(p, review_dir)
                    except Exception as e:
                        log(logger, logging.WARNING, "review_copy_failed", file=p.name, error=str(e)[:120])
            conn.execute(
                "INSERT OR REPLACE INTO ocr_files VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (p.name, iso(utcnow()), fx.status, fx.yacht, iso(fx.fix_utc) if fx.fix_utc else None, fx.lat,
                 fx.lon, fx.sog_kn, fx.cog_deg, stored, "; ".join(fx.notes) or None))
            log(logger, logging.INFO, "screenshot", file=p.name, status=fx.status, yacht=fx.yacht, stored=stored)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # drop review copies once a fix has been captured by hand (or after 3 days)
    if review_dir.exists():
        keep = {Path(f).stem for f in pending}
        for c in review_dir.glob("*.jpg"):
            old = (utcnow().timestamp() - c.stat().st_mtime) > 3 * 86400
            if c.stem not in keep or old:
                c.unlink(missing_ok=True)
    stats["status"] = "ok"
    return stats


def review_list(conn, hours=48) -> list[dict]:
    since = iso(utcnow() - timedelta(hours=hours))
    manual = manual_files()
    return [dict(r) for r in conn.execute(
        "SELECT file, status, yacht, notes FROM ocr_files WHERE status != 'ok' AND processed_utc >= ? ORDER BY file",
        (since,)) if r["file"] not in manual]
