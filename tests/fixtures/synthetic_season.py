"""Canned synthetic seasons for offline development and tests.

These wrap ``fkt_sim.conditions.SyntheticProvider`` with the three snowpack
characters the simulator backtests against, plus a tiny single-zone season for
fast unit tests. The synthetic fixture is the DEFAULT data source for the
pipeline until the real CAIC/Explorer/SNOTEL adapters are wired in Phase 8.
"""
from __future__ import annotations

from datetime import date

from fkt_sim.conditions import SeasonCharacter, SyntheticProvider
from fkt_sim.peaks import load_caic_zones

# The default season window (Section 9): mid-December to mid-June.
DEFAULT_START = date(2016, 12, 15)
DEFAULT_END = date(2017, 6, 15)


def used_zone_ids() -> list[str]:
    """CAIC zone_ids that the seeded 55 peaks actually touch."""
    return [z.zone_id for z in load_caic_zones().values() if z.used_in_seed]


def make_season(
    character: SeasonCharacter = SeasonCharacter.NEAR_NORMAL,
    *,
    start: date = DEFAULT_START,
    end: date = DEFAULT_END,
    seed: int = 1453,
    degraded_before: date | None = None,
) -> SyntheticProvider:
    """Build a full-state synthetic provider for one season character."""
    return SyntheticProvider(
        character,
        start=start,
        end=end,
        seed=seed,
        degraded_before=degraded_before,
    )


def three_test_seasons() -> dict[SeasonCharacter, SyntheticProvider]:
    """The good/bad/average trio, classified by snowpack character not depth."""
    return {c: make_season(c) for c in SeasonCharacter}


def tiny_season(zone: str = "sawatch") -> SyntheticProvider:
    """A short single-zone season (~2 weeks) for fast, focused unit tests.

    Includes a degraded-data cutoff partway through so tests can exercise the
    rose-missing fallback path.
    """
    return SyntheticProvider(
        SeasonCharacter.NEAR_NORMAL,
        start=date(2017, 3, 1),
        end=date(2017, 3, 14),
        seed=7,
        degraded_before=date(2017, 3, 5),
    )
