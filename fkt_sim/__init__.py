"""Colorado 14er splitboard FKT season simulator.

Backtests a "ski/splitboard all 54 Colorado 14ers in the fastest single season,
summit descents, conditions-permitting" project against historical avalanche and
weather data. See the build prompt for the full design.

Phase 1 (this commit) ships the domain model and seed data only:
``peaks.py`` plus ``data/peaks_seed.csv`` (deduped to 55 objectives: 54 summits
with El Diente + Mt. Wilson as one linked push), ``caic_zones.csv``,
and ``trailheads.csv``.
"""

from fkt_sim.peaks import (
    Aspect,
    CaicZone,
    GatekeeperTier,
    Line,
    Peak,
    Trailhead,
    cluster_map,
    load_caic_zones,
    load_peaks,
    load_trailheads,
)

__all__ = [
    "Aspect",
    "CaicZone",
    "GatekeeperTier",
    "Line",
    "Peak",
    "Trailhead",
    "cluster_map",
    "load_caic_zones",
    "load_peaks",
    "load_trailheads",
]
