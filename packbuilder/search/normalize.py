"""Canonical search keys, shared by the index builder and the query engine.

The rules are deliberately small and table-driven (abbreviations.json) so the
same function can be re-implemented in Kotlin. Steps, in order:

1. Unicode NFKD, drop combining marks (diacritics), lowercase.
2. Delete apostrophes (' and U+2019 and `): "McDonald's" -> "mcdonalds".
3. "&" -> " and " (then "and" is a stopword).
4. Every other character that is not a-z or 0-9 becomes a space.
5. Split on spaces.
6. Expand abbreviations by position (see abbreviations.json).
7. Drop stopwords, unless that would leave nothing.

key("N. Main St.") == "north main street"
key("St. Marys") == "saint marys"
key("The Home Depot") == "home depot"

street_key() is the join key for the separate addresses track: it is key()
applied to the OSM addr:street / name value. That track must produce the
same string for the join to work (see README.md, "Join point").
"""
from __future__ import annotations

import json
import os
import re
import unicodedata

_HERE = os.path.dirname(os.path.abspath(__file__))
_TABLES = None
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def tables(path: str | None = None) -> dict:
    global _TABLES
    if _TABLES is None or path:
        with open(path or os.path.join(_HERE, "abbreviations.json"), encoding="utf-8") as f:
            _TABLES = json.load(f)
        _TABLES["_stop"] = set(_TABLES["stopwords"])
    return _TABLES


def fold(text: str) -> str:
    """Steps 1-4: lowercase ASCII-ish text with single spaces."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = t.replace("'", "").replace("’", "").replace("`", "")
    t = t.replace("&", " and ")
    t = _NON_ALNUM.sub(" ", t)
    return t.strip()


def expand_tokens(tokens: list[str]) -> list[str]:
    """Step 6: positional abbreviation expansion. Multi-word expansions
    (e.g. 'hs' -> 'high school') are split into separate tokens."""
    tb = tables()
    n = len(tokens)
    if n == 0:
        return tokens
    out: list[str] = []
    # index of the last token that is not a directional (for suffix rules)
    last_core = n - 1
    while last_core > 0 and tokens[last_core] in tb["directional"]:
        last_core -= 1
    for i, tok in enumerate(tokens):
        rep = None
        if i + 1 < n and tok in tb.get("route_prefix", {}) and tokens[i + 1].isdigit():
            out.extend(tb["route_prefix"][tok].split())
            continue
        if n > 1:
            if i == 0 and tok in tb["prefix"]:
                rep = tb["prefix"][tok]
            elif i == last_core and i > 0 and tok in tb["suffix"]:
                rep = tb["suffix"][tok]
            elif (i == 0 or i == n - 1) and tok in tb["directional"]:
                rep = tb["directional"][tok]
        if rep is None and tok in tb["anywhere"]:
            rep = tb["anywhere"][tok]
        if rep is None:
            out.append(tok)
        else:
            out.extend(rep.split())
    return out


def tokens(text: str, expand: bool = True, drop_stop: bool = True) -> list[str]:
    toks = fold(text).split()
    if expand:
        toks = expand_tokens(toks)
    if drop_stop:
        kept = [t for t in toks if t not in tables()["_stop"]]
        if kept:
            toks = kept
    return toks


def key(text: str) -> str:
    return " ".join(tokens(text))


def street_key(text: str) -> str:
    return key(text)


def fnv1a32(s: str) -> int:
    """FNV-1a 32-bit hash of the UTF-8 bytes, as a signed 32-bit integer.
    Keys of the spell_del table (typo correction); trivial to port to Kotlin."""
    h = 0x811C9DC5
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h - (1 << 32) if h >= (1 << 31) else h


def deletes1(word: str) -> set[str]:
    """The word and every string made by deleting one character."""
    return {word} | {word[:i] + word[i + 1:] for i in range(len(word))}


def squash(text: str) -> str:
    """Key without spaces: 'wilkes barre' -> 'wilkesbarre'. Used as an extra
    alias so a user who types names joined together still matches."""
    return key(text).replace(" ", "")


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        print(repr(arg), "->", repr(key(arg)))
