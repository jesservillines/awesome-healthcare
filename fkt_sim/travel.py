"""Travel + effort modeling (Section 6).

Three concerns:

* **Drive-time matrix** between trailheads, behind a swappable
  ``DriveTimeProvider`` so the seed straight-line estimate can be replaced by a
  real routing API (OSRM/Google) without touching callers.
* **Objectives**: a single peak or a cluster "super-objective" bagged in one
  push, carrying combined vert/miles, the max member angle, and the max member
  tier (its strictest go/no-go is resolved in ``gonogo.strictest``).
* **Effort budget**: a fatigue cost per objective plus a rolling multi-day
  accumulator with a cap, so the scheduler must insert rest/reposition days
  instead of stacking Capitol onto a dawn Sangres push. Splitboarders pay a
  transition-time penalty on low-angle/rolling days where skiers skin-glide.
"""
from __future__ import annotations

import abc
import math
from datetime import date, timedelta

from pydantic import BaseModel, Field

from fkt_sim.peaks import GatekeeperTier, Peak, Trailhead, cluster_map

# --------------------------------------------------------------------------- #
# Geography / drive times
# --------------------------------------------------------------------------- #
_EARTH_RADIUS_MI = 3958.8


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_RADIUS_MI * math.asin(math.sqrt(a))


class DriveTimeProvider(abc.ABC):
    """Pairwise trailhead drive time in hours. Swappable (seed vs real API)."""

    @abc.abstractmethod
    def drive_hours(self, th_a: str, th_b: str) -> float: ...

    def reposition_days(self, th_a: str, th_b: str, full_day_hours: float = 4.0) -> float:
        """Fraction of a day a reposition costs.

        A cross-state Front Range <-> San Juan haul (4-7 hr) burns a partial (or
        whole) day the scheduler must pay before the next objective.
        """
        hrs = self.drive_hours(th_a, th_b)
        return round(hrs / full_day_hours, 3)


class StraightLineDriveTime(DriveTimeProvider):
    """Seed estimate: great-circle distance x road circuity / average speed.

    Replace with an ``OSRMDriveTime``/``GoogleDriveTime`` adapter later without
    changing the scheduler -- this is the swappable seam Section 6 asks for.
    """

    def __init__(
        self,
        trailheads: dict[str, Trailhead],
        circuity: float = 1.5,
        avg_mph: float = 40.0,
    ) -> None:
        self.trailheads = trailheads
        self.circuity = circuity
        self.avg_mph = avg_mph
        self._cache: dict[tuple[str, str], float] = {}

    def road_miles(self, th_a: str, th_b: str) -> float:
        a, b = self.trailheads[th_a], self.trailheads[th_b]
        return haversine_miles(a.lat, a.lon, b.lat, b.lon) * self.circuity

    def drive_hours(self, th_a: str, th_b: str) -> float:
        if th_a == th_b:
            return 0.0
        key = (th_a, th_b) if th_a < th_b else (th_b, th_a)
        if key not in self._cache:
            self._cache[key] = self.road_miles(th_a, th_b) / self.avg_mph
        return round(self._cache[key], 3)


# --------------------------------------------------------------------------- #
# Objectives (single peak or one-push cluster)
# --------------------------------------------------------------------------- #
class Objective(BaseModel):
    """A schedulable unit: one peak, or a cluster bagged in a single push."""
    objective_id: str            # == cluster_id
    name: str
    peak_names: list[str]
    caic_zone: str
    trailhead_id: str            # representative TH (drive/reposition anchor)
    tier: GatekeeperTier         # strictest (max) member tier
    combined_vert_ft: int
    combined_miles_rt: float
    max_angle_deg: int
    typical_window: tuple[str, str]   # widest member window (earliest..latest)
    push_days: int               # calendar days the push occupies (basecamps > 1)

    @property
    def is_cluster(self) -> bool:
        return len(self.peak_names) > 1


def _window_union(peaks: list[Peak]) -> tuple[str, str]:
    starts = [p.typical_window[0] for p in peaks]
    ends = [p.typical_window[1] for p in peaks]
    # earliest opening .. latest closing (string "MM-DD" sorts correctly)
    return min(starts), max(ends)


# Clusters that realistically need a multi-day basecamp regardless of distance.
_BASECAMP_CLUSTERS = {"chicago_basin": 3}
_MULTIDAY_MILES = 20.0


def build_objectives(peaks: list[Peak]) -> dict[str, Objective]:
    """Collapse the 55 peaks into schedulable objectives by ``cluster_id``.

    Combined vert is the sum (real fatigue of the linkup); combined miles is the
    longest member approach plus a half-weight for the added traverse distance
    of the others (they overlap, so summing would over-count).
    """
    objectives: dict[str, Objective] = {}
    for cid, members in cluster_map(peaks).items():
        members_sorted = sorted(members, key=lambda p: -p.approach_miles_rt)
        longest = members_sorted[0].approach_miles_rt
        extra = sum(p.approach_miles_rt for p in members_sorted[1:]) * 0.5
        miles = round(longest + extra, 1)

        push_days = _BASECAMP_CLUSTERS.get(cid, 1)
        if push_days == 1 and miles > _MULTIDAY_MILES:
            push_days = 2

        # Representative trailhead: the most-common member TH (anchor for drives).
        th_counts: dict[str, int] = {}
        for p in members:
            th_counts[p.trailhead_id] = th_counts.get(p.trailhead_id, 0) + 1
        representative_th = max(th_counts, key=lambda t: th_counts[t])

        name = (members[0].name if len(members) == 1
                else f"{cid} ({'+'.join(p.name.split()[-1] for p in members)})")
        objectives[cid] = Objective(
            objective_id=cid,
            name=name,
            peak_names=[p.name for p in members],
            caic_zone=members[0].caic_zone,
            trailhead_id=representative_th,
            tier=GatekeeperTier(max(int(p.tier) for p in members)),
            combined_vert_ft=sum(p.vert_ft for p in members),
            combined_miles_rt=miles,
            max_angle_deg=max(p.best_line.max_angle_deg for p in members),
            typical_window=_window_union(members),
            push_days=push_days,
        )
    return objectives


# --------------------------------------------------------------------------- #
# Effort / fatigue
# --------------------------------------------------------------------------- #
class EffortModel(BaseModel):
    """Fatigue cost weights + the splitboard transition penalty. Tunable seeds."""
    w_vert_per_kft: float = 1.0       # per 1000 ft of vertical
    w_mile: float = 0.25              # per round-trip mile
    w_angle_over_30: float = 0.10     # per degree of sustained pitch above 30
    w_tier: float = 0.5               # per gatekeeper tier
    # Splitboard penalty: rolling/low-angle pushes (skiers skin-glide; a splitter
    # must transition) cost ~10-20% more "effort time".
    splitboard_low_angle_threshold: int = 33
    splitboard_penalty: float = 1.15

    def splitboard_multiplier(self, objective: Objective) -> float:
        return (self.splitboard_penalty
                if objective.max_angle_deg <= self.splitboard_low_angle_threshold
                else 1.0)

    def fatigue_cost(self, objective: Objective) -> float:
        """Total fatigue of the whole push (summed over a multi-day basecamp)."""
        base = (
            self.w_vert_per_kft * objective.combined_vert_ft / 1000.0
            + self.w_mile * objective.combined_miles_rt
            + self.w_angle_over_30 * max(0, objective.max_angle_deg - 30)
            + self.w_tier * int(objective.tier)
        )
        return round(base * self.splitboard_multiplier(objective), 3)

    def daily_fatigue_cost(self, objective: Objective) -> float:
        """Per-day fatigue load. A multi-day basecamp spreads its total over its
        ``push_days`` so a long linkup uses the rolling budget across the push
        rather than spiking a single day past the cap."""
        return round(self.fatigue_cost(objective) / max(1, objective.push_days), 3)


class FatigueTracker:
    """Rolling N-day fatigue accumulator with a cap (Section 6).

    The cap is what stops the optimizer from reporting an impossible sequence
    (Capitol then a dawn Sangres day). When the next objective would push the
    trailing-window load over the cap, the scheduler must insert a rest or
    reposition day; days naturally fall out of the window as time advances.
    """

    def __init__(self, window_days: int = 3, cap: float = 24.0) -> None:
        self.window_days = window_days
        self.cap = cap
        self._log: dict[date, float] = {}

    def _window_start(self, as_of: date) -> date:
        return as_of - timedelta(days=self.window_days - 1)

    def rolling_load(self, as_of: date) -> float:
        start = self._window_start(as_of)
        return round(sum(c for d, c in self._log.items() if start <= d <= as_of), 3)

    def would_exceed(self, cost: float, as_of: date) -> bool:
        """True if adding ``cost`` on ``as_of`` breaches the rolling cap."""
        return self.rolling_load(as_of) + cost > self.cap

    def record(self, day: date, cost: float) -> None:
        self._log[day] = self._log.get(day, 0.0) + cost

    def rest(self, day: date) -> None:
        """A rest/hold/reposition day adds no load; the window slides past it."""
        self._log.setdefault(day, 0.0)
