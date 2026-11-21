"""Build Valhalla's timezone database (tz_world.sqlite) without spatialite CLI tools.

Upstream Valhalla builds this file with the shell script
`valhalla_build_timezones`, which needs the `spatialite` and `spatialite_tool`
command-line programs plus `unzip`. That script is not shipped in any pyvalhalla
wheel (checked: win_amd64 and manylinux x86_64, 3.9.1), and the spatialite
CLI has no maintained portable Windows build.

The pyvalhalla wheel does vendor the exact SQLite and libspatialite shared
libraries that valhalla_build_tiles uses, so this module drives them through
ctypes and runs the same SQL the upstream script runs:
ImportSHP -> CreateSpatialIndex -> VACUUM -> ANALYZE. The result has the
table/column names Valhalla queries (tz_world.TZID, tz_world.geom).

Input: timezone-boundary-builder release 2026d (the release that
valhalla 3.9.1's own script pins), "timezones-with-oceans-1970" shapefile.
Data licence: ODbL 1.0 (README of evansiroky/timezone-boundary-builder).
"""
from __future__ import annotations

import ctypes
import os
import sys
import urllib.request
import zipfile
from pathlib import Path

TZ_RELEASE = "2026d"
TZ_ZIP = "timezones-with-oceans-1970.shapefile.zip"
TZ_URL = f"https://github.com/evansiroky/timezone-boundary-builder/releases/download/{TZ_RELEASE}/{TZ_ZIP}"
TZ_SHA256 = "f9b2f0304772f2a944486da5d13d1740ff9754e76b983f80450b0e4d74131931"
SHP_BASENAME = "combined-shapefile-with-oceans-1970"


def fetch_shapefile(download_dir: Path) -> Path:
    """Download (once) and unzip the pinned shapefile; returns the .shp path without extension."""
    from common import file_hash  # local import keeps this module importable standalone

    download_dir.mkdir(parents=True, exist_ok=True)
    zpath = download_dir / f"{TZ_RELEASE}-{TZ_ZIP}"
    if not zpath.exists() or file_hash(zpath) != TZ_SHA256:
        print(f"[tz] downloading {TZ_URL}", flush=True)
        tmp = zpath.with_suffix(".part")
        urllib.request.urlretrieve(TZ_URL, tmp)
        got = file_hash(tmp)
        if got != TZ_SHA256:
            raise RuntimeError(f"sha256 mismatch for {TZ_ZIP}: {got}")
        tmp.replace(zpath)
    out = download_dir / f"tz-{TZ_RELEASE}"
    stem = out / SHP_BASENAME
    if not stem.with_suffix(".shp").exists():
        with zipfile.ZipFile(zpath) as z:
            for m in z.namelist():
                name = Path(m).name
                if name.startswith(SHP_BASENAME):
                    out.mkdir(parents=True, exist_ok=True)
                    (out / name).write_bytes(z.read(m))
    return stem


def _load_libs():
    import glob

    import valhalla

    libs = Path(valhalla.__file__).resolve().parent.parent / "pyvalhalla.libs"
    if os.name == "nt":
        os.add_dll_directory(str(libs))
        sq = glob.glob(str(libs / "sqlite3-*.dll"))
        sp = glob.glob(str(libs / "spatialite-*.dll"))
    else:
        sq = glob.glob(str(libs / "libsqlite3*.so*"))
        sp = glob.glob(str(libs / "libspatialite*.so*"))
    if not sq or not sp:
        raise FileNotFoundError(f"vendored sqlite3/spatialite not found in {libs}")
    sqlite = ctypes.CDLL(sq[0])
    spatialite = ctypes.CDLL(sp[0])
    sqlite.sqlite3_open_v2.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int, ctypes.c_char_p]
    sqlite.sqlite3_exec.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_char_p)]
    sqlite.sqlite3_close_v2.argtypes = [ctypes.c_void_p]
    sqlite.sqlite3_errmsg.argtypes = [ctypes.c_void_p]
    sqlite.sqlite3_errmsg.restype = ctypes.c_char_p
    sqlite.sqlite3_prepare_v2.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
                                          ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    sqlite.sqlite3_step.argtypes = [ctypes.c_void_p]
    sqlite.sqlite3_column_text.argtypes = [ctypes.c_void_p, ctypes.c_int]
    sqlite.sqlite3_column_text.restype = ctypes.c_char_p
    sqlite.sqlite3_finalize.argtypes = [ctypes.c_void_p]
    spatialite.spatialite_alloc_connection.restype = ctypes.c_void_p
    spatialite.spatialite_init_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    spatialite.spatialite_cleanup_ex.argtypes = [ctypes.c_void_p]
    spatialite.spatialite_version.restype = ctypes.c_char_p
    return sqlite, spatialite


def _query1(sqlite, db, sql: str):
    stmt = ctypes.c_void_p()
    if sqlite.sqlite3_prepare_v2(db, sql.encode(), -1, ctypes.byref(stmt), None) != 0:
        raise RuntimeError(f"{sql}: {sqlite.sqlite3_errmsg(db).decode()}")
    rows = []
    while sqlite.sqlite3_step(stmt) == 100:  # SQLITE_ROW
        v = sqlite.sqlite3_column_text(stmt, 0)
        rows.append(v.decode() if v is not None else None)
    sqlite.sqlite3_finalize(stmt)
    return rows


def build_tz_db(shp_stem: Path, out_db: Path) -> dict:
    """Create out_db from the shapefile. Must run with SPATIALITE_SECURITY=relaxed
    in the environment (ImportSHP refuses to read files otherwise)."""
    if os.environ.get("SPATIALITE_SECURITY") != "relaxed":
        raise RuntimeError("set SPATIALITE_SECURITY=relaxed before calling build_tz_db")
    sqlite, spatialite = _load_libs()
    out_db.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_db.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    db = ctypes.c_void_p()
    flags = 0x2 | 0x4  # READWRITE | CREATE
    if sqlite.sqlite3_open_v2(str(tmp).encode(), ctypes.byref(db), flags, None) != 0:
        raise RuntimeError("sqlite3_open_v2 failed")
    cache = spatialite.spatialite_alloc_connection()
    spatialite.spatialite_init_ex(db, cache, 0)

    def ex(sql: str):
        err = ctypes.c_char_p()
        rc = sqlite.sqlite3_exec(db, sql.encode(), None, None, ctypes.byref(err))
        if rc != 0:
            raise RuntimeError(f"{sql[:80]}...: rc={rc} {err.value.decode() if err.value else ''}")

    shp = str(shp_stem).replace("\\", "/")
    ex("SELECT InitSpatialMetaData(1);")
    # same arguments spatialite_tool -i -shp ... -t tz_world -s 4326 -g geom -c UTF-8 uses
    n = _query1(sqlite, db, f"SELECT ImportSHP('{shp}', 'tz_world', 'UTF-8', 4326, 'geom', 'PK_UID', "
                            f"'AUTO', 0, 0, 0, 0, 'SAME');")
    if not n or n[0] in (None, "0", "-1"):
        raise RuntimeError(f"ImportSHP imported nothing (returned {n})")
    ex("SELECT CreateSpatialIndex('tz_world', 'geom');")
    count = _query1(sqlite, db, "SELECT count(*) FROM tz_world;")[0]
    sample = _query1(sqlite, db, "SELECT TZID FROM tz_world WHERE ST_Intersects(geom, MakePoint(-80.08, 42.13, 4326)) "
                                 "AND rowid IN (SELECT rowid FROM SpatialIndex WHERE f_table_name='tz_world' "
                                 "AND search_frame=BuildMBR(-80.09,42.12,-80.07,42.14));")
    ex("VACUUM;")
    ex("ANALYZE;")
    version = spatialite.spatialite_version().decode()
    sqlite.sqlite3_close_v2(db)
    spatialite.spatialite_cleanup_ex(cache)
    tmp.replace(out_db)
    return {"rows": int(count), "erie_pa_tz": sample, "spatialite": version,
            "size_bytes": out_db.stat().st_size}


if __name__ == "__main__":
    # usage: python tzdb.py SHP_STEM OUT_DB   (run by build.py in a child process)
    import json

    print(json.dumps(build_tz_db(Path(sys.argv[1]), Path(sys.argv[2]))))
