"""Minimal Mapbox Vector Tile (MVT 2.1) protobuf reader/writer, standard library only.

Enough to list layers and features, decode tags, and merge two tiles at the
layer level without touching geometry. Written for this project from the
public MVT specification (https://github.com/mapbox/vector-tile-spec, CC-BY 3.0
for the spec text); no code copied.
"""
import struct


def read_varint(buf, pos):
    result = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, pos
        shift += 7


def fields(buf, start=0, end=None):
    """Yield (field_number, wire_type, value) where value is int or a memoryview slice."""
    end = len(buf) if end is None else end
    pos = start
    mv = memoryview(buf)
    while pos < end:
        key, pos = read_varint(buf, pos)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, pos = read_varint(buf, pos)
        elif wt == 2:
            n, pos = read_varint(buf, pos)
            v = mv[pos:pos + n]
            pos += n
        elif wt == 5:
            v = mv[pos:pos + 4]
            pos += 4
        elif wt == 1:
            v = mv[pos:pos + 8]
            pos += 8
        else:
            raise ValueError(f"unsupported wire type {wt}")
        yield fn, wt, v


def packed_varints(mv):
    b = bytes(mv)
    pos, out = 0, []
    while pos < len(b):
        v, pos = read_varint(b, pos)
        out.append(v)
    return out


def decode_value(mv):
    for fn, wt, v in fields(bytes(mv)):
        if fn == 1:
            return bytes(v).decode("utf-8", "replace")
        if fn == 2:
            return struct.unpack("<f", bytes(v))[0]
        if fn == 3:
            return struct.unpack("<d", bytes(v))[0]
        if fn in (4, 5):
            return v
        if fn == 6:
            return (v >> 1) ^ -(v & 1)
        if fn == 7:
            return bool(v)
    return None


class Layer:
    __slots__ = ("name", "version", "extent", "keys", "values", "features", "raw")

    def __init__(self):
        self.name, self.version, self.extent = "", 2, 4096
        self.keys, self.values, self.features = [], [], []  # values: raw Value message bytes
        self.raw = b""

    @classmethod
    def parse(cls, mv, with_features=True):
        l = cls()
        l.raw = mv
        for fn, wt, v in fields(bytes(mv)):
            if fn == 1:
                l.name = bytes(v).decode()
            elif fn == 15:
                l.version = v
            elif fn == 5:
                l.extent = v
            elif fn == 3:
                l.keys.append(bytes(v).decode())
            elif fn == 4:
                l.values.append(bytes(v))
            elif fn == 2:
                l.features.append(bytes(v) if with_features else None)
        return l

    def feature_parts(self, fbytes):
        """(id or None, type, [tag ints], geometry bytes)"""
        fid, ftype, tags, geom = None, 0, [], b""
        for fn, wt, v in fields(fbytes):
            if fn == 1:
                fid = v
            elif fn == 3:
                ftype = v
            elif fn == 2:
                tags = packed_varints(v)
            elif fn == 4:
                geom = bytes(v)
        return fid, ftype, tags, geom

    def feature_props(self, fbytes):
        _, ftype, tags, _ = self.feature_parts(fbytes)
        return ftype, {self.keys[tags[i]]: decode_value(self.values[tags[i + 1]]) for i in range(0, len(tags), 2)}


def decode_geometry(geom: bytes):
    """MVT command stream -> list of parts, each a list of (x, y) in tile units.

    Points: one part per point. Lines: one part per LineString. Polygons: one
    part per ring (exterior rings clockwise in tile coordinates, MVT 2.1)."""
    vals = packed_varints(geom)
    parts, cur = [], None
    x = y = 0
    i = 0
    while i < len(vals):
        cmd, count = vals[i] & 7, vals[i] >> 3
        i += 1
        if cmd == 1:  # MoveTo
            for _ in range(count):
                x += (vals[i] >> 1) ^ -(vals[i] & 1)
                y += (vals[i + 1] >> 1) ^ -(vals[i + 1] & 1)
                i += 2
                cur = [(x, y)]
                parts.append(cur)
        elif cmd == 2:  # LineTo
            for _ in range(count):
                x += (vals[i] >> 1) ^ -(vals[i] & 1)
                y += (vals[i + 1] >> 1) ^ -(vals[i + 1] & 1)
                i += 2
                cur.append((x, y))
        elif cmd == 7:  # ClosePath
            if cur:
                cur.append(cur[0])
        else:
            raise ValueError(f"unknown geometry command {cmd}")
    return parts


def ring_area(ring):
    """Signed area (tile units squared) of a closed ring; positive = clockwise in tile coordinates (y down)."""
    a = 0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        a += x1 * y2 - x2 * y1
    return a / 2


def parse_tile(data, with_features=True):
    return [Layer.parse(v, with_features) for fn, wt, v in fields(data) if fn == 3]


# ---------- encoding ----------

def enc_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def enc_field(fn, payload: bytes):
    return enc_varint((fn << 3) | 2) + enc_varint(len(payload)) + payload


def enc_feature(fid, ftype, tags, geom):
    out = b""
    if fid is not None:
        out += enc_varint(1 << 3) + enc_varint(fid)
    if tags:
        out += enc_field(2, b"".join(enc_varint(t) for t in tags))
    out += enc_varint(3 << 3) + enc_varint(ftype)
    out += enc_field(4, geom)
    return out


def merge_layers(a: Layer, b: Layer):
    """Union of two same-named layers. Returns (layer bytes, number of duplicate features dropped).

    Features of b are appended with their tag indices remapped into a's key/value
    tables. A feature of b whose id, type, geometry and decoded tags all equal a
    feature of a is dropped (the same OSM object rendered identically by both packs).
    """
    if a.extent != b.extent:
        raise ValueError(f"layer {a.name}: extent {a.extent} != {b.extent}")
    keys = list(a.keys)
    kidx = {k: i for i, k in enumerate(keys)}
    values = list(a.values)
    vidx = {v: i for i, v in enumerate(values)}
    seen = set()
    for f in a.features:
        fid, ft, tags, geom = a.feature_parts(f)
        seen.add((fid, ft, geom, frozenset((a.keys[tags[i]], a.values[tags[i + 1]]) for i in range(0, len(tags), 2))))
    feats = list(a.features)
    dropped = 0
    for f in b.features:
        fid, ft, tags, geom = b.feature_parts(f)
        sig = (fid, ft, geom, frozenset((b.keys[tags[i]], b.values[tags[i + 1]]) for i in range(0, len(tags), 2)))
        if sig in seen:
            dropped += 1
            continue
        new_tags = []
        for i in range(0, len(tags), 2):
            k, v = b.keys[tags[i]], b.values[tags[i + 1]]
            if k not in kidx:
                kidx[k] = len(keys)
                keys.append(k)
            if v not in vidx:
                vidx[v] = len(values)
                values.append(v)
            new_tags += [kidx[k], vidx[v]]
        feats.append(enc_feature(fid, ft, new_tags, geom))
    out = enc_varint(15 << 3) + enc_varint(a.version)
    out += enc_field(1, a.name.encode())
    out += b"".join(enc_field(2, f) for f in feats)
    out += b"".join(enc_field(3, k.encode()) for k in keys)
    out += b"".join(enc_field(4, v) for v in values)
    out += enc_varint(5 << 3) + enc_varint(a.extent)
    return out, dropped


def merge_tiles(data_a: bytes, data_b: bytes):
    """Merge two uncompressed MVT tiles. Returns (tile bytes, duplicates dropped)."""
    la = {l.name: l for l in parse_tile(data_a)}
    lb = {l.name: l for l in parse_tile(data_b)}
    out, dropped = b"", 0
    for name in list(la) + [n for n in lb if n not in la]:
        if name in la and name in lb:
            body, d = merge_layers(la[name], lb[name])
            dropped += d
        else:
            body = bytes((la.get(name) or lb[name]).raw)
        out += enc_field(3, body)
    return out, dropped
