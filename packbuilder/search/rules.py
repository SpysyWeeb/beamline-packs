"""Category rules from categories.json (build time) and the category term list
that is copied into the search database (query time)."""
from __future__ import annotations

import json
import os
import re

import normalize

HERE = os.path.dirname(os.path.abspath(__file__))


def _compile_test(key: str, val):
    neg = key.startswith("!")
    if neg:
        key = key[1:]
    if key.endswith("~"):
        key = key[:-1]
        pat = re.compile(val, re.I)

        def t(tags, k=key, p=pat):
            v = tags.get(k)
            return v is not None and p.search(v) is not None
    elif val == "*":
        def t(tags, k=key):
            v = tags.get(k)
            return v is not None and v != "no"
    elif isinstance(val, list):
        s = frozenset(val)

        def t(tags, k=key, s=s):
            return tags.get(k) in s
    else:
        def t(tags, k=key, v=val):
            return tags.get(k) == v
    return neg, t


class RuleBook:
    def __init__(self, path: str | None = None, idterms_path: str | None = None) -> None:
        path = path or os.path.join(HERE, "categories.json")
        idterms_path = idterms_path or os.path.join(HERE, "categories_idterms.json")
        with open(path, encoding="utf-8") as f:
            self.doc = json.load(f)
        self.idterms = {}
        if os.path.exists(idterms_path):
            with open(idterms_path, encoding="utf-8") as f:
                self.idterms = json.load(f)["id_terms"]
        self.cats = self.doc["categories"]
        self.by_id = {c["id"]: c for c in self.cats}
        self._rules = []  # (cat_id, [(neg, test), ...])
        for c in self.cats:
            for r in c["match"]:
                self._rules.append((c["id"], [_compile_test(k, v) for k, v in r.items()]))
        self.order = {c["id"]: i for i, c in enumerate(self.cats)}

    def categories_for(self, tags: dict) -> list[str]:
        out: list[str] = []
        for cid, tests in self._rules:
            if cid in out:
                continue
            ok = True
            for neg, t in tests:
                if t(tags) == neg:
                    ok = False
                    break
            if ok:
                out.append(cid)
        out.sort(key=self.order.__getitem__)
        return out

    def tag_values(self, key: str) -> frozenset[str]:
        """Every value the match rules ask of `key` ("natural": {"beach", "peak", ...}): the
        extract keeps natural=* and waterway=* objects only for these values."""
        out: set[str] = set()
        for c in self.cats:
            for r in c["match"]:
                v = r.get(key)
                if isinstance(v, str) and v != "*":
                    out.add(v)
                elif isinstance(v, list):
                    out.update(v)
        return frozenset(out)

    def in_park(self, cid: str | None) -> bool:
        """True for categories marked in_park (named natural places: found as '<park> <label>')."""
        return bool(cid and self.by_id[cid].get("in_park"))

    def primary(self, cats: list[str]) -> str | None:
        for c in cats:
            if not self.by_id[c].get("umbrella") and not self.by_id[c].get("facet"):
                return c
        return cats[0] if cats else None

    def unnamed_ok(self, cid: str) -> bool:
        return bool(self.by_id[cid].get("unnamed"))

    def term_rows(self):
        """(term_key, category_id, weight, source) for the database.
        Own terms and the label weigh 1.0 (or the weight given as ["term", w]),
        iD-derived words 0.6."""
        seen = {}
        for c in self.cats:
            own = [c["label"]] + list(c.get("terms", []))
            for t in own:
                w = 1.0
                if isinstance(t, list):  # ["rest stop", 0.8]
                    t, w = t[0], float(t[1])
                k = normalize.key(t)
                if k:
                    seen[(k, c["id"])] = max(seen.get((k, c["id"]), 0.0), w)
            for t in self.idterms.get(c["id"], []):
                k = normalize.key(t)
                if k and (k, c["id"]) not in seen:
                    seen[(k, c["id"])] = 0.6
        return [(k, cid, w) for (k, cid), w in sorted(seen.items())]
