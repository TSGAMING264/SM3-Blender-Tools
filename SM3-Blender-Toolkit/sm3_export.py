from __future__ import annotations

"""WoS-style direct SM3 mesh exporter.

Core rule copied from the upstream WoS Blender Toolkit design:

    one Blender MESH object == one game mesh entry/section

SM3 differs from WoS in two important ways:
- loose SM3 .mesh files do not carry a WRAP external-patch table;
- XESM3 overlays the loose geometry onto the stock runtime resource.

Therefore the imported SM3 collection still owns the exact resource identity and
provides native section schemas/material-reference provenance, but the exporter
NEVER forces replacement geometry to match the stock section count.  Native
objects keep their own source section profile.  Arbitrary replacement objects
use target section profiles by object order.  A mesh object is split only when
its used bone palette would exceed XESM3/SM3's proven 48-bone safe limit.
"""

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

import bpy
import bmesh
from mathutils import Vector

from .sm3_format import choose_position_divisors
from .sm3_mesh_writer import ExportSection, ExportVertex, write_raw_mesh
from .target_cache import cached_position_divisors, load_target_template

BONE_GROUP_RE = re.compile(r"^bone_(\d+)$", re.IGNORECASE)
NATIVE_SECTION_RE = re.compile(r"SM3_MeshObject_(\d+)", re.IGNORECASE)


def _u32(value, default=0):
    if value is None:
        return int(default) & 0xFFFFFFFF
    if isinstance(value, str):
        try:
            return int(value, 0) & 0xFFFFFFFF
        except Exception:
            return int(default) & 0xFFFFFFFF
    try:
        return int(value) & 0xFFFFFFFF
    except Exception:
        return int(default) & 0xFFFFFFFF


def _native_section_index(obj) -> Optional[int]:
    """Return exact section provenance for an imported SM3 object."""
    for container in (obj, getattr(obj, "data", None)):
        if container is None:
            continue
        value = container.get("sm3_section_index")
        if value is not None:
            try:
                return int(value)
            except Exception:
                pass
    m = NATIVE_SECTION_RE.search(str(getattr(obj, "name", "")))
    return int(m.group(1)) if m else None


def _find_armature(obj):
    for mod in obj.modifiers:
        if mod.type == "ARMATURE" and mod.object is not None:
            return mod.object
    if obj.parent and obj.parent.type == "ARMATURE":
        return obj.parent
    for col in obj.users_collection:
        for candidate in col.objects:
            if candidate.type == "ARMATURE":
                return candidate
    return None


def _bone_index_for_group(obj, group_index: int, armature) -> Optional[int]:
    if group_index < 0 or group_index >= len(obj.vertex_groups):
        return None
    vg = obj.vertex_groups[group_index]
    m = BONE_GROUP_RE.match(str(vg.name))
    if m:
        return int(m.group(1))

    if armature is not None and armature.type == "ARMATURE":
        bone = armature.data.bones.get(vg.name)
        if bone is not None:
            value = bone.get("sm3_index")
            if value is not None:
                try:
                    return int(value)
                except Exception:
                    pass
            try:
                return list(armature.data.bones).index(bone)
            except Exception:
                pass
    return None


def _vertex_influences(obj, vertex, armature, warnings: List[str]):
    pairs = []
    for membership in vertex.groups:
        if float(membership.weight) <= 1.0e-8:
            continue
        bone_index = _bone_index_for_group(obj, int(membership.group), armature)
        if bone_index is None:
            continue
        pairs.append((int(bone_index), float(membership.weight)))

    pairs.sort(key=lambda row: row[1], reverse=True)
    pairs = pairs[:4]
    total = sum(weight for _bone, weight in pairs)
    if total <= 1.0e-12:
        # XESM3's NativeMESH safety parser currently requires a non-zero palette
        # count. bone_0 is harmless for target schemas that do not use skinning.
        return ((0, 1.0),)
    return tuple((bone, weight / total) for bone, weight in pairs)


def _triangulated_mesh(obj):
    mesh = obj.data.copy()
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    bmesh.ops.triangulate(bm, faces=list(bm.faces))
    bm.normal_update()
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    if len(mesh.uv_layers):
        try:
            mesh.calc_tangents(uvmap=mesh.uv_layers.active.name)
        except Exception:
            pass
    return mesh


def _color_for_loop(mesh, loop_index: int, vertex_index: int, channel: int):
    if not hasattr(mesh, "color_attributes") or len(mesh.color_attributes) == 0:
        return (1.0, 1.0, 1.0, 1.0)
    layer = mesh.color_attributes.get(f"Col_{channel}")
    if layer is None:
        if channel != 0:
            return (1.0, 1.0, 1.0, 1.0)
        layer = mesh.color_attributes.active_color or mesh.color_attributes[0]
    try:
        item = layer.data[loop_index] if layer.domain == "CORNER" else layer.data[vertex_index]
        values = getattr(item, "color_srgb", None)
        if values is None:
            values = item.color
        values = tuple(float(v) for v in values)
        return values[:4] if len(values) >= 4 else values[:3] + (1.0,)
    except Exception:
        return (1.0, 1.0, 1.0, 1.0)


def _build_triangle_records(obj, *, flip_uv_v: bool, reverse_winding: bool, warnings: List[str]):
    """Extract Blender triangles exactly once, WoS-style."""
    mesh = _triangulated_mesh(obj)
    armature = _find_armature(obj)
    matrix = obj.matrix_world.copy()
    try:
        normal_matrix = matrix.to_3x3().inverted().transposed()
    except Exception:
        normal_matrix = matrix.to_3x3()
    tangent_matrix = matrix.to_3x3().normalized()

    uv_layers = list(mesh.uv_layers)
    records = []
    try:
        for poly in mesh.polygons:
            if len(poly.loop_indices) != 3:
                continue
            loops = list(poly.loop_indices)
            if reverse_winding:
                loops = [loops[0], loops[2], loops[1]]

            corners = []
            tri_bones: Set[int] = set()
            for li in loops:
                loop = mesh.loops[li]
                vertex = mesh.vertices[loop.vertex_index]
                pos = matrix @ vertex.co
                normal = (normal_matrix @ loop.normal).normalized()

                tangent = Vector((1.0, 0.0, 0.0))
                binormal = Vector((0.0, 1.0, 0.0))
                try:
                    tangent = (tangent_matrix @ loop.tangent).normalized()
                    binormal = normal.cross(tangent) * float(loop.bitangent_sign)
                    if binormal.length > 1.0e-12:
                        binormal.normalize()
                except Exception:
                    pass

                uv_values = []
                for layer in uv_layers[:3]:
                    u, v = (float(x) for x in layer.data[li].uv)
                    if flip_uv_v:
                        v = 1.0 - v
                    uv_values.append((u, v))
                while len(uv_values) < 3:
                    uv_values.append(uv_values[-1] if uv_values else (0.0, 0.0))

                influences = _vertex_influences(obj, vertex, armature, warnings)
                tri_bones.update(b for b, w in influences if w > 1.0e-8)

                corners.append(ExportVertex(
                    position=(float(pos.x), float(pos.y), float(pos.z)),
                    uv=uv_values[0],
                    uv1=uv_values[1],
                    uv2=uv_values[2],
                    color=_color_for_loop(mesh, li, loop.vertex_index, 0),
                    color1=_color_for_loop(mesh, li, loop.vertex_index, 1),
                    normal=(float(normal.x), float(normal.y), float(normal.z)),
                    tangent=(float(tangent.x), float(tangent.y), float(tangent.z)),
                    binormal=(float(binormal.x), float(binormal.y), float(binormal.z)),
                    influences=influences,
                ))

            records.append({"corners": tuple(corners), "bones": tri_bones})
    finally:
        bpy.data.meshes.remove(mesh)
    return records


def _split_records_for_bones(records, max_bones: int):
    """WoS keeps one object as one entry; SM3 only splits when 48 bones require it."""
    chunks = []
    current = []
    current_bones: Set[int] = set()

    for tri in records:
        tri_bones = set(int(b) for b in tri["bones"])
        if len(tri_bones) > max_bones:
            raise ValueError(
                f"One triangle references {len(tri_bones)} bones; SM3 safe limit is {max_bones}"
            )
        candidate = current_bones | tri_bones
        if current and len(candidate) > max_bones:
            chunks.append((current, set(current_bones)))
            current = []
            current_bones = set()
        current.append(tri)
        current_bones.update(tri_bones)

    if current:
        chunks.append((current, set(current_bones)))
    return chunks


def _vertex_key(vertex: ExportVertex):
    def rr(values):
        return tuple(round(float(v), 7) for v in values)
    return (
        rr(vertex.position), rr(vertex.uv),
        rr(vertex.uv1 or vertex.uv), rr(vertex.uv2 or vertex.uv1 or vertex.uv),
        rr(vertex.color), rr(vertex.color1 or vertex.color),
        rr(vertex.normal), rr(vertex.tangent), rr(vertex.binormal),
        tuple((int(b), round(float(w), 7)) for b, w in vertex.influences),
    )


def _records_to_geometry(records):
    vertices: List[ExportVertex] = []
    triangles: List[Tuple[int, int, int]] = []
    lookup = {}
    for rec in records:
        tri = []
        for corner in rec["corners"]:
            key = _vertex_key(corner)
            idx = lookup.get(key)
            if idx is None:
                idx = len(vertices)
                lookup[key] = idx
                vertices.append(corner)
            tri.append(idx)
        if len(tri) == 3 and len(set(tri)) == 3:
            triangles.append((tri[0], tri[1], tri[2]))
    return vertices, triangles


def _native_piece_layout_is_intact(objects, template_count: int) -> bool:
    """Return True only when the Blender object split still matches the imported SM3 split.

    This is the important WoS-style join rule.  Blender Join keeps the active
    object's custom properties, including sm3_section_index.  That stale index
    must NOT make a joined/rebuilt model pretend it is still one untouched native
    section.  Native section provenance is trusted only when the complete original
    section set is still represented one-for-one.
    """
    if template_count <= 0 or len(objects) != int(template_count):
        return False
    indices = []
    for obj in objects:
        native = _native_section_index(obj)
        if native is None:
            return False
        native = int(native)
        if native < 0 or native >= int(template_count):
            return False
        indices.append(native)
    return sorted(indices) == list(range(int(template_count)))


def _profile_index(obj, object_ordinal: int, template_count: int, trust_native: bool) -> int:
    if template_count <= 0:
        raise ValueError("SM3 target has no sections")
    if trust_native:
        native = _native_section_index(obj)
        if native is not None and 0 <= int(native) < int(template_count):
            return int(native)
    # WoS-style joined/rebuilt fallback: CURRENT Blender object order defines
    # output sections.  Old section IDs retained by Blender Join are ignored.
    return int(object_ordinal) % int(template_count)


def _object_material_ref(obj, template_sec, trust_native: bool) -> int:
    """Preserve exact per-piece ref only while the native piece split is intact."""
    if trust_native:
        for container in (obj, getattr(obj, "data", None)):
            if container is None:
                continue
            value = container.get("sm3_serialized_material_ref")
            if value not in (None, ""):
                return _u32(value, template_sec.material_ref_serialized)
    return int(template_sec.material_ref_serialized) & 0xFFFFFFFF


def _object_position_divisor(obj, fallback: float, trust_native: bool) -> float:
    if trust_native:
        for container in (obj, getattr(obj, "data", None)):
            if container is None:
                continue
            value = container.get("sm3_position_divisor")
            if value not in (None, ""):
                try:
                    value = float(value)
                    if value > 0.0:
                        return value
                except Exception:
                    pass
    return float(fallback)


def export_objects_to_target_mesh(
    mesh_objects: Sequence[bpy.types.Object],
    target_collection: bpy.types.Collection,
    output_path: str,
    *,
    max_bones: int = 48,
    flip_uv_v: bool = True,
    reverse_winding: bool = True,
    write_report: bool = True,
):
    """Export SM3 using the same object-driven model as the WoS toolkit.

    There is deliberately NO target-section preservation/partition step here.
    Number of output sections is determined by the Blender geometry itself.
    """
    if target_collection is None:
        raise ValueError("Import an SM3 MESH first; its collection owns the game target identity")

    objects = []
    seen = set()
    for obj in mesh_objects or ():
        if obj is None or obj.type != "MESH":
            continue
        ptr = obj.as_pointer()
        if ptr in seen:
            continue
        seen.add(ptr)
        objects.append(obj)
    if not objects:
        raise ValueError("SM3 collection contains no MESH objects")

    template = load_target_template(target_collection, objects)
    target_hash = _u32(target_collection.get("sm3_mesh_hash"), template.filename_hash)
    if target_hash != int(template.filename_hash):
        raise ValueError(
            f"Target identity mismatch: collection=0x{target_hash:08X} template=0x{template.filename_hash:08X}"
        )

    divisors = cached_position_divisors(target_collection, len(template.sections))
    if not divisors:
        divisors = choose_position_divisors(template)

    warnings: List[str] = []
    sections: List[ExportSection] = []
    section_decisions = []

    # If the complete imported piece set still exists one-for-one, preserve each
    # piece's exact native section provenance.  If pieces were JOINED, deleted,
    # replaced, duplicated, or otherwise reorganized, switch to WoS-style direct
    # object export and ignore stale section IDs left behind by Blender Join.
    native_piece_layout = _native_piece_layout_is_intact(objects, len(template.sections))
    layout_mode = "NATIVE_ORIGINAL_PIECES" if native_piece_layout else "WOS_DIRECT_JOINED_OR_REBUILT"

    # IMPORTANT: preserve collection object order exactly, matching WoS.
    for object_ordinal, obj in enumerate(objects):
        records = _build_triangle_records(
            obj,
            flip_uv_v=flip_uv_v,
            reverse_winding=reverse_winding,
            warnings=warnings,
        )
        if not records:
            warnings.append(f"{obj.name}: no triangles after triangulation; skipped")
            continue

        profile_index = _profile_index(obj, object_ordinal, len(template.sections), native_piece_layout)
        template_sec = template.sections[profile_index]
        fallback_divisor = float(divisors[profile_index]) if profile_index < len(divisors) else 512.0
        divisor = _object_position_divisor(obj, fallback_divisor, native_piece_layout)
        material_ref = _object_material_ref(obj, template_sec, native_piece_layout)

        chunks = _split_records_for_bones(records, int(max_bones))
        for chunk_index, (chunk_records, bone_set) in enumerate(chunks):
            vertices, triangles = _records_to_geometry(chunk_records)
            if not vertices or not triangles:
                continue
            palette = sorted(int(b) for b in bone_set)
            if not palette:
                palette = [0]

            sec = ExportSection(
                source_object=obj.name,
                source_material_index=0,
                material_ref_serialized=material_ref,
                vertices=vertices,
                triangles=triangles,
                bone_palette=palette,
                position_divisor=divisor,
                primitive_type=4,  # triangle list, same as proven XESM3 custom MESH
                primitive_unknown=int(template_sec.primitive_unknown),
                unknown_30=int(template_sec.unknown_30),
                unknown_38=int(template_sec.unknown_38),
                unknown_40=int(template_sec.unknown_40),
                schema_template_section=profile_index,
                warnings=[],
            )
            sections.append(sec)
            section_decisions.append({
                "output_section": len(sections) - 1,
                "source_object": obj.name,
                "object_ordinal": object_ordinal,
                "bone_chunk": chunk_index,
                "native_source_section": _native_section_index(obj),
                "native_provenance_trusted": bool(native_piece_layout),
                "target_profile_section": profile_index,
                "material_ref_serialized": f"0x{material_ref:08X}",
                "vertex_stride": int(template_sec.vertex_stride),
                "vertex_count": len(vertices),
                "triangle_count": len(triangles),
                "bone_palette_count": len(palette),
            })

    if not sections:
        raise ValueError("No exportable SM3 sections were produced")

    result = write_raw_mesh(
        output_path,
        template,
        sections,
        filename_hash=template.filename_hash,
        player_target="AUTO",
        geometry_profile="TARGET_NATIVE",
    )

    if int(result.filename_hash) != int(template.filename_hash):
        try:
            os.remove(output_path)
        except Exception:
            pass
        raise ValueError(
            f"Exporter wrote wrong internal hash 0x{int(result.filename_hash):08X}; expected 0x{int(template.filename_hash):08X}"
        )

    result.report.update({
        "export_mode": "WOS_STYLE_DIRECT_OBJECT_SECTIONS_V111",
        "target_collection": target_collection.name,
        "target_filename_hash": f"0x{template.filename_hash:08X}",
        "target_stock_section_count": len(template.sections),
        "source_object_count": len(objects),
        "output_section_count": len(sections),
        "layout_mode": layout_mode,
        "native_piece_layout_intact": bool(native_piece_layout),
        "rule": "ONE_CURRENT_BLENDER_MESH_OBJECT_EQUALS_ONE_SM3_SECTION; ORIGINAL_PIECES_KEEP_NATIVE_PROVENANCE; JOINED_OR_REBUILT_OBJECTS_IGNORE_STALE_SECTION_IDS; SPLIT_ONLY_FOR_48_BONE_LIMIT",
        "section_decisions": section_decisions,
        "flip_uv_v": bool(flip_uv_v),
        "reverse_winding": bool(reverse_winding),
        "warnings": warnings,
    })

    if write_report:
        report_path = str(Path(output_path).with_suffix(Path(output_path).suffix + ".export.json"))
        Path(report_path).write_text(json.dumps(result.report, indent=2), encoding="utf-8")
        result.report["report_path"] = report_path
    return result
