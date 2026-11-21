"""Rule-based parser for typed US addresses.

    parse("123 n main st apt 4, erie pa 16501")
    -> {'house_number': '123', 'street': 'n main st', 'street_key': 'n main st',
        'unit': 'apt 4', 'city': 'erie', 'state': 'pa', 'zip': '16501', ...}

Tolerates missing parts (no ZIP, no city, no state, no number), commas or no
commas, and the locality written first ("erie pa 16501, 123 main st").
Pure standard library; all word lists come from us_street_norm.json. The only
things in code are the shapes of numbers (house number, ZIP, unit identifier)
and the order of the rules below.

Grammar, in the order it is applied:
  1. tokenise; remember which comma-separated part and which typed word each
     token came from ('a-1' is one word split into 'a', '1'). A trailing
     country ('usa', 'united states') is dropped.
  2. ZIP: a 5-digit or ZIP+4 token that ends a part, ends the input or follows
     a state; never the first token (a leading number is a house number), except
     when it is the whole input. A 3-4 or 6-8 digit number right after a final
     state is a mistyped ZIP: kept in 'zip_invalid', not used.
  3. state: a state name/abbreviation (multi-word allowed) that ends a part,
     ends the input or comes right before the ZIP; ambiguous tokens such as
     'pa', 'in', 'oh' are accepted only in those positions.
  4. house number: first token of the street block (digits with optional
     letter, '123-125', '123 1/2'; '123 A Main St' -> '123a').
  5. units, after at least one street word: designator + identifier ('apt 4',
     'apt #4b', 'unit a-1', 'ste 200', '# 4', 'fl 2' from '2nd floor'), several
     in a row ('fl 3 rm 4'). Designators without identifier ('rear', 'upper')
     right after a strong street type, after another unit or alone in a comma
     part, but not when they start a known place ('Lower Burrell') or a strong
     street type follows in the same part ('Trail Side Court').
  6. no leading house number: a number right after a street type or a route
     number is the house number ('State St 626 Erie'), else a final number.
  7. no house number, no unit and the whole rest is a known place: it is the
     city, not a street ('Erie PA 16501').
  8. street / city split: a comma boundary if there is one; else the end of
     the unit or trailing house number; else the longest run of trailing tokens
     that is a known place (gazetteer passed in by the lookup); else the street
     ends at the last highway number or strong street type (plus a following
     directional) and the rest is the city.
"""
from __future__ import annotations

import re

from .normalize import ascii_fold, find_highway, house_number, place_key, street_key, tables

_HN_RE = re.compile(r"^\d+[a-z]?$|^\d+-\d+[a-z]?$|^\d+-[a-z]$")
_ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$|^\d{9}$")
_BAD_ZIP_RE = re.compile(r"^\d{3,4}$|^\d{6,8}$")
_UNIT_ID_RE = re.compile(r"^[a-z0-9]{1,6}(-[a-z0-9]{1,4})?$")
_ORD_RE = re.compile(r"^(\d+)(st|nd|rd|th)$")


def _tokenise(text: str):
    """[(token, part_index, word_index)] keeping '#', '/' and '-' inside tokens."""
    s = ascii_fold(text or "").lower()
    s = re.sub(r"['’`]", "", s)
    s = re.sub(r"(?<![0-9])\.|\.(?![0-9])", " ", s)  # periods except inside numbers
    parts = re.split(r"[,;\n]+", s)
    toks = []
    wi = 0
    for pi, p in enumerate(parts):
        p = p.replace("#", " # ")
        p = re.sub(r"[^a-z0-9#/\- ]+", " ", p)
        for t in p.split():
            t = t.strip("-")
            if not t:
                continue
            m = re.match(r"^([a-z]+)-(\d+[a-z]?)$", t)  # 'i-79', 'pa-8'
            if m:
                toks.extend([(m.group(1), pi, wi), (m.group(2), pi, wi)])
            elif "-" in t and not re.match(r"^\d+-\d+[a-z]?$|^\d{5}-\d{4}$|^\d+-[a-z]$", t):
                toks.extend((x, pi, wi) for x in t.split("-") if x)
            else:
                toks.append((t, pi, wi))
            wi += 1
    return toks


def _drop_country(toks):
    for seq in tables().country_multi:
        n = len(seq)
        if len(toks) > n + 1 and tuple(x[0] for x in toks[-n:]) == seq:
            return toks[:-n]
    return toks


def _part_end(toks, i):
    return i == len(toks) - 1 or toks[i + 1][1] != toks[i][1]


def _unit(toks, sidx, k, known_places, after_unit):
    """(unit text, position after it) for a unit starting at street token sidx[k], else None."""
    t = tables()
    tok, part = toks[sidx[k]][0], toks[sidx[k]][1]

    def same(n):
        return n < len(sidx) and toks[sidx[n]][1] == part

    prev = toks[sidx[k - 1]][0]
    prev_boundary = toks[sidx[k - 1]][1] != part
    canon = t.units.get(tok)
    if canon is None:
        m = _ORD_RE.match(tok)  # '2nd floor' / '3rd fl'
        if m and same(k + 1) and t.units.get(toks[sidx[k + 1]][0]) == "fl" \
                and (after_unit or prev_boundary or prev in t.types):
            return f"fl {int(m.group(1))}", k + 2
        return None
    if t.unit_needs_id[canon]:
        n = k + 1
        if canon != "#" and same(n) and toks[sidx[n]][0] == "#":
            n += 1  # 'apt #4b', 'apt # 4b'
        if not same(n):
            return None
        word = toks[sidx[n]][2]
        end = n + 1
        while end < len(sidx) and toks[sidx[end]][2] == word:  # 'a-1' was split by the tokeniser
            end += 1
        ident = "-".join(toks[sidx[x]][0] for x in range(n, end))
        if not _UNIT_ID_RE.match(ident) or ident in t.types:
            return None
        m = _ORD_RE.match(ident)
        if canon == "fl" and m:  # 'floor 2nd'
            ident = m.group(1)
        return (f"# {ident}" if canon == "#" else f"{canon} {ident}"), end
    # designator without identifier
    part_end = k
    while same(part_end + 1):
        part_end += 1
    alone = prev_boundary and part_end == k
    if not (after_unit or alone or t.types.get(prev) in t.strong_types):
        return None
    if any(t.types.get(toks[sidx[x]][0]) in t.strong_types for x in range(k + 1, part_end + 1)):
        return None  # the street goes on: 'trail side court', 'park side ave'
    if known_places:
        words = [toks[sidx[x]][0] for x in range(k, part_end + 1)]
        if any(place_key(" ".join(words[:m])) in known_places for m in range(2, len(words) + 1)):
            return None  # 'lower burrell', 'upper darby'
    return canon, k + 1


def parse(text: str, known_places=None) -> dict:
    """Parse a typed address. known_places: optional set of place keys (normalize.place_key)."""
    t = tables()
    toks = _drop_country(_tokenise(text))
    used = [False] * len(toks)
    res = {"input": text, "house_number": None, "street": None, "street_key": None,
           "unit": None, "city": None, "city_key": None, "state": None, "zip": None, "zip4": None,
           "zip_invalid": None}
    if not toks:
        return res
    if len(toks) == 1 and _ZIP_RE.match(toks[0][0]):  # a ZIP on its own
        digits = toks[0][0].replace("-", "")
        res["zip"], res["zip4"] = digits[:5], (digits[5:] or None)
        return res

    # 2. ZIP
    zip_i = None
    for i in range(len(toks) - 1, 0, -1):
        tok = toks[i][0]
        if _ZIP_RE.match(tok):
            prev_state = toks[i - 1][0] in t.states
            if _part_end(toks, i) or prev_state:
                digits = tok.replace("-", "")
                res["zip"], res["zip4"] = digits[:5], (digits[5:] or None)
                used[i] = True
                zip_i = i
                break
    if zip_i is None and len(toks) >= 3 and _BAD_ZIP_RE.match(toks[-1][0]) and toks[-2][0] in t.states:
        res["zip_invalid"] = toks[-1][0]  # 'erie pa 1650'
        used[-1] = True

    # 3. state (longest multi-word match)
    state_span = None
    for i in range(len(toks) - 1, 0, -1):
        if used[i]:
            continue
        for seq in t.state_multi:
            n = len(seq)
            if i - n + 1 < 1:
                continue
            words = tuple(x[0] for x in toks[i - n + 1:i + 1])
            if words != seq or any(used[i - n + 1:i + 1]):
                continue
            if len({x[1] for x in toks[i - n + 1:i + 1]}) != 1:
                continue
            before_zip = zip_i is not None and zip_i == i + 1
            at_end = all(used[i + 1:]) or _part_end(toks, i)
            if not (before_zip or at_end):
                continue
            # an ambiguous token right after a house number is a street word
            if n == 1 and seq[0] in t.ambiguous_states and i - 1 >= 0 and _HN_RE.match(toks[i - 1][0]) \
                    and i - 1 == 0:
                continue
            state_span = (i - n + 1, i + 1)
            break
        if state_span:
            break
    if state_span:
        res["state"] = t.states[" ".join(x[0] for x in toks[state_span[0]:state_span[1]])]
        for k in range(*state_span):
            used[k] = True

    # locality-first order: "erie pa 16501 123 main st" or "erie pa, 123 main st"
    loc_end = None
    marks = [k for k in ([zip_i] if zip_i is not None else []) + ([state_span[1] - 1] if state_span else [])]
    if marks:
        last_mark = max(marks)
        rest = [k for k in range(last_mark + 1, len(toks)) if not used[k]]
        if rest and _HN_RE.match(toks[rest[0]][0]) and len(rest) >= 2:
            loc_end = last_mark
    if loc_end is not None:
        city_idx = [k for k in range(0, min(marks)) if not used[k]]
        street_idx = [k for k in range(loc_end + 1, len(toks)) if not used[k]]
        if city_idx:
            res["city"] = " ".join(toks[k][0] for k in city_idx)
        for k in city_idx:
            used[k] = True
    else:
        street_idx = [k for k in range(len(toks)) if not used[k]]

    # if the first comma part has no number but a later part starts with one, use that part
    if street_idx and not _HN_RE.match(toks[street_idx[0]][0]):
        for k in street_idx:
            if _HN_RE.match(toks[k][0]) and (k == 0 or toks[k - 1][1] != toks[k][1]):
                pre = [j for j in street_idx if j < k]
                if pre and res["city"] is None:
                    res["city"] = " ".join(toks[j][0] for j in pre)
                    for j in pre:
                        used[j] = True
                street_idx = [j for j in street_idx if j >= k]
                break

    # 4. house number
    if street_idx and _HN_RE.match(toks[street_idx[0]][0]):
        hn_toks = [toks[street_idx[0]][0]]
        used[street_idx[0]] = True
        street_idx = street_idx[1:]
        nxt = [toks[j][0] for j in street_idx]
        if nxt and nxt[0] in t.fractions:
            hn_toks.append(nxt[0])
            used[street_idx[0]] = True
            street_idx = street_idx[1:]
        elif len(nxt) >= 3 and re.fullmatch(r"[a-z]", nxt[0]) and nxt[0] not in t.dir \
                and nxt[1] not in t.types and hn_toks[0].isdigit() \
                and toks[street_idx[1]][1] == toks[street_idx[0]][1] \
                and not ((find_highway(nxt) or (None,))[0] == 0):
            hn_toks[0] += nxt[0]  # '123 A Main St' -> 123a ('123 A St' keeps street 'A St')
            used[street_idx[0]] = True
            street_idx = street_idx[1:]
        res["house_number"] = house_number(" ".join(hn_toks))[0]

    # 5. units (after at least one street word)
    units, split_at, k = [], None, 1
    while k < len(street_idx):
        got = _unit(toks, street_idx, k, known_places, bool(units))
        if got is None:
            if units:
                break
            k += 1
            continue
        text_u, end = got
        units.append(text_u)
        for j in street_idx[k:end]:
            used[j] = True
        street_idx = street_idx[:k] + street_idx[end:]
        split_at = k  # a unit closes the street part: tokens after it (same part) are the city
    res["unit"] = " ".join(units) or None

    # 6. no leading house number: 'state st 626 erie', 'main st 123', 'route 8 4500 gibsonia'
    if res["house_number"] is None and len(street_idx) > 1:
        words = [toks[j][0] for j in street_idx]
        hw = find_highway(words)
        pos = None
        for k in range(1, len(words)):
            if _HN_RE.match(words[k]) and not (hw and hw[0] <= k < hw[1]) \
                    and (words[k - 1] in t.types or (hw and k == hw[1])):
                pos = k
                break
        if pos is None and _HN_RE.match(words[-1]) and not hw:
            pos = len(words) - 1
        if pos is not None:
            res["house_number"] = house_number(words[pos])[0]
            used[street_idx[pos]] = True
            street_idx = street_idx[:pos] + street_idx[pos + 1:]
            if pos < len(street_idx) and split_at is None:
                split_at = pos

    # 7. only a place name left: 'Erie PA 16501' is a locality, not a street
    if res["house_number"] is None and not units and res["city"] is None and street_idx and known_places:
        if place_key(" ".join(toks[j][0] for j in street_idx)) in known_places:
            res["city"] = " ".join(toks[j][0] for j in street_idx)
            street_idx = []

    # 8. street / city split
    if street_idx and res["city"] is None:
        parts = sorted({toks[j][1] for j in street_idx})
        if len(parts) > 1:
            first = parts[0]
            s_idx = [j for j in street_idx if toks[j][1] == first]
            c_idx = [j for j in street_idx if toks[j][1] != first]
        elif split_at is not None and split_at < len(street_idx):
            s_idx, c_idx = street_idx[:split_at], street_idx[split_at:]
        else:
            s_idx, c_idx = _split_street_city([toks[j][0] for j in street_idx], street_idx,
                                              known_places, bool(res["state"] or res["zip"]))
        street_idx = s_idx
        if c_idx:
            res["city"] = " ".join(toks[j][0] for j in c_idx)
    if street_idx:
        res["street"] = " ".join(toks[j][0] for j in street_idx)
        res["street_key"] = street_key(res["street"])
    if res["city"]:
        res["city_key"] = place_key(res["city"])
    return res


def _split_street_city(words, idx, known_places, has_locality_marker):
    t = tables()
    n = len(words)
    # (a) gazetteer: longest trailing run that is a known place, leaving >= 1 street word
    if known_places:
        for start in range(1, n):
            cand = place_key(" ".join(words[start:]))
            if cand and cand in known_places:
                return idx[:start], idx[start:]
    # (b) highway form: street ends after the route number (+ modifiers, + directional)
    hw = find_highway(words)
    if hw:
        end = hw[1]
        if end < n and words[end] in t.dir:
            end += 1
        return idx[:end], idx[end:]
    # (c) last strong street type (else last weak type), plus a following directional
    for pool in (t.strong_types, t.type_canon):
        last = None
        for i in range(1, n):
            canon = t.types.get(words[i])
            if canon in pool:
                last = i
        if last is not None:
            end = last + 1
            if end < n and words[end] in t.dir and words[end] not in t.weak_dirs:
                end += 1
            return idx[:end], idx[end:]
    # (d) nothing to go on
    if has_locality_marker and n >= 2:
        return idx[:-1], idx[-1:]
    return idx, []
