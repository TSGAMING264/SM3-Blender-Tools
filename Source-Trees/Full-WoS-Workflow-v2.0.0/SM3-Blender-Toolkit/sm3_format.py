from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import math
import os
import re
import struct
import statistics
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


def align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def sm3_hash(text: str) -> int:
    h = 0
    for ch in text:
        h = ((h * 33) + ord(ch.lower())) & 0xFFFFFFFF
    return h


# Hashes recovered from CH_SPIDERMAN's string data and verified against the raw
# 75-bone Spider-Man skeleton. Unknown/general skeletons fall back to hash names.
_SPIDERMAN_BONE_NAMES: Sequence[str] = (
    "bip01 pelvis",
    "bip01 spine",
    "bip01 spine1",
    "bip01 spine2",
    "bip01 neck",
    "bip01 head",
    "bone r eye",
    "bone l eye",
    "bip01 l clavicle",
    "bip01 l upperarm",
    "bip01 l forearm",
    "bip01 l hand",
    "bip01 l finger0",
    "bip01 l finger01",
    "bip01 l finger02",
    "bip01 l finger1",
    "bip01 l finger11",
    "bip01 l finger12",
    "bip01 l finger2",
    "bip01 l finger21",
    "bip01 l finger22",
    "bip01 l finger3",
    "bip01 l finger31",
    "bip01 l finger32",
    "bip01 l finger4",
    "bip01 l finger41",
    "bip01 l finger42",
    "bip01 prop l hand",
    "bip01 l foretwist",
    "bip01 l foretwist1",
    "bip01 l bone01",
    "bone l upperarm split1",
    "bone l upperarm split2",
    "bone l upperarm split3",
    "bip01 l upperarm pivot",
    "bip01 l clavicle pivot",
    "bip01 r clavicle",
    "bip01 r upperarm",
    "bip01 r forearm",
    "bip01 r hand",
    "bip01 r finger0",
    "bip01 r finger01",
    "bip01 r finger02",
    "bip01 r finger1",
    "bip01 r finger11",
    "bip01 r finger12",
    "bip01 r finger2",
    "bip01 r finger21",
    "bip01 r finger22",
    "bip01 r finger3",
    "bip01 r finger31",
    "bip01 r finger32",
    "bip01 r finger4",
    "bip01 r finger41",
    "bip01 r finger42",
    "bip01 prop r hand",
    "bip01 r foretwist",
    "bip01 r foretwist1",
    "bip01 r bone01",
    "bone r upperarm split1",
    "bone r upperarm split2",
    "bone r upperarm split3",
    "bip01 r upperarm pivot",
    "bip01 r clavicle pivot",
    "bip01 r hip",
    "bip01 l hip",
    "bip01 r thigh",
    "bip01 r calf",
    "bip01 r foot",
    "bip01 r toe0",
    "sync",
    "bip01 l thigh",
    "bip01 l calf",
    "bip01 l foot",
    "bip01 l toe0",
)

BONE_HASH_NAMES: Dict[int, str] = {sm3_hash(name): name for name in _SPIDERMAN_BONE_NAMES}


@dataclass
class SM3Bone:
    index: int
    unknown00: bytes
    inverse_bind_raw: bytes
    inverse_values: Tuple[float, ...]
    name_pointer_serialized: int
    name_hash: int
    parent_index: int
    unknown_8c: int

    @property
    def name(self) -> str:
        return BONE_HASH_NAMES.get(self.name_hash, f"bone_{self.index:02d}_0x{self.name_hash:08X}")


@dataclass
class SM3Skeleton:
    path: str
    filename_pointer_serialized: int
    filename_hash: int
    bone_count: int
    bones_pointer_serialized: int
    bones: List[SM3Bone]
    raw: bytes


@dataclass
class VertexDecl:
    stream: int
    offset: int
    dtype: int
    method: int
    usage: int
    usage_index: int


@dataclass
class SM3MeshSection:
    index: int
    info_offset: int
    mesh_offset: Tuple[float, float, float]
    sphere_radius: float
    mesh_bbox: Tuple[float, float, float, float]
    material_ref_serialized: int
    bone_palette_pointer_serialized: int
    bone_palette: List[int]
    vertex_buffer_pointer_serialized: int
    unknown_30: int
    vertex_count: int
    unknown_38: int
    index_buffer_pointer_serialized: int
    unknown_40: int
    index_count: int
    index_size: int
    schema_pointer_serialized: int
    primitive_type: int
    primitive_unknown: int
    vertex_stride: int
    vertex_schema_pointer_serialized: int
    schema_unknown: int
    decl: List[VertexDecl]
    vertex_buffer_offset: int = 0
    index_buffer_offset: int = 0


@dataclass
class SM3Mesh:
    path: str
    filename_pointer_serialized: int
    filename_hash: int
    parsed_flags: int
    section_count: int
    section_table_pointer_serialized: int
    skeleton_ref_serialized: int
    external_mesh_count: int
    external_mesh_table_pointer_serialized: int
    model_offset: Tuple[float, float, float]
    sphere_radius: float
    model_bbox: Tuple[float, float, float, float]
    sections: List[SM3MeshSection]
    img_size: int
    phys_size: int
    raw: bytes


D3D_USAGE = {
    0: "POSITION",
    1: "BLENDWEIGHT",
    2: "BLENDINDICES",
    3: "NORMAL",
    4: "PSIZE",
    5: "TEXCOORD",
    6: "TANGENT",
    7: "BINORMAL",
    8: "TESSFACTOR",
    9: "POSITIONT",
    10: "COLOR",
    11: "FOG",
    12: "DEPTH",
    13: "SAMPLE",
}


def parse_skeleton(path: str | os.PathLike[str]) -> SM3Skeleton:
    p = Path(path)
    data = p.read_bytes()
    if len(data) < 0x10:
        raise ValueError("File is too small to be an SM3 raw SKEL")

    filename_ptr, filename_hash, bone_count, bones_ptr = struct.unpack_from("<4I", data, 0)
    expected = 0x10 + bone_count * 0x90
    if bone_count <= 0 or bone_count > 4096:
        raise ValueError(f"Unreasonable SM3 bone count: {bone_count}")
    if len(data) < expected:
        raise ValueError(f"Truncated SKEL: expected at least 0x{expected:X} bytes, got 0x{len(data):X}")

    bones: List[SM3Bone] = []
    for i in range(bone_count):
        off = 0x10 + i * 0x90
        unknown = data[off:off + 0x40]
        invraw = data[off + 0x40:off + 0x80]
        inv = struct.unpack("<16f", invraw)
        name_ptr, name_hash, parent_u, unknown_8c = struct.unpack_from("<4I", data, off + 0x80)
        parent = parent_u if parent_u < 0x80000000 else parent_u - 0x100000000
        bones.append(
            SM3Bone(
                index=i,
                unknown00=unknown,
                inverse_bind_raw=invraw,
                inverse_values=tuple(inv),
                name_pointer_serialized=name_ptr,
                name_hash=name_hash,
                parent_index=parent,
                unknown_8c=unknown_8c,
            )
        )

    return SM3Skeleton(
        path=str(p),
        filename_pointer_serialized=filename_ptr,
        filename_hash=filename_hash,
        bone_count=bone_count,
        bones_pointer_serialized=bones_ptr,
        bones=bones,
        raw=data,
    )


def _compute_phys_size(sections: Sequence[SM3MeshSection]) -> int:
    pos = 0
    for i, sec in enumerate(sections):
        pos += sec.vertex_count * sec.vertex_stride
        pos = align(pos, 4)
        pos += sec.index_count * sec.index_size
        # Original resources align between section IB and the next VB, but the
        # final PHYS payload can end directly after the last index.
        if i != len(sections) - 1:
            pos = align(pos, 4)
    return pos


def parse_mesh(path: str | os.PathLike[str]) -> SM3Mesh:
    p = Path(path)
    data = p.read_bytes()
    if len(data) < 0x50:
        raise ValueError("File is too small to be an SM3 raw MESH")

    filename_ptr, filename_hash, flags, section_count = struct.unpack_from("<4I", data, 0)
    if section_count <= 0 or section_count > 4096:
        raise ValueError(f"Unreasonable section count: {section_count}")

    section_table_ptr, skeleton_ref, external_count, external_table_ptr = struct.unpack_from("<4I", data, 0x10)
    model_offset = struct.unpack_from("<3f", data, 0x20)
    sphere_radius = struct.unpack_from("<f", data, 0x2C)[0]
    model_bbox = struct.unpack_from("<4f", data, 0x30)

    # SM3 raw MESH IMG layout (confirmed on CH_SPIDERMAN):
    # 0x40 model header
    # 0x10 preserved/engine block
    # N * 8 section table entries
    # 16-byte aligned section metadata records
    pos = align(0x40 + 0x10 + section_count * 8, 16)
    sections: List[SM3MeshSection] = []

    sentinel = b"\xFF\x00\x00\x00\x11\x00\x00\x00"

    for section_index in range(section_count):
        info_offset = pos
        if pos + 0x50 > len(data):
            raise ValueError(f"Section {section_index}: truncated MeshInfo at 0x{pos:X}")

        vals = struct.unpack_from("<8f12I", data, pos)
        mesh_offset = tuple(vals[0:3])
        mesh_sphere = vals[3]
        mesh_bbox = tuple(vals[4:8])
        ints = vals[8:]
        (
            material_ref,
            bone_palette_ptr,
            bone_count,
            vb_ptr,
            unknown_30,
            vertex_count,
            unknown_38,
            ib_ptr,
            unknown_40,
            index_count,
            index_size,
            schema_ptr,
        ) = ints
        pos += 0x50

        if pos + 8 > len(data):
            raise ValueError(f"Section {section_index}: missing primitive header")
        primitive_type, primitive_unknown = struct.unpack_from("<2I", data, pos)
        pos += 8

        if bone_count > 4096:
            raise ValueError(f"Section {section_index}: unreasonable bone palette count {bone_count}")
        if pos + bone_count * 2 > len(data):
            raise ValueError(f"Section {section_index}: truncated bone palette")
        palette = list(struct.unpack_from("<" + "H" * bone_count, data, pos)) if bone_count else []
        pos += bone_count * 2
        pos = align(pos, 4)

        if pos + 12 > len(data):
            raise ValueError(f"Section {section_index}: missing schema table")
        vertex_stride, vertex_schema_ptr, schema_unknown = struct.unpack_from("<3I", data, pos)
        pos += 12
        if vertex_stride <= 0 or vertex_stride > 4096:
            raise ValueError(f"Section {section_index}: invalid vertex stride {vertex_stride}")

        decl: List[VertexDecl] = []
        while True:
            if pos + 8 > len(data):
                raise ValueError(f"Section {section_index}: vertex declaration runs off file")
            raw_decl = data[pos:pos + 8]
            pos += 8
            if raw_decl == sentinel:
                break
            stream, offset, dtype, method, usage, usage_index = struct.unpack("<HHBBBB", raw_decl)
            decl.append(VertexDecl(stream, offset, dtype, method, usage, usage_index))
            if len(decl) > 64:
                raise ValueError(f"Section {section_index}: declaration has no sentinel")

        raw_end = pos
        # Original IMG section metadata is 16-byte aligned between sections.
        if section_index != section_count - 1:
            pos = align(pos, 16)

        sections.append(
            SM3MeshSection(
                index=section_index,
                info_offset=info_offset,
                mesh_offset=mesh_offset,
                sphere_radius=mesh_sphere,
                mesh_bbox=mesh_bbox,
                material_ref_serialized=material_ref,
                bone_palette_pointer_serialized=bone_palette_ptr,
                bone_palette=palette,
                vertex_buffer_pointer_serialized=vb_ptr,
                unknown_30=unknown_30,
                vertex_count=vertex_count,
                unknown_38=unknown_38,
                index_buffer_pointer_serialized=ib_ptr,
                unknown_40=unknown_40,
                index_count=index_count,
                index_size=index_size,
                schema_pointer_serialized=schema_ptr,
                primitive_type=primitive_type,
                primitive_unknown=primitive_unknown,
                vertex_stride=vertex_stride,
                vertex_schema_pointer_serialized=vertex_schema_ptr,
                schema_unknown=schema_unknown,
                decl=decl,
            )
        )

    phys_size = _compute_phys_size(sections)
    img_size = len(data) - phys_size
    if img_size <= 0:
        raise ValueError("Computed PHYS size exceeds file size")

    # Assign sequential PHYS spans. This matches the original SM3 resources and
    # the stock renderer path: VB, 4-byte align, IB, align before next section.
    cursor = img_size
    for i, sec in enumerate(sections):
        sec.vertex_buffer_offset = cursor
        cursor += sec.vertex_count * sec.vertex_stride
        cursor = align(cursor, 4)
        sec.index_buffer_offset = cursor
        cursor += sec.index_count * sec.index_size
        if i != len(sections) - 1:
            cursor = align(cursor, 4)

    if cursor != len(data):
        raise ValueError(
            f"MESH PHYS walk mismatch: ended 0x{cursor:X}, file is 0x{len(data):X} "
            f"(IMG=0x{img_size:X}, PHYS=0x{phys_size:X})"
        )

    return SM3Mesh(
        path=str(p),
        filename_pointer_serialized=filename_ptr,
        filename_hash=filename_hash,
        parsed_flags=flags,
        section_count=section_count,
        section_table_pointer_serialized=section_table_ptr,
        skeleton_ref_serialized=skeleton_ref,
        external_mesh_count=external_count,
        external_mesh_table_pointer_serialized=external_table_ptr,
        model_offset=tuple(model_offset),
        sphere_radius=sphere_radius,
        model_bbox=tuple(model_bbox),
        sections=sections,
        img_size=img_size,
        phys_size=phys_size,
        raw=data,
    )


def _read_dtype(data: bytes, offset: int, dtype: int):
    if dtype == 0:  # FLOAT1
        return struct.unpack_from("<f", data, offset)
    if dtype == 1:  # FLOAT2
        return struct.unpack_from("<2f", data, offset)
    if dtype == 2:  # FLOAT3
        return struct.unpack_from("<3f", data, offset)
    if dtype == 3:  # FLOAT4
        return struct.unpack_from("<4f", data, offset)
    if dtype in (4, 5):  # D3DCOLOR / UBYTE4
        return struct.unpack_from("<4B", data, offset)
    if dtype == 6:  # SHORT2
        return struct.unpack_from("<2h", data, offset)
    if dtype == 7:  # SHORT4
        return struct.unpack_from("<4h", data, offset)
    if dtype == 8:  # UBYTE4N
        vals = struct.unpack_from("<4B", data, offset)
        return tuple(v / 255.0 for v in vals)
    if dtype == 9:  # SHORT2N
        vals = struct.unpack_from("<2h", data, offset)
        return tuple(max(-1.0, v / 32767.0) for v in vals)
    if dtype == 10:  # SHORT4N
        vals = struct.unpack_from("<4h", data, offset)
        return tuple(max(-1.0, v / 32767.0) for v in vals)
    if dtype == 11:  # USHORT2N
        vals = struct.unpack_from("<2H", data, offset)
        return tuple(v / 65535.0 for v in vals)
    if dtype == 12:  # USHORT4N
        vals = struct.unpack_from("<4H", data, offset)
        return tuple(v / 65535.0 for v in vals)
    if dtype == 15:  # FLOAT16_2
        return struct.unpack_from("<2e", data, offset)
    if dtype == 16:  # FLOAT16_4
        return struct.unpack_from("<4e", data, offset)
    raise ValueError(f"Unsupported D3D declaration type {dtype}")


def decode_section_vertices(
    mesh: SM3Mesh,
    section: SM3MeshSection,
    *,
    position_divisor: float = 512.0,
    uv_divisor: float = 1024.0,
) -> Dict[str, object]:
    data = mesh.raw
    out: Dict[str, object] = {
        "positions": [],
        "normals": [],
        "tangents": [],
        "binormals": [],
        "blend_indices": [],
        "blend_weights": [],
        "texcoords": {},
        "colors": {},
    }

    positions: List[Tuple[float, float, float]] = out["positions"]  # type: ignore[assignment]
    normals: List[Tuple[float, float, float]] = out["normals"]  # type: ignore[assignment]
    tangents: List[Tuple[float, float, float]] = out["tangents"]  # type: ignore[assignment]
    binormals: List[Tuple[float, float, float]] = out["binormals"]  # type: ignore[assignment]
    blend_indices: List[Tuple[int, int, int, int]] = out["blend_indices"]  # type: ignore[assignment]
    blend_weights: List[Tuple[float, float, float, float]] = out["blend_weights"]  # type: ignore[assignment]
    texcoords: Dict[int, List[Tuple[float, float]]] = out["texcoords"]  # type: ignore[assignment]
    colors: Dict[int, List[Tuple[float, float, float, float]]] = out["colors"]  # type: ignore[assignment]

    mx, my, mz = mesh.model_offset
    bx, by, bz, _ = mesh.model_bbox

    for vertex_index in range(section.vertex_count):
        base = section.vertex_buffer_offset + vertex_index * section.vertex_stride
        for decl in section.decl:
            raw = _read_dtype(data, base + decl.offset, decl.dtype)
            usage = D3D_USAGE.get(decl.usage, f"USAGE_{decl.usage}")

            if usage == "POSITION":
                if decl.dtype == 7:
                    # SM3 PC player meshes use signed fixed-point SHORT4 values
                    # that already represent model-space positions.  Do NOT run
                    # them through the WOS SHORT4N bbox/offset decode a second time.
                    #
                    # Correct SM3 path:
                    #     position = raw_short / section_divisor
                    rx, ry, rz = raw[:3]
                    positions.append((
                        rx / position_divisor,
                        ry / position_divisor,
                        rz / position_divisor,
                    ))
                elif decl.dtype in (9, 10):
                    rx, ry, rz = raw[:3]
                    positions.append((rx * bx + mx, ry * by + my, rz * bz + mz))
                elif decl.dtype == 6:
                    # Several SM3 LOD/billboard sections use a SHORT2 position.
                    # Blender requires XYZ, so preserve XY exactly and supply Z=0.
                    positions.append((float(raw[0]), float(raw[1]), 0.0))
                elif decl.dtype == 0:
                    positions.append((float(raw[0]), 0.0, 0.0))
                elif decl.dtype == 1:
                    positions.append((float(raw[0]), float(raw[1]), 0.0))
                else:
                    vals = tuple(float(v) for v in raw[:3])
                    positions.append(vals if len(vals) == 3 else vals + (0.0,) * (3 - len(vals)))

            elif usage == "TEXCOORD":
                if decl.dtype == 6:
                    uv = (float(raw[0]) / uv_divisor, float(raw[1]) / uv_divisor)
                else:
                    uv = (float(raw[0]), float(raw[1]))
                texcoords.setdefault(decl.usage_index, []).append(uv)

            elif usage == "COLOR":
                if decl.dtype in (4, 5):
                    color = tuple(float(v) / 255.0 for v in raw[:4])
                else:
                    color = tuple(float(v) for v in raw[:4])
                colors.setdefault(decl.usage_index, []).append(color)  # type: ignore[arg-type]

            elif usage == "NORMAL":
                vals = tuple(float(v) for v in raw[:3])
                if decl.dtype in (4, 5):
                    vals = tuple((v / 127.5) - 1.0 for v in vals)
                normals.append(vals)
            elif usage == "TANGENT":
                vals = tuple(float(v) for v in raw[:3])
                if decl.dtype in (4, 5):
                    vals = tuple((v / 127.5) - 1.0 for v in vals)
                tangents.append(vals)
            elif usage == "BINORMAL":
                vals = tuple(float(v) for v in raw[:3])
                if decl.dtype in (4, 5):
                    vals = tuple((v / 127.5) - 1.0 for v in vals)
                binormals.append(vals)
            elif usage == "BLENDINDICES":
                blend_indices.append(tuple(int(v) for v in raw[:4]))
            elif usage == "BLENDWEIGHT":
                vals = tuple(float(v) for v in raw[:4])
                # Normalize small quantization error to exactly one when usable.
                total = sum(max(0.0, v) for v in vals)
                if total > 1.0e-8:
                    vals = tuple(max(0.0, v) / total for v in vals)
                blend_weights.append(vals)  # type: ignore[arg-type]

    return out


def read_indices(mesh: SM3Mesh, section: SM3MeshSection) -> List[int]:
    data = mesh.raw
    if section.index_size == 2:
        fmt = "<" + "H" * section.index_count
    elif section.index_size == 4:
        fmt = "<" + "I" * section.index_count
    else:
        raise ValueError(f"Section {section.index}: unsupported index size {section.index_size}")
    return list(struct.unpack_from(fmt, data, section.index_buffer_offset))


def triangle_strip_to_faces(indices: Sequence[int], vertex_count: int) -> List[Tuple[int, int, int]]:
    faces: List[Tuple[int, int, int]] = []
    strip: List[int] = []
    restart_values = {0xFFFF, 0xFFFFFFFF}

    def emit(local: Sequence[int]):
        for i in range(len(local) - 2):
            a, b, c = local[i], local[i + 1], local[i + 2]
            if a == b or b == c or a == c:
                continue
            if a >= vertex_count or b >= vertex_count or c >= vertex_count:
                continue
            if i & 1:
                faces.append((a, c, b))
            else:
                faces.append((a, b, c))

    for idx in indices:
        if idx in restart_values or idx >= vertex_count:
            emit(strip)
            strip = []
        else:
            strip.append(idx)
    emit(strip)
    return faces


def indices_to_faces(indices: Sequence[int], primitive_type: int, vertex_count: int) -> List[Tuple[int, int, int]]:
    # D3DPT_TRIANGLELIST = 4, D3DPT_TRIANGLESTRIP = 5
    if primitive_type == 5:
        return triangle_strip_to_faces(indices, vertex_count)
    if primitive_type == 4:
        return [
            (indices[i], indices[i + 1], indices[i + 2])
            for i in range(0, len(indices) - 2, 3)
            if indices[i] < vertex_count and indices[i + 1] < vertex_count and indices[i + 2] < vertex_count
        ]
    # Fallback: many original SM3 resources are strips. Treat unknown modes as
    # strips rather than crashing, but callers should expose the primitive value.
    return triangle_strip_to_faces(indices, vertex_count)


def skeleton_pair_candidate_for_mesh(mesh_path: str | os.PathLike[str]) -> Optional[str]:
    p = Path(mesh_path)
    stem = p.name
    m = re.search(r"\.([^.]*(\d{3}))\.mesh$", stem, re.IGNORECASE)
    if not m:
        return None
    model_name = m.group(1)
    suffix = m.group(2)
    base_name = model_name[:-3]
    skel_name_fragment = f"{base_name}skeleton_{base_name}{suffix}"

    candidates = []
    for root in [p.parent, p.parent.parent / "SKEL", p.parent.parent, p.parent / "SKEL"]:
        if root.exists():
            for cand in root.glob("*.skel"):
                if skel_name_fragment.lower() in cand.name.lower():
                    candidates.append(cand)
    if candidates:
        return str(sorted(candidates)[0])
    return None


POSITION_DIVISOR_CANDIDATES: Tuple[float, ...] = (
    128.0,
    256.0,
    512.0,
    1000.0,
    1024.0,
    2048.0,
    4096.0,
)


def raw_section_position_bounds(
    mesh: SM3Mesh,
    section: SM3MeshSection,
) -> Optional[Tuple[
    Tuple[int, int, int],
    Tuple[int, int, int],
    Tuple[float, float, float],
    Tuple[float, float, float],
]]:
    """Return raw SHORT4 POSITION min/max/center/half-extents for one section."""
    pos_decl = next((d for d in section.decl if d.usage == 0), None)
    if pos_decl is None or pos_decl.dtype != 7:
        return None

    mins = [32767, 32767, 32767]
    maxs = [-32768, -32768, -32768]
    for vertex_index in range(section.vertex_count):
        off = (
            section.vertex_buffer_offset
            + vertex_index * section.vertex_stride
            + pos_decl.offset
        )
        x, y, z, _w = struct.unpack_from("<4h", mesh.raw, off)
        vals = (x, y, z)
        for axis in range(3):
            mins[axis] = min(mins[axis], vals[axis])
            maxs[axis] = max(maxs[axis], vals[axis])

    center = tuple((mins[i] + maxs[i]) * 0.5 for i in range(3))
    half = tuple((maxs[i] - mins[i]) * 0.5 for i in range(3))
    return (
        tuple(mins),
        tuple(maxs),
        center,
        half,
    )


def section_position_divisor_error(
    mesh: SM3Mesh,
    section: SM3MeshSection,
    divisor: float,
) -> float:
    """Score one SHORT4 divisor against the section's stored center/bbox metadata.

    For SM3 character sections, MeshInfo.mesh_offset behaves as the section
    center and MeshInfo.mesh_bbox.xyz behaves as the section half-extents.

    Some sections are subsets whose stored metadata is broader than the actual
    vertex subset, so this is intentionally a *relative candidate* score rather
    than a claim that every section should produce near-zero error.
    """
    bounds = raw_section_position_bounds(mesh, section)
    if bounds is None:
        return 0.0

    _mins, _maxs, raw_center, raw_half = bounds
    pred_center = tuple(v / divisor for v in raw_center)
    pred_half = tuple(v / divisor for v in raw_half)

    # Absolute-space error is deliberately simple and robust.  It strongly
    # separates the real CH_SPIDERMAN 1/512 sections from the old double-
    # transformed decode.  It also identifies the unusual 001 section 0 scale.
    center_err = sum(
        abs(pred_center[i] - section.mesh_offset[i])
        for i in range(3)
    )
    half_err = sum(
        abs(pred_half[i] - section.mesh_bbox[i])
        for i in range(3)
    )
    return center_err + half_err


def choose_section_position_divisor(
    mesh: SM3Mesh,
    section: SM3MeshSection,
    candidates: Sequence[float] = POSITION_DIVISOR_CANDIDATES,
) -> float:
    """Choose the best fixed-point divisor independently for one SM3 section."""
    pos_decl = next((d for d in section.decl if d.usage == 0), None)
    if pos_decl is None or pos_decl.dtype != 7:
        return 1.0

    return min(
        candidates,
        key=lambda divisor: section_position_divisor_error(mesh, section, divisor),
    )


def choose_position_divisors(mesh: SM3Mesh) -> List[float]:
    """Return one fixed-point position divisor per section."""
    return [choose_section_position_divisor(mesh, sec) for sec in mesh.sections]


def position_divisor_diagnostics(mesh: SM3Mesh) -> List[Dict[str, object]]:
    """Return per-section divisor and raw/stored bounds diagnostics."""
    rows: List[Dict[str, object]] = []
    for sec in mesh.sections:
        bounds = raw_section_position_bounds(mesh, sec)
        if bounds is None:
            rows.append({
                "section": sec.index,
                "position_type": None,
                "divisor": 1.0,
                "error": 0.0,
            })
            continue

        raw_min, raw_max, raw_center, raw_half = bounds
        divisor = choose_section_position_divisor(mesh, sec)
        rows.append({
            "section": sec.index,
            "position_type": 7,
            "divisor": divisor,
            "error": section_position_divisor_error(mesh, sec, divisor),
            "raw_min": raw_min,
            "raw_max": raw_max,
            "raw_center": raw_center,
            "raw_half": raw_half,
            "stored_center": sec.mesh_offset,
            "stored_half": sec.mesh_bbox[:3],
            "decoded_center": tuple(v / divisor for v in raw_center),
            "decoded_half": tuple(v / divisor for v in raw_half),
        })
    return rows


def choose_position_divisor(
    mesh: SM3Mesh,
    skeleton_bind_positions: Optional[Sequence[Tuple[float, float, float]]] = None,
) -> float:
    """Compatibility helper retained for older callers.

    v0.1.1 no longer applies one divisor to the whole model in AUTO mode.
    Return the median per-section choice for legacy code.
    """
    values = choose_position_divisors(mesh)
    return float(statistics.median(values)) if values else 512.0
