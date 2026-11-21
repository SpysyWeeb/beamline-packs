# Search index (packbuilder/search)

Builds the offline search part of a state pack: one read-only SQLite file with
FTS5 and R*Tree, holding places, streets and points of interest (POIs) with
categories and brands, plus a prototype query engine in Python written so it
can be ported to Kotlin. Results and measurements: `docs/experiments/03-search.md`.

## Commands (run from the repo root)

| Step | Command |
|---|---|
| Python env (build only) | `python -m venv .venv/search` then `.venv/search/Scripts/python -m pip install -r packbuilder/search/requirements.txt` |
| SQLite CLI tools (for sizes) | `python tools/fetch_sqlite_tools.py` |
| Refresh iD category words (optional, output is in git) | `.venv/search/Scripts/python packbuilder/search/seed_categories.py` |
| Build the PA index | `.venv/search/Scripts/python -I packbuilder/search/build.py --state pa` |
| Rebuild one step | `... build.py --state pa --step index --force index` |
| Search from the command line | `python packbuilder/search/cli.py "walmart erie"`, `... "gas" --at pittsburgh`, `... "pizza" --near 42.12,-80.08` |
| Regression tests | `python packbuilder/search/tests/run_tests.py` (held-out set: `--cases cases_holdout.json`; review set: `--cases cases_review.json`; add `--allow-addr-version-mismatch` while the addresses database and normaliser versions differ, or `--no-addresses`) |
| Input robustness checks | `python packbuilder/search/tests/check_inputs.py` |
| Sizes, latency, SQLite cross-check | `.venv/search/Scripts/python packbuilder/search/measure.py --state pa` (`--only prefix` / `--only house` for the search-as-you-type and house-number sets) |
| Typo-method comparison | `python packbuilder/search/fuzzy_eval.py export ...` then `run ...` (see the file) |
| spellfix1 + Linux check (WSL) | `wsl -d Ubuntu-24.04 -u <wsl-user> -- bash /mnt/c/<repo>/packbuilder/search/wsl_spellfix.sh /mnt/c/<repo>` and `.../wsl_linux_check.sh` |
| Brands, NSI, chain coverage | `.venv/search/Scripts/python packbuilder/search/brand_eval.py --state pa` |

`build.py` runs three steps, each in its own process (so peak memory is per
step) and each skipped when its output exists: `check` (MD5 of the input PBF
against its `.md5`), `extract` (`data/search/work/pa-extract.pkl`), `index`
(`data/search/pa-search.sqlite`). Timings and peak memory go to
`data/search/pa-build-stats.json` (latest run per step, plus a `history` list of
every run). Ohio is wired in (`--state oh`) but was not built in this
experiment.

The index step imports the addresses track's `street_key()`
(`packbuilder/addresses/addrlib/normalize.py`) to fill `street.addr_key`, the
house-number join key; it stops with an error if that module cannot be loaded
(`--no-addr-key` builds without it). The normaliser's table version and file
hashes are stored in `meta.addr_key`. Rebuild the index whenever that
normaliser changes.

The query engine, the CLI and the tests use only the Python standard library
(`sqlite3`, `json`, `math`), so they run unchanged with Linux `python3`.

## Files

| File | What it is |
|---|---|
| `build.py` | the builder (steps above) |
| `osm_extract.py` | reads the PBF with pyosmium: boundaries, places, roads, POIs, address points, and the outlines of named parks; `natural=*` / `waterway=*` objects only for the values a category rule names (beach, waterfall, cave entrance, peak) |
| `geo.py` | point-in-polygon for millions of points (grid + prepared polygons), distances, `.poly` reader |
| `rules.py` | category tag rules and category words |
| `normalize.py` | the canonical search key (shared by builder and engine) |
| `query.py` | the query engine (`SearchEngine.search(text, lat, lon)`) |
| `cli.py` | command line around `query.py` |
| `categories.json` | 104 categories: OSM tag rules + everyday US words (hand-written); `in_park` marks the named natural places (beach, waterfall, cave, peak) |
| `categories_idterms.json` | generated: extra words copied from the iD tagging schema (ISC; full notice inside) |
| `THIRD_PARTY_NOTICES.md` | licence notices for data that ships in the database (OSM, iD tagging schema words) |
| `abbreviations.json` | abbreviations, route prefixes, stopwords, connectors ("near", "in", "on") |
| `ranking.json` | every ranking weight and search limit, plus the addresses join config |
| `chain_reference.json` | public store counts used for chain coverage |
| `tests/` | `cases.json` (355 cases: 345 used while tuning, 10 `natural` cases of next-round item 14), `cases_holdout.json` (99 held-out cases), `cases_review.json` (49 cases written from the reviews before the fixes), `points.json`, `run_tests.py`, `check_inputs.py` (bad positions, odd paths, hostile strings) |
| `measure.py`, `fuzzy_eval.py`, `brand_eval.py` | the measurement scripts |
| `wsl_spellfix.sh`, `wsl_linux_check.sh` | WSL helpers (spellfix1 build, Linux python3 run) |

## The database (schema version 1)

| Table | Contents |
|---|---|
| `entry` | one row per searchable thing: `kind` 1 place / 2 street / 3 POI, display `name`, `name_key`, `alt_keys` (`|`-separated; a leading `~` marks a derived key such as a street without its leading directional, or `route 30` / `state route 8` for a US / PA route ref), `type` (place type, road class, primary category, or `key=value` of the main OSM tag for a POI in no category), representative `lat`/`lon` (POI areas: centroid), `importance` 0..1, `muni_id` / `county_id` (entry ids of the containing municipality and county), `brand_id`, precomputed `subtitle` (first value of OSM `;` lists) |
| `place`, `street`, `poi` | per-kind details (OSM ids, population, way count and length, `street.addr_key` = the addresses track's street key of the name, brand / brand:wikidata / operator / opening_hours / phone / website / raw addr:* tags, all categories) |
| `street_line` | the line of a street entry: `entry_id`, `line` (the blob of `lines.py`: the street's ways in its municipality, simplified to about 4 m, as varint steps of 1e-5 degree). The query engine reads it for a street's distance (the nearest point of the line), for where two streets meet ("state st and 12th") and for the street at a point (`near_street`); rules in `ranking.json` `crossing` and `near_street`; `tests/test_lines.py` |
| `signal` | every traffic light (`highway=traffic_signals` node): `cell`, `lat`, `lon`; `cell = (floor(lat*100)+9000)*40000 + floor(lon*100)+18000`, a square of 0.01 degree (`signals.py`). Not searched: the app's guidance reads the cells along a route and shows a light at the junctions that have one (docs/next-round.md item 36) |
| `poi_door` | the front door of a POI that is a large building: `entry_id`, `lat`, `lon` (a point of its outline), `how` (`main`: a mapped main entrance; `entrance`: another mapped customer entrance; `parking`: a shop, the side its parking is on). Not in every file: added 2026-11-21, schema version still 1 (an app that does not know the table ignores it). See "Front doors" below |
| `poi_prefix` | search as you type: (grid `cell`, first 3 characters `p3` of the key, `entry_id`) for named POIs; `cell = (floor(lat*10)+900)*4000 + floor(lon*10)+1800` |
| `context` | extra place candidates per entry, NOT a single assignment: `source` 3 = populated place within 6 km, 4 = postal city from `addr:city`, 5 = ZIP from `addr:postcode`; `weight` 0..1. Ranking uses them for "in CITY" queries |
| `poi_cat` | category id -> POI (a POI can be in several categories) |
| `category`, `category_term` | categories and their words (weighted: own words 1.0, iD words 0.6) |
| `brand`, `brand_alias` | brands keyed by brand:wikidata, and every spelling seen in the data (normalised, plus a no-space form) |
| `vocab`, `spell_del`, `vocab_tri` | typo correction: word list with frequencies; SymSpell-style distance-1 deletions stored as 32-bit FNV-1a hashes; FTS5 trigram index of words of 7+ letters for distance-2 fallback |
| `entry_fts` | FTS5 over the keys (contentless, `detail=none`, `prefix='2 3'`), rowid = entry id |
| `entry_rtree` | R*Tree of entry bounding boxes (points for POIs) |
| `meta` | build facts: OSM timestamp, SQLite version, FTS options, rebuilt boundaries, licence line, third-party notices, state `bbox` (for the out-of-coverage flag), `addr_key` (normaliser version and hashes used for the join key) |

Indexes: `entry_name_key` (all keys), `entry_place_key` (partial index, places
only, for prefix lookups by importance), `entry_brand`, `vocab_word`.

### Front doors (next-round item 28)

An area's entry uses its centroid. For a large store that is the middle of the roof, and a route
that ends at the lane nearest to it ends as often behind the building as in front. `doors.py`
gives a large building (its centroid 15 m or more inside its outline, or outside it) the point of
its outline where customers go in, and the index keeps it in `poi_door`; the app's trip planner
ends the route at the lane by that point.

1. A mapped main entrance (a node of the outline with `entrance=main`); of several, the one nearest
   to the customer parking.
2. Else a mapped customer entrance (`entrance=yes`, `shop`, `entrance`, `secondary`): the only
   one, or of several the one nearest to the customer parking.
3. Else, for a shop only (`shop=*`, or `building=retail` / `supermarket`): the point of the outline
   nearest to the middle of the customer parking within 60 m (parking areas by their area, parking
   aisles by their length x 16 m; never underground or multi-storey parking, never `access=private`
   or `no`). At least 300 m2 of parking, else nothing.
4. Else no row: the entry keeps its centroid alone.

Never a service, emergency, exit, garage, staircase or home entrance. A POI mapped as a node keeps
its own point; when the same place is mapped as a node and as its building, the node's entry gets
the building's door. Rule 3 was measured against the 209 large buildings of Pennsylvania with
exactly one mapped main entrance: for shops the point is on the opposite side of the building for
4 %, median 19 m from the entrance; for schools, churches, libraries and offices 25-40 %, so the
rule is for shops only (`docs/experiments/33-front-doors.md`). Pennsylvania (extract 261114): 2,098
doors (261 main, 161 entrance, 1,676 parking) among 137,967 POIs; the other tables are the same
rows as before. Unit tests: `packbuilder/search/tests/test_doors.py`.

### Named natural places (next-round item 14)

Named beaches (`natural=beach`, `leisure=beach_resort`), waterfalls (`natural=waterfall`,
`waterway=waterfall`), cave entrances and peaks are POIs of the categories `beach`, `waterfall`,
`cave` and `peak` (label = the type shown). Other named natural objects (woods, water, wetlands,
ridges, springs, cliffs ...) stay out. Two derived keys (tier `exact_alt`, no engine change):

- a POI name with "No. N" is also keyed without the "no": `Beach no.1` -> `beach 1`;
- a POI of an `in_park` category inside named parks, nature reserves or protected areas (up to the three smallest
  that hold its point) is also keyed `<park> <label>` and `<park> <name>`, with the park's name
  as written and without a trailing "State Park", "Park", ... (`PARK_SUFFIXES`): "presque isle
  beach" finds the beaches of Presque Isle State Park, "ricketts glen waterfall" its falls.

### Streets

Named (or ref-only) drivable ways are cut by municipality boundary (every
vertex is located), grouped by (`name_key`, municipality) and split again into
clusters whose bounding boxes are more than ~600 m apart. Each cluster is one
street entry with a bounding box (R*Tree) and a representative point (middle
vertex of its longest piece). The street keeps its municipality and county, and
gets extra context candidates (nearby populated places, postal cities and ZIPs
seen on addresses with that street name in that municipality) instead of one
hard-assigned city.

### Join point for the addresses track

House numbers come from the addresses track (`data/addresses/pa-addresses.sqlite`,
built by `packbuilder/addresses`). `query.py` attaches that file read-only if it
exists, looks up (street key, house number) once per distinct key through that
track's view `address_rows(hn, street_key, lat, lon)`, and gives each address
point to the street result whose bounding box holds it best (inside the box,
else within `house_margin_deg`). The located point replaces the street's point
before the distance is computed and before duplicates are dropped. A street
whose house number was located (an exact address) gets `weights.house_located`
(1.0) times the tier value of its name match and the distance decay of the
house from the anchor (`scale_km`; 1 without an anchor) on top of its score, only inside the named place's
municipality or county when the query names one and never for an `any_word` match (a street that
only shares a word such as "Street" with the query), and the candidates are ordered
again. So an exact address in town goes first ("4319 peach st" near Erie: the
address before the neighbourhood "Peach Street Terrace"; next run, part 1),
while one in another town or 400 km away does not jump a nearby street
("10 w 8th st" near Erie keeps West 8th Street in Erie first, not 10 West 8th
Street in Pottstown).
Table, columns and margin are configured in `ranking.json` -> `addresses`.

Type labels made from OSM tag values (a place's type, a POI with no category)
are spelled the US way through `ranking.json` -> `us_spellings`
("neighbourhood" -> "Neighborhood", "leisure=sports_centre" -> "Sports
center"); the engine also rewrites the leading type label of a place's
subtitle in a pack built before that ("Neighbourhood in Erie"), never a name.
`build.py` writes "Neighborhood" itself.

The two tracks normalise street names differently: this index uses expanded
keys (`normalize.key("N. Main St.")` = `"north main street"`), the addresses
track uses abbreviated keys (`"n main st"`). The builder therefore stores, for
every street, the addresses track's key of its name in `street.addr_key`
(computed at build time), so no code of the other track runs at query time and
a Kotlin port only needs the column. The engine refuses to attach (raises
`RuntimeError`) when the search index has no `addr_key`, or when the addresses
database's `meta.normalisation_table_version` differs from the version the
index was keyed with; `allow_addr_version_mismatch=True` attaches anyway and
sets `addr_warning`. One shared normaliser for both tracks (and the Kotlin
port) is still an open decision. Without the file the engine returns the
street with `house_located: false`.

## Query engine in short

0. A position that is missing, not finite or out of range is ignored
   (`position_ignored`); one more than `coverage_margin_km` outside the state's
   bounding box sets `out_of_coverage`, and a pure category query that finds
   nothing there returns nothing.
1. Normalise; split off "near me", a trailing state word ("erie pa",
   "pittsburgh, pa"; kept when the whole phrase is a known name such as
   "university of pennsylvania"), a connector ("near X", "in X", "on X"; these
   words are never typo-corrected) and a leading house number (unless the rest
   has no street-type word and the phrase starts a brand, category or name:
   "7 eleven", "84 lumber").
2. Is the whole phrase a category word, a brand alias or a known name? If not,
   try the last 1-3 words as a place ("walmart erie", "pizza 16509").
3. Typo candidates for unknown words and for rare words one edit away from a
   much more common word ("sheets" -> "sheetz").
4. Candidates by SQL: category browse (R*Tree rings around the anchor), brand
   browse (ordered by squared distance), name search (FTS5, ordered by distance
   and by importance), short prefixes (1-3 characters: places by prefix through
   the partial index, POIs from `poi_prefix` in the 3x3 / 5x5 / 7x7 grid cells
   around the anchor). POIs of the category the query names, or whose whole
   name is a word of it ("Rest Area"), rank only as category hits.
5. Score = name tier (exact > derived exact > prefix > word prefix > contains >
   fuzzy) + importance (places weigh more) + distance decay `1/(1+d/scale)` +
   context bonus; category and brand hits have their own base score and a
   short distance scale. All numbers are in `ranking.json`.

Each result carries: `title`, `subtitle`, `category` label, `distance_km`
(always from the returned `lat`/`lon`), `lat`/`lon`, `kind`, `type`, `id`, plus
`score`/`how`/`tier` for debugging; house-number results add `house_number`,
`street_name`, `house_located`. The response adds `out_of_coverage` and
`position_ignored`.

## SQLite requirements (both targets)

No loadable extension and no custom tokenizer: FTS5 (built-in `unicode61` and
`trigram` tokenizers) and R*Tree, which androidx `sqlite-bundled` compiles in.
The trigram tokenizer needs SQLite 3.34 or newer, partial indexes 3.8.0 or
newer. `CROSS JOIN` fixes the join order of the category browse for small
boxes (plain SQLite syntax). No SQL math functions are used (distances and the
`poi_prefix` grid cell are computed in code). Tested with SQLite 3.49.1 (Windows
Python 3.12.10), 3.45.1 (Ubuntu 24.04 python3 3.12.3) and the 3.50.1 CLI
(the version androidx sqlite-bundled 2.7.x builds).
