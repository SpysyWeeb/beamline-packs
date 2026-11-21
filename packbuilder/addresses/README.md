# packbuilder/addresses

Builds the street-address part of a state pack. Two files, same schema apart from two columns:

- `data/addresses/pa-addresses-slim.sqlite`: **the file a device ships** (about 290 MB for PA).
- `data/addresses/pa-addresses.sqlite`: the full build output, which also has the diagnostic columns
  `addresses.source_id` (OSM id, NAD UUID) and `addresses.zip_src`. Other tracks and the evaluations read this one.

Results and measurements are in `docs/experiments/04-addresses.md`.

The search track owns full-text search and ranking; this folder owns the address
data, its normalisation and the house-number lookup. The search index joins
`streets` (one row per street spelling per locality) and resolves house numbers
through `addrlib.lookup`.

## Run

Windows, from the repo root (Python 3.12):

```
py -3.12 -m venv .venv/addresses
.venv/addresses/Scripts/python.exe -m pip install -r packbuilder/addresses/requirements.txt
.venv/addresses/Scripts/python.exe packbuilder/addresses/run.py          # all steps, skips finished ones
.venv/addresses/Scripts/python.exe packbuilder/addresses/run.py --list   # one command per step
.venv/addresses/Scripts/python.exe packbuilder/addresses/run.py --force tiger build
.venv/addresses/Scripts/python.exe -m unittest discover -s packbuilder/addresses/tests -v
```

On Linux use `.venv/addresses/bin/python`. Nothing is Windows-specific.

### Another state, another folder (round 3)

`common.py` reads three environment variables, so every step process of `run.py` sees the same:
`BEAMLINE_ADDR_STATE` (a key of `sources.json` "states": `pa`, the default, or `oh`),
`BEAMLINE_ADDR_DATA` (the data folder written; default `data/addresses`) and
`BEAMLINE_ADDR_INPUTS` (a data folder only read: a download or work file missing from the data
folder is taken from there). Work files and outputs are named by the state (`osm_oh.sqlite`,
`oh-addresses-slim.sqlite`). Web requests carry a generic User-Agent.

```
BEAMLINE_ADDR_STATE=oh BEAMLINE_ADDR_DATA=data/r3-p/addresses-oh BEAMLINE_ADDR_INPUTS=data/addresses     .venv/addresses/Scripts/python packbuilder/addresses/run.py --only download extra osm tiger build
```

The build writes the pack format 3 meta (`docs/pack-format.md`): `key_fingerprint`
(`key_fingerprint.py` over `addrlib/fingerprint_spellings.json`, the same value the search builder
writes), `normaliser_sha256` (LF-normalised), `credit` (the short credit of the manifest's
addresses part) and the indexes `localities(city_key)`, `localities(muni_key)`. The NAD step for
Ohio (5.5 million rows on the hosted service, about 9 h at its measured 166 rows/s) was not run in
round 3: the OH file has OSM points and TIGER ranges only.

| Step | Script | Reads | Writes |
|---|---|---|---|
| download | `step_download.py --skip-nad` | Census, TIGER (URLs in `sources.json`) | `data/addresses/download/`, `sources.lock.json` (SHA-256) |
| nad | `step_nad.py` | USDOT NAD r24 hosted view (PA rows only) | `download/nad_service/*.jsonl.gz`, `work/nad_pa.sqlite` |
| extra | `step_extra.py` | open point layers in `sources.json` `extra_point_sources` (City of Scranton 911, PDDL) | `work/extra_pa.sqlite` |
| osm | `step_osm.py` | `data/osm/pennsylvania-261114.osm.pbf` (waits for its MD5) | `work/osm_pa.sqlite` |
| tiger | `step_tiger.py` | TIGER/Line 2026 ADDRFEAT, 67 counties | `work/tiger_pa.sqlite` |
| coverage | `step_coverage.py` | the above + Census housing units | `reports/coverage_*.{csv,json}`, `county_types.json` |
| build | `step_build.py` | work DBs | `data/addresses/pa-addresses.sqlite` and `pa-addresses-slim.sqlite` |
| eval_accuracy | `step_eval_accuracy.py` | work DBs | `reports/accuracy.json`, `holdout_sample.csv` |
| eval_parser | `step_eval_parser.py` | the pack file | `reports/e2e.json`, `e2e_cases.csv` (with the target in the pack, and with it removed) |
| estimate | `step_estimate.py` | metrics, Census listing | `reports/estimate.json` |
| variants | `step_variants.py` | both pack files | `work/variant_{points,ranges}_only.sqlite`, `reports/variants.json` (sizes, and what each variant answers) |

`report_tables.py` prints the markdown tables used in the experiment report.

Every step writes `data/addresses/metrics/<step>.json` (wall time, CPU time, peak
working set). `step_download.py --only nad` would fetch the 8.5 GB national NAD
text file instead (resumable, 4 range requests); not needed for one state.

Normalisation edits change street keys: after editing `addrlib/us_street_norm.json`
re-run `tiger` and `build` (`run.py --force tiger build`).

## Files

| File | Ships to device | What |
|---|---|---|
| `addrlib/normalize.py` | yes (to be ported to Kotlin) | street keys, house numbers, place keys, ZIP, units |
| `addrlib/us_street_norm.json` | yes | all word tables: directionals (and the weak ones), Saint prefix words, street types, ordinals, highway forms, unit designators, house-number fractions and block placeholders, states, country words, city placeholders, place words |
| `addrlib/parser.py` | yes | rule-based typed-address parser |
| `addrlib/lookup.py`, `addrlib/lookup_config.json` | yes | house-number lookup; weights and thresholds are data |
| `addrlib/geo.py` | yes | edge BLOB decoding, interpolation, distances |
| `step_*.py`, `common.py`, `geoassign.py`, `run.py` | no | build and evaluation (numpy, shapely, pyosmium, pyshp) |

`addrlib` uses only the Python standard library (json, re, sqlite3, struct,
math, unicodedata), so it runs unchanged on a comma device.

## Output schema (`pa-addresses.sqlite`, schema_version 1)

```
meta(key, value)                       build info, attribution, input hashes, stats
localities(id, zip, city, city_key, city_src, municipality, muni_key, county_fips)
streets(id, street, street_key, street_core, locality_id, n_points, n_ranges, lat_e6, lon_e6)
addresses(id, street_id, hn, hn_int, unit, lat_e6, lon_e6, source, source_id, zip_src)   -- accuracy 'point'
ranges(id, street_id, edge_id, side, hn_from, hn_to, hn_lo, hn_hi, parity, source)       -- accuracy 'interpolated'
edges(id, geom)                        geom = little-endian int32 (lon, lat) micro-degree pairs
places(place_key, name, kind, zip, county_fips, n)   postal city / municipality -> ZIP
VIEW address_rows(id, hn, street, street_key, unit, city, municipality, zip, county_fips, lat, lon, source, accuracy)
```

- `street` is the name as the source wrote it; `street_key` is `normalize.street_key(street)`
  ('North Main Street' and 'N Main St' -> `n main st`; 'PA 8', 'State Route 8', 'SR 0008' -> `sr 8`).
  `street_core` drops leading/trailing directionals and the trailing type (`main`), for loose matching.
- `hn` is canonical: lower-case letter suffix without space ('123a'), ranges '123-125', fractions '123 1/2'.
- `city` is the source's city when it has one (OSM `addr:city`, NAD `Post_City`), else the most
  common source city of the row's ZIP (`city_src='zip'`), else the municipality.
- `municipality` is the TIGER/Line 2026 county subdivision (township, borough, city) the point lies in.
- `source`: `osm` (OSM point, node/way/relation), `nad` (NAD point that OSM lacks), `scranton911`
  (Scranton 911 point that neither has); in `ranges`: `tiger` or `osm` (addr:interpolation).
- `zip_src`: `source`, `nad` / `scranton911` (OSM point matched to such a point), `tiger_street` / `tiger_edge`
  (ZIP of the nearest TIGER edge side within 150 m, same street / any street), NULL.
- Coordinates are integers in micro-degrees (`lat_e6 / 1e6`).

Joining from search: `streets.street_key` / `street_core` for the street, `localities` for
ZIP/city/municipality, then `AddressIndex.resolve(parsed)` for the house number.

The slim file has the same tables and view; only `addresses.source_id` and `addresses.zip_src`
are missing, and `meta.variant` says which file it is. To drop the NAD points from a pack (if
their licence is ever in doubt): `DELETE FROM addresses WHERE source = 'nad'`.

## Lookup contract (`addrlib.lookup.AddressIndex`)

- Each result has `accuracy` (`point`, `interpolated`, `street`), `match` (`exact_key`,
  `highway_alternative`, `core_key`, `key_variant`, `fuzzy_key`) and `locality_match`.
- **Check `locality_match`.** When the typed street is not found in the typed town, the lookup
  still returns the best match elsewhere in the state with `locality_match: false`
  (`1 Saint Marys St Erie` gives St Marys St in Phoenixville). Show it as a guess, not an answer.
- A typed state other than the pack's (`626 State St Erie OH` on the PA pack) sets
  `state_mismatch: true` and `locality_match: false` on every result.
- A house number above `max_house_number` (lookup_config.json) is not used; the street is
  returned with `house_number_rejected`.
- Input with no street (`Erie PA 16501`, `16501`) parses to a city or ZIP and returns `[]`:
  place search is the search track's job.
- Works on every variant file: without `ranges`/`edges` it answers from points only, without
  `addresses` from ranges only.

## Data sources and licences

| Source | Used for | Licence (checked at) |
|---|---|---|
| OpenStreetMap, Geofabrik PA extract 2026-11-14 | address points | ODbL 1.0 (openstreetmap.org/copyright) |
| Census TIGER/Line 2026 ADDRFEAT, COUSUB | ranges, municipalities | not copyrighted; credit "U.S. Census Bureau" (use constraints in each file's ISO metadata) |
| Census 2025 cartographic boundary counties | county polygons, land area | as above (US Government work) |
| Census PEP Vintage 2025 housing units (CO-EST2025-HU-42, NST-EST2025-HU) | coverage denominator | US Government work, public domain |
| USDOT National Address Database r24 | extra points (10 PA counties) | US Public Domain (USDOT DCAT record, `license: http://www.usa.gov/publicdomain/label/1.0/`); data.gov parent record CC0 1.0 |
| City of Scranton GIS 911 address points | extra points (Lackawanna County) | PDDL 1.0 ("GIS is a public domain and may be copied without permission"), ArcGIS item licenseInfo; only address fields are fetched |

Python packages (build-time only unless noted): see the report for the full table.
