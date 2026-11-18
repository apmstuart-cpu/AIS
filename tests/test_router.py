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
    assert land.is_land(29.03, -13.65) and not land.is_land(28.80, -13.72, core=True)
    assert land.is_land(28.80, -13.72)  # the Bocaina strait is within 3 nm of land: kept off it at sea
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


def test_forecast_fetch_never_asks_beyond_16_days():
    from datetime import timedelta

    from yachtspeed.geo import utcnow
    from yachtspeed.router import WeatherGrid

    asked = []

    class C:
        def fetch(self, provider, kind, lat, lon, start, end):
            asked.append(end)
            return []

    t0 = utcnow() + timedelta(days=4)
    g = WeatherGrid(connect_mem(), C(), (20, -30, 21, -29), t0, t0 + timedelta(days=21), "om_model:gfs_seamless",
                    res=1.0, use_marine=False)
    g.load()
    limit = (utcnow() + timedelta(days=16)).strftime("%Y-%m-%d")
    assert asked and all(e <= limit for e in asked)


def connect_mem():
    from yachtspeed.db import connect

    return connect(":memory:")


def test_final_upwind_miles_are_motored_when_there_is_an_engine():
    land = mask_for_bbox((15.0, -64.0, 19.0, -60.0))
    g = ConstGrid(16, 80.0, T0)  # trade from the east: English Harbour is dead upwind from the west
    start, dest = (17.05, -62.20), (17.005, -61.765)
    sail = route(g, land, start, T0, dest, "oyster885_std", 0.85, step_h=1.0, max_hours=24)
    motor = route(g, land, start, T0, dest, "oyster885_std", 0.85, step_h=1.0, max_hours=24,
                  motor_wind_below_kn=10, motor_speed_kn=8)
    assert motor["status"] == "ok" and motor["hours"] < 6
    assert sail["status"] != "ok" or sail["hours"] > motor["hours"]


def test_routes_keep_three_miles_off_the_coast():
    from yachtspeed.geo import haversine_nm

    land = mask_for_bbox((27.0, -19.0, 30.0, -13.0))
    assert land.offshore_nm == 3.0
    assert land.is_land(28.88, -17.92) and not land.is_land(28.88, -17.92, core=True)  # 2.4 nm off La Palma
    assert not land.is_land(28.95, -17.92)  # 6.6 nm off: open water


def test_island_across_the_finish_is_rounded_not_overshot():
    """An ocean crossing whose straight finish is blocked by an island (as Antigua blocks English Harbour
    from the north): the fleet must work round it, not sail on past the destination and come back."""
    from yachtspeed.geo import initial_bearing

    s, dest = (28.857, -13.817), (17.0, -61.8)
    b = initial_bearing(*dest, *s)

    def off(p, nm, brg):
        return (p[0] + nm / 60 * math.cos(math.radians(brg)),
                p[1] + nm / 60 * math.sin(math.radians(brg)) / math.cos(math.radians(p[0])))

    c = off(dest, 15, b)
    corners = [off(off(c, 15, b + 90), 3, b), off(off(c, 15, b - 90), 3, b), off(off(c, 15, b - 90), -3, b),
               off(off(c, 15, b + 90), -3, b)]
    land = LandMask([[[lo, la] for la, lo in corners]], (10, -70, 35, -5), 0.024, 3.0)
    r = route(ConstGrid(16, 60.0, T0), land, s, T0, dest, "oyster885_std", 0.85, step_h=4.0, max_hours=24 * 28,
              use_waves=False, use_current=False, motor_wind_below_kn=7, motor_speed_kn=8.5, motor_max_h=177)
    assert r["status"] == "ok"
    past = max((haversine_nm(p["lat"], p["lon"], *dest) for p in r["track"] if p["lon"] < dest[1] - 0.01), default=0)
    assert past < 60  # before the fix: 141 nm past the destination


def test_max_twa_keeps_her_off_a_dead_run():
    """Wind dead astern for the whole leg: with max_twa=150 she broad-reaches and gybes, never sailing deeper."""
    g = ConstGrid(16, 90.0, T0)  # from the east, destination due west
    free = route(g, NO_LAND, (20.0, -30.0), T0, (20.0, -34.0), "oyster885_std", 0.85, step_h=1.0)
    capped = route(g, NO_LAND, (20.0, -30.0), T0, (20.0, -34.0), "oyster885_std", 0.85, step_h=1.0, max_twa=150)
    assert free["status"] == capped["status"] == "ok"
    assert max(p["twa_deg"] for p in capped["track"][1:] if p.get("twa_deg") is not None) <= 150
    assert capped["maneuvers"] >= 1 and capped["hours"] < free["hours"] * 1.1  # gybing costs little


def test_measured_polar_follows_the_logged_percentages():
    from yachtspeed import speedtable as S

    hist = {"overall": {"median_pct": 84, "hours": 2000},
            "by_wind_and_angle": {"12-16 kn": {"110-150 deg": {"median_pct": 80, "hours": 477},
                                               "40-70 deg": {"median_pct": 93, "hours": 37},
                                               "150-180 deg": {"median_pct": 120, "hours": 300}}}}
    fac = S.measured_factors(hist)
    assert abs(fac[(14.0, 130.0)] - (477 * 0.80 + 50 * 0.84) / 527) < 0.002   # well sampled: close to its own 80%
    assert 0.84 < fac[(14.0, 55.0)] < 0.93                                     # thin: pulled toward 84%
    assert fac[(14.0, 165.0)] == 0.95                                          # above the polar: capped
    S.register_measured(hist, name="t_measured")
    base, meas = S.table_speed(14, 130, "oyster885_std"), S.table_speed(14, 130, "t_measured")
    assert fac[(14.0, 130.0)] - 0.005 <= meas / base <= 0.84  # blended with the neighbouring wind bands (84%)


def test_light_penalty_finds_the_breeze():
    """Light air north of 22N, trades south of it: the most-wind setting goes south and spends far more time in 10+ kn."""
    from yachtspeed.climate import _wind10_pct

    class Split:
        t0, data_end_h = T0, 10000

        def sample(self, la, lo, t):
            return {"wind_speed_kn": 6.0 if la > 22 else 14.0, "wind_dir_from_deg": 65.0}

    kw = dict(step_h=4.0, max_hours=24 * 28, use_waves=False, use_current=False, motor_wind_below_kn=10,
              motor_speed_kn=8.5, motor_max_h=177)
    s, d = (28.0, -20.0), (17.0, -50.0)
    plain = route(Split(), NO_LAND, s, T0, d, "oyster885_std", 0.85, **kw)
    breezy = route(Split(), NO_LAND, s, T0, d, "oyster885_std", 0.85, light_penalty=(10.0, 10.0), **kw)
    assert _wind10_pct(breezy["track"]) >= _wind10_pct(plain["track"]) + 20
