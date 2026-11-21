"""Measure a PMTiles vector pack: size per zoom and per layer, feature counts, and
driving-relevant attribute coverage. Decodes the tiles itself (packbuilder/map/mvt.py).

Usage (from the repo root):
    .venv/map/Scripts/python packbuilder/map/tile_stats.py data/map/packs/X.pmtiles [--deep 14 15] [--procs 4]

Writes X.stats.json next to the pack (or into --out-dir DIR) and prints a summary.
  per_zoom:  tiles addressed, unique tile contents, bytes stored (gzip) per zoom
  layers:    per zoom and layer: features and uncompressed bytes
  deep:      for each --deep zoom: road attribute coverage (name, ref, shield_text,
             network, oneway), road kinds, POI kinds, Unicode blocks used by name
             strings, and sample POIs with accented names (with coordinates)
"""
import argparse
import gzip
import json
import math
import mmap
import sys
from collections import Counter, defaultdict
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mvt  # noqa: E402
from pmtiles.reader import Reader, MmapSource  # noqa: E402
from pmtiles.tile import deserialize_directory, deserialize_header, tileid_to_zxy  # noqa: E402

_mm = None
_hdr = None


def entries(get_bytes, header):
    """Yield (tile_id, data_offset, length, run_length) for every tile run."""
    stack = [(header["root_offset"], header["root_length"])]
    while stack:
        off, ln = stack.pop()
        for e in deserialize_directory(get_bytes(off, ln)):
            if e.run_length > 0:
                yield e.tile_id, e.offset, e.length, e.run_length
            else:
                stack.append((header["leaf_directory_offset"] + e.offset, e.length))


def _init(path):
    global _mm, _hdr
    f = open(path, "rb")
    _mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    _hdr = deserialize_header(_mm[0:127])


def point_lonlat(geom: bytes, z, x, y, extent):
    vals = mvt.packed_varints(geom)
    if len(vals) < 3 or (vals[0] & 7) != 1:
        return None
    zz = lambda n: (n >> 1) ^ -(n & 1)  # noqa: E731
    px, py = zz(vals[1]), zz(vals[2])
    n = 2 ** z
    lon = (x + px / extent) / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + py / extent) / n))))
    return round(lon, 6), round(lat, 6)


def block_of(cp):
    s = (cp // 256) * 256
    return f"{s}-{s + 255}"


def _work(job):
    """job: list of (tile_id, offset, length, run_length, deep) -> partial stats."""
    layers = defaultdict(lambda: [0, 0])  # (z, layer) -> [features, bytes]
    deep = defaultdict(lambda: defaultdict(Counter))
    samples = defaultdict(list)
    for tid, off, ln, run, want_deep in job:
        z, x, y = tileid_to_zxy(tid)
        raw = _mm[_hdr["tile_data_offset"] + off:_hdr["tile_data_offset"] + off + ln]
        data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
        for l in mvt.parse_tile(data, with_features=True):
            rec = layers[(z, l.name)]
            rec[0] += len(l.features) * run
            rec[1] += len(l.raw) * run
            if not want_deep:
                continue
            d = deep[z]
            for f in l.features:
                fid, ftype, tags, geom = l.feature_parts(f)
                props = {l.keys[tags[i]]: mvt.decode_value(l.values[tags[i + 1]]) for i in range(0, len(tags), 2)}
                for k, v in props.items():
                    if isinstance(v, str) and (k == "name" or k.startswith("name:en") or k.startswith("pgf:")):
                        for ch in v:
                            d["name_blocks"][block_of(ord(ch))] += 1
                        if any(ord(c) > 0x7F for c in v) and k == "name":
                            d["non_ascii_names"][v] += 1
                if l.name == "roads":
                    kind = props.get("kind", "?")
                    d["road_kind"][kind] += 1
                    for a in ("name", "ref", "shield_text", "network", "oneway", "is_link", "is_bridge", "is_tunnel"):
                        if a in props:
                            d["road_attr"][a] += 1
                            if kind in ("highway", "major_road"):
                                d["road_attr_major"][a] += 1
                    if kind in ("highway", "major_road"):
                        d["road_attr_major"]["_features"] += 1
                    d["road_attr"]["_features"] += 1
                    if "network" in props:
                        d["road_network"][props["network"]] += 1
                    if "oneway" in props:
                        d["oneway_values"][str(props["oneway"])] += 1
                elif l.name == "pois":
                    d["poi_kind"][props.get("kind", "?")] += 1
                    d["poi_min_zoom"][str(props.get("min_zoom"))] += 1
                    nm = props.get("name")
                    if isinstance(nm, str) and any(0x80 <= ord(c) <= 0x24F for c in nm) and len(samples[z]) < 40:
                        pt = point_lonlat(geom, z, x, y, l.extent)
                        if pt:
                            samples[z].append({"name": nm, "kind": props.get("kind"), "lon": pt[0], "lat": pt[1]})
                elif l.name == "places":
                    d["place_kind"][props.get("kind", "?")] += 1
    return ({f"{k[0]}|{k[1]}": v for k, v in layers.items()},
            {z: {k: dict(c) for k, c in dd.items()} for z, dd in deep.items()},
            dict(samples))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pack")
    ap.add_argument("--deep", nargs="*", type=int, default=[])
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--no-layers", action="store_true", help="only the directory pass (fast)")
    ap.add_argument("--out-dir", help="write X.stats.json here instead of next to the pack "
                    "(so re-running a check leaves a delivered pack folder untouched)")
    a = ap.parse_args()
    path = Path(a.pack)
    with open(path, "rb") as f:
        get = MmapSource(f)
        rd = Reader(get)
        header = rd.header()
        meta = rd.metadata()
        ents = list(entries(get, header))
    per_zoom = defaultdict(lambda: {"tiles": 0, "unique": 0, "bytes": 0})
    seen_off = set()
    for tid, off, ln, run in ents:
        z = tileid_to_zxy(tid)[0]
        pz = per_zoom[z]
        pz["tiles"] += run
        if off not in seen_off:
            seen_off.add(off)
            pz["unique"] += 1
            pz["bytes"] += ln
    stats = {
        "pack": path.name,
        "file_bytes": path.stat().st_size,
        "header": {k: (v.name if hasattr(v, "name") else v) for k, v in header.items()},
        "per_zoom": {z: per_zoom[z] for z in sorted(per_zoom)},
        "vector_layers": [l["id"] for l in meta.get("vector_layers", [])],
        "generator": {k: meta.get(k) for k in ("planetiler:version", "planetiler:githash", "planetiler:osm:osmosisreplicationtime", "version", "description")},
    }
    if not a.no_layers:
        # dedupe identical contents (run-length/ocean tiles) but weight by run length
        uniq = {}
        for tid, off, ln, run in ents:
            z = tileid_to_zxy(tid)[0]
            key = off
            if key in uniq:
                uniq[key][3] += run
            else:
                uniq[key] = [tid, off, ln, run, z in a.deep]
        jobs = list(uniq.values())
        chunks = [jobs[i::a.procs * 8] for i in range(a.procs * 8)]
        layers = defaultdict(lambda: [0, 0])
        deep = defaultdict(lambda: defaultdict(Counter))
        samples = defaultdict(list)
        with Pool(a.procs, initializer=_init, initargs=(str(path),)) as pool:
            for lres, dres, sres in pool.imap_unordered(_work, chunks):
                for k, v in lres.items():
                    layers[k][0] += v[0]
                    layers[k][1] += v[1]
                for z, dd in dres.items():
                    for k, c in dd.items():
                        deep[z][k].update(c)
                for z, s in sres.items():
                    samples[z] += s[: max(0, 40 - len(samples[z]))]
        by_zoom = defaultdict(dict)
        for k, (n, b) in sorted(layers.items(), key=lambda kv: (int(kv[0].split("|")[0]), kv[0])):
            z, name = k.split("|")
            by_zoom[int(z)][name] = {"features": n, "bytes_uncompressed": b}
        stats["layers"] = dict(sorted(by_zoom.items()))
        stats["deep"] = {}
        for z, dd in deep.items():
            out = {}
            for k, c in dd.items():
                top = 60 if k not in ("non_ascii_names",) else 80
                out[k] = dict(c.most_common(top))
            stats["deep"][z] = out
        stats["samples_accented_pois"] = dict(samples)
        # Every 256-code-point block used by a name must exist as a glyph file in the
        # style bundle (real or placeholder); otherwise MapLibre requests a missing file.
        glyph_dir = Path(__file__).resolve().parent / "style-bundle" / "glyphs" / "Noto Sans Regular"
        if glyph_dir.is_dir():
            missing = {}
            for z, dd in deep.items():
                for blk, n in dd.get("name_blocks", {}).items():
                    if not (glyph_dir / f"{blk}.pbf").exists():
                        missing[blk] = missing.get(blk, 0) + n
            stats["name_blocks_without_glyph_file"] = missing
            if missing:
                print(f"WARNING: name characters in blocks with no glyph file in the style bundle: {missing}")
            elif deep:
                print("glyph check: every name character falls in a range the style bundle has a file for")
    out = path.with_suffix(".stats.json")
    if a.out_dir:
        Path(a.out_dir).mkdir(parents=True, exist_ok=True)
        out = Path(a.out_dir) / out.name
    out.write_text(json.dumps(stats, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"{path.name}: {stats['file_bytes'] / 1e6:.1f} MB, zoom {header['min_zoom']}-{header['max_zoom']}")
    for z, pz in stats["per_zoom"].items():
        print(f"  z{z:<2} tiles {pz['tiles']:>8}  unique {pz['unique']:>8}  {pz['bytes'] / 1e6:8.2f} MB")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
