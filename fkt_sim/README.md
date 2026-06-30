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

## The 58 → 54 dedupe (the #1 review item)

Colorado has **53 ranked** 14ers (≥300 ft prominence). The five *named*
sub-prominence 14ers are **Mt. Cameron, Conundrum Peak, North Eolus, El Diente
Peak, and North Maroon Peak**. The canonical "54" that the Davenport /
Jespersen "ski all 54" projects use is the **53 ranked peaks + North Maroon**.

So from the 58-name list (the prompt's seed table, which also contained an
artifact duplicate `Mt. Princeton…` row) we drop exactly four:

```
DROPPED = { Mt. Cameron, Conundrum Peak, North Eolus, El Diente Peak }
```

The one genuinely debatable call is **El Diente vs North Maroon**. We keep
North Maroon (independent trailhead and ski line) and drop El Diente (its
standard ski descent is the Mt. Wilson traverse — dependent on Mt. Wilson). If
review prefers the El-Diente-inclusive list, it's a one-line change in the data
builder and one row in `peaks_seed.csv`.

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
