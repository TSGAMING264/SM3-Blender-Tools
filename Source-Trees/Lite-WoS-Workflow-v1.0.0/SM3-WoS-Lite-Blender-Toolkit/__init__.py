from __future__ import annotations

bl_info = {
    "name": "SM3 WoS Lite BlenderToolkit",
    "author": "TSGAMING264 workflow / OpenAI implementation; WoS workflow references credited in README",
    "version": (1, 0, 0),
    "blender": (4, 1, 0),
    "location": "3D View > N Panel > SM3 WoS Lite; File > Import/Export; Drag-and-Drop",
    "description": "Lean Spider-Man 3 model port workflow: import, weight transfer, armature deform, material routing, color tests, DDS->WRAP TEX, export",
    "category": "Import-Export",
}

import os
import tempfile
from pathlib import Path

import bpy
from bpy.types import Operator, Panel, FileHandler
from bpy.props import StringProperty, CollectionProperty, PointerProperty
from bpy_extras.io_utils import ImportHelper, ExportHelper

from .blender_io import import_mesh, import_skeleton, transfer_weights_from_sm3_collection
from .target_cache import recover_target_collection_from_objects
from .wrap_io import is_wrap_file, unwrap_file
from .spider_export import (
    CANONICAL_SPIDER_MATERIALS,
    STOCK_EXTRA_MATERIALS,
    export_spider_000_material_routed,
    material_hash,
)
from .sm3_texture_convert import dds_to_wrap_tex


# -----------------------------------------------------------------------------
# Small shared helpers
# -----------------------------------------------------------------------------


def _raw_name_from_wrap(path: str) -> str:
    name = Path(path).name
    return name.replace(".wrap.mesh", ".mesh").replace(".wrap.skel", ".skel")


def _import_mesh_path(filepath: str):
    kwargs = dict(
        auto_find_skeleton=True,
        import_skeleton_if_found=True,
        position_divisor_mode="AUTO",
        uv_divisor=1024.0,
        flip_uv_v=True,
        reverse_winding=True,
        convert_to_triangle_list=True,
        safe_viewer_mode=False,
    )
    if not is_wrap_file(filepath):
        return import_mesh(filepath, **kwargs)

    flat, info = unwrap_file(filepath)
    with tempfile.TemporaryDirectory(prefix="sm3_lite_wrap_mesh_") as td:
        rawpath = Path(td) / _raw_name_from_wrap(filepath)
        rawpath.write_bytes(flat)
        kwargs["auto_find_skeleton"] = False
        kwargs["import_skeleton_if_found"] = False
        col, objs, arm, mesh = import_mesh(str(rawpath), **kwargs)

    col["sm3_source_mesh"] = os.path.abspath(filepath)
    col["sm3_source_basename"] = Path(filepath).name.replace(".wrap.mesh", ".mesh")
    col["sm3_wrap_archive_hash"] = f"0x{int(info.archive_hash) & 0xFFFFFFFF:08X}"
    col["sm3_wrap_imported"] = True
    for obj in objs:
        obj["sm3_source_mesh"] = os.path.abspath(filepath)
        obj["sm3_wrap_imported"] = True
        if getattr(obj, "data", None) is not None:
            obj.data["sm3_source_mesh"] = os.path.abspath(filepath)
    return col, objs, arm, mesh


def _import_skeleton_path(filepath: str, collection):
    if not is_wrap_file(filepath):
        return import_skeleton(filepath, collection=collection)
    flat, info = unwrap_file(filepath)
    with tempfile.TemporaryDirectory(prefix="sm3_lite_wrap_skel_") as td:
        rawpath = Path(td) / _raw_name_from_wrap(filepath)
        rawpath.write_bytes(flat)
        arm, skel = import_skeleton(str(rawpath), collection=collection)
    arm["sm3_source_skel"] = os.path.abspath(filepath)
    arm["sm3_wrap_archive_hash"] = f"0x{int(info.archive_hash) & 0xFFFFFFFF:08X}"
    arm["sm3_wrap_imported"] = True
    return arm, skel


def _armature_for_source(source_collection):
    if source_collection is None:
        return None
    return next((obj for obj in source_collection.objects if obj.type == "ARMATURE"), None)


def _attach_armature_keep_world(obj, armature):
    if obj is None or armature is None:
        return
    world = obj.matrix_world.copy()
    obj.parent = armature
    try:
        obj.matrix_parent_inverse = armature.matrix_world.inverted()
    except Exception:
        pass
    obj.matrix_world = world

    arm_mod = next((m for m in obj.modifiers if m.type == "ARMATURE"), None)
    if arm_mod is None:
        arm_mod = obj.modifiers.new(name="SM3 Armature", type="ARMATURE")
    arm_mod.object = armature


def _active_mesh(context):
    obj = context.view_layer.objects.active
    return obj if obj is not None and obj.type == "MESH" else None


def _selected_meshes(context):
    objs = [obj for obj in context.selected_objects if obj.type == "MESH"]
    if not objs:
        active = _active_mesh(context)
        if active is not None:
            objs = [active]
    return objs


def _set_vertex_color(obj, rgba):
    mesh = obj.data
    if not hasattr(mesh, "color_attributes"):
        raise ValueError(f"{obj.name}: Blender color attributes are unavailable")
    attr = mesh.color_attributes.get("Col_0")
    if attr is None or attr.domain != "CORNER" or attr.data_type != "BYTE_COLOR":
        if attr is not None:
            mesh.color_attributes.remove(attr)
        attr = mesh.color_attributes.new(name="Col_0", type="BYTE_COLOR", domain="CORNER")
    for item in attr.data:
        if hasattr(item, "color_srgb"):
            item.color_srgb = rgba
        else:
            item.color = rgba
    try:
        mesh.color_attributes.active_color = attr
    except Exception:
        pass
    mesh.update()


def _ensure_material_slots(obj):
    added = 0
    for h, real_name, role in CANONICAL_SPIDER_MATERIALS:
        found = None
        for mat in obj.data.materials:
            if material_hash(mat) == h:
                found = mat
                break
        if found is None:
            exact = f"0x{h:08X}"
            mat = bpy.data.materials.get(exact) or bpy.data.materials.new(exact)
            mat["sm3_real_mat_hash"] = exact
            mat["sm3_real_mat_name"] = real_name
            mat["sm3_lite_role"] = role
            obj.data.materials.append(mat)
            added += 1
        else:
            found["sm3_real_mat_hash"] = f"0x{h:08X}"
            found["sm3_real_mat_name"] = real_name
            found["sm3_lite_role"] = role
    return added


def _face_count_for_hash(obj, h: int) -> int:
    slots = []
    for i, mat in enumerate(obj.data.materials):
        if material_hash(mat) == int(h):
            slots.append(i)
    if not slots:
        return 0
    slot_set = set(slots)
    return sum(1 for poly in obj.data.polygons if int(poly.material_index) in slot_set)


def _target_collection_for_export(context, objects):
    for obj in objects:
        for key in ("sm3_export_owner_collection", "sm3_export_target_collection"):
            name = str(obj.get(key, "") or "")
            if name and name in bpy.data.collections:
                return bpy.data.collections[name]
    source = context.scene.sm3_lite_source_collection
    if source is not None:
        return source
    try:
        return recover_target_collection_from_objects(objects, scene=context.scene)
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Import
# -----------------------------------------------------------------------------


class SM3LITE_OT_ImportMesh(Operator, ImportHelper):
    bl_idname = "sm3_lite.import_mesh"
    bl_label = "Import SM3 Mesh"
    bl_description = "Import raw .mesh or .wrap.mesh using the proven SM3 parser"
    filename_ext = ".mesh"
    filter_glob: StringProperty(default="*.mesh;*.wrap.mesh", options={"HIDDEN"})

    def execute(self, context):
        try:
            col, objs, arm, mesh = _import_mesh_path(self.filepath)
            context.scene.sm3_lite_source_collection = col
            bpy.ops.object.select_all(action="DESELECT")
            for obj in objs:
                obj.select_set(True)
            if objs:
                context.view_layer.objects.active = objs[0]
            self.report({"INFO"}, f"Imported {col.name}: {len(mesh.sections)} section(s)")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class SM3LITE_OT_ImportMeshDrop(Operator):
    bl_idname = "sm3_lite.import_mesh_drop"
    bl_label = "Import SM3 Mesh"
    files: CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: StringProperty(subtype="DIR_PATH")

    def execute(self, context):
        last_col = None
        count = 0
        for item in self.files:
            try:
                col, objs, _arm, _mesh = _import_mesh_path(os.path.join(self.directory, item.name))
                last_col = col
                count += 1
            except Exception as exc:
                self.report({"ERROR"}, f"{item.name}: {exc}")
                return {"CANCELLED"}
        if last_col is not None:
            context.scene.sm3_lite_source_collection = last_col
        self.report({"INFO"}, f"Imported {count} SM3 mesh file(s)")
        return {"FINISHED"}


class SM3LITE_FH_Mesh(FileHandler):
    bl_idname = "SM3_LITE_MESH_FILEHANDLER"
    bl_label = "SM3 Mesh"
    bl_import_operator = SM3LITE_OT_ImportMeshDrop.bl_idname
    bl_file_extensions = ".mesh"

    @classmethod
    def poll_drop(cls, context):
        return context.area and context.area.type == "VIEW_3D"


class SM3LITE_OT_ImportSkeleton(Operator, ImportHelper):
    bl_idname = "sm3_lite.import_skeleton"
    bl_label = "Import SM3 Skeleton"
    filename_ext = ".skel"
    filter_glob: StringProperty(default="*.skel;*.wrap.skel", options={"HIDDEN"})

    def execute(self, context):
        source = context.scene.sm3_lite_source_collection
        if source is None:
            self.report({"ERROR"}, "Import/select the stock SM3 mesh collection first")
            return {"CANCELLED"}
        try:
            arm, _skel = _import_skeleton_path(self.filepath, source)
            for obj in source.objects:
                if obj.type == "MESH" and len(obj.vertex_groups):
                    _attach_armature_keep_world(obj, arm)
            self.report({"INFO"}, f"Imported skeleton: {arm.name}")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


# -----------------------------------------------------------------------------
# Model port workflow
# -----------------------------------------------------------------------------


class SM3LITE_OT_TransferWeightsArmature(Operator):
    bl_idname = "sm3_lite.transfer_weights_armature"
    bl_label = "Transfer Weights + Armature"
    bl_description = "Transfer stock SM3 weights to the active replacement model, limit to 4, normalize, then bind it to the stock armature"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        source = context.scene.sm3_lite_source_collection
        target = _active_mesh(context)
        if source is None:
            self.report({"ERROR"}, "Choose/import the stock SM3 source collection")
            return {"CANCELLED"}
        if target is None:
            self.report({"ERROR"}, "Select the replacement MESH and make it active")
            return {"CANCELLED"}
        if target in source.objects:
            self.report({"ERROR"}, "Active object is part of the stock source; select your replacement model")
            return {"CANCELLED"}
        arm = _armature_for_source(source)
        if arm is None:
            self.report({"ERROR"}, "The stock source collection has no armature. Import the matching .skel first.")
            return {"CANCELLED"}
        try:
            result = transfer_weights_from_sm3_collection(
                source,
                target,
                clear_existing=True,
                limit_to_four=True,
                normalize=True,
            )
            _attach_armature_keep_world(target, arm)
            target["sm3_lite_rig_ready"] = True
            target["sm3_lite_armature"] = arm.name

            # WoS-style cleanup: donor BODY can be hidden after transfer; keep skeleton.
            for obj in source.objects:
                if obj.type == "MESH":
                    obj.hide_set(True)
                    obj.hide_render = True

            weighted = int(result.get("weighted_vertices_after", 0))
            total = max(1, int(result.get("target_vertex_count", len(target.data.vertices))))
            self.report({"INFO"}, f"Rig ready: {weighted}/{total} weighted vertices; stock body hidden, armature kept")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class SM3LITE_OT_EnsureMaterials(Operator):
    bl_idname = "sm3_lite.ensure_materials"
    bl_label = "Add / Verify 6 Spider Materials"
    bl_description = "Append any missing canonical Spider-Man material hash slots without changing face assignments"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = _active_mesh(context)
        if obj is None:
            self.report({"ERROR"}, "Select the replacement MESH")
            return {"CANCELLED"}
        added = _ensure_material_slots(obj)
        self.report({"INFO"}, f"Spider material slots ready; added {added}")
        return {"FINISHED"}


class SM3LITE_OT_ColorBlack(Operator):
    bl_idname = "sm3_lite.color_black"
    bl_label = "BLACK"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        objs = _selected_meshes(context)
        if not objs:
            self.report({"ERROR"}, "Select a MESH")
            return {"CANCELLED"}
        for obj in objs:
            _set_vertex_color(obj, (0.0, 0.0, 0.0, 1.0))
            obj["sm3_lite_vertex_color_test"] = "BLACK"
        self.report({"INFO"}, f"Applied BLACK vertex color to {len(objs)} mesh(es)")
        return {"FINISHED"}


class SM3LITE_OT_ColorWhite(Operator):
    bl_idname = "sm3_lite.color_white"
    bl_label = "WHITE"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        objs = _selected_meshes(context)
        if not objs:
            self.report({"ERROR"}, "Select a MESH")
            return {"CANCELLED"}
        for obj in objs:
            _set_vertex_color(obj, (1.0, 1.0, 1.0, 1.0))
            obj["sm3_lite_vertex_color_test"] = "WHITE"
        self.report({"INFO"}, f"Applied WHITE vertex color to {len(objs)} mesh(es)")
        return {"FINISHED"}


# -----------------------------------------------------------------------------
# DDS -> WRAP TEX (drag and drop like WoS)
# -----------------------------------------------------------------------------


class SM3LITE_OT_ConvertDDS(Operator, ImportHelper):
    bl_idname = "sm3_lite.convert_dds"
    bl_label = "DDS -> WRAP TEX"
    bl_description = "Convert a DXT1/DXT3/DXT5 DDS to an SM3 CH_SPIDERMAN .wrap.tex beside the DDS"
    filename_ext = ".dds"
    filter_glob: StringProperty(default="*.dds", options={"HIDDEN"})

    def execute(self, context):
        try:
            out, report = dds_to_wrap_tex(self.filepath)
            self.report({"INFO"}, f"Created {Path(out).name} | 0x{report['tex_hash']:08X} | {report['width']}x{report['height']} {report['fourcc']}")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class SM3LITE_OT_ConvertDDSDrop(Operator):
    bl_idname = "sm3_lite.convert_dds_drop"
    bl_label = "DDS -> WRAP TEX"
    files: CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: StringProperty(subtype="DIR_PATH")

    def execute(self, context):
        count = 0
        for item in self.files:
            try:
                dds_to_wrap_tex(os.path.join(self.directory, item.name))
                count += 1
            except Exception as exc:
                self.report({"ERROR"}, f"{item.name}: {exc}")
                return {"CANCELLED"}
        self.report({"INFO"}, f"Converted {count} DDS file(s) to SM3 WRAP TEX")
        return {"FINISHED"}


class SM3LITE_FH_DDS(FileHandler):
    bl_idname = "SM3_LITE_DDS_FILEHANDLER"
    bl_label = "SM3 DDS -> WRAP TEX"
    bl_import_operator = SM3LITE_OT_ConvertDDSDrop.bl_idname
    bl_file_extensions = ".dds"

    @classmethod
    def poll_drop(cls, context):
        return context.area and context.area.type == "VIEW_3D"


# -----------------------------------------------------------------------------
# Export
# -----------------------------------------------------------------------------


class SM3LITE_OT_ExportSpider(Operator, ExportHelper):
    bl_idname = "sm3_lite.export_spider"
    bl_label = "Export Spider-Man 000 Mesh"
    bl_description = "Export selected weighted mesh(es) with native Spider-Man material routing and weight-safe 32-bone sections"
    filename_ext = ".mesh"
    filter_glob: StringProperty(default="*.mesh", options={"HIDDEN"})

    def invoke(self, context, event):
        if not self.filepath:
            self.filepath = "0xAC92103D.ch_spiderman000.mesh"
        return super().invoke(context, event)

    def execute(self, context):
        objects = _selected_meshes(context)
        if not objects:
            self.report({"ERROR"}, "Select the replacement MESH to export")
            return {"CANCELLED"}
        target = _target_collection_for_export(context, objects)
        if target is None:
            self.report({"ERROR"}, "No saved stock Spider-Man target found. Import stock 000 and run Transfer Weights + Armature first.")
            return {"CANCELLED"}
        for obj in objects:
            arm = next((m.object for m in obj.modifiers if m.type == "ARMATURE" and m.object is not None), None)
            if arm is None and not (obj.parent and obj.parent.type == "ARMATURE"):
                self.report({"ERROR"}, f"{obj.name}: no armature deform/binding found")
                return {"CANCELLED"}
        try:
            result = export_spider_000_material_routed(
                objects,
                target,
                self.filepath,
                flip_uv_v=True,
                reverse_winding=True,
                write_report=True,
            )
            self.report({"INFO"}, f"Exported {Path(self.filepath).name}: {result.section_count} section(s), weights preserved")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


# -----------------------------------------------------------------------------
# Lean panel
# -----------------------------------------------------------------------------


class SM3LITE_PT_Main(Panel):
    bl_label = "SM3 WoS Lite"
    bl_idname = "SM3LITE_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "SM3 WoS Lite"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        active = _active_mesh(context)

        box = layout.box()
        box.label(text="1. Import stock SM3", icon="IMPORT")
        row = box.row(align=True)
        row.operator(SM3LITE_OT_ImportMesh.bl_idname, text="Import Mesh")
        row.operator(SM3LITE_OT_ImportSkeleton.bl_idname, text="Import Skeleton")
        box.prop(scene, "sm3_lite_source_collection", text="Stock Source")

        box = layout.box()
        box.label(text="2. Rig replacement model", icon="ARMATURE_DATA")
        box.label(text=f"Active: {active.name if active else 'Select replacement MESH'}")
        box.label(text="Align the replacement over stock Spider-Man first.", icon="INFO")
        box.operator(SM3LITE_OT_TransferWeightsArmature.bl_idname, text="TRANSFER WEIGHTS + ARMATURE", icon="MOD_ARMATURE")
        if active is not None:
            weighted = sum(1 for v in active.data.vertices if len(v.groups))
            arm = next((m.object for m in active.modifiers if m.type == "ARMATURE" and m.object), None)
            box.label(text=f"Weights: {weighted}/{len(active.data.vertices)} | Armature: {arm.name if arm else 'NONE'}")

        box = layout.box()
        box.label(text="3. Spider-Man material routing", icon="MATERIAL")
        box.operator(SM3LITE_OT_EnsureMaterials.bl_idname, text="ADD / VERIFY 6 MATERIALS")
        for h, _name, role in CANONICAL_SPIDER_MATERIALS:
            faces = _face_count_for_hash(active, h) if active is not None else 0
            box.label(text=f"0x{h:08X}  {role}  [{faces} faces]")
        box.label(text="Assign faces with Blender's normal Material > Assign controls.", icon="INFO")

        box = layout.box()
        box.label(text="4. Vertex color test", icon="COLOR")
        row = box.row(align=True)
        row.operator(SM3LITE_OT_ColorBlack.bl_idname, text="BLACK")
        row.operator(SM3LITE_OT_ColorWhite.bl_idname, text="WHITE")
        if active is not None:
            box.label(text=f"Current test: {active.get('sm3_lite_vertex_color_test', 'UNCHANGED')}")

        box = layout.box()
        box.label(text="5. Texture conversion", icon="TEXTURE")
        box.label(text="Drag a DXT DDS into the 3D View -> .wrap.tex beside it")
        box.label(text="Best name: 0xHASH.resource_name.dds", icon="INFO")
        box.operator(SM3LITE_OT_ConvertDDS.bl_idname, text="DDS -> WRAP TEX")

        box = layout.box()
        box.label(text="6. Export", icon="EXPORT")
        box.label(text="Select the weighted replacement model and export.")
        box.operator(SM3LITE_OT_ExportSpider.bl_idname, text="EXPORT SPIDER-MAN 000 MESH")


# -----------------------------------------------------------------------------
# File menu hooks
# -----------------------------------------------------------------------------


def _menu_import(self, context):
    self.layout.operator(SM3LITE_OT_ImportMesh.bl_idname, text="SM3 Mesh / WRAP Mesh")
    self.layout.operator(SM3LITE_OT_ImportSkeleton.bl_idname, text="SM3 Skeleton / WRAP Skeleton")


def _menu_export(self, context):
    self.layout.operator(SM3LITE_OT_ExportSpider.bl_idname, text="SM3 Spider-Man 000 Mesh")


CLASSES = (
    SM3LITE_OT_ImportMesh,
    SM3LITE_OT_ImportMeshDrop,
    SM3LITE_FH_Mesh,
    SM3LITE_OT_ImportSkeleton,
    SM3LITE_OT_TransferWeightsArmature,
    SM3LITE_OT_EnsureMaterials,
    SM3LITE_OT_ColorBlack,
    SM3LITE_OT_ColorWhite,
    SM3LITE_OT_ConvertDDS,
    SM3LITE_OT_ConvertDDSDrop,
    SM3LITE_FH_DDS,
    SM3LITE_OT_ExportSpider,
    SM3LITE_PT_Main,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.sm3_lite_source_collection = PointerProperty(
        name="Stock SM3 Source",
        type=bpy.types.Collection,
        description="Imported stock Spider-Man 000 collection used for weight transfer and export schema",
    )
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)
    bpy.types.TOPBAR_MT_file_export.append(_menu_export)


def unregister():
    try:
        bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
        bpy.types.TOPBAR_MT_file_export.remove(_menu_export)
    except Exception:
        pass
    if hasattr(bpy.types.Scene, "sm3_lite_source_collection"):
        del bpy.types.Scene.sm3_lite_source_collection
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


if __name__ == "__main__":
    register()
