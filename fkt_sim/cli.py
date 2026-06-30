"""Command-line entrypoint for the 14er FKT simulator.

Examples:
    python -m fkt_sim.cli --character near_normal
    python -m fkt_sim.cli --three
    python -m fkt_sim.cli --character continental_pwl --charts-dir out/ --fragility
"""
from __future__ import annotations

import argparse
from datetime import date

from fkt_sim.conditions import SeasonCharacter, SyntheticProvider
from fkt_sim.peaks import load_peaks, load_trailheads
from fkt_sim.report import (
    binding_constraints,
    fragility,
    save_gantt,
    save_veto_histogram,
    text_report,
)
from fkt_sim.simulate import run_season
from fkt_sim.travel import build_objectives


def _parse_md_year(s: str) -> date:
    return date.fromisoformat(s)


def _run_one(character: SeasonCharacter, args, peaks, trailheads) -> None:
    objectives = build_objectives(peaks)
    provider = SyntheticProvider(character, start=args.start, end=args.end,
                                 seed=args.seed,
                                 degraded_before=args.degraded_before)
    schedule = run_season(provider, start=args.start, end=args.end,
                          peaks=peaks, trailheads=trailheads)
    print(text_report(schedule, objectives, provider, peaks,
                      args.start, args.end, character=character))

    if args.fragility:
        fr = fragility(character, start=args.start, end=args.end, seed=args.seed,
                       peaks=peaks, trailheads=trailheads)
        print("\n-- FRAGILITY (remove single best CRUX window) " + "-" * 20)
        if fr.removed_objective:
            print(f"  Removed: {fr.removed_objective} ({fr.note})")
            print(f"  Elapsed: {fr.baseline_elapsed} -> {fr.perturbed_elapsed} "
                  f"(delta {fr.elapsed_delta})")
            print(f"  Peaks:   {fr.baseline_peaks} -> {fr.perturbed_peaks} "
                  f"(delta {fr.peaks_delta})")
        else:
            print(f"  {fr.note}")

    if args.charts_dir:
        import os
        os.makedirs(args.charts_dir, exist_ok=True)
        gantt = os.path.join(args.charts_dir, f"gantt_{character.value}.png")
        save_gantt(schedule, objectives, gantt)
        bc = binding_constraints(schedule, objectives, provider, peaks,
                                 args.start, args.end)
        hist = os.path.join(args.charts_dir, f"vetoes_{character.value}.png")
        save_veto_histogram(bc.veto_counts, hist)
        print(f"\n  Charts written: {gantt}, {hist}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Colorado 14er splitboard FKT simulator")
    p.add_argument("--character", choices=[c.value for c in SeasonCharacter],
                   default=SeasonCharacter.NEAR_NORMAL.value,
                   help="snowpack character of the synthetic season")
    p.add_argument("--three", action="store_true",
                   help="run all three season characters")
    p.add_argument("--start", type=_parse_md_year, default=date(2016, 12, 15))
    p.add_argument("--end", type=_parse_md_year, default=date(2017, 6, 15))
    p.add_argument("--seed", type=int, default=1453)
    p.add_argument("--degraded-before", type=_parse_md_year, default=None,
                   help="forecasts before this date carry no rose (degraded)")
    p.add_argument("--fragility", action="store_true",
                   help="compute the fragility metric")
    p.add_argument("--charts-dir", default=None,
                   help="write Gantt + veto-histogram PNGs here")
    args = p.parse_args(argv)

    peaks = load_peaks()
    trailheads = load_trailheads()
    characters = (list(SeasonCharacter) if args.three
                  else [SeasonCharacter(args.character)])
    for character in characters:
        _run_one(character, args, peaks, trailheads)
        if len(characters) > 1:
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
