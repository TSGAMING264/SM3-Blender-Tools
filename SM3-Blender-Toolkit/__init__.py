bl_info = {
    "name": "Spider-Man 3 Blender Toolkit",
    "author": "TSGAMING264",
    "version": (1, 1, 7),
    "blender": (4, 5, 0),
    "location": "File > Import/Export; 3D View > N Panel > SM3 Tools",
    "description": "WoS-direct SM3 mesh/skeleton import, index-based vertex-group rename, and direct object-section export",
    "category": "Import-Export",
}

import os
import re
import traceback
from pathlib import Path

import bpy
import bmesh
from mathutils import Vector
from bpy.types import Operator, PropertyGroup, Menu, Panel
from bpy.props import StringProperty, BoolProperty, CollectionProperty, IntProperty, PointerProperty
from bpy_extras.io_utils import ExportHelper

from .blender_io import import_mesh, import_skeleton, safe_export_skeleton, rename_vertex_groups_from_armature
from .sm3_export import export_objects_to_target_mesh

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
    filter_glob: StringProperty(default="*.mesh", options={'HIDDEN'})
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
                col, _objects, _arm, _mesh = import_mesh(
                    filepath,
                    auto_find_skeleton=False,
                    import_skeleton_if_found=False,
                    flip_uv_v=context.scene.sm3_flip_uv_v_axis,
                    reverse_winding=context.scene.sm3_reverse_winding,
                    convert_to_triangle_list=context.scene.sm3_convert_triangle_list,
                )
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
                col, _objects, _arm, _mesh = import_mesh(
                    os.path.join(self.directory, file.name),
                    auto_find_skeleton=False,
                    import_skeleton_if_found=False,
                    flip_uv_v=context.scene.sm3_flip_uv_v_axis,
                    reverse_winding=context.scene.sm3_reverse_winding,
                    convert_to_triangle_list=context.scene.sm3_convert_triangle_list,
                )
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
    filter_glob: StringProperty(default="*.skel", options={'HIDDEN'})
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
                target_col = _guess_collection_for_skeleton(filepath)
                arm, _skel = import_skeleton(filepath, collection=target_col)
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
                target_col = _guess_collection_for_skeleton(filepath)
                import_skeleton(filepath, collection=target_col)
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
        layout.prop(scene, "sm3_add_white_color")

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
                if context.scene.sm3_add_white_color:
                    for obj in mesh_objects:
                        _ensure_white_color_attribute(obj)
                output_path = os.path.join(base_dir, _source_basename(col))
                export_objects_to_target_mesh(
                    mesh_objects,
                    col,
                    output_path,
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
# N PANEL -- intentionally mirrors the lean WoS toolkit layout
# -----------------------------------------------------------------------------

class SM3_PT_Tools(Panel):
    bl_label = "SM3 Tools"
    bl_idname = "SM3_PT_tools"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "SM3 Tools"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        box = layout.box()
        box.label(text="Import / Export", icon="IMPORT")
        row = box.row(align=True)
        row.operator(IMPORT_OT_SM3_Mesh.bl_idname, text="Import Mesh", icon="MESH_DATA")
        row.operator(IMPORT_OT_SM3_Skeleton.bl_idname, text="Import Skeleton", icon="ARMATURE_DATA")
        row = box.row(align=True)
        row.operator(EXPORT_OT_SM3_Mesh.bl_idname, text="Export Mesh", icon="MESH_DATA")
        row.operator(EXPORT_OT_SM3_Skeleton.bl_idname, text="Export Skeleton", icon="ARMATURE_DATA")

        box = layout.box()
        box.label(text="Import Options", icon="IMPORT")
        box.prop(scene, "sm3_flip_uv_v_axis")
        box.prop(scene, "sm3_reverse_winding")
        box.prop(scene, "sm3_convert_triangle_list")

        box = layout.box()
        box.label(text="Export Options", icon="EXPORT")
        box.prop(scene, "sm3_flip_uv_v_axis")
        box.prop(scene, "sm3_reverse_winding")
        box.prop(scene, "sm3_add_white_color")

        box = layout.box()
        box.label(text="Rename Vertex Groups (by Bone Index)", icon="GROUP_VERTEX")
        box.prop_search(scene, "sm3_collection_search_dropdown", bpy.data, "collections", text="")
        box.operator(SM3_OT_RenameVertexGroups.bl_idname, text="Rename Vertex Groups")

        box = layout.box()
        box.label(text="Rename Weights by Proximity", icon="GROUP_VERTEX")
        row = box.row(align=True)
        row.prop(scene, "sm3_rename_weights_source", text="Source")
        row.prop(scene, "sm3_rename_weights_target", text="Target")
        box.operator(SM3_OT_RenameWeightsProximity.bl_idname, text="Rename Target Groups")

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
    bpy.types.Scene.sm3_flip_uv_v_axis = BoolProperty(name="Flip UV V-Axis", default=True)
    bpy.types.Scene.sm3_reverse_winding = BoolProperty(name="Reverse Triangle Winding Order", default=True)
    bpy.types.Scene.sm3_convert_triangle_list = BoolProperty(name="Convert to Triangle List", default=True)
    bpy.types.Scene.sm3_add_white_color = BoolProperty(
        name="Add White Color Attribute",
        description="Add a full-white face-corner color layer to meshes that do not already have one",
        default=True,
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
        "sm3_collection_search_dropdown", "sm3_flip_uv_v_axis",
        "sm3_reverse_winding", "sm3_convert_triangle_list",
        "sm3_add_white_color", "sm3_rename_weights_source",
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
