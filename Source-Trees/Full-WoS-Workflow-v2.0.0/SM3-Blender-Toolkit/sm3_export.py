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
from dataclasses import replace
from typing import Dict, List, Optional, Sequence, Set, Tuple

import bpy
import bmesh
from mathutils import Vector

from .sm3_format import choose_position_divisors, parse_mesh
from .sm3_mesh_writer import ExportSection, ExportVertex, write_raw_mesh
from .target_cache import cached_position_divisors, load_target_template, cached_snapshot_text, mesh_from_snapshot, bundled_template_path_for_hash

BONE_GROUP_RE = re.compile(r"^bone_(\d+)$", re.IGNORECASE)
NATIVE_SECTION_RE = re.compile(r"SM3_MeshObject_(\d+)", re.IGNORECASE)

SPIDER_ATLAS_BUILD = "1.3.8"


def _scaled_uv_pair(pair, scale: float):
    if pair is None:
        return None
    return (float(pair[0]) * float(scale), float(pair[1]) * float(scale))


def _write_raw_mesh_spider_uv1024(output_path, template, sections, **kwargs):
    """Write dedicated Spider atlas geometry with an effective SHORT2 UV scale of 1024.

    Blender can retain an older sm3_mesh_writer module in memory when an add-on
    ZIP is reinstalled without fully restarting Blender.  Older writer builds do
    not accept the ``uv_divisor`` keyword and always serialize UVs with *1000.

    New writers receive uv_divisor=1024 directly.  Legacy writers are supported
    without changing the model: a temporary export-only copy of the section
    vertices is pre-scaled by 1024/1000, then the legacy *1000 serializer produces
    the exact same integer TEXCOORD values as a native *1024 writer.
    """
    try:
        result = write_raw_mesh(
            output_path, template, sections, uv_divisor=1024.0, **kwargs
        )
        result.report['uv_encoding'] = 'SHORT2 uv * 1024'
        result.report['uv_writer_compat_mode'] = 'NATIVE_UV_DIVISOR_1024'
        return result
    except TypeError as exc:
        message = str(exc)
        if 'uv_divisor' not in message or 'unexpected keyword' not in message:
            raise

    scale = 1024.0 / 1000.0
    compat_sections = []
    for sec in sections:
        verts = []
        for v in sec.vertices:
            verts.append(replace(
                v,
                uv=_scaled_uv_pair(v.uv, scale),
                uv1=_scaled_uv_pair(v.uv1, scale),
                uv2=_scaled_uv_pair(v.uv2, scale),
            ))
        compat_sections.append(replace(sec, vertices=verts))

    result = write_raw_mesh(output_path, template, compat_sections, **kwargs)
    # The legacy writer's report describes its internal multiplier (1000), but
    # the temporary 1.024 pre-scale makes the serialized integers equivalent to
    # SHORT2 uv * 1024 from the original, unmodified SM3_ATLAS coordinates.
    result.report['uv_encoding'] = 'SHORT2 uv * 1024'
    result.report['uv_writer_compat_mode'] = 'LEGACY_1000_PRESCALED_TO_EFFECTIVE_1024'
    return result


# Validated 32-bone palette from the working Spider atlas export supplied for
# comparison.  The Spider canvas material only receives 32 matrices.  Saved
# projects may carry weights on the full 75-bone SM3 rig, so atlas export
# collapses non-canvas helper/twist/finger-detail bones to their nearest parent
# that exists in this proven palette.  This is export-only; Blender weights are
# not destructively edited.
SPIDER_CANVAS_SAFE_BONES = (
    0, 1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13, 15, 21, 24,
    36, 37, 38, 39, 40, 41, 43, 49, 52, 66, 67, 68, 69,
    71, 72, 73, 74,
)
SPIDER_CANVAS_SAFE_BONE_SET = set(SPIDER_CANVAS_SAFE_BONES)

# Parent fallback for ch_spiderman000/001 (75-bone rig), used only when the
# imported armature is unavailable.  Normally we read parent indices directly
# from the Blender armature's sm3_index properties.
SPIDER_000_PARENT = (
    -1,0,1,2,3,4,5,5,4,8,9,10,11,12,13,11,15,16,11,18,19,11,21,22,11,
    24,25,11,9,28,9,9,9,9,9,8,4,36,37,38,39,40,41,39,43,44,39,46,47,39,
    49,50,39,52,53,39,37,56,37,37,37,37,37,36,0,0,0,66,67,68,0,0,71,72,73,
)


def _spider_parent_map(obj):
    arm = _find_armature(obj)
    out = {}
    if arm is not None and getattr(arm, 'type', None) == 'ARMATURE':
        bones = list(arm.data.bones)
        idx_by_ptr = {}
        for order, bone in enumerate(bones):
            raw = bone.get('sm3_index')
            try:
                idx = int(raw) if raw is not None else int(order)
            except Exception:
                idx = int(order)
            idx_by_ptr[bone.as_pointer()] = idx
        for order, bone in enumerate(bones):
            raw = bone.get('sm3_index')
            try:
                idx = int(raw) if raw is not None else int(order)
            except Exception:
                idx = int(order)
            if bone.parent is None:
                out[idx] = -1
            else:
                out[idx] = int(idx_by_ptr.get(bone.parent.as_pointer(), -1))
    if not out:
        out = {i: int(p) for i, p in enumerate(SPIDER_000_PARENT)}
    return out


def _collapse_spider_records_to_32(records, obj):
    """Collapse full-rig influences to the proven Spider canvas 32-bone set."""
    parents = _spider_parent_map(obj)
    remap = {}

    def map_bone(b):
        b = int(b)
        if b in remap:
            return remap[b]
        cur = b
        seen = set()
        while cur not in SPIDER_CANVAS_SAFE_BONE_SET and cur >= 0 and cur not in seen:
            seen.add(cur)
            cur = int(parents.get(cur, -1))
        if cur < 0 or cur not in SPIDER_CANVAS_SAFE_BONE_SET:
            cur = 0
        remap[b] = int(cur)
        return int(cur)

    for rec in records:
        tri_bones = set()
        for corner in rec.get('corners', ()):
            accum = {}
            for bone, weight in corner.influences:
                target = map_bone(bone)
                accum[target] = accum.get(target, 0.0) + float(weight)
            pairs = sorted(accum.items(), key=lambda kv: kv[1], reverse=True)[:4]
            total = sum(w for _b, w in pairs)
            if total <= 1.0e-12:
                pairs = [(0, 1.0)]
                total = 1.0
            corner.influences = tuple((int(b), float(w) / total) for b, w in pairs)
            tri_bones.update(int(b) for b, w in corner.influences if w > 1.0e-8)
        rec['bones'] = tri_bones
    return remap



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


def _triangulated_mesh(obj, *, tangent_uv_layer_name: str | None = None, preflip_tangent_uv_v: bool = False):
    """Return a triangulated copy and calculate tangents from the requested UV.

    v1.3.5 could serialize the packed atlas UV as TEXCOORD0 while Blender had
    calculated loop tangents from a different active UV layer.  That creates a
    TBN basis that does not correspond to the UVs the game actually samples.
    Dedicated Spider atlas export now asks for the exact packed atlas layer.
    """
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
            uv_name = str(tangent_uv_layer_name or '').strip()
            if not uv_name:
                uv_name = mesh.uv_layers.active.name if mesh.uv_layers.active is not None else mesh.uv_layers[0].name
            tangent_uv_layer = mesh.uv_layers.get(uv_name)
            if tangent_uv_layer is None:
                raise ValueError(f'Tangent UV layer {uv_name!r} is missing')

            # v1.3.7: WoS flips the UVs BEFORE Blender calculates tangents.
            # Our SM3 exporter historically flipped V only while serializing,
            # so v1.3.6 calculated a tangent basis for (u, v) but sent
            # TEXCOORD0 as (u, 1-v).  The diffuse could look correct while the
            # TBN basis still described a different UV parameterization.
            #
            # This mesh is an export-only copy, so it is safe to pre-flip the
            # exact tangent/TEXCOORD0 layer here.  Serialization below detects
            # this and does not flip that layer a second time.
            if preflip_tangent_uv_v:
                for uv_item in tangent_uv_layer.data:
                    uv_item.uv.y = 1.0 - float(uv_item.uv.y)

            mesh.calc_tangents(uvmap=uv_name)
        except Exception:
            # Keep the historical fallback behavior for generic exports.
            try:
                mesh.calc_tangents()
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


def _build_triangle_records(
    obj,
    *,
    flip_uv_v: bool,
    reverse_winding: bool,
    warnings: List[str],
    preferred_uv_layer_name: str | None = None,
    wos_compatible_tbn: bool = False,
):
    """Extract Blender triangles exactly once, WoS-style.

    For Spider baked-atlas exports, Blender can contain both the packed atlas UV
    and the preserved source UV layer (``SM3_SRC``).  Blender's viewport uses
    the active-render layer, while older exporter builds simply serialized the
    first UV layer in the mesh.  That made the model look correct in Blender
    but sample unrelated atlas regions in-game whenever layer order differed.

    ``preferred_uv_layer_name`` explicitly selects the packed atlas UV as
    TEXCOORD0 and keeps any other UV layers secondary.

    ``wos_compatible_tbn`` is the v1.3.6 test path.  It mirrors the proven WoS
    Blender exporter: smooth vertex normal + loop tangent +
    ``cross(normal, tangent) * -bitangent_sign``.  This is intentionally limited
    to the dedicated Spider atlas exporters so normal SM3 export behavior does
    not change.
    """
    preflipped_tangent_uv = bool(wos_compatible_tbn and flip_uv_v and preferred_uv_layer_name)
    mesh = _triangulated_mesh(
        obj,
        tangent_uv_layer_name=preferred_uv_layer_name,
        preflip_tangent_uv_v=preflipped_tangent_uv,
    )
    armature = _find_armature(obj)
    matrix = obj.matrix_world.copy()
    try:
        normal_matrix = matrix.to_3x3().inverted().transposed()
    except Exception:
        normal_matrix = matrix.to_3x3()
    tangent_matrix = matrix.to_3x3().normalized()

    uv_layers = list(mesh.uv_layers)
    if preferred_uv_layer_name:
        preferred = mesh.uv_layers.get(str(preferred_uv_layer_name))
        if preferred is None:
            available = ", ".join(layer.name for layer in uv_layers) or "NONE"
            raise ValueError(
                f"Prepared Spider atlas UV layer {preferred_uv_layer_name!r} was not found; "
                f"available UV layers: {available}"
            )
        uv_layers = [preferred] + [layer for layer in uv_layers if layer.name != preferred.name]
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
                if wos_compatible_tbn:
                    # Match WoS BlenderToolkit/BlenderMesh.py as closely as the
                    # SM3 coordinate path allows: use the smooth per-vertex normal
                    # instead of a split loop normal, and invert Blender's
                    # bitangent sign when reconstructing the binormal.
                    try:
                        source_normal = mesh.vertex_normals[loop.vertex_index].vector
                    except Exception:
                        source_normal = loop.normal
                    normal = (normal_matrix @ source_normal).normalized()
                else:
                    normal = (normal_matrix @ loop.normal).normalized()

                tangent = Vector((1.0, 0.0, 0.0))
                binormal = Vector((0.0, 1.0, 0.0))
                try:
                    tangent = (tangent_matrix @ loop.tangent).normalized()
                    sign = float(loop.bitangent_sign)
                    if wos_compatible_tbn:
                        sign = -sign
                    binormal = normal.cross(tangent) * sign
                    if binormal.length > 1.0e-12:
                        binormal.normalize()
                except Exception:
                    pass

                uv_values = []
                for layer in uv_layers[:3]:
                    u, v = (float(x) for x in layer.data[li].uv)
                    # v1.3.7: the packed Spider atlas layer was already flipped
                    # on this export-only mesh copy BEFORE calc_tangents so its
                    # TBN basis matches the exact TEXCOORD0 sent to SM3.
                    already_preflipped = bool(
                        preflipped_tangent_uv
                        and preferred_uv_layer_name
                        and layer.name == str(preferred_uv_layer_name)
                    )
                    if flip_uv_v and not already_preflipped:
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



def _tbn_handedness_summary(records):
    """Small export diagnostic: handedness of serialized N/T/B basis."""
    right = left = degenerate = 0
    for rec in records:
        for v in rec.get('corners', ()):
            try:
                n = Vector(v.normal).normalized()
                t = Vector(v.tangent).normalized()
                b = Vector(v.binormal).normalized()
                value = n.cross(t).dot(b)
                if value > 0.5:
                    right += 1
                elif value < -0.5:
                    left += 1
                else:
                    degenerate += 1
            except Exception:
                degenerate += 1
    total = right + left + degenerate
    return {
        'right_handed_corners': right,
        'left_handed_corners': left,
        'degenerate_corners': degenerate,
        'total_corners': total,
        'right_handed_ratio': (right / total) if total else 0.0,
        'left_handed_ratio': (left / total) if total else 0.0,
    }

def _vertex_color_summary(records):
    """Summarize the actual corner color values that will be serialized."""
    values = []
    for rec in records:
        for v in rec.get('corners', ()):
            try:
                rgba = tuple(round(float(c), 6) for c in tuple(v.color)[:4])
                if len(rgba) == 4:
                    values.append(rgba)
            except Exception:
                pass
    unique = sorted(set(values))
    if not values:
        mode = 'NONE'
    elif len(unique) == 1 and unique[0] == (0.0, 0.0, 0.0, 1.0):
        mode = 'BLACK'
    elif len(unique) == 1 and unique[0] == (1.0, 1.0, 1.0, 1.0):
        mode = 'WHITE'
    else:
        mode = 'MIXED'
    return {
        'mode': mode,
        'corner_count': len(values),
        'unique_count': len(unique),
        'unique_rgba_sample': [list(x) for x in unique[:8]],
    }


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



def _split_records_for_bones_smart(records, max_bones: int):
    """Partition triangles into a small number of palette-safe sections without remapping weights.

    The old Spider canvas path forced the entire character into one 32-bone
    palette by collapsing unsupported bones to parents.  That changed the skin
    deformation seen in-game.  This splitter keeps every original global bone
    index + weight exactly as Blender supplied it and only duplicates vertices
    at section boundaries.

    We try several deterministic best-fit orders, merge compatible bins, and
    keep the candidate with the fewest sections / least palette duplication.
    """
    limit = int(max_bones)
    if limit <= 0:
        raise ValueError('max_bones must be > 0')
    items = []
    bone_frequency = {}
    for order, rec in enumerate(records):
        bones = set(int(b) for b in rec.get('bones', ()))
        if len(bones) > limit:
            raise ValueError(
                f'One triangle references {len(bones)} bones; Spider canvas section limit is {limit}'
            )
        for bone in bones:
            bone_frequency[bone] = bone_frequency.get(bone, 0) + 1
        items.append((order, rec, bones))

    if not items:
        return []

    def rarity_score(bones):
        # Prefer hard/rare combinations first so they do not get stranded into
        # extra tiny bins late in the pass.
        return sum(1.0 / max(1, bone_frequency.get(b, 1)) for b in bones)

    orders = [
        list(items),
        sorted(items, key=lambda x: (-len(x[2]), -rarity_score(x[2]), x[0])),
        sorted(items, key=lambda x: (-rarity_score(x[2]), -len(x[2]), x[0])),
        sorted(items, key=lambda x: (min(x[2]) if x[2] else -1, max(x[2]) if x[2] else -1, x[0])),
    ]

    def pack(ordered):
        bins = []
        for original_index, rec, bones in ordered:
            best = None
            best_key = None
            for i, bin_ in enumerate(bins):
                union = bin_['bones'] | bones
                if len(union) > limit:
                    continue
                added = len(union) - len(bin_['bones'])
                overlap = len(bin_['bones'] & bones)
                # Fewer added bones first, then more overlap, then fuller bin,
                # then stable bin order.
                key = (added, -overlap, -len(bin_['bones']), i)
                if best_key is None or key < best_key:
                    best_key = key
                    best = i
            if best is None:
                bins.append({'records': [(original_index, rec)], 'bones': set(bones)})
            else:
                bins[best]['records'].append((original_index, rec))
                bins[best]['bones'].update(bones)

        # Merge any pair whose combined palette still fits. Choose the merge
        # that removes the most palette duplication first.
        changed = True
        while changed and len(bins) > 1:
            changed = False
            choice = None
            choice_key = None
            for i in range(len(bins)):
                for j in range(i + 1, len(bins)):
                    union = bins[i]['bones'] | bins[j]['bones']
                    if len(union) > limit:
                        continue
                    overlap = len(bins[i]['bones'] & bins[j]['bones'])
                    key = (-overlap, len(union), -(len(bins[i]['records']) + len(bins[j]['records'])), i, j)
                    if choice_key is None or key < choice_key:
                        choice_key = key
                        choice = (i, j, union)
            if choice is not None:
                i, j, union = choice
                bins[i]['records'].extend(bins[j]['records'])
                bins[i]['bones'] = union
                del bins[j]
                changed = True

        # Try to dissolve small bins triangle-by-triangle into the others.
        progress = True
        while progress and len(bins) > 1:
            progress = False
            for src_index in sorted(range(len(bins)), key=lambda i: len(bins[i]['records'])):
                if src_index >= len(bins):
                    continue
                src_records = list(bins[src_index]['records'])
                placements = []
                shadow_bones = [set(b['bones']) for b in bins]
                possible = True
                for original_index, rec in src_records:
                    bones = set(int(b) for b in rec.get('bones', ()))
                    best = None
                    best_key = None
                    for dst in range(len(bins)):
                        if dst == src_index:
                            continue
                        union = shadow_bones[dst] | bones
                        if len(union) > limit:
                            continue
                        added = len(union) - len(shadow_bones[dst])
                        overlap = len(shadow_bones[dst] & bones)
                        key = (added, -overlap, -len(shadow_bones[dst]), dst)
                        if best_key is None or key < best_key:
                            best_key = key
                            best = dst
                    if best is None:
                        possible = False
                        break
                    placements.append((original_index, rec, best, bones))
                    shadow_bones[best].update(bones)
                if possible and placements:
                    for original_index, rec, dst, bones in placements:
                        bins[dst]['records'].append((original_index, rec))
                        bins[dst]['bones'].update(bones)
                    del bins[src_index]
                    progress = True
                    break

        result = []
        for bin_ in bins:
            ordered_records = [rec for _idx, rec in sorted(bin_['records'], key=lambda x: x[0])]
            result.append((ordered_records, set(bin_['bones'])))
        return result

    candidates = [pack(order) for order in orders]
    def candidate_key(chunks):
        palette_sum = sum(len(bones) for _recs, bones in chunks)
        smallest = min((len(recs) for recs, _bones in chunks), default=0)
        return (len(chunks), palette_sum, -smallest)
    return min(candidates, key=candidate_key)


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

    # Spider-atlas prep stores an explicit profile section.  Trust that marker
    # even if Blender joins/reorders objects and the generic native-layout test
    # becomes false.
    if bool(obj.get("sm3_spider_atlas_prepared", False)):
        value = obj.get("sm3_spider_atlas_section")
        if value is not None:
            try:
                idx = int(value)
                if 0 <= idx < int(template_count):
                    return idx
            except Exception:
                pass

    if trust_native:
        native = _native_section_index(obj)
        if native is not None and 0 <= int(native) < int(template_count):
            return int(native)
    # WoS-style joined/rebuilt fallback: CURRENT Blender object order defines
    # output sections.  Old section IDs retained by Blender Join are ignored.
    return int(object_ordinal) % int(template_count)


def _object_material_ref(obj, template_sec, trust_native: bool) -> int:
    """Preserve exact per-piece ref, with a hard override for Spider atlas prep."""
    if bool(obj.get("sm3_spider_atlas_prepared", False)):
        for key in ("sm3_spider_atlas_material_ref", "sm3_serialized_material_ref"):
            for container in (obj, getattr(obj, "data", None)):
                if container is None:
                    continue
                value = container.get(key)
                if value not in (None, ""):
                    return _u32(value, template_sec.material_ref_serialized)

    if trust_native:
        for container in (obj, getattr(obj, "data", None)):
            if container is None:
                continue
            value = container.get("sm3_serialized_material_ref")
            if value not in (None, ""):
                return _u32(value, template_sec.material_ref_serialized)
    return int(template_sec.material_ref_serialized) & 0xFFFFFFFF


def _object_position_divisor(obj, fallback: float, trust_native: bool) -> float:
    if bool(obj.get("sm3_spider_atlas_prepared", False)) or trust_native:
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




def export_prepared_spider_atlas_one_section(
    obj,
    target_collection,
    output_path: str,
    *,
    flip_uv_v: bool = True,
    reverse_winding: bool = True,
    write_report: bool = True,
):
    """Dedicated Spider-atlas exporter with weight-safe smart sections.

    v1.8.4 keeps the v1.8.3 weight-safe split and records the actual serialized vertex-color test.  Every Blender
    influence keeps its original global SM3 bone index + weight.  The atlas
    mesh is partitioned into as few 32-bone sections as practical, all using
    the same canvas material and the same packed SM3_ATLAS UV layer.

    The function name is kept for saved-project/operator compatibility even
    though output may now contain multiple palette-safe sections.
    """
    if obj is None or getattr(obj, 'type', None) != 'MESH':
        raise ValueError('Select the prepared joined Spider-Man mesh')
    if target_collection is None:
        raise ValueError('Prepared Spider mesh is not inside an SM3 target collection')
    if not bool(obj.get('sm3_spider_atlas_prepared', False)):
        raise ValueError('Object is not prepared for Spider atlas export')

    build = str(obj.get('sm3_spider_atlas_build', ''))
    if build not in ('1.3.0', '1.3.1', '1.3.2', '1.3.3', '1.3.4', '1.3.5', '1.3.6', '1.3.7', '1.3.8'):
        raise ValueError(f'Run PREP CURRENT JOINED MESH first (found prep {build or "NONE"})')

    target_code = str(obj.get('sm3_spider_atlas_target', target_collection.get('sm3_spider_atlas_target', '000')))
    expected_hash = 0xAC92103D if target_code == '000' else (0xAC92103E if target_code == '001' else 0)
    profile_index = int(obj.get('sm3_spider_atlas_section', target_collection.get('sm3_spider_atlas_section', 4 if target_code == '000' else 3)))
    material_ref = _u32(obj.get('sm3_spider_atlas_material_ref', target_collection.get('sm3_spider_atlas_material_ref', 0x00000614 if target_code == '000' else 0)), 0)
    if not expected_hash:
        raise ValueError('Spider atlas target identity is not ch_spiderman000/001')
    if target_code == '000' and (profile_index != 4 or material_ref != 0x00000614):
        raise ValueError(f'Spider 000 prepared profile mismatch: section {profile_index}, material 0x{material_ref:08X}; expected section 4 / 0x00000614')

    snap = cached_snapshot_text(target_collection)
    if not snap:
        snap = cached_snapshot_text(obj) or cached_snapshot_text(getattr(obj, 'data', None))
    template = mesh_from_snapshot(snap, path='<SM3_ORIGINAL_TARGET_CACHE_IN_BLEND>') if snap else None
    if template is None:
        template = load_target_template(target_collection, [obj])

    if int(template.filename_hash) != int(expected_hash):
        raise ValueError(f'Stock schema hash mismatch: got 0x{int(template.filename_hash):08X}, expected 0x{expected_hash:08X}')
    if profile_index < 0 or profile_index >= len(template.sections):
        raise ValueError(f'Spider canvas section {profile_index} is outside stock schema ({len(template.sections)} sections)')
    if target_code == '000':
        if len(template.sections) != 10:
            raise ValueError(f'Spider 000 stock schema must contain 10 sections; found {len(template.sections)}')
        stock_ref = _u32(template.sections[4].material_ref_serialized, 0)
        if stock_ref != 0x00000614:
            raise ValueError(f'Stock Spider canvas material mismatch: section 4 is 0x{stock_ref:08X}, expected 0x00000614')

    warnings = []
    available_uv_names = [layer.name for layer in obj.data.uv_layers]
    atlas_uv_name = str(obj.get('sm3_friend_atlas_uv_export', '') or '').strip()
    if not atlas_uv_name:
        for layer in obj.data.uv_layers:
            if getattr(layer, 'active_render', False) and layer.name not in ('SM3_SRC', 'SM3_SRC_MASTER'):
                atlas_uv_name = layer.name
                break
        if not atlas_uv_name and obj.data.uv_layers.active is not None:
            candidate = obj.data.uv_layers.active.name
            if candidate not in ('SM3_SRC', 'SM3_SRC_MASTER'):
                atlas_uv_name = candidate
        if not atlas_uv_name:
            atlas_uv_name = next((layer.name for layer in obj.data.uv_layers if layer.name not in ('SM3_SRC', 'SM3_SRC_MASTER')), '')
    if not atlas_uv_name:
        raise ValueError('No packed atlas UV layer was found. Rebuild/capture the atlas before Spider export.')
    if atlas_uv_name in ('SM3_SRC', 'SM3_SRC_MASTER'):
        raise ValueError('Refusing to export the pre-combine source UV as TEXCOORD0; use SM3_ATLAS.')

    records = _build_triangle_records(
        obj,
        flip_uv_v=bool(flip_uv_v),
        reverse_winding=bool(reverse_winding),
        warnings=warnings,
        preferred_uv_layer_name=atlas_uv_name,
        wos_compatible_tbn=True,
    )
    if not records:
        raise ValueError('Prepared Spider mesh has no triangles')

    # IMPORTANT v1.8.3: NO _collapse_spider_records_to_32() call here.
    # Keep the exact Blender/SM3 global bone indices and weights, and split only
    # at section boundaries so each local palette remains <= 32 matrices.
    source_bones = sorted({int(b) for rec in records for b in rec.get('bones', ())})
    chunks = _split_records_for_bones_smart(records, 32)
    if not chunks:
        raise ValueError('Weight-safe Spider splitter produced no sections')

    template_sec = template.sections[profile_index]
    divisor = float(obj.get('sm3_position_divisor', target_collection.get('sm3_position_divisor', 512.0)) or 512.0)
    if target_code == '000':
        divisor = 512.0
        material_ref = 0x00000614

    sections = []
    section_decisions = []
    total_triangles = 0
    for chunk_index, (chunk_records, bone_set) in enumerate(chunks):
        vertices, triangles = _records_to_geometry(chunk_records)
        if not vertices or not triangles:
            continue
        palette = sorted(int(b) for b in bone_set) or [0]
        if len(palette) > 32:
            raise ValueError(f'Smart Spider section {chunk_index} still uses {len(palette)} bones; limit is 32')
        section = ExportSection(
            source_object=obj.name,
            source_material_index=0,
            material_ref_serialized=material_ref,
            vertices=vertices,
            triangles=triangles,
            bone_palette=palette,
            position_divisor=divisor,
            primitive_type=4,
            primitive_unknown=int(template_sec.primitive_unknown),
            unknown_30=int(template_sec.unknown_30),
            unknown_38=int(template_sec.unknown_38),
            unknown_40=int(template_sec.unknown_40),
            schema_template_section=profile_index,
            warnings=[],
        )
        sections.append(section)
        total_triangles += len(triangles)
        section_decisions.append({
            'output_section': len(sections) - 1,
            'source_object': obj.name,
            'object_ordinal': 0,
            'bone_chunk': chunk_index,
            'native_source_section': _native_section_index(obj),
            'native_provenance_trusted': False,
            'target_profile_section': profile_index,
            'material_ref_serialized': f'0x{material_ref:08X}',
            'vertex_stride': int(template_sec.vertex_stride),
            'vertex_count': len(vertices),
            'triangle_count': len(triangles),
            'bone_palette_count': len(palette),
            'bone_palette': palette,
            'spider_bone_remap_count': 0,
            'weights_preserved': True,
        })

    if not sections:
        raise ValueError('Weight-safe Spider export became empty')
    if total_triangles != len(records):
        warnings.append(f'Triangle record count {len(records)} serialized as {total_triangles}; check for degenerate triangles')
    if len(sections) > 8:
        warnings.append(f'Weight-safe split required {len(sections)} sections; this is valid for testing but higher than the intended compact 2-6 range')

    result = _write_raw_mesh_spider_uv1024(
        output_path,
        template,
        sections,
        filename_hash=template.filename_hash,
        player_target='AUTO',
        geometry_profile='TARGET_NATIVE',
    )
    if int(result.section_count) != len(sections):
        try:
            os.remove(output_path)
        except Exception:
            pass
        raise ValueError(f'Smart-section writer roundtrip produced {result.section_count} sections; expected {len(sections)}')

    vertex_color_summary = _vertex_color_summary(records)
    result.report.update({
        'export_mode': 'DEDICATED_SPIDER_ATLAS_WEIGHT_SAFE_SMART_SECTIONS_V184_NEUTRAL_SHADE_TEST',
        'target_collection': target_collection.name,
        'target_filename_hash': f'0x{int(template.filename_hash):08X}',
        'target_stock_section_count': len(template.sections),
        'source_object_count': 1,
        'output_section_count': len(sections),
        'layout_mode': 'SMART_MULTI_SECTION_PRESERVE_ORIGINAL_WEIGHTS',
        'bone_palette_limit_per_section': 32,
        'source_global_bone_count': len(source_bones),
        'source_global_bones': source_bones,
        'spider_bone_remap_count': 0,
        'weights_preserved': True,
        'game_shading_test_profile': str(obj.get('sm3_spider_shading_profile', '') or ''),
        'serialized_vertex_color_mode': vertex_color_summary.get('mode', 'UNKNOWN'),
        'serialized_vertex_color_summary': vertex_color_summary,
        'uv_layer_used_for_texcoord0': atlas_uv_name,
        'uv_layers_available': available_uv_names,
        'uv_source_layer_preserved': str(obj.get('sm3_friend_atlas_uv_source', '') or ''),
        'tangent_uv_layer': atlas_uv_name,
        'tangent_uv_v_preflipped_before_calc': True,
        'tbn_matches_serialized_texcoord0': True,
        'serialized_uv_divisor': 1024.0,
        'tbn_mode': 'WOS_COMPAT_FINAL_SERIALIZED_UV_VERTEX_NORMAL_NEG_BITANGENT_SIGN',
        'tbn_handedness': _tbn_handedness_summary(records),
        'section_decisions': section_decisions,
        'flip_uv_v': bool(flip_uv_v),
        'reverse_winding': bool(reverse_winding),
        'warnings': warnings,
    })
    if write_report:
        report_path = str(Path(output_path).with_suffix(Path(output_path).suffix + '.export.json'))
        Path(report_path).write_text(json.dumps(result.report, indent=2), encoding='utf-8')
        result.report['report_path'] = report_path
    return result


def export_prepared_spider_atlas_one_section_001(
    obj,
    target_collection,
    output_path: str,
    *,
    flip_uv_v: bool = True,
    reverse_winding: bool = True,
    write_report: bool = True,
):
    """Weight-safe Spider 001 atlas exporter using smart <=32-bone sections."""
    if obj is None or getattr(obj, 'type', None) != 'MESH':
        raise ValueError('Select the prepared joined Spider-Man mesh')
    if not bool(obj.get('sm3_spider_atlas_prepared', False)):
        raise ValueError('Object is not prepared for Spider atlas export')

    template_path = bundled_template_path_for_hash(0xAC92103E)
    if not template_path:
        raise ValueError('Bundled untouched stock ch_spiderman001 template is missing')
    template = parse_mesh(template_path)
    if int(template.filename_hash) != 0xAC92103E:
        raise ValueError(f'Stock 001 template hash mismatch: 0x{int(template.filename_hash):08X}')
    if len(template.sections) != 6:
        raise ValueError(f'Spider 001 stock schema must contain 6 sections; found {len(template.sections)}')
    profile_index = 3
    material_ref = 0x00000560
    template_sec = template.sections[profile_index]
    if _u32(template_sec.material_ref_serialized, 0) != material_ref:
        raise ValueError(
            f'Stock Spider 001 canvas material mismatch: section 3 is '
            f'0x{_u32(template_sec.material_ref_serialized, 0):08X}, expected 0x00000560'
        )

    available_uv_names = [layer.name for layer in obj.data.uv_layers]
    atlas_uv_name = str(obj.get('sm3_friend_atlas_uv_export', '') or '').strip()
    if not atlas_uv_name:
        for layer in obj.data.uv_layers:
            if getattr(layer, 'active_render', False) and layer.name not in ('SM3_SRC', 'SM3_SRC_MASTER'):
                atlas_uv_name = layer.name
                break
    if not atlas_uv_name and obj.data.uv_layers.active is not None:
        candidate = obj.data.uv_layers.active.name
        if candidate not in ('SM3_SRC', 'SM3_SRC_MASTER'):
            atlas_uv_name = candidate
    if not atlas_uv_name:
        atlas_uv_name = next((layer.name for layer in obj.data.uv_layers if layer.name not in ('SM3_SRC', 'SM3_SRC_MASTER')), '')
    if not atlas_uv_name:
        raise ValueError('No packed atlas UV layer was found for Spider 001 export')

    warnings = []
    records = _build_triangle_records(
        obj,
        flip_uv_v=bool(flip_uv_v),
        reverse_winding=bool(reverse_winding),
        warnings=warnings,
        preferred_uv_layer_name=atlas_uv_name,
        wos_compatible_tbn=True,
    )
    if not records:
        raise ValueError('Prepared Spider mesh has no triangles')

    source_bones = sorted({int(b) for rec in records for b in rec.get('bones', ())})
    chunks = _split_records_for_bones_smart(records, 32)
    sections = []
    decisions = []
    total_triangles = 0
    for chunk_index, (chunk_records, bone_set) in enumerate(chunks):
        vertices, triangles = _records_to_geometry(chunk_records)
        if not vertices or not triangles:
            continue
        palette = sorted(int(b) for b in bone_set) or [0]
        if len(palette) > 32:
            raise ValueError(f'Spider 001 smart section {chunk_index} uses {len(palette)} bones; limit is 32')
        sections.append(ExportSection(
            source_object=obj.name,
            source_material_index=0,
            material_ref_serialized=material_ref,
            vertices=vertices,
            triangles=triangles,
            bone_palette=palette,
            position_divisor=512.0,
            primitive_type=4,
            primitive_unknown=int(template_sec.primitive_unknown),
            unknown_30=int(template_sec.unknown_30),
            unknown_38=int(template_sec.unknown_38),
            unknown_40=int(template_sec.unknown_40),
            schema_template_section=profile_index,
            warnings=[],
        ))
        total_triangles += len(triangles)
        decisions.append({
            'output_section': len(sections) - 1,
            'source_object': obj.name,
            'bone_chunk': chunk_index,
            'target_profile_section': profile_index,
            'material_ref_serialized': '0x00000560',
            'vertex_stride': int(template_sec.vertex_stride),
            'vertex_count': len(vertices),
            'triangle_count': len(triangles),
            'bone_palette_count': len(palette),
            'bone_palette': palette,
            'spider_bone_remap_count': 0,
            'weights_preserved': True,
        })
    if not sections:
        raise ValueError('Weight-safe Spider 001 export became empty')
    if total_triangles != len(records):
        warnings.append(f'Triangle record count {len(records)} serialized as {total_triangles}; check for degenerate triangles')

    result = _write_raw_mesh_spider_uv1024(
        output_path,
        template,
        sections,
        filename_hash=template.filename_hash,
        player_target='AUTO',
        geometry_profile='TARGET_NATIVE',
    )
    if int(result.section_count) != len(sections):
        try:
            os.remove(output_path)
        except Exception:
            pass
        raise ValueError(f'Spider 001 writer produced {result.section_count} sections; expected {len(sections)}')

    result.report.update({
        'export_mode': 'DEDICATED_SPIDER_ATLAS_WEIGHT_SAFE_SMART_SECTIONS_001_V183',
        'target_collection': getattr(target_collection, 'name', ''),
        'target_filename_hash': '0xAC92103E',
        'target_stock_section_count': 6,
        'source_object_count': 1,
        'output_section_count': len(sections),
        'layout_mode': 'SMART_MULTI_SECTION_PRESERVE_ORIGINAL_WEIGHTS',
        'bone_palette_limit_per_section': 32,
        'source_global_bone_count': len(source_bones),
        'source_global_bones': source_bones,
        'spider_bone_remap_count': 0,
        'weights_preserved': True,
        'uv_layer_used_for_texcoord0': atlas_uv_name,
        'uv_layers_available': available_uv_names,
        'uv_source_layer_preserved': str(obj.get('sm3_friend_atlas_uv_source', '') or ''),
        'tangent_uv_layer': atlas_uv_name,
        'tangent_uv_v_preflipped_before_calc': True,
        'tbn_matches_serialized_texcoord0': True,
        'serialized_uv_divisor': 1024.0,
        'tbn_mode': 'WOS_COMPAT_FINAL_SERIALIZED_UV_VERTEX_NORMAL_NEG_BITANGENT_SIGN',
        'tbn_handedness': _tbn_handedness_summary(records),
        'section_decisions': decisions,
        'flip_uv_v': bool(flip_uv_v),
        'reverse_winding': bool(reverse_winding),
        'warnings': warnings,
    })
    if write_report:
        report_path = str(Path(output_path).with_suffix(Path(output_path).suffix + '.export.json'))
        Path(report_path).write_text(json.dumps(result.report, indent=2), encoding='utf-8')
        result.report['report_path'] = report_path
    return result


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

    collection_prepared = (
        bool(target_collection.get("sm3_spider_atlas_prepared", False))
        and str(target_collection.get("sm3_spider_atlas_build", "")) == SPIDER_ATLAS_BUILD
    )

    # Never export a scene prepared by the superseded experimental builds.
    # In particular, v1.2.4 briefly allowed a completed one-section reference
    # mod to act as a template. v1.2.6 intentionally forbids that path.
    stale = []
    if bool(target_collection.get("sm3_spider_atlas_prepared", False)) and str(target_collection.get("sm3_spider_atlas_build", "")) != SPIDER_ATLAS_BUILD:
        stale.append(f"{target_collection.name} [collection prep]")
    for obj in objects:
        if bool(obj.get("sm3_friend_spider_atlas_prepared", False)):
            stale.append(obj.name)
            continue
        if bool(obj.get("sm3_spider_atlas_prepared", False)) and str(obj.get("sm3_spider_atlas_build", "")) != SPIDER_ATLAS_BUILD:
            stale.append(obj.name)
    if stale:
        raise ValueError(
            "This scene contains Spider-atlas prep data from an older experimental build: "
            + ", ".join(stale[:6])
            + ". Re-open/re-import the FULL VANILLA CH_SPIDERMAN target and run v1.3.0 prep again. "
              "The completed friend/reference mod is reference-only and must not be used as a target/template."
        )

    # The Spider canvas shader was validated with a 32-bone palette.  Once the
    # dedicated prep marker is present, do not let the generic UI default of 48
    # silently take over again.
    prepared_limits = []
    if collection_prepared:
        try:
            prepared_limits.append(int(target_collection.get("sm3_spider_atlas_max_bones", 32)))
        except Exception:
            prepared_limits.append(32)
    for obj in objects:
        if bool(obj.get("sm3_spider_atlas_prepared", False)) and str(obj.get("sm3_spider_atlas_build", "")) == SPIDER_ATLAS_BUILD:
            try:
                prepared_limits.append(int(obj.get("sm3_spider_atlas_max_bones", 32)))
            except Exception:
                prepared_limits.append(32)
    if prepared_limits:
        max_bones = min(int(max_bones), max(1, min(prepared_limits)))

    # Saved joined projects must use the ORIGINAL target schema cached in the .blend.
    # Do not require the current scene to still contain all ten vanilla mesh objects,
    # and do not let a replacement/source path override the proven cached schema.
    prepared_now = [
        obj for obj in objects
        if (
            (bool(obj.get("sm3_spider_atlas_prepared", False))
             and str(obj.get("sm3_spider_atlas_build", "")) == SPIDER_ATLAS_BUILD)
            or collection_prepared
        )
    ]
    template = None
    if prepared_now:
        snap = cached_snapshot_text(target_collection)
        if not snap:
            for obj in objects:
                snap = cached_snapshot_text(obj) or cached_snapshot_text(getattr(obj, "data", None))
                if snap:
                    break
        if snap:
            template = mesh_from_snapshot(snap, path="<SM3_ORIGINAL_TARGET_CACHE_IN_BLEND>")
    if template is None:
        template = load_target_template(target_collection, objects)
    target_hash = _u32(target_collection.get("sm3_mesh_hash"), template.filename_hash)
    if target_hash != int(template.filename_hash):
        raise ValueError(
            f"Target identity mismatch: collection=0x{target_hash:08X} template=0x{template.filename_hash:08X}"
        )

    prepared_objects = prepared_now
    if prepared_objects:
        # Hard provenance check: a Spider atlas export must still be backed by
        # the FULL VANILLA source schema that prep validated. A one-section
        # completed/reference mod can therefore never sneak in through cache,
        # a changed source path, or an older .blend.
        for pobj in prepared_objects:
            target_code = str(pobj.get("sm3_spider_atlas_target", target_collection.get("sm3_spider_atlas_target", "")))
            expected_count = int(pobj.get("sm3_spider_atlas_source_section_count", target_collection.get("sm3_spider_atlas_source_section_count", 0)) or 0)
            expected_section = int(pobj.get("sm3_spider_atlas_section", target_collection.get("sm3_spider_atlas_section", -1)) or -1)
            expected_ref = _u32(pobj.get("sm3_spider_atlas_material_ref", target_collection.get("sm3_spider_atlas_material_ref", 0)), 0)
            expected_hash = 0xAC92103D if target_code == "000" else (0xAC92103E if target_code == "001" else 0)
            if not expected_hash or int(template.filename_hash) != expected_hash:
                raise ValueError("Spider atlas target identity is not the validated ch_spiderman000/001 resource")
            if expected_count <= 1 or len(template.sections) != expected_count:
                raise ValueError(
                    f"Spider atlas export lost FULL VANILLA provenance: template has {len(template.sections)} section(s), "
                    f"prep recorded {expected_count}. The completed one-section friend/reference mod is not a target/template."
                )
            if target_code == "000" and (expected_count != 10 or expected_section != 4 or expected_ref != 0x00000614):
                raise ValueError(
                    "Spider 000 atlas provenance mismatch. Required FULL VANILLA profile is 10 sections, canvas section 4, ref 0x00000614."
                )
            if expected_section < 0 or expected_section >= len(template.sections):
                raise ValueError("Spider atlas source section is outside the validated vanilla template")
            template_ref = _u32(template.sections[expected_section].material_ref_serialized, 0)
            if expected_ref and template_ref != expected_ref:
                raise ValueError(
                    f"Spider atlas vanilla material ref mismatch: template section {expected_section} is 0x{template_ref:08X}, "
                    f"expected 0x{expected_ref:08X}"
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

        is_prepared_spider = obj in prepared_objects
        if is_prepared_spider:
            profile_index = int(obj.get("sm3_spider_atlas_section", target_collection.get("sm3_spider_atlas_section", 4)))
            if profile_index < 0 or profile_index >= len(template.sections):
                raise ValueError(f"Prepared Spider atlas profile section {profile_index} is outside stock target")
        else:
            profile_index = _profile_index(obj, object_ordinal, len(template.sections), native_piece_layout)
        template_sec = template.sections[profile_index]
        fallback_divisor = float(divisors[profile_index]) if profile_index < len(divisors) else 512.0
        if is_prepared_spider:
            divisor = float(obj.get("sm3_position_divisor", target_collection.get("sm3_position_divisor", 512.0)) or 512.0)
            material_ref = _u32(obj.get("sm3_spider_atlas_material_ref", target_collection.get("sm3_spider_atlas_material_ref", 0x00000614)), 0x00000614)
            # v1.8.3: never collapse prepared Spider weights. Let each output
            # section own its own <=32-bone palette instead.
            bone_remap = {}
            max_bones = min(int(max_bones), 32)
        else:
            divisor = _object_position_divisor(obj, fallback_divisor, native_piece_layout)
            material_ref = _object_material_ref(obj, template_sec, native_piece_layout)
            bone_remap = {}

        chunks = (_split_records_for_bones_smart(records, int(max_bones))
                  if is_prepared_spider else _split_records_for_bones(records, int(max_bones)))
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
                primitive_type=4,  # triangle list, same as validated XESM3 custom MESH
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
                "spider_bone_remap_count": 0,
                "weights_preserved": bool(is_prepared_spider),
                "spider_canvas_palette": None,
            })

    if not sections:
        raise ValueError("No exportable SM3 sections were produced")

    # A prepared Spider atlas export must never fall back to the normal red-body
    # material path.  Refuse the file instead of creating a model that is
    # completely red in-game.
    prepared_refs = {
        _u32(obj.get("sm3_spider_atlas_material_ref", target_collection.get("sm3_spider_atlas_material_ref", 0)), 0)
        for obj in prepared_objects
    }
    prepared_refs.discard(0)
    if prepared_refs:
        bad = [sec.material_ref_serialized for sec in sections if _u32(sec.material_ref_serialized, 0) not in prepared_refs]
        if bad:
            raise ValueError(
                "Spider atlas export lost its material routing: "
                f"expected {sorted(f'0x{x:08X}' for x in prepared_refs)}, "
                f"got {sorted(f'0x{_u32(x,0):08X}' for x in bad)}"
            )

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
        "export_mode": "WOS_STYLE_DIRECT_OBJECT_SECTIONS_V183_WEIGHT_SAFE_PREPARED_SPIDER",
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
