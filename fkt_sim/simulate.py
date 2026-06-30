"""Season replay loop -- ties everything together (Section 9).

Replays a season day by day: each day the scheduler is handed the current state
and the day's conditions, and either bags an objective, holds for weather, rests
for the effort budget, or repositions. The result is a ``Schedule`` plus the
binding-constraint counters the report consumes.
"""
from __future__ import annotations

from datetime import date, timedelta

from fkt_sim.conditions import ConditionsProvider, SeasonCharacter
from fkt_sim.gonogo import GoNoGoConfig
from fkt_sim.peaks import Peak, load_peaks
from fkt_sim.scheduler import (
    Schedule,
    Scheduler,
    SchedulerWeights,
)
from fkt_sim.travel import (
    EffortModel,
    FatigueTracker,
    StraightLineDriveTime,
    build_objectives,
)


def run_season(
    provider: ConditionsProvider,
    *,
    start: date,
    end: date,
    peaks: list[Peak] | None = None,
    trailheads=None,
    weights: SchedulerWeights | None = None,
    effort: EffortModel | None = None,
    fatigue: FatigueTracker | None = None,
    gonogo_config: GoNoGoConfig | None = None,
    stop_when_complete: bool = True,
) -> Schedule:
    """Replay one season and return its Schedule.

    The loop stops early once every objective is bagged (the FKT clock is the
    span between the first and last summit, so trailing idle days don't matter).
    """
    from fkt_sim.peaks import load_trailheads

    peaks = peaks if peaks is not None else load_peaks()
    trailheads = trailheads if trailheads is not None else load_trailheads()
    objectives = build_objectives(peaks)
    drive_time = StraightLineDriveTime(trailheads)

    scheduler = Scheduler(
        objectives, peaks, provider, drive_time,
        effort=effort, fatigue=fatigue,
        gonogo_config=gonogo_config, weights=weights,
    )

    schedule = Schedule(remaining_objectives=sorted(objectives.keys()))
    day = start
    while day <= end:
        sd = scheduler.advance(day)
        schedule.days.append(sd)
        if stop_when_complete and not scheduler.remaining:
            break
        day += timedelta(days=1)

    schedule.completed_objectives = list(scheduler.completed)
    schedule.remaining_objectives = sorted(scheduler.remaining)
    schedule.completed_peaks = [
        name for oid in scheduler.completed
        for name in objectives[oid].peak_names
    ]
    return schedule


def run_three_seasons(
    *,
    start: date = date(2016, 12, 15),
    end: date = date(2017, 6, 15),
    seed: int = 1453,
    **kwargs,
) -> dict[SeasonCharacter, Schedule]:
    """Backtest the good/bad/average trio (classified by snowpack character)."""
    from fkt_sim.conditions import SyntheticProvider

    out: dict[SeasonCharacter, Schedule] = {}
    for character in SeasonCharacter:
        provider = SyntheticProvider(character, start=start, end=end, seed=seed)
        out[character] = run_season(provider, start=start, end=end, **kwargs)
    return out


# Jespersen's 2017 benchmark to beat (~138 days, Jan 3 - May 21).
JESPERSEN_DAYS = 138
