import math
import time
from datetime import timedelta

import pytest

from yachtspeed.geo import haversine_nm, utcnow
from yachtspeed.land import LandMask, mask_for_bbox
from yachtspeed.router import route


class ConstGrid:
    """Stand-in for WeatherGrid: uniform wind, no sea state or current."""

    def __init__(self, speed, dir_from, t0, hours=1000):
        self.w = {"wind_speed_kn": speed, "wind_dir_from_deg": dir_from}
        self.t0 = t0
        self.data_end_h = hours

    def sample(self, lat, lon, t):
        return dict(self.w)


NO_LAND = LandMask([], (0, -80, 60, 20), res=0.5)
T0 = utcnow().replace(minute=0, second=0, microsecond=0)


def test_dead_downwind_route_gybes_and_beats_sailing_straight():
    from yachtspeed.speedtable import boat_speed, table_speed

    g = ConstGrid(12, 0.0, T0)  # wind from north
    start, dest = (31.0, -20.0), (29.0, -20.0)  # 120 nm due south: dead downwind
    r = route(g, NO_LAND, start, T0, dest, "oyster885_std", 0.85, step_h=1.0, maneuver_min=3)
    assert r["status"] == "ok" and r["maneuvers"] >= 1
    straight_h = 120 / (boat_speed(12, 180, "oyster885_std", 0.85))
    vmg_h = 120 / table_speed(12, 180, "oyster885_std", 0.85)
    assert r["hours"] < straight_h  # gybing beats running dead downwind
    assert r["hours"] == pytest.approx(vmg_h, rel=0.12)  # and is close to the polar's VMG estimate
    assert r["sailed_nm"] > r["direct_nm"]


def test_upwind_route_tacks():
    g = ConstGrid(12, 180.0, T0)  # wind from south, destination south
    r = route(g, NO_LAND, (31.0, -20.0), T0, (30.0, -20.0), "oyster885_std", 0.85, step_h=0.5)
    assert r["status"] == "ok" and r["maneuvers"] >= 1
    headings = {p["heading"] for p in r["track"] if p["heading"] is not None}
    assert not any(abs(((h - 180 + 540) % 360) - 180) < 35 for h in headings)  # never inside the no-go zone


def test_route_goes_round_an_island():
    island = [[-20.3, 29.8], [-19.7, 29.8], [-19.7, 30.2], [-20.3, 30.2], [-20.3, 29.8]]
    land = LandMask([island], (28, -22, 32, -18), res=0.02)
    g = ConstGrid(15, 90.0, T0)  # beam reach southwards
    r = route(g, land, (31.0, -20.0), T0, (29.0, -20.0), "oyster885_std", 0.85, step_h=0.5)
    assert r["status"] == "ok"
    pts = r["track"]
    for a, b in zip(pts, pts[1:]):
        assert land.segment_clear(a["lat"], a["lon"], b["lat"], b["lon"])
    assert r["sailed_nm"] > r["direct_nm"] + 2  # had to deviate


def test_real_land_mask_blocks_lanzarote_and_finds_a_way_to_rubicon():
    land = mask_for_bbox((27.0, -15.5, 34.0, -8.0))
    assert land.is_land(29.03, -13.65) and not land.is_land(28.80, -13.72)
    g = ConstGrid(14, 30.0, T0)  # NE trade
    r = route(g, land, (32.29, -9.77), T0, (28.857, -13.817), "oyster885_std", 0.85, step_h=1.0)
    assert r["status"] == "ok"
    for a, b in zip(r["track"], r["track"][1:]):
        assert land.segment_clear(a["lat"], a["lon"], b["lat"], b["lon"], skip_near=[(28.857, -13.817)], skip_nm=2)


def test_transatlantic_runs_in_reasonable_time():
    land = mask_for_bbox((7.0, -66.0, 35.0, -10.0))
    g = ConstGrid(16, 60.0, T0)  # steady NE trade
    t = time.time()
    r = route(g, land, (28.13, -15.418), T0, (14.083, -60.955), "oyster885_std", 0.85, step_h=4.0,
              max_hours=24 * 25)
    took = time.time() - t
    assert r["status"] == "ok", r
    assert 2600 < r["sailed_nm"] < 3300 and 10 * 24 < r["hours"] < 20 * 24
    assert took < 120


def test_blend_averages_models_as_vectors():
    from yachtspeed.router import BlendGrid

    a = ConstGrid(10, 350.0, T0)
    b = ConstGrid(14, 10.0, T0)
    w = BlendGrid([a, b]).sample(30, -20, T0)
    assert w["wind_speed_kn"] == pytest.approx(12.0)
    assert min(w["wind_dir_from_deg"], 360 - w["wind_dir_from_deg"]) < 2.0  # ~north, not 180
    assert w["models"] == 2
    r = route(BlendGrid([a, b]), NO_LAND, (31.0, -20.0), T0, (30.0, -20.0), "oyster885_std", 0.85)
    assert r["status"] == "ok"


def test_grid_waits_for_per_minute_budget(conn, monkeypatch):
    from datetime import timedelta as td

    from yachtspeed import router
    from yachtspeed.weather import QuotaExceeded

    calls = {"n": 0, "slept": 0}

    class C:
        def fetch(self, *a):
            calls["n"] += 1
            if calls["n"] == 1:
                raise QuotaExceeded("local m budget", utcnow() + td(seconds=2))
            return []

    monkeypatch.setattr("time.sleep", lambda s: calls.__setitem__("slept", calls["slept"] + 1))
    g = router.WeatherGrid(conn, C(), (30, -20, 30, -20), T0, T0 + td(hours=2), "om_model:x", res=0.5,
                           use_marine=False)
    g.load()
    assert calls["n"] == 2 and calls["slept"] == 1 and g.missing_cells == 0


def test_sail_track_reproduces_route_time_on_same_forecast():
    from yachtspeed.router import sail_track

    g = ConstGrid(13, 30.0, T0)  # NE breeze, broad reach / run to the SW
    r = route(g, NO_LAND, (32.0, -10.0), T0, (30.5, -12.0), "oyster885_std", 0.85, step_h=1.0)
    ev = sail_track(g, r["track"], T0, "oyster885_std", 0.85, use_waves=False, use_current=False)
    assert ev["status"] == "ok"
    assert ev["hours"] == pytest.approx(r["hours"], rel=0.06)


def test_course_under_other_models():
    from yachtspeed.router import BlendGrid, sail_track

    light = ConstGrid(8, 30.0, T0)
    fresh = ConstGrid(18, 30.0, T0)
    blend = BlendGrid([light, fresh], ["light", "fresh"])
    r = route(blend, NO_LAND, (32.0, -10.0), T0, (30.5, -12.0), "oyster885_std", 0.85, step_h=1.0)
    h_light = sail_track(light, r["track"], T0, "oyster885_std", 0.85)["hours"]
    h_fresh = sail_track(fresh, r["track"], T0, "oyster885_std", 0.85)["hours"]
    assert h_fresh < r["hours"] < h_light  # the same course is faster in more wind
