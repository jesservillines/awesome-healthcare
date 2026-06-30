"""Phase-2 tests: conditions contracts + the synthetic season provider."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from fkt_sim.conditions import (
    AvySize,
    CaicForecast,
    DangerLevel,
    DataSource,
    DayConditions,
    ElevationBand,
    SeasonCharacter,
    SyntheticProvider,
    elevation_band_for,
)
from fkt_sim.peaks import Aspect, Line, load_peaks
from tests.fixtures.synthetic_season import (
    make_season,
    three_test_seasons,
    tiny_season,
    used_zone_ids,
)

ATL = ElevationBand.ATL


def _line(primary=Aspect.N, secondary=None, **kw):
    return Line(
        name="test", primary_aspect=primary,
        secondary_aspects=secondary or [], max_angle_deg=kw.get("angle", 40),
        summit_continuous=kw.get("cont", True),
    )


# --- contracts ------------------------------------------------------------- #
def test_elevation_band_thresholds():
    assert elevation_band_for(11000) == ElevationBand.BTL
    assert elevation_band_for(11800) == ElevationBand.NTL
    assert elevation_band_for(14000) == ElevationBand.ATL


def test_danger_rose_takes_worst_aspect():
    fc = make_season().forecast("sawatch", date(2017, 3, 10))
    assert fc is not None and fc.rose is not None
    line = _line(primary=Aspect.N, secondary=[Aspect.E])
    n = fc.rose.rating(Aspect.N, ATL)
    e = fc.rose.rating(Aspect.E, ATL)
    assert fc.rose.rating_for_line(line, ATL) == max(n, e)


def test_records_carry_provenance():
    prov = make_season()
    d = date(2017, 3, 10)
    fc = prov.forecast("sawatch", d)
    wx = prov.weather("sawatch", d)
    assert fc.source == DataSource.SYNTHETIC and isinstance(fc.retrieved_at, datetime)
    assert wx.source == DataSource.SYNTHETIC and isinstance(wx.retrieved_at, datetime)
    for o in prov.observations("sawatch", d):
        assert o.source == DataSource.SYNTHETIC


def test_station_weather_signals():
    fc_prov = make_season()
    # Scan the season for at least one corn-cycle day and one storm day.
    saw_corn = saw_storm = False
    for d in fc_prov.iter_days():
        wx = fc_prov.weather("sawatch", d)
        saw_corn |= wx.corn_cycle_ok
        saw_storm |= wx.active_storm
    assert saw_corn and saw_storm, "season should contain both corn and storm days"


# --- determinism ----------------------------------------------------------- #
def test_provider_is_deterministic():
    a = SyntheticProvider(SeasonCharacter.CONTINENTAL_PWL, seed=99)
    b = SyntheticProvider(SeasonCharacter.CONTINENTAL_PWL, seed=99)
    d = date(2017, 4, 1)
    fa, fb = a.forecast("aspen", d), b.forecast("aspen", d)
    assert fa.model_dump(exclude={"retrieved_at"}) == fb.model_dump(exclude={"retrieved_at"})
    assert (a.weather("aspen", d).model_dump(exclude={"retrieved_at"})
            == b.weather("aspen", d).model_dump(exclude={"retrieved_at"}))


def test_different_seeds_differ():
    a = SyntheticProvider(seed=1)
    b = SyntheticProvider(seed=2)
    # Over a month, the two seeds must diverge somewhere.
    days = [date(2017, 3, d) for d in range(1, 31)]
    diffs = sum(
        a.forecast("sawatch", d).overall_danger != b.forecast("sawatch", d).overall_danger
        for d in days
    )
    assert diffs > 0


# --- degraded-data fallback ------------------------------------------------ #
def test_degraded_before_drops_the_rose():
    prov = tiny_season()
    early = prov.forecast("sawatch", date(2017, 3, 2))   # before cutoff
    late = prov.forecast("sawatch", date(2017, 3, 10))   # after cutoff
    assert early.degraded and early.rose is None
    assert not late.degraded and late.rose is not None

    # Degraded forecast still answers danger_for_line, via the overall fallback.
    line = _line()
    lvl, used_fallback = early.danger_for_line(line, ATL)
    assert lvl == early.overall_danger and used_fallback is True


# --- UNKNOWN handling ------------------------------------------------------ #
def test_unknown_day_has_no_forecast():
    prov = SyntheticProvider(
        unknown_zone_days=frozenset({("sawatch", "2017-03-10")})
    )
    dc = prov.day_conditions("sawatch", date(2017, 3, 10))
    assert dc.is_unknown and dc.forecast is None
    # A normal day is known.
    assert not prov.day_conditions("sawatch", date(2017, 3, 11)).is_unknown


def test_out_of_window_is_unknown():
    prov = make_season(start=date(2017, 1, 1), end=date(2017, 1, 31))
    assert prov.forecast("sawatch", date(2016, 12, 1)) is None
    assert prov.weather("sawatch", date(2017, 2, 15)) is None


# --- observations veto feed ------------------------------------------------ #
def test_recent_obs_on_aspect_filters_by_size_and_aspect():
    prov = make_season()
    # Find a day in any zone with at least one D2+ observation, then check filter.
    for d in prov.iter_days():
        for z in used_zone_ids():
            obs = prov.observations(z, d)
            big = [o for o in obs if o.size >= AvySize.D2]
            if not big:
                continue
            target = big[0]
            line = _line(primary=target.aspect)
            dc = DayConditions(zone=z, date=d, observations=obs)
            hits = dc.recent_obs_on(line, target.elevation_band, AvySize.D2)
            assert all(o.aspect == target.aspect for o in hits)
            assert all(o.size >= AvySize.D2 for o in hits)
            return
    pytest.fail("no D2+ observations generated across the whole season")


# --- season character behaves as designed ---------------------------------- #
def test_pwl_year_keeps_persistent_problems_later_than_stable_year():
    seasons = three_test_seasons()
    pwl = seasons[SeasonCharacter.CONTINENTAL_PWL]
    stable = seasons[SeasonCharacter.STABLE_EARLY]

    def persistent_day_count(prov):
        n = 0
        for d in prov.iter_days():
            fc = prov.forecast("aspen", d)
            if fc and any(p.problem_type.value.endswith("persistent_slab")
                          for p in fc.problems):
                n += 1
        return n

    assert persistent_day_count(pwl) > persistent_day_count(stable)


def test_stable_year_runs_shallower_snowpack():
    seasons = three_test_seasons()
    pwl = seasons[SeasonCharacter.CONTINENTAL_PWL]
    stable = seasons[SeasonCharacter.STABLE_EARLY]
    peak_depth = lambda prov: max(
        prov.weather("sawatch", d).snow_depth_in for d in prov.iter_days()
    )
    assert peak_depth(stable) < peak_depth(pwl)


# --- real adapters are stubbed, not silently fake -------------------------- #
def test_real_adapters_raise_until_wired():
    from fkt_sim.conditions import (
        AvalancheExplorerAdapter,
        CaicForecastAdapter,
        SnotelAdapter,
    )
    with pytest.raises(NotImplementedError):
        CaicForecastAdapter().forecast("sawatch", date(2017, 3, 1))
    with pytest.raises(NotImplementedError):
        AvalancheExplorerAdapter().observations("sawatch", date(2017, 3, 1))
    with pytest.raises(NotImplementedError):
        SnotelAdapter().weather("sawatch", date(2017, 3, 1))


def test_day_conditions_bundles_all_feeds_for_real_peaks_zones():
    prov = make_season()
    zones = used_zone_ids()
    # Every zone the 55 peaks use must produce a complete DayConditions bundle.
    peak_zones = {p.caic_zone for p in load_peaks()}
    assert peak_zones <= set(zones)
    for z in zones:
        dc = prov.day_conditions(z, date(2017, 4, 15))
        assert isinstance(dc, DayConditions)
        assert dc.forecast is not None and dc.weather is not None
