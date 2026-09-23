from __future__ import annotations

import os
import re
import math
import struct
import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import bpy
import bmesh
from mathutils import Matrix, Vector

from .sm3_material_names import resolve_mesh_material
from .target_cache import (
    cache_target_template, register_template_file,
    stamp_target_cache_to_collection, stamp_target_cache_to_objects,
)


def _u32_hex(value: int) -> str:
    """Store unsigned game metadata as text so Blender never narrows it to C int."""
    return f"0x{int(value) & 0xFFFFFFFF:08X}"


def _idprop_to_u32(value, default: int = 0) -> int:
    if value is None:
        return default & 0xFFFFFFFF
    if isinstance(value, str):
        try:
            return int(value, 0) & 0xFFFFFFFF
        except ValueError:
            return default & 0xFFFFFFFF
    return int(value) & 0xFFFFFFFF


def _remove_collection_and_objects(collection: bpy.types.Collection):
    """Remove a toolkit-created collection and its directly owned objects."""
    for obj in list(collection.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(collection)


def remove_previous_spiderman_pair_imports():
    """Remove prior toolkit CH_SPIDERMAN 000/001 collections before a clean pair test."""
    targets = []
    for col in list(bpy.data.collections):
        source = str(col.get("sm3_source_mesh", "")).lower()
        name = col.name.lower()
        if (
            "ch_spiderman000.mesh" in source
            or "ch_spiderman001.mesh" in source
            or name.startswith("sm3_ch_spiderman000")
            or name.startswith("sm3_ch_spiderman001")
        ):
            targets.append(col)
    for col in targets:
        if col.name in bpy.data.collections:
            _remove_collection_and_objects(col)


from .sm3_format import (
    BONE_HASH_NAMES,
    SM3Mesh,
    SM3Skeleton,
    choose_position_divisors,
    decode_section_vertices,
    position_divisor_diagnostics,
    indices_to_faces,
    parse_mesh,
    parse_skeleton,
    read_indices,
    skeleton_pair_candidate_for_mesh,
)


def inverse_values_to_bind_matrix(values: Sequence[float]) -> Matrix:
    # SM3/WOS game data is row-major affine. The 4th float of each of the first
    # three rows is runtime/unknown data and is deliberately ignored.
    m = Matrix((
        (values[0], values[1], values[2], 0.0),
        (values[4], values[5], values[6], 0.0),
        (values[8], values[9], values[10], 0.0),
        (values[12], values[13], values[14], 1.0),
    ))
    m.invert()
    m.transpose()
    return m


def bind_matrix_to_game_inverse(bind_matrix: Matrix) -> Matrix:
    inv = bind_matrix.copy()
    inv.invert()
    inv.transpose()
    return inv


def skeleton_bind_positions(skel: SM3Skeleton) -> List[Tuple[float, float, float]]:
    result = []
    for bone in skel.bones:
        m = inverse_values_to_bind_matrix(bone.inverse_values)
        p = m.translation
        result.append((float(p.x), float(p.y), float(p.z)))
    return result


def _model_name_from_path(filepath: str) -> str:
    name = Path(filepath).name
    parts = name.split(".")
    if len(parts) >= 3 and parts[0].lower().startswith("0x"):
        return parts[1]
    return Path(filepath).stem


def _new_collection(name: str) -> bpy.types.Collection:
    # Match the WoS BlenderToolkit workflow: model collection == resource name.
    base = name
    candidate = base
    n = 1
    while candidate in bpy.data.collections:
        candidate = f"{base}.{n:03d}"
        n += 1
    col = bpy.data.collections.new(candidate)
    bpy.context.scene.collection.children.link(col)
    return col


def _link_object_only_to_collection(obj: bpy.types.Object, collection: bpy.types.Collection):
    if obj.name not in collection.objects:
        collection.objects.link(obj)
    for col in list(obj.users_collection):
        if col != collection:
            col.objects.unlink(obj)


def import_skeleton(
    filepath: str,
    *,
    collection: Optional[bpy.types.Collection] = None,
    object_name: Optional[str] = None,
    safe_viewer_mode: bool = False,
) -> Tuple[bpy.types.Object, SM3Skeleton]:
    skel = parse_skeleton(filepath)
    skeleton_name = object_name or _model_name_from_path(filepath)

    arm_data = bpy.data.armatures.new(skeleton_name)
    arm_obj = bpy.data.objects.new(skeleton_name, arm_data)
    if collection is None:
        bpy.context.collection.objects.link(arm_obj)
    else:
        collection.objects.link(arm_obj)

    arm_obj.show_in_front = True
    if not safe_viewer_mode:
        arm_obj["sm3_format"] = "RAW_SKEL"
        arm_obj["sm3_source_skel"] = os.path.abspath(filepath)
        arm_obj["sm3_filename_hash"] = _u32_hex(skel.filename_hash)
        arm_obj["sm3_bone_count"] = int(skel.bone_count)
        arm_obj["sm3_record_stride"] = 0x90
        arm_obj["sm3_inverse_bind_offset"] = 0x40

    bpy.context.view_layer.objects.active = arm_obj
    arm_obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")

    edit_bones: List[bpy.types.EditBone] = []
    bind_mats: List[Matrix] = []
    for raw_bone in skel.bones:
        eb = arm_data.edit_bones.new(raw_bone.name)
        eb.head = Vector((0.0, 0.0, 0.0))
        eb.tail = Vector((0.0, 0.05, 0.0))
        m = inverse_values_to_bind_matrix(raw_bone.inverse_values)
        eb.matrix = m
        edit_bones.append(eb)
        bind_mats.append(m)

    # Parenting after creation preserves the absolute bind matrices.
    for raw_bone, eb in zip(skel.bones, edit_bones):
        if 0 <= raw_bone.parent_index < len(edit_bones):
            eb.parent = edit_bones[raw_bone.parent_index]
            eb.use_connect = False

    # Bone display lengths do not participate in the game transform. Use the
    # nearest child distance when available so the armature is readable.
    child_indices: Dict[int, List[int]] = {}
    for raw_bone in skel.bones:
        if raw_bone.parent_index >= 0:
            child_indices.setdefault(raw_bone.parent_index, []).append(raw_bone.index)
    for raw_bone, eb in zip(skel.bones, edit_bones):
        lengths = []
        for child_i in child_indices.get(raw_bone.index, []):
            d = (bind_mats[child_i].translation - bind_mats[raw_bone.index].translation).length
            if d > 1.0e-5:
                lengths.append(d)
        eb.length = max(0.02, min(lengths) if lengths else 0.06)

    bpy.ops.object.mode_set(mode="OBJECT")

    if not safe_viewer_mode:
        for raw_bone in skel.bones:
            bone = arm_data.bones.get(raw_bone.name)
            if bone is None:
                continue
            bone["sm3_index"] = int(raw_bone.index)
            bone["sm3_name_hash"] = _u32_hex(raw_bone.name_hash)
            bone["sm3_parent_index"] = int(raw_bone.parent_index)
            bone["sm3_unknown_8c"] = _u32_hex(raw_bone.unknown_8c)
            bone["sm3_serialized_name_pointer"] = _u32_hex(raw_bone.name_pointer_serialized)

    return arm_obj, skel


def safe_export_skeleton(arm_obj: bpy.types.Object, output_path: str) -> str:
    if arm_obj is None or arm_obj.type != "ARMATURE":
        raise ValueError("Select an SM3 armature first")

    source = arm_obj.get("sm3_source_skel")
    if not source or not os.path.isfile(source):
        raise ValueError("Armature has no readable sm3_source_skel. Import the raw .skel first.")

    source_skel = parse_skeleton(source)
    if len(arm_obj.data.bones) < source_skel.bone_count:
        raise ValueError("Armature has fewer bones than the source SM3 skeleton")

    by_index: Dict[int, bpy.types.Bone] = {}
    for bone in arm_obj.data.bones:
        idx = bone.get("sm3_index")
        if idx is not None:
            by_index[int(idx)] = bone

    missing = [i for i in range(source_skel.bone_count) if i not in by_index]
    if missing:
        raise ValueError(f"Missing original SM3 bone indices: {missing[:8]}")

    data = bytearray(source_skel.raw)
    meaningful = (
        (0, 0, 0), (1, 0, 1), (2, 0, 2),
        (4, 1, 0), (5, 1, 1), (6, 1, 2),
        (8, 2, 0), (9, 2, 1), (10, 2, 2),
        (12, 3, 0), (13, 3, 1), (14, 3, 2),
    )

    for i in range(source_skel.bone_count):
        bone = by_index[i]
        stored_hash = bone.get("sm3_name_hash")
        if stored_hash is not None and _idprop_to_u32(stored_hash) != source_skel.bones[i].name_hash:
            raise ValueError(f"Bone index {i} hash no longer matches the source skeleton")

        game_inv = bind_matrix_to_game_inverse(bone.matrix_local)
        record = 0x10 + i * 0x90 + 0x40

        # Patch only the 12 transform floats consumed by the affine skinning path.
        # Keep source bytes at +0C,+1C,+2C,+3C exactly intact.
        for float_index, r, c in meaningful:
            struct.pack_into("<f", data, record + float_index * 4, float(game_inv[r][c]))

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return str(out)


def _armature_bone_name(arm_obj: Optional[bpy.types.Object], index: int) -> str:
    if arm_obj and arm_obj.type == "ARMATURE":
        for bone in arm_obj.data.bones:
            if int(bone.get("sm3_index", -9999)) == index:
                return bone.name
    return f"bone_{index}"


def _as_blend_row(value, *, cast, width: int = 4):
    """Normalize one decoded blend row without assuming it is subscriptable.

    The SM3 decoders normally return 4-tuples.  Blender ID properties and older
    cached .blend data can surface a scalar for a one-component value, though.
    WoS' importer conceptually just iterates the available index/weight values,
    so accept both scalar and sequence representations here.
    """
    if value is None:
        return ()
    if isinstance(value, (int, float)):
        try:
            return (cast(value),)
        except Exception:
            return ()
    try:
        vals = list(value)
    except TypeError:
        try:
            return (cast(value),)
        except Exception:
            return ()
    out = []
    for item in vals[:width]:
        try:
            out.append(cast(item))
        except Exception:
            continue
    return tuple(out)


def _iter_blend_values(value):
    """Return a small tuple for one SM3 blend row.

    Fresh SM3 decode returns tuples.  Keep scalar tolerance only so malformed or
    legacy data cannot crash Blender with a subscript error.
    """
    if value is None:
        return ()
    if isinstance(value, (int, float)):
        return (value,)
    try:
        return tuple(value)
    except TypeError:
        return (value,)


def _create_vertex_groups_fast(
    obj: bpy.types.Object,
    section,
    blend_indices,
    blend_weights,
    armature: Optional[bpy.types.Object] = None,
):
    """WoS-direct SM3 weight import.

    One rule only: section bone palette entries are GLOBAL skeleton indices.
    Create raw ``bone_<global index>`` groups first and assign the decoded
    weights.  Skeleton names are applied later by Rename Vertex Groups.
    """
    palette = tuple(int(v) for v in (getattr(section, "bone_palette", ()) or ()))
    if not palette:
        return 0

    # Exact WoS behavior: create a group for every palette entry, whether or not
    # a particular vertex ends up using it.  Duplicate palette entries reuse the
    # same Blender group.
    groups = {}
    for bone_index in palette:
        name = f"bone_{bone_index}"
        vg = obj.vertex_groups.get(name)
        if vg is None:
            vg = obj.vertex_groups.new(name=name)
        groups[bone_index] = vg

    assignments = 0
    vertex_count = len(obj.data.vertices)
    row_count = min(len(blend_indices), len(blend_weights), vertex_count)

    for vertex_index in range(row_count):
        indices = _iter_blend_values(blend_indices[vertex_index])
        weights = _iter_blend_values(blend_weights[vertex_index])
        for palette_index_raw, weight_raw in zip(indices, weights):
            try:
                palette_index = int(palette_index_raw)
                weight = float(weight_raw)
            except Exception:
                continue
            if weight == 0.0 or palette_index < 0 or palette_index >= len(palette):
                continue
            bone_index = palette[palette_index]
            vg = groups.get(bone_index)
            if vg is None:
                continue
            vg.add([vertex_index], weight, 'REPLACE')
            assignments += 1

    # Persist the GAME bone index behind each Blender vertex-group index.
    # This is deliberately redundant with the bone_<index> name: it lets Rename
    # recover correctly even if Blender/another tool/user has changed group names.
    try:
        obj["sm3_vertex_group_bone_map"] = json.dumps({
            str(int(vg.index)): int(bone_index)
            for bone_index, vg in groups.items()
        }, sort_keys=True)
    except Exception:
        # The visible bone_<index> names remain the primary WoS-compatible path.
        pass

    return assignments


def _apply_uv_layers(mesh_data: bpy.types.Mesh, texcoords: Dict[int, Sequence[Tuple[float, float]]], flip_v: bool):
    for channel, coords in sorted(texcoords.items()):
        if len(coords) != len(mesh_data.vertices):
            continue
        uv_layer = mesh_data.uv_layers.new(name=f"UVMap_{channel}")
        for loop in mesh_data.loops:
            u, v = coords[loop.vertex_index]
            uv_layer.data[loop.index].uv = (u, 1.0 - v if flip_v else v)


def _apply_color_layers(mesh_data: bpy.types.Mesh, colors: Dict[int, Sequence[Tuple[float, float, float, float]]]):
    if not hasattr(mesh_data, "color_attributes"):
        return
    for channel, values in sorted(colors.items()):
        if len(values) != len(mesh_data.vertices):
            continue
        layer = mesh_data.color_attributes.new(name=f"Col_{channel}", type="BYTE_COLOR", domain="CORNER")
        for loop in mesh_data.loops:
            color = values[loop.vertex_index]
            item = layer.data[loop.index]
            if hasattr(item, "color_srgb"):
                item.color_srgb = color
            else:
                item.color = color


def _apply_vector_attribute(mesh_data: bpy.types.Mesh, name: str, values: Sequence[Sequence[float]]):
    if not values or len(values) != len(mesh_data.vertices):
        return
    attr = mesh_data.attributes.get(name) or mesh_data.attributes.new(name=name, type="FLOAT_VECTOR", domain="POINT")
    for i, value in enumerate(values):
        attr.data[i].vector = value[:3]



def _validate_blender_geometry_payload(positions, faces, vertex_count: int, section_index: int):
    """Validate all geometry before Blender converts Python values to C ints."""
    if len(positions) != vertex_count:
        raise ValueError(
            f"SECTION {section_index:02d}: positions={len(positions)} expected={vertex_count}"
        )

    clean_positions = []
    for vi, p in enumerate(positions):
        xyz = tuple(float(v) for v in p[:3])
        if len(xyz) != 3 or not all(math.isfinite(v) for v in xyz):
            raise ValueError(f"SECTION {section_index:02d}: invalid position at vertex {vi}: {p}")
        clean_positions.append(xyz)

    clean_faces = []
    for fi, face in enumerate(faces):
        if len(face) != 3:
            continue
        tri = tuple(int(v) for v in face)
        for v in tri:
            if v < 0 or v >= vertex_count:
                raise ValueError(
                    f"SECTION {section_index:02d}: face {fi} index {v} outside 0..{vertex_count-1}"
                )
            if v > 0x7FFFFFFF:
                raise ValueError(
                    f"SECTION {section_index:02d}: face {fi} index 0x{v:X} exceeds signed C int"
                )
        clean_faces.append(tri)

    return clean_positions, clean_faces



def import_mesh(
    filepath: str,
    *,
    auto_find_skeleton: bool = True,
    import_skeleton_if_found: bool = True,
    position_divisor_mode: str = "AUTO",
    uv_divisor: float = 1024.0,
    flip_uv_v: bool = True,
    reverse_winding: bool = True,
    convert_to_triangle_list: bool = True,
    safe_viewer_mode: bool = False,
) -> Tuple[bpy.types.Collection, List[bpy.types.Object], Optional[bpy.types.Object], SM3Mesh]:
    mesh = parse_mesh(filepath)
    model_name = _model_name_from_path(filepath)
    collection = _new_collection(model_name)

    arm_obj: Optional[bpy.types.Object] = None
    skel: Optional[SM3Skeleton] = None
    skel_path = skeleton_pair_candidate_for_mesh(filepath) if auto_find_skeleton else None

    if skel_path:
        # v0.1.2 always creates a fresh armature for a fresh mesh import.
        # Reusing an older partially-imported armature made debugging extremely
        # confusing and could move the old armature away from old meshes.
        if import_skeleton_if_found:
            try:
                arm_obj, skel = import_skeleton(skel_path, collection=collection, safe_viewer_mode=safe_viewer_mode)
            except Exception as exc:
                raise RuntimeError(f"SKEL IMPORT FAILED: {Path(skel_path).name}: {exc}") from exc
        else:
            skel = parse_skeleton(skel_path)

    if position_divisor_mode == "AUTO":
        position_divisors = choose_position_divisors(mesh)
    else:
        manual_divisor = float(position_divisor_mode)
        position_divisors = [manual_divisor for _ in mesh.sections]

    diagnostics = position_divisor_diagnostics(mesh)
    objects: List[bpy.types.Object] = []

    for sec in mesh.sections:
        try:
            position_divisor = position_divisors[sec.index]
            decoded = decode_section_vertices(
                mesh,
                sec,
                position_divisor=position_divisor,
                uv_divisor=uv_divisor,
            )
            indices = read_indices(mesh, sec)

            # Match the WoS toolkit import option:
            # - ON: original game triangle strips are converted to triangle list.
            # - OFF: treat incoming indices as an already-exported triangle list.
            if convert_to_triangle_list:
                faces = indices_to_faces(indices, sec.primitive_type, sec.vertex_count)
            else:
                faces = [
                    (indices[i], indices[i + 1], indices[i + 2])
                    for i in range(0, len(indices) - 2, 3)
                    if (
                        indices[i] < sec.vertex_count
                        and indices[i + 1] < sec.vertex_count
                        and indices[i + 2] < sec.vertex_count
                    )
                ]

            if reverse_winding:
                faces = [(a, c, b) for a, b, c in faces]
        except Exception as exc:
            raise RuntimeError(
                f"SECTION {sec.index:02d} DECODE FAILED "
                f"(verts={sec.vertex_count}, indices={sec.index_count}, stride={sec.vertex_stride}): {exc}"
            ) from exc

        # Match the WoS BlenderToolkit Outliner naming convention:
        #   Object: SM3_MeshObject_0
        #   Data:   SM3_Mesh_0
        #
        # The original SM3 resource/section identity is still kept in custom
        # metadata below, so the friendly WoS-style name does not lose provenance.
        mesh_data = bpy.data.meshes.new(f"SM3_Mesh_{sec.index}")
        obj = bpy.data.objects.new(f"SM3_MeshObject_{sec.index}", mesh_data)
        collection.objects.link(obj)

        positions = decoded["positions"]
        try:
            positions, faces = _validate_blender_geometry_payload(
                positions, faces, sec.vertex_count, sec.index
            )
            mesh_data.from_pydata(positions, [], faces)
            mesh_data.update()
        except Exception as exc:
            raise RuntimeError(
                f"SECTION {sec.index:02d} BLENDER MESH CREATE FAILED "
                f"(positions={len(positions)}, faces={len(faces)}): {exc}"
            ) from exc

        if safe_viewer_mode:
            # Diagnostic viewer path: geometry + armature only.
            # Avoid every nonessential Blender API feature until raw model layout is proven.
            obj.name = f"SM3_MeshObject_{sec.index}_SAFE"
        else:
            try:
                _apply_uv_layers(mesh_data, decoded["texcoords"], flip_uv_v)
                _apply_color_layers(mesh_data, decoded["colors"])
                _apply_vector_attribute(mesh_data, "sm3_normal", decoded["normals"])
                _apply_vector_attribute(mesh_data, "sm3_tangent", decoded["tangents"])
                _apply_vector_attribute(mesh_data, "sm3_binormal", decoded["binormals"])
            except Exception as exc:
                raise RuntimeError(f"SECTION {sec.index:02d} ATTRIBUTE IMPORT FAILED: {exc}") from exc

            # v0.7.1 Lite: restore the REAL 0xXXXXXXXX material hash names without
            # loading the heavy MAT->TEX/atlas research database. A compact indexed
            # MESH->MAT map resolves all verified in-game model materials in ~one
            # lightweight one-time load. Unresolved/context-dependent cases stay as
            # SM3_REF_XXXXXXXX rather than guessing.
            material_field_offset = int(sec.info_offset) + 0x20
            resolved_material = resolve_mesh_material(
                mesh.filename_hash, material_field_offset, sec.material_ref_serialized
            )
            if resolved_material is not None:
                real_hash, real_name, resolution_mode = resolved_material
                mat_name = f"0x{int(real_hash) & 0xFFFFFFFF:08X}"
                material = bpy.data.materials.get(mat_name) or bpy.data.materials.new(mat_name)
                material["sm3_real_mat_hash"] = mat_name
                material["sm3_real_mat_name"] = real_name
                material["sm3_material_resolution"] = f"LITE_COMPACT_{resolution_mode}"
            else:
                real_hash, real_name, resolution_mode = None, "", "UNRESOLVED_SERIALIZED_REF"
                mat_name = f"SM3_REF_{int(sec.material_ref_serialized) & 0xFFFFFFFF:08X}"
                material = bpy.data.materials.get(mat_name) or bpy.data.materials.new(mat_name)
                material["sm3_material_resolution"] = "LITE_UNRESOLVED_SERIALIZED_REF"
            mesh_data.materials.append(material)

            try:
                _create_vertex_groups_fast(
                    obj,
                    sec,
                    decoded["blend_indices"],
                    decoded["blend_weights"],
                    None,  # WoS rule: MESH import always creates raw bone_<global index> groups.
                )
            except Exception as exc:
                raise RuntimeError(f"SECTION {sec.index:02d} WEIGHT IMPORT FAILED: {exc}") from exc

            if arm_obj is not None and obj.vertex_groups:
                obj.parent = arm_obj
                mod = obj.modifiers.new(name="SM3 Armature", type="ARMATURE")
                mod.object = arm_obj

            obj["sm3_source_mesh"] = os.path.abspath(filepath)
            obj["sm3_resource_name"] = model_name
            obj["sm3_mesh_hash"] = _u32_hex(mesh.filename_hash)
            obj["sm3_section_index"] = int(sec.index)
            mesh_data["sm3_resource_name"] = model_name
            mesh_data["sm3_section_index"] = int(sec.index)
            obj["sm3_position_divisor"] = float(position_divisor)
            obj["sm3_position_decode"] = "SHORT4_RAW_DIVISOR_NO_BBOX_OFFSET"
            obj["sm3_uv_divisor"] = float(uv_divisor)
            obj["sm3_primitive_type"] = int(sec.primitive_type)
            # Local pointer is section/object provenance.  Do NOT store it as the
            # identity of a shared Blender material datablock.
            obj["sm3_serialized_material_ref"] = _u32_hex(sec.material_ref_serialized)
            obj["sm3_material_field_offset"] = _u32_hex(material_field_offset)
            obj["sm3_real_mat_hash"] = "" if real_hash is None else _u32_hex(real_hash)
            obj["sm3_real_mat_name"] = real_name
            obj["sm3_material_resolution"] = (
                "LITE_UNRESOLVED_SERIALIZED_REF"
                if real_hash is None else f"LITE_COMPACT_{resolution_mode}"
            )
            obj["sm3_bone_palette_count"] = int(len(sec.bone_palette))
            # v0.7.5 update-safe identity mirror on Mesh datablock.
            mesh_data["sm3_source_mesh"] = os.path.abspath(filepath)
            mesh_data["sm3_mesh_hash"] = _u32_hex(mesh.filename_hash)
            mesh_data["sm3_resource_name"] = model_name

        objects.append(obj)

    divisor_text = ",".join(
        f"{sec.index}:{position_divisors[sec.index]:g}"
        for sec in mesh.sections
    )
    if not safe_viewer_mode:
        collection["sm3_toolkit_version"] = "1.1.7-skel-source-any-model-rename"
        collection["sm3_is_export_target"] = True
        collection["sm3_source_mesh"] = os.path.abspath(filepath)
        collection["sm3_source_basename"] = Path(filepath).name
        collection["sm3_resource_name"] = model_name
        collection["sm3_mesh_hash"] = _u32_hex(mesh.filename_hash)
        collection["sm3_section_count"] = int(mesh.section_count)
        collection["sm3_img_size"] = int(mesh.img_size)
        collection["sm3_phys_size"] = int(mesh.phys_size)
        collection["sm3_position_divisors"] = divisor_text
        collection["sm3_position_decode"] = "SHORT4_RAW_DIVISOR_NO_BBOX_OFFSET"
        collection["sm3_position_auto"] = bool(position_divisor_mode == "AUTO")
        if skel_path:
            collection["sm3_paired_skeleton"] = os.path.abspath(skel_path)
        # v0.7.4: cache the target schema/identity inside the .blend so a future
        # toolkit update does not force a re-import just to export an existing mesh.
        cache_target_template(collection, mesh)
        # v0.8: learn this target in a persistent per-user vault. Replacing the
        # add-on no longer erases the template needed by an already-weighted model.
        try:
            vault_path = register_template_file(filepath, mesh.filename_hash, model_name)
            if vault_path:
                collection["sm3_persistent_template_vault"] = vault_path
        except Exception:
            pass

    return collection, objects, arm_obj, mesh


def compare_two_armatures(arm_a: bpy.types.Object, arm_b: bpy.types.Object) -> str:
    if arm_a.type != "ARMATURE" or arm_b.type != "ARMATURE":
        raise ValueError("Select exactly two armatures")

    def by_index(arm):
        out = {}
        for bone in arm.data.bones:
            idx = bone.get("sm3_index")
            if idx is not None:
                out[int(idx)] = bone
        return out

    a = by_index(arm_a)
    b = by_index(arm_b)
    common = sorted(set(a) & set(b))
    same_hashes = True
    same_parents = True
    length_deltas = []

    for i in common:
        same_hashes &= _idprop_to_u32(a[i].get("sm3_name_hash"), 0xFFFFFFFF) == _idprop_to_u32(b[i].get("sm3_name_hash"), 0xFFFFFFFE)
        same_parents &= int(a[i].get("sm3_parent_index", -999)) == int(b[i].get("sm3_parent_index", -998))
        parent = int(a[i].get("sm3_parent_index", -1))
        if parent >= 0 and parent in a and parent in b:
            la = (a[i].matrix_local.translation - a[parent].matrix_local.translation).length
            lb = (b[i].matrix_local.translation - b[parent].matrix_local.translation).length
            length_deltas.append((i, a[i].name, la, lb, abs(la - lb)))

    max_delta = max((x[4] for x in length_deltas), default=0.0)
    mean_delta = sum(x[4] for x in length_deltas) / len(length_deltas) if length_deltas else 0.0

    lines = [
        "SPIDER-MAN 3 SKELETON COMPARISON",
        "================================",
        f"A: {arm_a.name}",
        f"B: {arm_b.name}",
        f"Common indexed bones: {len(common)}",
        f"Same bone hashes/order: {'YES' if same_hashes else 'NO'}",
        f"Same parent hierarchy: {'YES' if same_parents else 'NO'}",
        f"Max parent-child length delta: {max_delta:.9f}",
        f"Mean parent-child length delta: {mean_delta:.9f}",
        "",
    ]
    if same_hashes and same_parents and max_delta < 1.0e-4:
        lines += [
            "RESULT: SAME SKELETON PROPORTIONS / HIERARCHY.",
            "The two armatures differ primarily by coordinate frame / rigid placement,",
            "not by a different black-suit body proportion skeleton.",
            "",
        ]
    lines.append("Per-bone parent length comparison:")
    for i, name, la, lb, delta in length_deltas:
        lines.append(f"{i:02d} {name:28s} A={la:.7f} B={lb:.7f} delta={delta:.9f}")
    return "\n".join(lines)


def _indexed_vertex_group_bone_index(name: str) -> Optional[int]:
    """Extract the GLOBAL SM3 bone index from a WoS-style group name."""
    text = str(name or "").strip()
    match = re.match(r"^bone_(\d+)(?:(?:_|\.)|$)", text, re.IGNORECASE)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _skeleton_index_name_map(armature_obj: bpy.types.Object) -> Dict[int, str]:
    """Return GAME bone-index -> bone-name, preferring the imported raw SKEL.

    WoS can trust armature order because its SKEL importer resolves names while
    importing.  For SM3 we also keep the original .skel path.  Reading that
    source again makes Rename self-healing if an older add-on build left the
    armature with generic bone_# names.
    """
    source = str(armature_obj.get("sm3_source_skel", "") or "")
    if source and os.path.isfile(source):
        skel = parse_skeleton(source)
        return {int(raw.index): str(raw.name) for raw in skel.bones}

    # Fallback remains fully generic: use the explicit SM3 index property when
    # present, otherwise the imported armature order (the literal WoS rule).
    result: Dict[int, str] = {}
    for order_index, bone in enumerate(armature_obj.data.bones):
        raw_index = bone.get("sm3_index")
        bone_index = int(raw_index) if raw_index is not None else int(order_index)
        result[bone_index] = str(bone.name)
    return result


def _heal_armature_names_from_index_map(armature_obj: bpy.types.Object, names: Dict[int, str]) -> int:
    """Make armature bone names agree with the same GAME-index map as groups."""
    pending = []
    for order_index, bone in enumerate(armature_obj.data.bones):
        raw_index = bone.get("sm3_index")
        bone_index = int(raw_index) if raw_index is not None else int(order_index)
        target = names.get(bone_index)
        if target and str(bone.name) != str(target):
            pending.append((bone_index, bone, str(target)))

    # Two-pass rename prevents Blender's automatic .001 suffix from a temporary
    # collision when repairing an armature imported by an older toolkit build.
    for bone_index, bone, _target in pending:
        bone.name = f"__SM3_RENAME_TMP_{bone_index:04d}__"
    for _bone_index, bone, target in pending:
        bone.name = target
    return len(pending)


def _stored_vertex_group_bone_map(mesh_obj: bpy.types.Object) -> Dict[int, int]:
    raw = mesh_obj.get("sm3_vertex_group_bone_map")
    if not raw:
        return {}
    try:
        data = json.loads(str(raw))
        return {int(k): int(v) for k, v in data.items()}
    except Exception:
        return {}


def rename_vertex_groups_from_armature(collection: bpy.types.Collection):
    """Rename SM3 weight groups by GAME bone index, for any character.

    Primary path is exactly WoS-style: bone_<global index> -> SKEL bone index.
    SM3 additionally stores a hidden vertex-group-index -> game-bone-index map
    at import time, so the same command can recover if names were altered.

    No character name/hash table is used to choose a rig.  The matching MESH
    palette and matching SKEL index are the relationship.
    """
    if collection is None:
        raise ValueError("Choose an SM3 collection first")

    mesh_objects = [obj for obj in collection.objects if obj.type == "MESH"]
    armatures = [obj for obj in collection.objects if obj.type == "ARMATURE"]
    if not mesh_objects:
        raise ValueError(f"Collection '{collection.name}' contains no mesh objects")
    if not armatures:
        raise ValueError(
            f"Collection '{collection.name}' contains no SM3 armature. "
            "Import the matching .skel into this collection first."
        )

    armature_obj = armatures[-1]
    bones = _skeleton_index_name_map(armature_obj)
    if not bones:
        raise ValueError("The imported SM3 skeleton produced no bone-index map")

    repaired_armature = _heal_armature_names_from_index_map(armature_obj, bones)
    valid_names = set(bones.values())

    renamed = 0
    already_named = 0
    indexed_seen = 0
    unmapped = []

    for mesh_obj in mesh_objects:
        stored = _stored_vertex_group_bone_map(mesh_obj)
        for vg in mesh_obj.vertex_groups:
            name = str(vg.name)
            bone_index = None

            # Literal WoS-compatible route.
            match = re.match(r"^bone_(\d+)(?:_|\.|$)", name, re.IGNORECASE)
            if match is not None:
                bone_index = int(match.group(1))
            # SM3 self-healing route: importer persisted the game index behind
            # this Blender group index, independent of its visible name.
            elif int(vg.index) in stored:
                bone_index = int(stored[int(vg.index)])
            elif name in valid_names:
                already_named += 1
                continue
            else:
                continue

            indexed_seen += 1
            target_name = bones.get(int(bone_index))
            if target_name is None:
                if len(unmapped) < 8:
                    unmapped.append(f"{name}->{bone_index}")
                continue

            if str(vg.name) == str(target_name):
                already_named += 1
            else:
                vg.name = str(target_name)
                renamed += 1

        if len(mesh_obj.vertex_groups):
            existing = next((
                mod for mod in mesh_obj.modifiers
                if mod.type == "ARMATURE" and mod.object == armature_obj
            ), None)
            if existing is None:
                mod = mesh_obj.modifiers.new(name="SM3 Armature", type="ARMATURE")
                mod.object = armature_obj

    collection["sm3_last_rename_renamed"] = int(renamed)
    collection["sm3_last_rename_already"] = int(already_named)
    collection["sm3_last_rename_indexed_seen"] = int(indexed_seen)
    collection["sm3_last_rename_armature_repaired"] = int(repaired_armature)

    if indexed_seen == 0 and already_named == 0:
        sample = []
        for mesh_obj in mesh_objects:
            sample.extend(str(vg.name) for vg in list(mesh_obj.vertex_groups)[:4])
            if len(sample) >= 8:
                break
        sample_text = ", ".join(sample[:8]) if sample else "<no groups>"
        raise ValueError(
            "No SM3 bone-index vertex groups could be identified. "
            f"Sample groups: {sample_text}"
        )

    if unmapped:
        raise ValueError(
            f"Found group bone indices outside this {len(bones)}-bone SKEL. "
            f"Sample: {', '.join(unmapped)}"
        )

    return int(renamed)


def _remove_temp_weight_transfer_collection(collection):
    if collection is None:
        return
    for obj in list(collection.objects):
        data = obj.data if getattr(obj, "type", None) == "MESH" else None
        bpy.data.objects.remove(obj, do_unlink=True)
        if data is not None and data.users == 0:
            bpy.data.meshes.remove(data)
    if collection.name in bpy.data.collections:
        bpy.data.collections.remove(collection)


def _build_joined_weight_donor(source_collection: bpy.types.Collection):
    """Duplicate + join all source meshes so Data Transfer has one donor object.

    The original SM3 sections are never modified.
    Vertex groups with matching bone_<index> names merge naturally during Join.
    """
    source_meshes = [
        obj for obj in source_collection.all_objects
        if obj.type == "MESH"
    ]
    if not source_meshes:
        raise ValueError(
            f"Source collection '{source_collection.name}' contains no mesh objects"
        )

    temp_collection = bpy.data.collections.new("__SM3_WEIGHT_TRANSFER_TEMP__")
    bpy.context.scene.collection.children.link(temp_collection)

    duplicates = []
    for src in source_meshes:
        dup = src.copy()
        dup.data = src.data.copy()
        dup.animation_data_clear()
        temp_collection.objects.link(dup)
        duplicates.append(dup)

    bpy.ops.object.select_all(action="DESELECT")
    for dup in duplicates:
        dup.hide_set(False)
        dup.hide_viewport = False
        dup.select_set(True)

    bpy.context.view_layer.objects.active = duplicates[0]
    bpy.ops.object.join()

    donor = bpy.context.view_layer.objects.active
    donor.name = "__SM3_JOINED_WEIGHT_DONOR__"
    return donor, temp_collection


def _clear_vertex_groups(obj: bpy.types.Object):
    if obj.type != "MESH":
        return
    for vg in list(obj.vertex_groups):
        obj.vertex_groups.remove(vg)


def _limit_and_normalize_vertex_weights(
    obj: bpy.types.Object,
    max_influences: int = 4,
):
    """Limit weights to SM3's four blend slots and normalize per vertex."""
    if obj.type != "MESH":
        raise ValueError("Target must be a mesh")

    group_by_index = {vg.index: vg for vg in obj.vertex_groups}
    affected = 0

    # Snapshot first; v.groups changes as assignments are removed/replaced.
    snapshots = []
    for vertex in obj.data.vertices:
        entries = [(g.group, float(g.weight)) for g in vertex.groups if g.weight > 0.0]
        snapshots.append((vertex.index, entries))

    for vertex_index, entries in snapshots:
        if not entries:
            continue

        entries.sort(key=lambda item: item[1], reverse=True)
        keep = entries[:max(1, int(max_influences))]
        remove = entries[max(1, int(max_influences)):]

        total = sum(weight for _group, weight in keep)
        if total <= 1.0e-12:
            continue

        for group_index, _weight in remove:
            vg = group_by_index.get(group_index)
            if vg is not None:
                try:
                    vg.remove([vertex_index])
                except RuntimeError:
                    pass

        for group_index, weight in keep:
            vg = group_by_index.get(group_index)
            if vg is not None:
                vg.add([vertex_index], weight / total, "REPLACE")

        affected += 1

    return affected


def transfer_weights_from_sm3_collection(
    source_collection: bpy.types.Collection,
    target_object: bpy.types.Object,
    clear_existing: bool = True,
    limit_to_four: bool = True,
    normalize: bool = True,
):
    """SM3 model-swap Data Transfer, adapted to an SM3 section collection.

    Source:
        Imported original SM3 model collection (e.g. ch_spiderman000).

    Target:
        Replacement mesh (e.g. joined WOS donor model).

    The original SM3 source sections remain unchanged. A temporary duplicate of
    the source sections is joined, used as Blender Data Transfer source, then
    deleted.

    Mapping:
        Nearest Face Interpolated
        All vertex groups
        Match destination groups by name
        Replace weights
        Object/world transforms enabled

    The target should already be rotated/scaled/positioned over the SM3 source
    body before this operation, just like the tutorial workflow.
    """
    if source_collection is None:
        raise ValueError("Choose an SM3 source collection")
    if target_object is None or target_object.type != "MESH":
        raise ValueError("Choose one replacement target MESH object")

    temp_collection = None
    donor = None

    # Preserve user selection/active object as best as possible.
    old_active = bpy.context.view_layer.objects.active
    old_selected = list(bpy.context.selected_objects)

    try:
        donor, temp_collection = _build_joined_weight_donor(source_collection)

        if clear_existing:
            _clear_vertex_groups(target_object)

        # Blender's Data Transfer operator copies from ACTIVE -> selected target.
        bpy.ops.object.select_all(action="DESELECT")
        donor.select_set(True)
        target_object.select_set(True)
        bpy.context.view_layer.objects.active = donor

        result = bpy.ops.object.data_transfer(
            data_type="VGROUP_WEIGHTS",
            use_create=True,
            vert_mapping="POLYINTERP_NEAREST",
            use_object_transform=True,
            use_max_distance=False,
            layers_select_src="ALL",
            layers_select_dst="NAME",
            mix_mode="REPLACE",
            mix_factor=1.0,
        )
        if "FINISHED" not in result:
            raise RuntimeError(f"Blender Data Transfer returned {result}")

        # Make the target active for cleanup/weight post-processing.
        bpy.ops.object.select_all(action="DESELECT")
        target_object.select_set(True)
        bpy.context.view_layer.objects.active = target_object

        weighted_vertices_before = sum(
            1 for v in target_object.data.vertices if len(v.groups) > 0
        )

        processed = 0
        if limit_to_four or normalize:
            processed = _limit_and_normalize_vertex_weights(
                target_object,
                max_influences=4 if limit_to_four else 1000000,
            )

        weighted_vertices_after = sum(
            1 for v in target_object.data.vertices if len(v.groups) > 0
        )

        # v0.8 WoS-style ownership rewrite:
        # The REPLACEMENT collection owns export identity. The imported SM3 model
        # is only a weight/template donor and may be deleted after this operation.
        # This matches WoS much more closely and removes the add-on-update bug
        # where the user had to re-import the original target just to export an
        # already-weighted WOS model.
        source_hash = str(source_collection.get("sm3_mesh_hash", "") or "")
        source_path = str(source_collection.get("sm3_source_mesh", "") or "")
        source_resource = str(source_collection.get("sm3_resource_name", source_collection.name) or source_collection.name)
        source_basename = str(source_collection.get("sm3_source_basename", Path(source_path).name if source_path else f"{source_resource}.mesh"))

        if source_hash:
            target_object["sm3_weight_source_mesh_hash"] = source_hash
            target_object["sm3_export_target_mesh_hash"] = source_hash

        # Capture the ordinary collection(s) the donor/replacement already lives
        # in (for WoS this is normally ch_spiderman_rvb000). Do NOT move it into
        # the imported SM3 target collection.
        owner_collections = [
            col for col in list(target_object.users_collection)
            if col != source_collection and not bool(col.get("sm3_is_export_target", False))
        ]
        if not owner_collections:
            owner_name = f"SM3_Export_{source_resource}"[:63]
            owner = bpy.data.collections.get(owner_name) or bpy.data.collections.new(owner_name)
            try:
                bpy.context.scene.collection.children.link(owner)
            except Exception:
                pass
            if target_object.name not in owner.objects:
                owner.objects.link(target_object)
            owner_collections = [owner]

        # If a previous Lite version linked the replacement into the imported
        # source target, remove only that redundant ownership link. Geometry and
        # weights are untouched.
        if source_collection in target_object.users_collection:
            try:
                source_collection.objects.unlink(target_object)
            except Exception:
                pass

        primary_owner = owner_collections[0]
        for owner in owner_collections:
            try:
                stamp_target_cache_to_collection(source_collection, owner)
            except Exception:
                # Object-level cache below remains a fallback.
                pass

        target_object["sm3_export_target_collection"] = primary_owner.name
        target_object["sm3_export_target_source_mesh"] = source_path
        target_object["sm3_export_target_source_basename"] = source_basename
        target_object["sm3_export_target_resource_name"] = source_resource
        target_object["sm3_export_target_locked"] = True
        target_object["sm3_export_owner_collection"] = primary_owner.name
        if getattr(target_object, "data", None) is not None:
            target_object.data["sm3_export_target_mesh_hash"] = source_hash
            target_object.data["sm3_weight_source_mesh_hash"] = source_hash
            target_object.data["sm3_export_target_collection"] = primary_owner.name
            target_object.data["sm3_export_target_source_mesh"] = source_path
            target_object.data["sm3_export_target_source_basename"] = source_basename
            target_object.data["sm3_export_target_resource_name"] = source_resource
            target_object.data["sm3_export_target_locked"] = True

        # Carry the compact target template on the object/data too. The collection
        # is authoritative, but redundant stamps make joins and file migrations safe.
        stamp_target_cache_to_objects(source_collection, [target_object])

        return {
            "source_collection": source_collection.name,
            "target_object": target_object.name,
            "source_group_count": len(donor.vertex_groups),
            "target_group_count": len(target_object.vertex_groups),
            "target_vertex_count": len(target_object.data.vertices),
            "weighted_vertices_before": weighted_vertices_before,
            "weighted_vertices_after": weighted_vertices_after,
            "postprocessed_vertices": processed,
            "limited_to_four": bool(limit_to_four),
            "normalized": bool(normalize),
        }

    finally:
        # Delete only our temporary donor; never touch original source/target.
        try:
            _remove_temp_weight_transfer_collection(temp_collection)
        except Exception:
            pass

        try:
            bpy.ops.object.select_all(action="DESELECT")
            for obj in old_selected:
                if obj and obj.name in bpy.data.objects:
                    obj.select_set(True)
            if old_active and old_active.name in bpy.data.objects:
                bpy.context.view_layer.objects.active = old_active
        except Exception:
            pass
