# packbuilder/catalog

The PC side of getting state packs in the app (`docs/architecture-downloads.md` section 7; the format
is `docs/catalog-format.md`). Python 3.12, standard library only, run from the repository root;
nothing here uploads anything. Owner during the feature round: track G. Report:
`docs/experiments/18-getting-packs.md`.

| File | What it does |
|---|---|
| `download_rules.json` | The downloader's rules (timeouts, back-off, sizes, the User-Agent of rule N3). The single source: `python android/routing/scripts/sync_data.py --only download_rules.json` copies it into `:core:download`'s resources, whose test compares the copy. Edit this file, never the copy. |
| `catalog_format.py` | The reader rules of 2.3 (path rule, scope, per-entry checks, licence), shared by the three tools below. |
| `build_catalog.py` | Pack folders or archives in, a site out: one release folder per pack build (`<pack_id>-<pack_version>/`: `manifest.json`, one transfer file per part, `transfer.json`) and `catalog/catalog.json`. gzip -6 for parts of at least 64 KiB that shrink to 90 % or less; a release folder the catalog already names is never written again; `--check` re-reads every file the catalog names. |
| `serve_packs.py` | The development server: real `Range`, strong `ETag`, `If-Match`, `If-None-Match`, `If-Range`; `GET`/`HEAD` only; faults for tests (`--cut-after`, `--ignore-range`, `--fail-every`, `--throttle-kbps`); `--log-headers` prints every request header. Binds 127.0.0.1 by default (the emulator reaches it as 10.0.2.2). |
| `catalog_client.py` | The reference client (`list`, `get`): the algorithm of 2.4 with the address and redirect rules of 3.1. Its result passes `android/scripts/read_pack.py --verify`. The starting point of a comma 3X client. |
| `build_state_shapes.py` | Writes `android/feature/states/src/main/assets/states/us_states.json` (format `beamline-state-shapes/1`, 6.8) from Natural Earth 5.1.2 admin-1 in `data/map/work/overview/ne_overview.gpkg` (read-only); `--check` rebuilds in memory and compares. |
| `tests/` | `python -m unittest discover packbuilder/catalog/tests` (7.5): a tiny pack built, served on a free port and fetched (cut and resumed, ignored range, damaged file, refused redirects), byte-equal rebuilds, `transfer.json` reuse, refusals; the shapes asset. `make_tar_fixture.py` made `:core:download`'s `python-ustar.tar` test fixture. |

## Common runs

```
python packbuilder/catalog/build_catalog.py --out data/catalog/site --pack data/r3-packs/us-pa --pack data/r3-packs/us-oh --check
python packbuilder/catalog/serve_packs.py --root data/catalog/site [--log-headers] [--cut-after 100000000]
python packbuilder/catalog/catalog_client.py list http://127.0.0.1:8765/catalog/catalog.json
python packbuilder/catalog/catalog_client.py get http://127.0.0.1:8765/catalog/catalog.json PA --out <folder> [--skip-optional]
python packbuilder/catalog/build_state_shapes.py --check
python -m unittest discover packbuilder/catalog/tests
```

Two sites exist on this PC (`data/` is not in git):

- `data/catalog/site`: P's format-3 PA and OH packs (`--manifest-versions 3`, the default), for I's
  integration (10.4) and for publishing day (8.3). Board note `data/r3-board/g-catalog-site.md`.
- `data/r3-g/site`: G's own device checks (10.3), built from the round-2 z14 PA archive with
  `--archive data/android/archives-z14/us-pa-2026-11-14.tar --manifest-versions 2,3`.

## Notes

- `SOURCE_DATE_EPOCH` (seconds) fixes `serial` and `generated_utc`, so two builds of the same inputs
  give the same bytes. A run that changes no entry leaves `catalog.json` as it is.
- `build_catalog.py` hard-links a part that travels as it is (`map.pmtiles`, `routing.json`) when the
  input is a folder on the same volume. Never write into a release folder or its links.
- The server prints the client's address only with `--log-clients`, so a log pasted into a report
  holds no home-network address. `--bind 0.0.0.0` (the phone over Wi-Fi) prints a warning first.
