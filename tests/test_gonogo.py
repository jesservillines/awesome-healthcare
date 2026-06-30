"""Phase-3 tests: the go/no-go engine, one test per veto path + degraded data."""
from __future__ import annotations

from datetime import date, datetime, timezone

from fkt_sim.conditions import (
    AvalancheObs,
    AvalancheProblem,
    AvyProblemType,
    AvySize,
    CaicForecast,
    DangerLevel,
    DangerRose,
    DataSource,
    DayConditions,
    ElevationBand,
    StationWeather,
    Trigger,
)
from fkt_sim.gonogo import (
    Decision,
    GoNoGoConfig,
    VetoCode,
    Verdict,
    evaluate_line,
    strictest,
)
from fkt_sim.peaks import Aspect, GatekeeperTier, Line, Peak

ATL = ElevationBand.ATL
NTL = ElevationBand.NTL
NOW = datetime(2017, 4, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def make_peak(tier=GatekeeperTier.ROUTINE, aspect=Aspect.E, angle=35,
              summit_continuous=True, verified=False, secondary=None):
    line = Line(
        name="Test Line", primary_aspect=aspect,
        secondary_aspects=secondary or [], max_angle_deg=angle,
        summit_continuous=summit_continuous, verified=verified,
    )
    peak = Peak(
        name="Test Peak", elevation_ft=14000, range_name="Sawatch",
        caic_zone="sawatch", trailhead_id="x", cluster_id="x", lines=[line],
        tier=tier, typical_window=("03-01", "05-31"),
        approach_miles_rt=8.0, vert_ft=4000,
    )
    return peak, line


def uniform_rose(level: DangerLevel) -> DangerRose:
    grid = {b: {a: level for a in Aspect} for b in ElevationBand}
    return DangerRose(grid=grid)


def make_forecast(overall=DangerLevel.MODERATE, rose_level=None, problems=None,
                  degraded=False):
    rose = None if (degraded or rose_level is None) else uniform_rose(rose_level)
    return CaicForecast(
        source=DataSource.SYNTHETIC, zone="sawatch", date=date(2017, 4, 1),
        overall_danger=overall, rose=rose, problems=problems or [],
        degraded=degraded, retrieved_at=NOW,
    )


def make_weather(depth=80.0, new_snow=0.0, wind=8.0, tmin=24.0, tmax=42.0):
    return StationWeather(
        source=DataSource.SYNTHETIC, zone="sawatch", date=date(2017, 4, 1),
        station_id="SYN", temp_min_f=tmin, temp_max_f=tmax,
        new_snow_24h_in=new_snow, snow_depth_in=depth,
        wind_speed_mph=wind, wind_gust_mph=wind * 1.5, retrieved_at=NOW,
    )


def make_obs(aspect=Aspect.E, size=AvySize.D2, band=ATL):
    return AvalancheObs(
        source=DataSource.SYNTHETIC, date=date(2017, 4, 1), zone="sawatch",
        aspect=aspect, elevation_band=band, size=size, trigger=Trigger.NATURAL,
        retrieved_at=NOW,
    )


def day(forecast=None, obs=None, weather=None, d=date(2017, 4, 1)):
    return DayConditions(zone="sawatch", date=d, forecast=forecast,
                         observations=obs or [], weather=weather)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_clean_day_is_go():
    peak, line = make_peak(tier=GatekeeperTier.ROUTINE, aspect=Aspect.E)
    dc = day(make_forecast(rose_level=DangerLevel.MODERATE), weather=make_weather())
    v = evaluate_line(line, peak, dc)
    assert v.decision == Decision.GO
    assert v.quality > 0
    assert v.veto_codes == []
    assert v.line_verified is False  # seed line


# --------------------------------------------------------------------------- #
# Veto 1: danger rating vs tier threshold
# --------------------------------------------------------------------------- #
def test_danger_above_threshold_vetoes():
    peak, line = make_peak(tier=GatekeeperTier.MODERATE)
    dc = day(make_forecast(rose_level=DangerLevel.CONSIDERABLE), weather=make_weather())
    v = evaluate_line(line, peak, dc)
    assert v.decision == Decision.NO_GO
    assert VetoCode.DANGER_RATING in v.veto_codes


def test_crux_threshold_is_stricter_than_routine():
    # CONSIDERABLE is NO_GO for everyone; MODERATE is GO for tier1 but NO_GO for tier2.
    dc = day(make_forecast(rose_level=DangerLevel.MODERATE), weather=make_weather())
    p1, l1 = make_peak(tier=GatekeeperTier.MODERATE, aspect=Aspect.W)  # non-corn aspect
    p2, l2 = make_peak(tier=GatekeeperTier.CRUX, aspect=Aspect.W)
    assert evaluate_line(l1, p1, dc).decision == Decision.GO
    v2 = evaluate_line(l2, p2, dc)
    assert v2.decision == Decision.NO_GO
    assert VetoCode.DANGER_RATING in v2.veto_codes


def test_rose_worst_aspect_governs():
    # Line wraps E (safe) + N (dangerous); the N cell must veto.
    grid = {b: {a: DangerLevel.LOW for a in Aspect} for b in ElevationBand}
    grid[ATL][Aspect.N] = DangerLevel.CONSIDERABLE
    fc = CaicForecast(source=DataSource.SYNTHETIC, zone="sawatch",
                      date=date(2017, 4, 1), overall_danger=DangerLevel.LOW,
                      rose=DangerRose(grid=grid), retrieved_at=NOW)
    peak, line = make_peak(tier=GatekeeperTier.MODERATE, aspect=Aspect.E,
                           secondary=[Aspect.N])
    v = evaluate_line(line, peak, day(fc, weather=make_weather()))
    assert v.decision == Decision.NO_GO and VetoCode.DANGER_RATING in v.veto_codes


# --------------------------------------------------------------------------- #
# Veto 2: observed activity
# --------------------------------------------------------------------------- #
def test_on_aspect_d2_observation_vetoes_despite_low_rating():
    peak, line = make_peak(tier=GatekeeperTier.MODERATE, aspect=Aspect.E)
    dc = day(make_forecast(rose_level=DangerLevel.LOW),
             obs=[make_obs(aspect=Aspect.E, size=AvySize.D2)],
             weather=make_weather())
    v = evaluate_line(line, peak, dc)
    assert v.decision == Decision.NO_GO
    assert VetoCode.OBSERVED_ACTIVITY in v.veto_codes


def test_off_aspect_or_small_observation_does_not_veto():
    peak, line = make_peak(tier=GatekeeperTier.MODERATE, aspect=Aspect.E)
    dc = day(make_forecast(rose_level=DangerLevel.LOW),
             obs=[make_obs(aspect=Aspect.W, size=AvySize.D3),    # wrong aspect
                  make_obs(aspect=Aspect.E, size=AvySize.D1)],   # too small
             weather=make_weather())
    assert evaluate_line(line, peak, dc).decision == Decision.GO


# --------------------------------------------------------------------------- #
# Veto 3: persistent-slab override
# --------------------------------------------------------------------------- #
def test_persistent_slab_forces_crux_threshold_on_routine_peak():
    pwl = AvalancheProblem(
        problem_type=AvyProblemType.PERSISTENT_SLAB,
        aspects=[Aspect.N, Aspect.NE, Aspect.E], elevation_bands=[ATL],
        size_min=AvySize.D2, size_max=AvySize.D3,
    )
    # MODERATE on a tier-0 peak would normally be GO; the PWL drops it to NO_GO.
    peak, line = make_peak(tier=GatekeeperTier.ROUTINE, aspect=Aspect.E)
    dc = day(make_forecast(rose_level=DangerLevel.MODERATE, problems=[pwl]),
             weather=make_weather())
    v = evaluate_line(line, peak, dc)
    assert v.decision == Decision.NO_GO
    codes = [r.code for r in v.reasons]
    assert VetoCode.PERSISTENT_SLAB in codes      # override recorded
    assert VetoCode.DANGER_RATING in v.veto_codes  # and it caused the veto


def test_persistent_slab_off_aspect_does_not_override():
    pwl = AvalancheProblem(
        problem_type=AvyProblemType.DEEP_PERSISTENT_SLAB,
        aspects=[Aspect.N], elevation_bands=[ATL],
        size_min=AvySize.D2, size_max=AvySize.D3,
    )
    peak, line = make_peak(tier=GatekeeperTier.ROUTINE, aspect=Aspect.S)
    dc = day(make_forecast(rose_level=DangerLevel.MODERATE, problems=[pwl]),
             weather=make_weather(tmin=20, tmax=45))  # corn ok
    v = evaluate_line(line, peak, dc)
    assert v.decision == Decision.GO


# --------------------------------------------------------------------------- #
# Veto 4: coverage / summit continuity
# --------------------------------------------------------------------------- #
def test_lean_coverage_vetoes():
    peak, line = make_peak(tier=GatekeeperTier.ROUTINE, aspect=Aspect.E)
    dc = day(make_forecast(rose_level=DangerLevel.LOW), weather=make_weather(depth=12))
    v = evaluate_line(line, peak, dc)
    assert v.decision == Decision.NO_GO and VetoCode.COVERAGE in v.veto_codes


def test_non_continuous_line_needs_fill_to_summit():
    # summit_continuous=False with merely adequate (not deep) coverage -> NO_GO.
    peak, line = make_peak(tier=GatekeeperTier.CRUX, aspect=Aspect.N,
                           summit_continuous=False)
    dc = day(make_forecast(rose_level=DangerLevel.LOW), weather=make_weather(depth=40))
    v = evaluate_line(line, peak, dc)
    assert v.decision == Decision.NO_GO and VetoCode.COVERAGE in v.veto_codes
    # With a deep fill, the same line clears the continuity veto.
    dc2 = day(make_forecast(rose_level=DangerLevel.LOW), weather=make_weather(depth=70))
    assert evaluate_line(line, peak, dc2).decision == Decision.GO


# --------------------------------------------------------------------------- #
# Veto 5: weather window (storm + corn freeze-thaw)
# --------------------------------------------------------------------------- #
def test_active_storm_vetoes():
    peak, line = make_peak(tier=GatekeeperTier.ROUTINE, aspect=Aspect.W)
    dc = day(make_forecast(rose_level=DangerLevel.LOW),
             weather=make_weather(new_snow=10))
    v = evaluate_line(line, peak, dc)
    assert v.decision == Decision.NO_GO and VetoCode.WEATHER_STORM in v.veto_codes


def test_corn_line_no_overnight_freeze_vetoes():
    peak, line = make_peak(tier=GatekeeperTier.MODERATE, aspect=Aspect.S)
    dc = day(make_forecast(rose_level=DangerLevel.LOW),
             weather=make_weather(tmin=38, tmax=50), d=date(2017, 5, 1))
    v = evaluate_line(line, peak, dc)
    assert v.decision == Decision.NO_GO
    assert VetoCode.NO_OVERNIGHT_FREEZE in v.veto_codes


def test_corn_line_frozen_solid_vetoes():
    peak, line = make_peak(tier=GatekeeperTier.MODERATE, aspect=Aspect.SE)
    dc = day(make_forecast(rose_level=DangerLevel.LOW),
             weather=make_weather(tmin=10, tmax=28), d=date(2017, 5, 1))
    v = evaluate_line(line, peak, dc)
    assert v.decision == Decision.NO_GO and VetoCode.FROZEN_SOLID in v.veto_codes


def test_corn_signal_not_required_before_april():
    # Same frozen-solid profile in March is fine (not corn season yet).
    peak, line = make_peak(tier=GatekeeperTier.MODERATE, aspect=Aspect.S)
    dc = day(make_forecast(rose_level=DangerLevel.LOW),
             weather=make_weather(tmin=10, tmax=28), d=date(2017, 3, 15))
    assert evaluate_line(line, peak, dc).decision == Decision.GO


# --------------------------------------------------------------------------- #
# UNKNOWN-data handling
# --------------------------------------------------------------------------- #
def test_missing_forecast_is_no_go_for_crux():
    peak, line = make_peak(tier=GatekeeperTier.CRUX)
    v = evaluate_line(line, peak, day(forecast=None, weather=make_weather()))
    assert v.decision == Decision.NO_GO and VetoCode.UNKNOWN_DATA in v.veto_codes


def test_missing_forecast_is_unknown_for_routine():
    peak, line = make_peak(tier=GatekeeperTier.ROUTINE)
    v = evaluate_line(line, peak, day(forecast=None, weather=make_weather()))
    assert v.decision == Decision.UNKNOWN


# --------------------------------------------------------------------------- #
# Degraded-data fallback
# --------------------------------------------------------------------------- #
def test_degraded_forecast_uses_overall_and_flags_degraded():
    # No rose; overall MODERATE. Tier-1 GO but flagged degraded.
    peak, line = make_peak(tier=GatekeeperTier.MODERATE, aspect=Aspect.W)
    fc = make_forecast(overall=DangerLevel.MODERATE, degraded=True)
    v = evaluate_line(line, peak, day(fc, weather=make_weather()))
    assert v.decision == Decision.GO and v.degraded is True


def test_degraded_forecast_still_vetoes_on_high_overall():
    peak, line = make_peak(tier=GatekeeperTier.MODERATE, aspect=Aspect.W)
    fc = make_forecast(overall=DangerLevel.HIGH, degraded=True)
    v = evaluate_line(line, peak, day(fc, weather=make_weather()))
    assert v.decision == Decision.NO_GO
    assert VetoCode.DANGER_RATING in v.veto_codes and v.degraded is True


# --------------------------------------------------------------------------- #
# Cluster resolution
# --------------------------------------------------------------------------- #
def test_strictest_no_go_binds_cluster():
    go = Verdict(decision=Decision.GO, peak="A", line="a", quality=0.8)
    bad = Verdict(decision=Decision.NO_GO, peak="B", line="b",
                  reasons=[], quality=0.0)
    combined = strictest([go, bad])
    assert combined.decision == Decision.NO_GO and combined.peak == "B"


def test_strictest_go_takes_min_quality_and_unions_flags():
    a = Verdict(decision=Decision.GO, peak="A", line="a", quality=0.8,
                degraded=False, line_verified=True)
    b = Verdict(decision=Decision.GO, peak="B", line="b", quality=0.4,
                degraded=True, line_verified=False)
    combined = strictest([a, b])
    assert combined.decision == Decision.GO
    assert combined.quality == 0.4
    assert combined.degraded is True and combined.line_verified is False


def test_strictest_unknown_when_no_nogo_but_some_unknown():
    a = Verdict(decision=Decision.GO, peak="A", line="a", quality=0.8)
    u = Verdict(decision=Decision.UNKNOWN, peak="U", line="u", quality=0.0)
    assert strictest([a, u]).decision == Decision.UNKNOWN
