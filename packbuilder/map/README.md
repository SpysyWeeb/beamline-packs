# packbuilder/map: the map part of a state pack

Builds the vector basemap of a state pack as one PMTiles file, the whole-US
overview pack, and the offline style bundle the app draws them with. Results
and measurements: `docs/experiments/02-map.md` (first round) and
`docs/experiments/07-map-data.md` (second round: lane tags, overview pack,
state-line clipping, pack size).

- Tiles: [Planetiler](https://github.com/onthegomap/planetiler) 0.10.2 running the
  [Protomaps basemap profile](https://github.com/protomaps/basemaps) (tiles 4.15.2,
  BSD-3-Clause), compiled here from a pinned commit with a small patch of ours
  (`profile/beamline-profile.patch`), then a finish step of our own.
- Overview: Planetiler 0.10.2 `generate-custom` with `overview/overview.yml`, from
  Natural Earth and US Census TIGER/Line data only (public domain).
- Style: generated from `@protomaps/basemaps` 5.7.2 (BSD-3-Clause code, CC0 design),
  with Noto Sans glyphs (OFL-1.1) and the Protomaps v4 sprites (MIT-derived icons).
  `style/` and `style-bundle/` belong to the style engineer.
- Tools and libraries are pinned in `versions.json`, `requirements.txt`,
  `style/style_inputs.lock.json`, `tools/fetch_map_tools.py` and `tools/fetch_map_vendor.py`.
  The Census files are pinned by SHA-256 in `fetch_sources.py`.
- **Not pinned:** the auxiliary data inputs that `fetch_sources.py` downloads
  (OSM water and land polygons, updated daily; Natural Earth; Wikidata QRank,
  weekly; pgf-encoding). Their publishers only serve the latest file, so they
  cannot be pinned by hash in git. What was used is recorded instead: every
  download's SHA-256, size and Last-Modified go into
  `data/map/work/data/sources/manifest.json`, and that manifest is copied into
  each pack's `.build.json`. To reproduce a pack exactly, keep the source files
  together with the release.

## One-time setup (Windows, from the repo root)

```powershell
# shared inputs (not this track's script): Geofabrik extracts, .md5 and .poly files into data/osm/
pwsh tools/fetch_osm_extracts.ps1 -States pennsylvania,ohio -Date 261114
py -3.12 -m venv .venv/map
.venv/map/Scripts/python -m pip install -r packbuilder/map/requirements.txt
python tools/fetch_map_tools.py                          # Maven 3.9.16, go-pmtiles 1.31.2, planetiler.jar 0.10.2 into tools/bin/
.venv/map/Scripts/python packbuilder/map/build_profile.py # clone + patch + compile the Protomaps profile jar (about 1 min)
.venv/map/Scripts/python packbuilder/map/fetch_sources.py # Census files (52 MB, pinned) + about 2.5 GB of Natural Earth, water/land polygons, QRank
```

Java (Temurin 25) and Node.js must already be installed. Every step skips
itself when its output exists; `--force` redoes it.

## Build a state pack

```powershell
.venv/map/Scripts/python packbuilder/map/build_pack.py pa                  # recommended variant -> data/map/packs-v2/pa-261114-z14.pmtiles
.venv/map/Scripts/python packbuilder/map/build_pack.py oh
.venv/map/Scripts/python packbuilder/map/build_pack.py pa --variant z15    # other variants in build_config.json "variants"
.venv/map/Scripts/python packbuilder/map/lanes_check.py data/map/packs-v2/pa-261114-z14.pmtiles --state pa --coverage --direction --out-dir data/map/work/checks
.venv/map/Scripts/python packbuilder/map/tile_stats.py data/map/packs-v2/pa-261114-z14.pmtiles --deep 14 --out-dir data/map/work/checks
tools/bin/pmtiles/pmtiles.exe verify data/map/packs-v2/pa-261114-z14.pmtiles
```

The checks write their JSON reports next to the pack unless `--out-dir` is
given; use `--out-dir` when re-running them on a delivered pack folder.
`lanes_check.py --direction` checks the direction of every z14 feature with a
direction-dependent lane value against the OSM way it came from.
`build_times.py LOG...` prints Planetiler's own per-stage wall and CPU times
from build logs (used to measure the cost of the lane tags).

`build_pack.py` checks the OSM extract against its `.md5` file (it stops at once
if the file is missing; `--wait-minutes N` waits for a download in progress),
writes the state's outline from the Census TIGER/Line 2026 state file
(`census_clip.py`, `data/map/work/clip-v2/<state>.geojson`) and then runs two stages:

1. **Planetiler** writes `data/map/work/raw-v2/<pack>.pmtiles`, clipped to that
   outline with no buffer, with 4 threads and a 4 GB Java heap
   (`build_config.json`). The variant sets the max zoom, the Beamline profile
   options (lane tags, clamp of z15-only features into the max zoom, POI kinds
   to leave out, building detail) and the tile format (MVT or MLT). Wall time,
   peak memory and the hashes of every input go into
   `data/map/work/raw-v2/<pack>.build.json`. The landcover layer is fed an empty
   GeoPackage so that no CC-BY data and no extra credit line end up in the pack.
2. **`finish_pack.py`** writes the final `data/map/packs-v2/<pack>.pmtiles`: the
   header bounds and centre come from the clip polygon, and characters the
   style bundle has no glyphs for (everything above U+FFFF, variation selectors,
   the zero-width joiner) are removed from label text (MVT only; MLT tiles are
   copied unchanged). `<pack>.build.json` is the combined record and
   `<pack>.map.json` describes the map part for the app's pack manifest.

When the raw pack already exists, only stage 2 runs. `run_queue.py` runs a list
of build commands one after another (never two Planetiler runs at once).

### The profile patch (`profile/beamline-profile.patch`)

Every option is off unless `build_pack.py` passes it; with none, the patched
jar writes the stock tiles.

| Planetiler argument | Effect |
|---|---|
| `--beamline_lanes_minzoom=14` | roads with highway=motorway, trunk, primary and their links carry `lanes`, `lanes:forward`, `lanes:backward`, `lanes:both_ways` (integers) and `turn:lanes`, `turn:lanes:forward`, `turn:lanes:backward`, `turn:lanes:both_ways` (OSM strings) from z14. Lines with a direction-dependent lane value (`:forward`, `:backward`, `turn:lanes`) are not line-merged, so each keeps its OSM way's direction (patch version 2; version 1 merged them and could reverse a two-way road) |
| `--beamline_top_zoom_clamp=true` | POIs, house numbers and private roads that the stock profile only writes into z15 tiles go into the build's max zoom (z14), with no POI label grid at that zoom |
| `--beamline_poi_drop_kinds=...` | POI kinds that are not written (`build_config.json` "poi_drop_kinds": trees, benches, crossings, ...) |
| `--beamline_buildings_detail_at_top=true` | buildings at the max zoom are not merged (individual houses), like the stock z15 |

## US overview pack (zoom 0-7)

```powershell
.venv/map/Scripts/python packbuilder/map/overview/build_overview.py    # data/map/packs-v2/us-overview-ne512-tiger2026-z0-7.pmtiles
```

Land, ocean, lakes, national parks, country and state lines, state names and
cities from Natural Earth 5.1.2; interstates and other limited-access highways
(with interstate and US-route shields) from Census TIGER/Line 2026 primary
roads. Layers and attributes follow the Protomaps 4.x schema, so the same
style draws it below zoom 8 and the state packs from zoom 8.

## Pack size experiments

```powershell
.venv/map/Scripts/python packbuilder/map/size_report.py breakdown data/map/packs-v2/pa-261114-z15.pmtiles --out-dir data/map/work/checks
.venv/map/Scripts/python packbuilder/map/size_report.py option data/map/packs-v2/pa-261114-z14.pmtiles landuse_undrawn
```

`breakdown` shares each tile's stored bytes out over its layers and sub-groups;
`option` removes or simplifies features by the rules in `size_options.json`,
rewrites the whole pack and measures it.

## Style bundle (style engineer's folders)

```powershell
.venv/map/Scripts/python packbuilder/map/style/fetch_style_inputs.py   # pinned npm package, glyphs, sprites
node packbuilder/map/style/generate_style.mjs                          # writes packbuilder/map/style-bundle/
```

The styles are templates: replace `{{PACK_URL}}` and `{{STYLE_BASE_URL}}`
before giving the JSON to MapLibre (see `viewer/compose_style.js`).

## Several states

```powershell
.venv/map/Scripts/python packbuilder/map/seam_report.py data/map/packs-v2/pa-261114-z14.pmtiles data/map/packs-v2/oh-261114-z14.pmtiles --states pa oh --out data/map/work/seam/seam-v2.json
.venv/map/Scripts/python packbuilder/map/merge_packs.py OUT.pmtiles A.pmtiles B.pmtiles   # one merged file, if ever wanted
```

## Look at it

```powershell
python viewer/serve.py --packs data/map/packs-v2     # then open http://127.0.0.1:8765/
.venv/map/Scripts/python packbuilder/map/screenshots.py --views packbuilder/map/screenshot_views_v2.json   # PNGs in data/map/screenshots-v2/
.venv/map/Scripts/python packbuilder/map/screenshots.py --views packbuilder/map/screenshot_views_seam_before.json --packs data/map/packs
```

`screenshots.py` exits with 1 if a view has a MapLibre error, a console
message, a failed or 4xx/5xx request (such as a missing glyph range) or a
request that leaves 127.0.0.1. Views can add a lane debug overlay
(`"overlay": "lanes"`, or `"lanesdir"` which also marks each lane line's
direction) or serve the styles with `"encoding": "mlt"`.

## Files

| File | What |
|---|---|
| `versions.json` | pinned versions and licences of the profile, patch, Planetiler, styles, assets, tools |
| `build_config.json` | states, clip source, threads, heap, lane zoom, POI kinds to drop, build variants, finish-step settings |
| `fetch_sources.py` | auxiliary inputs (recorded in a manifest) and the SHA-256-pinned Census files |
| `build_profile.py`, `profile/beamline-profile.patch` | compiles the patched Protomaps profile jar with the portable Maven |
| `census_clip.py` | a state's TIGER/Line outline as GeoJSON for `--clip` |
| `boundary_compare.py` | distance between the OSM state line in a pack and the TIGER/Line outline |
| `build_pack.py` | runs Planetiler for one state and variant (measured by `measure.py`), then `finish_pack.py` |
| `finish_pack.py` | header bounds from the clip polygon, label text clean-up, optional layer removal |
| `run_queue.py` | runs build commands one after another |
| `build_times.py` | Planetiler's per-stage wall and CPU times from build logs |
| `lanes_check.py`, `lanes_checkpoints.json` | lane attributes in the tiles against the OSM ways (checkpoints, coverage, direction of every directional feature) |
| `size_report.py`, `size_options.json` | size per zoom/layer and measured size options |
| `overview/` | the US overview pack: Planetiler YAML schema (`overview.yml`), Census towns (`census_places.py`), build script |
| `poly2geojson.py` | Geofabrik `.poly` to GeoJSON (first round's clip) |
| `tile_stats.py` | sizes per zoom and layer, attribute coverage, name scripts, glyph check |
| `mvt.py` | small MVT protobuf reader/writer with geometry decoding (no dependencies) |
| `merge_packs.py` | merges overlapping packs into one PMTiles file |
| `seam_report.py` | what two packs share along a border, and points outside their own state |
| `screenshots.py`, `screenshot_views*.json` | headless rendering checks |
| `style/`, `style-bundle/` | style inputs and the generated offline style bundle (style engineer) |
