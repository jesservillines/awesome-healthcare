"""The optimizer: greedy daily step + look-ahead replanner (Section 8).

Layer 1 -- greedy step: each day, compute the GO set, drop objectives the
effort budget can't afford, score the rest by
``cluster_bonus + quality + urgency - travel - fatigue`` and take the best.
Tier-2 objectives ramp up in priority as their window opens and nears its end,
so a CRUX peak late in its window outranks a routine peak.

Layer 2 -- look-ahead replanner: periodically peek the forecast horizon and, for
each remaining CRUX objective, boost its priority in inverse proportion to how
many GO days it has left. This reserves scarce windows so the schedule doesn't
strand itself needing three Elk CRUX peaks in the last safe week, and keeps
routine peaks routed into the gaps.
"""
from __future__ import annotations

from datetime import date, timedelta
from enum import Enum

from pydantic import BaseModel, Field

from fkt_sim.conditions import ConditionsProvider
from fkt_sim.gonogo import (
    Decision,
    GoNoGoConfig,
    Reason,
    Verdict,
    VetoCode,
    evaluate_line,
    strictest,
)
from fkt_sim.peaks import GatekeeperTier, Peak
from fkt_sim.travel import (
    DriveTimeProvider,
    EffortModel,
    FatigueTracker,
    Objective,
)


class DayKind(str, Enum):
    SUMMIT = "summit"        # an objective was completed (single-push)
    BASECAMP = "basecamp"    # a day inside a multi-day push
    HOLD = "hold"            # no GO objective today (weather/conditions hold)
    REST = "rest"            # forced rest (effort budget) with GO available
    REPOSITION = "reposition"  # day consumed driving between ranges


class ScheduledDay(BaseModel):
    date: date
    kind: DayKind
    objective_id: str | None = None
    peaks: list[str] = Field(default_factory=list)
    quality: float = 0.0
    degraded: bool = False
    line_verified: bool = True
    note: str = ""


class SchedulerWeights(BaseModel):
    cluster_bonus_per_peak: float = 1.5
    quality_weight: float = 2.0
    travel_penalty_per_day: float = 3.0
    fatigue_penalty: float = 0.15
    urgency_weight: float = 4.0        # tier-scaled window urgency
    out_of_window_penalty: float = 2.0
    # Look-ahead reservation: scarcity bonus for CRUX with few GO days left.
    reservation_weight: float = 6.0
    reservation_horizon_days: int = 14
    replan_every_days: int = 7


class Schedule(BaseModel):
    """The season result: ordered days plus the binding-constraint counters."""
    days: list[ScheduledDay] = Field(default_factory=list)
    completed_objectives: list[str] = Field(default_factory=list)
    completed_peaks: list[str] = Field(default_factory=list)
    remaining_objectives: list[str] = Field(default_factory=list)

    @property
    def summit_days(self) -> list[ScheduledDay]:
        return [d for d in self.days if d.kind in (DayKind.SUMMIT, DayKind.BASECAMP)]

    def _completion_dates(self) -> dict[str, date]:
        """Map each completed objective to the date it finished (last push day)."""
        out: dict[str, date] = {}
        done = set(self.completed_objectives)
        for d in self.days:
            if d.objective_id in done and d.kind in (DayKind.SUMMIT, DayKind.BASECAMP):
                # later dates overwrite -> ends on the finishing day
                if d.objective_id not in out or d.date > out[d.objective_id]:
                    out[d.objective_id] = d.date
        return out

    @property
    def first_summit(self) -> date | None:
        comp = self._completion_dates()
        return min(comp.values()) if comp else None

    @property
    def last_summit(self) -> date | None:
        comp = self._completion_dates()
        return max(comp.values()) if comp else None

    @property
    def elapsed_days(self) -> int | None:
        """Calendar days from first completed objective to last (inclusive)."""
        f, l = self.first_summit, self.last_summit
        return (l - f).days + 1 if f and l else None

    @property
    def weather_hold_days(self) -> int:
        return sum(1 for d in self.days if d.kind == DayKind.HOLD)

    @property
    def rest_days(self) -> int:
        return sum(1 for d in self.days if d.kind == DayKind.REST)

    @property
    def reposition_days(self) -> int:
        return sum(1 for d in self.days if d.kind == DayKind.REPOSITION)

    @property
    def fraction_degraded(self) -> float:
        decided = [d for d in self.days
                   if d.kind in (DayKind.SUMMIT, DayKind.BASECAMP)]
        if not decided:
            return 0.0
        return round(sum(1 for d in decided if d.degraded) / len(decided), 3)

    @property
    def unverified_lines_used(self) -> list[str]:
        out: list[str] = []
        for d in self.days:
            if d.kind in (DayKind.SUMMIT, DayKind.BASECAMP) and not d.line_verified:
                out.extend(d.peaks)
        return sorted(set(out))

    @property
    def is_complete(self) -> bool:
        return not self.remaining_objectives


def evaluate_objective(
    objective: Objective,
    peaks_by_name: dict[str, Peak],
    provider: ConditionsProvider,
    day: date,
    cfg: GoNoGoConfig,
    lookback_days: int = 3,
) -> Verdict:
    """Go/no-go for an objective starting on ``day``.

    A single-day objective (true linkup) needs every member GO on the same day
    -- ``strictest`` over the members. A multi-day basecamp instead needs every
    member to have at least one GO day *somewhere in the push window*: you camp
    and ski each peak when its own conditions come in. The basecamp's quality is
    the weakest member's best in-window day, and degraded/unverified flags union
    across members.
    """
    if objective.push_days <= 1:
        dc = provider.day_conditions(objective.caic_zone, day, lookback_days)
        verdicts = [
            evaluate_line(peaks_by_name[name].best_line, peaks_by_name[name], dc, cfg)
            for name in objective.peak_names
        ]
        return strictest(verdicts)

    # Multi-day basecamp: each member must clear go/no-go on some day in window.
    window = [day + timedelta(days=i) for i in range(objective.push_days)]
    member_best: dict[str, Verdict | None] = {}
    degraded = False
    for name in objective.peak_names:
        peak = peaks_by_name[name]
        best: Verdict | None = None
        for d in window:
            dc = provider.day_conditions(objective.caic_zone, d, lookback_days)
            v = evaluate_line(peak.best_line, peak, dc, cfg)
            degraded = degraded or v.degraded
            if v.decision == Decision.GO and (best is None or v.quality > best.quality):
                best = v
        member_best[name] = best

    missing = [n for n, b in member_best.items() if b is None]
    if missing:
        return Verdict(
            decision=Decision.NO_GO, peak=missing[0], line="basecamp",
            reasons=[Reason(
                code=VetoCode.BASECAMP_NO_WINDOW, is_veto=True,
                detail=f"{len(missing)} basecamp member(s) had no GO day in the "
                       f"{objective.push_days}-day window: {', '.join(missing)}.",
            )],
            quality=0.0, degraded=degraded, line_verified=True,
        )
    best_by_member = [b for b in member_best.values() if b is not None]
    return Verdict(
        decision=Decision.GO, peak=objective.name, line="basecamp",
        quality=min(b.quality for b in best_by_member),
        degraded=degraded,
        line_verified=all(b.line_verified for b in best_by_member),
    )


class LookAheadReplanner:
    """Reserve scarce CRUX windows by peeking the forecast horizon (Section 8)."""

    def __init__(self, weights: SchedulerWeights, cfg: GoNoGoConfig) -> None:
        self.w = weights
        self.cfg = cfg

    def reservation_bonus(
        self,
        remaining: list[Objective],
        peaks_by_name: dict[str, Peak],
        provider: ConditionsProvider,
        day: date,
    ) -> dict[str, float]:
        """Per-objective priority bonus, larger when a CRUX has few GO days left.

        Counts GO days over the next ``reservation_horizon_days``; a CRUX with a
        single upcoming window is boosted hard so a present GO day for it is not
        squandered, while one with many windows gets little boost.
        """
        bonus: dict[str, float] = {}
        for obj in remaining:
            if obj.tier != GatekeeperTier.CRUX:
                continue
            go_days = 0
            for off in range(self.w.reservation_horizon_days):
                d = day + timedelta(days=off)
                if evaluate_objective(obj, peaks_by_name, provider, d,
                                      self.cfg).decision == Decision.GO:
                    go_days += 1
            bonus[obj.objective_id] = self.w.reservation_weight / (go_days + 1)
        return bonus


class Scheduler:
    """Stateful day-by-day optimizer over the objective set."""

    def __init__(
        self,
        objectives: dict[str, Objective],
        peaks: list[Peak],
        provider: ConditionsProvider,
        drive_time: DriveTimeProvider,
        *,
        effort: EffortModel | None = None,
        fatigue: FatigueTracker | None = None,
        gonogo_config: GoNoGoConfig | None = None,
        weights: SchedulerWeights | None = None,
    ) -> None:
        self.objectives = objectives
        self.peaks_by_name = {p.name: p for p in peaks}
        self.provider = provider
        self.drive_time = drive_time
        self.effort = effort or EffortModel()
        self.fatigue = fatigue or FatigueTracker()
        self.cfg = gonogo_config or GoNoGoConfig()
        self.w = weights or SchedulerWeights()
        self.replanner = LookAheadReplanner(self.w, self.cfg)

        self.remaining: set[str] = set(objectives.keys())
        self.completed: list[str] = []
        self.position: str | None = None
        self._active_until: date | None = None   # last day of an in-progress push
        self._active_obj: Objective | None = None
        self._reservation: dict[str, float] = {}
        self._last_replan: date | None = None

    # -- scoring ------------------------------------------------------------ #
    def _urgency(self, obj: Objective, day: date) -> float:
        """Tier-scaled urgency that ramps as the window opens and nears its end."""
        if int(obj.tier) == 0:
            return 0.0
        end_m, end_d = (int(x) for x in obj.typical_window[1].split("-"))
        try:
            window_end = date(day.year, end_m, end_d)
        except ValueError:
            window_end = date(day.year, end_m, 28)
        days_left = (window_end - day).days
        # Closer to (or past) the window end -> more urgent. 0 at >30 days out.
        proximity = max(0.0, min(1.5, (30 - days_left) / 30))
        return self.w.urgency_weight * int(obj.tier) * proximity

    def _in_window(self, obj: Objective, day: date) -> bool:
        s, e = obj.typical_window
        return s <= f"{day.month:02d}-{day.day:02d}" <= e

    def _score(self, obj: Objective, verdict: Verdict, day: date) -> float:
        size_bonus = self.w.cluster_bonus_per_peak * (len(obj.peak_names) - 1)
        quality = self.w.quality_weight * verdict.quality
        urgency = self._urgency(obj, day)
        reservation = self._reservation.get(obj.objective_id, 0.0)
        travel = 0.0
        if self.position is not None:
            travel = self.w.travel_penalty_per_day * self.drive_time.reposition_days(
                self.position, obj.trailhead_id
            )
        fatigue = self.w.fatigue_penalty * self.effort.daily_fatigue_cost(obj)
        window_pen = 0.0 if self._in_window(obj, day) else self.w.out_of_window_penalty
        return (size_bonus + quality + urgency + reservation
                - travel - fatigue - window_pen)

    # -- daily step --------------------------------------------------------- #
    def advance(self, day: date) -> ScheduledDay:
        """Decide and record one calendar day, mutating scheduler state."""
        # Continue an in-progress multi-day push.
        if self._active_until is not None and self._active_obj is not None:
            obj = self._active_obj
            self.fatigue.record(day, self.effort.daily_fatigue_cost(obj))
            done = day >= self._active_until
            note = "" if done else "basecamp day"
            sd = ScheduledDay(
                date=day, kind=DayKind.BASECAMP, objective_id=obj.objective_id,
                peaks=obj.peak_names, note=note,
            )
            if done:
                self._finish(obj, day)
            return sd

        # Weekly look-ahead replan.
        if (self._last_replan is None
                or (day - self._last_replan).days >= self.w.replan_every_days):
            self._reservation = self.replanner.reservation_bonus(
                [self.objectives[o] for o in self.remaining],
                self.peaks_by_name, self.provider, day,
            )
            self._last_replan = day

        # Greedy: evaluate every remaining objective today.
        candidates: list[tuple[float, Objective, Verdict]] = []
        budget_blocked = False
        for oid in self.remaining:
            obj = self.objectives[oid]
            verdict = evaluate_objective(obj, self.peaks_by_name, self.provider,
                                         day, self.cfg)
            if verdict.decision != Decision.GO:
                continue
            if self.fatigue.would_exceed(self.effort.daily_fatigue_cost(obj), day):
                budget_blocked = True
                continue
            candidates.append((self._score(obj, verdict, day), obj, verdict))

        if not candidates:
            # No affordable GO objective: a forced rest (budget) or a hold.
            self.fatigue.rest(day)
            kind = DayKind.REST if budget_blocked else DayKind.HOLD
            return ScheduledDay(date=day, kind=kind,
                                note=("effort budget" if budget_blocked
                                      else "no GO objective"))

        score, obj, verdict = max(candidates, key=lambda c: c[0])

        # Reposition cost: if a drive eats a meaningful slice of the day, spend a
        # reposition day first and attempt the objective tomorrow.
        if self.position is not None and self.position != obj.trailhead_id:
            if self.drive_time.reposition_days(self.position, obj.trailhead_id) >= 1.0:
                self.position = obj.trailhead_id
                self.fatigue.rest(day)
                return ScheduledDay(
                    date=day, kind=DayKind.REPOSITION,
                    note=f"reposition to {obj.trailhead_id} for {obj.name}",
                )

        # Commit to the objective.
        self.position = obj.trailhead_id
        self.fatigue.record(day, self.effort.daily_fatigue_cost(obj))
        if obj.push_days > 1:
            self._active_obj = obj
            self._active_until = day + timedelta(days=obj.push_days - 1)
            return ScheduledDay(
                date=day, kind=DayKind.BASECAMP, objective_id=obj.objective_id,
                peaks=obj.peak_names, quality=verdict.quality,
                degraded=verdict.degraded, line_verified=verdict.line_verified,
                note="basecamp start",
            )
        self._finish(obj, day, verdict)
        return ScheduledDay(
            date=day, kind=DayKind.SUMMIT, objective_id=obj.objective_id,
            peaks=obj.peak_names, quality=verdict.quality,
            degraded=verdict.degraded, line_verified=verdict.line_verified,
        )

    def _finish(self, obj: Objective, day: date, verdict: Verdict | None = None) -> None:
        if obj.objective_id in self.remaining:
            self.remaining.discard(obj.objective_id)
            self.completed.append(obj.objective_id)
        self._active_obj = None
        self._active_until = None
