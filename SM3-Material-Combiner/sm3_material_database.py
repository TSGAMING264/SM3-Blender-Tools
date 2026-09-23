from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


DATABASE_VERSION = "SM3 CLEAN DATABASE v5.2.116"
DEFAULT_PLAYER_PACK = "CH_SPIDERMAN.PCPACK"


def _u32(value) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value & 0xFFFFFFFF
    text = str(value).strip()
    if not text:
        return 0
    return int(text, 0) & 0xFFFFFFFF


def _hex(value: int) -> str:
    return f"0x{int(value) & 0xFFFFFFFF:08X}"


def _split_u32_values(text: str) -> Set[int]:
    out: Set[int] = set()
    for part in str(text or "").split("|"):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(_u32(part))
        except Exception:
            pass
    return out


@dataclass(frozen=True)
class MaterialResolution:
    mesh_hash: int
    mesh_name: str
    material_field_offset: int
    serialized_ref: int
    mat_hash: int
    mat_name: str
    context_dependent: bool
    match_mode: str
    example_packs: Tuple[str, ...]


@dataclass(frozen=True)
class TextureBinding:
    mat_hash: int
    mat_name: str
    field_offset: int
    tex_hash: int
    tex_name: str
    context_dependent: bool
    example_packs: Tuple[str, ...]


class SM3MaterialDatabase:
    """Lazy loader for the v5.2.116 clean SM3 MESH -> MAT -> TEX database.

    The central rule is deliberately enforced here:

        serialized material pointer != material identity

    Serialized pointers are APKF-layout-local values.  The real MAT hash is the
    stable material identity used for Blender-visible 0xXXXXXXXX material names.
    """

    def __init__(self, data_dir: Optional[str | Path] = None):
        self.data_dir = Path(data_dir) if data_dir else Path(__file__).resolve().parent / "data"
        self.loaded = False
        self.load_error = ""

        self.mesh_field_index: Dict[Tuple[int, int], List[dict]] = {}
        self.mesh_serialized_index: Dict[Tuple[int, int], List[dict]] = {}
        self.mat_texture_index: Dict[int, List[TextureBinding]] = {}
        self.player_field_ref_index: Dict[Tuple[str, int, int, int], Set[int]] = {}
        self.player_mat_ref_index: Dict[Tuple[str, int, int], Set[int]] = {}
        self.stats: Dict[str, int] = {}

    def _rows(self, filename: str):
        path = self.data_dir / filename
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
            yield from csv.DictReader(f)

    def load(self) -> "SM3MaterialDatabase":
        if self.loaded:
            return self

        try:
            mesh_rows = 0
            for row in self._rows("MESH_MATERIAL_DATABASE.csv"):
                mesh_hash = _u32(row.get("mesh_hash"))
                field_offset = _u32(row.get("mesh_material_field_offset"))
                entry = {
                    "mesh_hash": mesh_hash,
                    "mesh_name": str(row.get("mesh_name") or ""),
                    "field_offset": field_offset,
                    "mat_hash": _u32(row.get("mat_hash")),
                    "mat_name": str(row.get("mat_name") or ""),
                    "serialized_refs": _split_u32_values(row.get("serialized_pointer_values", "")),
                    "context_dependent": str(row.get("context_dependent") or "0") == "1",
                    "context_variant_count": int(row.get("context_variant_count") or 0),
                    "example_packs": tuple(p for p in str(row.get("example_packs") or "").split("|") if p),
                }
                self.mesh_field_index.setdefault((mesh_hash, field_offset), []).append(entry)
                for local_ref in entry["serialized_refs"]:
                    self.mesh_serialized_index.setdefault((mesh_hash, local_ref), []).append(entry)
                mesh_rows += 1

            mat_tex_rows = 0
            for row in self._rows("MATERIAL_TEXTURE_DATABASE.csv"):
                mat_hash = _u32(row.get("mat_hash"))
                binding = TextureBinding(
                    mat_hash=mat_hash,
                    mat_name=str(row.get("mat_name") or ""),
                    field_offset=_u32(row.get("material_texture_field_offset")),
                    tex_hash=_u32(row.get("tex_hash")),
                    tex_name=str(row.get("tex_name") or ""),
                    context_dependent=str(row.get("context_dependent") or "0") == "1",
                    example_packs=tuple(p for p in str(row.get("example_packs") or "").split("|") if p),
                )
                self.mat_texture_index.setdefault(mat_hash, []).append(binding)
                mat_tex_rows += 1

            # The Spider-Man chain carries the exact package-local serialized
            # pointer needed by the experimental player exporter/NativeMESH path.
            player_rows = 0
            for row in self._rows("SPIDERMAN_MESH_MAT_TEX_CHAIN.csv"):
                pack = str(row.get("source_pack") or "").strip().upper()
                mesh_hash = _u32(row.get("mesh_hash"))
                field_offset = _u32(row.get("mesh_material_field_offset"))
                mat_hash = _u32(row.get("mat_hash"))
                serialized = _u32(row.get("serialized_material_pointer"))
                self.player_field_ref_index.setdefault(
                    (pack, mesh_hash, field_offset, mat_hash), set()
                ).add(serialized)
                self.player_mat_ref_index.setdefault(
                    (pack, mesh_hash, mat_hash), set()
                ).add(serialized)
                player_rows += 1

            for bindings in self.mat_texture_index.values():
                bindings.sort(key=lambda b: (b.field_offset, b.tex_hash, b.tex_name.casefold()))

            self.stats = {
                "mesh_material_rows": mesh_rows,
                "mesh_field_keys": len(self.mesh_field_index),
                "mesh_serialized_keys": len(self.mesh_serialized_index),
                "material_texture_rows": mat_tex_rows,
                "materials_with_textures": len(self.mat_texture_index),
                "spiderman_chain_rows": player_rows,
                "player_field_keys": len(self.player_field_ref_index),
            }
            self.loaded = True
            self.load_error = ""
        except Exception as exc:
            self.load_error = f"{type(exc).__name__}: {exc}"
            raise
        return self

    def resolve_mesh_material(
        self,
        mesh_hash: int,
        material_field_offset: int,
        serialized_ref: int,
    ) -> Optional[MaterialResolution]:
        self.load()
        mesh_hash = _u32(mesh_hash)
        field = _u32(material_field_offset)
        serialized = _u32(serialized_ref)
        candidates = list(self.mesh_field_index.get((mesh_hash, field), ()))
        serialized_matches = [c for c in candidates if serialized in c["serialized_refs"]]
        if len(serialized_matches) == 1:
            c = serialized_matches[0]
            mode = "FIELD+SERIALIZED_EXACT"
        elif len(candidates) == 1:
            c = candidates[0]
            mode = "FIELD_UNIQUE"
        else:
            # Loose/custom MESH exports can move the MeshInfo record while still
            # preserving the stock APKF-local serialized ref.  Fall back to
            # mesh-hash + serialized-ref ONLY when it identifies one real MAT.
            loose_candidates = self.mesh_serialized_index.get((mesh_hash, serialized), ())
            unique_by_mat = {}
            for candidate in loose_candidates:
                unique_by_mat[candidate["mat_hash"]] = candidate
            if len(unique_by_mat) == 1:
                c = next(iter(unique_by_mat.values()))
                mode = "MESH+SERIALIZED_UNIQUE"
            else:
                # Context-dependent or genuinely ambiguous.  Do not guess.
                return None

        return MaterialResolution(
            mesh_hash=mesh_hash,
            mesh_name=c["mesh_name"],
            material_field_offset=field,
            serialized_ref=serialized,
            mat_hash=c["mat_hash"],
            mat_name=c["mat_name"],
            context_dependent=bool(c["context_dependent"]),
            match_mode=mode,
            example_packs=tuple(c["example_packs"]),
        )

    def textures_for_material(self, mat_hash: int) -> List[TextureBinding]:
        self.load()
        return list(self.mat_texture_index.get(_u32(mat_hash), ()))

    def resolve_player_serialized_ref(
        self,
        mesh_hash: int,
        mat_hash: int,
        *,
        material_field_offset: Optional[int] = None,
        preferred_pack: str = DEFAULT_PLAYER_PACK,
    ) -> Optional[int]:
        """Resolve a REAL MAT hash back to the target APKF-local pointer.

        For the player exporter we prefer CH_SPIDERMAN.PCPACK because the
        NativeMESH runtime mapping is based on the stock player package.
        """
        self.load()
        pack = str(preferred_pack or DEFAULT_PLAYER_PACK).strip().upper()
        mesh_hash = _u32(mesh_hash)
        mat_hash = _u32(mat_hash)

        if material_field_offset is not None:
            refs = self.player_field_ref_index.get(
                (pack, mesh_hash, _u32(material_field_offset), mat_hash), set()
            )
            if len(refs) == 1:
                return next(iter(refs))

        refs = self.player_mat_ref_index.get((pack, mesh_hash, mat_hash), set())
        if len(refs) == 1:
            return next(iter(refs))
        return None

    def material_candidates(self, mesh_hash: int, material_field_offset: int) -> List[dict]:
        self.load()
        return list(self.mesh_field_index.get((_u32(mesh_hash), _u32(material_field_offset)), ()))

    def summary(self) -> str:
        self.load()
        return (
            f"{DATABASE_VERSION}: "
            f"{self.stats.get('mesh_material_rows', 0):,} MESH/MAT rows, "
            f"{self.stats.get('material_texture_rows', 0):,} MAT/TEX rows, "
            f"{self.stats.get('spiderman_chain_rows', 0):,} Spider-Man chain rows"
        )


_DATABASE: Optional[SM3MaterialDatabase] = None


def get_material_database() -> SM3MaterialDatabase:
    global _DATABASE
    if _DATABASE is None:
        _DATABASE = SM3MaterialDatabase()
    return _DATABASE.load()


def material_hash_name(mat_hash: int) -> str:
    return _hex(mat_hash)


def unresolved_ref_name(serialized_ref: int) -> str:
    return f"SM3_REF_{int(serialized_ref) & 0xFFFFFFFF:08X}"
