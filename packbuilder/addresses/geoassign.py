"""Build-time spatial helpers: county / county-subdivision assignment and nearest-edge
queries with shapely (BSD-3). Coordinates are degrees; distances use a local
equirectangular projection (x = lon * cos(41 deg)), good to a few percent in PA.
"""
from __future__ import annotations

import math

import numpy as np
import shapefile  # pyshp
import shapely
from shapely.geometry import shape

from common import SOURCES, dl_file

KX = math.cos(math.radians(41.0))  # PA mid-latitude
M_PER_DEG = 111_320.0


def to_xy(lon, lat):
    return np.asarray(lon, dtype="f8") * KX, np.asarray(lat, dtype="f8")


def load_counties():
    """[(fips5, name, aland_m2, polygon_xy)] for the state's counties (cartographic boundary 1:500k)."""
    r = shapefile.Reader(str(dl_file("cb_2025_us_county_500k.zip")))
    fields = [f[0] for f in r.fields[1:]]
    out = []
    for sr in r.iterShapeRecords():
        rec = dict(zip(fields, sr.record))
        if rec["STATEFP"] != SOURCES["state"]["fips"]:
            continue
        geom = shapely.transform(shape(sr.shape.__geo_interface__), lambda c: c * np.array([KX, 1.0]))
        out.append((rec["GEOID"], rec["NAME"], int(rec["ALAND"]), geom))
    return out


def load_cousubs():
    """[(geoid, name, namelsad, polygon_xy)] for the state's county subdivisions."""
    r = shapefile.Reader(str(dl_file(f"tl_2026_{SOURCES['state']['fips']}_cousub.zip")))
    fields = [f[0] for f in r.fields[1:]]
    out = []
    for sr in r.iterShapeRecords():
        rec = dict(zip(fields, sr.record))
        if rec["COUSUBFP"] == "00000":  # undefined/water
            continue
        geom = shapely.transform(shape(sr.shape.__geo_interface__), lambda c: c * np.array([KX, 1.0]))
        out.append((rec["GEOID"], rec["NAME"], rec["NAMELSAD"], geom))
    return out


def assign(polys, lon, lat):
    """Index into polys (list of tuples whose last item is a polygon) for each point, -1 if none."""
    x, y = to_xy(lon, lat)
    pts = shapely.points(x, y)
    tree = shapely.STRtree([p[-1] for p in polys])
    pi, gi = tree.query(pts, predicate="within")
    res = np.full(len(pts), -1, dtype=np.int64)
    res[pi] = gi  # a point on a shared border gets one of the two
    return res
