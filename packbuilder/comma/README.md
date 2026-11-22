# packbuilder/comma

The pack side of "Beamline on a comma": for each state pack, the three parts only a comma device
reads, and `comma.json`, the comma catalog. The contract is `docs/comma-pack-format.md`
(sections 2, 4 and 5); the phone's pack and `catalog.json` are not changed by anything here.
Python 3.12, run from the repository root.

| File | What it does |
|---|---|
| `roads.py` | `map.pmtiles` -> `roads.tar`: StarPilot's road-line tiles (`SPRT` version 1, its `road_tiles.py` at commit 008b0cb) for zooms 8, 11 and 14, one `<z>/<x>/<y>.rt` per map tile with a road, each zoom from the map's tiles of that zoom; the contract's class table. A map whose directories are not gzip (its header's `internal_compression`) is refused with a plain message. Needs the `pmtiles` package (`.venv/map`); `--verify` needs only the standard library. |
| `mapd.py` | `setup`: builds Mapd's program from StarPilot's public source at the commit `pins.json` pins (folder `mapd_repo`; a C compiler, pkg-config and zlib's developer files). Go 1.25.1 is installed only from go.dev's archive whose size and SHA-256 `pins.json` pins for the machine (linux-amd64, linux-arm64; another platform is refused, or give `--go` an installed Go); go.dev's own list is not read. `build`: Mapd's own preparation of the state's OpenStreetMap extract (with pyosmium), `mapd generate` over the whole two-degree groups covering the pack's bounds, one gzip tar per group, `mapd.tar` of them. `verify`: the member and cell checks and, with `--mapd`, Mapd's own `--snapshot-admit` of the unpacked archives. Linux only for setup, build and the admission. |
| `search_code.py` | `search-code.tar`, the five files `query.py` reads at run time, at the top level, LF line ends: `query.py`, `lines.py` and `normalize.py` from the frozen release in `search-release/` only (each refused unless it has the size and SHA-256 `pins.json` "search_code" pins; a CRLF checkout gives the same bytes), `ranking.json` and `abbreviations.json` from `packbuilder/search` (with `--manifest`, refused unless `ranking.json` is the one the pack's search file was built with; `--rev COMMIT` takes these two as committed there). `--verify` refuses a tar whose code is not the pinned release. `--answers` unpacks a tar and asks its code, in a separate Python (`-I -S`, imported the way StarPilot imports it), the known queries of `search_probes.json` over a pack's `search.sqlite`. `--status` says whether `packbuilder/search` has moved ahead of the release. |
| `search-release/` | The frozen release of the search code a comma runs: `query.py`, `lines.py`, `normalize.py`, byte for byte those of the first published pack with comma files (release 1, `us-pa-202611220901`). Never edited: a new release is made on purpose (below). |
| `search_probes.json` | The known queries of the check (Pennsylvania: Peach Street and Downs Drive from the public test trip's start, Erie, Pittsburgh), each with the place it must find among the first results. A state without any is asked for its own most important place by name. |
| `catalog.py` | `comma.json` beside the site `packbuilder/catalog/build_catalog.py` wrote: the comma parts' transfer files go into each pack's release folder (the phone builder's gzip rule), `routing`, `search` and `addresses` name the very same files as `catalog.json`, `engines` comes from `pins.json`. The serial is a real UTC time `YYYYMMDDhhmmss` (the second after the last one when needed); one that is not, or lies more than two days ahead of this clock, is never written. `--check` re-reads every file it names. Standard library only. |
| `check.py` | The weekly build's check of one pack's three parts (exit 1 when one is missing or fails): the search code the pinned release and answering the known queries from the pack's own search file (`search_code.py --answers`), the road tiles, the Mapd archives and, with `--mapd`, Mapd's admission. Its result also reports, without failing, when `packbuilder/search` has moved ahead of the release. |
| `common.py` | Hashing, deterministic tars (owner 0, mode 0644, one time), the member rules and the strict unpack of the contract, Mapd's group and cell names. |
| `pins.json` | The pinned StarPilot commit, the Go version with its archives' sizes and SHA-256 per platform, the pyvalhalla wheel(s) `comma.json` offers, and the frozen search code release (`search_code`: each file's bytes and SHA-256). |
| `tests/` | `.venv/map/Scripts/python -m unittest discover -s packbuilder/comma/tests`: no network. The `.rt` bytes are compared with StarPilot's own encoder and read back with its own `decode_road_tile` (loaded by path from a StarPilot clone, `STARPILOT_SRC`, default `data/starpilot/src` or `data/starpilot/fork`; skipped without it or without numpy); a real `map.pmtiles` is used when there is one in `data/r3-packs/weekly`; the frozen search code answers the known queries from a real Pennsylvania `search.sqlite` when there is one (`BEAMLINE_SEARCH_SQLITE`, default `data/search/pa-search.sqlite`); the whole Mapd build and admission run on Linux with `MAPD_BIN` naming a built `mapd`. A test fails when `search-release/` and the pins disagree; another only reports (a skip with the reason) when `packbuilder/search` has moved ahead of the release. `test_weekly.py` covers the packs repository's `build/weekly.py` (the comma side kept apart from the phone's in run and publish, the comma steps' time limit with a step that sleeps, the phone's publish gate, publish to a folder and to a faked GitHub, the extract fallback with a faked fetch); `test_device_pack.py` covers `starpilot/make_device_pack.py` and is skipped, with the reason, where that file is not in the checkout. |

## In the weekly build

`build/weekly.py` of the packs repository: `setup` builds Mapd into `data/comma/mapd-tool` (a failure
is recorded in `data/comma/setup.json` and never stops the setup); `run` builds the three parts of
every pack into `data/out/comma/us-<state>/` after the pack is assembled, checks them with
`check.py --mapd` and writes `catalog/comma.json` after `catalog.json`; `publish` uploads the comma
parts with the pack's other files to its release and `comma.json` to the `catalog` release after
`catalog.json`.

Every comma step (Mapd's setup included) has a time limit, `build/config.json` "comma"
"step_time_limit_s" (30 minutes; a whole run took 46 minutes on 2026-11-22): it runs in a process
group of its own, and past the limit the whole group (git, go, a compiler) is killed and the step
counts as failed.

The comma side never holds back the phone's packs: each of these steps runs apart, and its result is
`data/out/run.json` "comma" (passed, with `comma.json`'s size and SHA-256, or the step that failed and
why). After a failure the later comma steps are skipped and no `comma.json` is left in the site; the
phone's packs and `catalog.json` are checked and published as if there were no comma side; `publish`
leaves the published `comma.json` as it is, deletes no build it names, and ends as failed after
publishing (`weekly.py comma-status` fails the workflow when nothing is published). `publish` takes
the comma files only when run.json records the comma side as passed and every file `comma.json`
names has the size and SHA-256 it gives.

## By hand

```
.venv/map/Scripts/python packbuilder/comma/roads.py --map <pack>/map.pmtiles --out parts/us-pa/roads.tar --mtime <created> --report roads.json
python packbuilder/comma/search_code.py --out parts/us-pa/search-code.tar --manifest <pack>/manifest.json --mtime <created>
python3 packbuilder/comma/mapd.py setup --dest data/comma/mapd-tool                     (Linux)
<python with pyosmium> packbuilder/comma/mapd.py build --extract <state>.osm.pbf --manifest <pack>/manifest.json \
    --mapd data/comma/mapd-tool/mapd --out parts/us-pa/mapd.tar                         (Linux)
python packbuilder/comma/check.py --pack <pack> --parts parts/us-pa [--mapd data/comma/mapd-tool/mapd]
python packbuilder/catalog/build_catalog.py --out site --pack <pack> --check
python packbuilder/comma/catalog.py --site site --parts parts --check
```

`<created>` is the pack's `created_utc` in seconds, so the same inputs give the same bytes.

```
python packbuilder/comma/search_code.py --status                                       # the release against its pins and packbuilder/search
python packbuilder/comma/search_code.py --answers parts/us-pa/search-code.tar --db <pack>/search.sqlite --state PA
```

## A new release of the search code

`packbuilder/search` may move on; the comma keeps running the frozen release until a new one is made
on purpose, in this order: StarPilot's pin list takes the new files' SHA-256 (next to the old ones);
then copy `query.py`, `lines.py` and `normalize.py` from `packbuilder/search` into `search-release/`,
write their bytes and SHA-256 into `pins.json` "search_code" with the next release number (`--status`
prints them), and add the release's row to `docs/comma-pack-format.md` section 2. A pack never carries
code StarPilot does not name.

## Licences

The parts are made from OpenStreetMap data (the pack's map and extract): ODbL 1.0, "© OpenStreetMap
contributors", like the rest of the pack. Mapd (`mapd_repo` of StarPilot, MIT licence, its notice in
that folder's LICENSE), its Go modules (pinned by its `go.sum`) and Go (BSD-3-Clause) run at build time
only; nothing of them is in a part. The pyvalhalla wheel `comma.json` names is
fetched by the device from PyPI, not copied here (MIT).
