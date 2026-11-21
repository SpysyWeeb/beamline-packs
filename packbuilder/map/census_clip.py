"""Write a state's outline from the US Census TIGER/Line state file as GeoJSON, for clipping a pack.

Usage (from the repo root):
    .venv/map/Scripts/python packbuilder/map/census_clip.py pa [--out data/map/work/clip-v2/pa.geojson]
    .venv/map/Scripts/python packbuilder/map/census_clip.py pa --compare-poly data/osm/pennsylvania.poly

The input is build_config.json "clip" (tl_2026_us_state.zip, fetched and
SHA-256-checked by fetch_sources.py). TIGER/Line state polygons cover the
whole state area including its water: Pennsylvania's reaches into Lake Erie up
to the Canadian border. (The Census cartographic boundary files, cb_*, are
cut at the shoreline and would remove Lake Erie from a Pennsylvania pack.)
The Census Bureau says these boundaries are for statistical purposes and are
not legal land descriptions; for cutting map packs that is good enough.
Coordinates are NAD83 longitude/latitude, used as WGS84 (the two differ by
about a metre in the conterminous US, below what a z15 tile can show).

Neighbouring states share their TIGER edges exactly, so packs clipped with
these outlines meet without a gap or an overlap.
"""
import argparse
import io
import json
import sys
import zipfile
from pathlib import Path

import shapefile  # pyshp
from shapely.geometry import mapping, shape

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "build_config.json").read_text())


def read_states(zip_path: Path):
    """Yield (record dict, shapely geometry) for every state in a Census state shapefile zip."""
    z = zipfile.ZipFile(zip_path)
    base = [n[:-4] for n in z.namelist() if n.endswith(".shp")][0]
    r = shapefile.Reader(shp=io.BytesIO(z.read(base + ".shp")), dbf=io.BytesIO(z.read(base + ".dbf")),
                         shx=io.BytesIO(z.read(base + ".shx")))
    for sr in r.iterShapeRecords():
        yield sr.record.as_dict(), shape(sr.shape.__geo_interface__)


def state_outline(usps: str, zip_path: Path = None):
    zip_path = zip_path or REPO / CONFIG["clip"]["file"]
    for rec, geom in read_states(zip_path):
        if rec["STUSPS"] == usps.upper():
            if not geom.is_valid:
                raise SystemExit(f"{usps}: TIGER polygon is not valid")
            return rec, geom
    raise SystemExit(f"{usps} not in {zip_path}")


def write_geojson(usps: str, out: Path):
    rec, geom = state_outline(usps)
    out.parent.mkdir(parents=True, exist_ok=True)
    fc = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "properties": {"STUSPS": rec["STUSPS"], "NAME": rec["NAME"], "GEOID": rec["GEOID"],
                       "source": Path(CONFIG["clip"]["file"]).name},
        "geometry": mapping(geom)}]}
    out.write_text(json.dumps(fc), encoding="utf-8")
    return rec, geom


def read_poly(path: Path):
    """A Geofabrik .poly file as a shapely (Multi)Polygon."""
    from shapely.geometry import MultiPolygon, Polygon
    rings, cur, name = [], None, None
    for line in Path(path).read_text().splitlines()[1:]:
        t = line.strip()
        if not t:
            continue
        if t == "END":
            if cur is not None:
                rings.append((name, cur))
                cur = None
            continue
        if cur is None:
            name, cur = t, []
        else:
            x, y = map(float, t.split()[:2])
            cur.append((x, y))
    outer = [Polygon(r) for n, r in rings if not n.startswith("!")]
    holes = [Polygon(r) for n, r in rings if n.startswith("!")]
    g = MultiPolygon(outer) if len(outer) > 1 else outer[0]
    for h in holes:
        g = g.difference(h)
    return g


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("state")
    ap.add_argument("--out")
    ap.add_argument("--compare-poly", help="also report how far a Geofabrik .poly extends beyond the TIGER outline")
    a = ap.parse_args()
    st = CONFIG["states"][a.state]
    out = Path(a.out) if a.out else REPO / "data" / "map" / "work" / "clip-v2" / f"{a.state}.geojson"
    rec, geom = write_geojson(st["usps"], out)
    print(f"{rec['NAME']}: {geom.geom_type}, bounds {[round(v, 4) for v in geom.bounds]} -> {out}")
    if a.compare_poly:
        poly = read_poly(Path(a.compare_poly))
        extra = poly.difference(geom)
        # rough area in km2 at the state's latitude
        import math
        lat = (geom.bounds[1] + geom.bounds[3]) / 2
        k = 111.32 ** 2 * math.cos(math.radians(lat))
        print(f".poly area outside the TIGER outline: {extra.area * k:,.0f} km2 "
              f"(TIGER {geom.area * k:,.0f} km2, .poly {poly.area * k:,.0f} km2)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
