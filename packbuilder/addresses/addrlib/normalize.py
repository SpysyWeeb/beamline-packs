"""US street-name, house-number, place and ZIP normalisation.

Pure standard library (runs on the build PC, on a comma device, and is meant to
be ported to Kotlin as-is): every word list lives in us_street_norm.json, the
code only applies positional rules.

Street key rules (applied to the street part only, without house number/unit):
  1. lower-case, strip accents, '&' -> 'and', drop apostrophes and periods,
     every other non-alphanumeric character (hyphen, comma, slash) -> space.
  2. split glued highway tokens: 'i79' -> 'i 79', 'pa8' -> 'pa 8'.
  3. highway forms: '<system words> <number>[letter] [modifier]' becomes
     '<system> <number> [modifier]', e.g. 'State Route 8' and 'PA 8' -> 'sr 8',
     'US Hwy 19' -> 'us 19', 'I-79' -> 'i 79', 'Route 8' -> 'rte 8'.
  4. ordinal words -> numbers: 'Twenty First' -> '21st', '2nd' stays '2nd'.
  5. 'st' followed by a name word means Saint when it starts the name (after an
     optional directional) or follows a saint prefix word ('St Clair St' ->
     'saint clair st', 'Mount St Marys Rd' -> 'mount saint marys rd'); elsewhere
     it is the street type ('Main St Exn' -> 'main st ext').
  6. every directional, street-type and name-word spelling -> its canonical
     token, wherever it appears in the name.
The key is the canonical tokens joined by single spaces. It is a matching key,
not a display form. key_variants() gives the lookup's second-chance keys
(a misspelt street type, a bare number used as an ordinal).
"""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

DATA_FILE = Path(__file__).with_name("us_street_norm.json")

_NUM_RE = re.compile(r"^0*(\d+)([a-z]?)$")
_ORD_NUM_RE = re.compile(r"^0*(\d+)(st|nd|rd|th|d)$")
_GLUED_RE = re.compile(r"^([a-z]+)(\d+[a-z]?)$")
_ZIP_RE = re.compile(r"^(\d{5})(?:-?(\d{4}))?$")


class Tables:
    def __init__(self, path: Path = DATA_FILE):
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        self.raw = d
        self.dir = _invert(d["directionals"])
        self.weak_dirs = set(d["weak_directionals"])
        self.saint_prefixes = set(d["saint_prefix_words"])
        self.types = _invert(d["street_types"])
        self.type_canon = set(d["street_types"])
        self.strong_types = set(d["strong_street_types"])
        self.name_words = _invert({k: v for k, v in d["name_words"].items() if not k.startswith("_")})
        o = d["ordinals"]
        self.ord_units = o["units"]
        self.ord_teens = o["teens"]
        self.ord_tens = o["tens_ordinal"]
        self.card_tens = o["tens_cardinal"]
        h = d["highways"]
        self.hwy_prefixes = []  # (tokens tuple, system)
        for system, variants in h["systems"].items():
            for v in variants:
                self.hwy_prefixes.append((tuple(v.split()), system))
        self.state_prefixes = [tuple(s.split()) for s in h["state_prefixes"]]
        self.state_followers = [tuple(s.split()) for s in h["state_prefix_followers"]]
        self.hwy_mods = _invert(h["modifiers"])
        self.hwy_fallbacks = h["lookup_fallbacks"]
        self.glue_prefixes = {p[0] for p, _ in self.hwy_prefixes if len(p) == 1} | \
                             {p[0] for p in self.state_prefixes if len(p) == 1}
        self.units = {}
        self.unit_needs_id = {}
        for canon, spec in d["unit_designators"].items():
            for w in spec["words"]:
                self.units[w] = canon
            self.unit_needs_id[canon] = spec["needs_id"]
        # source data fields also accept 'no 4' / 'number 4' as '# 4'
        self.units_source = dict(self.units, **{w: "#" for w in d["unit_number_words_in_source_data"]})
        self.fractions = list(d["house_number_fractions"])
        self.hn_placeholder_suffixes = tuple(d["house_number_placeholder_suffixes"])
        self.city_placeholders = set(d["city_placeholders"])
        self.country_multi = sorted({tuple(x.split()) for x in d["country_words"]}, key=len, reverse=True)
        self.states = _invert(d["states"])
        self.state_multi = sorted({tuple(v.split()) for vs in d["states"].values() for v in vs},
                                  key=len, reverse=True)
        self.ambiguous_states = set(d["ambiguous_state_tokens"])
        self.place_words = _invert({k: v for k, v in d["place_words"].items() if not k.startswith("_")})
        self.place_drop_prefixes = [tuple(x.split()) for x in d["place_drop_prefixes"]]
        self.place_drop_suffixes = set(d["place_drop_suffixes"])


def _invert(m: dict) -> dict:
    out = {}
    for canon, variants in m.items():
        if canon.startswith("_"):
            continue
        for v in variants:
            out.setdefault(v, canon)
        out.setdefault(canon, canon)
    return out


@lru_cache(maxsize=1)
def tables() -> Tables:
    return Tables()


# ---------------------------------------------------------------- text basics

def ascii_fold(s: str) -> str:
    s = s.replace("½", " 1/2").replace("¼", " 1/4").replace("¾", " 3/4")
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def clean(s: str, keep: str = "") -> str:
    """Lower-case, fold accents, drop apostrophes/periods, other punctuation -> space."""
    s = ascii_fold(s or "").lower().replace("&", " and ")
    s = re.sub(r"['’`.]", "", s)
    s = re.sub(r"[^a-z0-9%s]+" % re.escape(keep), " ", s)
    return " ".join(s.split())


def tokens(s: str) -> list[str]:
    t = tables()
    out = []
    for tok in clean(s).split():
        m = _GLUED_RE.match(tok)
        if m and m.group(1) in t.glue_prefixes:
            out.extend([m.group(1), m.group(2)])
        else:
            out.append(tok)
    return out


def ordinal_suffix(n: int) -> str:
    if 10 <= n % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


# ---------------------------------------------------------------- highways

def _match_seq(toks: list[str], i: int, seq: tuple) -> bool:
    return tuple(toks[i:i + len(seq)]) == seq


def _route_number(tok: str):
    m = _NUM_RE.match(tok)
    if not m:
        return None
    return m.group(1) + m.group(2)


def find_highway(toks: list[str]):
    """Return (start, end, canonical_tokens) for the first highway form, else None."""
    t = tables()
    for i in range(len(toks)):
        best = None
        # '<state> [route|hwy|...] <n>'  -> sr n
        for sp in t.state_prefixes:
            if _match_seq(toks, i, sp):
                j = i + len(sp)
                fl_best = 0
                for fl in t.state_followers:
                    if _match_seq(toks, j, fl) and len(fl) > fl_best:
                        fl_best = len(fl)
                j += fl_best
                if j < len(toks) and _route_number(toks[j]):
                    if best is None or j - i > best[0]:
                        best = (j - i, "sr", j)
        for seq, system in t.hwy_prefixes:
            if _match_seq(toks, i, seq):
                j = i + len(seq)
                if j < len(toks) and _route_number(toks[j]):
                    if best is None or len(seq) > best[0]:
                        best = (len(seq), system, j)
        if best:
            _, system, j = best
            out = [system, _route_number(toks[j])]
            k = j + 1
            while k < len(toks) and toks[k] in t.hwy_mods:
                out.append(t.hwy_mods[toks[k]])
                k += 1
            return i, k, out
    return None


# ---------------------------------------------------------------- ordinals

def _ordinals(toks: list[str]) -> list[str]:
    t = tables()
    out = []
    i = 0
    while i < len(toks):
        w = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if w.isdigit() and nxt == ordinal_suffix(int(w)) and i + 2 < len(toks):
            out.append(f"{int(w)}{nxt}")  # '21 st street' -> '21st street'
            i += 2
            continue
        if w in t.card_tens and nxt in t.ord_units:
            n = t.card_tens[w] + t.ord_units[nxt]
            out.append(f"{n}{ordinal_suffix(n)}")
            i += 2
            continue
        n = t.ord_units.get(w) or t.ord_teens.get(w) or t.ord_tens.get(w)
        if n:
            out.append(f"{n}{ordinal_suffix(n)}")
        else:
            m = _ORD_NUM_RE.match(w)
            if m:
                n = int(m.group(1))
                out.append(f"{n}{ordinal_suffix(n)}")
            else:
                out.append(w)
        i += 1
    return out


# ---------------------------------------------------------------- street keys

def _saint_position(toks: list[str], i: int) -> bool:
    """True when an 'st' at position i may mean Saint: it starts the name, follows a leading
    directional, or follows a saint prefix word ('mount st marys')."""
    t = tables()
    return i == 0 or (i == 1 and toks[0] in t.dir) or toks[i - 1] in t.saint_prefixes


def _canon_words(toks: list[str]) -> list[str]:
    t = tables()
    out = []
    n = len(toks)
    for i, w in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < n else None
        if w in ("st", "saint", "snt") and nxt is not None and nxt not in t.types \
                and nxt not in t.dir and not nxt[0].isdigit() and (w != "st" or _saint_position(toks, i)):
            out.append("saint")
            continue
        if w in t.weak_dirs:
            out.append(t.dir[w] if (i == 0 and n > 1) else w)
            continue
        if w in t.dir:
            out.append(t.dir[w])
        elif w in t.types:
            out.append(t.types[w])
        elif w in t.name_words:
            out.extend(t.name_words[w].split())
        else:
            out.append(w)
    return out


def street_key(name: str) -> str:
    toks = tokens(name)
    if not toks:
        return ""
    hw = find_highway(toks)
    if hw:
        s, e, canon = hw
        before = _canon_words(_ordinals(toks[:s]))
        after = _canon_words(_ordinals(toks[e:]))
        return " ".join(before + canon + after)
    return " ".join(_canon_words(_ordinals(toks)))


def street_core(key: str) -> str:
    """Key without leading/trailing directionals and trailing street type (fallback matching)."""
    t = tables()
    toks = key.split()
    if len(toks) > 1 and toks[0] in t.dir:
        toks = toks[1:]
    if len(toks) > 1 and toks[-1] in t.dir:
        toks = toks[:-1]
    if len(toks) > 1 and toks[-1] in t.type_canon and toks[-2] not in t.hwy_mods:
        toks = toks[:-1]
    return " ".join(toks)


def highway_alternatives(key: str) -> list[str]:
    """Other keys a highway key may be stored under ('rte 8' <-> 'sr 8', 'us 8', ...)."""
    t = tables()
    toks = key.split()
    for i, w in enumerate(toks):
        if w in t.hwy_fallbacks and i + 1 < len(toks) and _NUM_RE.match(toks[i + 1]):
            return [" ".join(toks[:i] + [alt] + toks[i + 1:]) for alt in t.hwy_fallbacks[w]]
    return []


def is_highway_key(key: str) -> bool:
    return bool(highway_alternatives(key))


def osa_distance(a: str, b: str, limit: int) -> int:
    """Optimal string alignment distance (Damerau-Levenshtein without repeated edits); > limit -> limit+1."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2 = None
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        best = cur[0]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            best = min(best, v)
        if best > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def fix_type_typo(word: str):
    """Canonical street type for a misspelt type word ('sreet', 'aveune', 'cicrle' -> 'st', 'ave', 'cir'),
    else None. One edit (OSA) against the full type words only (the first spelling of each type in the
    table: 'street', 'avenue'), because comparing with rare abbreviations misfires ('main' is one edit
    from 'mtin'). Words under 4 letters are left alone; a tie between types is broken in favour of a
    strong type, else no answer."""
    t = tables()
    if len(word) < 4 or word in t.types or not word.isalpha():
        return None
    hits = {canon for canon, spellings in t.raw["street_types"].items()
            if len(spellings[0]) >= 4 and osa_distance(word, spellings[0], 1) <= 1}
    if len(hits) > 1:
        hits = hits & t.strong_types
    return next(iter(hits)) if len(hits) == 1 else None


def key_variants(key: str) -> list[str]:
    """Second-chance keys for the lookup, tried only after the key itself found nothing:
    misspelt street-type words, wherever they are ('state sreet' -> 'state st', and type words used
    in the name: 'bracnh ave' -> 'br ave', 'valely vw dr' -> 'vly vw dr'), and a bare number before a
    street type read as an ordinal ('w 8 st' -> 'w 8th st'). Highway numbers ('sr 8') are left alone.
    Ordered from the smallest change: the street-type position (last word before a trailing
    directional) first, then one other word at a time, then the ordinal form, then all fixes together.
    The lookup uses the first variant that finds a street ('state sreet' must not become 'est sreet',
    'state' being one edit from 'estate')."""
    t = tables()
    toks = key.split()
    fixes = {i: fix_type_typo(w) for i, w in enumerate(toks)}
    fixes = {i: f for i, f in fixes.items() if f}
    j = len(toks) - 1
    while j > 0 and toks[j] in t.dir:
        j -= 1
    order = ([j] if j in fixes else []) + [i for i in sorted(fixes) if i != j]
    out = [" ".join(toks[:i] + [fixes[i]] + toks[i + 1:]) for i in order]
    fixed = [fixes.get(i, w) for i, w in enumerate(toks)]
    ords = list(fixed)
    for i, w in enumerate(fixed):
        if w.isdigit() and i + 1 < len(fixed) and fixed[i + 1] in t.type_canon \
                and not (i > 0 and fixed[i - 1] in t.hwy_fallbacks):
            n = int(w)
            ords[i] = f"{n}{ordinal_suffix(n)}"
    out += [" ".join(ords), " ".join(fixed)]
    return [k for k in dict.fromkeys(out) if k != key]


# ---------------------------------------------------------------- house numbers

@lru_cache(maxsize=1)
def _frac_re():
    alts = "|".join(re.escape(f) for f in tables().fractions)
    return re.compile(r"^(\d+)\s*([a-z]?)\s*(%s)$" % alts)


def house_number(hn: str):
    """Canonical house number text and its integer part.

    '123 A' -> ('123a', 123); '123-125' -> ('123-125', 123); '123 1/2' -> ('123 1/2', 123)
    Returns (None, None) when there is no leading number.
    """
    s = ascii_fold(hn or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*-\s*", "-", s)
    m = _frac_re().match(s)
    if m:
        return f"{int(m.group(1))}{m.group(2)} {m.group(3)}", int(m.group(1))
    m = re.match(r"^0*(\d+)(?:-?\s?([a-z]))?$", s)
    if m:
        return f"{int(m.group(1))}{m.group(2) or ''}", int(m.group(1))
    m = re.match(r"^0*(\d+)-0*(\d+)([a-z]?)$", s)
    if m:
        return f"{int(m.group(1))}-{int(m.group(2))}{m.group(3)}", int(m.group(1))
    m = re.match(r"^0*(\d+)", s)
    if m:
        return re.sub(r"[^a-z0-9/-]", "", s), int(m.group(1))
    return None, None


def split_housenumbers(raw: str) -> list[str]:
    """OSM addr:housenumber may hold several numbers: '12;14', '12, 14'."""
    parts = [p.strip() for p in re.split(r"[;,]", raw or "") if p.strip()]
    return parts


def is_placeholder_housenumber(hn: str) -> bool:
    """'700blk', '2800 block': a block placeholder rather than an address."""
    return clean(hn or "").replace(" ", "").endswith(tables().hn_placeholder_suffixes)


def source_city(c: str | None, state_code: str | None = None) -> str | None:
    """A city value from source data, or None when it is a placeholder ('Not stated'), only digits
    ('19428'), or empty. A trailing state ('Franklin, PA') is dropped."""
    t = tables()
    if not c:
        return None
    c = c.strip()
    words = clean(c).split()
    if not words or " ".join(words) in t.city_placeholders or all(w.isdigit() for w in words):
        return None
    if state_code and len(words) > 1 and words[-1] == state_code.lower():  # 'Franklin, PA' (not 'Mount Penn')
        c = re.sub(r"[\s,]+\S+$", "", c).strip()
    return c or None


# ---------------------------------------------------------------- places, ZIP, units

def place_key(name: str) -> str:
    """'City of Erie' -> 'erie', 'Millcreek Township' -> 'millcreek', 'Mt. Lebanon' -> 'mount lebanon'."""
    t = tables()
    toks = [t.place_words.get(w, w) for w in clean(name).split()]
    for pre in t.place_drop_prefixes:
        if len(toks) > len(pre) and tuple(toks[:len(pre)]) == pre:
            toks = toks[len(pre):]
            break
    if len(toks) > 1 and toks[-1] in t.place_drop_suffixes:
        toks = toks[:-1]
    return " ".join(toks)


def zip_parts(s: str):
    m = _ZIP_RE.match((s or "").strip())
    if not m:
        return None, None
    return m.group(1), m.group(2)


def unit_text(designator: str | None, ident: str | None) -> str | None:
    t = tables()
    d = t.units_source.get(clean(designator or "", keep="#"), None) if designator else None
    i = clean(ident or "").replace(" ", "")
    if not d and not i:
        return None
    if d == "#" or (d is None and i):
        return f"# {i}" if i else None
    return f"{d} {i}".strip()


def state_code(s: str) -> str | None:
    return tables().states.get(clean(s))
