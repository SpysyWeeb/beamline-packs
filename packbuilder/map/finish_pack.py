"""Finish a PMTiles pack after Planetiler: fix the header and clean label text.

Usage (from the repo root):
    .venv/map/Scripts/python packbuilder/map/finish_pack.py IN.pmtiles OUT.pmtiles
        [--clip CLIP.geojson] [--drop-layers landcover] [--center LON,LAT,ZOOM] [--force]

build_pack.py runs this automatically on Planetiler's output. It can also be
run by hand, for example on a pack cut from another build.

What it does (rules in build_config.json, "finish"):
  - Header bounds and centre: Planetiler writes the bounding box of the OSM
    extract, not of the clip polygon. With --clip the bounds become the clip
    polygon's bounding box and the centre the middle of its largest part.
    --center overrides the centre (Geofabrik's US outline spans -180..180
    because of the Aleutians, so its box says nothing about the lower 48).
  - Text: removes the code points listed in "remove_codepoints" from every
    string value in every tile. By default these are all characters above
    U+FFFF (emoji, Osage, maths letters ...), variation selectors and the
    zero-width joiner. The style bundle ships glyphs for the Basic Multilingual
    Plane only, so such characters would make MapLibre GL JS request glyph
    ranges that do not exist (HTTP 404) and are untested on MapLibre Native.
    Whitespace left over by a removal is collapsed.
  - Layers: --drop-layers (or "drop_layers") removes whole layers, e.g. the
    CC-BY landcover layer of a pack cut from Protomaps' hosted build.
MLT packs (MapLibre Tiles) are not decoded: their tiles are copied unchanged
and only the header is fixed (rec["text_cleanup"] says so).
Tiles that need no change are copied byte for byte. The result is checked:
same number of addressed tiles, no listed code point left in a changed tile,
header bounds equal to the clip bounding box.
"""
import argparse
import gzip
import json
import math
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mvt  # noqa: E402
from pmtiles.reader import MmapSource, Reader  # noqa: E402
from pmtiles.tile import Compression, TileType  # noqa: E402
from pmtiles.writer import Writer  # noqa: E402
from tile_stats import entries  # noqa: E402

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "build_config.json").read_text())


def char_class(ranges):
    """A regex character class for a list of [first, last] code points."""
    return "[" + "".join(f"\\U{a:08x}-\\U{b:08x}" for a, b in ranges) + "]"


def utf8_prefilter(ranges):
    """A bytes regex that matches the UTF-8 encoding of any listed code point (may over-match)."""
    alts = set()
    for a, b in ranges:
        if b > 0xFFFF:
            alts.add(rb"[\xf0-\xf4]")  # any 4-byte sequence
            b = min(b, 0xFFFF)
        if b - a < 64:  # small BMP range: the exact encodings
            alts.update(re.escape(chr(cp).encode("utf-8", "surrogatepass")) for cp in range(a, b + 1))
            continue
        cp = a
        while cp <= b:  # otherwise the first two bytes of each 64-code-point block
            enc = chr(cp).encode("utf-8", "surrogatepass")
            alts.add(re.escape(enc[:2]) if len(enc) == 3 else re.escape(enc[:1]))
            cp = (cp // 64 + 1) * 64
    return re.compile(b"|".join(sorted(alts)))


def polygon_bbox(geojson_path: Path):
    """(bbox of everything, bbox of the largest part by bbox area)"""
    g = json.loads(Path(geojson_path).read_text())
    feats = g["features"] if g.get("type") == "FeatureCollection" else [g]
    parts = []
    for f in feats:
        geom = f.get("geometry", f)
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        for poly in polys:
            ring = poly[0]
            xs, ys = [p[0] for p in ring], [p[1] for p in ring]
            parts.append((min(xs), min(ys), max(xs), max(ys)))
    allb = (min(p[0] for p in parts), min(p[1] for p in parts), max(p[2] for p in parts), max(p[3] for p in parts))
    largest = max(parts, key=lambda p: (p[2] - p[0]) * (p[3] - p[1]))
    return allb, largest


def clean_layer(layer: mvt.Layer, pattern: re.Pattern, ws: re.Pattern):
    """Rewrite a layer whose string values contain removable code points.

    Returns (layer bytes or None if unchanged, number of strings changed)."""
    new_vals, changed = [], 0
    for v in layer.values:
        s = mvt.decode_value(v)
        if isinstance(s, str) and pattern.search(s):
            t = ws.sub(" ", pattern.sub("", s)).strip()
            new_vals.append(mvt.enc_field(1, t.encode("utf-8")))
            changed += 1
        else:
            new_vals.append(v)
    if not changed:
        return None, 0
    # Removing characters can make two values equal; keep the table free of duplicates.
    table, index, remap = [], {}, []
    for v in new_vals:
        if v not in index:
            index[v] = len(table)
            table.append(v)
        remap.append(index[v])
    feats = []
    for f in layer.features:
        fid, ftype, tags, geom = layer.feature_parts(f)
        tags = [t if i % 2 == 0 else remap[t] for i, t in enumerate(tags)]
        feats.append(mvt.enc_feature(fid, ftype, tags, geom))
    out = mvt.enc_varint(15 << 3) + mvt.enc_varint(layer.version)
    out += mvt.enc_field(1, layer.name.encode())
    out += b"".join(mvt.enc_field(2, f) for f in feats)
    out += b"".join(mvt.enc_field(3, k.encode()) for k in layer.keys)
    out += b"".join(mvt.enc_field(4, v) for v in table)
    out += mvt.enc_varint(5 << 3) + mvt.enc_varint(layer.extent)
    return out, changed


def finish(src_path: Path, out_path: Path, clip=None, drop_layers=(), force=False, center=None) -> dict:
    cfg = CONFIG["finish"]
    drop = set(drop_layers) | set(cfg.get("drop_layers", []))
    ranges = cfg["remove_codepoints"]
    pattern = re.compile(char_class(ranges))
    prefilter = utf8_prefilter(ranges)
    ws = re.compile(r"\s{2,}")
    if out_path.exists() and not force:
        raise SystemExit(f"{out_path} exists (use --force)")
    t0 = time.time()
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    rec = {"input": src_path.name, "tiles_changed": 0, "strings_changed": 0, "layers_dropped": {},
           "remove_codepoints": ranges}
    samples = []
    with open(src_path, "rb") as fin:
        src = MmapSource(fin)
        reader = Reader(src)
        hdr = reader.header()
        meta = reader.metadata()
        mlt = hdr["tile_type"] == TileType.MLT
        if mlt:
            # MapLibre Tiles: this step has no MLT decoder, so tiles are copied unchanged and only
            # the header is fixed. The text clean-up must happen before encoding (not implemented).
            if drop:
                raise SystemExit("--drop-layers needs MVT tiles")
            rec["text_cleanup"] = "skipped: MLT tiles are copied unchanged (no MLT decoder in this step)"
        elif hdr["tile_type"] != TileType.MVT or hdr["tile_compression"] != Compression.GZIP:
            raise SystemExit("only gzip-compressed MVT packs (or MLT packs, header only) are supported")
        else:
            rec["text_cleanup"] = "done"
        ents = sorted(entries(src, hdr))
        base = hdr["tile_data_offset"]
        done = {}  # source offset -> rewritten bytes (only for changed tiles)
        unchanged = set()  # source offsets checked and left as they are
        drop_names = [n.encode() for n in drop]
        with open(tmp, "wb") as fout:
            w = Writer(fout)
            for tid, off, ln, run in ents:
                data = done.get(off)
                if data is None and mlt:
                    data = src(base + off, ln)
                elif data is None and off in unchanged:
                    data = src(base + off, ln)
                elif data is None:
                    raw = src(base + off, ln)
                    tile = gzip.decompress(raw)
                    if prefilter.search(tile) or any(n in tile for n in drop_names):
                        parts, changed_tile = [], False
                        for layer in mvt.parse_tile(tile):
                            if layer.name in drop:
                                rec["layers_dropped"][layer.name] = rec["layers_dropped"].get(layer.name, 0) + 1
                                changed_tile = True
                                continue
                            body, n = clean_layer(layer, pattern, ws)
                            if body is None:
                                body = bytes(layer.raw)
                            else:
                                rec["strings_changed"] += n
                                changed_tile = True
                                if len(samples) < 20:
                                    samples += [s for s in (mvt.decode_value(v) for v in layer.values)
                                                if isinstance(s, str) and pattern.search(s)][:20 - len(samples)]
                                # check: nothing removable is left
                                for v in mvt.Layer.parse(body).values:
                                    s = mvt.decode_value(v)
                                    if isinstance(s, str) and pattern.search(s):
                                        raise SystemExit(f"check failed: {s!r} still has a removable character")
                            parts.append(mvt.enc_field(3, body))
                        if changed_tile:
                            rec["tiles_changed"] += 1
                            data = gzip.compress(b"".join(parts), compresslevel=9, mtime=0)
                            done[off] = data
                        else:
                            data = raw
                            unchanged.add(off)
                    else:
                        data = raw
                        unchanged.add(off)
                for k in range(run):
                    w.write_tile(tid + k, data)
            header = dict(hdr)
            if clip:
                (w_, s_, e_, n_), (lw, ls, le, ln_) = polygon_bbox(clip)
                header.update(min_lon_e7=round(w_ * 1e7), min_lat_e7=round(s_ * 1e7),
                              max_lon_e7=round(e_ * 1e7), max_lat_e7=round(n_ * 1e7),
                              center_lon_e7=round((lw + le) / 2 * 1e7), center_lat_e7=round((ls + ln_) / 2 * 1e7))
                span = max(le - lw, (ln_ - ls) * 1.3, 1e-6)
                header["center_zoom"] = max(hdr["min_zoom"], min(hdr["max_zoom"], int(math.log2(360 / span))))
                rec["clip_bbox"] = [round(v, 6) for v in (w_, s_, e_, n_)]
            if center:  # explicit lon,lat,zoom, e.g. for a US outline that spans the antimeridian
                header.update(center_lon_e7=round(center[0] * 1e7), center_lat_e7=round(center[1] * 1e7),
                              center_zoom=int(center[2]))
            meta = dict(meta)
            if drop:
                meta["vector_layers"] = [vl for vl in meta.get("vector_layers", []) if vl.get("id") not in drop]
                if isinstance(meta.get("tilestats"), dict):
                    ts = meta["tilestats"]
                    ts["layers"] = [l for l in ts.get("layers", []) if l.get("layer") not in drop]
                    ts["layerCount"] = len(ts["layers"])
            meta["beamline:finish"] = {"tiles_changed": rec["tiles_changed"], "strings_changed": rec["strings_changed"],
                                       "layers_dropped": sorted(rec["layers_dropped"]),
                                       "bounds_from": Path(clip).name if clip else None}
            w.finalize(header, meta)
        addressed_in = hdr["addressed_tiles_count"]
    # checks on the written file
    with open(tmp, "rb") as f:
        h2 = Reader(MmapSource(f)).header()
    if h2["addressed_tiles_count"] != addressed_in:
        raise SystemExit(f"check failed: {h2['addressed_tiles_count']} addressed tiles, input had {addressed_in}")
    if clip:
        got = [h2[k] / 1e7 for k in ("min_lon_e7", "min_lat_e7", "max_lon_e7", "max_lat_e7")]
        if any(abs(a - b) > 1e-6 for a, b in zip(got, rec["clip_bbox"])):
            raise SystemExit(f"check failed: header bounds {got} != clip bbox {rec['clip_bbox']}")
    for attempt in range(30):  # the old file may be open in a viewer for a moment
        try:
            os.replace(tmp, out_path)
            break
        except PermissionError:
            print(f"  {out_path.name} is in use, retrying in 10 s", flush=True)
            time.sleep(10)
    else:
        raise SystemExit(f"could not replace {out_path}; the result is in {tmp}")
    rec.update(output_bytes=out_path.stat().st_size, input_bytes=src_path.stat().st_size,
               addressed_tiles=addressed_in, seconds=round(time.time() - t0, 1),
               header_bounds=[h2[k] / 1e7 for k in ("min_lon_e7", "min_lat_e7", "max_lon_e7", "max_lat_e7")],
               header_center=[h2["center_lon_e7"] / 1e7, h2["center_lat_e7"] / 1e7, h2["center_zoom"]],
               samples_before=samples)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--clip", help="GeoJSON polygon whose bounding box becomes the header bounds")
    ap.add_argument("--drop-layers", default="", help="comma-separated layer names to remove")
    ap.add_argument("--center", help="lon,lat,zoom for the header centre instead of the clip-derived one")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    out = Path(a.output)
    if out.exists() and not a.force:
        print(f"skip: {out} exists (use --force to redo)")
        return 0
    center = [float(x) for x in a.center.split(",")] if a.center else None
    rec = finish(Path(a.input), out, a.clip, [x for x in a.drop_layers.split(",") if x], force=True, center=center)
    out.with_name(out.stem + ".finish.json").write_text(json.dumps(rec, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in rec.items() if k != "samples_before"}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
