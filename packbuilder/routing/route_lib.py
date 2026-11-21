"""Small helpers around pyvalhalla's Actor: requests from presets, route summaries.

No UI code here; the same request building is what the app does on the device.
"""
from __future__ import annotations

import itertools
import json
import math
import sys
from pathlib import Path

import valhalla

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

PRESETS_FILE = C.HERE / "presets.json"
TRIPS_FILE = C.HERE / "trips.json"
PRESETS_FORMAT = "beamline-route-presets/2"
PRESETS_FORMAT_1 = "beamline-route-presets/1"

START_TYPES = {1, 2, 3}
END_TYPES = {4, 5, 6}
UTURN_TYPES = {12, 13}

# Road kinds an option can avoid (the maneuver flag, and the hard exclusion that names it).
ROAD_FLAGS = ("highway", "toll")
ROAD_WORDS = {"highway": "highways", "toll": "toll roads"}
# Keys the planner runs (what compose() must give, and what a format-1 preset holds besides id,
# label and notes). Two presets with the same values here route the same way.
RUN_KEYS = ("costing", "costing_options", "fallback_costing_options", "fallback_when", "select", "max_time_increase",
            "max_time_extra_s", "alternates", "candidates", "fallback_candidates")
# Preset keys route_lib.request() would apply but the Kotlin planner does not: refused in the file.
UNSUPPORTED_KEYS = ("request_options",)

# The presets of format 1 as combinations of format 2 (route_eval.py and test_routing.py use these ids);
# Shortest left in batch D (docs/next-round.md item 13), so five of the six.
LEGACY_COMBINATIONS = {
    "fastest": ("fastest", ()), "simpler": ("simpler", ()), "eco": ("eco", ()),
    "avoid_highways": ("fastest", ("avoid_highways",)), "avoid_tolls": ("fastest", ("avoid_tolls",)),
}


def load_preset_file(path: Path = PRESETS_FILE) -> dict:
    """presets.json format 2, checked (ValueError with every problem when it is not valid)."""
    pf = C.load_json(path)
    errs = validate_preset_file(pf)
    if errs:
        raise ValueError(f"{path.name}: " + "; ".join(errs))
    return pf


def load_presets() -> dict:
    """The format-1 presets (fastest, simpler, eco, avoid_highways, avoid_tolls),
    composed from presets.json format 2 (LEGACY_COMBINATIONS)."""
    pf = load_preset_file()
    return {k: dict(compose(t, o, pf), id=k) for k, (t, o) in LEGACY_COMBINATIONS.items()}


def load_presets_format1(path: Path) -> dict:
    """A format-1 presets file (kept for the old reference: android/routing/testdata/reference/presets-format1.json)."""
    pf = C.load_json(path)
    if pf.get("format") != PRESETS_FORMAT_1:
        raise ValueError(f"{path.name}: format is not {PRESETS_FORMAT_1}")
    return {p["id"]: p for p in pf["presets"]}


def option_flags(option: dict) -> list[str]:
    """Road kinds an option avoids: the flags whose hard exclusion (exclude_highways, exclude_tolls) it sets."""
    return [f for f in ROAD_FLAGS if option.get("costing_options", {}).get(f"exclude_{f}s") is True]


def compose(type_id: str, option_ids=(), pf: dict | None = None) -> dict:
    """One route type with a set of options as the format-1 preset the planner runs
    (docs/architecture.md 2.11 "Composition"; Kotlin: PresetSet.compose).

    Every request of the type (its one request, or each pool candidate) gets the type's
    costing_options, then the candidate's, then each selected option's costing_options on top
    (options in file order). The fallback request(s) take each option's fallback_costing_options
    instead. fallback_when maps are united (the smaller limit wins), except for a type with
    "option_fallback_when": false. With no options the result equals format 1's preset of the same
    id; "options" and "avoids" are added only when options are selected."""
    pf = pf if pf is not None else load_preset_file()
    types = {t["id"]: t for t in pf["route_types"]}
    if type_id not in types:
        raise KeyError(f"no route type {type_id}")
    known = {o["id"] for o in pf["options"]}
    unknown = set(option_ids) - known
    if unknown:
        raise KeyError(f"no option {sorted(unknown)}")
    sel = [o for o in pf["options"] if o["id"] in set(option_ids)]
    t = types[type_id]

    def merged(*parts: dict) -> dict:
        d: dict = {}
        for p in parts:
            d.update(p)
        return d

    base = t.get("costing_options", {})
    opts = [o["costing_options"] for o in sel]
    fbs = [o["fallback_costing_options"] for o in sel]
    out = {"id": "+".join([type_id] + [o["id"] for o in sel]), "label": t["label"], "costing": t.get("costing", "auto")}
    if "select" in t:
        out["select"] = t["select"]
        out["max_time_increase"] = t["max_time_increase"]
        if "max_time_extra_s" in t:
            out["max_time_extra_s"] = t["max_time_extra_s"]
        if "alternates" in t:
            out["alternates"] = t["alternates"]
        out["candidates"] = [merged(base, c, *opts) for c in t["candidates"]]
        if sel:
            out["fallback_candidates"] = [merged(base, c, *fbs) for c in t["candidates"]]
    else:
        out["costing_options"] = merged(base, *opts)
        if sel:
            out["fallback_costing_options"] = merged(base, *fbs)
    if sel and t.get("option_fallback_when", True):
        when: dict = {}
        for o in sel:
            for k, v in o.get("fallback_when", {}).items():
                when[k] = min(v, when.get(k, v))
        if when:
            out["fallback_when"] = when
    if sel:
        out["options"] = [o["id"] for o in sel]
        out["avoids"] = [f for f in ROAD_FLAGS if any(f in option_flags(o) for o in sel)]
    return out


def same_run(a: dict, b: dict) -> bool:
    """Two presets that route the same way (equal RUN_KEYS; an absent key equals an empty one)."""
    return all((a.get(k) or None) == (b.get(k) or None) for k in RUN_KEYS)


def validate_preset_file(pf: dict) -> list[str]:
    """Problems with presets.json format 2: the format, ids, types, options, the default, option
    conflicts (two options that set the same key are refused), and every composition."""
    errs: list[str] = []
    if pf.get("format") != PRESETS_FORMAT:
        return [f"format is not {PRESETS_FORMAT}"]
    types, options = pf.get("route_types", []), pf.get("options", [])
    ids = [t.get("id") for t in types] + [o.get("id") for o in options]
    if len(set(ids)) != len(ids) or None in ids:
        errs.append(f"route type and option ids must be present and unique: {ids}")
    for item in types + options:
        for k in UNSUPPORTED_KEYS:
            if k in item:
                errs.append(f"{item.get('id')}: {k} is not supported by the app planner")
    for t in types:
        if not isinstance(t.get("offered"), bool):
            errs.append(f"{t.get('id')}: offered must be true or false")
        if "option_fallback_when" in t and not isinstance(t["option_fallback_when"], bool):
            errs.append(f"{t.get('id')}: option_fallback_when must be true or false")
    if not any(t.get("offered") for t in types):
        errs.append("no route type is offered")
    for o in options:
        for k in ("label", "costing_options", "fallback_costing_options"):
            if k not in o:
                errs.append(f"{o.get('id')}: {k} missing")
        for k in o.get("fallback_when", {}):
            if k not in ("highway_km_over", "toll_km_over"):
                errs.append(f"{o.get('id')}: unknown fallback_when condition {k}")
    for i, a in enumerate(options):
        for b in options[i + 1:]:
            ka = set(a.get("costing_options", {})) | set(a.get("fallback_costing_options", {}))
            kb = set(b.get("costing_options", {})) | set(b.get("fallback_costing_options", {}))
            if ka & kb:
                errs.append(f"options {a.get('id')} and {b.get('id')} both set {sorted(ka & kb)}")
    d = pf.get("default", {})
    if d.get("route_type") not in {t.get("id") for t in types if t.get("offered")}:
        errs.append(f"default route_type {d.get('route_type')} is not an offered type")
    if not set(d.get("options", [])) <= {o.get("id") for o in options}:
        errs.append(f"default options {d.get('options')} unknown")
    errs += validate_every_request(pf.get("every_request"))
    if errs:
        return errs
    oids = [o["id"] for o in options]
    for t in types:
        for n in range(len(oids) + 1):
            for combo in itertools.combinations(oids, n):
                errs += validate_preset(compose(t["id"], combo, pf))
    return errs


ROAD_CLASSES = ("motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "service_other")
SEARCH_FILTER_KEYS = {"min_road_class": str, "max_road_class": str, "exclude_tunnel": bool, "exclude_bridge": bool,
                      "exclude_ramp": bool, "exclude_closures": bool}


# The Valhalla edge uses a stop may end on when it is a point of interest (batch E): a driveway or a
# parking aisle; never an alley or a drive-through lane (Kotlin: EveryRequest.LOT_USES).
LOT_USES = ("driveway", "parking_aisle")
# The same when the lot is looked for around the place's door (docs/next-round.md item 28): a plain
# service road too, the lane along a store's front (Kotlin: EveryRequest.DOOR_USES).
DOOR_USES = LOT_USES + ("service_road",)
# At most this many exclude_polygons rings of 5 vertices (the app's Valhalla config allows 100 vertices).
MAX_RINGS = 20


def validate_every_request(er: dict | None) -> list[str]:
    """Problems with presets.json "every_request" (docs/architecture.md 2.11): costing options valid
    for 3.9.1 auto, stop options limited to search_filter and search_cutoff, the arrival side rule."""
    if er is None:
        return ["every_request missing"]
    errs = [f"every_request costing_options: {e}" for e in validate_costing_options(er.get("costing_options", {}))]
    for k, v in er.get("stops", {}).items():
        if k == "search_filter":
            for fk, fv in v.items():
                if fk not in SEARCH_FILTER_KEYS or not isinstance(fv, SEARCH_FILTER_KEYS[fk]):
                    errs.append(f"every_request stops search_filter: bad {fk}={fv!r}")
                elif fk.endswith("_road_class") and fv not in ROAD_CLASSES:
                    errs.append(f"every_request stops search_filter: unknown road class {fv}")
        elif k == "search_cutoff":
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                errs.append("every_request stops search_cutoff must be a positive whole number of metres")
        else:
            errs.append(f"every_request stops: {k} is not supported by the app planner")
    side = er.get("arrival_side")
    if side is not None:
        if side.get("preferred_side") not in ("same", "opposite"):
            errs.append("every_request arrival_side preferred_side must be same or opposite")
        if side.get("street_side_cutoff", "service_other") not in ROAD_CLASSES:
            errs.append("every_request arrival_side street_side_cutoff is not a road class")
        mx = side.get("max_extra_s")
        if isinstance(mx, bool) or not isinstance(mx, (int, float)) or mx < 0:
            errs.append("every_request arrival_side max_extra_s must be a number >= 0")
        mm = side.get("max_extra_maneuvers")
        if mm is not None and (isinstance(mm, bool) or not isinstance(mm, int) or mm < 0):
            errs.append("every_request arrival_side max_extra_maneuvers must be a whole number >= 0")
    tc = er.get("tie_check")
    if tc is not None:
        mx = tc.get("max_extra_s")
        if isinstance(mx, bool) or not isinstance(mx, (int, float)) or mx < 0:
            errs.append("every_request tie_check max_extra_s must be a number >= 0")
        tm = tc.get("tail_m", 300)
        if isinstance(tm, bool) or not isinstance(tm, int) or tm <= 0:
            errs.append("every_request tie_check tail_m must be a positive whole number of metres")
        nu = tc.get("never_uses", [])
        if not isinstance(nu, list) or not all(isinstance(x, str) and x for x in nu):
            errs.append("every_request tie_check never_uses must be a list of Valhalla edge uses")
        ws = tc.get("door_walk_m_per_s")
        if ws is not None and (isinstance(ws, bool) or not isinstance(ws, (int, float)) or ws <= 0):
            errs.append("every_request tie_check door_walk_m_per_s must be a number > 0")
    ad = er.get("address_street")
    if ad is not None:
        for k in ("within_m", "probe_step_m", "probe_radius_m", "end_margin_m"):
            v = ad.get(k)
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                errs.append(f"every_request address_street {k} must be a positive whole number of metres")
        w = ad.get("words", {})
        if not isinstance(w, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in w.items()):
            errs.append("every_request address_street words must map words to words")
    lot = er.get("lot")
    if lot is not None:
        v = lot.get("within_m")
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            errs.append("every_request lot within_m must be a positive whole number of metres")
        u = lot.get("uses")
        if not isinstance(u, list) or not u or not all(x in LOT_USES for x in u):
            errs.append(f"every_request lot uses must be a list of {', '.join(LOT_USES)}")
        du = lot.get("door_uses")
        if du is not None and (not isinstance(du, list) or not du or not all(x in DOOR_USES for x in du)):
            errs.append(f"every_request lot door_uses must be a list of {', '.join(DOOR_USES)}")
        for k in ("probe_step_m", "probe_within_m", "street_margin_m"):
            v = lot.get(k)
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                errs.append(f"every_request lot {k} must be a positive whole number of metres")
        if ad is None:
            errs.append("every_request lot needs address_street (its probe)")
    dt = er.get("left_out")
    if dt is not None:
        u = dt.get("uses")
        if not isinstance(u, list) or not u or not all(isinstance(x, str) and x for x in u):
            errs.append("every_request left_out uses must be a list of Valhalla edge uses")
        for k in ("within_m", "box_half_m", "clear_m"):
            v = dt.get(k)
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                errs.append(f"every_request left_out {k} must be a positive whole number of metres")
        v = dt.get("max_rings")
        # Valhalla service_limits.max_exclude_polygons_vertices 100 (the app's config): 5 vertices a ring
        if isinstance(v, bool) or not isinstance(v, int) or not 0 < v <= MAX_RINGS:
            errs.append(f"every_request left_out max_rings must be a whole number from 1 to {MAX_RINGS}")
    return errs


_EVERY: dict | None = None


def every_request() -> dict:
    """presets.json "every_request" (read once)."""
    global _EVERY
    if _EVERY is None:
        _EVERY = load_preset_file()["every_request"]
    return _EVERY


def load_checks() -> dict:
    """App-side result checks from presets.json (thresholds live in data, not code)."""
    return C.load_json(PRESETS_FILE).get("checks", {})


# Valhalla 3.9.1 auto costing options: name -> (type, min, max). From
# docs/docs/api/route/api-reference.md at tag 3.9.1 ("Automobile and bus costing options",
# "Other costing options", "Hard exclusions"). Valhalla silently ignores unknown names and
# clamps or ignores bad values, so presets.json is checked against this list instead.
AUTO_OPTIONS_3_9_1 = {
    **{k: (float, 0.0, None) for k in (
        "maneuver_penalty", "gate_cost", "gate_penalty", "private_access_penalty", "destination_only_penalty",
        "toll_booth_cost", "toll_booth_penalty", "ferry_cost", "service_penalty", "service_factor",
        "country_crossing_cost", "country_crossing_penalty", "height", "width", "length", "weight",
        "speed_penalty_factor", "alley_penalty")},
    "alley_factor": (float, 0.1, 100000.0),  # src/sif/autocost.cc kAlleyFactorRange (kMinFactor..kMaxFactor)
    **{k: (float, 0.0, 1.0) for k in (
        "use_ferry", "use_highways", "use_tolls", "use_living_streets", "use_tracks", "use_distance")},
    "top_speed": (float, 10.0, 252.0),
    "fixed_speed": (float, 0.0, 252.0),
    "closure_factor": (float, 1.0, 10.0),
    **{k: (bool, None, None) for k in (
        "shortest", "disable_hierarchy_pruning", "ignore_closures", "ignore_restrictions", "ignore_oneways",
        "ignore_non_vehicular_restrictions", "ignore_access", "ignore_construction", "exclude_unpaved",
        "exclude_cash_only_tolls", "include_hov2", "include_hov3", "include_hot", "exclude_bridges",
        "exclude_tunnels", "exclude_tolls", "exclude_highways", "exclude_ferries")},
    "speed_types": (list, None, None),
    "hierarchy_limits": (dict, None, None),
}


def validate_costing_options(opts: dict) -> list[str]:
    """Problems with one costing_options object (empty list when it is valid for 3.9.1 auto)."""
    errs = []
    for k, v in opts.items():
        if k not in AUTO_OPTIONS_3_9_1:
            errs.append(f"unknown auto costing option {k!r}")
            continue
        typ, lo, hi = AUTO_OPTIONS_3_9_1[k]
        if typ is float:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                errs.append(f"{k}={v!r} is not a number")
            elif (lo is not None and v < lo) or (hi is not None and v > hi):
                errs.append(f"{k}={v} is outside {lo}..{hi}")
        elif not isinstance(v, typ):
            errs.append(f"{k}={v!r} is not a {typ.__name__}")
    return errs


def validate_preset(p: dict) -> list[str]:
    """Problems with one format-1 preset, or one compose() gives (option names, ranges, select rule fields)."""
    errs = []
    if p.get("costing", "auto") != "auto":
        errs.append(f"{p['id']}: only auto costing is supported")
    for k in UNSUPPORTED_KEYS:
        if k in p:
            errs.append(f"{p['id']}: {k} is not supported by the app planner")
    groups = [("costing_options", p.get("costing_options", {}))]
    if "fallback_costing_options" in p:
        groups.append(("fallback_costing_options", p["fallback_costing_options"]))
    if "select" in p:
        if p["select"] not in ("min_maneuvers", "min_distance"):
            errs.append(f"{p['id']}: unknown select rule {p['select']}")
        if not 0 <= p.get("max_time_increase", -1) <= 1:
            errs.append(f"{p['id']}: max_time_increase must be 0..1")
        if "max_time_extra_s" in p and not (isinstance(p["max_time_extra_s"], (int, float))
                                            and not isinstance(p["max_time_extra_s"], bool) and p["max_time_extra_s"] >= 0):
            errs.append(f"{p['id']}: max_time_extra_s must be a number of seconds >= 0")
        groups += [(f"candidates[{i}]", c) for i, c in enumerate(p.get("candidates", []))]
        groups += [(f"fallback_candidates[{i}]", c) for i, c in enumerate(p.get("fallback_candidates", []))]
        if not p.get("candidates"):
            errs.append(f"{p['id']}: select rule without candidates")
        if "fallback_candidates" in p and len(p["fallback_candidates"]) != len(p["candidates"]):
            errs.append(f"{p['id']}: fallback_candidates must match candidates one to one")
    for k in p.get("fallback_when", {}):
        if k not in ("highway_km_over", "toll_km_over"):
            errs.append(f"{p['id']}: unknown fallback_when condition {k}")
    if "fallback_when" in p and "fallback_costing_options" not in p and "fallback_candidates" not in p:
        errs.append(f"{p['id']}: fallback_when without fallback_costing_options")
    for name, opts in groups:
        errs += [f"{p['id']} {name}: {e}" for e in validate_costing_options(opts)]
    return errs


def load_trips() -> dict:
    return C.load_json(TRIPS_FILE)


def actor(config: dict | Path) -> valhalla.Actor:
    return valhalla.Actor(config if isinstance(config, dict) else str(config))


def loc(p: dict, **extra) -> dict:
    d = {"lat": p["lat"], "lon": p["lon"]}
    d.update(extra)
    return d


def request(locations: list[dict], preset: dict | None = None, **extra) -> dict:
    """The route request of a preset. presets.json every_request applies: its costing options under
    the preset's, its stop options on every location after the first (a location's own keys win)."""
    preset = preset or {"costing": "auto", "costing_options": {}}
    costing = preset.get("costing", "auto")
    er = every_request()
    stop_opts = er.get("stops", {})
    req = {
        "locations": [_public(loc) for loc in locations[:1]]
        + [_public(loc) if loc.get(TIE_KEY) == "none" else {**stop_opts, **_public(loc)} for loc in locations[1:]],
        "costing": costing,
        "costing_options": {costing: {**er.get("costing_options", {}), **preset.get("costing_options", {})}},
        "units": "kilometers",
        "directions_options": {"units": "kilometers"},
    }
    # batch E2: the drive-through lanes and alleys near the stops (AVOID_KEY), left out of every route of the plan;
    # item 35: before them the way into a lot this plan leaves out (ENTRY_KEY), so max_rings never drops it
    rings: list = []
    for loc in locations[1:]:
        rings += [r for r in loc.get(ENTRY_KEY, ()) if r not in rings]
    for loc in locations[1:]:
        rings += [r for r in loc.get(AVOID_KEY, ()) if r not in rings]
    if rings:
        req["exclude_polygons"] = rings[:er.get("left_out", {}).get("max_rings", len(rings))]
    req.update(preset.get("request_options", {}))
    req.update(extra)
    return req


# Keys of a location given to run_preset that are not Valhalla's (taken off before a request):
# STREET_KEY the street a place's address names (tied to it by [stop_ends]), TIE_KEY "none" for a
# map point (a dropped pin: no every_request stop options). Kotlin: Stop.street, Stop.tied.
STREET_KEY = "_street"
TIE_KEY = "_tie"
# POI_KEY true: the place is a point of interest (a business, shop, amenity of the search pack; batch E,
# docs/next-round.md item 18): [stop_ends] offers its own lot first. Kotlin: Stop.pointOfInterest. A stop
# that ends in a lot carries preferred_side LOT_SIDE ("either", Valhalla's default: no route change), which
# tells [arrival_side] it is a lot stop.
POI_KEY = "_poi"
LOT_SIDE = "either"
# Batch E2 (docs/next-round.md item 19): the end point of each stop is decided once per plan ([decide_ends])
# and every route type routes to it. END_KEY the kind of end ("lot", "street" its address street, "near" the
# nearest street, "place" the place itself untied (batch H), "pin" a map point; Kotlin: Stop.end), FIXED_KEY
# true once decided (the location is sent as it is, nothing is decided again; a re-route keeps it; Kotlin:
# Stop.fixed), AVOID_KEY the drive-through
# lanes and alleys found near the stop as exclude_polygons rings ([left_out_rings]; every request of the plan
# leaves them out; Kotlin: Stop.avoid).
END_KEY = "_end"
FIXED_KEY = "_fixed"
AVOID_KEY = "_avoid"
# DOOR_KEY (docs/next-round.md item 28): where customers go into a point of interest that is a large
# building, as {"lat", "lon"} (the search pack's poi_door table: a mapped entrance, else for a shop the
# side its parking is on). [stop_ends] looks for the lot around the door, not around the centroid, which
# for a large store is the middle of the roof, as near to the service road behind it as to the lane
# along its front. Kotlin: Stop.door.
DOOR_KEY = "_door"
# ENTRY_KEY (docs/next-round.md item 35): the way into the stop's lot that this plan leaves out, as
# exclude_polygons rings like AVOID_KEY's ([decide_entrances]: another way in is faster by the clock
# and costs the engine hardly more). Unlike AVOID_KEY it belongs to one plan: every plan decides it
# again from where the trip is then, so a driver who misses the turn is not sent back to it.
# Kotlin: Stop.entry.
ENTRY_KEY = "_entry"
PRIVATE_KEYS = (STREET_KEY, TIE_KEY, POI_KEY, END_KEY, FIXED_KEY, AVOID_KEY, DOOR_KEY, ENTRY_KEY)


def _public(loc: dict) -> dict:
    return {k: v for k, v in loc.items() if k not in PRIVATE_KEYS}


class _Memo:
    """One plan's engine calls: a route request asked again gets the first answer (Kotlin: RoutePlanner
    Calls, which answers each distinct request once)."""

    def __init__(self, a):
        self.a, self.seen = (a.a, a.seen) if isinstance(a, _Memo) else (a, {})

    def route(self, s: str) -> str:
        if s not in self.seen:
            try:
                self.seen[s] = (True, self.a.route(s))
            except RuntimeError as e:  # valhalla.ValhallaError is a RuntimeError
                self.seen[s] = (False, e)
        ok, v = self.seen[s]
        if not ok:
            raise v
        return v

    def trace_attributes(self, s: str) -> str:
        return self.a.trace_attributes(s)


def run_preset(a: valhalla.Actor, locations: list[dict], preset: dict, **extra) -> dict:
    """The route the app guides on (Kotlin: RoutePlanner.plan): the end of each stop decided once with
    the plan's Fastest request ([decide_ends], [decision_preset]; stops already decided are kept), then
    the type's own requests to those ends ([run_preset_plain]). The answer says "tie_check" ("kept": every
    stop that had a choice kept its first end; "untied": one took a later end), "arrival_side" (the
    decision's side request: "asked" or "side") and holds "_ends", the decided locations."""
    m = _Memo(a)
    ends, info = decide_ends(m, locations, decision_preset(preset), **extra)
    ends = decide_entrances(m, ends, decision_preset(preset), **extra)
    resp = run_preset_plain(m, ends, preset, **extra)
    if "error" not in resp:
        resp.update({k: v for k, v in info.items() if v})
    resp["_ends"] = ends
    return resp


def decision_preset(preset: dict) -> dict:
    """The preset that decides the ends (Kotlin: PresetSet.decisionPreset): Fastest with the same options
    when [preset] is a route type of presets.json (its id "type" or "type+option+..."), else [preset]
    itself (a format-1 preset of the old reference plans)."""
    pf = _preset_file()
    parts = str(preset.get("id", "")).split("+")
    types = {t["id"] for t in pf["route_types"]}
    opts = {o["id"] for o in pf["options"]}
    if "fastest" in types and parts[0] in types and all(p in opts for p in parts[1:]):
        return compose("fastest", parts[1:], pf)
    return preset


_PF: dict | None = None


def _preset_file() -> dict:
    global _PF
    if _PF is None:
        _PF = load_preset_file()
    return _PF


def street_key(name: str, words: dict) -> str:
    """A street name compared with an address street: lower case, ASCII letters and digits only, each
    word through presets.json every_request.address_street.words ("E 12th St" = "East 12th Street")."""
    out, word = [], []
    for ch in name.lower() + " ":
        if ch.isascii() and ch.isalnum():
            word.append(ch)
        elif word:
            w = "".join(word)
            out.append(words.get(w, w))
            word = []
    return " ".join(out)


def _round6(x: float) -> float:
    return math.floor(x * 1e6 + 0.5) / 1e6


def _grid(lat: float, lon: float, step: int, within: int) -> list[dict]:
    """Probe points every [step] m within [within] m of the place, each twice 0.55 m apart, in whole
    micro-degrees (Kotlin: AddressTie.grid)."""
    lat_u, lon_u = math.floor(lat * 1e6 + 0.5), math.floor(lon * 1e6 + 0.5)
    dlat = math.floor(step / 111320.0 * 1e6 + 0.5)
    dlon = math.floor(dlat / math.cos(math.radians((lat_u // 10000) / 100.0)) + 0.5)
    n = within // step
    out = []
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            if (i * i + j * j) * step * step <= within * within:
                la, lo = lat_u + i * dlat, lon_u + j * dlon
                out += [{"lat": la / 1e6, "lon": lo / 1e6}, {"lat": (la + 5) / 1e6, "lon": lo / 1e6}]
    return out


def _probe(shape: list[dict], rule: dict, use: bool) -> dict:
    return {"shape": shape, "costing": "auto", "shape_match": "map_snap",
            "trace_options": {"search_radius": rule["probe_radius_m"], "breakage_distance": 5, "gps_accuracy": 5,
                              "interpolation_distance": 0},
            "filters": {"attributes": ["edge.names", "edge.road_class"] + (["edge.use"] if use else [])
                        + ["edge.begin_shape_index", "edge.end_shape_index", "shape"], "action": "include"}}


def address_probe_request(lat: float, lon: float, rule: dict, use: bool = False) -> dict:
    """The trace_attributes request that finds the streets around a place (Kotlin: AddressTie.request):
    a grid of points every probe_step_m within within_m, each point twice 0.55 m apart and the pairs
    farther apart than breakage_distance, so each pair is matched alone to its nearest edge within
    probe_radius_m. Whole micro-degrees, so Python and Kotlin send the same numbers. [use] (a point of
    interest, batch E): each edge's Valhalla use too (driveway, parking_aisle, alley, ...); the
    filter does not change the match, so the address street's point stays the same."""
    return _probe(_grid(lat, lon, rule["probe_step_m"], rule["within_m"]), rule, use)


def lot_probe_request(lat: float, lon: float, rule: dict, lot: dict) -> dict:
    """Batch E: the second probe of a point of interest (Kotlin: AddressTie.lotRequest), a finer grid
    (every lot.probe_step_m within lot.probe_within_m) with edge.use, so the edges nearest to the place
    are found (the 50 m grid alone can match the pair at the place to a slightly farther edge)."""
    return _probe(_grid(lat, lon, lot["probe_step_m"], lot["probe_within_m"]), rule, True)


def _probe_edges(resp: dict, lat: float, lon: float, points: bool = False):
    """Each edge of a probe answer with its nearest point to the place: (edge, d_min, at, pts, seg) in
    metres east/north of the place (pts), at = metres along the edge (Kotlin: AddressTie.near). An
    edge matched by one pair only, across it, comes back as a single point (both points of the pair
    project onto it); [points] keeps those (the lot search: the pair at the place itself gives the
    edge nearest to it), else they are skipped (the address street, as in C1)."""
    shape = decode_polyline6(resp.get("shape", ""))
    kx, ky = 111320.0 * math.cos(math.radians(lat)), 111320.0
    for e in resp.get("edges", []):
        pts = [((q[1] - lon) * kx, (q[0] - lat) * ky) for q in shape[e["begin_shape_index"]:e["end_shape_index"] + 1]]
        if len(pts) < 2:
            if points and pts:
                yield e, math.hypot(pts[0][0], pts[0][1]), 0.0, pts, []
            continue
        seg = [math.hypot(x2 - x1, y2 - y1) for (x1, y1), (x2, y2) in zip(pts, pts[1:])]
        along, d_min, at = 0.0, -1.0, 0.0
        for (x1, y1), (x2, y2), L in zip(pts, pts[1:], seg):
            t = 0.0 if L == 0 else max(0.0, min(1.0, -(x1 * (x2 - x1) + y1 * (y2 - y1)) / (L * L)))
            d = math.hypot(x1 + t * (x2 - x1), y1 + t * (y2 - y1))
            if d_min < 0 or d < d_min:
                d_min, at = d, along + t * L
            along += L
        yield e, d_min, at, pts, seg


def _point_inside(pts, seg, at: float, margin: float, lat: float, lon: float) -> tuple:
    """The point [at] metres along an edge, kept [margin] metres inside it (at most half its length)
    so it is not a crossing, as (lat, lon) in whole micro-degrees (a single point: that point)."""
    total = sum(seg)
    m = min(float(margin), total / 2)
    at = max(m, min(total - m, at))
    x, y = pts[-1]
    for k, ((x1, y1), (x2, y2)) in enumerate(zip(pts, pts[1:])):
        L = seg[k]
        if at <= L:
            f = 0.0 if L == 0 else at / L
            x, y = x1 + f * (x2 - x1), y1 + f * (y2 - y1)
            break
        at -= L
    kx, ky = 111320.0 * math.cos(math.radians(lat)), 111320.0
    return (_round6(lat + y / ky), _round6(lon + x / kx))


def address_tie_point(resp: dict, lat: float, lon: float, street: str, rule: dict, classes) -> tuple | None:
    """From [address_probe_request]'s answer: the point of the nearest edge named [street] (of a road
    class in [classes]) within within_m of the place, kept end_margin_m inside the edge so it is not a
    crossing; None when there is none. Kotlin: AddressTie.point."""
    words = rule.get("words", {})
    want = street_key(street, words)
    if not want:
        return None
    best = None
    for e, d_min, at, pts, seg in _probe_edges(resp, lat, lon):
        if e.get("road_class") not in classes or not any(street_key(n, words) == want for n in e.get("names", [])):
            continue
        if d_min > rule["within_m"] or (best is not None and d_min >= best[0]):
            continue
        best = (d_min, _point_inside(pts, seg, at, rule["end_margin_m"], lat, lon))
    return best[1] if best else None


def lot_tie_point(resps: list[dict], lat: float, lon: float, lot: dict, margin: float, classes, door: bool = False) -> tuple | None:
    """Batch E (docs/next-round.md item 18): from probe answers asked with edge.use, the point of the
    nearest edge whose use is one of lot["uses"] (driveway, parking_aisle; never an alley or a
    drive-through lane) within lot["within_m"] of the place and at least lot["street_margin_m"] nearer
    to it than every street of a road class in [classes] (the stop tie's classes; so a business on its
    street with a driveway behind it still ends on the street), [margin] metres inside the edge; None
    when there is none. [door] (docs/next-round.md item 28): the point is the place's door, so the uses
    are lot["door_uses"] (a plain service road too: the lane along a store's front is often mapped as
    one, and by the door it is not the delivery road behind the building). Kotlin: AddressTie.lotPoint."""
    uses = set(lot.get("door_uses", lot["uses"]) if door else lot["uses"])
    best, street_d = None, None
    for e, d_min, at, pts, seg in (x for resp in resps for x in _probe_edges(resp, lat, lon, points=True)):
        if e.get("use") in uses:
            if d_min <= lot["within_m"] and (best is None or d_min < best[0]):
                best = (d_min, at, pts, seg)
        elif e.get("road_class") in classes:
            if street_d is None or d_min < street_d:
                street_d = d_min
    if best is None or (street_d is not None and best[0] + lot["street_margin_m"] > street_d):
        return None
    return _point_inside(best[2], best[3], best[1], margin, lat, lon)


def _dist_to_polyline(x: float, y: float, pts) -> float:
    if len(pts) == 1:
        return math.hypot(pts[0][0] - x, pts[0][1] - y)
    best = -1.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / L2))
        d = math.hypot(x1 + t * dx - x, y1 + t * dy - y)
        if best < 0 or d < best:
            best = d
    return best


def left_out_rings(resps: list[dict], lat: float, lon: float, rule: dict) -> list[list[list[float]]]:
    """Batch E2 (docs/next-round.md item 19): the drive-through lanes and alleys the stop's probes see (an
    edge whose use is in rule uses, within rule within_m of the place) as Valhalla exclude_polygons rings, nearest first: a
    square of 2 x box_half_m at the middle of each such edge, in whole micro-degrees, only where the
    middle is more than clear_m from both ends of its edge and from every other edge the probes see (the
    same edge matched the other way is not another edge; so the box never touches another edge). Kotlin: AddressTie.leftOutRings."""
    edges = [x for resp in resps for x in _probe_edges(resp, lat, lon)]
    h, clear = rule["box_half_m"], rule["clear_m"]
    found = []
    for e, d_min, at, pts, seg in edges:
        if e.get("use") not in rule["uses"] or d_min > rule["within_m"]:
            continue
        total = sum(seg)
        if total / 2 <= clear:
            continue
        mid = _point_inside(pts, seg, total / 2, 0, lat, lon)
        kx, ky = 111320.0 * math.cos(math.radians(lat)), 111320.0
        mx, my = (mid[1] - lon) * kx, (mid[0] - lat) * ky
        # another edge: not this one, nor this one matched the other way (the same points in reverse)
        rev = pts[::-1]
        if any(other != pts and other != rev and _dist_to_polyline(mx, my, other) <= clear for _, _, _, other, _ in edges):
            continue
        cu, lu = math.floor(mid[0] * 1e6 + 0.5), math.floor(mid[1] * 1e6 + 0.5)
        if any(f[1] == (cu, lu) for f in found):
            continue
        found.append((d_min, (cu, lu)))
    out = []
    for _, (cu, lu) in sorted(found, key=lambda f: f[0]):
        dlat = math.floor(h / 111320.0 * 1e6 + 0.5)
        dlon = math.floor(dlat / math.cos(math.radians((cu // 10000) / 100.0)) + 0.5)
        corners = [(-1, -1), (1, -1), (1, 1), (-1, 1), (-1, -1)]
        out.append([[(lu + i * dlon) / 1e6, (cu + j * dlat) / 1e6] for i, j in corners])
    return out


def stop_ends(a, loc: dict) -> list[dict]:
    """The possible ends of one stop after the first, best first (batch E2, docs/next-round.md item 19;
    Kotlin: RoutePlanner.ends):
    - a point of interest (POI_KEY): its own lot when [lot_tie_point] finds one (no stop tie, the place as
      display_lat/display_lon, preferred_side "either"), then its address street, then the nearest street;
      the lot is looked for around its door when it has one (DOOR_KEY: the small probe and every distance
      of [lot_tie_point] are the door's), else around the place;
    - a place that names its address street (STREET_KEY): the point [address_tie_point] finds on that
      street (the place as display_lat/display_lon; rule 2's tie stays on it), then the nearest street;
    - any other tied place: the nearest street (the place as it is, rule 2's tie);
    - batch H (docs/next-round.md, the owner's answers after batch G): every list above ends with the
      place itself, untied (its own point, no stop tie: TIE_KEY "none"), so the end before it has to pass
      the check against it ([decide_ends]);
    - a map point (TIE_KEY "none") or a stop already decided (FIXED_KEY): exactly as it is.
    Each end is marked with END_KEY. A point of interest's probes also give the drive-through lanes and
    alleys near it ([left_out_rings]), kept on each of its ends (AVOID_KEY). One trace_attributes call for a
    place that names a street, two for a point of interest (the address probe with edge.use and the
    small lot probe)."""
    if loc.get(FIXED_KEY):
        return [loc]
    plain = {k: v for k, v in loc.items() if k not in (STREET_KEY, POI_KEY, DOOR_KEY)}
    if loc.get(TIE_KEY) == "none":
        return [{**plain, END_KEY: "pin"}]
    near = {**plain, END_KEY: "near"}
    place = {**plain, TIE_KEY: "none", END_KEY: "place"}
    er = every_request()
    rule, lot, dt = er.get("address_street"), er.get("lot"), er.get("left_out")
    street = loc.get(STREET_KEY)
    poi = bool(loc.get(POI_KEY)) and lot is not None
    if rule is None or not (street or poi):
        return [near, place]
    sf = er.get("stops", {}).get("search_filter", {})
    classes = ROAD_CLASSES[ROAD_CLASSES.index(sf.get("max_road_class", "motorway")):
                           ROAD_CLASSES.index(sf.get("min_road_class", "service_other")) + 1]

    def probe(req):
        try:
            return json.loads(a.trace_attributes(json.dumps(req)))
        except (RuntimeError, ValueError):  # valhalla.ValhallaError is a RuntimeError
            return None

    q = q_lot = None
    rings: list = []
    resp = probe(address_probe_request(loc["lat"], loc["lon"], rule, use=poi))
    try:
        if resp is not None and street:
            q = address_tie_point(resp, loc["lat"], loc["lon"], street, rule, classes)
        if poi:
            door = loc.get(DOOR_KEY) or loc
            near_resp = probe(lot_probe_request(door["lat"], door["lon"], rule, lot))
            resps = [r for r in (resp, near_resp) if r is not None]
            q_lot = lot_tie_point(resps, door["lat"], door["lon"], lot, rule["end_margin_m"], classes,
                                  door=DOOR_KEY in loc and loc[DOOR_KEY] is not None)
            if dt is not None:
                rings = left_out_rings(resps, loc["lat"], loc["lon"], dt)
    except (ValueError, KeyError):
        pass
    rest = {k: v for k, v in plain.items() if k not in ("lat", "lon")}
    display = {"display_lat": loc["lat"], "display_lon": loc["lon"]}
    ends = []
    if q_lot is not None:
        ends.append({"lat": q_lot[0], "lon": q_lot[1], **display, **rest, "preferred_side": LOT_SIDE, TIE_KEY: "none",
                     END_KEY: "lot"})
    if q is not None:
        ends.append({"lat": q[0], "lon": q[1], **display, **rest, END_KEY: "street"})
    ends += [near, place]
    if rings:
        ends = [{**e, AVOID_KEY: rings} for e in ends]
    return ends


def tail_request(shape: list[tuple[float, float]], tail_m: float, match: str = "edge_walk") -> dict:
    """The trace_attributes request over a leg's last [tail_m] metres (its shape from the end back to the
    first point past tail_m), asked with edge.use; [match] edge_walk (the route's own edges), else map_snap
    (Kotlin: RoutePlanner.tailRequest)."""
    tail = [shape[-1]]
    d = 0.0
    for q in reversed(shape[:-1]):
        d += haversine_m(q, tail[-1])
        tail.append(q)
        if d > tail_m:
            break
    tail.reverse()
    return {"shape": [{"lat": y, "lon": x} for y, x in tail], "costing": "auto", "shape_match": match,
            "filters": {"attributes": ["edge.use"], "action": "include"}}


def tail_uses(a, resp: dict, leg: int, tail_m: float) -> list | None:
    """The Valhalla edge uses of the last [tail_m] metres of leg [leg] of a route: edge_walk, and when the
    engine finds no exact match (3 of 900 Erie routes) map_snap once more; None when both calls fail."""
    try:
        shape = decode_polyline6(resp["trip"]["legs"][leg]["shape"])
    except (KeyError, IndexError):
        return None
    if not shape:  # Kotlin: an empty shape asks nothing
        return None
    for match in ("edge_walk", "map_snap"):
        try:
            t = json.loads(a.trace_attributes(json.dumps(tail_request(shape, tail_m, match))))
            return [e.get("use") for e in t.get("edges", [])]
        except (RuntimeError, ValueError):
            continue
    return None


def decide_ends(a, locations: list[dict], preset: dict, **extra) -> tuple[list[dict], dict]:
    """Batch E2 (docs/next-round.md item 19; presets.json every_request.tie_check; Kotlin:
    RoutePlanner.decide): the end point of each stop, decided once per plan with [preset] (the plan's
    Fastest with the driver's options, [decision_preset]) and then used by every route type.
    Stop by stop (the others at their end so far), its ends ([stop_ends]) are tried best first; an end
    is kept when its route finds a way, is at most max_extra_s slower than the route to the next end on
    the list with no more U-turns (when that one finds a way) and its last tail_m metres use none of
    never_uses (an alley, a drive-through lane; one trace_attributes call, asked only for the end about
    to be kept; a failed call does not count against it). The last end is kept when none before it is.
    Item 28 (front doors): a lot found around the place's door (DOOR_KEY) may be slower by max_extra_s plus
    the walk it saves ([_walk_saved_s]: how much nearer to the door its route ends than the next end's, at
    door_walk_m_per_s), because the next end of a large store is often 200 m from its door.
    Batch H: the last end is the place itself, untied ([stop_ends]); it never wins with an alley or a
    drive-through lane in its last tail_m metres (C1: no alleys unless there is no other way): such a
    place route does not count against the end before it (the nearest street), and when the place end
    would be kept anyway the nearest street is kept instead when its route finds a way. Its tail is asked
    only when it would win (at most one call per stop).
    Then the arrival side rule ([arrival_side]) on the route to the chosen ends, for the stops decided
    here. Returns the locations with FIXED_KEY (and the side options where the side route was kept) and
    {"tie_check": "kept" | "untied" | None, "arrival_side": ...}."""
    rule = every_request().get("tie_check")
    todo = [i for i in range(1, len(locations)) if not locations[i].get(FIXED_KEY)]
    if not todo:
        return locations, {}
    ends = {i: stop_ends(a, locations[i]) for i in todo}
    chosen = [locations[0]] + [ends[i][0] if i in ends else locations[i] for i in range(1, len(locations))]

    def run(i, end):
        return run_preset_plain(a, chosen[:i] + [end] + chosen[i + 1:], preset, **extra)

    asked = moved = False
    for i in todo:
        if len(ends[i]) == 1 or rule is None:
            continue
        asked = True
        last = len(ends[i]) - 1
        place_bad: list = []  # batch H: [True] once the place end's way in is known to use a lane or an alley

        def bad_place(i=i, last=last, place_bad=place_bad):
            if not place_bad:
                r = run(i, ends[i][last])
                place_bad.append(ends[i][last].get(END_KEY) == "place" and "error" not in r and _tail_never(a, r, i - 1, rule))
            return place_bad[0]

        pick = ends[i][-1]
        door = locations[i].get(DOOR_KEY)
        for k, end in enumerate(ends[i][:-1]):
            here = run(i, end)
            if "error" in here:
                continue
            nxt = run(i, ends[i][k + 1])
            allowed = rule["max_extra_s"]
            if door is not None and end.get(END_KEY) == "lot" and "error" not in nxt and rule.get("door_walk_m_per_s"):
                allowed += _walk_saved_s(here, nxt, i - 1, door, rule["door_walk_m_per_s"])
            if ("error" not in nxt and (here["trip"]["summary"]["time"] > nxt["trip"]["summary"]["time"] + allowed
                                        or uturn_count(here) > uturn_count(nxt))
                    and not (k + 1 == last and bad_place())):
                continue
            if _tail_never(a, here, i - 1, rule):
                continue
            pick = end
            break
        if pick is ends[i][-1] and last > 0 and bad_place() and "error" not in run(i, ends[i][last - 1]):
            pick = ends[i][last - 1]
        chosen[i] = pick
        moved = moved or pick is not ends[i][0]
    resp = run_preset_plain(a, chosen, preset, **extra)
    sided = arrival_side(a, resp, only=set(todo)) if "error" not in resp else resp
    out = list(chosen)
    for i in todo:
        d = dict(chosen[i])
        if sided.get("arrival_side") == "side":
            rl = sided["_request"]["locations"][i]
            for k in ("preferred_side", "street_side_cutoff"):
                if k in rl and k not in d:
                    d[k] = rl[k]
        d[FIXED_KEY] = True
        out[i] = d
    return out, {"tie_check": ("untied" if moved else "kept") if asked else None, "arrival_side": sided.get("arrival_side")}


def entrance_request(shape: list[tuple[float, float]], rule: dict) -> dict:
    """The trace_attributes request over a leg's last rule tail_m metres, the route's own edges
    (edge_walk), asked with each edge's use and its place in the answer's shape (Kotlin:
    RoutePlanner.entranceRequest)."""
    tail = [shape[-1]]
    d = 0.0
    for q in reversed(shape[:-1]):
        d += haversine_m(q, tail[-1])
        tail.append(q)
        if d > rule["tail_m"]:
            break
    tail.reverse()
    return {"shape": [{"lat": y, "lon": x} for y, x in tail], "costing": "auto", "shape_match": "edge_walk",
            "filters": {"attributes": ["edge.use", "edge.begin_shape_index", "edge.end_shape_index", "shape"], "action": "include"}}


def entrance_ring(a, resp: dict, leg: int, rule: dict) -> list | None:
    """Item 35: where leg [leg] of a route leaves the public roads for the lot it ends in, as one
    exclude_polygons ring: a square of 2 x box_half_m, in whole micro-degrees, into_m along the way the
    route takes inside the lot (the edges after the last one whose use is not in lot_uses). None when
    the leg does not end in a lot, lies in one for all of its last tail_m, has too short a way inside
    (less than into_m + box_half_m: the square would touch the road), or the engine cannot walk the
    route's own edges (no second call: a guess at the edges is not good enough to close one).
    Kotlin: RoutePlanner.entranceRing."""
    try:
        shape = decode_polyline6(resp["trip"]["legs"][leg]["shape"])
    except (KeyError, IndexError):
        return None
    if len(shape) < 2:
        return None
    try:
        t = json.loads(a.trace_attributes(json.dumps(entrance_request(shape, rule))))
    except (RuntimeError, ValueError):
        return None
    edges = t.get("edges", [])
    pts = decode_polyline6(t.get("shape", ""))
    k = len(edges)
    while k > 0 and edges[k - 1].get("use") in rule["lot_uses"]:
        k -= 1
    if k == 0 or k == len(edges):
        return None
    i0, i1 = edges[k].get("begin_shape_index"), edges[-1].get("end_shape_index")
    if not isinstance(i0, int) or not isinstance(i1, int):
        return None   # an answer without the edges' places in the shape
    path = pts[i0:i1 + 1]
    if len(path) < 2:
        return None
    lat, lon = path[0]
    kx, ky = 111320.0 * math.cos(math.radians(lat)), 111320.0
    xy = [((q[1] - lon) * kx, (q[0] - lat) * ky) for q in path]
    seg = [math.hypot(x2 - x1, y2 - y1) for (x1, y1), (x2, y2) in zip(xy, xy[1:])]
    if sum(seg) < rule["into_m"] + rule["box_half_m"]:
        return None
    mid = _point_inside(xy, seg, float(rule["into_m"]), 0, lat, lon)
    cu, lu = math.floor(mid[0] * 1e6 + 0.5), math.floor(mid[1] * 1e6 + 0.5)
    dlat = math.floor(rule["box_half_m"] / 111320.0 * 1e6 + 0.5)
    dlon = math.floor(dlat / math.cos(math.radians((cu // 10000) / 100.0)) + 0.5)
    corners = [(-1, -1), (1, -1), (1, 1), (-1, 1), (-1, -1)]
    return [[(lu + i * dlon) / 1e6, (cu + j * dlat) / 1e6] for i, j in corners]


def decide_entrances(a, ends: list[dict], preset: dict, **extra) -> list[dict]:
    """Item 35 (presets.json every_request.entrance; Kotlin: RoutePlanner.decideEntrances): the way
    into each stop's lot. The engine picks a way in by its own cost, which counts every small junction
    of an entrance; two ways into one lot can be near ties there while one is clearly faster by the
    clock (the owner's case: 18 s). So, stop by stop, for a stop that ends in a lot: the plan's route
    is asked ([preset]: the plan's Fastest), the place where it leaves the road is closed
    ([entrance_ring]) and the route is asked again. The other way in is taken (its ring kept on the
    stop as ENTRY_KEY, which every route of the plan then leaves out) when it is at least min_gain_s
    faster, costs at most within_cost times as much and has no more U-turns. Decided anew by every
    plan (a ring of an earlier plan is dropped first), also for stops whose end is fixed.
    The second route counts only when it was really asked with the way in closed and ends where the
    first one ends (the review of 2026-11-21): a request that finds no route is asked once more
    without any exclude_polygons ([route]), and that answer, free to use the lanes and alleys the
    plan leaves out, is not another way in; nor is a route that ends more than same_end_m away
    because the square took the stop's own lane."""
    rule = every_request().get("entrance")
    out = [{k: v for k, v in e.items() if k != ENTRY_KEY} for e in ends]
    if not rule:
        return out
    for i in range(1, len(out)):
        if out[i].get(END_KEY) != "lot":
            continue
        first = run_preset_plain(a, out, preset, **extra)
        if "error" in first:
            break
        ring = entrance_ring(a, first, i - 1, rule)
        if ring is None:
            continue
        trial = out[:i] + [{**out[i], ENTRY_KEY: [ring]}] + out[i + 1:]
        other = run_preset_plain(a, trial, preset, **extra)
        if "error" in other or "exclude_polygons" not in other.get("_request", {}):
            continue
        try:
            end1 = decode_polyline6(first["trip"]["legs"][i - 1]["shape"])[-1]
            end2 = decode_polyline6(other["trip"]["legs"][i - 1]["shape"])[-1]
        except (KeyError, IndexError):
            continue
        if haversine_m(end1, end2) > rule["same_end_m"]:
            continue
        s1, s2 = first["trip"]["summary"], other["trip"]["summary"]
        if "cost" not in s1 or "cost" not in s2:
            continue
        if (s2["time"] <= s1["time"] - rule["min_gain_s"] and s2["cost"] <= s1["cost"] * rule["within_cost"]
                and uturn_count(other) <= uturn_count(first)):
            out = trial
    return out


def _walk_saved_s(here: dict, nxt: dict, leg: int, door: dict, speed: float) -> float:
    """Item 28: the walking time the end of route [here] saves over the end of route [nxt]: how much nearer to
    the place's [door] leg [leg] of [here] ends than leg [leg] of [nxt], at [speed] m/s; 0 when it is not
    nearer (Kotlin: RoutePlanner.walkSavedS)."""
    def to_door(r):
        shape = decode_polyline6(r["trip"]["legs"][leg]["shape"])
        return haversine_m(shape[-1], (door["lat"], door["lon"]))
    try:
        return max(0.0, to_door(nxt) - to_door(here)) / speed
    except (KeyError, IndexError):
        return 0.0


def _tail_never(a, resp: dict, leg: int, rule: dict) -> bool:
    """True when the last tail_m metres of leg [leg] use one of the tie_check rule's never_uses (a failed
    tail call: False, it does not count against the route)."""
    uses = tail_uses(a, resp, leg, rule.get("tail_m", 300))
    return uses is not None and bool(set(uses) & set(rule.get("never_uses", ())))


def uturn_count(r: dict) -> int:
    return sum(1 for leg in r["trip"]["legs"] for m in leg["maneuvers"] if m["type"] in UTURN_TYPES)


# A check request's route is "the same way" as an alternate a pool picked when its shape starts with
# at least this share of the picked route's length (batch D; Kotlin: RoutePlanner.SAME_WAY_SHARE).
SAME_WAY_SHARE = 0.5


def _shape(trip: dict) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for leg in trip["legs"]:
        out += decode_polyline6(leg["shape"])
    return out


def same_way(picked: dict, routes: list[dict]) -> int | None:
    """Which of [routes] (a check request's main route and alternates, in order) is the route a pool
    picked, asked again with other stop options (Kotlin: RoutePlanner.sameWay): the one whose shape
    starts with the longest run of the picked route's shape points (ties: the first), when that run
    covers at least SAME_WAY_SHARE of the picked route's length (the checks change only the ends);
    else None."""
    ps = _shape(picked["trip"])
    if len(ps) < 2:
        return None
    total = sum(haversine_m(ps[i], ps[i + 1]) for i in range(len(ps) - 1))
    best, best_n = None, 0
    for j, r in enumerate(routes):
        rs = _shape(r["trip"])
        n = 0
        while n < len(ps) and n < len(rs) and ps[n] == rs[n]:
            n += 1
        if n > best_n:
            best, best_n = j, n
    if best is None:
        return None
    run = sum(haversine_m(ps[i], ps[i + 1]) for i in range(best_n - 1))
    return best if run >= SAME_WAY_SHARE * total else None


def _check_route(a: valhalla.Actor, resp: dict, req: dict) -> dict:
    """The route a check request (tie check, arrival side) gives for [resp]: its main route when
    [resp] is the main route of its request, else (an alternate a pool picked; batch D) the request
    is asked with the same alternates and the route the same way as [resp] ([same_way]) is the
    answer ({"error": "no same way"} when none is). The answer holds "_request" and "_index"."""
    index = resp.get("_index", 0)
    if index == 0:
        out = route(a, {k: v for k, v in req.items() if k != "alternates"})
        if "error" not in out:
            out["_index"] = 0
        return out
    full = route(a, req)
    if "error" in full:
        return full
    routes = [full] + full.get("alternates", [])
    j = same_way(resp, routes)
    if j is None:
        return {"error": "no same way"}
    return {"trip": routes[j]["trip"], "_request": full["_request"], "_index": j}


def arrival_side(a: valhalla.Actor, resp: dict, only: set | None = None) -> dict:
    """presets.json every_request.arrival_side (Kotlin: RoutePlanner.arrivalSide). When the chosen
    route arrives at a stop with the destination on the left, ask the same request once more with
    preferred_side and street_side_cutoff on those stops (batch D: cutoff tertiary, so on a
    residential street or below the engine ignores the side and the plain route stays); keep that
    route when it succeeds, no longer arrives on the left at any of them, has no more U-turns, takes
    at most max_extra_s longer and has at most max_extra_maneuvers more guidance instructions (one
    block at most; batch D, next-round item 16). For an alternate a pool picked the side request
    keeps its alternates and its route the same way is the side route ([_check_route]). The kept
    response says "arrival_side": "asked" (first route kept) or "side" (side route)."""
    rule = every_request().get("arrival_side")
    if rule is None or "error" in resp or "_request" not in resp:
        return resp
    req = resp["_request"]
    legs = resp["trip"]["legs"]
    if len(legs) != len(req["locations"]) - 1:
        return resp
    # a stop that already says its side (a lot stop, preferred_side LOT_SIDE; batch E) is not asked
    left = [i + 1 for i, leg in enumerate(legs)
            if leg["maneuvers"][-1]["type"] == 6 and "preferred_side" not in req["locations"][i + 1]
            and (only is None or i + 1 in only)]
    if not left:
        return resp
    side_req = dict(req)
    side_req["locations"] = [dict(loc) for loc in req["locations"]]
    for i in left:
        side_req["locations"][i].update({k: rule[k] for k in ("preferred_side", "street_side_cutoff") if k in rule})
    side = _check_route(a, resp, side_req)
    resp["arrival_side"] = "asked"
    if "error" in side:
        return resp

    def uturns(r):
        return sum(1 for leg in r["trip"]["legs"] for m in leg["maneuvers"] if m["type"] in UTURN_TYPES)

    s_legs = side["trip"]["legs"]
    if (len(s_legs) == len(legs) and all(s_legs[i - 1]["maneuvers"][-1]["type"] == 5 for i in left)
            and uturns(side) <= uturns(resp)
            and side["trip"]["summary"]["time"] <= resp["trip"]["summary"]["time"] + rule["max_extra_s"]
            and ("max_extra_maneuvers" not in rule
                 or instruction_count(side) <= instruction_count(resp) + rule["max_extra_maneuvers"])):
        for k in ("used_fallback", "fallback_reason", "fallback_warning", "candidates_considered", "tie_check"):
            if k in resp:
                side[k] = resp[k]
        side["arrival_side"] = "side"
        return side
    return resp


def instruction_count(r: dict) -> int:
    """Guidance instructions of a route: every maneuver but the start and the arrival (what Simpler's
    min_maneuvers counts; Kotlin: Route.turnCount)."""
    return sum(1 for leg in r["trip"]["legs"] for m in leg["maneuvers"] if m["type"] not in START_TYPES | END_TYPES)


def run_preset_plain(a: valhalla.Actor, locations: list[dict], preset: dict, **extra) -> dict:
    """Route with a preset from presets.json and return the chosen response.

    A preset is either a plain costing_options object (one request), or has a
    "select" rule: build a pool of candidate routes (one request per entry in
    "candidates", the first one also asking for "alternates"), keep those whose
    time is within "max_time_increase" of the fastest candidate, and pick the one
    with the fewest maneuvers ("min_maneuvers") or the shortest distance
    ("min_distance"). "fallback_costing_options" is tried when the main options
    find no route (hard exclusions can make a route impossible), and when a
    "fallback_when" condition holds for the main route (for example
    {"highway_km_over": 0.5}: Valhalla's exclude_highways only forbids driving
    ONTO a motorway, so a trip that starts on one may stay on motorways the whole
    way). Then the route with less of that road type is kept.

    Format 2 (compose()): a pool preset may have "fallback_candidates" (the pool once more with the
    fallback options: run when every candidate failed, or when the picked route exceeds a
    fallback_when limit; the picked route with less of the flagged road is kept, ties the first).
    A route the fallback produced because the request found no route gets "fallback_reason":
    "no_route" and, for a composed preset, "fallback_warning" (option_fallback_warning).
    """
    if "select" not in preset:
        resp = route(a, request(locations, preset, **extra))
        if "fallback_costing_options" not in preset:
            return resp
        fb = dict(preset, costing_options=preset["fallback_costing_options"])
        if "error" in resp:
            resp = route(a, request(locations, fb, **extra))
            resp["used_fallback"] = True
            return _no_route_fallback(preset, resp)
        for cond, limit in preset.get("fallback_when", {}).items():
            flag = cond.split("_km_over")[0]  # highway_km_over -> highway
            if flagged_km(resp, flag) > limit:
                alt = route(a, request(locations, fb, **extra))
                if "error" not in alt and flagged_km(alt, flag) < flagged_km(resp, flag):
                    alt["used_fallback"] = True
                    alt["fallback_reason"] = f"{flag} km {flagged_km(resp, flag):.1f} > {limit}"
                    return alt
        return resp
    best = _run_pool(a, locations, preset, preset["candidates"], **extra)
    if "fallback_candidates" not in preset:
        return best
    if "error" in best:
        fb = _run_pool(a, locations, preset, preset["fallback_candidates"], **extra)
        fb["used_fallback"] = True
        return _no_route_fallback(preset, fb)
    for cond, limit in preset.get("fallback_when", {}).items():
        flag = cond.split("_km_over")[0]
        if flagged_km(best, flag) > limit:
            alt = _run_pool(a, locations, preset, preset["fallback_candidates"], **extra)
            if "error" not in alt and flagged_km(alt, flag) < flagged_km(best, flag):
                alt["used_fallback"] = True
                alt["fallback_reason"] = f"{flag} km {flagged_km(best, flag):.1f} > {limit}"
                return alt
    return best


def _no_route_fallback(preset: dict, resp: dict) -> dict:
    if "error" not in resp:
        resp["fallback_reason"] = "no_route"
        w = option_fallback_warning(preset, resp)
        if w:
            resp["fallback_warning"] = w
    return resp


def option_fallback_warning(preset: dict, resp: dict) -> str | None:
    """The warning of a route the fallback produced because no route avoids what the options
    avoid (docs/architecture.md 2.11 rule 6; Kotlin: RouteWarning.OptionFallback). Names the
    avoided roads the route uses; None for a preset without options (format 1)."""
    avoided = [f for f in ROAD_FLAGS if f in preset.get("avoids", [])]
    if not avoided or "error" in resp:
        return None
    used = [f for f in avoided if flagged_km(resp, f) > 0]
    words = " and ".join(ROAD_WORDS[f] for f in (used or avoided))
    return f"No route to this place avoids {words}." + (" This route uses them." if used else "")


def _run_pool(a: valhalla.Actor, locations: list[dict], preset: dict, candidates: list[dict], **extra) -> dict:
    """The candidate pool of a select preset over [candidates]: the picked response, or {"error": ...}."""
    pool = []
    for i, opts in enumerate(candidates):
        p = {"costing": preset.get("costing", "auto"), "costing_options": opts}
        kw = dict(extra)
        if i == 0 and preset.get("alternates") and len(locations) == 2:
            kw["alternates"] = preset["alternates"]
        resp = route(a, request(locations, p, **kw))
        if "error" in resp:
            continue
        pool.append({"trip": resp["trip"], "_request": resp["_request"], "_index": 0})
        pool += [{"trip": alt["trip"], "_request": resp["_request"], "_index": j + 1}
                 for j, alt in enumerate(resp.get("alternates", []))]
    if not pool:
        return {"error": "no candidate route"}
    ok = [r for r in pool if r["trip"]["summary"]["time"] <= pool_limit_s(pool, preset)]

    def n_man(r):
        return sum(1 for leg in r["trip"]["legs"] for m in leg["maneuvers"] if m["type"] not in START_TYPES | END_TYPES)

    if preset["select"] == "min_maneuvers":
        best = min(ok, key=lambda r: (n_man(r), r["trip"]["summary"]["time"]))
    elif preset["select"] == "min_distance":
        best = min(ok, key=lambda r: (r["trip"]["summary"]["length"], r["trip"]["summary"]["time"]))
    else:
        raise ValueError(f"unknown select rule {preset['select']}")
    best = dict(best)
    best["candidates_considered"] = len(pool)
    return best


def pool_limit_s(pool: list[dict], preset: dict) -> float:
    """The longest time a pool route may take (Kotlin: RoutePlanner.poolLimitS): the quickest route of
    the pool plus max_time_increase of it, and never more than max_time_extra_s on top of it (batch D,
    the owner's answer: 10 %, at most 10 minutes; a preset without the key has no such cap)."""
    tmin = min(r["trip"]["summary"]["time"] for r in pool)
    lim = tmin * (1 + preset["max_time_increase"])
    if preset.get("max_time_extra_s") is not None:
        lim = min(lim, tmin + preset["max_time_extra_s"])
    return lim


def flagged_km(resp: dict, flag: str) -> float:
    """km of maneuvers carrying a flag ("highway" = motorway, "toll") in a route response."""
    if "error" in resp:
        return 0.0
    return sum(m["length"] for leg in resp["trip"]["legs"] for m in leg["maneuvers"] if m.get(flag))


def route(a: valhalla.Actor, req: dict) -> dict:
    """Returns the parsed response, or {"error": ...} with Valhalla's error text. A response holds
    "_request", the request that produced it. When the stops' every_request tie finds no street
    (error 171), the request is asked once more without it (Kotlin: RoutePlanner Calls.route). Batch E2:
    when a request that leaves out drive-through lanes and alleys (exclude_polygons) still finds no route,
    it is asked once more without them (the end decision's tail check then judges the way in)."""
    resp = _route_once(a, req)
    if "error" in resp and str(resp["error"]).startswith("171 "):
        loose = untie(req)
        if loose is not None:
            resp = _route_once(a, loose)
            req = loose
    if "error" in resp and "exclude_polygons" in req:
        resp = _route_once(a, {k: v for k, v in req.items() if k != "exclude_polygons"})
    return resp


def untie(req: dict) -> dict | None:
    """[req] without the every_request stop options (each key a location after the first holds with
    the rule's value), or None when no location holds any of them."""
    stop_opts = every_request().get("stops", {})
    locs, changed = list(req["locations"][:1]), False
    for loc in req["locations"][1:]:
        drop = {k for k, v in stop_opts.items() if loc.get(k) == v}
        if drop:
            loc = {k: v for k, v in loc.items() if k not in drop}
            changed = True
        locs.append(loc)
    return dict(req, locations=locs) if changed else None


def _route_once(a: valhalla.Actor, req: dict) -> dict:
    try:
        resp = json.loads(a.route(json.dumps(req)))
        resp["_request"] = req
        return resp
    except valhalla.ValhallaError as e:
        return {"error": f"{e.code} {e.message} (HTTP {e.http_code})"}
    except RuntimeError as e:
        msg = str(e)
        try:
            return {"error": json.loads(msg)}
        except ValueError:
            return {"error": msg}


def decode_polyline6(s: str) -> list[tuple[float, float]]:
    """Decode an encoded polyline with 6 digits precision -> [(lat, lon)]."""
    coords, idx, lat, lon = [], 0, 0, 0
    while idx < len(s):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(s[idx]) - 63
                idx += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += d
            else:
                lon += d
        coords.append((lat / 1e6, lon / 1e6))
    return coords


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


def summarize(resp: dict) -> dict:
    if "error" in resp:
        return {"ok": False, "error": resp["error"]}
    trip = resp["trip"]
    mans = [m for leg in trip["legs"] for m in leg["maneuvers"]]
    names = set()
    for m in mans:
        names.update(m.get("street_names", []))
        names.update(m.get("begin_street_names", []))
    shape = [pt for leg in trip["legs"] for pt in decode_polyline6(leg["shape"])]
    out = {
        "ok": True,
        "time_s": round(trip["summary"]["time"], 1),
        "length_km": round(trip["summary"]["length"], 3),
        "legs": len(trip["legs"]),
        "maneuvers_total": len(mans),
        "maneuvers": sum(1 for m in mans if m["type"] not in START_TYPES | END_TYPES),
        "has_toll": trip["summary"].get("has_toll", False),
        "has_highway": trip["summary"].get("has_highway", False),
        "toll_km": round(sum(m["length"] for m in mans if m.get("toll")), 2),
        "highway_km": round(sum(m["length"] for m in mans if m.get("highway")), 2),
        "street_names": sorted(names),
        "shape": shape,
        # 3.9.1 trip.locations hold the requested coordinates (no projected point), so the
        # distance from them to the first/last shape point is how far the ends were snapped
        "endpoint_gap_m": [round(haversine_m((trip["locations"][0]["lat"], trip["locations"][0]["lon"]), shape[0])),
                           round(haversine_m((trip["locations"][-1]["lat"], trip["locations"][-1]["lon"]), shape[-1]))]
        if shape else None,
    }
    # with admin_crossings=true, Valhalla 3.9.1 puts admins/admin_crossings in each leg summary
    legs_with_admins = [leg["summary"] for leg in trip["legs"] if "admins" in leg["summary"]]
    if legs_with_admins:
        out["admins"] = [a for s in legs_with_admins for a in s["admins"]]
        out["admin_crossings"] = [c for s in legs_with_admins for c in s.get("admin_crossings", [])]
    return out


def names_match(expected: list[str], names: list[str]) -> bool:
    """True if any expected road name/ref is part of any route street name ('I 76' in 'I 76 East')."""
    return any(e in n for e in expected for n in names)


def bearing_deg(a: dict, b: dict) -> float:
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlon = math.radians(b["lon"] - a["lon"])
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def far_snap_warnings(summary: dict, checks: dict | None = None) -> list[str]:
    """Warnings for route ends that Valhalla snapped far from the request.

    loki snaps a point to the nearest road within search_cutoff (35 km by default), so a
    destination outside the installed packs or in a lake can silently become a road
    several km away. The app shows these warnings; the threshold is in presets.json checks."""
    if not summary.get("ok") or not summary.get("endpoint_gap_m"):
        return []
    limit = (checks if checks is not None else load_checks()).get("warn_if_endpoint_gap_m_over", 1000)
    return [f"route {which} is {gap} m from the requested point"
            for which, gap in zip(("start", "end"), summary["endpoint_gap_m"]) if gap > limit]


def endpoint_gap_m(summary: dict, start: dict, end: dict) -> tuple[int, int]:
    """Distance from the requested start/end to where the route actually starts/ends."""
    s = summary["shape"]
    return (round(haversine_m((start["lat"], start["lon"]), s[0])),
            round(haversine_m((end["lat"], end["lon"]), s[-1])))


def _cells(shape: list[tuple[float, float]], step_m: float = 25.0, cell_deg: float = 0.0004) -> set:
    """Grid cells (~40 m) touched by a densified polyline."""
    cells = set()
    for a, b in zip(shape, shape[1:]):
        n = max(1, int(haversine_m(a, b) / step_m))
        for i in range(n + 1):
            t = i / n
            cells.add((round((a[0] + t * (b[0] - a[0])) / cell_deg), round((a[1] + t * (b[1] - a[1])) / cell_deg)))
    return cells


def overlap(shape_a, shape_b) -> float:
    """Share of route A's length (approx.) that lies on route B, 0..1."""
    ca, cb = _cells(shape_a), _cells(shape_b)
    if not ca:
        return 1.0
    # tolerate a one-cell offset (parallel carriageways, rounding)
    nb = {(x + dx, y + dy) for (x, y) in cb for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
    return len(ca & nb) / len(ca)
