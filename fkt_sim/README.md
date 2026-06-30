# Colorado 14er Splitboard FKT Season Simulator

Backtests *"ski/splitboard all 54 Colorado 14ers in the fastest single season,
summit descents, conditions-permitting"* against historical avalanche and
weather data, to estimate the **theoretical fastest possible season** for past
winters. Benchmark to beat: Josh Jespersen's 2017 run (~138 days, Jan 3 – May
21).

## Status — Phase 1 (domain model + seed data)

This is the first slice of the [build plan](#build-order). It ships only the
data model and the seed datasets; the conditions adapters, go/no-go engine,
scheduler, and reports come in later phases.

```
fkt_sim/peaks.py        # Peak / Line / Aspect / GatekeeperTier + CSV loaders
data/peaks_seed.csv     # the 54-peak Davenport ski list (deduped, see below)
data/caic_zones.csv     # the 10 real CAIC backcountry forecast zones
data/trailheads.csv     # approximate trailhead coordinates
tests/test_peaks_load.py
```

Run the tests:

```bash
pip install -r requirements.txt
python -m pytest tests/test_peaks_load.py -q
```

## ⚠️ Data provenance — read before trusting any value

Per Section 7 of the build prompt, **the seed line attributes are NOT
authoritative.** Every `Line` loaded from `peaks_seed.csv` carries
`verified=False`. The `primary_aspect`, `max_angle_deg`, `summit_continuous`,
and `typical_window` values are best-effort estimates from general
mountaineering knowledge and must be checked against Dawson's *Guide to
Colorado's Fourteeners* / wildsnow, the 14ers.com route pages, and the actual
CAIC zone polygons before any simulation output is treated as real. The most
reliable seed column is `tier`; the least reliable are the angle/window columns.

Trailhead coordinates and `winter_gate_miles_add` are likewise approximate
seeds. CAIC zone assignments for the Needle Mountains (Chicago Basin) and Pikes
Peak are operational best-guesses and flagged in `caic_zones.csv`.

## The 58 → 55 dedupe

Colorado has **53 ranked** 14ers (≥300 ft prominence). The five *named*
sub-prominence 14ers are **Mt. Cameron, Conundrum Peak, North Eolus, El Diente
Peak, and North Maroon Peak**.

Per review we **keep both El Diente and North Maroon**. El Diente's standard
ski descent is the Mt. Wilson ↔ El Diente traverse, so it is bagged in **one
push** with Mt. Wilson — both share the `wilson_group` cluster and the
scheduler treats the linkup as a single super-objective governed by its
strictest member. From the 58-name list (the prompt's seed table, which also
contained an artifact duplicate `Mt. Princeton…` row) we therefore drop only
the three trivial bumps:

```
DROPPED = { Mt. Cameron, Conundrum Peak, North Eolus }
```

This yields **55 named objectives** — 54 "summits to ski" in the project sense,
with El Diente + Mt. Wilson counting as a single linked push.

## Data model notes

- A `Peak` carries a list of `Line`s; the seed CSV provides one primary line
  per peak (flattened into columns) plus a `fallback_line` name. Multiple
  first-class lines per peak can be added later without changing the model.
- `cluster_id` groups peaks baggable in a single push (e.g. `mosquito_decalibron`,
  `grays_torreys`, `chicago_basin`, `blanca_group`, `crestones`, `maroon_bells`).
  The scheduler will treat a cluster as one "super-objective" with combined
  vert/miles and the *strictest* go/no-go among its members.
- `caic_zone` is a foreign key into `caic_zones.csv`; `trailhead_id` into
  `trailheads.csv`. The loader validates both references.

## Build order

1. **`peaks.py` + seed CSVs (this phase).** ← stop for review here
2. `conditions.py` contracts + a synthetic season fixture (offline pipeline).
3. `gonogo.py` + tests (each veto path + degraded-data fallback).
4. `travel.py` (drive-time matrix, clusters, effort budget).
5. `scheduler.py` (greedy, then look-ahead replanner).
6. `simulate.py` end-to-end on the synthetic fixture.
7. `report.py` (Gantt, binding-constraint analysis, fragility).
8. Wire real CAIC/Explorer/SNOTEL adapters; verify critical-path lines; run
   three seasons classified by snowpack character.
