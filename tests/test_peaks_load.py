"""Phase-1 tests: the domain model loads and the seed data is internally sound.

Run with ``pytest`` (or ``python -m pytest tests/test_peaks_load.py``).
"""
from __future__ import annotations

from fkt_sim.peaks import (
    Aspect,
    GatekeeperTier,
    cluster_map,
    load_caic_zones,
    load_peaks,
    load_trailheads,
)

# The three trivial bumps dropped from the 58-named list. El Diente and North
# Maroon are BOTH kept per review; El Diente links with Mt. Wilson as one push.
DROPPED_FROM_58 = {"Mt. Cameron", "Conundrum Peak", "North Eolus"}


def test_exactly_55_unique_peaks():
    peaks = load_peaks()
    assert len(peaks) == 55
    names = [p.name for p in peaks]
    assert len(set(names)) == 55, "peak names must be unique"


def test_dropped_subpeaks_absent_kept_subpeaks_present():
    names = {p.name for p in load_peaks()}
    assert names.isdisjoint(DROPPED_FROM_58), "dropped sub-peaks leaked into the list"
    # Both sub-prominence peaks we keep per review.
    assert "North Maroon Peak" in names
    assert "El Diente Peak" in names


def test_el_diente_linked_with_mt_wilson_in_one_push():
    clusters = cluster_map(load_peaks())
    wilson = {p.name for p in clusters["wilson_group"]}
    assert {"Mt. Wilson", "El Diente Peak"} <= wilson, (
        "El Diente must share the wilson_group cluster (one push via the traverse)"
    )


def test_every_peak_maps_to_a_real_caic_zone():
    zones = load_caic_zones()
    peaks = load_peaks(zones=zones, trailheads=load_trailheads())
    for p in peaks:
        assert p.caic_zone in zones, f"{p.name}: zone {p.caic_zone} not defined"


def test_every_peak_maps_to_a_real_trailhead():
    trailheads = load_trailheads()
    peaks = load_peaks(trailheads=trailheads, zones=load_caic_zones())
    for p in peaks:
        assert p.trailhead_id in trailheads, f"{p.name}: bad trailhead {p.trailhead_id}"


def test_zones_used_in_seed_are_flagged_used():
    zones = load_caic_zones()
    used = {p.caic_zone for p in load_peaks()}
    for zid in used:
        assert zones[zid].used_in_seed, f"{zid} is referenced but not flagged used_in_seed"


def test_model_fields_are_sane():
    for p in load_peaks():
        assert p.elevation_ft >= 14000, p.name
        assert isinstance(p.tier, GatekeeperTier)
        assert p.lines, f"{p.name} has no line"
        line = p.best_line
        assert isinstance(line.primary_aspect, Aspect)
        assert 20 <= line.max_angle_deg <= 60, f"{p.name}: implausible angle"
        # window parses and is ordered within the calendar year
        (sm, sd), (em, ed) = (
            tuple(int(x) for x in p.typical_window[0].split("-")),
            tuple(int(x) for x in p.typical_window[1].split("-")),
        )
        assert (sm, sd) <= (em, ed), f"{p.name}: window start after end"
        assert p.approach_miles_rt > 0 and p.vert_ft > 0, p.name


def test_all_seed_lines_unverified():
    # Provenance guard: nothing in the seed may masquerade as verified.
    for p in load_peaks():
        assert not p.all_verified, f"{p.name}: seed line wrongly marked verified"


def test_cluster_members_share_zone_and_trailhead_consistency():
    peaks = load_peaks()
    clusters = cluster_map(peaks)
    assert len(clusters) >= 1
    # Known multi-peak linkups must actually contain >1 peak.
    for cid in ("mosquito_decalibron", "grays_torreys", "chicago_basin",
                "blanca_group", "crestones", "maroon_bells",
                "belford_oxford_missouri", "redcloud_sunshine",
                "shavano_tabeguache", "kit_carson_group", "wilson_group"):
        assert len(clusters[cid]) >= 2, f"{cid} should be a multi-peak linkup"
    # A linkup is one push: all members share one CAIC zone.
    for cid, members in clusters.items():
        z = {m.caic_zone for m in members}
        assert len(z) == 1, f"cluster {cid} spans multiple CAIC zones: {z}"


def test_crux_peaks_have_late_windows():
    # Tier-2 (CRUX) peaks should not open before March (continental snowpack).
    for p in load_peaks():
        if p.tier == GatekeeperTier.CRUX:
            start_month = int(p.typical_window[0].split("-")[0])
            assert start_month >= 3, f"{p.name}: CRUX window opens too early"
