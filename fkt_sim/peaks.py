"""Domain model for the Colorado 14er splitboard FKT simulator.

This module defines the core entities (`Aspect`, `GatekeeperTier`, `Line`,
`Peak`) and the loaders that build them from the seed CSVs in ``data/``.

Provenance note (see Section 7 of the build prompt): every ``Line`` loaded from
``peaks_seed.csv`` carries ``verified=False``. The aspect / angle / window /
continuity values are best-effort SEEDS and MUST be checked against an
authoritative source (Dawson's ski guide, 14ers.com, the CAIC zone polygons)
before any simulation output is treated as real. The report layer is required
to loudly flag any peak in a final schedule whose line is still unverified.
"""
from __future__ import annotations

import csv
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

# Repo-relative default data directory: <repo>/data
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class Aspect(str, Enum):
    N = "N"
    NE = "NE"
    E = "E"
    SE = "SE"
    S = "S"
    SW = "SW"
    W = "W"
    NW = "NW"


class GatekeeperTier(int, Enum):
    ROUTINE = 0   # road-accessible, low-consequence, high-volume
    MODERATE = 1  # committing but not season-defining
    CRUX = 2      # season-defining; needs a specific rare safe window


class Line(BaseModel):
    """A specific ski/splitboard descent line on a peak."""

    name: str
    primary_aspect: Aspect
    secondary_aspects: list[Aspect] = Field(default_factory=list)
    max_angle_deg: int
    summit_continuous: bool          # does snow reach the TRUE summit normally?
    fallback_line: str | None = None
    notes: str = ""
    # Provenance: seed line attributes are unverified until checked against an
    # authoritative descent source. Never present output from unverified lines
    # as real without flagging it.
    verified: bool = False

    @property
    def all_aspects(self) -> list[Aspect]:
        """Primary + secondary aspects the line spans (deduped, order-stable)."""
        out: list[Aspect] = [self.primary_aspect]
        for a in self.secondary_aspects:
            if a not in out:
                out.append(a)
        return out


class Peak(BaseModel):
    """A 14er objective with its candidate descent line(s)."""

    name: str
    elevation_ft: int
    range_name: str          # Sawatch, Elk, Sangre de Cristo, San Juan, Front, Mosquito/Tenmile
    caic_zone: str           # FK to caic_zones.csv (zone_id)
    trailhead_id: str        # FK to trailheads.csv
    cluster_id: str          # peaks reachable in one push share a cluster_id
    lines: list[Line]
    tier: GatekeeperTier
    typical_window: tuple[str, str]   # (earliest_md, latest_md) as "MM-DD"
    approach_miles_rt: float
    vert_ft: int

    @field_validator("typical_window")
    @classmethod
    def _validate_window(cls, v: tuple[str, str]) -> tuple[str, str]:
        for md in v:
            _parse_md(md)  # raises on malformed "MM-DD"
        return v

    @property
    def best_line(self) -> Line:
        """The primary candidate line (first listed). Scheduler/go-no-go entry."""
        return self.lines[0]

    @property
    def all_verified(self) -> bool:
        return all(line.verified for line in self.lines)


# --------------------------------------------------------------------------- #
# Supporting reference data (zones, trailheads)
# --------------------------------------------------------------------------- #
class CaicZone(BaseModel):
    zone_id: str
    display_name: str
    region: str
    used_in_seed: bool
    notes: str = ""


class Trailhead(BaseModel):
    trailhead_id: str
    name: str
    lat: float
    lon: float
    elev_ft: int
    winter_gate_miles_add: float = 0.0
    notes: str = ""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _parse_md(md: str) -> tuple[int, int]:
    """Parse an 'MM-DD' month-day string into (month, day); raise if invalid."""
    parts = md.split("-")
    if len(parts) != 2:
        raise ValueError(f"window date must be 'MM-DD', got {md!r}")
    month, day = int(parts[0]), int(parts[1])
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ValueError(f"window date out of range: {md!r}")
    return month, day


def _parse_aspects(raw: str) -> list[Aspect]:
    """Parse a pipe-separated aspect field ('N|E') into a list of Aspect."""
    raw = (raw or "").strip()
    if not raw:
        return []
    return [Aspect(tok.strip()) for tok in raw.split("|") if tok.strip()]


def _parse_bool(raw: str) -> bool:
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "t"}


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #
def load_caic_zones(path: str | Path | None = None) -> dict[str, CaicZone]:
    path = Path(path) if path else DATA_DIR / "caic_zones.csv"
    out: dict[str, CaicZone] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            zone = CaicZone(
                zone_id=row["zone_id"].strip(),
                display_name=row["display_name"].strip(),
                region=row["region"].strip(),
                used_in_seed=_parse_bool(row["used_in_seed"]),
                notes=row.get("notes", "").strip(),
            )
            out[zone.zone_id] = zone
    return out


def load_trailheads(path: str | Path | None = None) -> dict[str, Trailhead]:
    path = Path(path) if path else DATA_DIR / "trailheads.csv"
    out: dict[str, Trailhead] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            th = Trailhead(
                trailhead_id=row["trailhead_id"].strip(),
                name=row["name"].strip(),
                lat=float(row["lat"]),
                lon=float(row["lon"]),
                elev_ft=int(row["elev_ft"]),
                winter_gate_miles_add=float(row.get("winter_gate_miles_add", 0) or 0),
                notes=row.get("notes", "").strip(),
            )
            out[th.trailhead_id] = th
    return out


def load_peaks(
    path: str | Path | None = None,
    *,
    zones: dict[str, CaicZone] | None = None,
    trailheads: dict[str, Trailhead] | None = None,
    validate_refs: bool = True,
) -> list[Peak]:
    """Load the 55-objective seed list (54 summits; El Diente links with Mt. Wilson).

    Each CSV row carries a single (primary) seed line, flattened into columns.
    When ``validate_refs`` is True, every peak's ``caic_zone`` and
    ``trailhead_id`` must resolve against the zone/trailhead tables.
    """
    path = Path(path) if path else DATA_DIR / "peaks_seed.csv"
    if validate_refs:
        zones = zones if zones is not None else load_caic_zones()
        trailheads = trailheads if trailheads is not None else load_trailheads()

    peaks: list[Peak] = []
    seen: set[str] = set()
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            name = row["name"].strip()
            if name in seen:
                raise ValueError(f"duplicate peak in seed CSV: {name!r}")
            seen.add(name)

            if validate_refs:
                if row["caic_zone"].strip() not in zones:  # type: ignore[operator]
                    raise ValueError(
                        f"{name}: caic_zone {row['caic_zone']!r} not in caic_zones.csv"
                    )
                if row["trailhead_id"].strip() not in trailheads:  # type: ignore[operator]
                    raise ValueError(
                        f"{name}: trailhead_id {row['trailhead_id']!r} not in trailheads.csv"
                    )

            line = Line(
                name=row["line_name"].strip(),
                primary_aspect=Aspect(row["primary_aspect"].strip()),
                secondary_aspects=_parse_aspects(row.get("secondary_aspects", "")),
                max_angle_deg=int(row["max_angle_deg"]),
                summit_continuous=_parse_bool(row["summit_continuous"]),
                fallback_line=(row.get("fallback_line") or "").strip() or None,
                notes=row.get("line_notes", "").strip(),
                verified=_parse_bool(row.get("verified", "0")),
            )
            peak = Peak(
                name=name,
                elevation_ft=int(row["elevation_ft"]),
                range_name=row["range_name"].strip(),
                caic_zone=row["caic_zone"].strip(),
                trailhead_id=row["trailhead_id"].strip(),
                cluster_id=row["cluster_id"].strip(),
                lines=[line],
                tier=GatekeeperTier(int(row["tier"])),
                typical_window=(
                    row["typical_window_start"].strip(),
                    row["typical_window_end"].strip(),
                ),
                approach_miles_rt=float(row["approach_miles_rt"]),
                vert_ft=int(row["vert_ft"]),
            )
            peaks.append(peak)
    return peaks


def cluster_map(peaks: list[Peak]) -> dict[str, list[Peak]]:
    """Group peaks by ``cluster_id`` (single-push linkups share a cluster)."""
    out: dict[str, list[Peak]] = {}
    for p in peaks:
        out.setdefault(p.cluster_id, []).append(p)
    return out
