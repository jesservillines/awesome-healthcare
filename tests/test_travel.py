"""Phase-4 tests: drive times, cluster objectives, and the effort budget."""
from __future__ import annotations

from datetime import date

from fkt_sim.peaks import GatekeeperTier, load_peaks, load_trailheads
from fkt_sim.travel import (
    EffortModel,
    FatigueTracker,
    Objective,
    StraightLineDriveTime,
    build_objectives,
    haversine_miles,
)

PEAKS = load_peaks()
TRAILHEADS = load_trailheads()
OBJ = build_objectives(PEAKS)


# --- drive times ----------------------------------------------------------- #
def test_haversine_known_distance():
    # Denver <-> Aspen is ~100 mi great-circle; allow generous tolerance.
    d = haversine_miles(39.7392, -104.9903, 39.1911, -106.8175)
    assert 90 < d < 120


def test_drive_time_self_is_zero_and_symmetric():
    dt = StraightLineDriveTime(TRAILHEADS)
    assert dt.drive_hours("maroon_lake", "maroon_lake") == 0.0
    assert dt.drive_hours("maroon_lake", "south_colony") == dt.drive_hours(
        "south_colony", "maroon_lake"
    )


def test_cross_state_reposition_costs_real_time():
    dt = StraightLineDriveTime(TRAILHEADS)
    # Front Range (Stevens Gulch) -> San Juan (Yankee Boy) is a long haul.
    hrs = dt.drive_hours("stevens_gulch", "yankee_boy")
    assert hrs > 4.0
    assert dt.reposition_days("stevens_gulch", "yankee_boy") >= 1.0
    # A short hop within the Sawatch is cheap.
    assert dt.drive_hours("missouri_gulch", "south_winfield") < 1.0


# --- objectives ------------------------------------------------------------ #
def test_objectives_cover_all_peaks_once():
    covered = [name for o in OBJ.values() for name in o.peak_names]
    assert sorted(covered) == sorted(p.name for p in PEAKS)
    assert len(covered) == 55


def test_cluster_objective_combines_members():
    decali = OBJ["mosquito_decalibron"]
    assert decali.is_cluster
    members = [p for p in PEAKS if p.name in decali.peak_names]
    assert decali.combined_vert_ft == sum(p.vert_ft for p in members)
    assert decali.max_angle_deg == max(p.best_line.max_angle_deg for p in members)
    # Tier is the strictest (max) member tier.
    assert int(decali.tier) == max(int(p.tier) for p in members)


def test_chicago_basin_is_multiday_basecamp():
    assert OBJ["chicago_basin"].push_days == 3
    assert OBJ["chicago_basin"].is_cluster


def test_single_peak_objective_is_not_cluster():
    quandary = OBJ["quandary"]
    assert not quandary.is_cluster
    assert quandary.peak_names == ["Quandary Peak"]
    assert quandary.push_days == 1


def test_window_union_spans_members():
    crestones = OBJ["crestones"]
    # union start is the earliest member open, end the latest close
    assert crestones.typical_window[0] <= "04-15"
    assert crestones.typical_window[1] >= "05-31"


# --- effort / fatigue ------------------------------------------------------ #
def test_low_angle_objective_gets_splitboard_penalty():
    em = EffortModel()
    sherman = OBJ["sherman"]          # 30 deg, rolling -> penalized
    capitol = OBJ["capitol"]          # 50 deg -> no penalty
    assert em.splitboard_multiplier(sherman) == em.splitboard_penalty
    assert em.splitboard_multiplier(capitol) == 1.0


def test_fatigue_cost_scales_with_vert_and_tier():
    em = EffortModel()
    capitol = OBJ["capitol"]          # big vert, CRUX
    sherman = OBJ["sherman"]          # small, routine
    assert em.fatigue_cost(capitol) > em.fatigue_cost(sherman)
    assert em.fatigue_cost(capitol) > 0


def test_rolling_window_drops_old_load():
    ft = FatigueTracker(window_days=3, cap=24.0)
    d0 = date(2017, 4, 1)
    ft.record(d0, 10.0)
    assert ft.rolling_load(d0) == 10.0
    # Four days later the d0 load has slid out of the 3-day window.
    assert ft.rolling_load(date(2017, 4, 4)) == 0.0


def test_cap_blocks_back_to_back_big_days():
    em = EffortModel()
    ft = FatigueTracker(window_days=3, cap=20.0)
    capitol = OBJ["capitol"]
    cost = em.fatigue_cost(capitol)
    d0 = date(2017, 5, 1)
    ft.record(d0, cost)
    # A second Capitol-class push the very next day should breach the cap.
    assert ft.would_exceed(cost, date(2017, 5, 2)) is True
    # After two rest days it is allowed again.
    ft.rest(date(2017, 5, 2))
    ft.rest(date(2017, 5, 3))
    assert ft.would_exceed(cost, date(2017, 5, 4)) is False


def test_multiday_basecamp_spreads_daily_cost_under_cap():
    em = EffortModel()
    chicago = OBJ["chicago_basin"]    # 3-day basecamp, large total
    assert chicago.push_days == 3
    # Total may exceed a single-day budget, but the per-day load must be tractable.
    assert em.daily_fatigue_cost(chicago) * chicago.push_days == em.fatigue_cost(chicago)
    ft = FatigueTracker(window_days=3, cap=24.0)
    assert not ft.would_exceed(em.daily_fatigue_cost(chicago), date(2017, 4, 20))


def test_rest_day_adds_no_load():
    ft = FatigueTracker()
    d = date(2017, 4, 10)
    ft.rest(d)
    assert ft.rolling_load(d) == 0.0
