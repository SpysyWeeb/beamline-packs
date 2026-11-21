# Copy of the snippet in docs/pack-format.md section 6 (kept identical; the report runs it).
"""Read a Beamline pack (folder or .tar, format 3), check its versions and, optionally, its checksums."""
import hashlib, json, sys, tarfile
from pathlib import Path

SUPPORTED_MANIFEST = 3
READS = {                                   # parts and the format versions this reader knows
    "map": ("pmtiles", 3),
    "routing": ("valhalla-tar", 1),
    "routing_index": ("beamline-routing-pack", 1),
    "search": ("beamline-search-sqlite", 1),
    "addresses": ("beamline-addresses-sqlite", 1),
}
REQUIRED = ("map", "routing", "routing_index", "search")   # "addresses" is optional (skippable)
STYLE_SCHEMA = ("protomaps-basemap", 4)     # tile schema and major version the style draws

def entry_name(e):                          # "./manifest.json" (as `tar -C dir .` writes) = "manifest.json"
    return e.name[2:] if e.name.startswith("./") else e.name

def read_manifest(path):
    p = Path(path)
    if p.is_dir():
        return json.loads((p / "manifest.json").read_text(encoding="utf-8"))
    with tarfile.open(p) as t:              # an archive: manifest.json is the first entry
        first = t.next()
        if first is None or not first.isfile() or entry_name(first) != "manifest.json":
            raise ValueError("first entry must be manifest.json")
        return json.loads(t.extractfile(first).read().decode("utf-8"))

def problems(m):
    v = m.get("manifest_version")
    if v != SUPPORTED_MANIFEST:
        why = "an old pack format; rebuild the pack" if isinstance(v, int) and v < SUPPORTED_MANIFEST else "update the reader"
        return [f"manifest_version {v}: {why}"]
    out = []
    if not isinstance(m.get("pack_version"), int):
        out.append("no pack_version")
    parts = m.get("parts", {})
    for name in REQUIRED:
        if name not in parts:
            out.append(f"no {name} part")
    for name, known in READS.items():
        part = parts.get(name)
        if part is not None and (part.get("format"), part.get("format_version")) != known:
            out.append(f"{name}: {part.get('format')} v{part.get('format_version')} unknown")
    mp = parts.get("map", {})
    try:                                    # "4.15.2" -> 4; anything else is not this style's schema
        major = int(str(mp.get("schema_version")).split(".")[0])
    except ValueError:
        major = None
    if (mp.get("schema"), major) != STYLE_SCHEMA:
        out.append(f"map tiles {mp.get('schema')} {mp.get('schema_version')} do not match the style")
    fps = {parts.get("search", {}).get("addr_key_fingerprint"), parts.get("addresses", {}).get("key_fingerprint", None)}
    if "addresses" in parts and len(fps) != 1:
        out.append("search and addresses were built with different street-key fingerprints")
    return out

def credit(m, skipped=()):
    """The credit for the parts installed: each part's attribution, split at "; ", each item once."""
    items = []
    for name, part in m["parts"].items():
        if name not in skipped:
            for item in (part.get("attribution") or m.get("attribution", "")).split(";"):
                if item.strip() and item.strip() not in items:
                    items.append(item.strip())
    return "; ".join(items)

def absent_parts(path, m):
    """Parts an installed folder does not hold, by its install-record.json (skipped, routing merged)."""
    rec = Path(path) / "install-record.json"
    if not rec.is_file():
        return set()
    r = json.loads(rec.read_text(encoding="utf-8"))
    out = {p for p in r.get("skipped_parts", []) if p == "addresses"}
    return out | ({"routing"} if r.get("routing_merged") else set())

def verify(path, m):
    """Sizes and SHA-256 of every part (slow: reads everything once)."""
    p = Path(path)
    absent = absent_parts(p, m) if p.is_dir() else set()
    want = {part["file"]: part for name, part in m["parts"].items() if name not in absent}
    def check(name, f):
        h, n = hashlib.sha256(), 0
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk); n += len(chunk)
        ok = n == want[name]["bytes"] and h.hexdigest() == want[name]["sha256"]
        print(f"  {name}: {n} bytes, sha256 {'ok' if ok else 'MISMATCH'}")
        return ok
    seen, ok = set(), True
    if p.is_dir():
        for name in want:
            if (p / name).is_file():
                with open(p / name, "rb") as f:
                    ok &= check(name, f); seen.add(name)
    else:
        with tarfile.open(p) as t:
            for e in t:
                if e.isfile() and entry_name(e) in want and entry_name(e) not in seen:
                    ok &= check(entry_name(e), t.extractfile(e)); seen.add(entry_name(e))
    for name in sorted(set(want) - seen):
        print(f"  {name}: MISSING")
    return ok and seen == set(want)

if __name__ == "__main__":
    try:
        m = read_manifest(sys.argv[1])
    except (ValueError, OSError, tarfile.TarError) as e:  # not a tar, no manifest, broken JSON
        sys.exit(f"not a readable pack: {e}")
    print(f"{m['pack_id']} {m.get('pack_version')}: {m['name']}, OSM {m['osm_snapshot']}, parts {', '.join(m['parts'])}")
    print("credit:", credit(m))
    found = problems(m)
    print("problems:", found or "none")
    if "--verify" in sys.argv:
        good = verify(sys.argv[1], m)
        print("checksums ok" if good else "checksums FAILED")
        sys.exit(0 if good and not found else 1)
    sys.exit(1 if found else 0)
