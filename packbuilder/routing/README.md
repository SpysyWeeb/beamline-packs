# Routing pack builder (Valhalla)

Builds one Valhalla routing graph for Pennsylvania and Ohio together, cuts it
into per-state routing packs, and tests installing, uninstalling and routing
with them. Results and the reasoning are in
[docs/experiments/01-routing.md](../../docs/experiments/01-routing.md).

Engine: Valhalla 3.9.1, the version bundled by valhalla-mobile 0.6.4 (the
Android/iOS wrapper), installed from the `pyvalhalla` 3.9.1 wheel. The wheel
contains the C++ build tools, so nothing has to be compiled. Works on Windows
and Linux (x86_64, aarch64).

## Prerequisites

- **Python 3.12** (the pinned pyvalhalla wheels are `cp312-abi3`;
  `tools/fetch_routing_env.py` stops on any other version). Windows x64,
  Linux x86_64 or Linux aarch64.
- **The OSM inputs** in `data/osm/` (not made by this track):
  `pennsylvania-261114.osm.pbf`, `ohio-261114.osm.pbf`, their `.md5` files,
  and `pennsylvania.poly` / `ohio.poly`. The repo's downloader is
  `tools/fetch_osm_extracts.ps1` (Geofabrik). The snapshot date `261114` is
  `SNAPSHOT` in `common.py`; the file names must match it. The `inputs` step
  waits up to an hour for the files to appear with a matching MD5, then stops
  with a `TimeoutError`.
- Network access for the `timezones` step (timezone-boundary-builder release,
  SHA-256 pinned) and the `boundaries` step (OSM API / Overpass).
- About 20 GB free disk for PA+OH: the tile build peaks at about 5.5 GB
  (temporary files + tiles), then merged input 0.6 GB, tiles 1.2 GB, packs
  1.2 GB, device 1.1 GB, test devices 5 GB, plus up to 4 GB of modified pack
  copies while the tests run (deleted at the end). The experiments
  (`exp_*`) add about 1.1-1.5 GB each.

## Setup (once)

```
python tools/fetch_routing_env.py        # creates .venv/routing, pinned + SHA-256-checked pyvalhalla
```

All commands below run from the repo root with that venv's Python
(`.venv/routing/Scripts/python` on Windows, `.venv/routing/bin/python` on Linux).
Heavy steps use 4 threads (`BEAMLINE_THREADS` to change). The weekly build of the packs repository
also sets `BEAMLINE_SNAPSHOT` (the extracts' YYMMDD, default `261114`) and `BEAMLINE_ROUTING_STATES`
(the graph's states, default `pa,oh`; a single state uses its own extract unmerged and its graph
build reads `<snapshot>-pa-v3.9.1-b...`). A phone holds states of one graph build only.

## Build (one command per step; finished steps are skipped unless `--force`)

```
python packbuilder/routing/build.py all
```

| step | what it does | output (under `data/routing/`) |
|---|---|---|
| `inputs` | waits until `data/osm/*-261114.osm.pbf` match their `.md5`, merges PA + OH and drops the objects both extracts contain (`merge_osm.py`), then opens the entrance pieces tagged as driveways (`driveways.py`; 4,112 ways in Pennsylvania's 2026-11-14 extract) | `osm/pa-oh-261114.osm.pbf` |
| `timezones` | downloads timezone-boundary-builder 2026d (SHA-256 pinned), builds the timezone DB with the wheel's own SQLite/SpatiaLite (`tzdb.py`) | `build/tz_world.sqlite` |
| `config` | `valhalla_build_config` with our paths, 4 threads, driving-only graph | `build/valhalla.json` |
| `boundaries` | complete US/PA/OH/neighbour boundary relations at the snapshot time (`fetch_boundaries.py`) | `osm/boundaries-261114.osm.pbf` |
| `admins` | `valhalla_build_admins` on those boundaries | `build/admins.sqlite` |
| `tiles` | `valhalla_build_tiles` on the merged extract | `build/valhalla_tiles/` |
| `packs` | `valhalla_build_extract -g` with each state polygon, plus a manifest (tile list, SHA-256, graph build id) | `packs/<state>/routing-<state>-261114.tar`, `manifest.json` |

Each stamp (`data/routing/build/stamps/<step>.json`) records the size and
mtime of the step's input files (and pinned values such as the timezone
release). A step whose recorded inputs changed is rebuilt, and that changes
the inputs of the steps after it. Stamps written before signatures existed
(the 2026-11-15 tiles and packs) are trusted as they are.

Experiments (not in `all`): `build.py tiles-unmerged` (two PBFs given directly
to `valhalla_build_tiles`), `build.py tiles-allmodes` (keep walking/cycling
edges), `build.py tiles-disk` (disk footprint before cleanup),
`compare_unmerged.py`, `measure_compression.py`, `find_crossings.py`.

## Install packs into the one tile set the engine loads

```
python packbuilder/routing/packs.py install pa          # first install: --layout tar (default) or dir
python packbuilder/routing/packs.py install oh
python packbuilder/routing/packs.py install pa oh       # release update: replaces both in one step
python packbuilder/routing/packs.py uninstall oh
python packbuilder/routing/packs.py list
```

The device folder (default `data/routing/device/`) then holds `routing.tar`
(or `tiles/`), `device.json` (layout and engine version) and a `valhalla.json`
whose tile path is relative to the device folder. Open the engine with
`packs.engine_config(device)`, which resolves that path. The engine settings
the device needs (matrix limits, hard exclusions, ...) come from
`device_config.json` and the `requires_config` of `presets.json`.

Rules `packs.py` enforces: all installed packs come from one graph build;
installing an installed state replaces it (an update); a pack built for
another Valhalla version than the engine is refused (manifest and every tile
header); every tile is checked against its manifest SHA-256; the layout is
fixed at the first install. Reopen the engine after an install or uninstall.

## Experiments and tests

```
python packbuilder/routing/route_eval.py crossings   # route across the PA/OH line at 6 crossings
python packbuilder/routing/route_eval.py presets     # presets.json x trips.json table
python packbuilder/routing/route_eval.py tune        # the sweeps behind presets.json
python packbuilder/routing/route_eval.py extras      # alternates, 4-stop trip, stop types
python packbuilder/routing/route_eval.py matrix      # 1x40 / 40x1 sources_to_targets
python packbuilder/routing/route_eval.py latency     # cold/warm latency, process memory
python packbuilder/routing/test_routing.py           # asserts the behaviours (25 checks, about 3-4 min)
```

## Data files (rules live here, not in code)

- `presets.json`: the six route presets as Valhalla `auto` costing options
  (or a candidate pool with a select rule, or a fallback with a condition),
  with the tuning notes, the engine settings they need (`requires_config`)
  and the app-side result checks (`checks`, e.g. warn when a route end is
  more than 1 km from the requested point). `route_lib.validate_preset`
  checks option names and ranges against Valhalla 3.9.1.
- `device_config.json`: engine settings for the device (matrix limits, no
  admin/timezone databases, single thread).
- `trips.json`: test places, the 21 fixed trips, the six border-crossing test
  points, the PA-only edge probes, the multi-stop and stop-type trips, the
  matrix corridor.
- `banner.json`: rules of the guidance banner (following-turn limits 0.5 mi
  and 1 min, lane-row rules, distance texts), read by the app's banner model
  (`android/routing/banner`, docs/experiments/09-routing-on-device.md).

The Android app copies `presets.json`, `device_config.json`, `banner.json`,
`trips.json` and the device `valhalla.json` with
`android/routing/scripts/sync_data.py`; edit them here, never the copies.

## Files

| file | role |
|---|---|
| `common.py` | paths, running the wheel's tools, wall time / memory measurement |
| `merge_osm.py` | streaming merge of sorted PBFs with de-duplication (pyosmium) |
| `driveways.py` | the entrance pieces of lots tagged `service=driveway` go into the graph's input as plain service roads (Valhalla closes driveways to through travel); the rule is in its head; `test_driveways.py` |
| `tzdb.py` | timezone DB without the spatialite CLI |
| `fetch_boundaries.py` | complete admin boundary relations (Overpass attic query, OSM API fallback) |
| `build.py` | the build steps above |
| `packs.py` | install / update / uninstall / list packs, device config |
| `route_lib.py` | requests from presets, preset candidate pools, route summaries |
| `route_eval.py` | the experiments |
| `test_routing.py` | the assertions |
| `find_crossings.py` | finds roads crossing the PA/OH line (used once to pick test points) |

Licences: pyvalhalla/Valhalla MIT, pyosmium BSD-2-Clause, shapely BSD-3-Clause,
psutil BSD-3-Clause; map data © OpenStreetMap contributors (ODbL 1.0); timezone
polygons from timezone-boundary-builder (ODbL 1.0). All of these are build-time
only; the device runs valhalla-mobile (MIT) with the tiles.
