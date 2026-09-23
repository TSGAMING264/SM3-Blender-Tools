from __future__ import annotations

import marshal
import struct
import zlib
from pathlib import Path
from typing import Optional, Tuple

_DB = None
_DB_ERROR = ""
_MAGIC = b"SM3LMT01"


def _u32(value) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        try:
            return int(value, 0) & 0xFFFFFFFF
        except Exception:
            return 0
    try:
        return int(value) & 0xFFFFFFFF
    except Exception:
        return 0


def _load():
    """Load the compact MESH->MAT index once.

    This is intentionally NOT the heavy Material Combiner database. The packed
    file contains only the verified identity information needed to restore the
    real 0xXXXXXXXX MAT names during normal model import.
    """
    global _DB, _DB_ERROR
    if _DB is not None:
        return _DB
    try:
        raw = (Path(__file__).resolve().parent / "mesh_mat_lite.bin").read_bytes()
        if len(raw) < 28 or raw[:8] != _MAGIC:
            raise ValueError("bad compact material database header")
        version, exact_n, field_n, loose_n, names_n = struct.unpack_from("<IIIII", raw, 8)
        if version != 1:
            raise ValueError(f"unsupported compact material database version {version}")
        exact, field, loose, names = marshal.loads(zlib.decompress(raw[28:]))
        # Tiny integrity guard: corrupted/incomplete files fall back safely.
        if len(exact) != exact_n or len(field) != field_n or len(loose) != loose_n or len(names) != names_n:
            raise ValueError("compact material database count mismatch")
        _DB = (exact, field, loose, names)
        _DB_ERROR = ""
    except Exception as exc:
        _DB = ({}, {}, {}, {})
        _DB_ERROR = f"{type(exc).__name__}: {exc}"
    return _DB


def database_error() -> str:
    _load()
    return _DB_ERROR


def material_name(mat_hash: int) -> str:
    _exact, _field, _loose, names = _load()
    return str(names.get(_u32(mat_hash), "") or "")


def resolve_mesh_material(mesh_hash: int, material_field_offset: int, serialized_ref: int) -> Optional[Tuple[int, str, str]]:
    """Return (REAL_MAT_HASH, friendly_name, match_mode), or None.

    Resolution order matches the verified v0.6 database logic while avoiding
    the multi-megabyte CSV scan that made the full toolkit feel slow.
    """
    exact, field, loose, names = _load()
    mh = _u32(mesh_hash)
    fo = _u32(material_field_offset)
    sr = _u32(serialized_ref)

    mat = exact.get((mh, fo, sr))
    mode = "FIELD+SERIALIZED_EXACT"
    if mat is None:
        mat = field.get((mh, fo))
        mode = "FIELD_UNIQUE"
    if mat is None:
        mat = loose.get((mh, sr))
        mode = "MESH+SERIALIZED_UNIQUE"
    if mat is None:
        return None
    mat = _u32(mat)
    return mat, str(names.get(mat, "") or ""), mode
