"""Conditions data contracts + adapters for the 14er FKT simulator.

This module defines the typed records that the go/no-go engine consumes, the
``ConditionsProvider`` interface that supplies them, and concrete providers:

* ``SyntheticProvider``  -- deterministic offline season generator (the default
  for development; see Section 11 of the build prompt).
* ``CaicForecastAdapter`` / ``AvalancheExplorerAdapter`` / ``SnotelAdapter``
  -- real-data adapter SKELETONS behind the same interface, wired in Phase 8.

Provenance rules (Section 7) are baked into the contracts:

* Every conditions record carries ``source`` (where it came from) and
  ``retrieved_at`` (when). The report can therefore state, per peak-day, the
  origin of each go/no-go input.
* Fidelity degrades with age. A forecast whose aspect/elevation rose is missing
  carries ``degraded=True`` and falls back to the overall danger rating.
* Missing data is **UNKNOWN, not GO**. ``DayConditions.is_unknown`` is True when
  no forecast exists; the go/no-go layer treats UNKNOWN as NO-GO for tier-2
  peaks and as a quality-penalized maybe for tier 0-1 only when bracketed by
  safe days.
"""
from __future__ import annotations

import abc
import hashlib
import random
from datetime import date, datetime, timedelta, timezone
from enum import Enum, IntEnum
from typing import ClassVar

from pydantic import BaseModel, Field

from fkt_sim.peaks import Aspect, Line

# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class DangerLevel(IntEnum):
    """North American Avalanche Danger Scale (plus UNKNOWN)."""
    UNKNOWN = 0
    LOW = 1
    MODERATE = 2
    CONSIDERABLE = 3
    HIGH = 4
    EXTREME = 5


class ElevationBand(str, Enum):
    BTL = "BTL"   # below treeline
    NTL = "NTL"   # near treeline
    ATL = "ATL"   # above treeline


class AvyProblemType(str, Enum):
    LOOSE_DRY = "loose_dry"
    LOOSE_WET = "loose_wet"
    STORM_SLAB = "storm_slab"
    WIND_SLAB = "wind_slab"
    PERSISTENT_SLAB = "persistent_slab"
    DEEP_PERSISTENT_SLAB = "deep_persistent_slab"
    WET_SLAB = "wet_slab"
    CORNICE = "cornice"
    GLIDE = "glide"


# Problem types that trigger the persistent-slab override (Section 5, veto 3).
PERSISTENT_PROBLEMS = frozenset(
    {AvyProblemType.PERSISTENT_SLAB, AvyProblemType.DEEP_PERSISTENT_SLAB}
)


class AvySize(IntEnum):
    """Destructive size (D-scale). Obs veto threshold is D2."""
    D1 = 1
    D2 = 2
    D3 = 3
    D4 = 4
    D5 = 5


class Trigger(str, Enum):
    NATURAL = "natural"
    HUMAN = "human"
    EXPLOSIVE = "explosive"
    UNKNOWN = "unknown"


class DataSource(str, Enum):
    CAIC = "caic"                    # source of truth
    AVALANCHE_EXPLORER = "caic_avy_explorer"
    SNOTEL = "snotel"
    CAIC_WX_STATION = "caic_wx_station"
    AVYAPI = "avyapi"                # acceptable third-party secondary
    SYNTHETIC = "synthetic"          # generated fixture (never real)


# Colorado treeline is ~11,400 ft; 14er ski lines are almost all above it.
_TREELINE_FT = 11_400
_NEAR_TREELINE_FT = 12_000


def elevation_band_for(elev_ft: int) -> ElevationBand:
    """Classify an elevation into a CAIC danger-rose band."""
    if elev_ft < _TREELINE_FT:
        return ElevationBand.BTL
    if elev_ft < _NEAR_TREELINE_FT:
        return ElevationBand.NTL
    return ElevationBand.ATL


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
class ProvenancedRecord(BaseModel):
    """Mixin: every conditions datum says where and when it came from."""
    source: DataSource
    retrieved_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class AvalancheProblem(BaseModel):
    problem_type: AvyProblemType
    aspects: list[Aspect] = Field(default_factory=list)
    elevation_bands: list[ElevationBand] = Field(default_factory=list)
    likelihood: str = ""           # e.g. "possible", "likely"
    size_min: AvySize = AvySize.D1
    size_max: AvySize = AvySize.D2

    def affects(self, aspect: Aspect, band: ElevationBand) -> bool:
        a_ok = (not self.aspects) or (aspect in self.aspects)
        b_ok = (not self.elevation_bands) or (band in self.elevation_bands)
        return a_ok and b_ok


class DangerRose(BaseModel):
    """Aspect/elevation danger rose: {band: {aspect: level}}.

    When a rose is unavailable for a (zone, date) the forecast holds
    ``rose=None`` and callers fall back to ``overall`` with ``degraded=True``.
    """
    grid: dict[ElevationBand, dict[Aspect, DangerLevel]] = Field(default_factory=dict)

    def rating(self, aspect: Aspect, band: ElevationBand) -> DangerLevel:
        return self.grid.get(band, {}).get(aspect, DangerLevel.UNKNOWN)

    def rating_for_line(self, line: Line, band: ElevationBand) -> DangerLevel:
        """Worst rating across all aspects the line spans, at its band."""
        levels = [self.rating(a, band) for a in line.all_aspects]
        levels = [lv for lv in levels if lv != DangerLevel.UNKNOWN]
        return max(levels) if levels else DangerLevel.UNKNOWN


class CaicForecast(ProvenancedRecord):
    zone: str
    date: date
    overall_danger: DangerLevel
    rose: DangerRose | None = None
    problems: list[AvalancheProblem] = Field(default_factory=list)
    discussion: str = ""
    degraded: bool = False          # True when the rose/problem detail is missing

    def danger_for_line(self, line: Line, band: ElevationBand) -> tuple[DangerLevel, bool]:
        """Return (danger_level, used_degraded_fallback) for a line at a band.

        Uses the rose when present; otherwise falls back to the overall rating
        and reports degraded=True so the verdict can be flagged.
        """
        if self.rose is not None:
            lvl = self.rose.rating_for_line(line, band)
            if lvl != DangerLevel.UNKNOWN:
                return lvl, self.degraded
        # No rose, or rose has no cell for this line: fall back to overall.
        return self.overall_danger, True

    def persistent_problem_on(self, line: Line, band: ElevationBand) -> AvalancheProblem | None:
        for p in self.problems:
            if p.problem_type in PERSISTENT_PROBLEMS and any(
                p.affects(a, band) for a in line.all_aspects
            ):
                return p
        return None


class AvalancheObs(ProvenancedRecord):
    """An observed avalanche (CAIC Avalanche Explorer record)."""
    date: date
    zone: str
    aspect: Aspect
    elevation_band: ElevationBand
    size: AvySize
    trigger: Trigger = Trigger.UNKNOWN
    comments: str = ""


class StationWeather(ProvenancedRecord):
    """Daily weather/snowpack summary from the nearest SNOTEL / CAIC station."""
    date: date
    zone: str
    station_id: str
    temp_min_f: float
    temp_max_f: float
    new_snow_24h_in: float
    snow_depth_in: float
    wind_speed_mph: float
    wind_gust_mph: float

    # Storm thresholds (ClassVar: shared constants, not per-record fields).
    STORM_NEW_SNOW_IN: ClassVar[float] = 4.0
    STORM_WIND_MPH: ClassVar[float] = 30.0
    THAW_F: ClassVar[float] = 34.0

    @property
    def overnight_freeze(self) -> bool:
        return self.temp_min_f <= 32.0

    @property
    def daytime_thaw(self) -> bool:
        return self.temp_max_f >= self.THAW_F

    @property
    def active_storm(self) -> bool:
        return (
            self.new_snow_24h_in >= self.STORM_NEW_SNOW_IN
            or self.wind_speed_mph >= self.STORM_WIND_MPH
        )

    @property
    def corn_cycle_ok(self) -> bool:
        """Classic corn signal: overnight refreeze + daytime softening."""
        return self.overnight_freeze and self.daytime_thaw


class DayConditions(BaseModel):
    """Everything the go/no-go function needs for one (zone, date)."""
    zone: str
    date: date
    forecast: CaicForecast | None = None
    observations: list[AvalancheObs] = Field(default_factory=list)
    weather: StationWeather | None = None

    @property
    def is_unknown(self) -> bool:
        """No forecast => UNKNOWN day (NOT GO). Section 7."""
        return self.forecast is None

    def recent_obs_on(
        self, line: Line, band: ElevationBand, min_size: AvySize = AvySize.D2
    ) -> list[AvalancheObs]:
        """Observations on the line's aspect band in this zone, size >= min_size."""
        aspects = set(line.all_aspects)
        return [
            o for o in self.observations
            if o.aspect in aspects
            and o.elevation_band == band
            and o.size >= min_size
        ]


# --------------------------------------------------------------------------- #
# Provider interface
# --------------------------------------------------------------------------- #
class ConditionsProvider(abc.ABC):
    """Supplies conditions for a (zone, date). Swappable: synthetic or real."""

    @abc.abstractmethod
    def forecast(self, zone: str, day: date) -> CaicForecast | None: ...

    @abc.abstractmethod
    def observations(self, zone: str, day: date, lookback_days: int = 3) -> list[AvalancheObs]: ...

    @abc.abstractmethod
    def weather(self, zone: str, day: date) -> StationWeather | None: ...

    def day_conditions(
        self, zone: str, day: date, lookback_days: int = 3
    ) -> DayConditions:
        """Bundle the three feeds into one DayConditions record."""
        return DayConditions(
            zone=zone,
            date=day,
            forecast=self.forecast(zone, day),
            observations=self.observations(zone, day, lookback_days),
            weather=self.weather(zone, day),
        )


# --------------------------------------------------------------------------- #
# Synthetic provider (deterministic offline season)
# --------------------------------------------------------------------------- #
class SeasonCharacter(str, Enum):
    """Classify a season by its dominant CAIC problem regime (Section 9)."""
    CONTINENTAL_PWL = "continental_pwl"   # persistent/deep-slab; slow, dangerous, late corn
    NEAR_NORMAL = "near_normal"
    STABLE_EARLY = "stable_early"         # fast-consolidating; lean coverage, rockout risk


# Shady, PWL-prone aspects in Colorado's continental snowpack.
_SHADY_ASPECTS = (Aspect.N, Aspect.NE, Aspect.E, Aspect.NW)
_ALL_ASPECTS = tuple(Aspect)


class SyntheticProvider(ConditionsProvider):
    """Generate a plausible, fully-deterministic season offline.

    The arc: danger is elevated in mid-winter and trends down toward spring;
    periodic storms spike danger and load new snow; the spring corn cycle
    (overnight freeze + daytime thaw) sets in late. Season character shifts the
    regime:

    * CONTINENTAL_PWL keeps persistent/deep-persistent slabs alive on shady
      upper aspects well into spring and compresses the safe corn window.
    * STABLE_EARLY clears fast (low danger by March) but runs shallow snow
      depth -> coverage/rockout vetoes.
    * NEAR_NORMAL sits between.

    Determinism: every (zone, date) draw is seeded from a hash of
    (master_seed, zone, date), so the same inputs always yield the same data
    and tests are reproducible. A configurable ``degraded_before`` date marks
    older forecasts as rose-less to exercise the degraded-data fallback.
    """

    def __init__(
        self,
        character: SeasonCharacter = SeasonCharacter.NEAR_NORMAL,
        *,
        start: date = date(2016, 12, 15),
        end: date = date(2017, 6, 15),
        seed: int = 1453,
        degraded_before: date | None = None,
        unknown_zone_days: frozenset[tuple[str, str]] = frozenset(),
    ) -> None:
        self.character = character
        self.start = start
        self.end = end
        self.seed = seed
        # Forecasts before this date have no rose (older = lower fidelity).
        self.degraded_before = degraded_before
        # Explicit (zone, isodate) pairs to leave as UNKNOWN (missing forecast).
        self.unknown_zone_days = unknown_zone_days

    # -- deterministic RNG -------------------------------------------------- #
    def _rng(self, zone: str, day: date, salt: str = "") -> random.Random:
        key = f"{self.seed}|{zone}|{day.isoformat()}|{salt}"
        h = hashlib.sha256(key.encode()).hexdigest()
        return random.Random(int(h[:16], 16))

    def _season_progress(self, day: date) -> float:
        """0.0 at season start, 1.0 at season end (clamped)."""
        span = (self.end - self.start).days or 1
        return max(0.0, min(1.0, (day - self.start).days / span))

    def _is_storm(self, zone: str, day: date) -> bool:
        # Storms cluster ~every 7-10 days; more frequent mid-winter.
        rng = self._rng(zone, day, "storm")
        prog = self._season_progress(day)
        base = 0.18 - 0.08 * prog  # storms taper toward spring
        if self.character == SeasonCharacter.STABLE_EARLY:
            base *= 0.6
        return rng.random() < base

    # -- baseline danger arc ------------------------------------------------ #
    def _base_overall(self, zone: str, day: date) -> DangerLevel:
        prog = self._season_progress(day)
        rng = self._rng(zone, day, "danger")
        # Mid-winter CONSIDERABLE-ish, stabilizing toward LOW by late spring so a
        # realistic (rare) corn window opens for steep CRUX lines. The PWL year
        # stays dangerous much longer -- its late, compressed window is the point.
        if self.character == SeasonCharacter.STABLE_EARLY:
            base = 2.6 - 1.8 * prog          # reaches LOW readily late
        elif self.character == SeasonCharacter.CONTINENTAL_PWL:
            base = 3.1 - 1.2 * prog          # still elevated into spring
        else:
            base = 2.9 - 1.7 * prog
        if self._is_storm(zone, day):
            base += 1.2                       # storm-day spike
        base += rng.uniform(-0.4, 0.4)
        return DangerLevel(int(max(1, min(4, round(base)))))

    # -- ConditionsProvider API -------------------------------------------- #
    def forecast(self, zone: str, day: date) -> CaicForecast | None:
        if (zone, day.isoformat()) in self.unknown_zone_days:
            return None  # genuinely missing -> UNKNOWN day
        if not (self.start <= day <= self.end):
            return None

        overall = self._base_overall(zone, day)
        degraded = self.degraded_before is not None and day < self.degraded_before

        rose = None if degraded else self._build_rose(zone, day, overall)
        problems = self._build_problems(zone, day, overall)
        return CaicForecast(
            source=DataSource.SYNTHETIC,
            zone=zone,
            date=day,
            overall_danger=overall,
            rose=rose,
            problems=problems,
            discussion=self._discussion(problems, overall),
            degraded=degraded,
        )

    def _build_rose(self, zone: str, day: date, overall: DangerLevel) -> DangerRose:
        rng = self._rng(zone, day, "rose")
        prog = self._season_progress(day)
        pwl_alive = self.character == SeasonCharacter.CONTINENTAL_PWL and prog < 0.85
        grid: dict[ElevationBand, dict[Aspect, DangerLevel]] = {}
        for band in ElevationBand:
            band_bump = {ElevationBand.BTL: -1, ElevationBand.NTL: 0, ElevationBand.ATL: 1}[band]
            cells: dict[Aspect, DangerLevel] = {}
            for asp in _ALL_ASPECTS:
                v = int(overall) + band_bump
                # Shady aspects run hotter, especially in a PWL year up high.
                if asp in _SHADY_ASPECTS:
                    v += 1 if (pwl_alive and band == ElevationBand.ATL) else 0
                # Sunny aspects calm down as spring stabilizes.
                if asp in (Aspect.S, Aspect.SW, Aspect.SE) and prog > 0.6:
                    v -= 1
                v += rng.choice((-1, 0, 0, 0))
                cells[asp] = DangerLevel(max(1, min(5, v)))
            grid[band] = cells
        return DangerRose(grid=grid)

    def _build_problems(
        self, zone: str, day: date, overall: DangerLevel
    ) -> list[AvalancheProblem]:
        rng = self._rng(zone, day, "problems")
        prog = self._season_progress(day)
        problems: list[AvalancheProblem] = []

        if self._is_storm(zone, day):
            problems.append(AvalancheProblem(
                problem_type=AvyProblemType.STORM_SLAB,
                aspects=list(_ALL_ASPECTS),
                elevation_bands=[ElevationBand.NTL, ElevationBand.ATL],
                likelihood="likely",
                size_min=AvySize.D1, size_max=AvySize.D2,
            ))
            if rng.random() < 0.6:
                problems.append(AvalancheProblem(
                    problem_type=AvyProblemType.WIND_SLAB,
                    aspects=[Aspect.N, Aspect.NE, Aspect.E, Aspect.SE],
                    elevation_bands=[ElevationBand.ATL],
                    likelihood="possible",
                    size_min=AvySize.D1, size_max=AvySize.D2,
                ))

        # Persistent weak layer regime.
        pwl_prob = {
            SeasonCharacter.CONTINENTAL_PWL: 0.7 - 0.5 * prog,
            SeasonCharacter.NEAR_NORMAL: 0.35 - 0.3 * prog,
            SeasonCharacter.STABLE_EARLY: 0.15 - 0.13 * prog,
        }[self.character]
        if rng.random() < max(0.0, pwl_prob):
            deep = self.character == SeasonCharacter.CONTINENTAL_PWL and rng.random() < 0.5
            problems.append(AvalancheProblem(
                problem_type=(AvyProblemType.DEEP_PERSISTENT_SLAB if deep
                              else AvyProblemType.PERSISTENT_SLAB),
                aspects=list(_SHADY_ASPECTS),
                elevation_bands=[ElevationBand.NTL, ElevationBand.ATL],
                likelihood="possible",
                size_min=AvySize.D2, size_max=(AvySize.D3 if deep else AvySize.D2),
            ))

        # Spring wet problems on sunny aspects.
        if prog > 0.55 and rng.random() < 0.4:
            problems.append(AvalancheProblem(
                problem_type=(AvyProblemType.WET_SLAB if rng.random() < 0.4
                              else AvyProblemType.LOOSE_WET),
                aspects=[Aspect.S, Aspect.SE, Aspect.SW, Aspect.E],
                elevation_bands=[ElevationBand.BTL, ElevationBand.NTL],
                likelihood="possible",
                size_min=AvySize.D1, size_max=AvySize.D2,
            ))
        return problems

    @staticmethod
    def _discussion(problems: list[AvalancheProblem], overall: DangerLevel) -> str:
        names = ", ".join(sorted({p.problem_type.value for p in problems})) or "none"
        return f"[synthetic] overall={overall.name}; problems={names}"

    def observations(
        self, zone: str, day: date, lookback_days: int = 3
    ) -> list[AvalancheObs]:
        """Observed slides over the trailing ``lookback_days`` (inclusive)."""
        out: list[AvalancheObs] = []
        for back in range(lookback_days + 1):
            d = day - timedelta(days=back)
            if not (self.start <= d <= self.end):
                continue
            out.extend(self._obs_for(zone, d))
        return out

    def _obs_for(self, zone: str, day: date) -> list[AvalancheObs]:
        rng = self._rng(zone, day, "obs")
        fc = self.forecast(zone, day)
        if fc is None:
            return []
        # More observed activity when danger is high or a storm just hit.
        p_activity = 0.05 + 0.18 * max(0, int(fc.overall_danger) - 2)
        if self._is_storm(zone, day):
            p_activity += 0.25
        out: list[AvalancheObs] = []
        n = 0
        while rng.random() < p_activity and n < 3:
            n += 1
            # Slides favor the aspects/bands the problems point at.
            asp = rng.choice(_SHADY_ASPECTS if fc.problems and any(
                pr.problem_type in PERSISTENT_PROBLEMS for pr in fc.problems
            ) else _ALL_ASPECTS)
            size = AvySize(rng.choices([1, 2, 3], weights=[3, 3, 1])[0])
            out.append(AvalancheObs(
                source=DataSource.SYNTHETIC,
                date=day,
                zone=zone,
                aspect=asp,
                elevation_band=rng.choice([ElevationBand.NTL, ElevationBand.ATL]),
                size=size,
                trigger=rng.choice([Trigger.NATURAL, Trigger.HUMAN]),
                comments="[synthetic] observed slide",
            ))
        return out

    def weather(self, zone: str, day: date) -> StationWeather | None:
        if not (self.start <= day <= self.end):
            return None
        rng = self._rng(zone, day, "wx")
        prog = self._season_progress(day)
        storm = self._is_storm(zone, day)

        # Temperature climbs through the season; nights below freezing reliably
        # except in deep mid-winter / storm warm-ups.
        base_max = 20 + 35 * prog + rng.uniform(-6, 6)
        base_min = base_max - rng.uniform(12, 22)
        if storm:
            base_max -= rng.uniform(4, 10)     # storms cool the day
            base_min += rng.uniform(0, 4)      # and cloud-insulate the night

        new_snow = 0.0
        if storm:
            new_snow = round(rng.uniform(4, 16), 1)
        elif rng.random() < 0.2:
            new_snow = round(rng.uniform(0.5, 3.5), 1)

        # Snowpack depth: builds to a peak ~70% through, then melts. Lean in a
        # STABLE_EARLY year (rockout risk).
        peak_depth = {
            SeasonCharacter.CONTINENTAL_PWL: 95,
            SeasonCharacter.NEAR_NORMAL: 80,
            SeasonCharacter.STABLE_EARLY: 52,
        }[self.character]
        arc = 1.0 - abs(prog - 0.70) / 0.70      # triangular peak at prog=0.70
        depth = max(0.0, peak_depth * max(0.0, arc) + rng.uniform(-6, 6))

        wind = rng.uniform(5, 18) + (rng.uniform(10, 25) if storm else 0)
        return StationWeather(
            source=DataSource.SYNTHETIC,
            date=day,
            zone=zone,
            station_id=f"SYN-{zone}",
            temp_min_f=round(base_min, 1),
            temp_max_f=round(base_max, 1),
            new_snow_24h_in=new_snow,
            snow_depth_in=round(depth, 1),
            wind_speed_mph=round(wind, 1),
            wind_gust_mph=round(wind * rng.uniform(1.3, 1.8), 1),
        )

    # convenience ----------------------------------------------------------- #
    def iter_days(self):
        d = self.start
        while d <= self.end:
            yield d
            d += timedelta(days=1)


# --------------------------------------------------------------------------- #
# Real-data adapter skeletons (wired in Phase 8)
# --------------------------------------------------------------------------- #
class _NotWiredYet(NotImplementedError):
    """Raised by real adapters until Phase 8 wires the HTTP/parse layer."""


class CaicForecastAdapter(ConditionsProvider):
    """CAIC historical zone forecasts (source of truth).

    Phase 8 implements: fetch the archived forecast for (zone, date) from CAIC's
    date+zone-addressable URLs, parse the danger rose / problems / discussion,
    and cache as JSON per (zone, date) under ``data/cache/``. Older seasons lack
    the rose -> set ``degraded=True``. ``avyapi.com`` may back-fill as a
    secondary, recorded via ``source=AVYAPI``.
    """

    def __init__(self, cache_dir: str = "data/cache", allow_avyapi_fallback: bool = False):
        self.cache_dir = cache_dir
        self.allow_avyapi_fallback = allow_avyapi_fallback

    def forecast(self, zone: str, day: date) -> CaicForecast | None:
        raise _NotWiredYet("CAIC forecast adapter is wired in Phase 8")

    def observations(self, zone, day, lookback_days=3):
        raise _NotWiredYet("use AvalancheExplorerAdapter for observations")

    def weather(self, zone, day):
        raise _NotWiredYet("use SnotelAdapter for weather")


class AvalancheExplorerAdapter(ConditionsProvider):
    """CAIC Avalanche Explorer observed-avalanche feed (ground-truth veto)."""

    def forecast(self, zone, day):
        raise _NotWiredYet("use CaicForecastAdapter for forecasts")

    def observations(self, zone: str, day: date, lookback_days: int = 3) -> list[AvalancheObs]:
        raise _NotWiredYet("Avalanche Explorer adapter is wired in Phase 8")

    def weather(self, zone, day):
        raise _NotWiredYet("use SnotelAdapter for weather")


class SnotelAdapter(ConditionsProvider):
    """SNOTEL / CAIC weather-station feed (temp, wind, new snow, snow height)."""

    def forecast(self, zone, day):
        raise _NotWiredYet("use CaicForecastAdapter for forecasts")

    def observations(self, zone, day, lookback_days=3):
        raise _NotWiredYet("use AvalancheExplorerAdapter for observations")

    def weather(self, zone: str, day: date) -> StationWeather | None:
        raise _NotWiredYet("SNOTEL adapter is wired in Phase 8")


class CompositeProvider(ConditionsProvider):
    """Stitch separate real feeds into one provider (forecast/obs/weather)."""

    def __init__(
        self,
        forecast_src: ConditionsProvider,
        obs_src: ConditionsProvider,
        weather_src: ConditionsProvider,
    ) -> None:
        self._f, self._o, self._w = forecast_src, obs_src, weather_src

    def forecast(self, zone, day):
        return self._f.forecast(zone, day)

    def observations(self, zone, day, lookback_days=3):
        return self._o.observations(zone, day, lookback_days)

    def weather(self, zone, day):
        return self._w.weather(zone, day)
