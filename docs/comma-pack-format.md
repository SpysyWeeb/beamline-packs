# Beamline on a comma: the pack folder and the comma catalog (contract)

One contract for three sides: the pack builders (`packbuilder/comma`, run by the weekly build of the
public packs repository `SpysyWeeb/beamline-packs`), the downloader of a StarPilot build that offers
Beamline maps, and that build's readers (routes, search, the road map). Version 1. Anything not written here is not promised.

## 1. The pack folder on the device

`<root>` is `/data/media/0/starpilot/maps/beamline` on a comma (`beamline.beamline_root()` in StarPilot's navigation code).
One state is installed at a time in version 1.

```
<root>/pack.json              what is installed (section 3)
<root>/routing.tar            Valhalla routing tiles of the state (valhalla-tar/1)
<root>/pylib/                 the unpacked pyvalhalla wheel for the device's processor
<root>/search.sqlite          places and streets (beamline-search-sqlite/1)
<root>/addresses.sqlite       house numbers (optional)
<root>/search/                Beamline's search code: query.py, lines.py, normalize.py, ranking.json, abbreviations.json
<root>/roads/<z>/<x>/<y>.rt   road-line tiles in StarPilot's own format (section 4), zooms 8, 11 and 14
<root>/mapd/<lat>/<lon>.tar.gz   the same map in Mapd's format, as Mapd's group archives (section 5)
```

A reader must treat a missing piece as "that feature is not installed", never as an error.
`<root>/.staging-*` and `<root>/.old-*` are the installer's and are never read.

## 2. The comma catalog

`comma.json`, published in the packs repository's `catalog` release beside `catalog.json` (which stays
the phone app's and is not changed by this). A downloader reads only `comma.json`:

    https://github.com/SpysyWeeb/beamline-packs/releases/download/catalog/comma.json

```json
{
  "format": "beamline-comma-catalog/1",
  "serial": 20261122101500,
  "generated_utc": "2026-11-22T10:15:00Z",
  "licence": {"id": "ODbL-1.0", "name": "Open Database License 1.0",
              "url": "https://opendatacommons.org/licenses/odbl/1-0/",
              "notice": "Map data © OpenStreetMap contributors, available under the Open Database License."},
  "engines": [
    {"valhalla_version": "3.9.1", "machine": "aarch64", "python": "cp312",
     "file": "pyvalhalla-3.9.1-cp312-abi3-manylinux_2_28_aarch64.whl",
     "url": "https://files.pythonhosted.org/packages/.../pyvalhalla-3.9.1-cp312-abi3-manylinux_2_28_aarch64.whl",
     "bytes": 34736379, "sha256": "…64 hex…"}
  ],
  "packs": [
    {"id": "us-pa", "state": "PA", "name": "Pennsylvania", "pack_version": 202611221015,
     "osm_snapshot": "2026-11-21", "bounds": [-80.519851, 39.719799, -74.689561, 42.516072],
     "valhalla_version": "3.9.1",
     "credit": "© OpenStreetMap contributors; U.S. Census Bureau TIGER/Line; USDOT National Address Database; City of Scranton GIS",
     "parts": [
       {"name": "routing", "file": "routing.tar", "optional": false, "bytes": 584673280, "sha256": "…",
        "transfer": {"path": "../us-pa-202611221015/routing.tar.gz", "encoding": "gzip", "bytes": 222852341, "sha256": "…"}},
       {"name": "search", "file": "search.sqlite", "optional": false, "bytes": 0, "sha256": "…", "transfer": {"…": "…"}},
       {"name": "addresses", "file": "addresses.sqlite", "optional": true, "bytes": 0, "sha256": "…", "transfer": {"…": "…"}},
       {"name": "search_code", "file": "search-code.tar", "optional": false, "bytes": 0, "sha256": "…", "transfer": {"…": "…"}},
       {"name": "roads", "file": "roads.tar", "optional": false, "bytes": 0, "sha256": "…", "transfer": {"…": "…"}},
       {"name": "mapd", "file": "mapd.tar", "optional": true, "bytes": 0, "sha256": "…", "transfer": {"…": "…"},
        "mapd": {"min_lat": 38, "min_lon": -82, "max_lat": 44, "max_lon": -74, "source_date": "2026-11-21"}}
     ]}
  ]
}
```

Rules (the same as the phone catalog's, `beamline-catalog/1`, where that has one):

- `serial` only grows; a reader keeps the highest it has seen and refuses a lower one.
- `transfer.path` is relative to the URL of `comma.json`; `encoding` is `identity` or `gzip`. `bytes` and
  `sha256` of the part are of the file **as installed** (after gunzip); those inside `transfer` are of the
  file as downloaded. Both are checked.
- A part of every pack shares its files with the phone's pack where the file is the same (`routing`,
  `search`, `addresses`): the comma downloads the very same release assets.
- `engines`: the downloader takes the entry whose `valhalla_version` equals the pack's, whose `machine`
  equals `platform.machine()` and whose `python` is `cp312`; none means "routes are not available for
  this device", the other parts still install. The wheel is fetched from `url` and checked against
  `bytes` and `sha256`.
- Unknown keys and unknown part names are ignored. A newer `format` is refused whole.
- Every part's data credit is in the pack's `credit`; StarPilot shows it where it shows the map.

### What the three comma-only parts are

- `search_code` (`search-code.tar`, uncompressed POSIX tar, members at the top level, no folders): the
  five files of `<root>/search/`, copied from `packbuilder/search` of the commit that built the pack's
  search file.
- `roads` (`roads.tar`, uncompressed POSIX tar): members `<z>/<x>/<y>.rt` for z in 8, 11 and 14, one per
  tile of the state's map that has at least one road (section 4). Installed by unpacking into `<root>/roads/`.
- `mapd` (`mapd.tar`, uncompressed POSIX tar): members `<lat>/<lon>.tar.gz`, Mapd's group archives for
  the whole two-degree groups the state touches (section 5). Installed by unpacking into `<root>/mapd/`.
  The part's `mapd` object carries what Mapd's admission command needs.

An unpacker refuses a tar with an absolute member, a `..` component, a link, a device or a member
outside the names above, and never follows links.

## 3. `pack.json` (written by the installer; read by StarPilot's navigation code and by Galaxy)

```json
{"format": "beamline-comma-pack/1", "id": "us-pa", "name": "Pennsylvania", "pack_version": 202611221015,
 "osm_snapshot": "2026-11-21", "valhalla_version": "3.9.1", "credit": "…",
 "parts": {"routing": "…sha256 as installed…", "search": "…", "addresses": "…", "search_code": "…", "roads": "…", "mapd": "…"},
 "engine": "pyvalhalla-3.9.1-cp312-abi3-manylinux_2_28_aarch64.whl",
 "mapd": {"min_lat": 38, "min_lon": -82, "max_lat": 44, "max_lon": -74, "source_date": "2026-11-21", "admitted": false}}
```

`parts` lists only what is installed. `beamline.pack_status()` reads `name` and
`osm_snapshot`; it must keep working with this file and with the older one that has only those keys.

## 4. Road tiles (`.rt`)

StarPilot's own road-line tile, read by its `road_tiles.decode_road_tile` (format `SPRT`, version 1;
`openpilot/starpilot/navigation/road_tiles.py` at StarPilot commit 008b0cb). One file per Web Mercator
tile; coordinates in tile units 0..4096; five classes. Zoom 14 is what StarPilot's driving map reads
(its `DATA_ZOOM`); zooms 8 and 11 are overviews for Galaxy's map in a browser, which cannot draw a
whole state from zoom-14 tiles. From Beamline's map tiles (`map.pmtiles`, schema protomaps-basemap 4,
layer `roads`), each zoom from the map's tiles of that same zoom (they are already thinned to the
roads that matter at that scale):

| StarPilot class | number | Beamline `roads` features |
|---|---|---|
| motorway | 0 | `kind_detail` motorway, trunk (not links) |
| major | 1 | `kind_detail` primary, secondary |
| street | 2 | `kind_detail` tertiary, residential, unclassified, living_street |
| link | 3 | `is_link` true, or `kind_detail` ending in `_link` |
| minor | 4 | `kind_detail` service, track, and the other `minor_road` kinds (driveway, alley, parking aisle) |

The class is decided by `kind_detail` alone, whatever `kind` the map files the feature under: the map
puts `track` under kind `path` and `living_street` under kind `other`, and both are taken. Every other
feature of kind `path`, `rail`, `ferry`, `aeroway`, `pier` and `other` is not a road and is left out
(footpaths, cycleways, busways and the like).
Only line geometry is taken. A tile without roads has no file. Each file must decode with StarPilot's
decoder and stay under its 2 MB limit.

## 5. Mapd archives

Made with Mapd's own generator (`mapd generate`, built from StarPilot's `mapd_repo`) from the same
OpenStreetMap extract as the pack, after Mapd's own preparation: only ways whose `highway` is one of
motorway, trunk, primary, secondary, tertiary, unclassified, residential and their `_link` kinds, with
every way's node coordinates stored on the way. Bounds are whole two-degree groups; every
quarter-degree cell of every group is present (empty cells too). Each group is one gzip tar
`<lat>/<lon>.tar.gz` whose members are `offline/<lat>/<lon>/<cell>`. On the device:

```
mapd --snapshot-admit --offline-root /data/media/0/starpilot/maps/offline \
  --min-lat <min_lat> --min-lon <min_lon> --max-lat <max_lat> --max-lon <max_lon> \
  --archive-dir <root>/mapd --source-date <source_date>
```

Admission is Mapd's; the installer runs it only when StarPilot's packaged `mapd` is present and the
offline root exists, and records the result in `pack.json` (`mapd.admitted`). Known limit: the
archives cover whole groups but hold one state's roads, so they replace what an official Mapd region
had in those groups for neighbouring states.

## 6. Installing

1. Read `comma.json` (bounded size, HTTPS, the fixed URL above; no redirects off github.com and
   objects.githubusercontent.com / release-assets.githubusercontent.com).
2. Download each wanted part and the engine wheel into `<root>/.staging-<pack_version>/`, resumable
   (HTTP Range), checking size and SHA-256 as downloaded and as installed.
3. Unpack the three tars and the wheel inside staging. Write `pack.json` there last.
4. Swap: move the present pieces of `<root>` to `<root>/.old-<n>`, move staging's pieces in, delete
   `.old-<n>`. A failure before the swap leaves the installed pack untouched.
5. Admit the Mapd archives when possible (section 5).

Only while the car is off, and by default only on Wi-Fi or Ethernet (a state is over a gigabyte).
Nothing but the catalog and the files it names is ever requested, and no request carries anything
about the device or its position.
