# packbuilder/pack: the state pack assembler (format 3)

`assemble.py` (standard library only) puts one state's builder outputs together as a format-3
pack (`docs/pack-format.md`): a pack folder and an archive, both in `data/r3-packs/` by default.

```
python packbuilder/pack/assemble.py pa            # the round-3 inputs (STATES in the script)
python packbuilder/pack/assemble.py oh
python packbuilder/pack/assemble.py pa --no-addresses --out data/r3-p/x   # a pack without house numbers
python android/scripts/read_pack.py data/r3-packs/us-pa-2026-11-14.tar --verify
python -m unittest discover packbuilder/pack/tests
```

| Part | Input (round 3) | Made by |
|---|---|---|
| map | `data/map/packs-v2/<st>-261114-z14.pmtiles` + its `.map.json` | `packbuilder/map/build_pack.py <st>` (z14, profile patch 2) |
| routing, routing_index | `data/routing/packs/<st>/` (tar + `manifest.json`, graph build 261114-pa-oh) | `packbuilder/routing` |
| search | `data/r3-packs/<st>-search.sqlite` | `packbuilder/search/build.py --state <st> --out-dir data/r3-packs` |
| addresses | `data/r3-p/addresses-<st>/<st>-addresses-slim.sqlite` | `BEAMLINE_ADDR_STATE=<st> BEAMLINE_ADDR_DATA=data/r3-p/addresses-<st> python packbuilder/addresses/run.py` |

It refuses parts of different OSM snapshots, a search and an address file with different street-key
fingerprints, a file whose size or SHA-256 differs from its builder's record, a file of the wrong
kind or state. `pack_version` is the build time `YYYYMMDDhhmm` UTC; the archive holds
`manifest.json` first, then the parts from the smallest to the largest.
