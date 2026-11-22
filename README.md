# Beamline packs

The offline data of [Beamline](#about-beamline), a driving-first navigation app for Android: one **state
pack** per US state, rebuilt every week from fresh OpenStreetMap data and published here as GitHub
Releases. The app reads the list of packs (`catalog.json`) and downloads a state's files; nothing else
is ever sent to or read from this repository.

- **What a pack holds:** the map (vector tiles, PMTiles), the routing tiles (Valhalla) and their index,
  the search index (SQLite) and, optionally, house numbers (SQLite). Data only: the map style ships in
  the app.
- **For a comma device** (StarPilot): three more files per pack build, the search code
  (`search-code.tar`: a frozen release of the code, `packbuilder/comma/search-release`, whose SHA-256
  StarPilot pins, with the pack's own rule files), the roads as StarPilot's own road-line tiles (`roads.tar`) and the roads in
  Mapd's format for its speed-limit map (`mapd.tar`), offered with the shared parts by `comma.json`
  (format `beamline-comma-catalog/1`). The app never reads them.
- **States:** Pennsylvania. More states are one line in `build/config.json` ("states").
- **How often:** every Monday early morning (09:17 UTC: 4:17 AM US Eastern in winter, 5:17 AM in
  summer), plus a "Run workflow" button.
- **Kept:** the newest two builds of each state. The catalog names only the newest; the one before
  stays a week longer for phones still downloading it.

> This repository is exported from the Beamline app repository (see `SOURCE.md`). Change files there
> and export again; changes made here are overwritten by the next export.

## Where the packs are

| Release tag | What it holds |
|---|---|
| `catalog` | `catalog.json`: the newest pack of every state, every file with its size and SHA-256; `comma.json`: the same for a comma device |
| `us-pa-<YYYYMMDDhhmm>` | one Pennsylvania build: `manifest.json`, one file per part (gzip where it pays) and the three comma files |
| `inputs` | the build's slower-changing inputs (below); not a pack, the app never reads it |

The catalog's address is `https://github.com/<owner>/beamline-packs/releases/download/catalog/catalog.json`;
it names every file by a path relative to itself (`../us-pa-202611230917/map.pmtiles`), the layout of
GitHub Releases. A published release is never rebuilt: a newer build is a new release.

While this repository is private, release files need a GitHub login to download, so the app cannot
fetch them yet. To put a build on a phone by hand: `gh release download us-pa-<version> --dir pa`,
then `python3 build/release_to_archive.py pa` writes `us-pa-<date>.tar` (every part checked), the
file the app's "Use a file instead" reads.

## Licences

**The data** (written in every release too):

- Map, routing and search data, and the comma files' roads: **© OpenStreetMap contributors**, available under the
  [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/) (ODbL). The packs are
  a Produced Work and a Derivative Database of OpenStreetMap; anyone may use and share them under
  the ODbL with that credit. See `ATTRIBUTION.md`.
- House numbers: OpenStreetMap (ODbL), the **U.S. Census Bureau TIGER/Line** address ranges and
  county subdivisions (public domain, a US government work), the **USDOT National Address Database**
  release 24 (US public domain) and, in Lackawanna County, the **City of Scranton GIS** 911 address
  points (public domain, PDDL 1.0).
- Low-zoom map context: Natural Earth (public domain); OpenStreetMap water and land polygons from
  osmdata.openstreetmap.de (ODbL); time zones from timezone-boundary-builder (ODbL), build time only.

**The tools** (build time only; nothing of them is in a pack):
Planetiler 0.10.2 (Apache-2.0); the Protomaps basemap profile 4.15.2 (BSD-3-Clause; schema adapted
from Tilezen, MIT) with Beamline's patch (`packbuilder/map/profile/`, BSD-3-Clause like the profile);
Apache Maven 3.9.16 (Apache-2.0); OpenJDK 25 (GPL-2.0 with the Classpath Exception); Valhalla 3.9.1
from the pyvalhalla wheel (MIT); pyosmium 4.3.2 (BSD-2-Clause, with libosmium, BSL-1.0); shapely 2.2.0
(BSD-3-Clause, with GEOS, LGPL-2.1); numpy (BSD-3-Clause); psutil (BSD-3-Clause); pmtiles (BSD-3-Clause);
Mapd, StarPilot's `mapd_repo` (MIT), with the Go modules its `go.sum` pins, built with Go 1.25.1
(BSD-3-Clause), for the comma files' Mapd archives;
pyshp (MIT); openpyxl and et_xmlfile (MIT); usaddress, python-crfsuite and probableparsing (MIT);
requests (Apache-2.0); urllib3, charset-normalizer (MIT); idna (BSD-3-Clause); certifi (MPL-2.0); and,
installed by the map builder's pinned list but not used here, playwright (Apache-2.0), pillow
(MIT-CMU), greenlet, pyee (MIT), typing_extensions (PSF-2.0). Each builder's README and
`requirements.txt` list theirs; the search index also carries the iD tagging schema's category words
(ISC; `packbuilder/search/THIRD_PARTY_NOTICES.md`).

**The code in this repository** is under the MIT License (`LICENSE`), the same as the app. The data
licences above apply to the packs.

## How the weekly build works

One job on GitHub's standard Linux runner (`.github/workflows/weekly.yml`), one entry point
(`build/weekly.py`):

1. **Privacy scan** (`build/privacy_scan.py`, required): generic patterns only (home paths,
   private network addresses, e-mail addresses, token-like strings, device identifiers, binary or
   large files). Nothing is built when it finds anything.
2. **Inputs.** The Geofabrik extract of every state (`<state>-latest.osm.pbf`, checked against its
   MD5, and its `.poly`); its replication date is the pack's OpenStreetMap date. When the latest
   extract cannot be downloaded or does not match its MD5 (in November 2026 Geofabrik's "latest" link
   pointed at a missing file for more than a day), the newest dated extract of the same folder
   (`<state>-YYMMDD.osm.pbf`) that matches its own MD5 is used, if its map date is at most 10 days old
   (`build/config.json` "extract_fallback"); the run's log, its summary and `run.json` say which file
   was used and why. After every published run the extract itself is kept in the `inputs` release
   (`extract-<state>-YYMMDD.osm.pbf`, one per state); when Geofabrik gives nothing usable at all (its
   server could not be reached from GitHub's runners for hours that same month), that kept extract is
   used under the same age limit. The slower-changing
   inputs come from the `inputs` release; each is fetched from its publisher again only when the
   publisher has a newer file and the kept copy is at least four weeks old (water and land polygons,
   Natural Earth, QRank), or never (the Census, TIGER/Line and NAD files and the time zones, which
   the builders pin by version or SHA-256). The Scranton points are fetched again every four weeks.
   A release is used rather than the Actions cache because the cache drops an entry not used for
   7 days (a weekly run is right at that edge, and a failed week would lose it) and holds 10 GB,
   while a release keeps its files until they are replaced.
3. **Build** (pinned versions, as the builders pin them): the map (`packbuilder/map`), the routing
   graph of all the states of the build together, cut per state (`packbuilder/routing`), the search
   index (`packbuilder/search`), the house numbers (`packbuilder/addresses`), the pack
   (`packbuilder/pack/assemble.py`, format 3), then the pack's comma files (`packbuilder/comma`): the
   search code (the frozen release in `packbuilder/comma/search-release`, never the working copy in
   `packbuilder/search`: each file's size and SHA-256 are pinned in `packbuilder/comma/pins.json` and
   named by StarPilot, so a pack never brings code StarPilot has not named; with this week's
   `ranking.json` and `abbreviations.json`), the road tiles from the pack's map, and Mapd's archives made from the same extract by
   Mapd's own generator, built from StarPilot's public source at a pinned commit with Go 1.25.1
   (installed only from go.dev's archive whose SHA-256 `packbuilder/comma/pins.json` pins).
4. **Checks** (nothing is published unless every one passes): the pack reads and every checksum
   matches (`tools/read_pack.py --verify`); the search tests (`packbuilder/search/tests`, thresholds
   in `build/config.json`); the routing part installs the way the app installs it and the test routes
   of `build/route_checks.json` plan with lengths near their references; the pack is not much smaller
   than the published one (95 % of its size, 90 % of each part; `--allow-smaller` from the
   "Run workflow" button passes a deliberate change). Apart from these, the comma files are checked:
   all there and keeping their rules (`packbuilder/comma/check.py`: the search code is the pinned
   release and, unpacked and imported in a separate Python the way StarPilot loads it, finds known
   places such as Peach Street and Erie in this week's search file; every road tile passes
   StarPilot's decoder checks; Mapd itself admits the archives).
5. **Catalog site** (`packbuilder/catalog/build_catalog.py --check`, then
   `packbuilder/comma/catalog.py --check` for `comma.json`).
6. **Publish:** first, before anything is uploaded, `catalog.json` must be the very file the run
   checked (its size and SHA-256 are recorded in `data/out/run.json`) and every file it names must
   still have the size and SHA-256 it gives; otherwise nothing is published. Then each pack build becomes a release, created as a draft, its files (the comma files
   included) uploaded and compared, then published; then `catalog.json` of the `catalog` release is
   replaced, then `comma.json`; then releases older than the newest two of each state are deleted
   (never one that `catalog.json` or `comma.json` names); then changed inputs are saved to `inputs`.
   A run that fails publishes nothing (a draft left behind is deleted) and the published packs stay
   as they were. Only the workflow's own `GITHUB_TOKEN` (contents: write) is used.

**The comma side never holds back the phone's packs.** Every comma step (Mapd's setup, the three
comma files, their check, `comma.json`) is kept apart: when one fails, the phone's packs and
`catalog.json` are built, checked and published exactly as if there were no comma side, the
published `comma.json` stays untouched, no build it names is deleted, and the workflow run ends as
failed after publishing, with the reason in its log and summary. `publish` takes the comma files and
`comma.json` only when the run recorded the comma side as passed (`data/out/run.json` "comma") and
every file `comma.json` names still has the size and SHA-256 it gives, so a later manual `publish`
cannot publish a `comma.json` whose check failed either. Every comma step has a time limit of 30
minutes (`build/config.json` "comma"): a stalled step (a git fetch or a Go module download in Mapd's
setup) is killed with everything it started and counts as that step's failure, so it cannot use up
the job's time and hold back the phone's packs. `comma.json`'s serial is a real UTC time
(`YYYYMMDDhhmmss`); one that is not, or that lies more than two days ahead of the clock, is never
written or published (a comma refuses it).

The run's numbers (time, memory and disk of every step, sizes, the comparison with last week) are on
the workflow run's summary page.

### Runner

The repository variable `PACKS_RUNNER` picks the machine: unset (or `github`) uses GitHub's
`ubuntu-24.04` runner; `self-hosted` uses a self-hosted Linux runner with the labels
`self-hosted, linux, x64, beamline-packs` (for example in WSL Ubuntu 24.04 on the owner's PC; run
`sudo sh build/setup_ubuntu.sh` there once). See the measured times and memory in the app
repository's report of this step.

## Run it yourself (Ubuntu 24.04)

```
sudo sh build/setup_ubuntu.sh                        # python3.12-venv, OpenJDK 25, git; gcc, pkg-config, zlib1g-dev (Mapd)
python3 build/weekly.py setup                        # the builders' virtual environments, the map tools, Mapd
python3 build/weekly.py run --store ~/packs-inputs   # build and check; the site is in data/out/site
python3 build/weekly.py publish --local ~/packs-site --store ~/packs-inputs
python3 packbuilder/catalog/serve_packs.py --root ~/packs-site
python3 packbuilder/catalog/catalog_client.py get http://127.0.0.1:8765/catalog/catalog.json PA --out pa
python3 tools/read_pack.py pa --verify
```

About 20 GB of free disk and 7 GB of memory are enough for Pennsylvania; everything is written under
`data/` (ignored by git).

## Files

| Path | What |
|---|---|
| `build/weekly.py` | the weekly build's one entry point (setup, run, publish) |
| `build/config.json` | the states, threads, memory, the checks' thresholds, the kept inputs |
| `build/check_routes.py`, `build/route_checks.json` | the test routes |
| `build/privacy_scan.py` | the privacy scan |
| `build/release_to_archive.py` | a release's files to the app's pack archive |
| `build/setup_ubuntu.sh` | the system packages (the workflow's, a self-hosted runner's and a local machine's) |
| `packbuilder/` | the pack builders (each folder has its README); `packbuilder/comma` the comma files and `comma.json` |
| `tools/` | the pinned tool fetchers, `read_pack.py` (the pack reader) |
| `.github/workflows/weekly.yml` | the workflow |

## About Beamline

Beamline is an offline-first, driving-first navigation app for Android: maps, routing, search and
guidance work with no connection, from these packs. The pack format (format 3) and the catalog
format (`beamline-catalog/1`) are described in the app repository (`docs/pack-format.md`,
`docs/catalog-format.md`); `tools/read_pack.py` and `packbuilder/catalog/catalog_client.py` read
them with nothing but Python's standard library. The comma files and `comma.json` are described in `docs/comma-pack-format.md` here.
