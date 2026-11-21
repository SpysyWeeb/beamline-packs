"""Resolve typed or parsed addresses against a pa-addresses.sqlite file.

    idx = AddressIndex("data/addresses/pa-addresses.sqlite")
    idx.geocode("123 n main st, erie pa 16501")  -> [ {lat, lon, accuracy, ...}, ... ]

Standard library only. Strategy (weights in lookup_config.json):
  1. street candidates: exact street_key, then highway alternatives
     ('rte 8' <-> 'sr 8'), then street_core (directionals/type ignored), then
     key variants (a misspelt street type 'sreet' -> 'st', a bare number used as
     an ordinal 'w 8 st' -> 'w 8th st'), then edit distance <= 2 on keys inside
     the typed locality (typos);
  2. for those streets: an address point with the same house number
     (accuracy 'point'; '123a' falls back to the point of '123'), else a
     house-number range containing the number, interpolated along the edge
     (accuracy 'interpolated'), else the nearest numbered point on the street
     (accuracy 'street');
  3. rank by locality agreement (ZIP, city/postal city, municipality), match
     quality, accuracy class, source.

Contract for callers: when the typed street is not found in the typed town, the
lookup still returns the best match elsewhere in the state, with
locality_match = False. A UI must show such a result as a guess ("not found in
Erie; did you mean ... in Phoenixville?"), not as the answer. A typed state
other than the pack's state gives state_mismatch = True (and locality_match =
False) on every result. Input with no street (only a place or a ZIP) returns [];
place search belongs to the search track.

Works on every pack variant: a file without the addresses table (ranges only) or
without ranges/edges (points only) is answered from what it has.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .geo import decode_line, interpolate
from .normalize import (highway_alternatives, house_number, key_variants, osa_distance,  # noqa: F401
                        place_key, street_core, tables)
from .parser import parse

CONFIG_FILE = Path(__file__).with_name("lookup_config.json")


def _parts(key: str):
    """(leading directional, trailing type, trailing directional) of a street key, None if absent."""
    t = tables()
    toks = key.split()
    pre = toks[0] if len(toks) > 1 and toks[0] in t.dir else None
    post = toks[-1] if len(toks) > 1 and toks[-1] in t.dir else None
    body = toks[(1 if pre else 0):(len(toks) - 1 if post else len(toks))]
    typ = body[-1] if len(body) > 1 and body[-1] in t.type_canon else None
    return pre, typ, post


def compatible(typed: str, cand: str) -> bool:
    """A core-key candidate may fill in parts the user left out, but not contradict ones typed
    ('pittsburgh st' -> 'e pittsburgh st' is fine, 'e pittsburgh st' -> 'w pittsburgh st' is not)."""
    return all(a is None or b is None or a == b for a, b in zip(_parts(typed), _parts(cand)))


class AddressIndex:
    def __init__(self, path, config_path: Path = CONFIG_FILE):
        self.con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, check_same_thread=False)
        self.cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.tables = {r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.known_places = {r[0] for r in self.con.execute("SELECT DISTINCT place_key FROM places")}
        self.state = None
        if "meta" in self.tables:
            row = self.con.execute("SELECT value FROM meta WHERE key = 'state'").fetchone()
            self.state = row[0].lower() if row and row[0] else None
        self._place_zips = {}

    # ------------------------------------------------------------ helpers
    def place_zips(self, ck: str) -> set:
        if ck not in self._place_zips:
            self._place_zips[ck] = {r[0] for r in self.con.execute(
                "SELECT DISTINCT zip FROM places WHERE place_key = ? AND zip IS NOT NULL", (ck,))}
        return self._place_zips[ck]

    def _streets(self, where: str, args) -> list:
        q = ("SELECT s.id, s.street, s.street_key, l.zip, l.city, l.city_key, l.municipality, l.muni_key, "
             "l.county_fips, s.lat_e6, s.lon_e6 FROM streets s JOIN localities l ON l.id = s.locality_id WHERE " + where)
        return self.con.execute(q, args).fetchall()

    def _loc_score(self, row, z, ck) -> float:
        w = self.cfg["locality_score"]
        s = 0.0
        if z and row[3] == z:
            s += w["zip"]
        if ck:
            if row[5] == ck:
                s += w["city"]
            elif row[7] == ck:
                s += w["municipality"]
            elif row[3] and row[3] in self.place_zips(ck):
                s += w["zip_of_named_place"]
        return s

    def _locality_streets(self, z, ck) -> list:
        conds, args = [], []
        if z:
            conds.append("l.zip = ?")
            args.append(z)
        if ck:
            conds.append("l.city_key = ? OR l.muni_key = ?")
            args += [ck, ck]
            zs = sorted(self.place_zips(ck))
            if zs:
                conds.append("l.zip IN (%s)" % ",".join("?" * len(zs)))
                args += zs
        if not conds:
            return []
        return self._streets("(" + ") OR (".join(conds) + ")", args)

    def _key_candidates(self, key: str, exact_kind: str):
        """(exact + highway-alternative rows, compatible core rows) for one key."""
        exact = [(r, exact_kind) for r in self._streets("s.street_key = ?", (key,))]
        alts = highway_alternatives(key)
        if alts:
            exact += [(r, "highway_alternative" if exact_kind == "exact_key" else exact_kind) for r in
                      self._streets("s.street_key IN (%s)" % ",".join("?" * len(alts)), alts)]
        core_kind = "core_key" if exact_kind == "exact_key" else exact_kind
        core = [(r, core_kind) for r in
                self._streets("s.street_core = ? AND s.street_key != ?", (street_core(key), key))
                if compatible(key, r[2])]
        return exact, core

    # ------------------------------------------------------------ main
    def candidate_tiers(self, key: str, z=None, ck=None):
        """[(list of (street_row, match_kind), inside_typed_locality)], most specific first.

        No locality typed: exact key (+ highway alternatives), then street_core, then key variants.
        Locality typed: exact, core, key-variant and typo-tolerant keys inside the locality,
        then exact, core and key variants anywhere in the state."""
        exact, core = self._key_candidates(key, "exact_key")
        inloc = lambda t: [(r, k) for r, k in t if self._loc_score(r, z, ck) > 0]
        # key variants (usually none: only for a misspelt type word or a bare-number ordinal), smallest
        # change first; each tier takes the first variant that finds a street
        variant, variant_inloc = [], []
        for v in key_variants(key):
            e2, c2 = self._key_candidates(v, "key_variant")
            rows = e2 + c2
            if rows and not variant:
                variant = rows
            if (z or ck) and not variant_inloc:
                variant_inloc = inloc(rows)
            if variant and (variant_inloc or not (z or ck)):
                break
        if not (z or ck):
            return [(t, False) for t in (exact, core, variant) if t]
        tiers = [(inloc(exact), True), (inloc(core), True), (variant_inloc, True)]
        if not any(t for t, _ in tiers):
            fz = self.cfg["fuzzy"]
            pool = self._locality_streets(z, ck)
            limit = fz["max_distance"] if len(key) >= fz["min_key_length_for_2_edits"] else 1
            dist = {k: osa_distance(key, k, limit) for k in {r[2] for r in pool}}
            best = min(dist.values(), default=limit + 1)
            if best <= limit:
                tiers.append(([(r, "fuzzy_key") for r in pool if dist[r[2]] == best], True))
        tiers += [(exact, False), (core, False), (variant, False)]
        return [(t, loc) for t, loc in tiers if t]

    def _resolve_streets(self, cands, hnn, hni, z, ck) -> list:
        cfg = self.cfg
        by_id = {r[0]: (r, kind) for r, kind in cands}
        ids = list(by_id)
        results = []

        def add(street_row, kind, lat, lon, acc, src, hn_out, extra=None):
            score = (self._loc_score(street_row, z, ck) + cfg["match_score"][kind]
                     + cfg["accuracy_score"][acc] + cfg["source_score"].get(src, 0))
            d = {"lat": lat, "lon": lon, "accuracy": acc, "source": src, "house_number": hn_out,
                 "street": street_row[1], "street_key": street_row[2], "zip": street_row[3],
                 "city": street_row[4], "municipality": street_row[6], "county_fips": street_row[8],
                 "match": kind, "score": round(score, 2),
                 "locality_match": (self._loc_score(street_row, z, ck) > 0) if (z or ck) else None}
            if extra:
                d.update(extra)
            results.append(d)

        qmarks = ",".join("?" * len(ids))
        has_points, has_ranges = "addresses" in self.tables, "ranges" in self.tables
        if hnn is not None:
            if has_points:
                tries = [hnn] + ([str(hni)] if str(hni) != hnn else [])  # '123a' -> the point of '123'
                for want in tries:
                    for sid, h, unit, la, lo, src, aid in self.con.execute(
                            f"SELECT street_id, hn, unit, lat_e6, lon_e6, source, id FROM addresses "
                            f"WHERE street_id IN ({qmarks}) AND hn = ?", ids + [want]):
                        r, kind = by_id[sid]
                        add(r, kind, la / 1e6, lo / 1e6, "point", src, h,
                            {"unit": unit, "address_id": aid, **({"base_number_of": hnn} if want != hnn else {})})
                    if results:
                        break
            have_point = {x["street_key"] + "|" + str(x["zip"]) for x in results}
            if has_ranges:
                for sid, edge, side, a, b, par, src in self.con.execute(
                        f"SELECT street_id, edge_id, side, hn_from, hn_to, parity, source FROM ranges "
                        f"WHERE street_id IN ({qmarks}) AND hn_lo <= ? AND hn_hi >= ?", ids + [hni, hni]):
                    if par == "O" and hni % 2 == 0 or par == "E" and hni % 2 == 1:
                        continue
                    r, kind = by_id[sid]
                    if r[2] + "|" + str(r[3]) in have_point:
                        continue  # a point on this street in this ZIP already answers it
                    geom = decode_line(self.con.execute("SELECT geom FROM edges WHERE id = ?", (edge,)).fetchone()[0])
                    frac = 0.5 if a == b else (hni - a) / (b - a)
                    lon, lat = interpolate(geom, frac, side, cfg["interpolation_offset_m"])
                    add(r, kind, lat, lon, "interpolated", src, hnn, {"edge_id": edge, "side": side})
            if not results and has_points:  # nearest numbered point on the same street(s)
                for sid, h, hint, la, lo, src in self.con.execute(
                        f"SELECT street_id, hn, hn_int, lat_e6, lon_e6, source FROM addresses "
                        f"WHERE street_id IN ({qmarks}) AND hn_int IS NOT NULL "
                        f"ORDER BY abs(hn_int - ?) LIMIT 20", ids + [hni]):
                    r, kind = by_id[sid]
                    add(r, kind, la / 1e6, lo / 1e6, "street", src, h, {"hn_gap": abs(hint - hni)})
        if not results:  # street only
            for r, kind in cands:
                if r[9] is not None:
                    add(r, kind, r[9] / 1e6, r[10] / 1e6, "street", "osm", None)
        results.sort(key=lambda x: (-x["score"], x.get("hn_gap", 0)))
        return results

    def resolve(self, p: dict, limit=None) -> list:
        """Resolve parsed fields (parser.parse output) to ranked positions."""
        limit = limit or self.cfg["max_results"]
        key = p.get("street_key")
        if not key:
            return []
        z = p.get("zip")
        ck = p.get("city_key") or (place_key(p["city"]) if p.get("city") else None)
        hn = p.get("house_number")
        hnn, hni = house_number(hn) if hn else (None, None)
        rejected = hni is not None and hni > self.cfg["max_house_number"]
        if rejected:  # '99999999999999999999 Main St': not a house number; answer with the street
            hnn = hni = None
        state = (p.get("state") or "").lower() or None
        mismatch = bool(state and self.state and state != self.state)
        fallback = []
        out = None
        for cands, in_loc in self.candidate_tiers(key, z, ck):
            if fallback and not in_loc and fallback[0].get("locality_match"):
                break  # the right street in the typed town beats an exact number in another town
            res = self._resolve_streets(cands, hnn, hni, z, ck)
            if any(x["accuracy"] != "street" for x in res) or (hnn is None and res):
                out = res[:limit]
                break
            if res and not fallback:
                fallback = res
        out = out if out is not None else fallback[:limit]
        for x in out:
            if rejected:
                x["house_number_rejected"] = hn
            if mismatch:
                x["state_mismatch"] = True
                x["locality_match"] = False
        return out

    def geocode(self, text: str, limit=None) -> list:
        return self.resolve(parse(text, self.known_places), limit)
