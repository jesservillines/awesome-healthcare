"""Reports: schedule, binding-constraint analysis, floor, fragility (Section 10).

The headline outputs:

* a season summary + a Gantt-style schedule chart;
* a **binding-constraint analysis** -- which peaks set the finish, a histogram of
  why NO-GO days were NO-GO, and the longest weather-hold stretch;
* the **theoretical floor vs Jespersen's 138 days**, wrapped in an honest-error
  banner (fraction of decisions on degraded data, count of unverified lines in
  the critical path, which CRUX windows were UNKNOWN);
* a **fragility** metric: how far the finish moves if the single best CRUX
  window is removed (simulating missing one good day).

Charts require matplotlib; the text report does not, so the module imports and
runs in a headless environment without it.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from fkt_sim.conditions import ConditionsProvider, SeasonCharacter, SyntheticProvider
from fkt_sim.gonogo import Decision, GoNoGoConfig, VetoCode
from fkt_sim.peaks import GatekeeperTier, Peak, load_peaks, load_trailheads
from fkt_sim.scheduler import DayKind, Schedule, evaluate_objective
from fkt_sim.simulate import JESPERSEN_DAYS, run_season
from fkt_sim.travel import Objective, build_objectives


# --------------------------------------------------------------------------- #
# Binding-constraint analysis
# --------------------------------------------------------------------------- #
@dataclass
class HoldStretch:
    length: int
    start: date | None
    end: date | None


def longest_hold_stretch(schedule: Schedule) -> HoldStretch:
    """Longest run of consecutive weather-HOLD days, and where it fell."""
    best = HoldStretch(0, None, None)
    run_start: date | None = None
    run_len = 0
    prev: date | None = None
    for d in schedule.days:
        if d.kind == DayKind.HOLD:
            if run_start is None:
                run_start, run_len = d.date, 0
            run_len += 1
            prev = d.date
            if run_len > best.length:
                best = HoldStretch(run_len, run_start, prev)
        else:
            run_start, run_len = None, 0
    return best


def veto_histogram(
    provider: ConditionsProvider,
    objectives: dict[str, Objective],
    peaks: list[Peak],
    start: date,
    end: date,
    *,
    only: list[str] | None = None,
    cfg: GoNoGoConfig | None = None,
) -> Counter:
    """Tally why NO-GO days were NO-GO across the season.

    Restricted by default to the binding (``only``) objectives so the histogram
    reflects what actually held up the finish rather than easy peaks.
    """
    cfg = cfg or GoNoGoConfig()
    pbn = {p.name: p for p in peaks}
    ids = only if only is not None else list(objectives)
    tally: Counter = Counter()
    d = start
    while d <= end:
        for oid in ids:
            v = evaluate_objective(objectives[oid], pbn, provider, d, cfg)
            if v.decision == Decision.NO_GO:
                for r in v.reasons:
                    if r.is_veto:
                        tally[r.code] += 1
            elif v.decision == Decision.UNKNOWN:
                tally[VetoCode.UNKNOWN_DATA] += 1
        d += timedelta(days=1)
    return tally


def critical_path(
    schedule: Schedule,
    objectives: dict[str, Objective],
    *,
    late_fraction: float = 0.25,
) -> dict[str, list[str]]:
    """Objectives that bind the finish: those completed in the final stretch of
    the season plus everything never completed."""
    finishers: list[str] = []
    first, last = schedule.first_summit, schedule.last_summit
    if first and last:
        span = (last - first).days or 1
        cutoff = last - timedelta(days=int(span * late_fraction))
        comp_dates = schedule._completion_dates()
        finishers = sorted(oid for oid, dt in comp_dates.items() if dt >= cutoff)
    return {
        "late_finishers": finishers,
        "never_completed": list(schedule.remaining_objectives),
    }


@dataclass
class BindingConstraints:
    finish_date: date | None
    elapsed_days: int | None
    late_finishers: list[str]
    never_completed: list[str]
    veto_counts: dict[str, int]
    longest_hold: HoldStretch
    crux_never_completed: list[str] = field(default_factory=list)


def binding_constraints(
    schedule: Schedule,
    objectives: dict[str, Objective],
    provider: ConditionsProvider,
    peaks: list[Peak],
    start: date,
    end: date,
    *,
    cfg: GoNoGoConfig | None = None,
) -> BindingConstraints:
    cp = critical_path(schedule, objectives)
    binders = sorted(set(cp["late_finishers"]) | set(cp["never_completed"]))
    hist = veto_histogram(provider, objectives, peaks, start, end,
                          only=binders or None, cfg=cfg)
    crux_remaining = [oid for oid in schedule.remaining_objectives
                      if objectives[oid].tier == GatekeeperTier.CRUX]
    return BindingConstraints(
        finish_date=schedule.last_summit,
        elapsed_days=schedule.elapsed_days,
        late_finishers=cp["late_finishers"],
        never_completed=cp["never_completed"],
        veto_counts={k.value: v for k, v in hist.most_common()},
        longest_hold=longest_hold_stretch(schedule),
        crux_never_completed=crux_remaining,
    )


# --------------------------------------------------------------------------- #
# Theoretical floor + honest-error banner
# --------------------------------------------------------------------------- #
@dataclass
class HonestError:
    fraction_degraded: float
    unverified_lines_in_critical_path: list[str]
    crux_windows_unknown: list[str]
    note: str


def honest_error_banner(
    schedule: Schedule,
    objectives: dict[str, Objective],
    bc: BindingConstraints,
) -> HonestError:
    binders = set(bc.late_finishers) | set(bc.never_completed)
    binder_peaks = {p for oid in binders for p in objectives[oid].peak_names}
    unverified = sorted(set(schedule.unverified_lines_used) & binder_peaks)
    # CRUX objectives that were never completed are, by construction, ones whose
    # safe windows never resolved -- the closest synthetic analogue to "UNKNOWN
    # CRUX window" that the report must flag.
    crux_unknown = sorted(bc.crux_never_completed)
    return HonestError(
        fraction_degraded=schedule.fraction_degraded,
        unverified_lines_in_critical_path=unverified,
        crux_windows_unknown=crux_unknown,
        note=("Seed lines are unverified; any peak above must be checked against "
              "Dawson/14ers.com/CAIC polygons before this result is treated as "
              "real. Degraded days fell back to the overall danger rating."),
    )


@dataclass
class TheoreticalFloor:
    elapsed_days: int | None
    complete: bool
    peaks_completed: int
    peaks_total: int
    jespersen_days: int
    delta_vs_jespersen: int | None
    verdict: str


def theoretical_floor(schedule: Schedule, peaks_total: int = 55) -> TheoreticalFloor:
    elapsed = schedule.elapsed_days
    complete = schedule.is_complete
    done = len(schedule.completed_peaks)
    if complete and elapsed is not None:
        delta = elapsed - JESPERSEN_DAYS
        verdict = (f"Beats Jespersen by {-delta} days" if delta < 0
                   else f"{delta} days slower than Jespersen" if delta > 0
                   else "Ties Jespersen")
    else:
        delta = None
        verdict = (f"INCOMPLETE: bagged {done}/{peaks_total} peaks; the season's "
                   f"binding CRUX windows never resolved. Not comparable to "
                   f"Jespersen's full-54 run.")
    return TheoreticalFloor(
        elapsed_days=elapsed, complete=complete, peaks_completed=done,
        peaks_total=peaks_total, jespersen_days=JESPERSEN_DAYS,
        delta_vs_jespersen=delta, verdict=verdict,
    )


# --------------------------------------------------------------------------- #
# Fragility: remove the single best CRUX window, re-run, measure the move.
# --------------------------------------------------------------------------- #
@dataclass
class Fragility:
    removed_objective: str | None
    removed_days: list[str]
    baseline_elapsed: int | None
    baseline_peaks: int
    perturbed_elapsed: int | None
    perturbed_peaks: int
    elapsed_delta: int | None
    peaks_delta: int
    note: str


def fragility(
    character: SeasonCharacter,
    *,
    start: date,
    end: date,
    seed: int = 1453,
    peaks: list[Peak] | None = None,
    trailheads=None,
) -> Fragility:
    """Knock out the most finish-critical CRUX window and re-run.

    Identifies the CRUX objective completed nearest the finish, removes the
    conditions on the day(s) it was bagged (so that window reads UNKNOWN), and
    re-runs. The shift in finish date / peaks bagged is how much slack a real
    attempt needs around its single best day. (Defined for SyntheticProvider,
    which supports day-level UNKNOWN injection.)
    """
    peaks = peaks if peaks is not None else load_peaks()
    trailheads = trailheads if trailheads is not None else load_trailheads()
    objectives = build_objectives(peaks)

    base_provider = SyntheticProvider(character, start=start, end=end, seed=seed)
    base = run_season(base_provider, start=start, end=end,
                      peaks=peaks, trailheads=trailheads)

    # Pick the CRUX objective completed closest to the finish.
    comp = base._completion_dates()
    crux_comp = {oid: dt for oid, dt in comp.items()
                 if objectives[oid].tier == GatekeeperTier.CRUX}
    if not crux_comp:
        return Fragility(
            removed_objective=None, removed_days=[],
            baseline_elapsed=base.elapsed_days,
            baseline_peaks=len(base.completed_peaks),
            perturbed_elapsed=base.elapsed_days,
            perturbed_peaks=len(base.completed_peaks),
            elapsed_delta=0, peaks_delta=0,
            note="No CRUX objective was completed; nothing to perturb.",
        )
    target = max(crux_comp, key=lambda o: crux_comp[o])
    zone = objectives[target].caic_zone
    target_days = [d.date for d in base.days if d.objective_id == target]
    unknown = frozenset((zone, dd.isoformat()) for dd in target_days)

    pert_provider = SyntheticProvider(character, start=start, end=end, seed=seed,
                                      unknown_zone_days=unknown)
    pert = run_season(pert_provider, start=start, end=end,
                      peaks=peaks, trailheads=trailheads)

    be, pe = base.elapsed_days, pert.elapsed_days
    return Fragility(
        removed_objective=target,
        removed_days=[dd.isoformat() for dd in target_days],
        baseline_elapsed=be, baseline_peaks=len(base.completed_peaks),
        perturbed_elapsed=pe, perturbed_peaks=len(pert.completed_peaks),
        elapsed_delta=(pe - be) if (be is not None and pe is not None) else None,
        peaks_delta=len(pert.completed_peaks) - len(base.completed_peaks),
        note=f"Removed the {objectives[target].name} window "
             f"({', '.join(dd.isoformat() for dd in target_days)}).",
    )


# --------------------------------------------------------------------------- #
# Text report
# --------------------------------------------------------------------------- #
def text_report(
    schedule: Schedule,
    objectives: dict[str, Objective],
    provider: ConditionsProvider,
    peaks: list[Peak],
    start: date,
    end: date,
    *,
    character: SeasonCharacter | None = None,
    cfg: GoNoGoConfig | None = None,
) -> str:
    bc = binding_constraints(schedule, objectives, provider, peaks, start, end, cfg=cfg)
    floor = theoretical_floor(schedule, peaks_total=len(peaks))
    he = honest_error_banner(schedule, objectives, bc)

    L: list[str] = []
    title = f"FKT SEASON REPORT" + (f" -- {character.value}" if character else "")
    L.append("=" * 68)
    L.append(title)
    L.append("=" * 68)
    L.append(f"Window: {start} -> {end}")
    L.append(f"Objectives bagged: {len(schedule.completed_objectives)}/{len(objectives)}"
             f"   Peaks: {floor.peaks_completed}/{floor.peaks_total}")
    L.append(f"First summit: {schedule.first_summit}   Last summit: {schedule.last_summit}")
    L.append(f"Elapsed (first->last): {schedule.elapsed_days} days")
    L.append(f"Weather holds: {schedule.weather_hold_days}   "
             f"Rest: {schedule.rest_days}   Repositions: {schedule.reposition_days}")
    L.append("")
    L.append("-- THEORETICAL FLOOR vs JESPERSEN (138 days) " + "-" * 22)
    L.append(f"  {floor.verdict}")
    L.append("")
    L.append("-- BINDING CONSTRAINTS " + "-" * 44)
    L.append(f"  Finish date set by: {', '.join(bc.late_finishers) or '(n/a)'}")
    if bc.never_completed:
        L.append(f"  Never completed ({len(bc.never_completed)}): "
                 f"{', '.join(bc.never_completed)}")
    L.append(f"  CRUX never completed: {', '.join(bc.crux_never_completed) or '(none)'}")
    L.append(f"  Longest weather hold: {bc.longest_hold.length} days "
             f"({bc.longest_hold.start} -> {bc.longest_hold.end})")
    L.append("  Why NO-GO (binding objectives):")
    for code, n in bc.veto_counts.items():
        L.append(f"      {code:22s} {n}")
    L.append("")
    L.append("!! HONEST-ERROR BANNER " + "!" * 44)
    L.append(f"  Fraction of summit days on degraded data: {he.fraction_degraded:.1%}")
    L.append(f"  Unverified seed lines in critical path "
             f"({len(he.unverified_lines_in_critical_path)}): "
             f"{', '.join(he.unverified_lines_in_critical_path) or '(none)'}")
    L.append(f"  CRUX windows unresolved/UNKNOWN: "
             f"{', '.join(he.crux_windows_unknown) or '(none)'}")
    L.append(f"  {he.note}")
    L.append("=" * 68)
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Charts (matplotlib, optional)
# --------------------------------------------------------------------------- #
_TIER_COLORS = {0: "#4caf50", 1: "#ff9800", 2: "#e53935"}


def save_gantt(schedule: Schedule, objectives: dict[str, Objective], path: str) -> str:
    """Gantt-style schedule: objectives on Y, date on X, colored by tier; hold
    days shaded. Returns the path written."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    rows = [d for d in schedule.days if d.kind in (DayKind.SUMMIT, DayKind.BASECAMP)
            and d.objective_id]
    order: list[str] = []
    for d in rows:
        if d.objective_id not in order:
            order.append(d.objective_id)
    y_of = {oid: i for i, oid in enumerate(order)}

    fig, ax = plt.subplots(figsize=(11, max(4, 0.32 * len(order) + 2)))
    # Shade hold days.
    for d in schedule.days:
        if d.kind == DayKind.HOLD:
            ax.axvspan(d.date, d.date + timedelta(days=1), color="#dddddd", alpha=0.5)
    # Plot objective bars.
    for d in rows:
        tier = int(objectives[d.objective_id].tier)
        ax.barh(y_of[d.objective_id], 1.0, left=d.date,
                color=_TIER_COLORS.get(tier, "#777"), edgecolor="black", height=0.6)
    ax.set_yticks(range(len(order)))
    ax.set_yticklabels(order, fontsize=7)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.set_title("FKT schedule (green=routine, orange=moderate, red=CRUX; "
                 "grey=weather hold)")
    ax.invert_yaxis()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def save_veto_histogram(veto_counts: dict[str, int], path: str) -> str:
    """Bar chart of why NO-GO days were NO-GO. Returns the path written."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    items = sorted(veto_counts.items(), key=lambda kv: -kv[1])
    labels = [k for k, _ in items]
    vals = [v for _, v in items]
    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.bar(labels, vals, color="#e53935")
    ax.set_ylabel("NO-GO days (binding objectives)")
    ax.set_title("Why NO-GO days were NO-GO")
    plt.xticks(rotation=30, ha="right", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path
