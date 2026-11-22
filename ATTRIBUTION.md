# Attribution and data licences

## OpenStreetMap (the map, routing and search data of every pack)

Map data © OpenStreetMap contributors. https://www.openstreetmap.org/copyright

The packs contain information from OpenStreetMap, which is made available here under the
**Open Database License (ODbL) 1.0**: https://opendatacommons.org/licenses/odbl/1-0/

- The map part (PMTiles vector tiles) is a Produced Work of OpenStreetMap; the routing tiles, the
  search index and the house-number database are Derivative Databases of it. They are offered here
  under the ODbL 1.0, as the ODbL asks, and may be used, shared and adapted under its terms.
- Anyone who shows, shares or adapts a pack must keep this credit: "© OpenStreetMap contributors",
  with the link above, and offer an adapted database under the ODbL.
- The inputs were Geofabrik's daily state extracts of OpenStreetMap (download.geofabrik.de), the
  OpenStreetMap water and land polygons of osmdata.openstreetmap.de (ODbL) and, for the routing
  graph's time zones, timezone-boundary-builder (ODbL; build time only). Each release names the
  date of its OpenStreetMap data.

The Beamline app shows the credit of the parts installed on every map screen.

The comma files of a pack build are made from the same OpenStreetMap data and carry the same licence
and credit: `roads.tar` (StarPilot's road-line tiles, cut from the pack's map: a Produced Work) and
`mapd.tar` (the roads in Mapd's format, made from the same extract by Mapd's own generator: a
Derivative Database). `search-code.tar` is Beamline's own search code (MIT, `LICENSE`). Mapd
(StarPilot's `mapd_repo`, MIT) and Go run at build time only; nothing of their code is in a file
published here. `comma.json` names a pyvalhalla wheel (MIT) that a device fetches from PyPI itself.

## House numbers (the optional addresses part)

- **U.S. Census Bureau, TIGER/Line Shapefiles 2026** (address ranges, county subdivisions) and the
  2025 cartographic boundary counties: a work of the US government, not subject to copyright in the
  United States. Credit: U.S. Census Bureau.
- **USDOT National Address Database**, release 24: US public domain
  (http://www.usa.gov/publicdomain/label/1.0/). Credit: U.S. Department of Transportation.
- **City of Scranton GIS**, 911 address points: public domain, Open Data Commons PDDL 1.0. Credit:
  City of Scranton GIS (appreciated by the publisher).
- OpenStreetMap address points: ODbL, as above.

## Low-zoom map context

- **Natural Earth** (naturalearthdata.com): public domain.
- **Wikidata QRank** (CC0 1.0): used at build time to rank places; nothing of it is copied into a pack
  beyond the ranking's effect.

## Search words

The search index includes category words from the iD editor's tagging schema (ISC licence); the full
notice is in `packbuilder/search/THIRD_PARTY_NOTICES.md` and in every search index's `meta` table.
