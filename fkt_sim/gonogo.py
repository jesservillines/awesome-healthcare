"""The go/no-go decision engine -- the safety/skiability core (Section 5).

``evaluate_line`` returns a structured ``Verdict`` (GO / NO_GO / UNKNOWN with
reasons), never a bare bool, so the reasons feed the binding-constraint report.
A line is GO only if every hard veto passes:

1. Danger rating vs the aspect/elevation rose, thresholded by tier.
2. Observed-activity veto: an on-aspect D2+ slide in the zone within N days.
3. Persistent-slab override: a Persistent/Deep-Persistent problem on-aspect
   forces the strict tier-2 threshold even on a tier 0-1 peak.
4. Skiability / continuity: coverage to the true summit (this is what
   disqualifies non-continuous "skied from below the summit" descents).
5. Weather window: no active storm; for spring corn lines, require an overnight
   refreeze AND daytime softening.

Missing data is UNKNOWN, never silently GO: a tier-2 peak with no forecast is
NO_GO; a tier 0-1 peak is UNKNOWN (the scheduler may upgrade it to a
quality-penalized GO only when surrounding days bracket it safely).
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from fkt_sim.conditions import (
    AvySize,
    DangerLevel,
    DayConditions,
    ElevationBand,
    elevation_band_for,
)
from fkt_sim.peaks import Aspect, GatekeeperTier, Line, Peak

# Bands a 14er ski line meaningfully occupies for danger/observation matching.
_LINE_BANDS = (ElevationBand.NTL, ElevationBand.ATL)

# Aspects that ski as spring corn and therefore need the freeze-thaw signal.
CORN_ASPECTS = frozenset({Aspect.S, Aspect.SE, Aspect.SW, Aspect.E})


class Decision(str, Enum):
    GO = "GO"
    NO_GO = "NO_GO"
    UNKNOWN = "UNKNOWN"


class VetoCode(str, Enum):
    DANGER_RATING = "danger_rating"
    OBSERVED_ACTIVITY = "observed_activity"
    PERSISTENT_SLAB = "persistent_slab"
    COVERAGE = "coverage"
    WEATHER_STORM = "weather_storm"
    NO_OVERNIGHT_FREEZE = "no_overnight_freeze"
    FROZEN_SOLID = "frozen_solid"
    UNKNOWN_DATA = "unknown_data"
    BASECAMP_NO_WINDOW = "basecamp_no_window"  # a member had no GO day in the push


class Reason(BaseModel):
    code: VetoCode
    is_veto: bool          # True if this reason forces NO_GO
    detail: str


class GoNoGoConfig(BaseModel):
    """Tunable thresholds. Defaults follow Section 5."""
    # GO requires danger <= this level for the tier.
    danger_threshold_by_tier: dict[int, DangerLevel] = Field(
        default_factory=lambda: {
            0: DangerLevel.MODERATE,   # tier 0-1: MODERATE or below
            1: DangerLevel.MODERATE,
            2: DangerLevel.LOW,        # tier 2 (CRUX): LOW or below
        }
    )
    obs_lookback_days: int = 3
    obs_min_size: AvySize = AvySize.D2
    # Coverage (snow height proxy, inches): below this is a lean-year rockout.
    coverage_min_depth_in: float = 24.0
    # A non-summit-continuous line needs a deeper fill to reach the true summit.
    fill_depth_in: float = 55.0
    # Spring corn regime begins on/after this month (require freeze-thaw).
    corn_season_start_month: int = 4

    def threshold_for(self, tier: GatekeeperTier) -> DangerLevel:
        return self.danger_threshold_by_tier.get(int(tier), DangerLevel.MODERATE)


class Verdict(BaseModel):
    """Structured go/no-go outcome for one (line, peak, day)."""
    decision: Decision
    peak: str
    line: str
    reasons: list[Reason] = Field(default_factory=list)
    quality: float = 0.0          # 0..1 skiability/desirability when GO
    degraded: bool = False        # any input came from degraded/fallback data
    line_verified: bool = True    # False if the seed line is still unverified

    @property
    def is_go(self) -> bool:
        return self.decision == Decision.GO

    @property
    def veto_codes(self) -> list[VetoCode]:
        return [r.code for r in self.reasons if r.is_veto]


# --------------------------------------------------------------------------- #
# Core evaluation
# --------------------------------------------------------------------------- #
def _worst_danger(fc, line: Line) -> tuple[DangerLevel, bool]:
    """Worst danger across the bands the line occupies; flag degraded fallback."""
    worst = DangerLevel.UNKNOWN
    degraded = False
    for band in _LINE_BANDS:
        lvl, used_fallback = fc.danger_for_line(line, band)
        degraded = degraded or used_fallback
        if lvl != DangerLevel.UNKNOWN and lvl > worst:
            worst = lvl
    return worst, degraded


def evaluate_line(
    line: Line,
    peak: Peak,
    day: DayConditions,
    config: GoNoGoConfig | None = None,
) -> Verdict:
    """Return the structured go/no-go verdict for ``line`` on ``peak`` today."""
    cfg = config or GoNoGoConfig()
    reasons: list[Reason] = []
    degraded = False
    summit_band = elevation_band_for(peak.elevation_ft)  # ATL for all 14ers

    # ---- UNKNOWN data gate (Section 7) ----
    if day.is_unknown or day.forecast is None:
        if peak.tier == GatekeeperTier.CRUX:
            reasons.append(Reason(
                code=VetoCode.UNKNOWN_DATA, is_veto=True,
                detail="No forecast for this (zone, date); UNKNOWN is NO-GO for "
                       "a CRUX peak.",
            ))
            return _verdict(Decision.NO_GO, peak, line, reasons, 0.0, True)
        reasons.append(Reason(
            code=VetoCode.UNKNOWN_DATA, is_veto=False,
            detail="No forecast; UNKNOWN. Scheduler may allow a quality-penalized "
                   "GO only if surrounding days bracket it safely.",
        ))
        return _verdict(Decision.UNKNOWN, peak, line, reasons, 0.0, True)

    fc = day.forecast
    degraded = degraded or fc.degraded

    # ---- Veto 3 (evaluated first: it tightens the threshold for veto 1) ----
    threshold = cfg.threshold_for(peak.tier)
    pwl = fc.persistent_problem_on(line, summit_band)
    if pwl is None:
        # Also catch persistent problems pegged to the NTL band of the line.
        pwl = fc.persistent_problem_on(line, ElevationBand.NTL)
    if pwl is not None:
        strict = cfg.threshold_for(GatekeeperTier.CRUX)
        if strict < threshold:
            threshold = strict
        reasons.append(Reason(
            code=VetoCode.PERSISTENT_SLAB, is_veto=False,
            detail=f"{pwl.problem_type.value} on-aspect -> tier-2 threshold "
                   f"({threshold.name}) enforced regardless of peak tier.",
        ))

    # ---- Veto 1: danger rating vs aspect/elevation rose ----
    danger, used_fallback = _worst_danger(fc, line)
    degraded = degraded or used_fallback
    if danger == DangerLevel.UNKNOWN:
        # Rose present but no cell for this line and no overall fallback value.
        reasons.append(Reason(
            code=VetoCode.UNKNOWN_DATA, is_veto=(peak.tier == GatekeeperTier.CRUX),
            detail="Danger rose has no cell for this line and no overall fallback.",
        ))
        if peak.tier == GatekeeperTier.CRUX:
            return _verdict(Decision.NO_GO, peak, line, reasons, 0.0, True)
    elif danger > threshold:
        reasons.append(Reason(
            code=VetoCode.DANGER_RATING, is_veto=True,
            detail=f"Danger {danger.name} exceeds {threshold.name} threshold for "
                   f"tier {int(peak.tier)} on the line's aspect/elevation.",
        ))

    # ---- Veto 2: observed-activity ground truth ----
    big_obs = [
        o for o in day.observations
        if o.aspect in set(line.all_aspects)
        and o.size >= cfg.obs_min_size
        and o.elevation_band in _LINE_BANDS
    ]
    if big_obs:
        worst = max(big_obs, key=lambda o: o.size)
        reasons.append(Reason(
            code=VetoCode.OBSERVED_ACTIVITY, is_veto=True,
            detail=f"{len(big_obs)} recent on-aspect slide(s); largest "
                   f"{worst.size.name} ({worst.trigger.value}) overrides the rating.",
        ))

    # ---- Veto 4: skiability / summit continuity ----
    wx = day.weather
    if wx is None:
        degraded = True
        if not line.summit_continuous:
            reasons.append(Reason(
                code=VetoCode.COVERAGE, is_veto=True,
                detail="No weather/snowpack data to confirm the fill a "
                       "non-continuous line needs to reach the true summit.",
            ))
    else:
        if wx.snow_depth_in < cfg.coverage_min_depth_in:
            reasons.append(Reason(
                code=VetoCode.COVERAGE, is_veto=True,
                detail=f"Snowpack {wx.snow_depth_in:.0f}in below coverage "
                       f"threshold {cfg.coverage_min_depth_in:.0f}in (rockout).",
            ))
        elif not line.summit_continuous and wx.snow_depth_in < cfg.fill_depth_in:
            reasons.append(Reason(
                code=VetoCode.COVERAGE, is_veto=True,
                detail=f"Line does not normally reach the true summit and "
                       f"snowpack {wx.snow_depth_in:.0f}in is below the "
                       f"{cfg.fill_depth_in:.0f}in fill needed for a continuous "
                       f"summit descent.",
            ))

    # ---- Veto 5: weather window ----
    if wx is not None:
        if wx.active_storm:
            reasons.append(Reason(
                code=VetoCode.WEATHER_STORM, is_veto=True,
                detail=f"Active storm: {wx.new_snow_24h_in:.0f}in new / "
                       f"{wx.wind_speed_mph:.0f}mph wind (loading + visibility).",
            ))
        if _is_corn_line(line, day) and not wx.active_storm:
            if not wx.overnight_freeze:
                reasons.append(Reason(
                    code=VetoCode.NO_OVERNIGHT_FREEZE, is_veto=True,
                    detail=f"Corn line with no overnight refreeze "
                           f"(min {wx.temp_min_f:.0f}F): wet-slide risk.",
                ))
            elif not wx.daytime_thaw:
                reasons.append(Reason(
                    code=VetoCode.FROZEN_SOLID, is_veto=True,
                    detail=f"Corn line frozen solid all day "
                           f"(max {wx.temp_max_f:.0f}F): unskiable / mistimed.",
                ))

    # ---- Resolve ----
    any_veto = any(r.is_veto for r in reasons)
    if any_veto:
        return _verdict(Decision.NO_GO, peak, line, reasons, 0.0, degraded)

    quality = _quality(line, day, danger, threshold)
    return _verdict(Decision.GO, peak, line, reasons, quality, degraded)


def _verdict(decision, peak, line, reasons, quality, degraded) -> Verdict:
    return Verdict(
        decision=decision,
        peak=peak.name,
        line=line.name,
        reasons=reasons,
        quality=round(quality, 3),
        degraded=degraded,
        line_verified=line.verified,
    )


def _is_corn_line(line: Line, day: DayConditions) -> bool:
    """A sunny-aspect line in the spring corn season."""
    in_season = day.date.month >= 4  # default; cfg.corn_season_start_month
    sunny = any(a in CORN_ASPECTS for a in line.all_aspects)
    return in_season and sunny


def _quality(line: Line, day: DayConditions, danger: DangerLevel,
             threshold: DangerLevel) -> float:
    """0..1 desirability for a GO day (corn ripeness / powder / wind / margin)."""
    wx = day.weather
    q = 0.5
    # Danger margin below the threshold is reassuring.
    if danger != DangerLevel.UNKNOWN:
        q += 0.12 * max(0, int(threshold) - int(danger))
    if wx is not None:
        sunny = any(a in CORN_ASPECTS for a in line.all_aspects)
        if sunny and wx.corn_cycle_ok:
            q += 0.3
        # Fresh-but-not-storm snow on a shady line skis as powder.
        shady = not sunny
        if shady and 1.0 <= wx.new_snow_24h_in < wx.STORM_NEW_SNOW_IN:
            q += 0.2
        # Wind scours and slabs; penalize.
        if wx.wind_speed_mph >= 22:
            q -= 0.2
        if wx.corn_cycle_ok and sunny and wx.wind_speed_mph < 15:
            q += 0.05
    return max(0.0, min(1.0, q))


# --------------------------------------------------------------------------- #
# Cluster resolution: a linkup is governed by its strictest member.
# --------------------------------------------------------------------------- #
def strictest(verdicts: list[Verdict]) -> Verdict:
    """Combine member verdicts for a single-push cluster (Section 6).

    NO_GO if any member is NO_GO; UNKNOWN if any is UNKNOWN and none NO_GO;
    otherwise GO with the minimum member quality. Reasons are unioned so the
    report can see which member bound the linkup.
    """
    if not verdicts:
        raise ValueError("strictest() requires at least one verdict")
    no_go = [v for v in verdicts if v.decision == Decision.NO_GO]
    unknown = [v for v in verdicts if v.decision == Decision.UNKNOWN]
    degraded = any(v.degraded for v in verdicts)
    all_verified = all(v.line_verified for v in verdicts)
    reasons: list[Reason] = []
    for v in verdicts:
        reasons.extend(v.reasons)

    if no_go:
        binder = no_go[0]
        return Verdict(
            decision=Decision.NO_GO, peak=binder.peak, line=binder.line,
            reasons=reasons, quality=0.0, degraded=degraded,
            line_verified=all_verified,
        )
    if unknown:
        binder = unknown[0]
        return Verdict(
            decision=Decision.UNKNOWN, peak=binder.peak, line=binder.line,
            reasons=reasons, quality=0.0, degraded=degraded,
            line_verified=all_verified,
        )
    binder = min(verdicts, key=lambda v: v.quality)
    return Verdict(
        decision=Decision.GO, peak=binder.peak, line=binder.line,
        reasons=reasons, quality=binder.quality, degraded=degraded,
        line_verified=all_verified,
    )
