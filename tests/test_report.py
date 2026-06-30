"""Phase-7 tests: reports, binding-constraint analysis, floor, fragility."""
from __future__ import annotations

import os
from datetime import date

import pytest

from fkt_sim.conditions import SeasonCharacter, SyntheticProvider
from fkt_sim.gonogo import VetoCode
from fkt_sim.peaks import load_peaks, load_trailheads
from fkt_sim.report import (
    binding_constraints,
    fragility,
    honest_error_banner,
    longest_hold_stretch,
    save_gantt,
    save_veto_histogram,
    text_report,
    theoretical_floor,
    veto_histogram,
)
from fkt_sim.simulate import run_season
from fkt_sim.travel import build_objectives

PEAKS = load_peaks()
TRAILHEADS = load_trailheads()
OBJ = build_objectives(PEAKS)
START = date(2016, 12, 15)
END = date(2017, 6, 15)


def _season(character=SeasonCharacter.NEAR_NORMAL, **kw):
    prov = SyntheticProvider(character, start=START, end=END, **kw)
    sch = run_season(prov, start=START, end=END, peaks=PEAKS, trailheads=TRAILHEADS)
    return prov, sch


def test_longest_hold_stretch():
    _, sch = _season()
    hs = longest_hold_stretch(sch)
    assert hs.length >= 0
    if hs.length > 0:
        assert hs.start is not None and hs.end is not None
        assert (hs.end - hs.start).days + 1 >= hs.length - 0  # within the run


def test_veto_histogram_only_counts_veto_reasons():
    prov, _ = _season()
    hist = veto_histogram(prov, OBJ, PEAKS, START, END,
                          only=["capitol", "blanca_group"])
    assert all(isinstance(k, VetoCode) for k in hist)
    assert sum(hist.values()) > 0


def test_binding_constraints_structure():
    prov, sch = _season()
    bc = binding_constraints(sch, OBJ, prov, PEAKS, START, END)
    assert set(bc.never_completed) == set(sch.remaining_objectives)
    # All CRUX-never-completed are genuinely CRUX and remaining.
    for oid in bc.crux_never_completed:
        assert int(OBJ[oid].tier) == 2 and oid in sch.remaining_objectives
    assert isinstance(bc.veto_counts, dict)


def test_theoretical_floor_incomplete_is_flagged():
    _, sch = _season()
    floor = theoretical_floor(sch, peaks_total=len(PEAKS))
    if not sch.is_complete:
        assert "INCOMPLETE" in floor.verdict
        assert floor.delta_vs_jespersen is None
    assert floor.jespersen_days == 138


def test_honest_error_banner_fields():
    prov, sch = _season(degraded_before=date(2017, 3, 1))
    bc = binding_constraints(sch, OBJ, prov, PEAKS, START, END)
    he = honest_error_banner(sch, OBJ, bc)
    assert 0.0 <= he.fraction_degraded <= 1.0
    # Unverified-line list is a subset of the peaks actually skied.
    assert set(he.unverified_lines_in_critical_path) <= set(sch.completed_peaks)


def test_text_report_contains_key_sections():
    prov, sch = _season()
    txt = text_report(sch, OBJ, prov, PEAKS, START, END,
                      character=SeasonCharacter.NEAR_NORMAL)
    for marker in ("FKT SEASON REPORT", "THEORETICAL FLOOR", "BINDING CONSTRAINTS",
                   "HONEST-ERROR BANNER", "Jespersen"):
        assert marker in txt


def test_fragility_measures_window_removal():
    fr = fragility(SeasonCharacter.NEAR_NORMAL, start=START, end=END,
                   peaks=PEAKS, trailheads=TRAILHEADS)
    # A CRUX peak was completed in this season, so a window is removed.
    assert fr.removed_objective is not None
    assert int(OBJ[fr.removed_objective].tier) == 2
    assert fr.baseline_peaks >= fr.perturbed_peaks - 0  # removal cannot add peaks
    # Removing a load-bearing window should not speed up the finish.
    if fr.elapsed_delta is not None:
        assert fr.elapsed_delta >= 0


def test_charts_write_files(tmp_path):
    pytest.importorskip("matplotlib")
    prov, sch = _season()
    bc = binding_constraints(sch, OBJ, prov, PEAKS, START, END)
    g = save_gantt(sch, OBJ, str(tmp_path / "gantt.png"))
    h = save_veto_histogram(bc.veto_counts, str(tmp_path / "vetoes.png"))
    assert os.path.getsize(g) > 0 and os.path.getsize(h) > 0
