"""Step 1 of the search index build: read one OSM PBF extract and keep what
search needs, in plain Python structures pickled to data/search/work/.

Reader: pyosmium (BSD-2-Clause) on libosmium (BSL-1.0). Build-time only.

Kept:
  boundaries  administrative boundaries (admin_level 4/6/7/8) and postal_code
              boundaries, as WKB multipolygons
  places      place=* nodes and areas (city, town, village, hamlet, suburb,
              neighbourhood, quarter, locality, island)
  roads       named (or ref'd) drivable highway ways, as WKB linestrings
  pois        nodes and areas that match a category rule, carry a brand, or
              are named amenities/shops/tourism/... ; areas use their centroid;
              natural=* and waterway=* objects only for the values a category
              rule names (beach, waterfall, cave_entrance, peak)
  parks       named parks, nature reserves and protected areas, as WKB
              multipolygons (a named natural place inside one is also found by
              the park's name; build.py)
  doors       on a point of interest that is a large building: "door", the point of its outline
              where customers go in (doors.py: a mapped entrance, else for a shop the side its
              parking is on), as (lon, lat, how)
  addrs       compact arrays of address points (street key, city, postcode,
              lon, lat) used only for ZIP places and postal-city context
  admin_relations / admin_member_ways
              tags and member-way geometry of every admin_level 4/6/8 relation,
              so build.py can rebuild boundaries that libosmium did not assemble
              (relations cut by the extract edge, e.g. the state itself)
"""
from __future__ import annotations

import array
import math
import time

import osmium
import osmium.filter
import osmium.geom
import shapely

import doors
import signals
import normalize
import rules

ROAD_CLASSES = {
    "motorway": 9, "trunk": 8, "primary": 7, "secondary": 6, "tertiary": 5,
    "unclassified": 4, "residential": 3, "living_street": 3, "road": 3,
    "motorway_link": 2, "trunk_link": 2, "primary_link": 2, "secondary_link": 2,
    "tertiary_link": 2, "service": 1, "pedestrian": 1, "track": 0, "busway": 1,
}
PLACE_TYPES = {"city", "town", "village", "hamlet", "suburb", "neighbourhood",
               "quarter", "locality", "island", "borough", "municipality"}
# keys that make a *named* object a searchable point of interest
POI_KEYS = ("amenity", "shop", "tourism", "leisure", "office", "craft",
            "healthcare", "historic", "aeroway", "emergency")
POI_SKIP = {
    "amenity": {"parking_space", "bench", "waste_basket", "bicycle_parking", "vending_machine",
                "waste_disposal", "recycling", "shelter", "hunting_stand", "loading_dock",
                "grit_bin", "post_box", "telephone", "clock", "fountain", "parking_entrance"},
    "leisure": {"pitch", "swimming_pool", "track", "garden", "picnic_table", "firepit",
                "slipway_ramp", "playground", "dog_park", "common"},
    "emergency": {"fire_hydrant", "defibrillator", "phone", "siren", "life_ring",
                  "fire_extinguisher", "suction_point", "water_tank", "access_point",
                  "assembly_point", "lifeguard", "fire_alarm_box"},
    "aeroway": {"runway", "taxiway", "apron", "gate", "holding_position", "windsock",
                "navigationaid", "parking_position", "hangar", "stopway"},
    "historic": {"boundary_stone", "milestone"},
}
POI_TAGS_KEEP = {
    "name", "alt_name", "short_name", "official_name", "brand", "brand:wikidata", "operator",
    "opening_hours", "phone", "contact:phone", "website", "contact:website",
    "addr:housenumber", "addr:street", "addr:city", "addr:postcode", "addr:state",
    "amenity", "shop", "tourism", "leisure", "office", "craft", "healthcare", "historic",
    "aeroway", "railway", "public_transport", "highway", "landuse", "boundary",
    "cuisine", "fuel:diesel", "fuel:HGV_diesel", "fuel:lpg", "hgv", "access", "emergency",
    "park_ride", "urgent_care", "healthcare:speciality", "atm", "toilets", "station",
    "train", "subway", "light_rail", "bus", "iata", "icao", "drink:beer", "microbrewery",
    "industrial", "sport", "government", "information", "service:vehicle:oil_change",
    "service:vehicle:inspection", "sanitary_dump_station", "wikidata", "protect_class",
    "covered", "capacity", "natural", "waterway",
}
FILTER_KEYS = ("place", "boundary", "highway", "amenity", "shop", "tourism", "leisure",
               "office", "craft", "healthcare", "historic", "aeroway", "emergency",
               "railway", "public_transport", "landuse", "brand", "brand:wikidata",
               "addr:postcode", "addr:street", "name", "natural", "waterway", "entrance")
# named parks, nature reserves and protected areas: their outlines are kept so a named natural
# place inside one is also found by the park's name ('presque isle beach'; categories.json in_park)
PARK_LEISURE = {"park", "nature_reserve"}
PARK_BOUNDARY = {"protected_area", "national_park"}


class Extract:
    def __init__(self, rulebook: "rules.RuleBook") -> None:
        self.rb = rulebook
        self.wkb = osmium.geom.WKBFactory()
        self.boundaries: list[dict] = []
        self.places: list[dict] = []
        self.roads: list[dict] = []
        self.pois: list[dict] = []
        self.parks: list[dict] = []
        # signals.py (item 36): the traffic lights as (lon, lat)
        self.signals: list[tuple] = []
        # natural=* / waterway=* objects are kept only for the values a category rule names
        # (beach, waterfall, cave_entrance, peak), not every named wood or water
        self.natural_values = rulebook.tag_values("natural")
        self.waterway_values = rulebook.tag_values("waterway")
        # addresses as interned ids + float arrays
        self.addr_street = array.array("i")
        self.addr_city = array.array("i")
        self.addr_pc = array.array("i")
        self.addr_lon = array.array("d")
        self.addr_lat = array.array("d")
        self.strings: dict[str, int] = {"": 0}
        self.counts = {"nodes_seen": 0, "ways_seen": 0, "areas_seen": 0, "area_wkb_errors": 0}
        # front doors (doors.py): customer entrances by node id, customer parking areas, and the
        # large POI buildings waiting for their door until every parking area is read
        self.entrances: dict[int, tuple] = {}
        self.parkings: list = []
        self.aisles: list = []
        self.door_todo: list[tuple] = []
        self.area_errors: list[tuple] = []

    def sid(self, s: str | None) -> int:
        if not s:
            return 0
        i = self.strings.get(s)
        if i is None:
            i = len(self.strings)
            self.strings[s] = i
        return i

    # ---- helpers -------------------------------------------------------
    def _add_addr(self, tags, lon: float, lat: float) -> None:
        st = tags.get("addr:street")
        pc = tags.get("addr:postcode")
        if not st and not pc:
            return
        self.addr_street.append(self.sid(normalize.street_key(st) if st else ""))
        self.addr_city.append(self.sid(normalize.key(tags.get("addr:city") or "")))
        pcs = (pc or "").strip()[:5]
        self.addr_pc.append(self.sid(pcs if pcs.isdigit() and len(pcs) == 5 else ""))
        self.addr_lon.append(lon)
        self.addr_lat.append(lat)

    def _natural(self, tags) -> bool:
        return tags.get("natural") in self.natural_values or tags.get("waterway") in self.waterway_values

    def _is_poi(self, tags: dict) -> bool:
        if "brand" in tags or "brand:wikidata" in tags:
            return True
        if "name" not in tags:
            return False
        for k in POI_KEYS:
            v = tags.get(k)
            if v and v != "no" and v not in POI_SKIP.get(k, ()):
                return True
        if tags.get("railway") in ("station", "halt") or tags.get("public_transport") == "station":
            return True
        return False

    def _poi(self, otype: str, oid: int, tags: dict, lon: float, lat: float, is_area: bool) -> dict | None:
        cats = self.rb.categories_for(tags)
        named = "name" in tags or "brand" in tags
        if not cats and not self._is_poi(tags):
            return None
        if not named and not any(self.rb.unnamed_ok(c) for c in cats):
            return None
        kept = {k: v for k, v in tags.items() if k in POI_TAGS_KEEP}
        poi = {"t": otype, "id": oid, "lon": lon, "lat": lat, "area": is_area, "tags": kept, "cats": cats}
        self.pois.append(poi)
        return poi

    def finish_doors(self) -> dict:
        """Gives each waiting large building its door (doors.door) once every parking area is read."""
        tree = shapely.STRtree(self.parkings) if self.parkings else None
        aisles = shapely.from_wkb(self.aisles) if self.aisles else []
        atree = shapely.STRtree(aisles) if len(aisles) else None
        how_counts = {"main": 0, "entrance": 0, "parking": 0, "none": 0}
        for poi, geom, entrances, retail in self.door_todo:
            c = geom.centroid
            dlat = (doors.PARKING_WITHIN_M + 5) / 111320.0
            dlon = dlat / max(0.01, math.cos(math.radians(c.y)))
            x0, y0, x1, y1 = geom.bounds
            box = shapely.box(x0 - dlon, y0 - dlat, x1 + dlon, y1 + dlat)
            near = [self.parkings[i] for i in sorted(tree.query(box))] if tree is not None else []
            near_aisles = [aisles[i] for i in sorted(atree.query(box))] if atree is not None else []
            try:
                d = doors.door(geom, entrances, near, retail, near_aisles)
            except shapely.errors.GEOSException:
                d = None
            how_counts[d[2] if d else "none"] += 1
            if d is not None:
                poi["door"] = d
        self.door_todo = []
        self.parkings = []
        self.aisles = []
        return how_counts

    # ---- object handlers ----------------------------------------------
    def node(self, n) -> None:
        self.counts["nodes_seen"] += 1
        if not n.location.valid():
            return
        tags = n.tags
        lon, lat = n.location.lon, n.location.lat
        if signals.is_signal(tags):
            self.signals.append((lon, lat))
        if "addr:street" in tags or "addr:postcode" in tags:
            self._add_addr(tags, lon, lat)
        place = tags.get("place")
        if place in PLACE_TYPES and "name" in tags:
            self.places.append({"t": "n", "id": n.id, "lon": lon, "lat": lat,
                                "tags": dict((t.k, t.v) for t in tags)})
            return
        if "entrance" in tags:
            kind = doors.entrance_kind(tags)
            if kind:
                self.entrances[n.id] = (lon, lat, kind)
        d = None
        for k in ("amenity", "shop", "tourism", "leisure", "office", "craft", "healthcare",
                  "historic", "aeroway", "emergency", "railway", "public_transport", "highway",
                  "brand", "brand:wikidata", "landuse"):
            if k in tags:
                d = dict((t.k, t.v) for t in tags)
                break
        if d is None and self._natural(tags):
            d = dict((t.k, t.v) for t in tags)
        if d is not None:
            self._poi("n", n.id, d, lon, lat, False)

    def way(self, w) -> None:
        self.counts["ways_seen"] += 1
        tags = w.tags
        hw = tags.get("highway")
        if hw in ROAD_CLASSES and ("name" in tags or "ref" in tags) and tags.get("area") != "yes":
            try:
                wkb = self.wkb.create_linestring(w)
            except Exception:
                return
            self.roads.append({
                "id": w.id, "hw": hw, "name": tags.get("name"), "ref": tags.get("ref"),
                "alt": [tags[k] for k in ("alt_name", "official_name", "short_name", "loc_name") if k in tags],
                "wkb": wkb,
            })
        if hw == "service" and doors.aisle_counts(tags):
            try:
                self.aisles.append(self.wkb.create_linestring(w))
            except Exception:
                pass
        if ("addr:street" in tags or "addr:postcode" in tags) and len(w.nodes) > 0:
            loc = w.nodes[0].location
            if loc.valid():
                self._add_addr(tags, loc.lon, loc.lat)

    def area(self, a) -> None:
        self.counts["areas_seen"] += 1
        tags = a.tags
        boundary = tags.get("boundary")
        place = tags.get("place")
        is_boundary = (boundary == "administrative" and tags.get("admin_level") in ("4", "6", "7", "8")) \
            or boundary == "postal_code"
        want_place = place in PLACE_TYPES and "name" in tags
        want_park = "name" in tags and (tags.get("leisure") in PARK_LEISURE
                                        or tags.get("boundary") in PARK_BOUNDARY)
        d = None
        poi = False
        if not is_boundary and not want_place:
            # cheap pre-check before building a dict for every building/landuse area
            hit = False
            for k in ("amenity", "shop", "tourism", "leisure", "office", "craft", "healthcare",
                      "historic", "aeroway", "railway", "public_transport", "highway", "brand",
                      "brand:wikidata", "landuse", "boundary", "emergency"):
                if k in tags:
                    hit = True
                    break
            if not hit and not self._natural(tags):
                return
            d = dict((t.k, t.v) for t in tags)
            cats = self.rb.categories_for(d)
            poi = bool(cats or self._is_poi(d)) and d.get("highway") not in ROAD_CLASSES  # road areas are not POIs
            if not poi and not want_park:
                return
        try:
            wkb = self.wkb.create_multipolygon(a)
        except Exception:
            self.counts["area_wkb_errors"] += 1
            if len(self.area_errors) < 200:
                self.area_errors.append(("w" if a.from_way() else "r", a.orig_id(), tags.get("name"),
                                         tags.get("boundary"), tags.get("admin_level")))
            return
        otype = "w" if a.from_way() else "r"
        oid = a.orig_id()
        if is_boundary:
            self.boundaries.append({"t": otype, "id": oid, "tags": dict((t.k, t.v) for t in tags), "wkb": wkb})
            return
        geom = shapely.from_wkb(wkb)
        if d is not None and doors.parking_counts(d):
            self.parkings.append(geom)
        if want_park and not want_place:
            self.parks.append({"t": otype, "id": oid, "name": tags.get("name"), "wkb": wkb})
            if not poi:
                return
        c = geom.centroid
        if want_place:
            self.places.append({"t": otype, "id": oid, "lon": c.x, "lat": c.y,
                                "tags": dict((t.k, t.v) for t in tags),
                                "bbox": geom.bounds})
            return
        kept = self._poi(otype, oid, d, c.x, c.y, True)
        if kept is not None and d.get("building") not in (None, "no") and doors.is_large(geom):
            ents = sorted({(nr.ref, *self.entrances[nr.ref]) for ring in a.outer_rings() for nr in ring
                           if nr.ref in self.entrances})
            self.door_todo.append((kept, geom, ents, doors.is_retail(d)))


class _MemberWays(osmium.SimpleHandler):
    """Keeps the geometry of ways that belong to administrative boundary relations,
    so boundaries libosmium could not assemble can be rebuilt (build.py)."""

    def __init__(self, wanted: set[int]):
        super().__init__()
        self.wanted = wanted
        self.wkb = osmium.geom.WKBFactory()
        self.found: dict[int, bytes] = {}

    def way(self, w):
        if w.id in self.wanted and w.id not in self.found:
            try:
                self.found[w.id] = self.wkb.create_linestring(w)
            except Exception:
                pass


def run(pbf: str, rulebook: "rules.RuleBook", threads: int = 4) -> dict:
    t0 = time.time()
    ex = Extract(rulebook)
    pool = osmium.io.ThreadPool(threads)
    # pre-pass over relations: administrative boundaries and their member ways
    admin_rels = []
    member_ways: set[int] = set()
    for r in osmium.FileProcessor(pbf, osmium.osm.RELATION, thread_pool=pool):
        t = r.tags
        if t.get("boundary") == "administrative" and t.get("admin_level") in ("4", "6", "8"):
            ways = [(m.ref, m.role) for m in r.members if m.type == "w"]
            admin_rels.append({"id": r.id, "tags": dict((x.k, x.v) for x in t), "ways": ways})
            member_ways.update(w for w, _ in ways)
    grab = _MemberWays(member_ways)
    fp = (osmium.FileProcessor(pbf, thread_pool=pool)
          .with_locations("flex_mem")
          .with_areas()
          .with_filter(osmium.filter.KeyFilter(*FILTER_KEYS))
          .handler_for_filtered(grab))
    header = fp.header
    timestamp = header.get("osmosis_replication_timestamp", "") if hasattr(header, "get") else ""
    for obj in fp:
        if obj.is_node():
            ex.node(obj)
        elif obj.is_way():
            if obj.id in member_ways:
                grab.way(obj)
            ex.way(obj)
        elif obj.is_area():
            ex.area(obj)
    door_counts = ex.finish_doors()
    strings = [None] * len(ex.strings)
    for s, i in ex.strings.items():
        strings[i] = s
    out = {
        "source": pbf, "timestamp": timestamp, "seconds": time.time() - t0,
        "boundaries": ex.boundaries, "places": ex.places, "roads": ex.roads, "pois": ex.pois,
        "parks": ex.parks, "signals": ex.signals,
        "addr": {"street": ex.addr_street, "city": ex.addr_city, "pc": ex.addr_pc,
                 "lon": ex.addr_lon, "lat": ex.addr_lat, "strings": strings},
        "area_errors": ex.area_errors,
        "admin_relations": admin_rels, "admin_member_ways": grab.found,
        "counts": dict(ex.counts, boundaries=len(ex.boundaries), places=len(ex.places),
                       roads=len(ex.roads), pois=len(ex.pois), parks=len(ex.parks), addrs=len(ex.addr_lon),
                       signals=len(ex.signals),
                       admin_relations=len(admin_rels), admin_member_ways=len(grab.found),
                       admin_member_ways_wanted=len(member_ways), entrances=len(ex.entrances),
                       **{f"doors_{k}": v for k, v in door_counts.items()}),
    }
    return out
