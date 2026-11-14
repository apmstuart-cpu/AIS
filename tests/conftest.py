import json

import pytest

from yachtspeed import db
from yachtspeed.config import Config, Yacht


@pytest.fixture
def cfg(tmp_path):
    c = Config(db_path=str(tmp_path / "t.db"), backup_dir=str(tmp_path / "bk"), settle_minutes=0)
    c.fleet = [Yacht(name="TESTBOAT", mmsi="235000001", verified=True)]
    return c


@pytest.fixture
def conn(cfg):
    c = db.connect(cfg.db_path)
    db.sync_fleet(c, cfg.fleet)
    return c


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    """Stands in for requests.Session; `handler(url, params)` returns (status, body)."""

    def __init__(self, handler):
        self.handler = handler
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, dict(params or {})))
        return FakeResponse(*self.handler(url, params or {}))


def om_body(params, overrides=None):
    """Synthesize an Open-Meteo-shaped response for one day."""
    day = params["start_date"]
    times = [f"{day}T{h:02d}:00" for h in range(24)]
    names = params["hourly"].split(",")
    hourly, units = {"time": times}, {"time": "iso8601"}
    for n in names:
        base = n.split("_previous_day")[0]
        if base.startswith("wind_speed") or base.startswith("wind_gusts"):
            hourly[n], units[n] = [12.0] * 24, "kn"
        elif base.startswith("wind_direction"):
            hourly[n], units[n] = [270.0] * 24, "°"
        elif n == "wave_height":
            hourly[n], units[n] = [1.0] * 24, "m"
        elif n == "ocean_current_velocity":
            hourly[n], units[n] = [1.852] * 24, "km/h"  # = 1 kn
        elif n == "ocean_current_direction":
            hourly[n], units[n] = [90.0] * 24, "°"
        else:
            hourly[n], units[n] = [5.0] * 24, "x"
    for k, v in (overrides or {}).items():
        hourly[k] = v
    return {"latitude": params["latitude"], "longitude": params["longitude"],
            "hourly": hourly, "hourly_units": units}
