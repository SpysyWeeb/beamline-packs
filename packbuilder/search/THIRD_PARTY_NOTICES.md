# Third-party notices for the search index

The search database built by `build.py` ships to devices. It contains data
from these sources besides this project's own files.

## OpenStreetMap data

Map data (c) OpenStreetMap contributors, available under the Open Database
Licence 1.0 (https://www.openstreetmap.org/copyright). The attribution line is
stored in the database's `meta` table (`license`).

## iD tagging schema (category words)

Some category words in `categories_idterms.json`, and therefore in the
`category_term` table of the database, are copied from the preset names,
aliases and terms of @openstreetmap/id-tagging-schema 7.3.0
(`dist/translations/en.json`). The same notice is stored in
`categories_idterms.json` (`attribution_license`) and in the database's `meta`
table (`third_party_notices`).

```
ISC License

Copyright (c) iD Contributors

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
```

## SQLite

Public domain (https://sqlite.org/copyright.html). Part of the platform or of
androidx sqlite-bundled on the device; not shipped by this folder.
