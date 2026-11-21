"""Offline search query engine (prototype) over the SQLite pack built by build.py.

Design rules, so the logic can be ported to Kotlin later:
  * all data access is plain SQL (FTS5 MATCH, R*Tree ranges, B-tree lookups);
    no SQLite extensions, no custom tokenizer, no SQL math functions;
  * words, abbreviations, categories, brands and weights live in data
    (abbreviations.json, ranking.json and tables inside the database);
  * this module has no UI code: search() returns plain dicts.

Pipeline for one query string (README.md has the long version):
  0. the text is cut to ranking.json search.max_query_chars code points
     (rule S5; the Kotlin engine cuts the same way), and the answer's "query" is
     the cut text; a position that is missing, not finite or out of range is
     ignored (position_ignored); one far outside the pack is flagged out_of_coverage;
  1. normalise (normalize.py); split off 'near me', a trailing state word
     ('erie pa'), a connector ('near X', 'in X', 'on X') and a leading house
     number (unless the number starts a brand or name: '7 eleven', '84 lumber');
  2. analyse the remaining words: whole phrase = category term? brand alias?
     known name? Otherwise try a trailing place ('walmart erie');
  3. typo candidates for unknown words and for rare words that sit one edit
     away from a far more common word (SymSpell-style deletion table);
  4. fetch candidates with SQL per interpretation: category browse (R*Tree
     rings), brand browse (ordered by distance), name search (FTS5);
  5. score = tiered name match + distance decay + importance + context
     bonus (weights in ranking.json); merge, deduplicate, top N;
  6. house numbers: street results are joined to the addresses track's
     database through street.addr_key (computed at build time); the located
     point replaces the street's point before distance and de-duplication.
  7. the streets' lines (table street_line, lines.py; a pack without the table
     answers as before): with a position, a street result's point is the point
     of its line nearest to the position, not the street's middle (so its
     distance, its pin and a route to it agree); 'state st and 12th', 'peach & w 26th',
     'state at 12th' also give the places where the two streets meet, first
     (ranking.json crossing); near_street() names the street at a point.
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lines  # noqa: E402
import normalize  # noqa: E402

KIND_PLACE, KIND_STREET, KIND_POI = 1, 2, 3
KIND_NAME = {1: "place", 2: "street", 3: "poi"}
ENTRY_COLS = "e.id, e.kind, e.name, e.name_key, e.alt_keys, e.type, e.lat, e.lon, e.importance, " \
             "e.muni_id, e.county_id, e.brand_id, e.subtitle"
HOUSE_RE = re.compile(r"^\d+[a-z]?$")
# row indexes
R_ID, R_KIND, R_NAME, R_KEY, R_ALT, R_TYPE, R_LAT, R_LON, R_IMP, R_MUNI, R_COUNTY, R_BRAND, R_SUB = range(13)


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(min(1.0, a)))


def osa_distance(a: str, b: str) -> int:
    """Optimal string alignment distance (Levenshtein + adjacent swaps)."""
    la, lb = len(a), len(b)
    if abs(la - lb) > 2:
        return 3
    prev2 = None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[lb]


def prefix_upper(p: str) -> str:
    """Smallest string greater than every string that starts with p."""
    return p[:-1] + chr(ord(p[-1]) + 1) if p else "￿"


def fts_quote(tok: str) -> str:
    return '"' + tok.replace('"', '""') + '"'


def ro_uri(path: str) -> str:
    """Read-only SQLite URI for a file path; '#', '%', '?' and spaces are percent-encoded."""
    return pathlib.Path(path).resolve().as_uri() + "?mode=ro"


def valid_position(lat, lon):
    """(lat, lon) as floats, or None when missing, not numeric, not finite or out of range."""
    if lat is None or lon is None:
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)) or abs(lat) > 90 or abs(lon) > 180:
        return None
    return lat, lon


def poi_cell(lat: float, lon: float) -> int:
    """Grid cell of the poi_prefix table (0.1 degree; same formula as build.py)."""
    return (math.floor(lat * 10) + 900) * 4000 + math.floor(lon * 10) + 1800


class Phrase:
    """A run of query words in the forms the lookups need."""

    def __init__(self, raw: list[str], partial: bool, stop: set):
        self.raw = list(raw)                      # folded words as typed
        self.partial = partial and bool(raw)      # last word may be unfinished
        exp = normalize.expand_tokens(self.raw)
        self.exp = [t for t in exp if t not in stop] or exp
        raw_ns = [t for t in self.raw if t not in stop] or self.raw
        self.key = " ".join(self.exp)             # canonical key (abbreviations expanded)
        # same, but the last word as typed: 'gas st' must prefix-match 'gas station'
        self.key_raw = " ".join(self.exp[:-1] + [raw_ns[-1]]) if self.exp and raw_ns else self.key
        self.last_raw = raw_ns[-1] if raw_ns else ""

    def __bool__(self):
        return bool(self.exp)


class Candidate:
    __slots__ = ("row", "score", "how", "tier")

    def __init__(self, row, score, how, tier=None):
        self.row, self.score, self.how, self.tier = row, score, how, tier


class SearchEngine:
    def __init__(self, db_path: str, ranking_path: str | None = None, addresses_path: str | None = None,
                 allow_addr_version_mismatch: bool = False):
        self.allow_addr_version_mismatch = allow_addr_version_mismatch
        self.addr_warning = None
        self.db = sqlite3.connect(ro_uri(db_path), uri=True, check_same_thread=False)
        self.db.execute("PRAGMA query_only=1")
        with open(ranking_path or os.path.join(HERE, "ranking.json"), encoding="utf-8") as f:
            self.rk = json.load(f)
        self.tb = normalize.tables()
        self.stop = self.tb["_stop"]
        self.cat_label, self.cat_code = {}, {}
        for cid, code, label in self.db.execute("SELECT id, code, label FROM category"):
            self.cat_label[cid] = label
            self.cat_code[cid] = code
        self.cat_id_by_code = {v: k for k, v in self.cat_code.items()}
        self.meta = dict(self.db.execute("SELECT key, value FROM meta"))
        # pack coverage (state bounding box): minlat, maxlat, minlon, maxlon
        self.bbox = tuple(float(x) for x in self.meta["bbox"].split(",")) if self.meta.get("bbox") else None
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.has_poi_prefix = "poi_prefix" in tables
        self.has_lines = "street_line" in tables
        self._lines: dict[int, list] = {}
        self.has_addr_key = "addr_key" in {r[1] for r in self.db.execute("PRAGMA table_info(street)")}
        self.state_names = set(self.tb.get("state_names", []))
        self.street_types = set(self.tb.get("street_types", []))
        # connector words are never typo-corrected ('near' must not become 'bear')
        self.no_fix = {w for words in self.tb["connectors"].values() for ph in words for w in ph.split()} | \
                      {w for ph in self.tb["near_me"] for w in ph.split()}
        self.addr_ok = False
        if addresses_path and os.path.exists(addresses_path):
            self._attach_addresses(addresses_path)
        self.conn_words = sorted(((tuple(w.split()), kind) for kind, words in self.tb["connectors"].items()
                                  for w in words), key=lambda x: -len(x[0]))
        self.near_me = sorted((tuple(w.split()) for w in self.tb["near_me"]), key=len, reverse=True)

    def _attach_addresses(self, addresses_path: str) -> None:
        """House-number join point: ATTACH the addresses track's database read-only.
        The join key (street.addr_key) was computed at build time with that track's
        street_key(), so no code of that track runs here. Fails loudly when the join
        cannot work (old search database, other normaliser version, missing view)."""
        a = self.rk["addresses"]
        if not self.has_addr_key:
            raise RuntimeError("search database has no street.addr_key column: rebuild it with build.py "
                               "or open the engine without addresses_path")
        built = json.loads(self.meta.get("addr_key") or "{}")
        if not built.get("table_version"):
            raise RuntimeError(f"search database was built without the house-number join key: {built}")
        self.db.execute("ATTACH DATABASE ? AS addr", (ro_uri(addresses_path),))
        r = self.db.execute(f"SELECT value FROM addr.{a['version_table']} WHERE key = ?", (a["version_key"],)).fetchone()
        if r is not None and str(r[0]) != str(built["table_version"]):
            msg = (f"addresses database uses normalisation table version {r[0]}, the search database was keyed "
                   f"with version {built['table_version']}: rebuild whichever is older")
            if not self.allow_addr_version_mismatch:
                raise RuntimeError(msg)
            self.addr_warning = msg + " (attached anyway: allow_addr_version_mismatch)"
        self.db.execute(f"SELECT {a['housenumber']}, {a['street_key']}, {a['lat']}, {a['lon']} "
                        f"FROM addr.{a['table']} LIMIT 1").fetchall()
        self.addr_ok = True

    # ------------------------------------------------------------------ word lookups
    def vocab_freq(self, tok: str, prefix: bool = False) -> int:
        if prefix:
            r = self.db.execute("SELECT 1 FROM vocab WHERE word >= ? AND word < ? LIMIT 1",
                                (tok, prefix_upper(tok))).fetchone()
            return 1 if r else 0
        r = self.db.execute("SELECT freq FROM vocab WHERE word = ?", (tok,)).fetchone()
        return r[0] if r else 0

    def corrections(self, tok: str) -> list[tuple[str, int, int]]:
        """Typo candidates [(word, distance, freq)], best first.
        1) SymSpell-style: hashed distance-1 deletions (spell_del) -> words within 1 edit;
        2) for long words with no such hit: FTS5 trigram candidates within 2 edits."""
        s = self.rk["search"]
        if len(tok) < s["fuzzy_min_chars"] or any(ch.isdigit() for ch in tok):
            return []
        hs = list({normalize.fnv1a32(d) for d in normalize.deletes1(tok)})
        q = ("SELECT DISTINCT v.word, v.freq FROM spell_del d JOIN vocab v ON v.id = d.word_id "
             f"WHERE d.h IN ({','.join('?' * len(hs))})")
        out = []
        for w, freq in self.db.execute(q, hs):
            d = osa_distance(tok, w)
            if d <= 1 and w != tok:
                out.append((w, d, freq))
        if not out and len(tok) >= s["trigram_fallback_from_chars"]:
            grams = sorted({tok[i:i + 3] for i in range(len(tok) - 2)})
            match = " OR ".join(fts_quote(g) for g in grams)
            try:
                rows = self.db.execute("SELECT v.word, v.freq FROM vocab_tri t JOIN vocab v ON v.id = t.rowid "
                                       "WHERE vocab_tri MATCH ? ORDER BY rank LIMIT ?",
                                       (match, s["trigram_candidates"])).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for w, freq in rows:
                d = osa_distance(tok, w)
                if d <= s["trigram_max_distance"] and w != tok:
                    out.append((w, d, freq))
        out.sort(key=lambda x: (x[1], -x[2]))
        return out[: s["fuzzy_candidates_per_token"]]

    def category_lookup(self, ph_key: str, prefix_key: str | None = None) -> dict[int, float]:
        """Category ids (with weights) for a phrase key; plural 's' tolerated;
        prefix_key enables search-as-you-type on category words."""
        if not ph_key:
            return {}
        rows = self.db.execute("SELECT cat_id, weight FROM category_term WHERE term = ?", (ph_key,)).fetchall()
        if not rows and ph_key.endswith("s") and len(ph_key) > 3:
            rows = self.db.execute("SELECT cat_id, weight FROM category_term WHERE term IN (?, ?)",
                                   (ph_key[:-1], ph_key[:-2] if ph_key.endswith("es") else ph_key[:-1])).fetchall()
        factor = 1.0
        if not rows and prefix_key and len(prefix_key) >= 3:
            found = self.db.execute("SELECT cat_id, weight, term FROM category_term WHERE term >= ? AND term < ? LIMIT 50",
                                    (prefix_key, prefix_upper(prefix_key))).fetchall()
            if found:
                # 'gas st' completes to 'gas station' before 'gas station store'
                shortest = min(len(t) for _, _, t in found)
                rows = [(c, w * shortest / len(t)) for c, w, t in found]
            factor = self.rk["search"]["prefix_factor"]
        if not rows:
            return {}
        best = max(w for _, w in rows)
        keep = self.rk["search"]["category_keep_ratio"]
        out = {}
        for c, w in rows:
            if w >= best * keep:
                out[c] = max(out.get(c, 0.0), w * factor)
        return out

    def brand_lookup(self, ph_key: str, prefix_key: str | None = None) -> dict[int, float]:
        """Brand ids for a phrase key -> weight (1.0 exact alias, prefix_factor for a
        prefix while typing). Exact aliases need brands with >= brand_min_pois POIs."""
        if not ph_key:
            return {}
        S = self.rk["search"]
        keys = [ph_key] + ([ph_key.replace(" ", "")] if " " in ph_key else [])
        rows = self.db.execute(
            "SELECT a.brand_id, a.count, 1.0 FROM brand_alias a JOIN brand b ON b.id = a.brand_id "
            f"WHERE a.alias IN ({','.join('?' * len(keys))}) AND b.poi_count >= ?",
            keys + [S["brand_min_pois"]]).fetchall()
        if prefix_key and len(prefix_key) >= S["brand_prefix_min_chars"]:
            short = len(prefix_key) < 3
            for k in {prefix_key, prefix_key.replace(" ", "")}:
                rows += self.db.execute(
                    "SELECT brand_id, count, ? FROM brand_alias WHERE alias > ? AND alias < ? "
                    "AND count >= ? ORDER BY count DESC LIMIT 20",
                    (S["prefix_factor"], k, prefix_upper(k),
                     S["short_brand_min_count"] if short else S["brand_min_pois"])).fetchall()
        if not rows:
            return {}
        # keep brands that own a fair share of the spelling's uses (drops stray tags)
        top = max(c for _, c, _ in rows)
        out = {}
        for b, c, f in rows:
            if c >= max(1, 0.05 * top):
                out[b] = max(out.get(b, 0.0), f)
        return out

    def place_lookup(self, key: str, anchor=None, allow_prefix=False, suffixes=True) -> list[tuple]:
        """Places whose key equals key; optionally 'X township/county/borough' and prefixes."""
        rows = self.db.execute(f"SELECT {ENTRY_COLS} FROM entry e WHERE e.name_key = ? AND e.kind = 1", (key,)).fetchall()
        if not rows and suffixes:
            rows = self.db.execute(f"SELECT {ENTRY_COLS} FROM entry e WHERE e.name_key IN (?, ?, ?) AND e.kind = 1",
                                   (key + " township", key + " county", key + " borough")).fetchall()
        if not rows and allow_prefix and len(key) >= 3:
            rows = self.db.execute(f"SELECT {ENTRY_COLS} FROM entry e WHERE e.name_key >= ? AND e.name_key < ? "
                                   "AND e.kind = 1 ORDER BY e.importance DESC LIMIT 10",
                                   (key, prefix_upper(key))).fetchall()
        if not rows:
            rows = self.db.execute(
                f"SELECT {ENTRY_COLS} FROM entry_fts f JOIN entry e ON e.id = f.rowid "
                "WHERE entry_fts MATCH ? AND e.kind = 1 ORDER BY e.importance DESC LIMIT 20",
                (" AND ".join(fts_quote(t) for t in key.split()),)).fetchall() if key else []
            rows = [r for r in rows if key in (r[R_ALT] or "").split("|")]

        def k(r):  # most important first; nearer first among similar
            d = haversine_km(anchor[0], anchor[1], r[R_LAT], r[R_LON]) if anchor else 0.0
            return -(r[R_IMP] - min(d, 300) / 1000.0)
        rows.sort(key=k)
        return rows

    def name_exists(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM entry WHERE name_key = ? LIMIT 1", (key,)).fetchone() is not None

    # ------------------------------------------------------------------ parsing
    def parse(self, text: str) -> dict:
        partial = not text.endswith(" ")
        toks = normalize.fold(text).split()
        near_me = False
        for ph in self.near_me:
            n = len(ph)
            for i in range(len(toks) - n + 1):
                if tuple(toks[i:i + n]) == ph:
                    near_me = True
                    if i + n == len(toks):
                        partial = False
                    toks = toks[:i] + toks[i + n:]
                    break
        toks, state_dropped = self._drop_state(toks)
        if state_dropped == "last":
            partial = False                  # 'erie pa': 'erie' was finished
        house = None
        if len(toks) >= 2 and HOUSE_RE.match(toks[0]) and not self._number_starts_name(toks):
            house, toks = toks[0], toks[1:]
        conn, head, tail = None, toks, []
        for i in range(1, len(toks)):
            for ph, kind in self.conn_words:
                n = len(ph)
                if tuple(toks[i:i + n]) == ph and i + n < len(toks):
                    conn, head, tail = kind, toks[:i], toks[i + n:]
                    break
            if conn:
                break
        return {"tokens": toks, "head": head, "tail": tail, "conn": conn, "near_me": near_me,
                "house": house, "partial": partial, "state_dropped": bool(state_dropped)}

    def _known_phrase(self, key: str) -> bool:
        """Is this key a name, a brand alias (any size) or a category word?"""
        if not key:
            return False
        if self.name_exists(key) or self.category_lookup(key):
            return True
        return self.db.execute("SELECT 1 FROM brand_alias WHERE alias IN (?, ?) LIMIT 1",
                               (key, key.replace(" ", ""))).fetchone() is not None

    def _drop_state(self, toks: list[str]):
        """'erie pa', 'pittsburgh, pa', 'philadelphia pennsylvania', 'erie pa 16501':
        drop the state word unless the whole phrase is itself known ('university of
        pennsylvania'). Returns (tokens, 'last' | 'before_zip' | None)."""
        if len(toks) < 2:
            return toks, None
        if toks[-1] in self.state_names:
            cand, how = toks[:-1], "last"
        elif len(toks) >= 3 and toks[-2] in self.state_names and re.fullmatch(r"\d{5}", toks[-1]):
            cand, how = toks[:-2] + toks[-1:], "before_zip"
        else:
            return toks, None
        if self._known_phrase(normalize.key(" ".join(toks))):
            return toks, None
        return cand, how

    def _number_starts_name(self, toks: list[str]) -> bool:
        """A leading number is a house number ('4319 peach st') unless it begins a
        brand, category or name ('7 eleven', '84 lumber', '99 cents'). Any street-type
        word after the number keeps it a house number."""
        head = toks
        for i in range(1, len(toks) - 1):
            if any(tuple(toks[i:i + len(ph)]) == ph for ph, _ in self.conn_words):
                head = toks[:i]          # '84 lumber near pittsburgh' -> '84 lumber'
                break
        if any(t in self.street_types for t in normalize.expand_tokens(head[1:])):
            return False
        for n in sorted({len(head), 2}, reverse=True):
            if n > len(head):
                continue
            k = normalize.key(" ".join(head[:n]))
            if self._known_phrase(k):
                return True
            if self.db.execute("SELECT 1 FROM entry WHERE name_key >= ? AND name_key < ? AND kind IN (1, 3) LIMIT 1",
                               (k, prefix_upper(k))).fetchone():
                return True                # a place or POI name starts with it ('99 cents ...')
        return False

    # ------------------------------------------------------------------ candidate fetchers
    def _fts(self, match: str, anchor, kinds=None, limit=None):
        limit = limit or self.rk["search"]["fts_candidates"]
        kind_sql = f" AND e.kind IN ({','.join(str(int(k)) for k in kinds)})" if kinds else ""
        rows = []
        try:
            if anchor:
                k = math.cos(math.radians(anchor[0])) ** 2
                rows += self.db.execute(
                    f"SELECT {ENTRY_COLS} FROM entry_fts f JOIN entry e ON e.id = f.rowid WHERE entry_fts MATCH ?{kind_sql} "
                    "ORDER BY (e.lat - ?) * (e.lat - ?) + (e.lon - ?) * (e.lon - ?) * ? LIMIT ?",
                    (match, anchor[0], anchor[0], anchor[1], anchor[1], k, limit)).fetchall()
            rows += self.db.execute(
                f"SELECT {ENTRY_COLS} FROM entry_fts f JOIN entry e ON e.id = f.rowid WHERE entry_fts MATCH ?{kind_sql} "
                "ORDER BY e.importance DESC LIMIT ?", (match, limit // 4 if anchor else limit)).fetchall()
        except sqlite3.OperationalError:
            pass
        return rows

    def fts_match(self, options: list[list[str]], partial: bool, last_raw: str | None) -> str:
        """FTS5 query: AND of positions, OR of alternatives; last word as prefix."""
        parts = []
        for i, opts in enumerate(options):
            alts = [fts_quote(o) for o in opts]
            if i == len(options) - 1 and partial:
                alts = [a + "*" for a in alts]
                if last_raw and last_raw not in opts:
                    alts.append(fts_quote(last_raw) + "*")
            parts.append(alts[0] if len(alts) == 1 else "(" + " OR ".join(alts) + ")")
        return " AND ".join(parts)

    def short_prefix_candidates(self, key: str, anchor):
        """Search-as-you-type for 1-3 characters: places by key prefix (partial index
        entry_place_key), POIs by the first 3 characters of their key in the grid
        cells around the anchor (table poi_prefix), nearest first."""
        hi = prefix_upper(key)
        rows = self.db.execute(
            f"SELECT {ENTRY_COLS} FROM entry e WHERE e.kind = 1 AND e.name_key >= ? AND e.name_key < ? "
            "ORDER BY e.importance DESC LIMIT 30", (key, hi)).fetchall()
        if anchor and self.has_poi_prefix:
            S = self.rk["search"]
            p3 = key[:3]
            la0, lo0 = math.floor(anchor[0] * 10), math.floor(anchor[1] * 10)
            ids, done = [], set()
            for ring in S["short_prefix_poi_rings"]:
                cells = [(la + 900) * 4000 + lo + 1800 for la in range(la0 - ring, la0 + ring + 1)
                         for lo in range(lo0 - ring, lo0 + ring + 1)]
                cells = [c for c in cells if c not in done]
                done.update(cells)
                ids += [r[0] for r in self.db.execute(
                    f"SELECT entry_id FROM poi_prefix WHERE cell IN ({','.join(str(c) for c in cells)}) "
                    "AND p3 >= ? AND p3 < ? LIMIT 5000", (p3, prefix_upper(p3)))]
                if len(ids) >= S["short_prefix_poi_min_hits"]:
                    break
            near = []
            for i in range(0, len(ids), 900):
                chunk = ids[i:i + 900]
                near += self.db.execute(f"SELECT {ENTRY_COLS} FROM entry e WHERE e.id IN ({','.join('?' * len(chunk))})",
                                        chunk).fetchall()
            if len(key) > 3:
                near = [r for r in near if r[R_KEY].startswith(key)]
            k = math.cos(math.radians(anchor[0])) ** 2
            near.sort(key=lambda r: (r[R_LAT] - anchor[0]) ** 2 + (r[R_LON] - anchor[1]) ** 2 * k)
            return rows + near[: S["short_prefix_poi_keep"]]
        if anchor:
            # databases built before poi_prefix existed
            for r_km in (5, 20):
                dlat = r_km / 111.0
                dlon = r_km / (111.0 * max(0.2, math.cos(math.radians(anchor[0]))))
                near = self.db.execute(
                    f"SELECT {ENTRY_COLS} FROM entry_rtree r JOIN entry e ON e.id = r.id "
                    "WHERE r.maxlat >= ? AND r.minlat <= ? AND r.maxlon >= ? AND r.minlon <= ? "
                    "AND e.name_key >= ? AND e.name_key < ? AND e.kind = 3 LIMIT 300",
                    (anchor[0] - dlat, anchor[0] + dlat, anchor[1] - dlon, anchor[1] + dlon, key, hi)).fetchall()
                rows += near
                if len(near) >= 20:
                    break
        return rows

    def browse_category(self, cat_ids, anchor, limit=None):
        cats = ",".join(str(int(c)) for c in cat_ids)
        want = limit or self.rk["search"]["browse_min_hits"]
        if not anchor:
            return self.db.execute(
                f"SELECT {ENTRY_COLS}, c.cat_id FROM poi_cat c JOIN entry e ON e.id = c.entry_id "
                f"WHERE c.cat_id IN ({cats}) ORDER BY e.importance DESC LIMIT ?", (want,)).fetchall()
        rows = []
        rtree_first_km = self.rk["search"].get("browse_rtree_first_max_km", 0)
        for r_km in self.rk["search"]["near_radius_km"]:
            dlat = r_km / 111.0
            dlon = r_km / (111.0 * max(0.2, math.cos(math.radians(anchor[0]))))
            box = (anchor[0] - dlat, anchor[0] + dlat, anchor[1] - dlon, anchor[1] + dlon)
            if r_km <= rtree_first_km:
                # small box: walk the R*Tree box and probe poi_cat (CROSS JOIN fixes the join
                # order); SQLite's own plan walks every POI of the category state-wide instead
                rows = self.db.execute(
                    f"SELECT {ENTRY_COLS}, c.cat_id FROM entry_rtree r CROSS JOIN poi_cat c CROSS JOIN entry e "
                    f"WHERE c.entry_id = r.id AND e.id = r.id AND c.cat_id IN ({cats}) "
                    "AND r.maxlat >= ? AND r.minlat <= ? AND r.maxlon >= ? AND r.minlon <= ?", box).fetchall()
            else:
                rows = self.db.execute(
                    f"SELECT {ENTRY_COLS}, c.cat_id FROM entry_rtree r JOIN poi_cat c ON c.entry_id = r.id "
                    f"JOIN entry e ON e.id = r.id WHERE c.cat_id IN ({cats}) "
                    "AND r.maxlat >= ? AND r.minlat <= ? AND r.maxlon >= ? AND r.minlon <= ?", box).fetchall()
            if len(rows) >= want:
                break
        return rows

    def browse_brand(self, brand_ids, anchor, limit=60):
        b = ",".join(str(int(x)) for x in brand_ids)
        if not anchor:
            return self.db.execute(f"SELECT {ENTRY_COLS} FROM entry e WHERE e.brand_id IN ({b}) "
                                   "ORDER BY e.importance DESC LIMIT ?", (limit,)).fetchall()
        k = math.cos(math.radians(anchor[0])) ** 2
        return self.db.execute(
            f"SELECT {ENTRY_COLS} FROM entry e WHERE e.brand_id IN ({b}) "
            "ORDER BY (e.lat - ?) * (e.lat - ?) + (e.lon - ?) * (e.lon - ?) * ? LIMIT ?",
            (anchor[0], anchor[0], anchor[1], anchor[1], k, limit)).fetchall()

    def in_bbox(self, bbox, cat_ids=None, brand_ids=None):
        minlat, maxlat, minlon, maxlon = bbox
        if cat_ids and not brand_ids:
            # walk the box, probe poi_cat, fetch only matching entries (see browse_category)
            return self.db.execute(
                f"SELECT DISTINCT {ENTRY_COLS} FROM entry_rtree r CROSS JOIN poi_cat c CROSS JOIN entry e "
                f"WHERE c.entry_id = r.id AND e.id = r.id AND c.cat_id IN ({','.join(str(int(c)) for c in cat_ids)}) "
                "AND r.maxlat >= ? AND r.minlat <= ? AND r.maxlon >= ? AND r.minlon <= ? AND e.kind = 3 LIMIT 500",
                (minlat, maxlat, minlon, maxlon)).fetchall()
        cond = ""
        if cat_ids:
            cond += f" AND e.id IN (SELECT entry_id FROM poi_cat WHERE cat_id IN ({','.join(str(int(c)) for c in cat_ids)}))"
        if brand_ids:
            cond += f" AND e.brand_id IN ({','.join(str(int(b)) for b in brand_ids)})"
        return self.db.execute(
            f"SELECT {ENTRY_COLS} FROM entry_rtree r JOIN entry e ON e.id = r.id "
            f"WHERE r.maxlat >= ? AND r.minlat <= ? AND r.maxlon >= ? AND r.minlon <= ? AND e.kind = 3{cond} LIMIT 500",
            (minlat, maxlat, minlon, maxlon)).fetchall()

    # ------------------------------------------------------------------ the streets' lines
    def street_lines(self, eid: int) -> list:
        """The lines of a street entry (lines.decode), [] without one or when its blob is broken."""
        if not self.has_lines:
            return []
        hit = self._lines.get(eid)
        if hit is None:
            row = self.db.execute("SELECT line FROM street_line WHERE entry_id = ?", (eid,)).fetchone()
            try:
                hit = lines.decode(row[0]) if row else []
            except ValueError:
                hit = []
            if len(self._lines) >= 512:
                self._lines.clear()
            self._lines[eid] = hit
        return hit

    def _street_side(self, toks: list[str], partial: bool, user) -> list:
        """The streets one side of a crossing names ('state st', '12th'), the nearest first, each once."""
        ph = Phrase(toks, partial, self.stop)
        if not ph:
            return []
        n = self.rk["crossing"]["candidates_per_side"]
        key, key_raw = ph.key, ph.key_raw
        rows = self._fts(self.fts_match([[t] for t in ph.exp], partial, ph.last_raw), user, kinds=[KIND_STREET], limit=n)
        if not rows:
            fixed = [self._best_fix(t) for t in ph.exp]
            if fixed != ph.exp:
                rows = self._fts(self.fts_match([[t] for t in fixed], partial, None), user, kinds=[KIND_STREET], limit=n)
                key = key_raw = " ".join(fixed)
        ok = ("exact", "exact_alt", "prefix", "word_prefix")
        out, seen = [], set()
        for r in rows:
            if r[R_ID] not in seen and (self.name_tier(key, r) in ok or self.name_tier(key_raw, r) in ok):
                seen.add(r[R_ID])
                # own: the street's own name was typed, not one of its other names ('state' also finds
                # every road whose other name is 'State Route ...')
                bare = r[:R_ALT] + (None,) + r[R_ALT + 1:]
                out.append((r, self.name_tier(key, bare) in ok or self.name_tier(key_raw, bare) in ok))
        return out

    def crossings(self, text: str, user) -> list[dict]:
        """Where two streets the text names meet ('state st and 12th', 'peach & w 26th', 'state at 12th'):
        result dicts as search() gives them, the nearest to [user] first (without a position in the order
        found: the best street matches first). [] when the text has no such word, a side names no street,
        or the streets do not meet. '&' folds to 'and'; '@' is read as 'at'. When two streets typed by
        their own names meet, crossings found only through another name of a road are left out. The
        words are the parsed ones ('near me' and a trailing state are off: 'state st and 12th pa'); a text
        that starts with a house number names no crossing ([partial] is not used: the parse says it).

        A result's id is negative and its own: the two street ids and the number of the meeting among that
        pair's (a pair can meet more than once), so two crossings never share an id in a list or a store."""
        if not self.has_lines:
            return []
        C = self.rk["crossing"]
        pp = self.parse(text.replace("@", " at "))
        if pp["house"]:
            return []
        toks, partial = pp["tokens"], pp["partial"]
        found = []
        for i in range(1, len(toks) - 1):
            if toks[i] not in C["words"]:
                continue
            left = self._street_side(toks[:i], False, user)
            right = self._street_side(toks[i + 1:], partial, user) if left else []
            if not right:
                continue
            ids = [r[R_ID] for r, _ in left + right]
            boxes = {}
            for k in range(0, len(ids), 900):
                chunk = ids[k:k + 900]
                for row in self.db.execute(
                        f"SELECT id, minlat, maxlat, minlon, maxlon FROM entry_rtree WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                    boxes[row[0]] = row[1:]
            m = C["box_margin_deg"]
            tests = work = 0
            for a, own_a in left:
                ba = boxes.get(a[R_ID])
                for b, own_b in right:
                    bb = boxes.get(b[R_ID])
                    if a[R_ID] == b[R_ID] or a[R_KEY] == b[R_KEY] or ba is None or bb is None:
                        continue
                    if ba[0] > bb[1] + m or bb[0] > ba[1] + m or ba[2] > bb[3] + m or bb[2] > ba[3] + m:
                        continue
                    if tests >= C["max_pairs"]:
                        break
                    la, lb = self.street_lines(a[R_ID]), self.street_lines(b[R_ID])
                    # the work of one pair: its segments against each other; a pair that would go over what
                    # one query may spend is left out (two long roads of a thousand points each)
                    cost = sum(len(x) - 1 for x in la) * sum(len(x) - 1 for x in lb)
                    if work + cost > C["max_segment_tests"]:
                        continue
                    work += cost
                    tests += 1
                    for k, (lat, lon) in enumerate(lines.crossings(la, lb, C["within_m"], C["apart_m"])[:8]):
                        found.append((a, b, lat, lon, own_a and own_b, k))
            if found:
                break
        if not found:
            return []
        if any(f[4] for f in found):
            found = [f for f in found if f[4]]
        if user:
            found = sorted(found, key=lambda f: haversine_km(user[0], user[1], f[2], f[3]))   # stable
        out, shown = [], []
        for a, b, lat, lon, _, k in found:
            if any(haversine_km(lat, lon, s[0], s[1]) * 1000.0 < C["apart_m"] for s in shown):
                continue
            shown.append((lat, lon))
            lat, lon = round(lat, 6), round(lon, 6)
            d = haversine_km(user[0], user[1], lat, lon) if user else None
            out.append({"id": -((((a[R_ID] << 32) | b[R_ID]) << 3) | k), "kind": "street", "title": f"{a[R_NAME]} & {b[R_NAME]}",
                        "subtitle": a[R_SUB] or "", "category": "Intersection", "type": "intersection",
                        "lat": lat, "lon": lon, "distance_km": round(d, 2) if d is not None else None,
                        "brand_id": None, "score": C["score"], "how": "crossing", "tier": "crossing"})
            if len(out) >= C["max_results"]:
                break
        return out

    def near_street(self, lat: float, lon: float) -> dict | None:
        """The street at a point (a dropped pin): the street entry whose line is nearest, within
        ranking.json near_street.within_m, as {"id", "name", "subtitle", "distance_m", "lat", "lon", "house"}
        (the point of the line; "house": the house number of that street's address point nearest to the
        position, within near_street.house_within_m, or None: [_house_near]); None without a street, without
        lines in the pack, or for a position that is not usable. Equal distances: the more important street,
        then the lower id."""
        at = valid_position(lat, lon)
        if not at or not self.has_lines:
            return None
        N = self.rk["near_street"]
        dlat = N["within_m"] / lines.M_PER_DEG
        dlon = N["within_m"] / (lines.M_PER_DEG * math.cos(math.radians(at[0])))
        rows = self.db.execute(
            "SELECT e.id, e.name, e.subtitle, e.importance FROM entry_rtree t JOIN entry e ON e.id = t.id "
            "WHERE t.minlat <= ? AND t.maxlat >= ? AND t.minlon <= ? AND t.maxlon >= ? AND e.kind = 2 ORDER BY e.id LIMIT ?",
            (at[0] + dlat, at[0] - dlat, at[1] + dlon, at[1] - dlon, N["max_streets"])).fetchall()
        best = None
        for eid, name, sub, imp in rows:
            nb = lines.nearest(self.street_lines(eid), at[0], at[1])
            if nb is None or nb[0] > N["within_m"]:
                continue
            key = (round(nb[0], 1), -imp, eid)
            if best is None or key < best[0]:
                best = (key, {"id": eid, "name": name, "subtitle": sub or "", "distance_m": round(nb[0], 1),
                              "lat": round(nb[1], 6), "lon": round(nb[2], 6)})
        if best is None:
            return None
        out = best[1]
        out["house"] = self._house_near(out["id"], at)
        return out

    def _house_near(self, eid: int, at) -> str | None:
        """The house number on street entry [eid] nearest to [at]: among the address points of the
        street's key (the addresses database, as [_houses] joins it) within near_street.house_within_m of
        the position; equal distances (to 0.1 m): the lower number as text. None without the addresses
        database, without a key, or when no address point is that near."""
        if not self.addr_ok or not self.has_addr_key:
            return None
        k = self.db.execute("SELECT addr_key FROM street WHERE entry_id = ?", (eid,)).fetchone()
        if not k or not k[0]:
            return None
        a = self.rk["addresses"]
        within = self.rk["near_street"]["house_within_m"]
        dlat = within / lines.M_PER_DEG
        dlon = within / (lines.M_PER_DEG * math.cos(math.radians(at[0])))
        rows = self.db.execute(
            f"SELECT {a['housenumber']}, {a['lat']}, {a['lon']} FROM addr.{a['table']} WHERE {a['street_key']} = ? "
            f"AND {a['lat']} BETWEEN ? AND ? AND {a['lon']} BETWEEN ? AND ?",
            (k[0], at[0] - dlat, at[0] + dlat, at[1] - dlon, at[1] + dlon)).fetchall()
        best = None
        for hn, la, lo in rows:
            d = haversine_km(at[0], at[1], la, lo) * 1000.0
            if d > within or not hn:
                continue
            key = (round(d, 1), str(hn))
            if best is None or key < best:
                best = key
        return best[1] if best else None

    def bbox_of(self, eid):
        return self.db.execute("SELECT minlat, maxlat, minlon, maxlon FROM entry_rtree WHERE id = ?", (eid,)).fetchone()

    def context_of(self, ids):
        out = {}
        ids = list(ids)
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            q = f"SELECT entry_id, place_id, weight FROM context WHERE entry_id IN ({','.join('?' * len(chunk))})"
            for eid, pid, w in self.db.execute(q, chunk):
                out.setdefault(eid, {})[pid] = w
        return out

    # ------------------------------------------------------------------ scoring helpers
    @staticmethod
    def name_tier(q: str, row) -> str | None:
        if not q:
            return None
        keys = [row[R_KEY]] + ((row[R_ALT] or "").split("|") if row[R_ALT] else [])
        qt = q.split()
        order = ["exact", "exact_alt", "prefix", "word_prefix", "contains"]
        best = None
        for key in keys:
            if not key:
                continue
            alt_dir = key.startswith("~")
            if alt_dir:
                key = key[1:]
            if key == q:
                if not alt_dir:
                    return "exact"
                best = "exact_alt"
                continue
            t = None
            if key.startswith(q):
                t = "prefix"
            else:
                kt = key.split()
                j = 0
                for tok in kt:
                    if j < len(qt) and tok.startswith(qt[j]):
                        j += 1
                if j == len(qt):
                    t = "word_prefix"
                elif all(any(k.startswith(x) for k in kt) for x in qt):
                    t = "contains"
            if t and (best is None or order.index(t) < order.index(best)):
                best = t
        return best

    @staticmethod
    def fuzzy_tier(options: list[list[str]], row) -> str | None:
        """Tier of a row against per-position alternatives (typo-corrected words)."""
        keys = [row[R_KEY]] + ((row[R_ALT] or "").split("|") if row[R_ALT] else [])
        best = None
        order = ["exact", "prefix", "word_prefix"]
        for key in keys:
            kt = key.lstrip("~").split() if key else []
            if not kt:
                continue
            if len(kt) == len(options) and all(k in o for k, o in zip(kt, options)):
                return "exact"
            if len(kt) >= len(options) and all(k in o for k, o in zip(kt[:len(options) - 1], options)) \
                    and any(kt[len(options) - 1].startswith(a) for a in options[-1]):
                t = "prefix"
            else:
                j = 0
                for tok in kt:
                    if j < len(options) and any(tok.startswith(a) for a in options[j]):
                        j += 1
                t = "word_prefix" if j == len(options) else None
            if t and (best is None or order.index(t) < order.index(best)):
                best = t
        return best

    # ------------------------------------------------------------------ main entry
    def search(self, text: str, lat: float | None = None, lon: float | None = None, limit: int | None = None,
               debug: bool = False) -> dict:
        t0 = time.perf_counter()
        rk, W, tiers, S = self.rk, self.rk["weights"], self.rk["tiers"], self.rk["search"]
        text = text[:S["max_query_chars"]]   # rule S5: the input cap, in code points, before anything else
        limit = limit or S["top_n"]
        user = valid_position(lat, lon)
        position_ignored = user is None and (lat is not None or lon is not None)
        out_cov = self.out_of_coverage(user)
        p = self.parse(text)
        partial = p["partial"]
        labels: list[dict] = []
        cands: dict[int, Candidate] = {}

        def add(row, score, how, tier=None):
            c = cands.get(row[R_ID])
            if c is None or score > c.score:
                cands[row[R_ID]] = Candidate(row, score, how, tier)

        def decay(row, anchor, scale):
            if not anchor:
                return 0.0
            return 1.0 / (1.0 + haversine_km(anchor[0], anchor[1], row[R_LAT], row[R_LON]) / scale)

        # ---------- 1. explicit connector: 'gas near pittsburgh', 'walmart in erie', 'cvs on forbes ave'
        ctx_places, street_rows = [], []
        head = p["head"]
        conn = p["conn"]
        if conn in ("near", "in"):
            tail = Phrase(p["tail"], partial, self.stop)
            ctx_places = self.place_lookup(tail.key, user, allow_prefix=partial)
            if not ctx_places:
                fixed = [self._best_fix(t) for t in tail.exp]
                if fixed != tail.exp:
                    ctx_places = self.place_lookup(" ".join(fixed), user)
            if ctx_places:
                labels.append({"text": " ".join(p["tail"]), "as": "place", "match": ctx_places[0][R_NAME]})
        elif conn == "on":
            tail = Phrase(p["tail"], partial, self.stop)
            rows = self._fts(self.fts_match([[t] for t in tail.exp], partial, tail.last_raw), user,
                             kinds=[KIND_STREET], limit=60)
            if not rows:
                fixed = [[self._best_fix(t)] for t in tail.exp]
                rows = self._fts(self.fts_match(fixed, partial, None), user, kinds=[KIND_STREET], limit=60)
            rows = [r for r in rows if self.name_tier(tail.key, r) in ("exact", "prefix", "word_prefix")
                    or self.name_tier(tail.key_raw, r) in ("exact", "prefix", "word_prefix")]
            if rows:
                # all portions of the best street name near the anchor (one long road is split
                # per municipality) plus two fallbacks
                first = rows[0][R_KEY]
                street_rows = [r for r in rows if r[R_KEY] == first][:8] + [r for r in rows if r[R_KEY] != first][:2]
                labels.append({"text": " ".join(p["tail"]), "as": "street", "match": first})
        if conn and not ctx_places and not street_rows:
            head, conn = p["tokens"], None          # 'walk in clinic': the connector was a plain word

        # ---------- 2. the head phrase, and a trailing place ('walmart erie', 'pizza 16509')
        whole = Phrase(head, partial, self.stop)
        whole_cats = self.category_lookup(whole.key)
        whole_brands = self.brand_lookup(whole.key)
        whole_known = bool(whole_cats or whole_brands) or self.name_exists(whole.key)
        variants = []   # (Phrase, bound_to_context)
        if conn:
            variants.append((whole, True))
        else:
            if len(head) >= 2 and not whole_known:
                for k in (3, 2, 1):
                    if len(head) - k < 1:
                        continue
                    tail = Phrase(head[-k:], False, self.stop)
                    rest = Phrase(head[:-k], partial and False, self.stop)
                    if len(tail.key) < 3:
                        continue
                    pl = self.place_lookup(tail.key, user, suffixes=False)
                    rest_typed = bool(self.category_lookup(rest.key) or self.brand_lookup(rest.key))
                    pl = [r for r in pl if r[R_TYPE] in S["context_place_types"] or rest_typed]
                    if pl:
                        ctx_places = pl
                        labels.append({"text": " ".join(head[-k:]), "as": "place", "match": pl[0][R_NAME]})
                        variants.append((Phrase(head[:-k], False, self.stop), True))
                        break
            variants.append((whole, False))
        anchor = (ctx_places[0][R_LAT], ctx_places[0][R_LON]) if ctx_places else user
        ctx_ids = {r[R_ID] for r in ctx_places[:3]}
        if p["near_me"]:
            labels.append({"text": "near me", "as": "near me"})
        if p["house"]:
            labels.append({"text": p["house"], "as": "house number"})

        # ---------- 3. typo candidates
        fuzzy_runs = []   # (options per position, bound, forced)
        for ph, bound in variants:
            if not ph:
                continue
            typed_class = bool(self.category_lookup(ph.key) or self.brand_lookup(ph.key))
            opts, changed, forced = [], False, False
            for i, t in enumerate(ph.exp):
                last = i == len(ph.exp) - 1
                if t in self.no_fix:
                    opts.append([t])
                    continue
                f = self.vocab_freq(t)
                if last and ph.partial and f == 0 and (
                        self.vocab_freq(t, prefix=True) or self.vocab_freq(ph.last_raw, prefix=True)):
                    opts.append([t])      # an unfinished word that starts a known word
                    continue
                corr = self.corrections(t)
                if f == 0 and corr:
                    opts.append([c[0] for c in corr])
                    changed = True
                elif f > 0 and corr and not typed_class:
                    # a real but rare word one edit away from a far more common one:
                    # 'sheets' -> 'sheetz', 'resturant' -> 'restaurant'
                    dom = [c for c in corr if c[1] == 1 and c[2] >= max(S["dominance_min_freq"], S["dominance_ratio"] * f)]
                    if dom:
                        opts.append([c[0] for c in dom] + [t])
                        changed = forced = True
                    else:
                        opts.append([t])
                else:
                    opts.append([t])
            if changed:
                fuzzy_runs.append((opts, bound, forced))

        # ---------- 4. candidates per interpretation
        def ctx_bonus(rows):
            if not ctx_ids:
                return {}
            cm = self.context_of([r[R_ID] for r in rows])
            out = {}
            for r in rows:
                if r[R_MUNI] in ctx_ids or r[R_COUNTY] in ctx_ids or r[R_ID] in ctx_ids:
                    out[r[R_ID]] = 1.0
                else:
                    out[r[R_ID]] = max((w for pid, w in cm.get(r[R_ID], {}).items() if pid in ctx_ids), default=0.0)
            return out

        def street_bonus(rows):
            if not street_rows:
                return {}
            skeys = {s_[R_KEY] for s_ in street_rows}
            ids = [r[R_ID] for r in rows]
            out = {}
            for i in range(0, len(ids), 900):
                chunk = ids[i:i + 900]
                q = f"SELECT entry_id, addr_street FROM poi WHERE entry_id IN ({','.join('?' * len(chunk))})"
                for eid, ast in self.db.execute(q, chunk):
                    out[eid] = 1.0 if ast and normalize.street_key(ast) in skeys else 0.0
            return out

        def along_street(cat_ids=None, brand_ids=None):
            rows = []
            m = S["street_poi_margin_km"] / 111.0
            for s_ in street_rows:
                bb = self.bbox_of(s_[R_ID])
                rows += self.in_bbox((bb[0] - m, bb[1] + m, bb[2] - m * 1.3, bb[3] + m * 1.3),
                                     cat_ids=cat_ids, brand_ids=brand_ids)
            return rows

        def run(options: list[list[str]], ph: Phrase, fuzzy: bool, bound: bool):
            best = [o[0] for o in options]
            key = " ".join(best)
            key_raw = ph.key_raw if not fuzzy else key
            v_anchor = anchor if (bound or not ctx_places) else user
            use_ctx = bound and bool(ctx_ids)
            prefix_ok = ph.partial and not fuzzy
            cats = self.category_lookup(key, key_raw if prefix_ok else None)
            brands = self.brand_lookup(key, key_raw if prefix_ok else None)
            fz = S["fuzzy_factor"] if fuzzy else 1.0
            browse_scale = rk["distance"]["browse_scale_km"] if v_anchor == user else rk["distance"]["context_browse_scale_km"]
            cat_rows = 0
            for kind, hit in (("category", cats), ("brand", brands)):
                if not hit:
                    continue
                lab = {"text": " ".join(best), "as": kind, "fuzzy": fuzzy}
                if kind == "category":
                    lab["match"] = [self.cat_code[c] for c in hit]
                    rows = along_street(cat_ids=hit) if street_rows else self.browse_category(hit, v_anchor)
                    cat_rows += len(rows)
                else:
                    lab["match"] = [self._brand_name(b) for b in list(hit)[:3]]
                    rows = along_street(brand_ids=hit) if street_rows else self.browse_brand(hit, v_anchor)
                labels.append(lab)
                cb = ctx_bonus(rows) if use_ctx else {}
                sb = street_bonus(rows)
                for r in rows:
                    if kind == "category":
                        w = hit.get(r[13], max(hit.values())) if len(r) > 13 else max(hit.values())
                        base = W["category_hit"] * w
                    else:
                        base = W["brand_hit"] * hit.get(r[R_BRAND], max(hit.values()))
                    sc = base * fz + W["distance"] * decay(r, v_anchor or user, browse_scale) \
                        + W["browse_importance"] * r[R_IMP] + W["context"] * cb.get(r[R_ID], 0.0) \
                        + W["street_address"] * sb.get(r[R_ID], 0.0)
                    add(r, sc, kind, kind)
            if street_rows:
                return
            # plain name search; a phrase that is a category/brand word demotes name matches
            exact_class = any(v >= 1.0 for v in cats.values()) or any(v >= 1.0 for v in brands.values())
            if out_cov and not cat_rows and any(v >= 1.0 for v in cats.values()):
                return      # 'gas' far outside the pack: nothing nearby, and name matches would be junk
            name_scale = W["name_when_category_or_brand"] if exact_class else (
                W["name_when_category_or_brand_prefix"] if (cats or brands) else 1.0)
            if not fuzzy and ph.partial and len(best) == 1 and len(key_raw) <= S["short_prefix_chars"]:
                rows = self.short_prefix_candidates(key_raw, v_anchor)
            else:
                rows = self._fts(self.fts_match(options, ph.partial and not fuzzy, ph.last_raw if not fuzzy else None),
                                 v_anchor)
            cb = ctx_bonus(rows) if use_ctx else {}
            cat_codes = {self.cat_code[c] for c in cats}
            generic = {t for (t,) in self.db.execute(
                f"SELECT term FROM category_term WHERE cat_id IN ({','.join(str(int(c)) for c in cats)})")} if cats else set()
            for r in rows:
                if r[R_KIND] == KIND_POI and (r[R_TYPE] in cat_codes or r[R_KEY] in generic):
                    # a POI of the category the query names, or one whose whole name is a word of
                    # that category ('Rest Area' for 'rest ar'), ranks as a category hit (by
                    # distance), not by its generic name from 250 km away
                    continue
                tier = self.fuzzy_tier(options, r) if fuzzy else self.name_tier(key, r)
                if prefix_ok and key_raw != key:
                    t2 = self.name_tier(key_raw, r)
                    order = ["exact", "exact_alt", "prefix", "word_prefix", "contains", None]
                    if order.index(t2) < order.index(tier):
                        tier = t2
                if tier is None:
                    continue
                if fuzzy:
                    tier = {"exact": "fuzzy", "prefix": "fuzzy_prefix"}.get(tier, "fuzzy_word")
                imp_w = W["importance"] + (W["place_importance"] if r[R_KIND] == KIND_PLACE else 0.0)
                sc = W["name"] * tiers[tier] + imp_w * r[R_IMP] \
                    + W["distance"] * decay(r, v_anchor, rk["distance"]["scale_km"]) \
                    + W["context"] * cb.get(r[R_ID], 0.0) + rk["kind_bonus"][KIND_NAME[r[R_KIND]]]
                add(r, sc * name_scale, "name", tier)

        for ph, bound in variants:
            if ph:
                run([[t] for t in ph.exp], ph, False, bound)
        if fuzzy_runs:
            strong = sum(1 for c in cands.values() if c.tier in ("exact", "prefix", "category", "brand"))
            for opts, bound, forced in fuzzy_runs:
                if forced or strong < S["fuzzy_if_strong_below"]:
                    ph = Phrase([o[0] for o in opts], False, self.stop)
                    run(opts, ph, True, bound)
                    labels.append({"text": " ".join(o[0] for o in opts), "as": "corrected"})
        if not head and ctx_places:       # only a place: 'in erie'
            for r in ctx_places[:3]:
                add(r, 2.0, "place", "exact")
        if not cands and len(whole.exp) >= 2 and not (out_cov and whole_cats):
            # last resort: entries matching any one word ('cracker barell' -> 'Cracker Barrel')
            words = [t for t in whole.exp if len(t) >= 3]
            if words:
                rows = self._fts(" OR ".join(fts_quote(t) + "*" for t in words), anchor)
                for r in rows:
                    kt = (r[R_KEY] or "").split()
                    hits = sum(1 for t in words if any(k.startswith(t) for k in kt))
                    if hits:
                        sc = W["name"] * tiers["any_word"] * hits / len(words) + W["importance"] * r[R_IMP] \
                            + W["distance"] * decay(r, anchor, rk["distance"]["scale_km"])
                        add(r, sc, "name", "any_word")

        # ---------- 5. results
        ranked = sorted(cands.values(), key=lambda c: -c.score)[: limit * 4]
        houses = self._houses(p["house"], ranked) if p["house"] else {}
        if houses:
            # an exact address (a located house number) goes first: house_located on top of its
            # score, times the distance decay of the house from the anchor (1 without one), and
            # only in the named place's municipality or county when the query names one; so a
            # house in another town or 400 km away does not jump a nearby street. Then the order
            # again (stable: equal scores keep their order). Scaled by the street name's tier value,
            # so a house on a street that only shares a word with the query ("247 Main Street" ->
            # 247 East 6th Street, any_word) does not jump a street whose name was typed; and none
            # at all for such a street (any_word: the typed street name is not in its name), which
            # also holds when the named town is not in this pack (no context to gate by).
            def hbonus(c):
                row = c.row
                if c.tier == "any_word":
                    return 0.0
                if ctx_ids and row[R_MUNI] not in ctx_ids and row[R_COUNTY] not in ctx_ids:
                    return 0.0
                pt = houses[row[R_ID]]
                d = 1.0 if not anchor else 1.0 / (1.0 + haversine_km(anchor[0], anchor[1], pt[0], pt[1])
                                                  / rk["distance"]["scale_km"])
                return W["house_located"] * tiers.get(c.tier, 0.0) * d
            ranked = sorted((Candidate(c.row, c.score + hbonus(c), c.how, c.tier) if c.row[R_ID] in houses else c
                             for c in ranked), key=lambda c: -c.score)
        results, shown = [], []
        if not p["house"]:
            # two streets and the word between them: where they meet goes first (step 7)
            results = self.crossings(text, user)[:limit]
            if results:
                labels.append({"text": text.strip(), "as": "crossing", "match": results[0]["title"]})
        for c in ranked:
            if len(results) >= limit:
                break
            r = c.row
            title, rlat, rlon = r[R_NAME], r[R_LAT], r[R_LON]
            hit = houses.get(r[R_ID])
            if hit:
                title, rlat, rlon = f"{p['house']} {r[R_NAME]}", hit[0], hit[1]
            elif r[R_KIND] == KIND_STREET and user and self.has_lines:
                # a street is where its line comes nearest to the user, not at its middle (step 7). The
                # nearest point is found on a flat plane around the user; it is taken only when it is
                # nearer than the street's own point as every other distance measures (hundreds of km
                # away the plane bends)
                nb = lines.nearest(self.street_lines(r[R_ID]), user[0], user[1])
                if nb is not None:
                    nlat, nlon = round(nb[1], 6), round(nb[2], 6)
                    if haversine_km(user[0], user[1], nlat, nlon) < haversine_km(user[0], user[1], rlat, rlon):
                        rlat, rlon = nlat, nlon
            if any(s[0] == title and haversine_km(s[1], s[2], rlat, rlon) < S["dedupe_km"] for s in shown):
                continue
            shown.append((title, rlat, rlon))
            # distance from the point actually returned (the house point when one was located)
            d = haversine_km(user[0], user[1], rlat, rlon) if user else None
            kind = KIND_NAME[r[R_KIND]]
            if kind == "poi":
                cat = self._poi_label(r[R_TYPE])
            elif kind == "place":
                cat = {"zip": "ZIP code"}.get(r[R_TYPE], self._us((r[R_TYPE] or "place").capitalize()))
            else:
                cat = "Street"
            sub = r[R_SUB] or ""
            if kind == "place" and r[R_TYPE]:
                # a pack built before the US spellings says "Neighbourhood in Erie": its leading type
                # label only (names such as "Centre County" stay as they are)
                raw = r[R_TYPE].capitalize()
                if raw != cat and sub.startswith(raw) and sub[len(raw):len(raw) + 1] in ("", " ", ","):
                    sub = cat + sub[len(raw):]
            res = {"id": r[R_ID], "kind": kind, "title": title, "subtitle": sub, "category": cat,
                   "type": r[R_TYPE], "lat": rlat, "lon": rlon,
                   "distance_km": round(d, 2) if d is not None else None, "brand_id": r[R_BRAND],
                   "score": round(c.score, 4), "how": c.how, "tier": c.tier}
            if p["house"] and kind == "street":
                res["house_number"] = p["house"]
                res["street_name"] = r[R_NAME]
                res["house_located"] = bool(hit)
            results.append(res)
            if len(results) >= limit:
                break
        out = {"query": text, "parse": labels, "results": results, "ms": round((time.perf_counter() - t0) * 1000, 2),
               "out_of_coverage": out_cov, "position_ignored": position_ignored}
        if debug:
            out["parsed"] = p
        return out

    # ------------------------------------------------------------------ helpers
    def _best_fix(self, tok: str) -> str:
        if self.vocab_freq(tok):
            return tok
        c = self.corrections(tok)
        return c[0][0] if c else tok

    def _brand_name(self, bid):
        r = self.db.execute("SELECT name FROM brand WHERE id = ?", (bid,)).fetchone()
        return r[0] if r else str(bid)

    def _poi_label(self, t: str | None):
        """Category label of a POI; for POIs in no category, the value of their main
        OSM tag ('shop=hairdresser' -> 'Hairdresser', 'office=financial' -> 'Financial office')."""
        if not t:
            return None
        if "=" in t:
            k, v = t.split("=", 1)
            return self._us(v.replace("_", " ").capitalize()) + self.rk.get("raw_type_label_suffix", {}).get(k, "")
        return self.cat_label.get(self.cat_id_by_code.get(t))

    def _us(self, label: str) -> str:
        """A type label from OSM tag values in US spelling ('Sports centre' -> 'Sports center',
        'Neighbourhood' -> 'Neighborhood'): each word through ranking.json us_spellings, the
        first letter's case kept."""
        table = self.rk.get("us_spellings", {})
        out = []
        for w in label.split(" "):
            u = table.get(w.lower()) if not w.startswith("_") else None
            out.append(w if u is None else (u[:1].upper() + u[1:] if w[:1].isupper() else u))
        return " ".join(out)

    def out_of_coverage(self, user) -> bool | None:
        """True when the position lies more than coverage_margin_km outside the pack's
        bounding box; None when unknown (no position or no bbox in meta)."""
        if not user or not self.bbox:
            return None
        minlat, maxlat, minlon, maxlon = self.bbox
        cl = (min(max(user[0], minlat), maxlat), min(max(user[1], minlon), maxlon))
        return haversine_km(user[0], user[1], cl[0], cl[1]) > self.rk["search"]["coverage_margin_km"]

    def _houses(self, number: str, ranked) -> dict:
        """House number -> point for the street candidates, through the addresses
        track's database (join point). One lookup per distinct street key; an address
        point is offered to every street whose box (+ margin) holds it and is given to
        the street that holds it best (inside the box first, then rank order).
        Returns {street entry id: (lat, lon)}."""
        if not self.addr_ok:
            return {}
        a = self.rk["addresses"]
        m = a["house_margin_deg"]
        cache, pick, owner = {}, {}, {}
        for c in ranked:
            r = c.row
            if r[R_KIND] != KIND_STREET:
                continue
            k = self.db.execute("SELECT addr_key FROM street WHERE entry_id = ?", (r[R_ID],)).fetchone()
            if not k or not k[0]:
                continue
            ck = (k[0], number)
            if ck not in cache:
                cache[ck] = self.db.execute(
                    f"SELECT {a['lat']}, {a['lon']} FROM addr.{a['table']} "
                    f"WHERE {a['street_key']} = ? AND {a['housenumber']} = ?", ck).fetchall()
            if not cache[ck]:
                continue
            bb = self.bbox_of(r[R_ID])
            best = None
            for la, lo in cache[ck]:
                dlat = max(bb[0] - la, 0.0, la - bb[1])
                dlon = max(bb[2] - lo, 0.0, lo - bb[3])
                if dlat <= m and dlon <= m * 1.3:
                    cand = (dlat + dlon, haversine_km(r[R_LAT], r[R_LON], la, lo), la, lo)
                    best = cand if best is None or cand < best else best
            if best:
                pt = (best[2], best[3])
                pick[r[R_ID]] = pt
                if pt not in owner or best[0] < owner[pt][0]:
                    owner[pt] = (best[0], r[R_ID])
        return {eid: pt for eid, pt in pick.items() if owner[pt][1] == eid}
