"""Configuration: TOML file for fleet and tuning; secrets only from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

DEFAULT_CONFIG_PATHS = ("/etc/yachtspeed/config.toml", "config.toml")


@dataclass
class Yacht:
    name: str
    mmsi: str
    model: str = ""
    verified: bool = False
    notes: str = ""
    callsign: str = ""
    length_m: float | None = None
    yb_race: str = ""  # YB Tracking race code, e.g. "arc2026"
    yb_team: str = ""  # team name on the tracker if it differs from `name`
    speed_table: str = ""  # fallback polar for passages, e.g. "oyster885gt_orc"
    polar_factor: float = 1.0  # scale for speed_table (racing VPP -> cruising)
    motor_below_kn: float | None = None  # passages: motor when sailing would be slower than this
    motor_speed_kn: float | None = None  # passages: motoring speed
    destination: str = ""  # named destination for reports, e.g. "rubicon" (see speedtable.DESTINATIONS)


@dataclass
class Config:
    db_path: str = "yachtspeed.db"
    # [[lat_min, lon_min], [lat_max, lon_max]] boxes (AISStream format)
    bounding_boxes: list = field(default_factory=lambda: [[[30.0, -7.0], [46.0, 37.0]]])
    fleet: list = field(default_factory=list)
    bucket_minutes: int = 10
    raw_samples_per_type_per_day: int = 20
    # weather
    cell_deg: float = 0.1
    settle_minutes: int = 90  # wait this long after a report before enriching it
    recent_days: int = 2  # newer than this: forecast API; older: historical-forecast API
    max_fetch_attempts: int = 8
    calls_per_minute: int = 300
    calls_per_hour: int = 3000
    calls_per_day: int = 8000
    enrich_interval_s: int = 120
    # model
    min_sog_kn: float = 2.0
    max_sog_kn: float = 30.0
    exclude_nav_status: list = field(default_factory=lambda: [1, 5, 6])
    min_matches: int = 8
    tws_tol_kn: float = 2.5
    tws_tol_frac: float = 0.2
    twa_tol_deg: float = 20.0
    wave_tol_m: float = 0.5
    voyage_gap_hours: float = 6.0
    # routing in the hourly server report (each model = one gridded forecast download per 6 h)
    report_route_models: list = field(default_factory=lambda: ["blend"])  # blend = average of 4 models
    report_route_res: float = 1.0
    # MarineTraffic screenshots read on the server by OCR (Dropbox shared-folder link, view-only)
    screenshots_url: str = ""
    # backups
    backup_dir: str = "backups"
    backup_keep: int = 14

    @property
    def mmsis(self) -> list[str]:
        return [y.mmsi for y in self.fleet if y.mmsi]

    @property
    def aisstream_key(self) -> str | None:
        return os.environ.get("AISSTREAM_API_KEY") or None


def load_config(path: str | None = None) -> Config:
    path = path or os.environ.get("YACHTSPEED_CONFIG")
    candidates = [path] if path else list(DEFAULT_CONFIG_PATHS)
    data: dict = {}
    for p in candidates:
        if p and Path(p).exists():
            with open(p, "rb") as f:
                data = tomllib.load(f)
            break
    else:
        if path:
            raise FileNotFoundError(path)
    cfg = Config()
    fleet = data.pop("yacht", [])
    for section in ("collector", "weather", "model", "backup", "storage", "report", "screenshots"):
        data.update(data.pop(section, {}))
    for key, value in data.items():
        if "key" in key.lower() or "token" in key.lower():
            raise ValueError(f"{key}: secrets belong in the environment (AISSTREAM_API_KEY), not config")
        if not hasattr(cfg, key):
            raise ValueError(f"unknown config key: {key}")
        setattr(cfg, key, value)
    cfg.fleet = [Yacht(**{**y, "mmsi": str(y.get("mmsi", "")).strip()}) for y in fleet]
    if "YACHTSPEED_DB" in os.environ:
        cfg.db_path = os.environ["YACHTSPEED_DB"]
    return cfg
