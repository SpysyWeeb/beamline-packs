# packbuilder/comma

The pack side of "Beamline on a comma": for each state pack, the three parts only a comma device
reads, and `comma.json`, the comma catalog. The contract is `docs/comma-pack-format.md`
(sections 2, 4 and 5); the phone's pack and `catalog.json` are not changed by anything here.
Python 3.12, run from the repository root.

| File | What it does |
|---|---|
| `roads.py` | `map.pmtiles` -> `roads.tar`: StarPilot's road-line tiles (`SPRT` version 1, its `road_tiles.py` at commit 008b0cb) for zooms 8, 11 and 14, one `<z>/<x>/<y>.rt` per map tile with a road, each zoom from the map's tiles of that zoom; the contract's class table. A map whose directories are not gzip (its header's `internal_compression`) is refused with a plain message. Needs the `pmtiles` package (`.venv/map`); `--verify` needs only the standard library. |
| `mapd.py` | `setup`: builds Mapd's program from StarPilot's public source at the commit `pins.json` pins (folder `mapd_repo`; a C compiler, pkg-config and zlib's developer files). Go 1.25.1 is installed only from go.dev's archive whose size and SHA-256 `pins.json` pins for the machine (linux-amd64, linux-arm64; another platform is refused, or give `--go` an installed Go); go.dev's own list is not read. `build`: Mapd's own preparation of the state's OpenStreetMap extract (with pyosmium), `mapd generate` over the whole two-degree groups covering the pack's bounds, one gzip tar per group, `mapd.tar` of them. `verify`: the member and cell checks and, with `--mapd`, Mapd's own `--snapshot-admit` of the unpacked archives. Linux only for setup, build and the admission. |
| `search_code.py` | `search-code.tar`: the five files of `packbuilder/search` that `query.py` reads at run time, at the top level, LF line ends; with `--manifest`, refused unless `ranking.json` is the one the pack's search file was built with; `--rev COMMIT` takes the files as committed there. |
| `catalog.py` | `comma.json` beside the site `packbuilder/catalog/build_catalog.py` wrote: the comma parts' transfer files go into each pack's release folder (the phone builder's gzip rule), `routing`, `search` and `addresses` name the very same files as `catalog.json`, `engines` comes from `pins.json`. `--check` re-reads every file it names. Standard library only. |
| `check.py` | The weekly build's check of one pack's three parts (exit 1 when one is missing or fails), Mapd's admission included with `--mapd`. |
| `common.py` | Hashing, deterministic tars (owner 0, mode 0644, one time), the member rules and the strict unpack of the contract, Mapd's group and cell names. |
| `pins.json` | The pinned StarPilot commit, the Go version with its archives' sizes and SHA-256 per platform, and the pyvalhalla wheel(s) `comma.json` offers. |
| `tests/` | `.venv/map/Scripts/python -m unittest discover -s packbuilder/comma/tests`: no network. The `.rt` bytes are compared with StarPilot's own encoder and read back with its own `decode_road_tile` (loaded by path from a StarPilot clone, `STARPILOT_SRC`, default `data/starpilot/src` or `data/starpilot/fork`; skipped without it or without numpy); a real `map.pmtiles` is used when there is one in `data/r3-packs/weekly`; the whole Mapd build and admission run on Linux with `MAPD_BIN` naming a built `mapd`. `test_weekly.py` covers the packs repository's `build/weekly.py` (the comma side kept apart from the phone's in run and publish, publish to a folder and to a faked GitHub, the extract fallback with a faked fetch); `test_device_pack.py` covers `starpilot/make_device_pack.py`. |

## In the weekly build

`build/weekly.py` of the packs repository: `setup` builds Mapd into `data/comma/mapd-tool` (a failure
is recorded in `data/comma/setup.json` and never stops the setup); `run` builds the three parts of
every pack into `data/out/comma/us-<state>/` after the pack is assembled, checks them with
`check.py --mapd` and writes `catalog/comma.json` after `catalog.json`; `publish` uploads the comma
parts with the pack's other files to its release and `comma.json` to the `catalog` release after
`catalog.json`.

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

## Licences

The parts are made from OpenStreetMap data (the pack's map and extract): ODbL 1.0, "© OpenStreetMap
contributors", like the rest of the pack. Mapd (`mapd_repo` of StarPilot, MIT licence, its notice in
that folder's LICENSE), its Go modules (pinned by its `go.sum`) and Go (BSD-3-Clause) run at build time
only; nothing of them is in a part. The pyvalhalla wheel `comma.json` names is
fetched by the device from PyPI, not copied here (MIT).
