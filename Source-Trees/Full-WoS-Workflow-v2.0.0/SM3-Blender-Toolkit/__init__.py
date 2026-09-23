bl_info = {
    "name": "SM3 WoS Workflow Clone",
    "author": "TSGAMING264",
    "version": (2, 0, 0),
    "blender": (4, 5, 0),
    "location": "File > Import/Export; 3D View > N Panel > SM3 Tools",
    "description": "WoS-style SM3 port workflow: canonical Spider slots, source textures, Material Combiner atlas, weight-safe export",
    "category": "Import-Export",
}

import os
import tempfile
import re
import json
import traceback
import hashlib
from pathlib import Path

import bpy
import bmesh
from mathutils import Vector
from bpy.types import Operator, PropertyGroup, Menu, Panel
from bpy.props import StringProperty, BoolProperty, CollectionProperty, IntProperty, PointerProperty, EnumProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

from .blender_io import import_mesh, import_skeleton, safe_export_skeleton, rename_vertex_groups_from_armature
# Blender can retain an older sm3_export module after reinstalling an extension
# in the same process.  Reload it explicitly so new exporter entry points are
# always available without requiring the user to restart Blender.
import importlib
# Reload the low-level mesh writer BEFORE sm3_export. Blender keeps imported
# package submodules alive across extension reinstalls, which can otherwise pair
# a new exporter with an old write_raw_mesh() signature.
from . import sm3_mesh_writer as _sm3_mesh_writer
from . import sm3_export as _sm3_export
importlib.invalidate_caches()
_sm3_mesh_writer = importlib.reload(_sm3_mesh_writer)
_sm3_export = importlib.reload(_sm3_export)
export_objects_to_target_mesh = _sm3_export.export_objects_to_target_mesh
export_prepared_spider_atlas_one_section = _sm3_export.export_prepared_spider_atlas_one_section
export_prepared_spider_atlas_one_section_001 = _sm3_export.export_prepared_spider_atlas_one_section_001
from .target_cache import cached_snapshot_text, mesh_from_snapshot, cache_target_template, fallback_template_path_for_hash, CACHE_PROP, DIVISOR_PROP
from .sm3_format import parse_mesh
from .wrap_io import is_wrap_file, unwrap_file
from .wos_material_workflow import (
    load_image_for_preview as _wos_load_image_for_preview,
    assign_image_to_material as _wos_assign_image_to_material,
    get_diffuse_image as _wos_get_diffuse_image,
    material_face_count as _wos_material_face_count,
)

_HAS_FILEHANDLER = hasattr(bpy.types, "FileHandler")


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------

def _write_last_error(label: str):
    report = f"{label}\n{'=' * len(label)}\n\n{traceback.format_exc()}"
    text = bpy.data.texts.get("SM3_Last_Error") or bpy.data.texts.new("SM3_Last_Error")
    text.clear()
    text.write(report)
    print("\n" + report + "\n")


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


def _is_sm3_target_collection(col):
    if col is None:
        return False
    return bool(_u32(col.get("sm3_mesh_hash"), 0)) and bool(
        col.get("sm3_is_export_target", False) or col.get("sm3_source_mesh")
    )


def _sm3_target_collections():
    return [c for c in bpy.data.collections if _is_sm3_target_collection(c)]


def _collection_meshes(col):
    return [obj for obj in col.objects if obj.type == "MESH"]


def _model_resource_name(filepath: str):
    name = Path(filepath).name
    parts = name.split(".")
    if len(parts) >= 3 and parts[0].lower().startswith("0x"):
        return parts[1]
    return Path(filepath).stem


def _collection_base_name(name: str) -> str:
    """Return Blender's logical base name, ignoring a trailing .### duplicate suffix."""
    return re.sub(r"\.\d{3}$", "", str(name or ""))


def _raw_name_from_wrap(path):
    name = Path(path).name
    return name.replace(".wrap.mesh", ".mesh").replace(".wrap.skel", ".skel").replace(".wrap.tex", ".tex")


def _import_mesh_path(filepath, scene):
    """Proven v0.4.0 regular import route.

    Raw MESH files go straight to the normal parser. WRAP resources are first
    reconstructed through the original generic WRAP component/patch resolver,
    then the exact same raw importer is used on a temporary flat MESH.
    """
    if not is_wrap_file(filepath):
        return import_mesh(
            filepath,
            auto_find_skeleton=True,
            import_skeleton_if_found=True,
            flip_uv_v=scene.sm3_flip_uv_v_axis,
            reverse_winding=scene.sm3_reverse_winding,
            convert_to_triangle_list=scene.sm3_convert_triangle_list,
        )

    flat, info = unwrap_file(filepath)
    with tempfile.TemporaryDirectory(prefix="sm3_wrap_import_") as td:
        rawpath = Path(td) / _raw_name_from_wrap(filepath)
        rawpath.write_bytes(flat)
        col, objs, arm, mesh = import_mesh(
            str(rawpath),
            auto_find_skeleton=False,
            import_skeleton_if_found=False,
            flip_uv_v=scene.sm3_flip_uv_v_axis,
            reverse_winding=scene.sm3_reverse_winding,
            convert_to_triangle_list=scene.sm3_convert_triangle_list,
        )

    col["sm3_source_mesh"] = os.path.abspath(filepath)
    col["sm3_source_basename"] = Path(filepath).name
    col["sm3_wrap_archive_hash"] = f"0x{info.archive_hash:08X}"
    col["sm3_wrap_imported"] = True
    for obj in objs:
        obj["sm3_source_mesh"] = os.path.abspath(filepath)
        obj["sm3_wrap_imported"] = True
        if getattr(obj, "data", None) is not None:
            obj.data["sm3_source_mesh"] = os.path.abspath(filepath)
    return col, objs, arm, mesh


def _import_skel_path(filepath):
    target_col = _guess_collection_for_skeleton(filepath)
    if not is_wrap_file(filepath):
        return import_skeleton(filepath, collection=target_col)

    flat, info = unwrap_file(filepath)
    with tempfile.TemporaryDirectory(prefix="sm3_wrap_skel_") as td:
        rawpath = Path(td) / _raw_name_from_wrap(filepath)
        rawpath.write_bytes(flat)
        arm, skel = import_skeleton(str(rawpath), collection=target_col)
    arm["sm3_source_skel"] = os.path.abspath(filepath)
    arm["sm3_wrap_archive_hash"] = f"0x{info.archive_hash:08X}"
    arm["sm3_wrap_imported"] = True
    return arm, skel


def _guess_collection_for_skeleton(filepath: str):
    """Choose the SM3 mesh collection that belongs to this skeleton.

    WoS-style behavior is index-driven, not character-driven.  The only job
    here is to put the armature beside the mesh the user is actually working
    on.  Blender duplicate collection suffixes (.001, .002, ...) therefore
    must not make a second Venom/Black Suit/etc. import bind to an older copy.
    """
    resource = _model_resource_name(filepath)
    low = resource.lower()
    marker = "skeleton_"
    if marker not in low:
        return None

    model_name = resource[low.index(marker) + len(marker):]
    wanted = model_name.casefold()

    # First choice: the collection currently selected in the toolkit, provided
    # it is the same logical model (Blender may have suffixed it with .001).
    scene = getattr(bpy.context, "scene", None)
    selected = getattr(scene, "sm3_collection_search_dropdown", None) if scene else None
    if selected is not None and _collection_base_name(selected.name).casefold() == wanted:
        return selected

    # Then prefer the exact unsuffixed collection if that is the only/current
    # match.  Otherwise choose the newest duplicate rather than silently using
    # an older model import.
    matches = [
        col for col in bpy.data.collections
        if _collection_base_name(col.name).casefold() == wanted
    ]
    if matches:
        return matches[-1]
    return None


def _source_basename(col):
    raw = str(col.get("sm3_source_basename", "") or "")
    if raw:
        return Path(raw).name
    source = str(col.get("sm3_source_mesh", "") or "")
    if source:
        return Path(source).name
    mesh_hash = _u32(col.get("sm3_mesh_hash"), 0)
    resource = str(col.get("sm3_resource_name", col.name) or col.name)
    return f"0x{mesh_hash:08X}.{resource}.mesh"


def _ensure_white_color_attribute(obj):
    mesh = getattr(obj, "data", None)
    if mesh is None or not hasattr(mesh, "color_attributes"):
        return
    if len(mesh.color_attributes):
        return
    attr = mesh.color_attributes.new(name="Col_0", type="BYTE_COLOR", domain="CORNER")
    for item in attr.data:
        if hasattr(item, "color_srgb"):
            item.color_srgb = (1.0, 1.0, 1.0, 1.0)
        else:
            item.color = (1.0, 1.0, 1.0, 1.0)


def _armature_from_object(obj):
    if obj is None:
        return None
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


def _attach_armature_modifier(obj, armature):
    if obj is None or obj.type != "MESH" or armature is None:
        return
    for mod in obj.modifiers:
        if mod.type == "ARMATURE" and mod.object == armature:
            return
    mod = obj.modifiers.new(name="SM3 Armature", type="ARMATURE")
    mod.object = armature


def _spider_atlas_target_profile(col):
    """Resolve Spider atlas export profile from ORIGINAL target cache first.

    Saved export-ready projects commonly contain only the joined replacement
    mesh.  That is valid: the importer stores the original SM3 target schema in
    the .blend.  Current scene mesh count must therefore NOT be used as proof of
    vanilla provenance.  The completed friend mod remains reference-only; we
    validate only the cached/original target identity and schema.
    """
    if col is None:
        return None

    mesh_hash = _u32(col.get("sm3_mesh_hash"), 0)
    resource = str(col.get("sm3_resource_name", col.name) or col.name).casefold()
    source = _source_basename(col).casefold()
    if mesh_hash == 0xAC92103D or "ch_spiderman000" in resource or "ch_spiderman000" in source:
        target = "000"; expected_hash = 0xAC92103D; expected_section = 4; expected_ref = 0x00000614; expected_count = 10
    elif mesh_hash == 0xAC92103E or "ch_spiderman001" in resource or "ch_spiderman001" in source:
        target = "001"; expected_hash = 0xAC92103E; expected_section = 3; expected_ref = None; expected_count = None
    else:
        return None

    # Cache may live on the collection OR on the joined replacement object/data.
    snap = cached_snapshot_text(col)
    if not snap:
        for obj in _collection_meshes(col):
            snap = cached_snapshot_text(obj) or cached_snapshot_text(getattr(obj, "data", None))
            if snap:
                break
    if snap:
        try:
            d = json.loads(snap)
            sh = _u32(d.get("filename_hash"), 0)
            sections = d.get("sections") or []
            if sh == expected_hash and len(sections) > expected_section:
                ref = _u32(sections[expected_section].get("material_ref_serialized"), 0)
                if target == "000" and (len(sections) != expected_count or ref != expected_ref):
                    return {"target": target, "invalid_reference_output": True, "reason": "BAD_CACHED_VANILLA_SCHEMA", "cached_section_count": len(sections), "material_ref": ref}
                if target == "001" and len(sections) <= 1:
                    return {"target": target, "invalid_reference_output": True, "reason": "BAD_CACHED_VANILLA_SCHEMA", "cached_section_count": len(sections)}
                return {
                    "target": target, "mesh_hash": expected_hash, "section": expected_section,
                    "material_ref": expected_ref if expected_ref is not None else ref,
                    "position_divisor": 512.0, "max_bones": 32,
                    "source_section_count": len(sections),
                    "detected_from_cached_vanilla_target": True,
                }
        except Exception:
            pass

    # Saved-project self-heal: older .blend files may predate the target-cache
    # feature entirely.  Player 000 has a pristine 10-section VANILLA template
    # bundled with the add-on.  Recover/cache that schema automatically instead
    # of asking the user to re-import vanilla.  This is NOT the friend's
    # completed one-section mod; one-section outputs are never accepted here.
    fallback = fallback_template_path_for_hash(expected_hash)
    if fallback:
        try:
            mesh = parse_mesh(fallback)
            if int(mesh.filename_hash) == expected_hash and len(mesh.sections) > expected_section:
                ref = _u32(mesh.sections[expected_section].material_ref_serialized, 0)
                valid = True
                if target == "000":
                    valid = (len(mesh.sections) == expected_count and ref == expected_ref)
                elif target == "001":
                    valid = len(mesh.sections) > 1
                if valid:
                    snap_text = cache_target_template(col, mesh)
                    col["sm3_mesh_hash"] = f"0x{expected_hash:08X}"
                    col["sm3_resource_name"] = "ch_spiderman000" if target == "000" else "ch_spiderman001"
                    col["sm3_is_export_target"] = True
                    col["sm3_spider_stock_template_recovered"] = True
                    col["sm3_spider_stock_template_path"] = fallback
                    div = col.get(DIVISOR_PROP)
                    for obj in _collection_meshes(col):
                        obj[CACHE_PROP] = snap_text
                        obj["sm3_target_cache_ready"] = True
                        obj["sm3_export_target_mesh_hash"] = f"0x{expected_hash:08X}"
                        if getattr(obj, "data", None) is not None:
                            obj.data[CACHE_PROP] = snap_text
                            obj.data["sm3_target_cache_ready"] = True
                            obj.data["sm3_export_target_mesh_hash"] = f"0x{expected_hash:08X}"
                        if div:
                            obj[DIVISOR_PROP] = div
                            if getattr(obj, "data", None) is not None:
                                obj.data[DIVISOR_PROP] = div
                    return {
                        "target": target, "mesh_hash": expected_hash, "section": expected_section,
                        "material_ref": expected_ref if expected_ref is not None else ref,
                        "position_divisor": 512.0, "max_bones": 32,
                        "source_section_count": len(mesh.sections),
                        "detected_from_bundled_vanilla_target": True,
                    }
        except Exception:
            pass

    # Cold/import-stage fallback: accept the untouched imported pieces and let
    # prep cache/stamp them.  This path is not required for an already-saved
    # joined project.
    pieces = [o for o in col.objects if o.type == "MESH"]
    pieces.sort(key=lambda o: int(o.get("sm3_section_index", 999999)))
    by_section = {int(o.get("sm3_section_index", -1)): o for o in pieces}
    piece = by_section.get(expected_section)
    if target == "000" and len(pieces) == expected_count and piece is not None:
        ref = _u32(piece.get("sm3_serialized_material_ref", piece.data.get("sm3_serialized_material_ref", 0)), 0)
        if ref == expected_ref:
            return {"target": target, "mesh_hash": expected_hash, "section": expected_section, "material_ref": expected_ref, "position_divisor": 512.0, "max_bones": 32, "source_section_count": expected_count, "detected_from_full_vanilla_target": True}
    if target == "001" and len(pieces) > 1 and piece is not None:
        ref = _u32(piece.get("sm3_serialized_material_ref", piece.data.get("sm3_serialized_material_ref", 0)), 0)
        return {"target": target, "mesh_hash": expected_hash, "section": expected_section, "material_ref": ref, "position_divisor": 512.0, "max_bones": 32, "source_section_count": len(pieces), "detected_from_full_vanilla_target": True}

    return {
        "target": target, "invalid_reference_output": True, "reason": "NO_CACHED_VANILLA_SCHEMA",
        "piece_count": len(pieces), "expected_piece_count": expected_count,
    }

def _find_full_vanilla_spider_target(target_code="000"):
    """Return a validated FULL VANILLA Spider target already imported in the scene.

    Completed one-section mods/reference outputs are ignored.  This makes the
    atlas workflow resistant to the common case where the collection dropdown
    is still pointing at a friend/reference mod from comparison testing.
    """
    for candidate in bpy.data.collections:
        profile = _spider_atlas_target_profile(candidate)
        if not profile or profile.get("invalid_reference_output"):
            continue
        if str(profile.get("target", "")) == str(target_code):
            return candidate, profile
    return None, None


def _remove_imported_collection(col):
    """Best-effort cleanup for a failed dedicated vanilla-target import."""
    if col is None:
        return
    try:
        for obj in list(col.objects):
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception:
                pass
        bpy.data.collections.remove(col)
    except Exception:
        pass


def _clear_mesh_geometry(obj):
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.clear()
        bm.to_mesh(obj.data)
        obj.data.update()
    finally:
        bm.free()


def _set_zero_vertex_color(obj):
    mesh = getattr(obj, "data", None)
    if mesh is None or not hasattr(mesh, "color_attributes"):
        return
    for attr in list(mesh.color_attributes):
        mesh.color_attributes.remove(attr)
    attr = mesh.color_attributes.new(name="Col_0", type="BYTE_COLOR", domain="CORNER")
    for item in attr.data:
        if hasattr(item, "color_srgb"):
            item.color_srgb = (0.0, 0.0, 0.0, 1.0)
        else:
            item.color = (0.0, 0.0, 0.0, 1.0)



# -----------------------------------------------------------------------------
# dual-project Spider atlas UV transfer
# -----------------------------------------------------------------------------

_ATLAS_SOURCE_UV_NAMES = {"SM3_SRC", "SM3_SRC_MASTER"}


def _preferred_atlas_uv_layer(obj):
    mesh = getattr(obj, "data", None)
    if obj is None or obj.type != "MESH" or mesh is None or not mesh.uv_layers:
        return None
    preferred = str(obj.get("sm3_friend_atlas_uv_export", "") or "").strip()
    if preferred and preferred in mesh.uv_layers and preferred not in _ATLAS_SOURCE_UV_NAMES:
        return mesh.uv_layers.get(preferred)
    for layer in mesh.uv_layers:
        if getattr(layer, "active_render", False) and layer.name not in _ATLAS_SOURCE_UV_NAMES:
            return layer
    if mesh.uv_layers.active and mesh.uv_layers.active.name not in _ATLAS_SOURCE_UV_NAMES:
        return mesh.uv_layers.active
    if "UVMap_0" in mesh.uv_layers:
        return mesh.uv_layers.get("UVMap_0")
    return next((layer for layer in mesh.uv_layers if layer.name not in _ATLAS_SOURCE_UV_NAMES), None)


def _qpos(co, digits=5):
    return tuple(round(float(v), digits) for v in co[:3])


def _poly_geom_key(mesh, poly, digits=5):
    return tuple(sorted(_qpos(mesh.vertices[i].co, digits) for i in poly.vertices))


def _mesh_topology_signature(mesh):
    h = hashlib.sha256()
    h.update(f"v={len(mesh.vertices)};p={len(mesh.polygons)};l={len(mesh.loops)};".encode("ascii"))
    for poly in mesh.polygons:
        h.update((",".join(str(int(i)) for i in poly.vertices) + ";").encode("ascii"))
    return h.hexdigest()


def _find_current_atlas_image(obj):
    # Prefer an image actually used by the active object's materials.
    candidates = []
    for slot in getattr(obj, "material_slots", ()):
        mat = getattr(slot, "material", None)
        if not mat or not getattr(mat, "use_nodes", False) or not mat.node_tree:
            continue
        for node in mat.node_tree.nodes:
            if node.bl_idname == "ShaderNodeTexImage" and getattr(node, "image", None) is not None:
                img = node.image
                score = 0
                low = (img.name + " " + str(getattr(img, "filepath", ""))).casefold()
                if "atlas" in low: score += 4
                if "spider" in low: score += 2
                candidates.append((score, img))
    if not candidates:
        for img in bpy.data.images:
            low = (img.name + " " + str(getattr(img, "filepath", ""))).casefold()
            score = (4 if "atlas" in low else 0) + (2 if "spider" in low else 0)
            if score:
                candidates.append((score, img))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _apply_atlas_preview_material(obj, image, uv_name):
    if obj is None or obj.type != "MESH" or image is None:
        return None
    mat = bpy.data.materials.get("SM3_ATLAS_TRANSFER_PREVIEW") or bpy.data.materials.new("SM3_ATLAS_TRANSFER_PREVIEW")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    tex = nt.nodes.new("ShaderNodeTexImage")
    uv = nt.nodes.new("ShaderNodeUVMap")
    uv.uv_map = str(uv_name)
    tex.image = image
    try:
        tex.interpolation = 'Linear'
        tex.extension = 'REPEAT'
    except Exception:
        pass
    nt.links.new(uv.outputs.get("UV"), tex.inputs.get("Vector"))
    nt.links.new(tex.outputs.get("Color"), bsdf.inputs.get("Base Color"))
    nt.links.new(bsdf.outputs.get("BSDF"), out.inputs.get("Surface"))
    # Preview-only: dedicated Spider exporters ignore Blender material identity and
    # force native 0x0614/0x0560.  Use one preview slot so the viewport exactly
    # shows the shared atlas without affecting the native export reference.
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    for poly in obj.data.polygons:
        poly.material_index = 0
    obj["sm3_atlas_transfer_preview"] = True
    obj["sm3_atlas_transfer_preview_image"] = image.name
    return mat


class EXPORT_OT_SM3_AtlasUVTransfer(Operator, ExportHelper):
    bl_idname = "export_scene.sm3_spider_atlas_uv_transfer"
    bl_label = "Export Spider Atlas UV Transfer"
    bl_description = "From the good Spider 000 project, save the exact packed atlas UV layout for the separate Spider 001 project"
    filename_ext = ".sm3atlasuv.json"
    filter_glob: StringProperty(default="*.sm3atlasuv.json", options={'HIDDEN'})

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.type != "MESH":
            self.report({'ERROR'}, "Select the prepared custom Spider mesh in the 000 project")
            return {'CANCELLED'}
        layer = _preferred_atlas_uv_layer(obj)
        if layer is None:
            self.report({'ERROR'}, "No packed atlas UV layer was found")
            return {'CANCELLED'}
        if layer.name in _ATLAS_SOURCE_UV_NAMES:
            self.report({'ERROR'}, f"Refusing to transfer source UV layer {layer.name}; packed atlas UV is required")
            return {'CANCELLED'}
        mesh = obj.data
        polygons = []
        for poly in mesh.polygons:
            corners = []
            for li in poly.loop_indices:
                vi = int(mesh.loops[li].vertex_index)
                co = mesh.vertices[vi].co
                uv = layer.data[li].uv
                corners.append({
                    "vertex": vi,
                    "position": [float(co.x), float(co.y), float(co.z)],
                    "uv": [float(uv.x), float(uv.y)],
                })
            polygons.append({"vertices": [int(v) for v in poly.vertices], "corners": corners})
        image = _find_current_atlas_image(obj)
        image_path = ""
        image_name = ""
        if image is not None:
            image_name = image.name
            raw = str(getattr(image, "filepath", "") or "")
            if raw:
                try:
                    image_path = bpy.path.abspath(raw)
                except Exception:
                    image_path = raw
        data = {
            "format": "SM3_SPIDER_ATLAS_UV_TRANSFER_V1",
            "source_target": str(obj.get("sm3_spider_atlas_target", "000") or "000"),
            "source_object": obj.name,
            "uv_layer": layer.name,
            "vertex_count": len(mesh.vertices),
            "polygon_count": len(mesh.polygons),
            "loop_count": len(mesh.loops),
            "topology_signature": _mesh_topology_signature(mesh),
            "atlas_image_name": image_name,
            "atlas_image_path": image_path,
            "polygons": polygons,
        }
        out = Path(self.filepath)
        if not str(out).lower().endswith(".sm3atlasuv.json"):
            out = Path(str(out) + ".sm3atlasuv.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, indent=2), encoding="utf-8")
        obj["sm3_atlas_uv_transfer_last_path"] = str(out)
        self.report({'INFO'}, f"Exported exact atlas UV transfer: {layer.name} | {len(mesh.polygons)} polygons")
        return {'FINISHED'}


class IMPORT_OT_SM3_AtlasUVTransfer(Operator, ImportHelper):
    bl_idname = "import_scene.sm3_spider_atlas_uv_transfer"
    bl_label = "Import Spider Atlas UV Transfer"
    bl_description = "In the separate Spider 001 project, copy the exact packed UV layout from Spider 000 and apply the same atlas preview when available"
    filename_ext = ".json"
    filter_glob: StringProperty(default="*.sm3atlasuv.json;*.json", options={'HIDDEN'})

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.type != "MESH":
            self.report({'ERROR'}, "Select the joined custom Spider mesh in the 001 project")
            return {'CANCELLED'}
        try:
            data = json.loads(Path(self.filepath).read_text(encoding="utf-8"))
        except Exception as exc:
            self.report({'ERROR'}, f"Could not read atlas UV transfer: {exc}")
            return {'CANCELLED'}
        if data.get("format") != "SM3_SPIDER_ATLAS_UV_TRANSFER_V1":
            self.report({'ERROR'}, "Not a supported SM3 Spider atlas UV transfer file")
            return {'CANCELLED'}
        mesh = obj.data
        src_polys = data.get("polygons") or []
        if len(src_polys) != len(mesh.polygons) or int(data.get("loop_count", -1)) != len(mesh.loops):
            self.report({'ERROR'}, f"Topology mismatch: transfer has {len(src_polys)} polygons/{data.get('loop_count')} loops; current mesh has {len(mesh.polygons)} polygons/{len(mesh.loops)} loops")
            return {'CANCELLED'}

        uv_name = str(data.get("uv_layer") or "UVMap_0")
        if uv_name in _ATLAS_SOURCE_UV_NAMES:
            uv_name = "UVMap_0"
        layer = mesh.uv_layers.get(uv_name) or mesh.uv_layers.new(name=uv_name)

        direct = (
            int(data.get("vertex_count", -1)) == len(mesh.vertices)
            and str(data.get("topology_signature", "")) == _mesh_topology_signature(mesh)
        )
        transferred = 0
        if direct:
            for poly, src in zip(mesh.polygons, src_polys):
                corners = src.get("corners") or []
                if len(corners) != len(poly.loop_indices):
                    raise RuntimeError("Corner count mismatch during direct UV transfer")
                for li, corner in zip(poly.loop_indices, corners):
                    layer.data[li].uv = corner["uv"]
                    transferred += 1
            mode = "exact topology"
        else:
            # Robust path for separate .blend projects that duplicated/split
            # vertices but kept the same geometry. Match triangles by the actual
            # corner positions, then assign UVs corner-for-corner by nearest point.
            lookup = {}
            for idx, src in enumerate(src_polys):
                corners = src.get("corners") or []
                key = tuple(sorted(_qpos(c.get("position", (0,0,0))) for c in corners))
                lookup.setdefault(key, []).append((idx, src))
            used = set()
            failures = []
            for poly in mesh.polygons:
                key = _poly_geom_key(mesh, poly)
                candidates = [it for it in lookup.get(key, []) if it[0] not in used]
                if not candidates:
                    failures.append(int(poly.index)); continue
                src_idx, src = candidates[0]
                used.add(src_idx)
                src_corners = src.get("corners") or []
                for li in poly.loop_indices:
                    vi = int(mesh.loops[li].vertex_index)
                    p = mesh.vertices[vi].co
                    best = min(src_corners, key=lambda c: sum((float(p[k]) - float(c["position"][k]))**2 for k in range(3)))
                    dist2 = sum((float(p[k]) - float(best["position"][k]))**2 for k in range(3))
                    if dist2 > 1.0e-8:
                        failures.append(int(poly.index)); break
                    layer.data[li].uv = best["uv"]
                    transferred += 1
            if failures:
                self.report({'ERROR'}, f"Atlas UV geometry match failed on {len(set(failures))} polygons. The 000 and 001 custom meshes must be the same geometry.")
                return {'CANCELLED'}
            mode = "geometry matched"

        mesh.uv_layers.active = layer
        for uv in mesh.uv_layers:
            try:
                uv.active_render = (uv == layer)
            except Exception:
                pass
        obj["sm3_friend_atlas_uv_export"] = layer.name
        obj["sm3_atlas_uv_transfer_imported"] = True
        obj["sm3_atlas_uv_transfer_source"] = str(self.filepath)
        obj["sm3_atlas_uv_transfer_mode"] = mode

        image_path = str(data.get("atlas_image_path") or "")
        preview = False
        if image_path and os.path.isfile(image_path):
            try:
                img = bpy.data.images.load(image_path, check_existing=True)
                _apply_atlas_preview_material(obj, img, layer.name)
                preview = True
            except Exception:
                preview = False
        self.report({'INFO'}, f"Imported exact 000 atlas UV into 001: {layer.name} | {mode} | preview {'ON' if preview else 'needs PNG'}")
        return {'FINISHED'}


class IMPORT_OT_SM3_AtlasPreviewImage(Operator, ImportHelper):
    bl_idname = "import_scene.sm3_spider_atlas_preview_image"
    bl_label = "Load Shared Atlas Preview"
    bl_description = "If the 001 project is pink, choose the same atlas_final PNG used by 000 and preview it with the transferred UVMap_0"
    filename_ext = ".png"
    filter_glob: StringProperty(default="*.png;*.dds;*.tga;*.jpg;*.jpeg", options={'HIDDEN'})

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.type != "MESH":
            self.report({'ERROR'}, "Select the 001 custom Spider mesh")
            return {'CANCELLED'}
        layer = _preferred_atlas_uv_layer(obj)
        if layer is None:
            self.report({'ERROR'}, "Import the 000 atlas UV transfer first")
            return {'CANCELLED'}
        try:
            img = bpy.data.images.load(self.filepath, check_existing=True)
            _apply_atlas_preview_material(obj, img, layer.name)
            obj["sm3_atlas_transfer_manual_preview_path"] = self.filepath
        except Exception as exc:
            self.report({'ERROR'}, f"Could not load atlas preview: {exc}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Shared atlas preview applied using {layer.name}")
        return {'FINISHED'}

# -----------------------------------------------------------------------------
# export collection list (same simple idea as WoS toolkit)
# -----------------------------------------------------------------------------

class SM3ExportCollectionItem(PropertyGroup):
    name: StringProperty()
    export: BoolProperty(name="Export", default=True)


def populate_export_collections():
    scene = bpy.context.scene
    scene.sm3_export_collections.clear()
    for col in sorted(_sm3_target_collections(), key=lambda c: c.name.casefold()):
        item = scene.sm3_export_collections.add()
        item.name = col.name
        item.export = True


# -----------------------------------------------------------------------------
# MESH IMPORT
# -----------------------------------------------------------------------------

class IMPORT_OT_SM3_Mesh(Operator):
    bl_idname = "import_scene.sm3_mesh_importer"
    bl_label = "Import Mesh (SM3)"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".mesh"
    filter_glob: StringProperty(default="*.mesh;*.wrap.mesh", options={'HIDDEN'})
    files: CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: StringProperty(subtype="DIR_PATH")

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.label(text="Import Options")
        layout.prop(scene, "sm3_flip_uv_v_axis")
        layout.prop(scene, "sm3_reverse_winding")
        layout.prop(scene, "sm3_convert_triangle_list")

    def execute(self, context):
        if not self.files:
            self.report({'WARNING'}, "No files received")
            return {'CANCELLED'}
        imported = 0
        last_col = None
        for file in self.files:
            filepath = os.path.join(self.directory, file.name)
            try:
                col, _objects, _arm, _mesh = _import_mesh_path(filepath, context.scene)
                last_col = col
                imported += 1
            except Exception as exc:
                _write_last_error(f"SM3 MESH IMPORT FAILED: {file.name}")
                self.report({'ERROR'}, f"{file.name}: {exc} | See Text Editor > SM3_Last_Error")
                return {'CANCELLED'}
        if last_col is not None:
            context.scene.sm3_collection_search_dropdown = last_col
        self.report({'INFO'}, f"Imported {imported} SM3 MESH file(s)")
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class IMPORT_OT_SM3_Mesh_Drop(Operator):
    bl_idname = "import_scene.sm3_mesh_drag_drop"
    bl_label = "Import Mesh (SM3)"
    bl_options = {'REGISTER', 'UNDO'}

    files: CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: StringProperty(subtype="DIR_PATH")

    def execute(self, context):
        if not self.files:
            return {'CANCELLED'}
        imported = 0
        for file in self.files:
            try:
                col, _objects, _arm, _mesh = _import_mesh_path(os.path.join(self.directory, file.name), context.scene)
                context.scene.sm3_collection_search_dropdown = col
                imported += 1
            except Exception as exc:
                _write_last_error(f"SM3 MESH IMPORT FAILED: {file.name}")
                self.report({'ERROR'}, f"{file.name}: {exc}")
                return {'CANCELLED'}
        self.report({'INFO'}, f"Imported {imported} SM3 MESH file(s)")
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# SKELETON IMPORT
# -----------------------------------------------------------------------------

class IMPORT_OT_SM3_Skeleton(Operator):
    bl_idname = "import_scene.sm3_skeleton_importer"
    bl_label = "Import Skeleton (SM3)"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".skel"
    filter_glob: StringProperty(default="*.skel;*.wrap.skel", options={'HIDDEN'})
    files: CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: StringProperty(subtype="DIR_PATH")

    def execute(self, context):
        if not self.files:
            self.report({'WARNING'}, "No files received")
            return {'CANCELLED'}
        imported = 0
        for file in self.files:
            filepath = os.path.join(self.directory, file.name)
            try:
                arm, _skel = _import_skel_path(filepath)
                target_col = _guess_collection_for_skeleton(filepath)
                if target_col is not None:
                    context.scene.sm3_collection_search_dropdown = target_col
                imported += 1
            except Exception as exc:
                _write_last_error(f"SM3 SKEL IMPORT FAILED: {file.name}")
                self.report({'ERROR'}, f"{file.name}: {exc} | See Text Editor > SM3_Last_Error")
                return {'CANCELLED'}
        self.report({'INFO'}, f"Imported {imported} SM3 SKEL file(s)")
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class IMPORT_OT_SM3_Skeleton_Drop(Operator):
    bl_idname = "import_scene.sm3_skeleton_drag_drop"
    bl_label = "Import Skeleton (SM3)"
    bl_options = {'REGISTER', 'UNDO'}

    files: CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: StringProperty(subtype="DIR_PATH")

    def execute(self, context):
        if not self.files:
            return {'CANCELLED'}
        for file in self.files:
            filepath = os.path.join(self.directory, file.name)
            try:
                _import_skel_path(filepath)
                target_col = _guess_collection_for_skeleton(filepath)
                if target_col is not None:
                    context.scene.sm3_collection_search_dropdown = target_col
            except Exception as exc:
                _write_last_error(f"SM3 SKEL IMPORT FAILED: {file.name}")
                self.report({'ERROR'}, f"{file.name}: {exc}")
                return {'CANCELLED'}
        self.report({'INFO'}, f"Imported {len(self.files)} SM3 SKEL file(s)")
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# MESH EXPORT
# -----------------------------------------------------------------------------

class EXPORT_OT_SM3_Mesh(Operator, ExportHelper):
    bl_idname = "export_scene.sm3_mesh_exporter"
    bl_label = "Export Mesh (SM3)"
    bl_options = {'PRESET'}

    filename_ext = ".mesh"
    filter_glob: StringProperty(default="*.mesh", options={'HIDDEN'})

    def invoke(self, context, event):
        populate_export_collections()
        if not self.filepath:
            self.filepath = "SM3_EXPORT.mesh"
        return super().invoke(context, event)

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.label(text="Collections:")
        if not scene.sm3_export_collections:
            layout.label(text="Import an SM3 MESH first", icon='INFO')
        for item in scene.sm3_export_collections:
            layout.prop(item, "export", text=item.name)
        layout.separator()
        layout.label(text="Export Options")
        layout.prop(scene, "sm3_flip_uv_v_axis")
        layout.prop(scene, "sm3_reverse_winding")
        layout.prop(scene, "sm3_vertex_color_mode", text="Vertex Color")

    def execute(self, context):
        base_dir = os.path.dirname(self.filepath)
        os.makedirs(base_dir, exist_ok=True)
        exported = 0
        for item in context.scene.sm3_export_collections:
            if not item.export:
                continue
            col = bpy.data.collections.get(item.name)
            if not _is_sm3_target_collection(col):
                continue
            mesh_objects = _collection_meshes(col)
            if not mesh_objects:
                self.report({'WARNING'}, f"{col.name}: no MESH objects; skipped")
                continue
            try:
                for obj in mesh_objects:
                    _set_export_color_attribute(obj, context.scene.sm3_vertex_color_mode)
                output_path = os.path.join(base_dir, _source_basename(col))
                export_objects_to_target_mesh(
                    mesh_objects,
                    col,
                    output_path,
                    max_bones=context.scene.sm3_max_bones,
                    flip_uv_v=context.scene.sm3_flip_uv_v_axis,
                    reverse_winding=context.scene.sm3_reverse_winding,
                    write_report=False,
                )
                exported += 1
            except Exception as exc:
                _write_last_error(f"SM3 MESH EXPORT FAILED: {col.name}")
                self.report({'ERROR'}, f"{col.name}: {exc} | See Text Editor > SM3_Last_Error")
                return {'CANCELLED'}
        if exported == 0:
            self.report({'WARNING'}, "No SM3 collections were exported")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Exported {exported} SM3 MESH collection(s)")
        return {'FINISHED'}



class EXPORT_OT_SM3_PreparedSpiderAtlas(Operator, ExportHelper):
    """Dedicated weight-safe export path for the prepared Spider atlas mesh.

    v1.8.4 keeps the v1.8.3 weight-safe smart sections and adds a controlled
    neutral-vs-legacy vertex-color shading test without changing atlas/material.
    """
    bl_idname = "export_scene.sm3_prepared_spider_atlas"
    bl_label = "Export PREPARED Spider Atlas Mesh"
    bl_options = {'PRESET'}

    filename_ext = ".mesh"
    filter_glob: StringProperty(default="*.mesh", options={'HIDDEN'})

    def invoke(self, context, event):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, "Select the prepared joined Spider-Man mesh")
            return {'CANCELLED'}
        if not bool(obj.get('sm3_spider_atlas_prepared', False)) or str(obj.get('sm3_spider_atlas_build', '')) not in ('1.3.0', '1.3.1', '1.3.2', '1.3.3', '1.3.4', '1.3.5', '1.3.6', '1.3.7', '1.3.8'):
            self.report({'ERROR'}, "Run PREP CURRENT JOINED MESH first (v1.3.5 through v1.3.8 prep is accepted)")
            return {'CANCELLED'}
        target = str(obj.get('sm3_spider_atlas_target', '000'))
        self.filepath = '0xAC92103D.ch_spiderman000.mesh' if target == '000' else '0xAC92103E.ch_spiderman001.mesh'
        return super().invoke(context, event)

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, "Select the prepared joined Spider-Man mesh")
            return {'CANCELLED'}
        if not bool(obj.get('sm3_spider_atlas_prepared', False)) or str(obj.get('sm3_spider_atlas_build', '')) not in ('1.3.0', '1.3.1', '1.3.2', '1.3.3', '1.3.4', '1.3.5', '1.3.6', '1.3.7', '1.3.8'):
            self.report({'ERROR'}, "This object is not prepared by a supported Spider atlas build")
            return {'CANCELLED'}
        col = next((c for c in obj.users_collection if bool(c.get('sm3_spider_atlas_prepared', False))), None)
        if col is None:
            col = obj.users_collection[0] if obj.users_collection else None
        if col is None:
            self.report({'ERROR'}, "Prepared mesh is not inside an SM3 target collection")
            return {'CANCELLED'}
        try:
            shading_profile = getattr(context.scene, 'sm3_spider_shading_test_profile', 'NEUTRAL_BLACK')
            shading_vertex_mode = _apply_spider_shading_profile(obj, shading_profile)
            result = export_prepared_spider_atlas_one_section(
                obj, col, self.filepath,
                flip_uv_v=context.scene.sm3_flip_uv_v_axis,
                reverse_winding=context.scene.sm3_reverse_winding,
                write_report=True,
            )
            rep = getattr(result, 'report', {}) or {}
            section_count = int(rep.get('output_section_count', 0))
            if section_count < 1:
                raise ValueError("Weight-safe Spider export produced no sections")
            decisions = rep.get('section_decisions') or []
            bad_mats = [d.get('material_ref_serialized') for d in decisions
                        if str(d.get('material_ref_serialized', '')).upper() not in ('0X00000614', '0X0614')]
            if bad_mats:
                raise ValueError(f"Weight-safe Spider export lost 0x0614 routing: {bad_mats}")
            bad_palettes = [d.get('bone_palette_count') for d in decisions if int(d.get('bone_palette_count', 999)) > 32]
            if bad_palettes:
                raise ValueError(f"Weight-safe Spider export exceeded 32-bone section limit: {bad_palettes}")
            if int(rep.get('spider_bone_remap_count', -1)) != 0 or not bool(rep.get('weights_preserved', False)):
                raise ValueError("Weight-safe Spider export unexpectedly remapped bones")
            self.report({'INFO'}, f"Spider atlas WEIGHT-SAFE export: {section_count} section(s) | 0x0614 | NO BONE REMAP | VCOL {shading_vertex_mode}")
            return {'FINISHED'}
        except Exception as exc:
            _write_last_error("DEDICATED SPIDER ATLAS EXPORT FAILED")
            self.report({'ERROR'}, f"{exc} | See Text Editor > SM3_Last_Error")
            return {'CANCELLED'}


class EXPORT_OT_SM3_PreparedSpiderAtlas001(Operator, ExportHelper):
    """Export OUR prepared atlas mesh through the stock Spider 001 canvas.

    The proven working CH_SPIDERMAN mod replaces BOTH ch_spiderman000 and
    ch_spiderman001.  001 uses stock section 3 / material 0x00000560 and the
    same packed atlas UVs / 32-bone palette.
    """
    bl_idname = "export_scene.sm3_prepared_spider_atlas_001"
    bl_label = "Export PREPARED Spider 001 Atlas Mesh"
    bl_options = {'PRESET'}

    filename_ext = ".mesh"
    filter_glob: StringProperty(default="*.mesh", options={'HIDDEN'})

    def invoke(self, context, event):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, "Select the prepared joined Spider-Man mesh")
            return {'CANCELLED'}
        if not bool(obj.get('sm3_spider_atlas_prepared', False)):
            self.report({'ERROR'}, "Run PREP CURRENT JOINED MESH first")
            return {'CANCELLED'}
        self.filepath = '0xAC92103E.ch_spiderman001.mesh'
        return super().invoke(context, event)

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, "Select the prepared joined Spider-Man mesh")
            return {'CANCELLED'}
        col = next((c for c in obj.users_collection if bool(c.get('sm3_spider_atlas_prepared', False))), None)
        if col is None:
            col = obj.users_collection[0] if obj.users_collection else None
        if col is None:
            self.report({'ERROR'}, "Prepared mesh is not inside an SM3 target collection")
            return {'CANCELLED'}
        try:
            result = export_prepared_spider_atlas_one_section_001(
                obj, col, self.filepath,
                flip_uv_v=context.scene.sm3_flip_uv_v_axis,
                reverse_winding=context.scene.sm3_reverse_winding,
                write_report=True,
            )
            rep = getattr(result, 'report', {}) or {}
            section_count = int(rep.get('output_section_count', 0))
            if section_count < 1:
                raise ValueError("Spider 001 weight-safe export produced no sections")
            decisions = rep.get('section_decisions') or []
            bad_mats = [d.get('material_ref_serialized') for d in decisions
                        if str(d.get('material_ref_serialized', '')).upper() not in ('0X00000560', '0X0560', '0X560')]
            if bad_mats:
                raise ValueError(f"Spider 001 export lost 0x0560 routing: {bad_mats}")
            bad_palettes = [d.get('bone_palette_count') for d in decisions if int(d.get('bone_palette_count', 999)) > 32]
            if bad_palettes:
                raise ValueError(f"Spider 001 exceeded 32-bone section limit: {bad_palettes}")
            if int(rep.get('spider_bone_remap_count', -1)) != 0 or not bool(rep.get('weights_preserved', False)):
                raise ValueError("Spider 001 weight-safe export unexpectedly remapped bones")
            self.report({'INFO'}, f"Spider 001 WEIGHT-SAFE export: {section_count} section(s) | 0x0560 | NO BONE REMAP")
            return {'FINISHED'}
        except Exception as exc:
            _write_last_error("DEDICATED SPIDER 001 ATLAS EXPORT FAILED")
            self.report({'ERROR'}, f"{exc} | See Text Editor > SM3_Last_Error")
            return {'CANCELLED'}



# -----------------------------------------------------------------------------
# SKELETON EXPORT
# -----------------------------------------------------------------------------

class EXPORT_OT_SM3_Skeleton(Operator, ExportHelper):
    bl_idname = "export_scene.sm3_skeleton_exporter"
    bl_label = "Export Skeleton (SM3)"
    bl_options = {'PRESET'}

    filename_ext = ".skel"
    filter_glob: StringProperty(default="*.skel", options={'HIDDEN'})

    def execute(self, context):
        base_dir = os.path.dirname(self.filepath)
        os.makedirs(base_dir, exist_ok=True)
        armatures = [
            obj for obj in bpy.data.objects
            if obj.type == "ARMATURE" and obj.get("sm3_source_skel")
        ]
        if not armatures:
            self.report({'WARNING'}, "No imported SM3 armatures found")
            return {'CANCELLED'}
        exported = 0
        for arm in armatures:
            source = str(arm.get("sm3_source_skel", "") or "")
            basename = Path(source).name if source else f"{arm.name}.skel"
            try:
                safe_export_skeleton(arm, os.path.join(base_dir, basename))
                exported += 1
            except Exception as exc:
                _write_last_error(f"SM3 SKEL EXPORT FAILED: {arm.name}")
                self.report({'ERROR'}, f"{arm.name}: {exc} | See Text Editor > SM3_Last_Error")
                return {'CANCELLED'}
        self.report({'INFO'}, f"Exported {exported} SM3 SKEL file(s)")
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# RENAME VERTEX GROUPS (WoS-style bone index -> imported SM3 skeleton names)
# -----------------------------------------------------------------------------

class SM3_OT_RenameVertexGroups(Operator):
    bl_idname = "sm3.rename_vertex_groups"
    bl_label = "Rename Vertex Groups (by Bone Index)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        col = context.scene.sm3_collection_search_dropdown
        if col is None:
            self.report({'ERROR'}, "Choose an SM3 collection first")
            return {'CANCELLED'}
        try:
            # WoS returns one integer: the number of groups renamed.
            # Accept the older tuple result too so an in-memory module from a
            # previous toolkit revision cannot trigger "cannot unpack".
            result = rename_vertex_groups_from_armature(col)
            if isinstance(result, (tuple, list)):
                renamed = int(result[0]) if result else 0
            else:
                renamed = int(result)
            already = int(col.get("sm3_last_rename_already", 0) or 0)
            repaired = int(col.get("sm3_last_rename_armature_repaired", 0) or 0)
            if renamed == 0 and already > 0:
                self.report({'INFO'}, f"Vertex groups already mapped ({already}) in '{col.name}'")
            else:
                extra = f"; repaired {repaired} armature bone name(s)" if repaired else ""
                self.report({'INFO'}, f"Renamed {renamed} vertex groups in '{col.name}'{extra}")
            return {'FINISHED'}
        except Exception as exc:
            _write_last_error(f"SM3 RENAME VERTEX GROUPS FAILED: {col.name}")
            self.report({'ERROR'}, f"{exc} | See Text Editor > SM3_Last_Error")
            return {'CANCELLED'}


# -----------------------------------------------------------------------------
# RENAME WEIGHTS BY PROXIMITY (same simple workflow as WoS)
# -----------------------------------------------------------------------------

def _group_centroids(obj):
    result = []
    info = {g.index: {"coords": [], "name": g.name} for g in obj.vertex_groups}
    matrix = obj.matrix_world
    for vert in obj.data.vertices:
        for membership in vert.groups:
            if membership.group in info:
                info[membership.group]["coords"].append(matrix @ vert.co)
    for data in info.values():
        if data["coords"]:
            avg = sum(data["coords"], Vector((0.0, 0.0, 0.0))) / len(data["coords"])
            result.append({"pos": avg, "name": data["name"]})
    return result


class SM3_OT_RenameWeightsProximity(Operator):
    bl_idname = "sm3.rename_weights_proximity"
    bl_label = "Rename Weights by Proximity"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        source = context.scene.sm3_rename_weights_source
        target = context.scene.sm3_rename_weights_target
        if not source or not target or source.type != "MESH" or target.type != "MESH":
            self.report({'ERROR'}, "Choose Source and Target MESH objects")
            return {'CANCELLED'}
        source_centroids = _group_centroids(source)
        if not source_centroids:
            self.report({'ERROR'}, "Source object has no weighted vertex groups")
            return {'CANCELLED'}

        # Temporary names prevent Blender from merging/colliding names midway.
        for vg in target.vertex_groups:
            vg.name = f"{vg.name}_SM3_TEMP_{vg.index}"

        matrix = target.matrix_world
        renamed = 0
        for vg in target.vertex_groups:
            coords = []
            for vert in target.data.vertices:
                if any(m.group == vg.index for m in vert.groups):
                    coords.append(matrix @ vert.co)
            if not coords:
                continue
            center = sum(coords, Vector((0.0, 0.0, 0.0))) / len(coords)
            closest = min(source_centroids, key=lambda row: (center - row["pos"]).length)
            vg.name = closest["name"]
            renamed += 1

        armature = _armature_from_object(source)
        if armature is not None:
            _attach_armature_modifier(target, armature)

        self.report({'INFO'}, f"Renamed {renamed} target groups on '{target.name}'")
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# FULL VANILLA SPIDER TARGET HELPERS
# -----------------------------------------------------------------------------

class SM3_OT_AutoFindVanillaSpiderTarget(Operator):
    bl_idname = "sm3.auto_find_vanilla_spider_target"
    bl_label = "Auto-Find FULL VANILLA Spider 000"
    bl_description = "Find an already imported 10-section vanilla ch_spiderman000 target and select it; completed one-section mods are ignored"
    bl_options = {"REGISTER"}

    def execute(self, context):
        col, profile = _find_full_vanilla_spider_target("000")
        if col is None:
            self.report({"ERROR"}, "No validated FULL VANILLA ch_spiderman000 is imported. Use 'Import FULL VANILLA Spider 000' and choose the untouched stock .mesh from the game.")
            return {"CANCELLED"}
        context.scene.sm3_collection_search_dropdown = col
        self.report({"INFO"}, f"Vanilla Spider target ready: {col.name} | 10 sections | canvas 4 | 0x0614")
        return {"FINISHED"}


class SM3_OT_ImportVanillaSpiderTarget(Operator):
    bl_idname = "sm3.import_vanilla_spider_target"
    bl_label = "Import FULL VANILLA Spider 000"
    bl_description = "Import and validate the untouched stock ch_spiderman000.mesh. Friend/completed one-section mods are rejected and removed."
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".mesh"
    filter_glob: StringProperty(default="*.mesh", options={"HIDDEN"})
    filepath: StringProperty(subtype="FILE_PATH")

    def execute(self, context):
        path = bpy.path.abspath(self.filepath)
        if not path or not os.path.isfile(path):
            self.report({"ERROR"}, "Choose the untouched stock ch_spiderman000.mesh from the game")
            return {"CANCELLED"}
        col = None
        try:
            col, _objects, _arm, _mesh = import_mesh(
                path,
                auto_find_skeleton=False,
                import_skeleton_if_found=False,
                flip_uv_v=context.scene.sm3_flip_uv_v_axis,
                reverse_winding=context.scene.sm3_reverse_winding,
                convert_to_triangle_list=context.scene.sm3_convert_triangle_list,
            )
            profile = _spider_atlas_target_profile(col)
            if not profile or profile.get("target") != "000" or profile.get("invalid_reference_output"):
                reason = (profile or {}).get("reason", "NOT_CH_SPIDERMAN000")
                count = len([o for o in col.objects if o.type == "MESH"]) if col else 0
                _remove_imported_collection(col)
                if reason == "ONE_SECTION_REFERENCE_MOD":
                    msg = "That file is a completed one-section 0x0614 mod/reference, not vanilla. It was NOT imported as the target."
                else:
                    msg = f"That file is not the validated FULL VANILLA Spider 000 target (found {count} mesh section(s); expected 10 with section 4 = 0x0614)."
                self.report({"ERROR"}, msg)
                return {"CANCELLED"}
            context.scene.sm3_collection_search_dropdown = col
            self.report({"INFO"}, f"FULL VANILLA target imported: {col.name} | section 4 / 0x0614")
            return {"FINISHED"}
        except Exception as exc:
            if col is not None:
                _remove_imported_collection(col)
            self.report({"ERROR"}, f"Vanilla target import failed: {exc}")
            return {"CANCELLED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


# -----------------------------------------------------------------------------
# EXPERIMENTAL SPIDER-MAN BAKED-ATLAS TARGET PREP
# -----------------------------------------------------------------------------

class SM3_OT_PrepareSpiderAtlasSection(Operator):
    bl_idname = "sm3.prepare_spider_atlas_section"
    bl_label = "Prepare OUR Custom Model -> Spider Atlas Section"
    bl_description = (
        "Prepare OUR selected/joined custom geometry using the original Spider target schema cached in the .blend. "
        "A full vanilla import is only a recovery fallback if that cache is missing; completed/reference mods are never targets or templates."
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        custom = context.active_object
        col = scene.sm3_collection_search_dropdown
        if custom is None or custom.type != "MESH":
            self.report({"ERROR"}, "Make the baked custom model the active MESH")
            return {"CANCELLED"}
        # Saved/export-ready projects commonly reopen with the dropdown unset
        # or pointing at a generic collection even though the ACTIVE joined mesh
        # still carries the original SM3 target cache.  Resolve the target from
        # the active mesh's own collection(s) BEFORE asking for a vanilla import.
        # The friend/completed mod remains reference-only and is never accepted.
        profile = _spider_atlas_target_profile(col) if col is not None else None

        if profile is None or profile.get("invalid_reference_output"):
            for candidate in list(getattr(custom, "users_collection", ()) or ()):
                candidate_profile = _spider_atlas_target_profile(candidate)
                if candidate_profile and not candidate_profile.get("invalid_reference_output"):
                    col = candidate
                    profile = candidate_profile
                    scene.sm3_collection_search_dropdown = candidate
                    break

        # Last fallback: a validated original/cached Spider target elsewhere in
        # the scene. This supports projects where the joined mesh was relinked.
        if profile is None or profile.get("invalid_reference_output"):
            auto_col, auto_profile = _find_full_vanilla_spider_target("000")
            if auto_col is not None:
                col = auto_col
                profile = auto_profile
                scene.sm3_collection_search_dropdown = auto_col

        if profile is None:
            self.report({"ERROR"}, "No cached Spider target schema was found for the active joined mesh. Select the joined SMWOS mesh inside the original ch_spiderman000 collection. Only if that cache is truly missing should you import the untouched stock Spider 000 mesh.")
            return {"CANCELLED"}
        if profile is None:
            self.report({"ERROR"}, "This custom-mesh atlas prep only supports ch_spiderman000 / ch_spiderman001")
            return {"CANCELLED"}
        if profile.get("invalid_reference_output"):
            reason = profile.get("reason", "INVALID_TARGET")
            if reason == "ONE_SECTION_REFERENCE_MOD":
                msg = ("STOP: that collection is the completed one-section Spider mod/reference (0x0614). "
                       "It is REFERENCE-ONLY and is never an export target/template. Import the untouched FULL VANILLA "
                       "ch_spiderman000.mesh from the game (10 sections), then select OUR custom baked mesh.")
            elif reason == "NOT_FULL_VANILLA_TARGET":
                msg = (f"Spider atlas export requires the untouched FULL VANILLA CH_SPIDERMAN target; found "
                       f"{profile.get('piece_count', '?')} mesh section(s), expected {profile.get('expected_piece_count', 'the full stock set')}. "
                       "The completed friend/reference mod is ignored as a target.")
            else:
                msg = f"Vanilla Spider target validation failed: {reason}. Import the untouched stock CH_SPIDERMAN mesh first."
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}

        pieces = [o for o in col.objects if o.type == "MESH"]
        pieces.sort(key=lambda o: int(o.get("sm3_section_index", 999999)))

        if not custom.data.uv_layers:
            self.report({"ERROR"}, "Custom model has no UV map; run the atlas rebuild first")
            return {"CANCELLED"}

        # Two supported states:
        #   A) original/full target still present -> old prep path (join OUR mesh into section 4)
        #   B) saved/export-ready project already joined -> prepare the ACTIVE joined mesh in-place
        # In both cases the ORIGINAL 10-section schema comes from the cache/profile,
        # never from the friend's completed one-section mod.
        in_place_saved_project = custom in pieces
        source_materials = [slot.material for slot in custom.material_slots]
        atlas_ref = _u32(profile.get("material_ref"), 0x00000614 if profile.get("target") == "000" else 0)

        if in_place_saved_project:
            target = custom
            # The geometry is already OUR joined/export-ready mesh. Do not delete it,
            # do not demand old vanilla objects, and do not join anything again.
            target["sm3_section_index"] = int(profile["section"])
            target.data["sm3_section_index"] = int(profile["section"])
            target["sm3_serialized_material_ref"] = int(atlas_ref)
            target.data["sm3_serialized_material_ref"] = int(atlas_ref)
            target["sm3_position_divisor"] = float(profile["position_divisor"])
            target.data["sm3_position_divisor"] = float(profile["position_divisor"])
            _set_zero_vertex_color(target)
        else:
            by_section = {int(o.get("sm3_section_index", -1)): o for o in pieces}
            target = by_section.get(int(profile["section"]))
            if target is None:
                self.report({"ERROR"}, f"Original target is missing native section {profile['section']}; use the saved-project mode only when OUR joined mesh is active inside the Spider target collection")
                return {"CANCELLED"}
            original_real = target.get("sm3_real_mat_name", target.get("sm3_real_mat_hash", ""))
            for piece in pieces:
                _clear_mesh_geometry(piece)
                piece["sm3_position_divisor"] = float(profile["position_divisor"])
                piece.data["sm3_position_divisor"] = float(profile["position_divisor"])

            target.data.materials.clear()
            for mat in source_materials:
                if mat is not None:
                    target.data.materials.append(mat)
            for uv in list(target.data.uv_layers):
                target.data.uv_layers.remove(uv)
            for ca in list(target.data.color_attributes):
                target.data.color_attributes.remove(ca)

            if custom.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            bpy.ops.object.select_all(action="DESELECT")
            custom.select_set(True)
            target.select_set(True)
            context.view_layer.objects.active = target
            bpy.ops.object.join()
            target["sm3_serialized_material_ref"] = int(atlas_ref)
            target.data["sm3_serialized_material_ref"] = int(atlas_ref)
            if original_real not in (None, ""):
                target["sm3_real_mat_name"] = original_real
            target["sm3_position_divisor"] = float(profile["position_divisor"])
            target.data["sm3_position_divisor"] = float(profile["position_divisor"])
            _set_zero_vertex_color(target)

        col["sm3_position_divisors"] = f"{int(profile['section'])}:{float(profile['position_divisor']):g}"

        armature = next((o for o in col.objects if o.type == "ARMATURE"), None)
        if armature is not None:
            for mod in list(target.modifiers):
                if mod.type == "ARMATURE" and mod.object != armature:
                    target.modifiers.remove(mod)
            _attach_armature_modifier(target, armature)

        scene.sm3_max_bones = int(profile["max_bones"])
        scene.sm3_add_white_color = False
        target["sm3_spider_atlas_prepared"] = True
        target["sm3_spider_atlas_section"] = int(profile["section"])
        target["sm3_spider_atlas_material_ref"] = int(atlas_ref)
        target["sm3_spider_atlas_max_bones"] = int(profile["max_bones"])
        target["sm3_spider_atlas_build"] = "1.3.8"
        target["sm3_spider_atlas_target"] = str(profile["target"])
        target["sm3_spider_atlas_source_section_count"] = int(profile.get("source_section_count", 10 if profile.get("target") == "000" else 0))
        target["sm3_spider_saved_project_mode"] = bool(in_place_saved_project)

        col["sm3_spider_atlas_prepared"] = True
        col["sm3_spider_atlas_section"] = int(profile["section"])
        col["sm3_spider_atlas_material_ref"] = int(atlas_ref)
        col["sm3_spider_atlas_build"] = "1.3.8"
        col["sm3_spider_atlas_target"] = str(profile["target"])
        col["sm3_spider_atlas_source_section_count"] = int(profile.get("source_section_count", 10 if profile.get("target") == "000" else 0))
        col["sm3_spider_saved_project_mode"] = bool(in_place_saved_project)

        uv_name = target.data.uv_layers.active.name if target.data.uv_layers and target.data.uv_layers.active else (target.data.uv_layers[0].name if target.data.uv_layers else "NONE")
        mode = "SAVED JOINED PROJECT" if in_place_saved_project else "FULL TARGET JOIN"
        self.report({"INFO"}, f"Prepared OUR Spider {profile['target']} [{mode}]: profile section {profile['section']} | ref 0x{int(atlas_ref):08X} | UV {uv_name} | 32 bones | divisor 512")
        return {"FINISHED"}

class SM3_OT_RebuildAtlasAndPrepareSavedProject(Operator):
    bl_idname = "sm3.rebuild_atlas_prepare_saved_project"
    bl_label = "REDO ATLAS + PREP SAVED PROJECT"
    bl_description = (
        "For an export-ready joined Spider-Man project: restore WoS/RVB source materials/textures, "
        "rebuild the baked atlas/TEX, then stamp the active joined mesh for one-section 0x0614 export "
        "using the original target schema cached in the .blend"
    )
    bl_options = {"REGISTER", "UNDO"}

    directory: StringProperty(name="Atlas Output Folder", subtype="DIR_PATH")

    def invoke(self, context, event):
        if context.active_object is None or context.active_object.type != "MESH":
            self.report({"ERROR"}, "Select the joined/export-ready Spider-Man mesh")
            return {"CANCELLED"}
        if not self.directory:
            if bpy.data.filepath:
                self.directory = os.path.join(os.path.dirname(bpy.data.filepath), "SM3_BAKED_ATLAS")
            else:
                self.directory = os.path.join(os.path.expanduser("~"), "SM3_BAKED_ATLAS")
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        obj = context.active_object
        scene = context.scene
        if obj is None or obj.type != "MESH":
            self.report({"ERROR"}, "Select the joined/export-ready Spider-Man mesh")
            return {"CANCELLED"}
        if not hasattr(scene, "sm3mat_source_texture_folder"):
            self.report({"ERROR"}, "Install/enable SM3 Material Combiner v1.4.4+ first")
            return {"CANCELLED"}
        if not str(scene.sm3mat_source_texture_folder or "").strip():
            self.report({"ERROR"}, "Set SM3 Materials > Source Texture Folder to the TEX1/TEX2 folder first")
            return {"CANCELLED"}
        try:
            scene.sm3mat_source_preset = "WOS_RVB_SPIDERMAN"
            scene.sm3mat_source_auto_reconstruct_wos_routing = True
            scene.sm3mat_friend_auto_setup_sources = True
            scene.sm3mat_friend_require_all_sources = True
            result = bpy.ops.sm3mat.create_friend_baked_atlas(directory=self.directory)
            if "FINISHED" not in result:
                self.report({"ERROR"}, "Atlas rebuild did not finish; see SM3_Material_Last_Error")
                return {"CANCELLED"}
            context.view_layer.objects.active = obj
            obj.select_set(True)
            result2 = bpy.ops.sm3.prepare_spider_atlas_section()
            if "FINISHED" not in result2:
                self.report({"ERROR"}, "Atlas built, but export prep failed; see the latest Blender report")
                return {"CANCELLED"}
            self.report({"INFO"}, "READY: atlas/TEX rebuilt and joined Spider mesh prepared for one-section 0x0614 export")
            return {"FINISHED"}
        except Exception as exc:
            _write_last_error("SM3 saved-project atlas rebuild + prep failed")
            self.report({"ERROR"}, f"{exc} | See Text Editor > SM3_Last_Error")
            return {"CANCELLED"}


# -----------------------------------------------------------------------------
# MESH UTILITIES
# -----------------------------------------------------------------------------

class SM3_OT_RecalcNormals(Operator):
    bl_idname = "sm3.recalc_normals"
    bl_label = "Recalculate Normals (Outside)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        objects = [o for o in context.selected_objects if o.type == "MESH"]
        if not objects:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}
        for obj in objects:
            bm = bmesh.new()
            bm.from_mesh(obj.data)
            bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
            bm.to_mesh(obj.data)
            bm.free()
            for poly in obj.data.polygons:
                poly.use_smooth = True
            obj.data.update()
        self.report({'INFO'}, f"Recalculated normals for {len(objects)} mesh(es)")
        return {'FINISHED'}


class SM3_OT_ShadeSmooth(Operator):
    bl_idname = "sm3.shade_smooth"
    bl_label = "Shade Smooth (Selected)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        objects = [o for o in context.selected_objects if o.type == "MESH"]
        if not objects:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}
        for obj in objects:
            for poly in obj.data.polygons:
                poly.use_smooth = True
            obj.data.update()
        self.report({'INFO'}, f"Shade smooth applied to {len(objects)} mesh(es)")
        return {'FINISHED'}


class SM3_OT_RemoveSmooth(Operator):
    bl_idname = "sm3.remove_smooth"
    bl_label = "Remove Smooth Shading (Selected)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        objects = [o for o in context.selected_objects if o.type == "MESH"]
        if not objects:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}
        for obj in objects:
            for poly in obj.data.polygons:
                poly.use_smooth = False
            obj.data.update()
        self.report({'INFO'}, f"Flat shading applied to {len(objects)} mesh(es)")
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# WOS 1:1 MATERIAL COMBINER WORKFLOW BRIDGE
# -----------------------------------------------------------------------------

WOS_SRC_MASTER_UV = "SM3_SRC_MASTER"
WOS_SRC_WORK_UV = "SM3_SRC"
WOS_ATLAS_UV = "SM3_ATLAS"

# v1.6.9: keep the full SM3/WoS-style material list on the SOURCE object, but
# build a separate one-material GAME OUTPUT after the atlas is packed.  This is
# the structural behavior proven by the completed WoS mod and by our old grey
# 0x0614 Spider test: the final game mesh does not keep SIDEWEB/TOPWEB/
# BACKSPIDER as separate render routes.
WOS_CLEAN_OUTPUT_SUFFIX = "__SM3_CLEAN_EXPORT"
WOS_COMBINER_WORK_SUFFIX = "__SM3_COMBINER_WORK"

def _combiner_work_name(source):
    return f"{source.name}{WOS_COMBINER_WORK_SUFFIX}"

def _remove_previous_combiner_work(source):
    wanted = source.name
    for obj in list(bpy.data.objects):
        if obj is source or obj.type != 'MESH':
            continue
        if not bool(obj.get('sm3_wos_combiner_work', False)):
            continue
        if str(obj.get('sm3_wos_combiner_source', '')) != wanted:
            continue
        mesh = getattr(obj, 'data', None)
        mats = [m for m in getattr(mesh, 'materials', []) if m is not None] if mesh is not None else []
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and getattr(mesh, 'users', 0) == 0:
            bpy.data.meshes.remove(mesh)
        for mat in mats:
            if getattr(mat, 'users', 0) == 0:
                bpy.data.materials.remove(mat)


def _combiner_work_for_source(source):
    """Return the protected combiner work copy for an editable source, if any."""
    if source is None or source.type != 'MESH':
        return None
    work_name = str(source.get('sm3_wos_combiner_work_name', '') or '').strip()
    if work_name:
        work = bpy.data.objects.get(work_name)
        if (
            work is not None
            and work.type == 'MESH'
            and bool(work.get('sm3_wos_combiner_work', False))
            and str(work.get('sm3_wos_combiner_source', '')) == source.name
        ):
            return work
    for obj in bpy.data.objects:
        if (
            obj.type == 'MESH'
            and bool(obj.get('sm3_wos_combiner_work', False))
            and str(obj.get('sm3_wos_combiner_source', '')) == source.name
        ):
            return obj
    return None


def _remove_clean_outputs_for_sources(source_names):
    wanted = {str(n) for n in source_names if n}
    for obj in list(bpy.data.objects):
        if obj.type != 'MESH' or not bool(obj.get('sm3_wos_clean_export', False)):
            continue
        if str(obj.get('sm3_wos_clean_source', '')) not in wanted:
            continue
        mesh = getattr(obj, 'data', None)
        mats = [m for m in getattr(mesh, 'materials', []) if m is not None] if mesh is not None else []
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and getattr(mesh, 'users', 0) == 0:
            bpy.data.meshes.remove(mesh)
        for mat in mats:
            if getattr(mat, 'users', 0) == 0:
                bpy.data.materials.remove(mat)


def _atlas_uv_delta(mesh, a_name, b_name):
    a = mesh.uv_layers.get(a_name) if mesh and hasattr(mesh, 'uv_layers') else None
    b = mesh.uv_layers.get(b_name) if mesh and hasattr(mesh, 'uv_layers') else None
    if a is None or b is None or len(a.data) != len(b.data):
        return None
    max_delta = 0.0
    for aa, bb in zip(a.data, b.data):
        du = abs(float(aa.uv[0]) - float(bb.uv[0]))
        dv = abs(float(aa.uv[1]) - float(bb.uv[1]))
        max_delta = max(max_delta, du, dv)
    return max_delta

def _editable_source_from_object(obj):
    if obj is None or obj.type != 'MESH':
        return None
    if bool(obj.get('sm3_wos_clean_export', False)):
        obj = bpy.data.objects.get(str(obj.get('sm3_wos_clean_source', ''))) or obj
    if bool(obj.get('sm3_wos_combiner_work', False)):
        return bpy.data.objects.get(str(obj.get('sm3_wos_combiner_source', '')))
    return obj

WOS_SAFE_000_REF = 0x00000614
WOS_SAFE_001_REF = 0x00000560
WOS_SAFE_000_REAL_HASH = 0xE52A3DF4

# Canonical Spider 000 SOURCE material roles for the WoS-style workflow.
# These labels describe what the user should expect to edit BEFORE atlas combine.
# The final clean game export may still collapse to the proven single-atlas game route.
SM3_CANONICAL_SPIDER_SOURCE_SLOTS = (
    (0xAC933008, "ch_spidermanred",        "PRIMARY BODY APPEARANCE",        "Changes most of the suit/body"),
    (0x3EF08B55, "ch_spidermanblue",       "SECONDARY BODY APPEARANCE",      "Covers the remaining body regions"),
    (0x1E7B964E, "ch_spidermanwhite",      "EYES",                           "Eye material region"),
    (0xE52A3DF4, "ch_spidermanspider",     "FRONT SPIDER + EYELIDS",          "Front emblem and eyelid detail"),
    (0xE771757E, "ch_spidermantopweb",     "WEBS / WEB COLOR",                "Suit web / web-color region"),
    (0x654DD425, "ch_spidermanbackspider", "BACK SPIDER",                     "Back emblem region"),
)

SM3_KNOWN_MATERIAL_ROLES = {
    h: role for h, _name, role, _note in SM3_CANONICAL_SPIDER_SOURCE_SLOTS
}
# Kept as a known stock material, but not part of the six canonical source slots above.
SM3_KNOWN_MATERIAL_ROLES[0x79C43D30] = "SIDEWEB (extra stock/legacy slot)"


def _material_hash_hint(mat):
    if mat is None:
        return 0
    for value in (mat.get('sm3_real_mat_hash'), mat.get('sm3_mat_hash'), mat.name):
        if value in (None, ''):
            continue
        try:
            return _u32(value, 0)
        except Exception:
            pass
        m = re.search(r"0x([0-9a-fA-F]{8})", str(value))
        if m:
            try:
                return int(m.group(1), 16) & 0xFFFFFFFF
            except Exception:
                pass
    return 0


def _clean_output_name(source):
    return f"{source.name}{WOS_CLEAN_OUTPUT_SUFFIX}"


def _remove_previous_clean_output(source):
    wanted_source = source.name
    for obj in list(bpy.data.objects):
        if obj is source or obj.type != 'MESH':
            continue
        if not bool(obj.get('sm3_wos_clean_export', False)):
            continue
        if str(obj.get('sm3_wos_clean_source', '')) != wanted_source:
            continue
        mesh = getattr(obj, 'data', None)
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and getattr(mesh, 'users', 0) == 0:
            bpy.data.meshes.remove(mesh)


def _build_clean_preview_material(obj, image, uv_name, material_ref):
    # Unique material per clean output so rebuilding one test cannot mutate the
    # user's source materials or another model in the .blend.  Deliberately no
    # normal/spec/overlay nodes are created here.
    mat_name = f"SM3_CLEAN_ATLAS_0x{int(material_ref) & 0xFFFFFFFF:08X}_{obj.name}"
    mat = bpy.data.materials.new(mat_name)
    mat.use_nodes = True
    tree = mat.node_tree
    tree.nodes.clear()
    out = tree.nodes.new('ShaderNodeOutputMaterial')
    bsdf = tree.nodes.new('ShaderNodeBsdfPrincipled')
    tex = tree.nodes.new('ShaderNodeTexImage')
    uv = tree.nodes.new('ShaderNodeUVMap')
    out.location = (480, 0)
    bsdf.location = (180, 0)
    tex.location = (-180, 20)
    uv.location = (-430, 20)
    uv.uv_map = str(uv_name)
    tex.image = image
    try:
        tex.interpolation = 'Linear'
        tex.extension = 'REPEAT'
    except Exception:
        pass
    tree.links.new(uv.outputs['UV'], tex.inputs['Vector'])
    tree.links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
    tree.links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    # Keep the Blender preview neutral.  These values do not rewrite the game's
    # MAT; the dedicated exporter still routes the mesh through 0x0614/0x0560.
    if bsdf.inputs.get('Metallic') is not None:
        bsdf.inputs['Metallic'].default_value = 0.0
    if bsdf.inputs.get('Roughness') is not None:
        bsdf.inputs['Roughness'].default_value = 0.5

    obj.data.materials.clear()
    obj.data.materials.append(mat)
    for poly in obj.data.polygons:
        poly.material_index = 0
    mat['sm3_clean_game_material'] = True
    mat['sm3_serialized_material_ref'] = int(material_ref) & 0xFFFFFFFF
    if int(material_ref) == WOS_SAFE_000_REF:
        mat['sm3_real_mat_hash'] = f"0x{WOS_SAFE_000_REAL_HASH:08X}"
        mat['sm3_real_mat_name'] = 'ch_spidermanspider'
    return mat


def _set_export_color_attribute(obj, mode="BLACK"):
    mesh = getattr(obj, "data", None)
    if mesh is None or not hasattr(mesh, "color_attributes"):
        return
    attr = mesh.color_attributes.get("Col_0")
    if attr is None:
        attr = mesh.color_attributes.new(name="Col_0", type="BYTE_COLOR", domain="CORNER")
    value = 1.0 if str(mode).upper() == "WHITE" else 0.0
    rgba = (value, value, value, 1.0)
    for item in attr.data:
        if hasattr(item, "color_srgb"):
            item.color_srgb = rgba
        else:
            item.color = rgba


def _spider_shading_vertex_mode(profile):
    """Map the controlled Spider game-shading test to the serialized vertex color.

    The 2026-09-22 in-game test proved geometry/weights were correct while the
    exported clean mesh still carried RGBA 255,255,255,255 on every corner.
    Keep the material/atlas route frozen and isolate that remaining shading
    variable with a black-vs-white vertex-color test.
    """
    return "WHITE" if str(profile).upper() == "LEGACY_WHITE" else "BLACK"


def _apply_spider_shading_profile(obj, profile):
    mode = _spider_shading_vertex_mode(profile)
    _set_export_color_attribute(obj, mode)
    obj['sm3_spider_shading_profile'] = str(profile)
    obj['sm3_spider_serialized_vertex_color'] = mode
    if getattr(obj, 'data', None) is not None:
        obj.data['sm3_spider_shading_profile'] = str(profile)
        obj.data['sm3_spider_serialized_vertex_color'] = mode
    return mode


def _copy_uv(mesh, src, dst):
    if mesh is None or not hasattr(mesh, "uv_layers") or not mesh.uv_layers:
        raise RuntimeError("Mesh has no UV map")
    src_layer = mesh.uv_layers.get(src) if src else mesh.uv_layers.active
    if src_layer is None:
        raise RuntimeError(f"UV layer not found: {src}")
    dst_layer = mesh.uv_layers.get(dst)
    if dst_layer is None:
        dst_layer = mesh.uv_layers.new(name=dst)
    if len(dst_layer.data) != len(src_layer.data):
        raise RuntimeError("UV loop count changed; refusing unsafe copy")
    for i, loop in enumerate(src_layer.data):
        dst_layer.data[i].uv = loop.uv[:]
    return dst_layer


def _activate_uv(mesh, name):
    layer = mesh.uv_layers.get(name)
    if layer is None:
        return False
    mesh.uv_layers.active = layer
    try:
        layer.active_render = True
    except Exception:
        pass
    return True


def _workflow_meshes(context):
    # Once a protected Material Combiner work copy is active, all atlas/UV
    # operations must target that copy only.  The editable source stays hidden
    # and untouched with all of its original material slots.
    active = getattr(context, "active_object", None)
    if active is not None and active.type == 'MESH':
        if bool(active.get('sm3_wos_combiner_work', False)):
            return [active]
        if bool(active.get('sm3_wos_clean_export', False)):
            return [active]

        # v1.7.8: after AUTO RESTORE SOURCE FACE ROUTING succeeds, that proven
        # joined/rigged target is the source workflow object.  Do NOT fall back
        # to scanning its whole collection, because the same collection may
        # intentionally contain the original WoS donor pieces and the hidden
        # __SM3_ROUTING_BACKUP__ snapshot.  The backup preserves the old
        # 7372/0/0/0 state and must never be validated as if it were the live
        # source model.
        if (
            not bool(active.get('sm3_wos_routing_backup', False))
            and bool(active.get('sm3_wos_material_routing_restored', False))
        ):
            return [active]

    col = context.scene.sm3_collection_search_dropdown
    if col is not None:
        meshes = [
            o for o in col.objects
            if o.type == 'MESH'
            and not bool(o.get('sm3_wos_combiner_work', False))
            and not bool(o.get('sm3_wos_clean_export', False))
            and not bool(o.get('sm3_wos_routing_backup', False))
        ]
        if meshes:
            return meshes
    return [
        o for o in context.selected_objects
        if o.type == 'MESH'
        and not bool(o.get('sm3_wos_combiner_work', False))
        and not bool(o.get('sm3_wos_clean_export', False))
        and not bool(o.get('sm3_wos_routing_backup', False))
    ]


def _armature_name(obj):
    arm = _armature_from_object(obj)
    return arm.name if arm else ""


def _image_texture_count(obj):
    found = set()
    for mat in obj.data.materials:
        if mat is None or not mat.use_nodes or mat.node_tree is None:
            continue
        for node in mat.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and getattr(node, 'image', None) is not None:
                found.add(node.image.name)
    return len(found)


class SM3_OT_WoS_CaptureSource(Operator):
    bl_idname = "sm3.wos_capture_source"
    bl_label = "1) CAPTURE SOURCE UV + RIG"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        meshes = _workflow_meshes(context)
        if not meshes:
            self.report({'ERROR'}, "Select/import an SM3 mesh or choose its collection")
            return {'CANCELLED'}
        captured = 0
        for obj in meshes:
            mesh = obj.data
            if not mesh.uv_layers or mesh.uv_layers.active is None:
                self.report({'ERROR'}, f"{obj.name}: no active UV map")
                return {'CANCELLED'}
            active_name = mesh.uv_layers.active.name
            # MASTER is immutable once captured. Working source may be refreshed.
            if mesh.uv_layers.get(WOS_SRC_MASTER_UV) is None:
                _copy_uv(mesh, active_name, WOS_SRC_MASTER_UV)
            _copy_uv(mesh, WOS_SRC_MASTER_UV, WOS_SRC_WORK_UV)
            _activate_uv(mesh, WOS_SRC_WORK_UV)
            obj['sm3_wos_vertex_count'] = len(mesh.vertices)
            obj['sm3_wos_loop_count'] = len(mesh.loops)
            obj['sm3_wos_poly_count'] = len(mesh.polygons)
            obj['sm3_wos_vgroup_count'] = len(obj.vertex_groups)
            obj['sm3_wos_armature'] = _armature_name(obj)
            obj['sm3_wos_source_uv'] = active_name
            # Preserve the exact pre-combine face -> material routing too.  The
            # Batman/WoS tutorial depends on this routing existing BEFORE the
            # atlas is generated; Material Combiner does not invent it.
            routing = [int(poly.material_index) for poly in mesh.polygons]
            obj['sm3_wos_source_material_indices'] = json.dumps(routing)
            obj['sm3_wos_source_material_names'] = json.dumps([
                (slot.material.name if slot.material is not None else '')
                for slot in obj.material_slots
            ])
            obj['sm3_wos_source_material_counts'] = json.dumps(_routing_face_counts(obj), sort_keys=True)
            obj['sm3_wos_source_captured'] = True
            captured += 1
        self.report({'INFO'}, f"Captured source UV + rig state for {captured} mesh(es)")
        return {'FINISHED'}


class SM3_OT_WoS_RestoreSource(Operator):
    bl_idname = "sm3.wos_restore_source"
    bl_label = "RESTORE SOURCE UV"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        meshes = _workflow_meshes(context)
        restored = 0
        for obj in meshes:
            if obj.data.uv_layers.get(WOS_SRC_MASTER_UV) is None:
                continue
            _copy_uv(obj.data, WOS_SRC_MASTER_UV, WOS_SRC_WORK_UV)
            _activate_uv(obj.data, WOS_SRC_WORK_UV)
            restored += 1
        if not restored:
            self.report({'ERROR'}, "No SM3_SRC_MASTER found. Run Capture Source first.")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Restored immutable source UV on {restored} mesh(es)")
        return {'FINISHED'}


class SM3_OT_WoS_ValidateCombiner(Operator):
    bl_idname = "sm3.wos_validate_combiner"
    bl_label = "2) VALIDATE MATERIAL COMBINER READY"

    def execute(self, context):
        meshes = _workflow_meshes(context)
        if not meshes:
            self.report({'ERROR'}, "No mesh found")
            return {'CANCELLED'}
        problems = []
        total_images = 0
        for obj in meshes:
            if obj.data.uv_layers.get(WOS_SRC_MASTER_UV) is None:
                problems.append(f"{obj.name}: source UV not captured")
            if not obj.data.materials:
                problems.append(f"{obj.name}: no Blender materials")
            used_slots = {int(poly.material_index) for poly in obj.data.polygons}
            if len(obj.material_slots) > 1 and len(used_slots) == 1 and len(obj.data.polygons) > 0:
                only = next(iter(used_slots))
                problems.append(
                    f"{obj.name}: MATERIAL FACE ROUTING COLLAPSED — all {len(obj.data.polygons)} faces use slot {only}. "
                    "Restore original WoS/source routing before Material Combiner."
                )
            total_images += _image_texture_count(obj)
            if not _armature_name(obj) and len(obj.vertex_groups):
                problems.append(f"{obj.name}: weighted mesh has no Armature modifier/parent")
        if problems:
            self.report({'ERROR'}, problems[0] + (f" (+{len(problems)-1} more)" if len(problems)>1 else ""))
            return {'CANCELLED'}
        self.report({'INFO'}, f"READY for original Material Combiner | {len(meshes)} mesh(es), {total_images} image texture(s)")
        return {'FINISHED'}


class SM3_OT_WoS_ResetAtlasStage(Operator):
    bl_idname = "sm3.wos_reset_atlas_stage"
    bl_label = "RESET ATLAS STAGE (KEEP RIG / ROUTING)"
    bl_description = (
        "Discard only stale generated atlas/work/clean-output state, restore SM3_SRC from "
        "SM3_SRC_MASTER, and return to the editable four-material source. Rig, weights, "
        "face routing, source images, skeleton, and topology are preserved."
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        active = _wos_active_mesh(context)
        source = _editable_source_from_object(active)
        if source is None or source.type != 'MESH':
            self.report({'ERROR'}, "Select the editable WoS/custom Spider mesh, work copy, or clean output")
            return {'CANCELLED'}
        master = source.data.uv_layers.get(WOS_SRC_MASTER_UV)
        if master is None:
            self.report({'ERROR'}, "SM3_SRC_MASTER is missing. Use the known-good pre-atlas save or CAPTURE SOURCE UV + RIG first.")
            return {'CANCELLED'}

        work = _combiner_work_for_source(source)
        names = {source.name}
        if work is not None:
            names.add(work.name)
        _remove_clean_outputs_for_sources(names)

        # Restore visibility BEFORE deleting the old isolated work copy.
        _restore_combiner_mesh_visibility(context)
        _remove_previous_combiner_work(source)
        source.pop('sm3_wos_combiner_work_name', None)

        mesh = source.data
        atlas = mesh.uv_layers.get(WOS_ATLAS_UV)
        if atlas is not None:
            mesh.uv_layers.remove(atlas)
        _copy_uv(mesh, WOS_SRC_MASTER_UV, WOS_SRC_WORK_UV)
        _activate_uv(mesh, WOS_SRC_WORK_UV)
        for key in ('sm3_wos_atlas_captured', 'sm3_wos_atlas_from', 'sm3_wos_captured_atlas_image'):
            source.pop(key, None)

        for key in ('sm3_smc_scope_object', SM3_COMBINER_VIS_SNAPSHOT):
            try:
                del context.scene[key]
            except Exception:
                pass
        try:
            context.scene.sm3_wos_selected_image = None
        except Exception:
            pass

        try:
            source.hide_set(False)
        except Exception:
            pass
        bpy.ops.object.select_all(action='DESELECT')
        source.select_set(True)
        context.view_layer.objects.active = source
        self.report({'INFO'}, "ATLAS STAGE RESET: rig/weights/routing kept | SM3_SRC restored | old work/clean output removed")
        return {'FINISHED'}


class SM3_OT_WoS_CaptureAtlas(Operator):
    bl_idname = "sm3.wos_capture_atlas"
    bl_label = "3) CAPTURE COMBINED ATLAS UV"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        active = _wos_active_mesh(context)
        source = _editable_source_from_object(active)
        if source is None or source.type != 'MESH':
            self.report({'ERROR'}, "Select the Spider source/work copy")
            return {'CANCELLED'}
        work = active if (active is not None and bool(active.get('sm3_wos_combiner_work', False))) else _combiner_work_for_source(source)
        if work is None or work.type != 'MESH':
            self.report({'ERROR'}, "No protected combiner work copy found. Press PREPARE COMBINER WORK COPY, then Generate Texture Atlas.")
            return {'CANCELLED'}
        if len(work.material_slots) != 1:
            self.report({'ERROR'}, f"{work.name}: atlas has not been combined yet ({len(work.material_slots)} materials remain)")
            return {'CANCELLED'}

        mesh = work.data
        if mesh.uv_layers.get(WOS_SRC_MASTER_UV) is None:
            self.report({'ERROR'}, f"{work.name}: source master UV missing")
            return {'CANCELLED'}
        # Material Combiner edits the active SM3_SRC layer on the protected work
        # copy. Never capture from the currently active layer because an older
        # save may leave stale SM3_ATLAS active and silently copy it onto itself.
        if mesh.uv_layers.get(WOS_SRC_WORK_UV) is None:
            self.report({'ERROR'}, f"{work.name}: {WOS_SRC_WORK_UV} missing")
            return {'CANCELLED'}
        delta = _atlas_uv_delta(mesh, WOS_SRC_WORK_UV, WOS_SRC_MASTER_UV)
        if delta is not None and delta <= 1.0e-7:
            self.report({'ERROR'}, "SM3_SRC still matches the pre-combine UV. Run Generate Texture Atlas on the protected work copy first.")
            return {'CANCELLED'}

        _copy_uv(mesh, WOS_SRC_WORK_UV, WOS_ATLAS_UV)
        _activate_uv(mesh, WOS_ATLAS_UV)
        work['sm3_wos_atlas_captured'] = True
        work['sm3_wos_atlas_from'] = WOS_SRC_WORK_UV
        work['sm3_wos_atlas_uv_delta'] = float(delta or 0.0)

        atlas_image = _find_current_atlas_image(work)
        if atlas_image is None:
            self.report({'ERROR'}, "Combined UV was found, but the generated atlas image is not linked to the work-copy material")
            return {'CANCELLED'}
        work['sm3_wos_captured_atlas_image'] = atlas_image.name
        try:
            context.scene.sm3_wos_selected_image = atlas_image
        except Exception:
            pass

        restored = _restore_combiner_mesh_visibility(context)
        bpy.ops.object.select_all(action='DESELECT')
        try:
            work.hide_set(False)
        except Exception:
            pass
        work.select_set(True)
        context.view_layer.objects.active = work
        self.report({'INFO'}, f"Captured FRESH Material Combiner UV: {WOS_SRC_WORK_UV} -> {WOS_ATLAS_UV} | atlas {atlas_image.name} | restored visibility on {restored} mesh(es)")
        return {'FINISHED'}


class SM3_OT_WoS_ValidateExport(Operator):
    bl_idname = "sm3.wos_validate_export"
    bl_label = "4) VALIDATE WOS-STYLE EXPORT"

    def execute(self, context):
        meshes = _workflow_meshes(context)
        if not meshes:
            self.report({'ERROR'}, "No mesh found")
            return {'CANCELLED'}
        for obj in meshes:
            mesh = obj.data
            checks = {
                'vertex count': (len(mesh.vertices), int(obj.get('sm3_wos_vertex_count', -1))),
                'loop count': (len(mesh.loops), int(obj.get('sm3_wos_loop_count', -1))),
                'polygon count': (len(mesh.polygons), int(obj.get('sm3_wos_poly_count', -1))),
                'vertex groups': (len(obj.vertex_groups), int(obj.get('sm3_wos_vgroup_count', -1))),
            }
            for label, (now, old) in checks.items():
                if old >= 0 and now != old:
                    self.report({'ERROR'}, f"{obj.name}: {label} changed {old} -> {now}")
                    return {'CANCELLED'}
            old_arm = str(obj.get('sm3_wos_armature', ''))
            if old_arm and _armature_name(obj) != old_arm:
                self.report({'ERROR'}, f"{obj.name}: armature changed")
                return {'CANCELLED'}
            if mesh.uv_layers.get(WOS_ATLAS_UV) is None:
                self.report({'ERROR'}, f"{obj.name}: {WOS_ATLAS_UV} missing")
                return {'CANCELLED'}
            _activate_uv(mesh, WOS_ATLAS_UV)
            _set_export_color_attribute(obj, context.scene.sm3_vertex_color_mode)
        self.report({'INFO'}, f"PASS: atlas UV + rig preserved | vertex color {context.scene.sm3_vertex_color_mode}")
        return {'FINISHED'}


class SM3_OT_WoS_BuildCleanGameOutput(Operator):
    bl_idname = "sm3.wos_build_clean_game_output"
    bl_label = "5) BUILD CLEAN GAME OUTPUT"
    bl_description = (
        "Duplicate the current atlas-ready source mesh, preserve rig/weights/UVs, "
        "then collapse ONLY the duplicate to one safe Spider game material route. "
        "All source material slots remain untouched and available for WoS-style editing."
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        requested = _wos_active_mesh(context)
        if requested is None or requested.type != 'MESH':
            self.report({'ERROR'}, "Select the joined Spider-Man source/work mesh")
            return {'CANCELLED'}
        editable = _editable_source_from_object(requested)
        work = _combiner_work_for_source(editable) if editable is not None else None
        # v1.8.5: a fresh combined work copy is the authority for atlas UV/image.
        # Never build from an older source/clean object's stale SM3_ATLAS layer.
        if (
            work is not None
            and bool(work.get('sm3_wos_atlas_captured', False))
            and work.data.uv_layers.get(WOS_ATLAS_UV) is not None
        ):
            source = work
        else:
            source = requested
            if bool(source.get('sm3_wos_clean_export', False)):
                source_name = str(source.get('sm3_wos_clean_source', ''))
                original = bpy.data.objects.get(source_name) if source_name else None
                if original is not None:
                    source = original

        mesh = source.data
        atlas_layer = mesh.uv_layers.get(WOS_ATLAS_UV) if hasattr(mesh, 'uv_layers') else None
        if atlas_layer is None:
            self.report({'ERROR'}, f"{WOS_ATLAS_UV} is missing. Run CAPTURE COMBINED ATLAS UV first.")
            return {'CANCELLED'}

        # Find the original Spider target/profile from the source object's real
        # collection.  The full target cache remains the authority; the completed
        # WoS mod is reference evidence only and is never used as a template.
        col = None
        profile = None
        candidates = list(getattr(source, 'users_collection', ()) or ())
        selected_col = context.scene.sm3_collection_search_dropdown
        if selected_col is not None and selected_col not in candidates:
            candidates.append(selected_col)
        for candidate in candidates:
            p = _spider_atlas_target_profile(candidate)
            if p and not p.get('invalid_reference_output'):
                col, profile = candidate, p
                break
        if col is None or profile is None:
            self.report({'ERROR'}, "Could not resolve the cached Spider 000/001 target profile for this source mesh")
            return {'CANCELLED'}

        target = str(profile.get('target', '000'))
        if target not in ('000', '001'):
            self.report({'ERROR'}, "Clean game output currently supports Spider 000 / 001 only")
            return {'CANCELLED'}
        material_ref = WOS_SAFE_000_REF if target == '000' else WOS_SAFE_001_REF
        expected_section = 4 if target == '000' else int(profile.get('section', 3))
        source_section_count = int(profile.get('source_section_count', 10 if target == '000' else 0) or 0)

        atlas_image = None
        captured_name = str(source.get('sm3_wos_captured_atlas_image', '') or '').strip()
        if captured_name:
            atlas_image = bpy.data.images.get(captured_name)
        if atlas_image is None:
            atlas_image = _find_current_atlas_image(source)
        if atlas_image is None:
            atlas_image = getattr(context.scene, 'sm3_wos_selected_image', None)
        if atlas_image is None:
            mat = _wos_active_material(source)
            atlas_image = _wos_get_diffuse_image(mat) if mat is not None else None
        if atlas_image is None:
            self.report({'ERROR'}, "No atlas image found. Select the generated atlas in the image field, then run this again.")
            return {'CANCELLED'}

        # Refresh instead of stacking multiple clean outputs every time the user
        # tweaks the atlas.  Only generated outputs belonging to THIS source are
        # removed; the source mesh/materials are never modified.
        _remove_previous_clean_output(source)

        clean = source.copy()
        clean.data = source.data.copy()
        clean.name = _clean_output_name(source)
        clean.data.name = clean.name + "_Mesh"
        col.objects.link(clean)

        _activate_uv(clean.data, WOS_ATLAS_UV)
        _build_clean_preview_material(clean, atlas_image, WOS_ATLAS_UV, material_ref)
        shading_profile = getattr(context.scene, 'sm3_spider_shading_test_profile', 'NEUTRAL_BLACK')
        shading_vertex_mode = _apply_spider_shading_profile(clean, shading_profile)

        # Dedicated one-section exporter markers.  This is the actual fix for
        # stock SIDEWEB/TOPWEB/BACKSPIDER interference: the final output has one
        # game section/ref, while the SOURCE still retains all editable slots.
        clean['sm3_wos_clean_export'] = True
        clean['sm3_wos_clean_source'] = source.name
        clean['sm3_wos_clean_atlas_image'] = atlas_image.name
        clean['sm3_wos_clean_stock_overlays_removed'] = True
        clean['sm3_wos_clean_source_material_count'] = len(source.material_slots)
        clean['sm3_friend_atlas_uv_export'] = WOS_ATLAS_UV
        clean['sm3_friend_atlas_uv_source'] = str(source.get('sm3_wos_source_uv', WOS_SRC_WORK_UV))
        clean['sm3_spider_atlas_prepared'] = True
        clean['sm3_spider_atlas_section'] = int(expected_section)
        clean['sm3_spider_atlas_material_ref'] = int(material_ref)
        clean['sm3_spider_atlas_max_bones'] = 32
        clean['sm3_spider_atlas_build'] = '1.3.8'
        clean['sm3_spider_atlas_target'] = target
        clean['sm3_spider_atlas_source_section_count'] = int(source_section_count)
        clean['sm3_serialized_material_ref'] = int(material_ref)
        clean['sm3_position_divisor'] = float(profile.get('position_divisor', 512.0))
        clean.data['sm3_serialized_material_ref'] = int(material_ref)
        clean.data['sm3_position_divisor'] = float(profile.get('position_divisor', 512.0))
        clean.data['sm3_wos_clean_export'] = True
        clean.data['sm3_friend_atlas_uv_export'] = WOS_ATLAS_UV

        # Preserve exactly the source object's transform/modifiers/vertex groups.
        # obj.copy() already copies the object-level vertex groups and armature
        # modifier targets; data.copy() keeps vertex weights/loops/UVs independent.
        if len(clean.data.vertices) != len(source.data.vertices) or len(clean.vertex_groups) != len(source.vertex_groups):
            bpy.data.objects.remove(clean, do_unlink=True)
            self.report({'ERROR'}, "Clean-copy integrity check failed; source was left untouched")
            return {'CANCELLED'}

        try:
            source.hide_set(True)
        except Exception:
            pass
        bpy.ops.object.select_all(action='DESELECT')
        clean.hide_set(False)
        clean.select_set(True)
        context.view_layer.objects.active = clean
        context.scene.sm3_collection_search_dropdown = col

        role = "0x0614 / 0xE52A3DF4" if target == '000' else "0x0560"
        self.report({'INFO'}, f"CLEAN OUTPUT READY: 1 material | {role} | {WOS_ATLAS_UV} | shading {shading_vertex_mode} | source slots preserved")
        return {'FINISHED'}


class SM3_OT_WoS_ReturnToSource(Operator):
    bl_idname = "sm3.wos_return_to_source"
    bl_label = "SHOW EDITABLE SOURCE"
    bl_description = "Return to the untouched source mesh with all original/editable material slots"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        active = context.active_object
        source = None
        clean = None
        work = None
        if active is not None and bool(active.get('sm3_wos_clean_export', False)):
            clean = active
            parent = bpy.data.objects.get(str(active.get('sm3_wos_clean_source', '')))
            if parent is not None and bool(parent.get('sm3_wos_combiner_work', False)):
                work = parent
                source = bpy.data.objects.get(str(parent.get('sm3_wos_combiner_source', '')))
            else:
                source = parent
        elif active is not None and bool(active.get('sm3_wos_combiner_work', False)):
            work = active
            source = bpy.data.objects.get(str(active.get('sm3_wos_combiner_source', '')))
        elif active is not None:
            source = active if active.type == 'MESH' else None
            if source is not None:
                work_name = str(source.get('sm3_wos_combiner_work_name', ''))
                work = bpy.data.objects.get(work_name) if work_name else None
                for obj in bpy.data.objects:
                    if not bool(obj.get('sm3_wos_clean_export', False)):
                        continue
                    clean_source = str(obj.get('sm3_wos_clean_source', ''))
                    if clean_source in {source.name, work.name if work else ''}:
                        clean = obj
                        break
        if source is None:
            self.report({'ERROR'}, "Could not find the editable source mesh")
            return {'CANCELLED'}
        try:
            source.hide_set(False)
            if work is not None:
                work.hide_set(True)
            if clean is not None:
                clean.hide_set(True)
        except Exception:
            pass
        bpy.ops.object.select_all(action='DESELECT')
        source.select_set(True)
        context.view_layer.objects.active = source
        self.report({'INFO'}, f"Editable source restored: {source.name} | all material slots preserved")
        return {'FINISHED'}


class SM3_OT_WoS_ShowCleanOutput(Operator):
    bl_idname = "sm3.wos_show_clean_output"
    bl_label = "SHOW CLEAN OUTPUT"
    bl_description = "Switch back to the generated one-material game-output copy"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        source = _wos_active_mesh(context)
        if source is None:
            self.report({'ERROR'}, "Select the editable source mesh")
            return {'CANCELLED'}
        if bool(source.get('sm3_wos_clean_export', False)):
            clean = source
            source = bpy.data.objects.get(str(clean.get('sm3_wos_clean_source', '')))
        else:
            work = source if bool(source.get('sm3_wos_combiner_work', False)) else None
            editable = _editable_source_from_object(source)
            if work is None and editable is not None:
                work_name = str(editable.get('sm3_wos_combiner_work_name', ''))
                work = bpy.data.objects.get(work_name) if work_name else None
            names = {source.name}
            if work is not None:
                names.add(work.name)
            if editable is not None:
                names.add(editable.name)
            clean = next(
                (
                    o for o in bpy.data.objects
                    if bool(o.get('sm3_wos_clean_export', False))
                    and str(o.get('sm3_wos_clean_source', '')) in names
                ),
                None,
            )
        if clean is None:
            self.report({'ERROR'}, "No clean output exists yet; run BUILD CLEAN GAME OUTPUT")
            return {'CANCELLED'}
        try:
            if source is not None:
                source.hide_set(True)
            clean.hide_set(False)
        except Exception:
            pass
        bpy.ops.object.select_all(action='DESELECT')
        clean.select_set(True)
        context.view_layer.objects.active = clean
        self.report({'INFO'}, f"Clean output active: {clean.name}")
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# WOS-STYLE GENERIC MATERIAL -> IMAGE -> ORIGINAL MATERIAL COMBINER WORKFLOW
# -----------------------------------------------------------------------------

def _wos_active_mesh(context):
    obj = getattr(context, "object", None)
    if obj is not None and obj.type == 'MESH':
        return obj
    meshes = _workflow_meshes(context)
    return meshes[0] if meshes else None


def _wos_active_material(obj):
    if obj is None or obj.type != 'MESH' or not obj.material_slots:
        return None
    idx = min(max(int(obj.active_material_index), 0), len(obj.material_slots) - 1)
    slot = obj.material_slots[idx]
    return slot.material


def _wos_active_uv_name(obj):
    if obj is None or obj.type != 'MESH' or not obj.data.uv_layers:
        return ""
    layer = obj.data.uv_layers.active
    return layer.name if layer else obj.data.uv_layers[0].name



def _routing_poly_signature(obj, poly, target_world_inv, decimals=5):
    """Return an order-independent triangle/face geometry signature in target-local space.

    This is intentionally based on geometry, not polygon order, so source routing can
    be recovered from one or more original source objects even after they were joined
    into the clean rigged target. Object transforms are normalized through target space.
    """
    pts = []
    for vi in poly.vertices:
        world = obj.matrix_world @ obj.data.vertices[int(vi)].co
        local = target_world_inv @ world
        pts.append((round(float(local.x), decimals), round(float(local.y), decimals), round(float(local.z), decimals)))
    pts.sort()
    return tuple(pts)


def _routing_source_candidates(context, target):
    return [
        ob for ob in context.selected_objects
        if ob is not None
        and ob != target
        and ob.type == 'MESH'
        and not bool(ob.get('sm3_wos_combiner_work', False))
        and not bool(ob.get('sm3_wos_clean_export', False))
        and not bool(ob.get('sm3_wos_routing_backup', False))
    ]


def _routing_face_counts(obj):
    counts = {}
    for poly in obj.data.polygons:
        idx = int(poly.material_index)
        counts[idx] = counts.get(idx, 0) + 1
    return counts


def _routing_selected_meshes(context):
    """Selected user meshes that are eligible for source-routing recovery."""
    return [
        ob for ob in context.selected_objects
        if ob is not None
        and ob.type == 'MESH'
        and not bool(ob.get('sm3_wos_combiner_work', False))
        and not bool(ob.get('sm3_wos_clean_export', False))
        and not bool(ob.get('sm3_wos_routing_backup', False))
    ]


def _routing_rig_score(obj):
    """Tie-breaker only; geometry coverage remains the authority."""
    score = 0
    try:
        if obj.parent is not None and obj.parent.type == 'ARMATURE':
            score += 1000
    except Exception:
        pass
    try:
        score += 100 * sum(1 for m in obj.modifiers if m.type == 'ARMATURE')
    except Exception:
        pass
    try:
        score += min(len(obj.vertex_groups), 99)
    except Exception:
        pass
    return score


def _routing_try_match(target, sources):
    """Prove that sources cover every target face, without changing anything."""
    if target is None or target.type != 'MESH' or not target.data.polygons:
        return None, ["candidate has no faces"]
    if not sources:
        return None, ["candidate has no source meshes"]

    target_inv = target.matrix_world.inverted_safe()
    diagnostics = []
    for decimals in (6, 5, 4, 3):
        src_map = {}
        ambiguous = set()
        invalid_source_faces = 0
        for src in sources:
            for poly in src.data.polygons:
                mi = int(poly.material_index)
                if mi < 0 or mi >= len(src.data.materials):
                    invalid_source_faces += 1
                    continue
                mat = src.data.materials[mi]
                if mat is None:
                    invalid_source_faces += 1
                    continue
                sig = _routing_poly_signature(src, poly, target_inv, decimals)
                route_key = (src.name, mi)
                record = (route_key, src, mat, int(poly.index))
                prev = src_map.get(sig)
                if prev is None:
                    src_map[sig] = record
                elif prev[0] != route_key:
                    ambiguous.add(sig)

        matches = []
        missing = 0
        ambiguous_hits = 0
        for poly in target.data.polygons:
            sig = _routing_poly_signature(target, poly, target_inv, decimals)
            if sig in ambiguous:
                ambiguous_hits += 1
                continue
            rec = src_map.get(sig)
            if rec is None:
                missing += 1
                continue
            matches.append((int(poly.index), rec[0], rec[1], rec[2]))

        diagnostics.append(
            f"decimals={decimals}: matched={len(matches)}/{len(target.data.polygons)} "
            f"missing={missing} ambiguous={ambiguous_hits} invalid_source={invalid_source_faces}"
        )
        if len(matches) == len(target.data.polygons) and not ambiguous_hits:
            return (decimals, matches), diagnostics
    return None, diagnostics


def _routing_auto_detect(context):
    """Find the clean joined target from the selected meshes.

    Every selected mesh is tested as a possible target.  A valid target must be
    completely covered by the geometry of the other selected meshes.  The
    largest fully-covered mesh wins; rig evidence is used only as a tie-breaker.
    This removes the fragile 'active last' requirement from v1.7.4.
    """
    meshes = _routing_selected_meshes(context)
    if len(meshes) < 2:
        return None, [], None, ["Select the clean rigged mesh AND the original WoS source part(s)."]

    candidates = []
    candidate_notes = []
    for cand in meshes:
        sources = [o for o in meshes if o != cand]
        chosen, diagnostics = _routing_try_match(cand, sources)
        face_count = len(cand.data.polygons)
        rig_score = _routing_rig_score(cand)
        status = "FULL MATCH" if chosen is not None else "no full match"
        candidate_notes.append(
            f"candidate {cand.name}: faces={face_count} rig_score={rig_score} {status}; "
            + " | ".join(diagnostics)
        )
        if chosen is not None:
            candidates.append((face_count, rig_score, cand.name, cand, sources, chosen))

    if not candidates:
        return None, [], None, candidate_notes

    # The clean joined model is expected to contain the complete face set while
    # the original source is one or more smaller parts.  Geometry has already
    # proven the coverage; choose the largest proven target, then use rig data
    # only to resolve a same-size duplicate.
    candidates.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    best = candidates[0]
    same_rank = [c for c in candidates if c[0] == best[0] and c[1] == best[1]]
    if len(same_rank) > 1:
        return None, [], None, candidate_notes + [
            "AMBIGUOUS: more than one selected mesh has the same full-coverage face count and rig score. "
            "Deselect duplicate full-model copies/backups and retry."
        ]

    _faces, _rig, _name, target, sources, chosen = best
    return target, sources, chosen, candidate_notes


class SM3_OT_WoS_RestoreMaterialRoutingFromSource(Operator):
    bl_idname = "sm3.wos_restore_material_routing_from_source"
    bl_label = "Auto Restore WoS Source Face Routing"
    bl_description = (
        "Batman/WoS workflow: select the clean rigged model plus the ORIGINAL source mesh part(s) in ANY order. "
        "v1.7.7 keeps the auto-target routing/face inspection fixes and isolates source capture/validation to the proven restored target, ignoring hidden routing backups and donor parts. "
        "and restores the source per-face material routing without touching rig/weights/UVs."
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        selected = _routing_selected_meshes(context)
        if len(selected) < 2:
            self.report({'ERROR'}, "Select the clean rigged mesh AND the original WoS source part(s); order no longer matters")
            return {'CANCELLED'}

        try:
            target, sources, chosen, auto_notes = _routing_auto_detect(context)
            if target is None or chosen is None:
                txt = bpy.data.texts.get("SM3_WoS_Routing_Report") or bpy.data.texts.new("SM3_WoS_Routing_Report")
                txt.clear()
                txt.write(
                    "AUTO TARGET DETECTION / SOURCE ROUTING FAILED\n"
                    "=============================================\n\n"
                    "Selected meshes:\n"
                    + "\n".join(f"- {o.name}: faces={len(o.data.polygons)} rig_score={_routing_rig_score(o)}" for o in selected)
                    + "\n\nCandidate tests:\n"
                    + "\n".join(auto_notes)
                    + "\n\nNOTHING was changed. Select exactly the clean joined/rigged model and the original material-separated source part(s).\n"
                )
                self.report({'ERROR'}, "Could not uniquely auto-detect the clean target; NOTHING changed. See SM3_WoS_Routing_Report")
                return {'CANCELLED'}

            decimals, matches = chosen

            # Safety sanity check: the chosen full target should not be smaller
            # than every donor.  This protects against accidentally treating one
            # tiny source part as the destination.
            target_faces = len(target.data.polygons)
            max_source_faces = max((len(o.data.polygons) for o in sources), default=0)
            if target_faces < max_source_faces:
                self.report({'ERROR'}, "Safety stop: auto target is smaller than a selected source; NOTHING changed")
                return {'CANCELLED'}

            # Keep one hidden pre-routing snapshot so the operation remains
            # reversible even after saving/reopening the project.
            old_backup_name = str(target.get('sm3_wos_routing_backup_name', ''))
            old_backup = bpy.data.objects.get(old_backup_name) if old_backup_name else None
            if old_backup is not None:
                try:
                    bpy.data.objects.remove(old_backup, do_unlink=True)
                except Exception:
                    pass
            backup = target.copy()
            backup.data = target.data.copy()
            backup.name = f"__SM3_ROUTING_BACKUP__{target.name}"
            backup.data.name = backup.name + "_Mesh"
            col = target.users_collection[0] if target.users_collection else context.collection
            col.objects.link(backup)
            backup['sm3_wos_routing_backup'] = True
            backup['sm3_wos_routing_target'] = target.name
            try:
                backup.hide_set(True)
                backup.hide_render = True
            except Exception:
                pass
            target['sm3_wos_routing_backup_name'] = backup.name

            # Build source-material slots in first-face encounter order, preserving
            # each source object's distinct material slot exactly like the Batman
            # tutorial before Material Combiner is run.
            route_order = []
            route_info = {}
            for _poly_idx, key, src, mat in matches:
                if key not in route_info:
                    route_order.append(key)
                    route_info[key] = (src, mat)

            target.data.materials.clear()
            route_to_slot = {}
            used_names = set()
            for slot_idx, key in enumerate(route_order):
                src, mat = route_info[key]
                new_mat = mat.copy()
                base = f"WOS_SRC_{slot_idx:02d}_{mat.name}"
                name = base
                n = 1
                while name in used_names or (bpy.data.materials.get(name) is not None and bpy.data.materials.get(name) != new_mat):
                    n += 1
                    name = f"{base}_{n}"
                new_mat.name = name
                used_names.add(name)
                new_mat['sm3_wos_source_object'] = src.name
                new_mat['sm3_wos_source_material'] = mat.name
                new_mat['sm3_wos_source_material_index'] = int(key[1])
                target.data.materials.append(new_mat)
                route_to_slot[key] = slot_idx

            for poly_idx, key, _src, _mat in matches:
                target.data.polygons[poly_idx].material_index = int(route_to_slot[key])
            target.data.update()
            target.active_material_index = 0

            counts = _routing_face_counts(target)
            target['sm3_wos_material_routing_restored'] = True
            target['sm3_wos_material_routing_decimals'] = int(decimals)
            target['sm3_wos_material_routing_sources'] = json.dumps([o.name for o in sources])
            target['sm3_wos_material_routing_counts'] = json.dumps(counts, sort_keys=True)
            target['sm3_wos_auto_target_v175'] = True

            # Make the proven target the only selected/active mesh after PASS so
            # the panel immediately displays the correct full-model face counts.
            for ob in list(context.selected_objects):
                try:
                    ob.select_set(False)
                except Exception:
                    pass
            target.select_set(True)
            context.view_layer.objects.active = target

            txt = bpy.data.texts.get("SM3_WoS_Routing_Report") or bpy.data.texts.new("SM3_WoS_Routing_Report")
            txt.clear()
            txt.write(
                "SOURCE MATERIAL ROUTING RESTORED — AUTO TARGET / BATMAN-WOS METHOD\n"
                "==================================================================\n\n"
                f"AUTO-DETECTED TARGET: {target.name}\n"
                f"Target faces: {len(target.data.polygons)}\n"
                f"Matched faces: {len(matches)}/{len(target.data.polygons)}\n"
                f"Geometry signature precision: {decimals} decimals\n"
                f"Source objects: {', '.join(o.name for o in sources)}\n"
                f"Restored source material slots: {len(route_order)}\n\n"
                "Candidate detection:\n"
                + "\n".join(auto_notes)
                + "\n\nRestored regions:\n"
            )
            for idx in range(len(route_order)):
                mat = target.data.materials[idx]
                txt.write(f"slot {idx:02d}: {mat.name} | faces={counts.get(idx, 0)}\n")
            txt.write(
                "\nNEXT: verify the target now shows multiple non-zero source regions, assign/verify each source image, "
                "then CAPTURE SOURCE UV + RIG -> protected Material Combiner. Do NOT delete __SM3_ROUTING_BACKUP__.\n"
            )

            if len(counts) <= 1:
                self.report({'WARNING'}, f"Target auto-detected ({target_faces} faces), but source routing restored only {len(counts)} used region. Check the source materials before combining.")
            else:
                self.report({'INFO'}, f"PASS: auto target {target.name} ({target_faces} faces), restored {len(route_order)} source region(s)")
            return {'FINISHED'}
        except Exception:
            _write_last_error("AUTO RESTORE WOS SOURCE MATERIAL ROUTING FAILED")
            self.report({'ERROR'}, "Routing restore failed; NOTHING intentional should be kept. Undo if needed and see SM3_Last_Error")
            return {'CANCELLED'}


class SM3_OT_WoS_RestoreRoutingBackup(Operator):
    bl_idname = "sm3.wos_restore_routing_backup"
    bl_label = "Restore Pre-Routing Backup"
    bl_description = "Restore the hidden mesh snapshot created immediately before source material routing was transferred"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        target = _wos_active_mesh(context)
        if target is None:
            self.report({'ERROR'}, "Select the routing target mesh")
            return {'CANCELLED'}
        name = str(target.get('sm3_wos_routing_backup_name', ''))
        backup = bpy.data.objects.get(name) if name else None
        if backup is None or not bool(backup.get('sm3_wos_routing_backup', False)):
            self.report({'ERROR'}, "No pre-routing backup found for this mesh")
            return {'CANCELLED'}
        old_data = target.data
        target.data = backup.data.copy()
        try:
            if old_data.users == 0:
                bpy.data.meshes.remove(old_data)
        except Exception:
            pass
        target.active_material_index = 0
        target['sm3_wos_material_routing_restored'] = False
        self.report({'INFO'}, "Restored pre-routing mesh/material state")
        return {'FINISHED'}


def _wos_material_combiner_available():
    try:
        getattr(bpy.ops.smc, 'refresh_ob_data')
        getattr(bpy.ops.smc, 'combiner')
        return True
    except Exception:
        return False


class SM3_OT_WoS_AddMaterial(Operator):
    bl_idname = "sm3.wos_add_material"
    bl_label = "Add Material"
    bl_description = "Add a normal Blender material slot, matching the WoS workflow before atlas combining"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = _wos_active_mesh(context)
        if obj is None:
            self.report({'ERROR'}, "Select a mesh first")
            return {'CANCELLED'}
        mat = bpy.data.materials.new(name=f"Material_{len(obj.material_slots):02d}")
        mat.use_nodes = True
        obj.data.materials.append(mat)
        obj.active_material_index = len(obj.material_slots) - 1
        self.report({'INFO'}, f"Added material: {mat.name}")
        return {'FINISHED'}


class SM3_OT_WoS_EnsureCanonicalSpiderSlots(Operator):
    bl_idname = "sm3.wos_ensure_canonical_spider_slots"
    bl_label = "ADD / VERIFY 6 CANONICAL SPIDER SLOTS"
    bl_description = (
        "Safely append any missing canonical Spider 000 source materials. "
        "Existing slots and face assignments are never reordered or changed"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = _wos_active_mesh(context)
        if obj is None:
            self.report({'ERROR'}, "Select the editable Spider mesh first")
            return {'CANCELLED'}

        existing = {}
        for idx, slot in enumerate(obj.material_slots):
            mat = slot.material
            h = _material_hash_hint(mat)
            if h:
                existing.setdefault(int(h) & 0xFFFFFFFF, []).append(idx)

        added = []
        for h, real_name, role, note in SM3_CANONICAL_SPIDER_SOURCE_SLOTS:
            if h in existing:
                continue
            mat_name = f"0x{h:08X}"
            mat = bpy.data.materials.get(mat_name)
            if mat is None:
                mat = bpy.data.materials.new(name=mat_name)
                mat.use_nodes = True
            mat['sm3_real_mat_hash'] = mat_name
            mat['sm3_real_mat_name'] = real_name
            mat['sm3_wos_source_role'] = role
            mat['sm3_wos_source_note'] = note
            obj.data.materials.append(mat)
            added.append(mat_name)

        # Always refresh readable role metadata on matching materials.
        counts = []
        for h, real_name, role, note in SM3_CANONICAL_SPIDER_SOURCE_SLOTS:
            slot_indices = []
            faces = 0
            for idx, slot in enumerate(obj.material_slots):
                mat = slot.material
                if _material_hash_hint(mat) == h:
                    slot_indices.append(idx)
                    if mat is not None:
                        mat['sm3_real_mat_hash'] = f"0x{h:08X}"
                        mat['sm3_real_mat_name'] = real_name
                        mat['sm3_wos_source_role'] = role
                        mat['sm3_wos_source_note'] = note
                    faces += _wos_material_face_count(obj, idx)
            counts.append((h, role, slot_indices, faces))

        text = bpy.data.texts.get("SM3_WoS_Canonical_Slot_Check") or bpy.data.texts.new("SM3_WoS_Canonical_Slot_Check")
        text.clear()
        text.write("SM3 WoS CLONE — CANONICAL SPIDER 000 SOURCE SLOTS\n")
        text.write("=================================================\n\n")
        for h, role, slot_indices, faces in counts:
            slots = ", ".join(str(i) for i in slot_indices) if slot_indices else "MISSING"
            text.write(f"0x{h:08X}  {role}\n  slots: {slots} | faces: {faces}\n\n")
        if added:
            text.write("Appended missing materials (existing indices/faces were NOT changed):\n")
            for name in added:
                text.write(f"  {name}\n")

        obj['sm3_wos_canonical_slots_checked'] = True
        obj['sm3_wos_canonical_slot_count'] = 6
        if added:
            self.report({'INFO'}, f"Canonical slot check complete; appended {len(added)} missing slot(s). Existing routing preserved.")
        else:
            self.report({'INFO'}, "Canonical slot check PASS: all 6 Spider source slots already exist. Routing preserved.")
        return {'FINISHED'}


class SM3_OT_WoS_WriteSlotGuide(Operator):
    bl_idname = "sm3.wos_write_slot_guide"
    bl_label = "OPEN SLOT GUIDE IN TEXT EDITOR"
    bl_description = "Create a copy/paste material-slot guide inside the Blender file for the next modder"

    def execute(self, context):
        text = bpy.data.texts.get("SM3_WoS_Slot_Guide") or bpy.data.texts.new("SM3_WoS_Slot_Guide")
        text.clear()
        text.write("SM3 WoS WORKFLOW CLONE — SPIDER 000 SOURCE SLOT GUIDE\n")
        text.write("=====================================================\n\n")
        for h, real_name, role, note in SM3_CANONICAL_SPIDER_SOURCE_SLOTS:
            text.write(f"0x{h:08X} = {role}\n")
            text.write(f"  stock name: {real_name}\n")
            text.write(f"  {note}\n\n")
        text.write("RULES\n-----\n")
        text.write("1. Keep these source material regions through texture assignment and Material Combiner.\n")
        text.write("2. Use SHOW ONLY THIS MATERIAL'S FACES to confirm which faces each slot owns.\n")
        text.write("3. Material Combiner does not decide face routing; the routing must already be correct.\n")
        text.write("4. Assign the correct source image to every USED material before generating the atlas.\n")
        text.write("5. After combine, capture SM3_ATLAS, validate, build clean output, then weight-safe export.\n")
        self.report({'INFO'}, "Wrote SM3_WoS_Slot_Guide to Blender Text Editor")
        return {'FINISHED'}


class SM3_OT_WoS_LoadMaterialImage(Operator, ImportHelper):
    bl_idname = "sm3.wos_load_material_image"
    bl_label = "Load Image for Active Material"
    bl_description = "Choose any DDS/PNG/TGA/JPG or SM3 TEX/WRAP.TEX and connect it to the active material's Base Color"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(
        default="*.dds;*.png;*.tga;*.jpg;*.jpeg;*.bmp;*.tex;*.wrap.tex",
        options={'HIDDEN'},
    )

    def execute(self, context):
        obj = _wos_active_mesh(context)
        mat = _wos_active_material(obj)
        if obj is None or mat is None:
            self.report({'ERROR'}, "Select a mesh and one of its material slots first")
            return {'CANCELLED'}
        try:
            image, info = _wos_load_image_for_preview(self.filepath)
            _wos_assign_image_to_material(mat, image, _wos_active_uv_name(obj))
            context.scene.sm3_wos_selected_image = image
            obj['sm3_wos_last_material'] = mat.name
            obj['sm3_wos_last_image'] = image.name
            extra = ""
            if info.get('container') in {'WRAP_TEX', 'LOOSE_TEX'}:
                fourcc = info.get('fourcc', b'')
                if isinstance(fourcc, bytes):
                    fourcc = fourcc.decode('ascii', 'replace')
                extra = f" | 0x{int(info.get('tex_hash', 0)) & 0xFFFFFFFF:08X} {info.get('width')}x{info.get('height')} {fourcc} {info.get('mips')} mips"
            self.report({'INFO'}, f"{mat.name} <- {image.name}{extra}")
            return {'FINISHED'}
        except Exception:
            _write_last_error("WOS MATERIAL IMAGE LOAD FAILED")
            self.report({'ERROR'}, "Could not load/assign image; see SM3_Last_Error")
            return {'CANCELLED'}


class SM3_OT_WoS_AssignLoadedImage(Operator):
    bl_idname = "sm3.wos_assign_loaded_image"
    bl_label = "Use Selected Blender Image"
    bl_description = "Assign the image chosen below to the active material without changing UVs"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = _wos_active_mesh(context)
        mat = _wos_active_material(obj)
        image = context.scene.sm3_wos_selected_image
        if obj is None or mat is None:
            self.report({'ERROR'}, "Select a mesh and active material")
            return {'CANCELLED'}
        if image is None:
            self.report({'ERROR'}, "Choose a Blender image first")
            return {'CANCELLED'}
        try:
            _wos_assign_image_to_material(mat, image, _wos_active_uv_name(obj))
            obj['sm3_wos_last_material'] = mat.name
            obj['sm3_wos_last_image'] = image.name
            self.report({'INFO'}, f"{mat.name} <- {image.name}")
            return {'FINISHED'}
        except Exception:
            _write_last_error("WOS MATERIAL IMAGE ASSIGN FAILED")
            self.report({'ERROR'}, "Could not assign image; see SM3_Last_Error")
            return {'CANCELLED'}


class SM3_OT_WoS_SelectMaterialFaces(Operator):
    bl_idname = "sm3.wos_select_material_faces"
    bl_label = "Show Only This Material's Faces"
    bl_description = "Enter Edit/Face Select mode, clear the old selection, and show ONLY faces owned by the active material"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = _wos_active_mesh(context)
        if obj is None or not obj.material_slots:
            self.report({'ERROR'}, "Select a mesh with materials")
            return {'CANCELLED'}

        idx = int(obj.active_material_index)

        # The old inspector changed polygon.select in Object Mode.  In Blender
        # 4.5 that can leave vertex/edge selection from a previous Edit-Mode
        # selection visually active, making a 61-face region look like the
        # whole character is selected.  Inspect through BMesh instead: force
        # Face Select, clear verts/edges/faces, then select only this region.
        try:
            if context.view_layer.objects.active != obj:
                context.view_layer.objects.active = obj
            if not obj.select_get():
                obj.select_set(True)

            if obj.mode != 'EDIT':
                bpy.ops.object.mode_set(mode='EDIT')

            context.tool_settings.mesh_select_mode = (False, False, True)

            bm = bmesh.from_edit_mesh(obj.data)
            for v in bm.verts:
                v.select = False
            for e in bm.edges:
                e.select = False
            for f in bm.faces:
                f.select = False

            count = 0
            for f in bm.faces:
                if int(f.material_index) == idx:
                    f.select_set(True)
                    count += 1

            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
            self.report({'INFO'}, f"Showing ONLY {count} face(s) using material slot {idx}")
            return {'FINISHED'}
        except Exception:
            _write_last_error("WOS SELECT MATERIAL FACES FAILED")
            self.report({'ERROR'}, "Could not isolate material faces; see SM3_Last_Error")
            return {'CANCELLED'}


class SM3_OT_WoS_AssignActiveMaterialToFaces(Operator):
    bl_idname = "sm3.wos_assign_material_faces"
    bl_label = "Assign Active Material to Selected Faces"
    bl_description = "Assign the active material slot to the currently selected faces, like normal Blender/WoS material setup"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = _wos_active_mesh(context)
        if obj is None or not obj.material_slots:
            self.report({'ERROR'}, "Select a mesh with materials")
            return {'CANCELLED'}
        if obj.mode != 'EDIT':
            self.report({'ERROR'}, "Enter Edit Mode and select faces first")
            return {'CANCELLED'}
        try:
            bpy.ops.object.material_slot_assign()
            self.report({'INFO'}, f"Assigned material slot {obj.active_material_index} to selected faces")
            return {'FINISHED'}
        except Exception:
            _write_last_error("ASSIGN MATERIAL TO FACES FAILED")
            self.report({'ERROR'}, "Material assignment failed; see SM3_Last_Error")
            return {'CANCELLED'}


class SM3_OT_WoS_PrepareCombinerWorkCopy(Operator):
    bl_idname = "sm3.wos_prepare_combiner_work_copy"
    bl_label = "Prepare Protected Combiner Work Copy"
    bl_description = (
        "Duplicate the editable source model for Material Combiner. "
        "The ORIGINAL combiner is intentionally destructive and collapses selected "
        "materials to one atlas material, so it must run on this protected copy."
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        source = _editable_source_from_object(_wos_active_mesh(context))
        if source is None or source.type != 'MESH':
            self.report({'ERROR'}, "Select the editable WoS/custom mesh first")
            return {'CANCELLED'}
        if source.data.uv_layers.get(WOS_SRC_MASTER_UV) is None:
            self.report({'ERROR'}, "Run CAPTURE SOURCE UV + RIG before preparing the combiner copy")
            return {'CANCELLED'}
        if len(source.material_slots) < 2:
            self.report({'ERROR'}, "Source has fewer than 2 material slots; nothing to combine")
            return {'CANCELLED'}

        col = source.users_collection[0] if source.users_collection else context.collection
        _remove_previous_combiner_work(source)

        work = source.copy()
        work.data = source.data.copy()
        work.name = _combiner_work_name(source)
        work.data.name = work.name + "_Mesh"
        col.objects.link(work)

        # Deep-copy material datablocks too.  Material Combiner sets duplicate
        # bookkeeping on materials and then removes/replaces material slots.
        # None of that should ever touch the editable source material datablocks.
        for i, mat in enumerate(list(work.data.materials)):
            if mat is not None:
                work.data.materials[i] = mat.copy()

        work['sm3_wos_combiner_work'] = True
        work['sm3_wos_combiner_source'] = source.name
        source['sm3_wos_combiner_work_name'] = work.name

        # v1.7.8: hard-scope the compatible Material Combiner to this exact
        # protected work copy. The upstream combiner refreshes its list again
        # when Create Atlas is invoked, so merely disabling other rows after an
        # earlier refresh is not sufficient. The companion combiner build reads
        # this scene property on EVERY refresh.
        context.scene['sm3_smc_scope_object'] = work.name

        # Hide stale generated clean outputs belonging to either source or prior work.
        for obj in bpy.data.objects:
            if obj.type != 'MESH' or not bool(obj.get('sm3_wos_clean_export', False)):
                continue
            clean_source = str(obj.get('sm3_wos_clean_source', ''))
            if clean_source in {source.name, work.name}:
                try:
                    obj.hide_set(True)
                except Exception:
                    pass

        try:
            source.hide_set(True)
            work.hide_set(False)
        except Exception:
            pass

        bpy.ops.object.select_all(action='DESELECT')
        work.select_set(True)
        context.view_layer.objects.active = work

        # v1.8.0 legacy-proof isolation: physically hide every other mesh.
        # This makes even an old Material Combiner installation see ONE mesh.
        isolated = _hard_isolate_combiner_work_copy(context, work)

        # Keep toolkit collection pointed at the same character collection.
        try:
            context.scene.sm3_collection_search_dropdown = col
        except Exception:
            pass

        self.report(
            {'INFO'},
            f"Protected combiner copy ready: {work.name} | {isolated} other mesh(es) physically hidden | source preserved with {len(source.material_slots)} slots"
        )
        return {'FINISHED'}



SM3_COMBINER_VIS_SNAPSHOT = "sm3_combiner_visibility_snapshot_v180"

def _hard_isolate_combiner_work_copy(context, work):
    """Physically hide every other mesh from the current view layer.

    This is intentionally independent of Material Combiner version. Older
    Material Combiner builds scan context.visible_objects, so scene properties
    or disabled list rows are not sufficient if Blender is still loading an
    older installation. Hiding non-work meshes before every refresh/create
    guarantees that even the historical combiner can only discover the
    protected __SM3_COMBINER_WORK mesh.
    """
    if work is None or work.type != 'MESH':
        return 0
    scene = context.scene

    # Keep the first snapshot made for this work session. This lets us restore
    # the user's previous visibility after the atlas UV has been captured.
    snapshot = {}
    prior = str(scene.get(SM3_COMBINER_VIS_SNAPSHOT, "")).strip()
    if prior:
        try:
            snapshot = json.loads(prior)
        except Exception:
            snapshot = {}

    hidden = 0
    for obj in list(context.view_layer.objects):
        if obj.type != 'MESH':
            continue
        try:
            was_hidden = bool(obj.hide_get())
        except Exception:
            was_hidden = False
        if obj.name not in snapshot:
            snapshot[obj.name] = was_hidden
        if obj == work:
            try:
                obj.hide_set(False)
            except Exception:
                pass
            continue
        try:
            obj.hide_set(True)
            hidden += 1
        except Exception:
            pass

    scene[SM3_COMBINER_VIS_SNAPSHOT] = json.dumps(snapshot)
    scene['sm3_smc_scope_object'] = work.name

    bpy.ops.object.select_all(action='DESELECT')
    try:
        work.hide_set(False)
    except Exception:
        pass
    work.select_set(True)
    context.view_layer.objects.active = work
    return hidden


def _restore_combiner_mesh_visibility(context):
    """Restore mesh visibility saved by v1.8.0 hard isolation."""
    scene = context.scene
    raw = str(scene.get(SM3_COMBINER_VIS_SNAPSHOT, "")).strip()
    if not raw:
        return 0
    try:
        snapshot = json.loads(raw)
    except Exception:
        snapshot = {}
    restored = 0
    for name, was_hidden in snapshot.items():
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != 'MESH':
            continue
        try:
            obj.hide_set(bool(was_hidden))
            restored += 1
        except Exception:
            pass
    try:
        del scene[SM3_COMBINER_VIS_SNAPSHOT]
    except Exception:
        pass
    return restored


def _lock_material_combiner_to_work_copy(scene, work):
    """Force Material Combiner list entries outside the protected work copy off.

    Upstream Material Combiner intentionally scans *every visible mesh* in the
    current view layer.  That is useful generally, but dangerous in this SM3/WoS
    workflow because a visible donor/reference mesh can be pulled into the same
    atlas.  Preserve the user's include/exclude choices for materials on the work
    copy while disabling every other visible object/material entry.
    """
    kept = 0
    blocked = 0
    for item in getattr(scene, 'smc_ob_data', []):
        ob = getattr(item, 'ob', None)
        if ob is None:
            continue
        if ob == work:
            kept += 1
            # Object rows need to remain enabled. Material rows keep the user's
            # current checkbox state so deliberate exclusions still work.
            if getattr(item, 'mat', None) is None:
                try:
                    item.used = True
                except Exception:
                    pass
        else:
            try:
                item.used = False
                blocked += 1
            except Exception:
                pass
    return kept, blocked


class SM3_OT_WoS_SyncMaterialCombiner(Operator):
    bl_idname = "sm3.wos_sync_material_combiner"
    bl_label = "Refresh Original Material Combiner"
    bl_description = "Run the original Material Combiner's material-list refresh on visible mesh objects"

    def execute(self, context):
        if not _wos_material_combiner_available():
            self.report({'ERROR'}, "Original Material Combiner is not installed/enabled")
            return {'CANCELLED'}
        active = getattr(context, "active_object", None)
        if active is None or active.type != 'MESH' or not bool(active.get('sm3_wos_combiner_work', False)):
            self.report({'ERROR'}, "Prepare the protected COMBINER WORK COPY first")
            return {'CANCELLED'}
        try:
            context.scene['sm3_smc_scope_object'] = active.name
            hidden = _hard_isolate_combiner_work_copy(context, active)
            # Clear stale rows from older Material Combiner installs before
            # asking the combiner to rebuild from visible objects.
            try:
                context.scene.smc_ob_data.clear()
            except Exception:
                pass
            bpy.ops.smc.refresh_ob_data()
            kept, blocked = _lock_material_combiner_to_work_copy(context.scene, active)
            if kept <= 0:
                self.report({'ERROR'}, "Protected work copy did not appear in Material Combiner list")
                return {'CANCELLED'}
            self.report(
                {'INFO'},
                f"HARD ISOLATED {active.name}: {kept} work entries, {blocked} stale rows disabled, {hidden} other mesh(es) hidden"
            )
            return {'FINISHED'}
        except Exception:
            _write_last_error("MATERIAL COMBINER REFRESH FAILED")
            self.report({'ERROR'}, "Material Combiner refresh failed; see SM3_Last_Error")
            return {'CANCELLED'}


class SM3_OT_WoS_RunMaterialCombiner(Operator):
    bl_idname = "sm3.wos_run_material_combiner"
    bl_label = "Generate Texture Atlas (Original Combiner)"
    bl_description = "Invoke the ORIGINAL Material Combiner; it creates the atlas and repacks the current UVs"

    def execute(self, context):
        if not _wos_material_combiner_available():
            self.report({'ERROR'}, "Original Material Combiner is not installed/enabled")
            return {'CANCELLED'}
        active = getattr(context, 'active_object', None)
        if active is None or active.type != 'MESH' or not bool(active.get('sm3_wos_combiner_work', False)):
            self.report({'ERROR'}, "Prepare/select the protected COMBINER WORK COPY first")
            return {'CANCELLED'}
        try:
            # v1.7.8 hard scope: the companion Material Combiner reads this on
            # every refresh, including the refresh performed inside invoke().
            # This prevents donor meshes, routing backups, old clean exports,
            # and unrelated scene materials from re-entering the atlas list.
            context.scene['sm3_smc_scope_object'] = active.name
            _hard_isolate_combiner_work_copy(context, active)
            try:
                context.scene.smc_ob_data.clear()
            except Exception:
                pass
            bpy.ops.smc.refresh_ob_data()
            kept, blocked = _lock_material_combiner_to_work_copy(context.scene, active)
            if kept <= 0:
                self.report({'ERROR'}, "Protected work copy did not appear in Material Combiner list")
                return {'CANCELLED'}
            result = bpy.ops.smc.combiner('INVOKE_DEFAULT', cats=False)
            if 'CANCELLED' in result:
                return {'CANCELLED'}
            return {'FINISHED'}
        except Exception:
            _write_last_error("MATERIAL COMBINER START FAILED")
            self.report({'ERROR'}, "Could not start Material Combiner; see SM3_Last_Error")
            return {'CANCELLED'}


class SM3_OT_WoS_AutoSetupMaterialImages(Operator):
    bl_idname = "sm3.wos_validate_material_images"
    bl_label = "Validate Material Images"
    bl_description = "Check that every material actually used by visible workflow meshes has a diffuse image that the original Material Combiner can see"

    def execute(self, context):
        meshes = _workflow_meshes(context)
        if not meshes:
            self.report({'ERROR'}, "No workflow mesh found")
            return {'CANCELLED'}
        missing = []
        used = 0
        for obj in meshes:
            used_indices = {int(poly.material_index) for poly in obj.data.polygons}
            for idx in sorted(used_indices):
                if idx >= len(obj.data.materials):
                    missing.append(f"{obj.name}: slot {idx} missing")
                    continue
                mat = obj.data.materials[idx]
                if mat is None:
                    missing.append(f"{obj.name}: slot {idx} empty")
                    continue
                used += 1
                if _wos_get_diffuse_image(mat) is None:
                    missing.append(f"{obj.name}: {mat.name} has no Base Color image")
        if missing:
            text = bpy.data.texts.get("SM3_WoS_Material_Check") or bpy.data.texts.new("SM3_WoS_Material_Check")
            text.clear()
            text.write("MISSING MATERIAL IMAGES\n=======================\n\n" + "\n".join(missing))
            self.report({'ERROR'}, f"{len(missing)} material problem(s); see SM3_WoS_Material_Check")
            return {'CANCELLED'}
        self.report({'INFO'}, f"PASS: {used} used material slot(s) have image textures")
        return {'FINISHED'}

# -----------------------------------------------------------------------------
# N PANEL -- intentionally mirrors the lean WoS toolkit layout
# -----------------------------------------------------------------------------

class SM3_PT_Tools(Panel):
    bl_label = "SM3 WoS Clone Workflow"
    bl_idname = "SM3_PT_tools"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "SM3 Tools"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        quick = layout.box()
        quick.label(text="START HERE — WoS-STYLE PORT FLOW", icon="INFO")
        quick.label(text="1 Import mesh + skeleton   2 Rig/rename weights   3 Verify 6 source slots")
        quick.label(text="4 Assign source images   5 Material Combiner atlas   6 Capture atlas UV")
        quick.label(text="7 Build clean output   8 Weight-safe export")
        quick.label(text="Do not collapse source materials before Material Combiner.", icon="ERROR")

        # Import/export deliberately follows the small original WoS toolkit.
        box = layout.box()
        box.label(text="1. Import / Export (WoS-style)", icon="MESH_DATA")
        row = box.row(align=True)
        row.operator(IMPORT_OT_SM3_Mesh.bl_idname, text="Import Mesh / WRAP", icon="MESH_DATA")
        row.operator(IMPORT_OT_SM3_Skeleton.bl_idname, text="Import Skeleton", icon="ARMATURE_DATA")
        row = box.row(align=True)
        row.operator(EXPORT_OT_SM3_Mesh.bl_idname, text="Export Wrap Mesh", icon="EXPORT")
        row.operator(EXPORT_OT_SM3_Skeleton.bl_idname, text="Export Skeleton", icon="EXPORT")

        box = layout.box()
        box.label(text="WoS Import / Export Options")
        box.prop(scene, "sm3_flip_uv_v_axis")
        box.prop(scene, "sm3_reverse_winding")
        box.prop(scene, "sm3_convert_triangle_list")
        box.prop(scene, "sm3_max_bones", text="Max Bones / Section")
        box.prop(scene, "sm3_vertex_color_mode", text="Generic Vertex Color")

        box = layout.box()
        box.label(text="2. Rig / Weight Setup", icon="GROUP_VERTEX")
        box.prop_search(scene, "sm3_collection_search_dropdown", bpy.data, "collections", text="Collection")
        box.operator(SM3_OT_RenameVertexGroups.bl_idname, text="Rename Vertex Groups")

        # ------------------------------------------------------------------
        # This is the important v1.6.0 reset: normal Blender materials first.
        # The tool does not decide what image/hash the model is allowed to use.
        # ------------------------------------------------------------------
        box = layout.box()
        box.label(text="3. Source Material Slots + Images", icon="MATERIAL")
        obj = _wos_active_mesh(context)
        if obj is None:
            box.label(text="Select a mesh object.", icon="INFO")
        else:
            row = box.row()
            row.label(text=f"Object: {obj.name}")
            if obj.data.uv_layers:
                row.label(text=f"UV: {_wos_active_uv_name(obj)}")
            else:
                row.label(text="NO UV", icon="ERROR")

            if obj.material_slots:
                box.template_list("MATERIAL_UL_matslots", "sm3_wos_materials", obj, "material_slots", obj, "active_material_index", rows=5)
            else:
                box.label(text="No materials yet.", icon="INFO")

            guide = box.box()
            guide.label(text="SPIDER 000 — CANONICAL SOURCE SLOT MAP", icon="MATERIAL")
            guide.label(text="0xAC933008  PRIMARY BODY APPEARANCE")
            guide.label(text="0x3EF08B55  SECONDARY BODY APPEARANCE")
            guide.label(text="0x1E7B964E  EYES")
            guide.label(text="0xE52A3DF4  FRONT SPIDER + EYELIDS")
            guide.label(text="0xE771757E  WEBS / WEB COLOR")
            guide.label(text="0x654DD425  BACK SPIDER")
            row = guide.row(align=True)
            row.operator(SM3_OT_WoS_EnsureCanonicalSpiderSlots.bl_idname, text="ADD / VERIFY 6 SLOTS", icon="CHECKMARK")
            row.operator(SM3_OT_WoS_WriteSlotGuide.bl_idname, text="WRITE SLOT GUIDE", icon="TEXT")
            guide.label(text="Safe setup only APPENDS missing slots; it never reorders or changes face routing.", icon="LOCKED")

            routing = box.box()
            routing.label(text="WoS RULE: face routing exists BEFORE the atlas.", icon="UV")
            routing.label(text="v1.8.1: select CLEAN mesh + original source parts in ANY order.")
            routing.label(text="Target is auto-detected by full geometry coverage; no ACTIVE-last step.")
            routing.operator(
                SM3_OT_WoS_RestoreMaterialRoutingFromSource.bl_idname,
                text="AUTO RESTORE SOURCE FACE ROUTING",
                icon="MATERIAL",
            )
            if str(obj.get('sm3_wos_routing_backup_name', '')):
                routing.operator(
                    SM3_OT_WoS_RestoreRoutingBackup.bl_idname,
                    text="Restore Pre-Routing Backup",
                    icon="LOOP_BACK",
                )
            selected_routing_meshes = _routing_selected_meshes(context)
            if len(selected_routing_meshes) >= 2:
                largest = max(selected_routing_meshes, key=lambda o: len(o.data.polygons))
                routing.label(text=f"Selected meshes: {len(selected_routing_meshes)} | largest: {largest.name} ({len(largest.data.polygons)} faces)")
            counts = _routing_face_counts(obj) if obj.data.polygons else {}
            routing.label(text=f"Current object regions: {len(counts)} / slots: {len(obj.material_slots)}")
            if len(obj.material_slots) > 1 and len(counts) == 1 and obj.data.polygons:
                routing.label(text="STOP: all faces are in one slot — do not combine yet.", icon="ERROR")

            box.operator(SM3_OT_WoS_AddMaterial.bl_idname, icon="ADD")

            mat = _wos_active_material(obj)
            if mat is not None:
                matbox = box.box()
                pretty = str(mat.get('sm3_real_mat_name', '') or '')
                matbox.label(text=f"Active: {mat.name}" + (f"  ({pretty})" if pretty else ""))
                mat_hash = _material_hash_hint(mat)
                role = SM3_KNOWN_MATERIAL_ROLES.get(mat_hash, '')
                if role:
                    matbox.label(text=f"SM3 role: {role}", icon="INFO")
                faces = _wos_material_face_count(obj, int(obj.active_material_index))
                matbox.label(text=f"Faces using this material: {faces}")
                current_image = _wos_get_diffuse_image(mat)
                if current_image is not None:
                    dims = tuple(int(x) for x in current_image.size[:2]) if current_image.size else (0, 0)
                    matbox.label(text=f"Image: {current_image.name}  {dims[0]}x{dims[1]}", icon="IMAGE_DATA")
                else:
                    matbox.label(text="Image: NONE", icon="ERROR")

                matbox.operator(SM3_OT_WoS_LoadMaterialImage.bl_idname, text="Choose Image / DDS / SM3 TEX...", icon="FILEBROWSER")
                matbox.template_ID(scene, "sm3_wos_selected_image")
                matbox.operator(SM3_OT_WoS_AssignLoadedImage.bl_idname, text="Use Selected Blender Image", icon="IMAGE_DATA")
                row = matbox.row(align=True)
                row.operator(SM3_OT_WoS_SelectMaterialFaces.bl_idname, text="SHOW ONLY THIS MATERIAL'S FACES")
                row.operator(SM3_OT_WoS_AssignActiveMaterialToFaces.bl_idname, text="Assign to Selected")
                note = matbox.box()
                note.label(text="Any material + any image. No Spider texture hash is hard-coded.")
                note.label(text="Make the model look correct BEFORE creating the atlas.")

        box = layout.box()
        box.label(text="4. Lock Source UV + Rig", icon="UV")
        box.prop_search(scene, "sm3_collection_search_dropdown", bpy.data, "collections", text="SM3 Collection")
        box.operator(SM3_OT_WoS_CaptureSource.bl_idname, text="CAPTURE SOURCE UV + RIG", icon="UV")
        box.operator(SM3_OT_WoS_AutoSetupMaterialImages.bl_idname, text="VALIDATE MATERIAL IMAGES", icon="CHECKMARK")
        box.operator(SM3_OT_WoS_ValidateCombiner.bl_idname, text="VALIDATE COMBINER READY", icon="CHECKMARK")
        box.label(text="SM3_SRC_MASTER stays untouched; SM3_SRC is the working UV.", icon="LOCKED")

        box = layout.box()
        box.label(text="5. ORIGINAL Material Combiner - Protected Work Copy", icon="TEXTURE")
        box.operator(SM3_OT_WoS_ResetAtlasStage.bl_idname, text="RESET ATLAS STAGE (KEEP RIG / ROUTING)", icon="LOOP_BACK")
        box.label(text="Use RESET when reopening an old save or when clean output samples a stale atlas UV.", icon="INFO")
        if _wos_material_combiner_available():
            box.label(text="Original Material Combiner detected.", icon="CHECKMARK")
            box.operator(
                SM3_OT_WoS_PrepareCombinerWorkCopy.bl_idname,
                text="PREPARE COMBINER WORK COPY",
                icon="DUPLICATE",
            )
            active_combiner = context.active_object
            if active_combiner is not None and bool(active_combiner.get('sm3_wos_combiner_work', False)):
                box.label(text=f"WORK COPY: {active_combiner.name}", icon="CHECKMARK")
                box.label(text="One material AFTER combining is EXPECTED on this copy.", icon="INFO")
            row = box.row(align=True)
            row.operator(SM3_OT_WoS_SyncMaterialCombiner.bl_idname, text="Update Material List", icon="FILE_REFRESH")
            row.operator(SM3_OT_WoS_RunMaterialCombiner.bl_idname, text="Generate Texture Atlas", icon="UV")
            # Mirror the important original Material Combiner controls when its
            # properties are registered, while still letting its own panel be
            # the authority for the combine operation.
            for prop, label in (
                ("smc_size", "Atlas Size"),
                ("smc_packer_type", "Packing"),
                ("smc_gaps", "Spacing"),
            ):
                if hasattr(scene, prop):
                    box.prop(scene, prop, text=label)
            if hasattr(scene, "smc_crop"):
                box.prop(scene, "smc_crop", text="Enable Cropping")
            if hasattr(scene, "smc_pixel_art"):
                box.prop(scene, "smc_pixel_art", text="Pixel Art Mode")
            helpbox = box.box()
            helpbox.label(text="The ORIGINAL combiner makes the atlas AND repacks UVs.")
            helpbox.label(text="It intentionally collapses selected materials to ONE atlas material.")
            helpbox.label(text="v2.0.0 clone: canonical six-slot guide + protected combiner + UV1024 + weight-safe export.")
            helpbox.label(text="Choose materials in its MatCombiner tab if you need to exclude any.")
        else:
            box.label(text="Original Material Combiner NOT detected.", icon="ERROR")
            box.label(text="Install/enable material-combiner-addon-master.zip separately.")

        box = layout.box()
        box.label(text="6. Capture Atlas UV", icon="UV")
        box.operator(SM3_OT_WoS_CaptureAtlas.bl_idname, text="CAPTURE COMBINED ATLAS UV", icon="UV")
        box.operator(SM3_OT_WoS_ValidateExport.bl_idname, text="VALIDATE RIG + ATLAS UV", icon="CHECKMARK")
        box.operator(SM3_OT_WoS_RestoreSource.bl_idname, text="Restore Original Source UV", icon="LOOP_BACK")

        box = layout.box()
        box.label(text="7-8. Clean Game Output + Weight-Safe Export", icon="SHADERFX")
        box.label(text="SOURCE keeps every material slot. CLEAN OUTPUT is a duplicate.")
        box.label(text="After weight transfer: delete/hide the original SM3 donor BODY mesh; KEEP the armature.", icon="INFO")
        box.label(text="Final Spider 000 route: smart <=32-bone sections -> same 0x0614 / 0xE52A3DF4 atlas.")
        box.label(text="SIDEWEB / TOPWEB / BACKSPIDER stay edit-only on the SOURCE.")
        box.prop(scene, "sm3_spider_shading_test_profile", text="Game Shading Test")
        box.label(text="v2.0.0: source-slot workflow is explicit; clean export remains weight-safe and atlas-based.", icon="INFO")
        box.operator(SM3_OT_WoS_BuildCleanGameOutput.bl_idname, text="BUILD / REFRESH CLEAN GAME OUTPUT", icon="MODIFIER")
        row = box.row(align=True)
        row.operator(SM3_OT_WoS_ReturnToSource.bl_idname, text="Show Editable Source", icon="OUTLINER_OB_MESH")
        row.operator(SM3_OT_WoS_ShowCleanOutput.bl_idname, text="Show Clean Output", icon="CHECKMARK")
        active = context.active_object
        if active is not None and bool(active.get('sm3_wos_clean_export', False)):
            target = str(active.get('sm3_spider_atlas_target', '000'))
            atlas_name = str(active.get('sm3_wos_clean_atlas_image', ''))
            box.label(text=f"READY: {active.name}", icon="CHECKMARK")
            box.label(text=f"Atlas: {atlas_name}" if atlas_name else "Atlas linked")
            if target == '000':
                box.operator(EXPORT_OT_SM3_PreparedSpiderAtlas.bl_idname, text="EXPORT CLEAN SPIDER 000 MESH (WEIGHT SAFE)", icon="EXPORT")
            else:
                box.operator(EXPORT_OT_SM3_PreparedSpiderAtlas001.bl_idname, text="EXPORT CLEAN SPIDER 001 MESH (WEIGHT SAFE)", icon="EXPORT")
        else:
            box.label(text="Build the clean output, then export from that generated object.", icon="INFO")

        box = layout.box()
        box.label(text="Mesh Utilities", icon="MOD_NORMALEDIT")
        box.operator(SM3_OT_RecalcNormals.bl_idname, text="Recalc Normals (Selected)")
        box.operator(SM3_OT_ShadeSmooth.bl_idname, text="Shade Smooth (Selected)")
        box.operator(SM3_OT_RemoveSmooth.bl_idname, text="Remove Smooth Shading (Selected)")


# -----------------------------------------------------------------------------
# FILE MENUS
# -----------------------------------------------------------------------------

class IMPORT_MT_SM3(Menu):
    bl_label = "SM3 Blender Toolkit"
    bl_idname = "IMPORT_MT_sm3_blender_toolkit"

    def draw(self, context):
        self.layout.operator(IMPORT_OT_SM3_Mesh.bl_idname, text="Mesh (.mesh)")
        self.layout.operator(IMPORT_OT_SM3_Skeleton.bl_idname, text="Skeleton (.skel)")


class EXPORT_MT_SM3(Menu):
    bl_label = "SM3 Blender Toolkit"
    bl_idname = "EXPORT_MT_sm3_blender_toolkit"

    def draw(self, context):
        self.layout.operator(EXPORT_OT_SM3_Mesh.bl_idname, text="Mesh (.mesh)")
        self.layout.operator(EXPORT_OT_SM3_Skeleton.bl_idname, text="Skeleton (.skel)")


def menu_func_import(self, context):
    self.layout.menu(IMPORT_MT_SM3.bl_idname)


def menu_func_export(self, context):
    self.layout.menu(EXPORT_MT_SM3.bl_idname)


_base_classes = (
    SM3ExportCollectionItem,
    IMPORT_OT_SM3_Mesh,
    IMPORT_OT_SM3_Mesh_Drop,
    IMPORT_OT_SM3_Skeleton,
    IMPORT_OT_SM3_Skeleton_Drop,
    EXPORT_OT_SM3_Mesh,
    EXPORT_OT_SM3_Skeleton,
    SM3_OT_RenameVertexGroups,
    SM3_OT_RenameWeightsProximity,
    SM3_OT_RecalcNormals,
    SM3_OT_ShadeSmooth,
    SM3_OT_RemoveSmooth,
    SM3_OT_WoS_CaptureSource,
    SM3_OT_WoS_RestoreSource,
    SM3_OT_WoS_ValidateCombiner,
    SM3_OT_WoS_ResetAtlasStage,
    SM3_OT_WoS_CaptureAtlas,
    SM3_OT_WoS_ValidateExport,
    SM3_OT_WoS_BuildCleanGameOutput,
    SM3_OT_WoS_ReturnToSource,
    SM3_OT_WoS_ShowCleanOutput,
    EXPORT_OT_SM3_PreparedSpiderAtlas,
    EXPORT_OT_SM3_PreparedSpiderAtlas001,
    SM3_OT_WoS_AddMaterial,
    SM3_OT_WoS_EnsureCanonicalSpiderSlots,
    SM3_OT_WoS_WriteSlotGuide,
    SM3_OT_WoS_LoadMaterialImage,
    SM3_OT_WoS_AssignLoadedImage,
    SM3_OT_WoS_SelectMaterialFaces,
    SM3_OT_WoS_AssignActiveMaterialToFaces,
    SM3_OT_WoS_RestoreMaterialRoutingFromSource,
    SM3_OT_WoS_RestoreRoutingBackup,
    SM3_OT_WoS_PrepareCombinerWorkCopy,
    SM3_OT_WoS_SyncMaterialCombiner,
    SM3_OT_WoS_RunMaterialCombiner,
    SM3_OT_WoS_AutoSetupMaterialImages,
    SM3_PT_Tools,
    IMPORT_MT_SM3,
    EXPORT_MT_SM3,
)


if _HAS_FILEHANDLER:
    from bpy.types import FileHandler as _FileHandler

    class SM3_Mesh_FileHandler(_FileHandler):
        bl_idname = "SM3_MESH_FILEHANDLER"
        bl_label = "Import Mesh (SM3)"
        bl_import_operator = IMPORT_OT_SM3_Mesh_Drop.bl_idname
        bl_file_extensions = ".mesh"

        @classmethod
        def poll_drop(cls, context):
            return context.area and context.area.type == "VIEW_3D"

    class SM3_Skeleton_FileHandler(_FileHandler):
        bl_idname = "SM3_SKEL_FILEHANDLER"
        bl_label = "Import Skeleton (SM3)"
        bl_import_operator = IMPORT_OT_SM3_Skeleton_Drop.bl_idname
        bl_file_extensions = ".skel"

        @classmethod
        def poll_drop(cls, context):
            return context.area and context.area.type == "VIEW_3D"

    _filehandler_classes = (SM3_Mesh_FileHandler, SM3_Skeleton_FileHandler)
else:
    _filehandler_classes = ()

classes = _base_classes + _filehandler_classes


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)

    bpy.types.Scene.sm3_export_collections = CollectionProperty(type=SM3ExportCollectionItem)
    bpy.types.Scene.sm3_export_collections_index = IntProperty(default=0)
    bpy.types.Scene.sm3_collection_search_dropdown = PointerProperty(type=bpy.types.Collection, name="Collections")
    bpy.types.Scene.sm3_wos_selected_image = PointerProperty(
        type=bpy.types.Image,
        name="Texture Image",
        description="Any Blender image to assign to the active material before running the original Material Combiner",
    )
    bpy.types.Scene.sm3_flip_uv_v_axis = BoolProperty(name="Flip UV V-Axis", default=True)
    bpy.types.Scene.sm3_reverse_winding = BoolProperty(name="Reverse Triangle Winding Order", default=True)
    bpy.types.Scene.sm3_convert_triangle_list = BoolProperty(name="Convert to Triangle List", default=True)
    bpy.types.Scene.sm3_add_white_color = BoolProperty(
        name="Add White Color Attribute",
        description="Add a full-white face-corner color layer to meshes that do not already have one",
        default=True,
    )
    bpy.types.Scene.sm3_vertex_color_mode = EnumProperty(
        name="Vertex Color",
        description="General/export diagnostic: serialize all face-corner vertex colors as black or white",
        items=(("BLACK", "Black", "RGBA 0,0,0,255"), ("WHITE", "White", "RGBA 255,255,255,255")),
        default="BLACK",
    )
    bpy.types.Scene.sm3_spider_shading_test_profile = EnumProperty(
        name="Spider Game Shading Test",
        description="Dedicated Spider atlas test. Neutral Black is recommended after the bright/glow in-game result; Legacy White reproduces the prior test exactly.",
        items=(
            ("NEUTRAL_BLACK", "Neutral Black (Recommended)", "Force RGBA 0,0,0,255 on the clean Spider mesh; atlas/material/weights stay unchanged"),
            ("LEGACY_WHITE", "Legacy White (Previous Test)", "Force RGBA 255,255,255,255 to reproduce the washed-out/glow test"),
        ),
        default="NEUTRAL_BLACK",
    )
    bpy.types.Scene.sm3_max_bones = IntProperty(
        name="Max Bones / Section",
        description="General SM3 default is 48; the proven Spider atlas material uses a 32-bone palette",
        default=48, min=1, max=48,
    )
    bpy.types.Scene.sm3_rename_weights_source = PointerProperty(
        type=bpy.types.Object,
        name="Source",
        description="Source SM3 mesh with the correct vertex-group names",
    )
    bpy.types.Scene.sm3_rename_weights_target = PointerProperty(
        type=bpy.types.Object,
        name="Target",
        description="Target mesh whose vertex groups will be renamed",
    )


def unregister():
    try:
        bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    except Exception:
        pass
    try:
        bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    except Exception:
        pass

    for prop in (
        "sm3_export_collections", "sm3_export_collections_index",
        "sm3_collection_search_dropdown", "sm3_wos_selected_image", "sm3_flip_uv_v_axis",
        "sm3_reverse_winding", "sm3_convert_triangle_list",
        "sm3_add_white_color", "sm3_vertex_color_mode", "sm3_spider_shading_test_profile", "sm3_max_bones", "sm3_rename_weights_source",
        "sm3_rename_weights_target",
    ):
        if hasattr(bpy.types.Scene, prop):
            try:
                delattr(bpy.types.Scene, prop)
            except Exception:
                pass

    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


if __name__ == "__main__":
    register()
