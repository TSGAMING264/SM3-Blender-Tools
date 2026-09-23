bl_info = {
    "name": "SM3 Material Combiner Toolkit",
    "author": "TSGAMING264",
    "version": (1, 2, 0),
    "blender": (4, 5, 0),
    "location": "3D View > N Panel > SM3 Materials",
    "description": "Separate heavy SM3 REAL MAT/TEX database, material combiner, atlas and texture research tools",
    "category": "Material",
}

import json
import os
import re
import traceback
from pathlib import Path

import bpy
from bpy.types import Operator, FileHandler, Panel, PropertyGroup, UIList
from bpy.props import CollectionProperty, IntProperty, StringProperty, BoolProperty, PointerProperty

from .sm3_texture import convert_texture_file
from .sm3_material_atlas import create_material_atlas, find_material_image
from .sm3_material_database import (
    DATABASE_VERSION as SM3_MATERIAL_DATABASE_VERSION,
    get_material_database,
    material_hash_name,
    unresolved_ref_name,
)


def _write_last_error(label: str):
    report = f"{label}\n{'=' * len(label)}\n\n{traceback.format_exc()}"
    text = bpy.data.texts.get("SM3_Material_Last_Error") or bpy.data.texts.new("SM3_Material_Last_Error")
    text.clear(); text.write(report)
    print("\n" + report + "\n")

# ============================================================
# Material + texture utilities
# ============================================================

_MATERIAL_SUFFIX_RE = re.compile(r"\.\d{3}$")
_MATERIAL_HEX_RE = re.compile(r"^0x([0-9A-Fa-f]{8})$")
_SM3_MATREF_RE = re.compile(r"^SM3_MATREF_([0-9A-Fa-f]{8})$", re.IGNORECASE)


def _clean_material_name(name: str) -> str:
    return _MATERIAL_SUFFIX_RE.sub("", str(name or ""))


def _idprop_u32(value, default=None):
    if value is None:
        return default
    try:
        if isinstance(value, str):
            return int(value, 0) & 0xFFFFFFFF
        return int(value) & 0xFFFFFFFF
    except Exception:
        return default


def _real_mat_hash_from_material(mat):
    if mat is None:
        return None
    explicit = _idprop_u32(mat.get("sm3_real_mat_hash"))
    if explicit is not None:
        return explicit

    # v0.4.3 scenes wrote the local pointer into both the visible 0x name and
    # sm3_serialized_material_ref.  Do not silently promote that legacy value
    # to a REAL MAT hash.  The migration button will move it to SM3_REF_.
    if mat.get("sm3_serialized_material_ref") is not None:
        return None

    base = _clean_material_name(mat.name)
    m = _MATERIAL_HEX_RE.match(base)
    if m:
        return int(m.group(1), 16) & 0xFFFFFFFF
    return None




# ============================================================
# SM3 clone material-list workflow
# ============================================================

SM3MAT_LIST_OBJECT = 0
SM3MAT_LIST_MATERIAL = 1


def _display_real_mat_hash(mat):
    value = _real_mat_hash_from_material(mat)
    if value is None:
        return ""
    return f"0x{int(value) & 0xFFFFFFFF:08X}"


class SM3MAT_MaterialListEntry(PropertyGroup):
    item_type: IntProperty(name="Entry Type", default=SM3MAT_LIST_MATERIAL)
    object_ref: PointerProperty(name="Object", type=bpy.types.Object)
    material_ref: PointerProperty(name="Material", type=bpy.types.Material)
    slot_index: IntProperty(name="Slot", default=-1)
    used: BoolProperty(name="Use", default=True)
    layer: IntProperty(name="Layer", default=1, min=1, max=99)
    sm3_hash: StringProperty(name="REAL MAT", default="")
    image_name: StringProperty(name="Image", default="")
    face_count: IntProperty(name="Faces", default=0, min=0)


class SM3MAT_UL_MaterialsToCombine(UIList):
    bl_idname = "SM3MAT_UL_materials_to_combine"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        if item.item_type == SM3MAT_LIST_OBJECT:
            if item.object_ref:
                row.label(text=item.object_ref.name, icon="OUTLINER_OB_MESH")
            else:
                row.label(text="<missing object>", icon="ERROR")
            return

        row.separator(factor=1.2)
        row.prop(item, "used", text="")
        row.label(text=f"S{item.slot_index}")
        if item.material_ref:
            row.prop(item.material_ref, "name", text="", emboss=False)
        else:
            row.label(text="<empty material>", icon="ERROR")
        if item.sm3_hash:
            hash_col = row.column(align=True)
            hash_col.scale_x = 0.70
            hash_col.label(text=item.sm3_hash)
        diag = row.column(align=True)
        diag.scale_x = 0.65
        diag.label(text=f"F{item.face_count}")
        if item.image_name:
            img = row.column(align=True)
            img.scale_x = 0.9
            shown = item.image_name if len(item.image_name) <= 24 else item.image_name[:21] + "..."
            img.label(text=shown, icon="IMAGE_DATA")
        else:
            row.label(text="NO IMAGE", icon="ERROR")
        layer_col = row.column(align=True)
        layer_col.scale_x = 0.30
        layer_col.prop(item, "layer", text="")


class SM3_OT_Update_Material_List(Operator):
    bl_idname = "sm3mat.update_material_list"
    bl_label = "Update Material List"
    bl_description = "Scan Blender mesh material slots and rebuild the SM3 Materials to Combine list"
    bl_options = {"REGISTER"}

    def execute(self, context):
        scene = context.scene
        previous = {}
        for item in scene.sm3mat_material_list:
            if item.item_type != SM3MAT_LIST_MATERIAL or not item.object_ref:
                continue
            key = (item.object_ref.as_pointer(), int(item.slot_index))
            previous[key] = (bool(item.used), int(item.layer))

        if scene.sm3mat_list_selected_only:
            objects = [obj for obj in context.selected_objects if obj.type == "MESH"]
        else:
            objects = [obj for obj in context.visible_objects if obj.type == "MESH"]

        objects = [obj for obj in objects if len(obj.material_slots) > 0]
        objects.sort(key=lambda o: o.name.casefold())

        scene.sm3mat_material_list.clear()
        material_count = 0
        for obj in objects:
            header = scene.sm3mat_material_list.add()
            header.item_type = SM3MAT_LIST_OBJECT
            header.object_ref = obj
            header.slot_index = -1

            for slot_index, slot in enumerate(obj.material_slots):
                mat = slot.material
                if mat is None:
                    continue
                entry = scene.sm3mat_material_list.add()
                entry.item_type = SM3MAT_LIST_MATERIAL
                entry.object_ref = obj
                entry.material_ref = mat
                entry.slot_index = slot_index
                entry.sm3_hash = _display_real_mat_hash(mat)
                entry.face_count = sum(1 for poly in obj.data.polygons if int(poly.material_index) == int(slot_index))
                image = find_material_image(mat)
                entry.image_name = image.name if image is not None else ""
                old = previous.get((obj.as_pointer(), slot_index))
                if old:
                    entry.used, entry.layer = old
                material_count += 1

        scene.sm3mat_material_list_index = min(
            max(0, int(scene.sm3mat_material_list_index)),
            max(0, len(scene.sm3mat_material_list) - 1),
        )
        self.report({"INFO"}, f"SM3 material list updated: {len(objects)} object(s), {material_count} material slot(s)")
        return {"FINISHED"}


class SM3_OT_Material_List_Select_All(Operator):
    bl_idname = "sm3mat.material_list_select_all"
    bl_label = "Select All"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        for item in context.scene.sm3mat_material_list:
            if item.item_type == SM3MAT_LIST_MATERIAL:
                item.used = True
        return {"FINISHED"}


class SM3_OT_Material_List_Select_None(Operator):
    bl_idname = "sm3mat.material_list_select_none"
    bl_label = "Select None"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        for item in context.scene.sm3mat_material_list:
            if item.item_type == SM3MAT_LIST_MATERIAL:
                item.used = False
        return {"FINISHED"}


def _material_list_slot_selection(scene):
    """Return {object_pointer: {slot indices}} for checked SM3 list entries."""
    result = {}
    for item in scene.sm3mat_material_list:
        if item.item_type != SM3MAT_LIST_MATERIAL or not item.used or not item.object_ref:
            continue
        result.setdefault(item.object_ref.as_pointer(), set()).add(int(item.slot_index))
    return result

def _material_combine_key(mat):
    if mat is None:
        return ("EMPTY", "")

    # v0.5.0: 0xXXXXXXXX means REAL MAT hash.  APKF-local serialized refs are
    # provenance only and must never be used as material identity.
    real_hash = _real_mat_hash_from_material(mat)
    if real_hash is not None:
        return ("REAL_MAT", real_hash)

    base = _clean_material_name(mat.name)
    m = _SM3_MATREF_RE.match(base)
    if m:
        return ("UNRESOLVED_REF", int(m.group(1), 16) & 0xFFFFFFFF)
    m = re.match(r"^SM3_REF_([0-9A-Fa-f]{8})$", base, re.IGNORECASE)
    if m:
        return ("UNRESOLVED_REF", int(m.group(1), 16) & 0xFFFFFFFF)

    return ("NAME", base.casefold())


class SM3_OT_Convert_Legacy_Material_Names(Operator):
    bl_idname = "sm3mat.convert_legacy_material_names"
    bl_label = "Upgrade Legacy Material Names"
    bl_description = "Convert old SM3_MATREF_XXXXXXXX/local-ref materials to the safe SM3_REF_XXXXXXXX unresolved form"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        meshes = [obj for obj in context.selected_objects if obj.type == "MESH"]
        if not meshes:
            self.report({"ERROR"}, "Select one or more mesh objects")
            return {"CANCELLED"}

        seen = set()
        renamed = 0
        for obj in meshes:
            for slot in obj.material_slots:
                mat = slot.material
                if mat is None or mat.as_pointer() in seen:
                    continue
                seen.add(mat.as_pointer())

                # Explicit v0.5.0 REAL MAT identity: leave it alone.
                if _idprop_u32(mat.get("sm3_real_mat_hash")) is not None:
                    continue

                base = _clean_material_name(mat.name)
                value = None
                m = _SM3_MATREF_RE.match(base)
                if m:
                    value = int(m.group(1), 16) & 0xFFFFFFFF
                if value is None:
                    value = _idprop_u32(mat.get("sm3_serialized_material_ref"))
                if value is None:
                    continue

                desired = unresolved_ref_name(value)
                if mat.name != desired:
                    mat.name = desired
                    renamed += 1
                mat["sm3_material_resolution"] = "UNRESOLVED_SERIALIZED_REF"

        self.report({"INFO"}, f"Legacy material upgrade: {renamed} material(s) moved to SM3_REF_XXXXXXXX")
        return {"FINISHED"}


class SM3_OT_Material_Combiner(Operator):
    bl_idname = "sm3mat.combine_duplicate_materials"
    bl_label = "Material Combiner"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        meshes = [obj for obj in context.selected_objects if obj.type == "MESH"]
        if not meshes:
            self.report({"ERROR"}, "Select one or more mesh objects")
            return {"CANCELLED"}

        canonical = {}
        remapped = 0
        renamed = 0
        touched = 0

        for obj in meshes:
            for slot in obj.material_slots:
                mat = slot.material
                if mat is None:
                    continue
                touched += 1
                key = _material_combine_key(mat)
                base = _clean_material_name(mat.name)
                keeper = canonical.get(key)
                if keeper is None:
                    canonical[key] = mat

                    # Canonical REAL MAT identity is the stable hash.
                    desired = base
                    if key[0] == "REAL_MAT":
                        desired = f"0x{int(key[1]) & 0xFFFFFFFF:08X}"
                        try:
                            mat["sm3_real_mat_hash"] = desired
                        except Exception:
                            pass
                    elif key[0] == "UNRESOLVED_REF":
                        desired = unresolved_ref_name(int(key[1]))

                    # Blender may add .001/.002 if another datablock owns the exact
                    # name.  The exporter deliberately strips that suffix, like WoS.
                    if desired and mat.name != desired:
                        try:
                            mat.name = desired
                            renamed += 1
                        except Exception:
                            pass
                    continue
                if keeper != mat:
                    slot.material = keeper
                    remapped += 1

        self.report(
            {"INFO"},
            f"Material Combiner: {len(canonical)} unique material(s), {remapped} duplicate slot(s) combined, {touched} slot(s) checked",
        )
        return {"FINISHED"}


class SM3_OT_Material_Database_Status(Operator):
    bl_idname = "sm3mat.material_database_status"
    bl_label = "Material Database Status"
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            db = get_material_database()
            msg = db.summary()
            text = bpy.data.texts.get("SM3_Material_Database_Status") or bpy.data.texts.new("SM3_Material_Database_Status")
            text.clear()
            text.write(msg + "\n")
            self.report({"INFO"}, msg)
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Material database failed: {exc}")
            return {"CANCELLED"}


class SM3_OT_Resolve_Materials_From_Database(Operator):
    bl_idname = "sm3mat.resolve_materials_from_database"
    bl_label = "Resolve Selected Materials from Database"
    bl_description = "Convert imported APKF-local material refs into REAL MAT 0x hashes using mesh hash + field offset + serialized ref"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        meshes = [obj for obj in context.selected_objects if obj.type == "MESH"]
        if not meshes:
            self.report({"ERROR"}, "Select one or more imported SM3 mesh objects")
            return {"CANCELLED"}

        try:
            db = get_material_database()
        except Exception as exc:
            self.report({"ERROR"}, f"Material database failed: {exc}")
            return {"CANCELLED"}

        resolved = 0
        unresolved = 0
        for obj in meshes:
            mesh_hash = _idprop_u32(obj.get("sm3_mesh_hash"))
            field_offset = _idprop_u32(obj.get("sm3_material_field_offset"))
            serialized = _idprop_u32(obj.get("sm3_serialized_material_ref"))
            if mesh_hash is None or field_offset is None or serialized is None:
                unresolved += 1
                continue

            result = db.resolve_mesh_material(mesh_hash, field_offset, serialized)
            if result is None:
                unresolved += 1
                if obj.material_slots and obj.material_slots[0].material:
                    obj.material_slots[0].material.name = unresolved_ref_name(serialized)
                continue

            desired = material_hash_name(result.mat_hash)
            mat = bpy.data.materials.get(desired) or bpy.data.materials.new(desired)
            mat["sm3_real_mat_hash"] = desired
            mat["sm3_real_mat_name"] = result.mat_name
            mat["sm3_material_database"] = SM3_MATERIAL_DATABASE_VERSION
            mat["sm3_material_resolution"] = result.match_mode
            bindings = db.textures_for_material(result.mat_hash)
            mat["sm3_texture_binding_count"] = len(bindings)
            mat["sm3_texture_bindings"] = "|".join(
                f"0x{b.field_offset:08X}:0x{b.tex_hash:08X}:{b.tex_name}" for b in bindings
            )

            if len(obj.data.materials) == 0:
                obj.data.materials.append(mat)
            else:
                obj.data.materials[0] = mat
            obj["sm3_real_mat_hash"] = desired
            obj["sm3_real_mat_name"] = result.mat_name
            obj["sm3_material_resolution"] = result.match_mode
            resolved += 1

        self.report({"INFO"}, f"Material database: {resolved} resolved, {unresolved} unresolved")
        return {"FINISHED"}


def _find_texture_file(index, tex_hash: int, tex_name: str):
    h = f"{int(tex_hash) & 0xFFFFFFFF:08x}"
    n = str(tex_name or "").casefold()
    # Strongest: filename contains the exact 8-digit TEX hash.
    for path in index:
        if h in path.name.casefold():
            return path
    # Second: exact/contained resource name.
    if n:
        for path in index:
            stem = path.stem.casefold()
            if stem == n or n in stem:
                return path
    return None


def _load_database_textures_into_material(mat, dds_files, db):
    real_hash = _real_mat_hash_from_material(mat)
    if real_hash is None:
        return 0, 0
    bindings = db.textures_for_material(real_hash)
    if not bindings:
        return 0, 0

    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (700, 0)
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (400, 0)
    links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])

    loaded = 0
    missing = 0
    y = 300
    diffuse_node = None
    normal_node = None
    for binding in bindings:
        path = _find_texture_file(dds_files, binding.tex_hash, binding.tex_name)
        tex = nodes.new("ShaderNodeTexImage")
        tex.name = f"SM3_TEX_{binding.tex_hash:08X}_{binding.field_offset:02X}"
        tex.label = f"+0x{binding.field_offset:X} 0x{binding.tex_hash:08X} {binding.tex_name}"
        tex.location = (-500, y)
        y -= 230
        tex["sm3_tex_hash"] = f"0x{binding.tex_hash:08X}"
        tex["sm3_tex_name"] = binding.tex_name
        tex["sm3_mat_field_offset"] = f"0x{binding.field_offset:08X}"
        if path is not None:
            try:
                image = bpy.data.images.get(path.name) or bpy.data.images.load(str(path), check_existing=True)
                tex.image = image
                loaded += 1
            except Exception:
                missing += 1
        else:
            missing += 1

        lname = binding.tex_name.casefold()
        if diffuse_node is None and ("_dif" in lname or "diffuse" in lname):
            diffuse_node = tex
        if normal_node is None and ("_nor" in lname or "normal" in lname):
            normal_node = tex

    if diffuse_node is not None and diffuse_node.image is not None:
        links.new(diffuse_node.outputs["Color"], bsdf.inputs["Base Color"])
        if "Alpha" in diffuse_node.outputs and "Alpha" in bsdf.inputs:
            links.new(diffuse_node.outputs["Alpha"], bsdf.inputs["Alpha"])

    if normal_node is not None and normal_node.image is not None:
        try:
            normal_node.image.colorspace_settings.name = "Non-Color"
        except Exception:
            pass
        normal_map = nodes.new("ShaderNodeNormalMap")
        normal_map.location = (120, -250)
        links.new(normal_node.outputs["Color"], normal_map.inputs["Color"])
        links.new(normal_map.outputs["Normal"], bsdf.inputs["Normal"])

    return loaded, missing


class SM3_OT_Texture_Research_Report(Operator):
    bl_idname = "sm3mat.texture_research_report"
    bl_label = "Build Selected MAT -> TEX Report"
    bl_description = "Create a Blender Text Editor report showing selected SM3 sections, REAL MAT identities, serialized refs, and all known MAT -> TEX bindings"
    bl_options = {"REGISTER"}

    def execute(self, context):
        meshes = [obj for obj in context.selected_objects if obj.type == "MESH"]
        if not meshes:
            self.report({"ERROR"}, "Select one or more imported SM3 mesh sections")
            return {"CANCELLED"}

        try:
            db = get_material_database()
        except Exception as exc:
            self.report({"ERROR"}, f"Material database failed: {exc}")
            return {"CANCELLED"}

        lines = [
            "SM3 TEXTURE RESEARCH REPORT",
            "===========================",
            "",
            "Purpose:",
            "  Compare the model section -> REAL MAT -> REAL TEX relationships.",
            "  0xXXXXXXXX material names are REAL MAT hashes.",
            "  Serialized refs are APKF-local provenance and are NOT MAT hashes.",
            "",
        ]

        section_count = 0
        binding_count = 0

        for obj in sorted(meshes, key=lambda o: o.name.casefold()):
            mesh_hash = _idprop_u32(obj.get("sm3_mesh_hash"))
            field_offset = _idprop_u32(obj.get("sm3_material_field_offset"))
            serialized = _idprop_u32(obj.get("sm3_serialized_material_ref"))
            real_hash = _idprop_u32(obj.get("sm3_real_mat_hash"))

            mat = obj.material_slots[0].material if obj.material_slots else None
            if real_hash is None and mat is not None:
                real_hash = _real_mat_hash_from_material(mat)

            mat_name = str(obj.get("sm3_real_mat_name") or "")
            if not mat_name and mat is not None:
                mat_name = str(mat.get("sm3_real_mat_name") or "")

            lines.append(f"OBJECT: {obj.name}")
            lines.append(f"  MESH hash: {f'0x{mesh_hash:08X}' if mesh_hash is not None else '<unknown>'}")
            lines.append(
                f"  Material field: {f'0x{field_offset:08X}' if field_offset is not None else '<unknown>'}"
            )
            lines.append(
                f"  Serialized local ref: {f'0x{serialized:08X}' if serialized is not None else '<unknown>'}"
            )
            lines.append(
                f"  REAL MAT: {f'0x{real_hash:08X}' if real_hash is not None else '<unresolved>'}"
                + (f"  {mat_name}" if mat_name else "")
            )

            if real_hash is None:
                lines.append("  TEX bindings: unresolved because REAL MAT is unknown")
                lines.append("")
                section_count += 1
                continue

            bindings = db.textures_for_material(real_hash)
            if not bindings:
                lines.append("  TEX bindings: no MAT -> TEX rows in database")
            else:
                lines.append(f"  TEX bindings ({len(bindings)}):")
                for binding in bindings:
                    lines.append(
                        f"    MAT +0x{binding.field_offset:X}"
                        f" -> TEX 0x{binding.tex_hash:08X} {binding.tex_name}"
                    )
                    binding_count += 1

            lines.append("")
            section_count += 1

        lines += [
            "SUMMARY",
            "-------",
            f"Selected mesh sections: {section_count}",
            f"Known MAT -> TEX binding rows listed: {binding_count}",
            "",
            "Use this report together with the DDS loader to test which texture",
            "relationships actually control the visible parts of the model.",
        ]

        block = bpy.data.texts.get("SM3_Texture_Research_Report") or bpy.data.texts.new(
            "SM3_Texture_Research_Report"
        )
        block.clear()
        block.write("\n".join(lines) + "\n")

        self.report(
            {"INFO"},
            f"Texture research report created for {section_count} section(s). "
            "Open Text Editor > SM3_Texture_Research_Report",
        )
        return {"FINISHED"}


class SM3_OT_Load_Resolved_DDS_Textures(Operator):
    bl_idname = "sm3mat.load_resolved_dds_textures"
    bl_label = "Load DDS Folder into Resolved Materials"
    bl_description = "Find DDS files by REAL TEX hash/name and build Blender material nodes from the MAT -> TEX database"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        folder = Path(bpy.path.abspath(context.scene.sm3mat_texture_folder or ""))
        if not folder.exists() or not folder.is_dir():
            self.report({"ERROR"}, "Choose a valid DDS texture folder")
            return {"CANCELLED"}
        meshes = [obj for obj in context.selected_objects if obj.type == "MESH"]
        if not meshes:
            self.report({"ERROR"}, "Select one or more mesh objects")
            return {"CANCELLED"}
        try:
            db = get_material_database()
        except Exception as exc:
            self.report({"ERROR"}, f"Material database failed: {exc}")
            return {"CANCELLED"}

        dds_files = [p for p in folder.rglob("*") if p.is_file() and p.suffix.casefold() == ".dds"]
        seen = set()
        loaded = 0
        missing = 0
        materials = 0
        for obj in meshes:
            for slot in obj.material_slots:
                mat = slot.material
                if mat is None or mat.as_pointer() in seen:
                    continue
                seen.add(mat.as_pointer())
                materials += 1
                l, m = _load_database_textures_into_material(mat, dds_files, db)
                loaded += l
                missing += m

        self.report({"INFO"}, f"DDS material load: {loaded} texture(s) loaded, {missing} missing across {materials} material(s)")
        return {"FINISHED"}


class SM3_OT_Create_Material_Atlas(Operator):
    bl_idname = "sm3mat.create_material_atlas"
    bl_label = "Create Real Texture Atlas"
    bl_description = "Pack checked SM3 slots into one atlas, remap UVs by object+slot, and preserve the original SM3 material slots/hashes"
    bl_options = {"REGISTER", "UNDO"}

    directory: StringProperty(
        name="Atlas Output Folder",
        description="Folder for the atlas PNG, DDS, and JSON report",
        subtype="DIR_PATH",
    )

    def invoke(self, context, event):
        has_selected_mesh = bool([obj for obj in context.selected_objects if obj.type == "MESH"])
        has_checked_list = bool(_material_list_slot_selection(context.scene))
        if not has_selected_mesh and not has_checked_list:
            self.report({"ERROR"}, "Update Materials to Combine and check materials, or select one or more mesh objects")
            return {"CANCELLED"}
        if not self.directory:
            if bpy.data.filepath:
                self.directory = os.path.join(os.path.dirname(bpy.data.filepath), "SM3_ATLAS")
            else:
                self.directory = os.path.join(os.path.expanduser("~"), "SM3_ATLAS")
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        scene = context.scene
        slot_selection = _material_list_slot_selection(scene)
        if scene.sm3mat_material_list and slot_selection:
            meshes = []
            seen = set()
            for item in scene.sm3mat_material_list:
                obj = item.object_ref
                if item.item_type != SM3MAT_LIST_MATERIAL or not item.used or obj is None:
                    continue
                ptr = obj.as_pointer()
                if ptr not in seen:
                    seen.add(ptr)
                    meshes.append(obj)
        else:
            meshes = [obj for obj in context.selected_objects if obj.type == "MESH"]
            slot_selection = None
        if not meshes:
            self.report({"ERROR"}, "Update Materials to Combine and check at least one material, or select mesh objects")
            return {"CANCELLED"}
        try:
            report = create_material_atlas(
                meshes,
                self.directory,
                atlas_name=scene.sm3mat_atlas_name,
                padding=scene.sm3mat_atlas_padding,
                color_tile_size=scene.sm3mat_atlas_color_tile_size,
                slot_selection=slot_selection,
            )
            text_block = bpy.data.texts.get("SM3_Material_Atlas_Result") or bpy.data.texts.new("SM3_Material_Atlas_Result")
            text_block.clear()
            text_block.write(json.dumps(report, indent=2))
            # Refresh the panel so S# rows immediately show the post-atlas
            # materials/images while keeping slot selection state by object+slot.
            try:
                bpy.ops.sm3mat.update_material_list()
            except Exception:
                pass
            self.report(
                {"INFO"},
                f"Slot-safe atlas: {report['atlas_size'][0]}x{report['atlas_size'][1]} | {report.get('slot_count_before', 0)} checked slot(s) | SM3 slots preserved",
            )
            return {"FINISHED"}
        except Exception as exc:
            _write_last_error("SM3 real material atlas failed")
            self.report({"ERROR"}, f"{exc} | See Text Editor > SM3_Last_Import_Error")
            return {"CANCELLED"}


class SM3_OT_Texture_DragDrop_Convert(Operator):
    """Drag and drop DDS/TEX files to convert between DDS and raw SM3 NativeTEX."""
    bl_idname = "import_scene.sm3mat_dds_tex_converter"
    bl_label = "DDS/TEX Converter (SM3)"
    bl_options = {"REGISTER"}

    files: CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: StringProperty(subtype="DIR_PATH")

    def execute(self, context):
        if not self.files:
            self.report({"WARNING"}, "No DDS/TEX files received")
            return {"CANCELLED"}

        converted = 0
        failures = []
        outputs = []
        for file in self.files:
            filepath = os.path.join(self.directory, file.name)
            try:
                report = convert_texture_file(filepath)
                converted += 1
                outputs.append(report.get("output", filepath))
            except Exception as exc:
                failures.append(f"{file.name}: {exc}")

        if outputs:
            text_block = bpy.data.texts.get("SM3_Texture_Convert_Result") or bpy.data.texts.new("SM3_Texture_Convert_Result")
            text_block.clear()
            text_block.write("SM3 DDS/TEX CONVERSION\n\n" + "\n".join(outputs))
            if failures:
                text_block.write("\n\nFAILURES\n" + "\n".join(failures))

        if failures:
            self.report({"WARNING"}, f"Converted {converted}; {len(failures)} failed. See SM3_Texture_Convert_Result")
        else:
            self.report({"INFO"}, f"Converted {converted} texture file(s). Output written beside source file(s).")
        return {"FINISHED"} if converted else {"CANCELLED"}


class SM3_Texture_FileHandler(FileHandler):
    bl_idname = "SM3_MATERIAL_DDS_TEX_FILEHANDLER"
    bl_label = "Convert DDS/TEX Files (SM3)"
    bl_import_operator = "import_scene.sm3mat_dds_tex_converter"
    bl_file_extensions = ".dds;.tex"

    @classmethod
    def poll_drop(cls, context):
        return context.area and context.area.type == "VIEW_3D"

    @classmethod
    def can_handle(cls, context, filepath):
        return filepath.lower().endswith((".dds", ".tex"))




class SM3MAT_PT_Main(Panel):
    bl_label = "SM3 Material Combiner Toolkit"
    bl_idname = "SM3MAT_PT_MAIN"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "SM3 Materials"

    def draw(self, context):
        layout = self.layout
        s = context.scene
        layout.label(text="v1.1.0 - SM3 Material Combiner Clone")
        layout.label(text="Heavy database tools load only with this add-on")

        matbox = layout.box()
        matbox.label(text="Materials to Combine:", icon="MATERIAL")
        matbox.prop(s, "sm3mat_list_selected_only", text="Selected Objects Only")
        matbox.template_list(
            "SM3MAT_UL_materials_to_combine",
            "sm3_materials",
            s,
            "sm3mat_material_list",
            s,
            "sm3mat_material_list_index",
            rows=12,
        )
        refresh = matbox.row(align=True)
        refresh.scale_y = 1.2
        refresh.operator(SM3_OT_Update_Material_List.bl_idname, text="Update Material List", icon="FILE_REFRESH")
        selrow = matbox.row(align=True)
        selrow.operator(SM3_OT_Material_List_Select_All.bl_idname, text="Select All")
        selrow.operator(SM3_OT_Material_List_Select_None.bl_idname, text="Select None")
        matbox.label(text="S# = SM3 material slot; duplicate hashes stay separate")

        box = layout.box(); box.label(text="REAL SM3 Material Database", icon="MATERIAL")
        box.label(text="0xXXXXXXXX = REAL MAT hash")
        box.label(text="SM3_REF_XXXXXXXX = unresolved local ref")
        box.operator(SM3_OT_Material_Database_Status.bl_idname, text="Material Database Status", icon="INFO")
        box.operator(SM3_OT_Resolve_Materials_From_Database.bl_idname, text="Resolve Selected -> REAL MAT", icon="FILE_REFRESH")
        box.operator(SM3_OT_Texture_Research_Report.bl_idname, text="Build Selected MAT -> TEX Report", icon="TEXT")
        box.operator(SM3_OT_Convert_Legacy_Material_Names.bl_idname, text="Upgrade Old Local-Ref Names", icon="FILE_REFRESH")
        box.operator(SM3_OT_Material_Combiner.bl_idname, text="Combine Duplicate REAL MAT Hashes", icon="MATERIAL")

        tex = layout.box(); tex.label(text="Resolved DDS / MAT -> TEX", icon="TEXTURE")
        tex.prop(s, "sm3mat_texture_folder", text="DDS Folder")
        tex.operator(SM3_OT_Load_Resolved_DDS_Textures.bl_idname, text="Load Matching DDS", icon="IMAGE_DATA")
        tex.label(text="Drag DDS/TEX into 3D View for NativeTEX conversion")

        atlas = layout.box(); atlas.label(text="Material Atlas", icon="UV")
        atlas.prop(s, "sm3mat_atlas_name", text="Atlas Name")
        atlas.prop(s, "sm3mat_atlas_padding", text="Padding")
        atlas.prop(s, "sm3mat_atlas_color_tile_size", text="Color Tile")
        atlas.operator(SM3_OT_Create_Material_Atlas.bl_idname, text="Create SLOT-SAFE Atlas + Remap UVs + DDS", icon="IMAGE_DATA")
        atlas.label(text="Reads each S# image separately; keeps S# + MAT hashes")


classes = (
    SM3MAT_MaterialListEntry,
    SM3MAT_UL_MaterialsToCombine,
    SM3_OT_Update_Material_List,
    SM3_OT_Material_List_Select_All,
    SM3_OT_Material_List_Select_None,
    SM3_OT_Convert_Legacy_Material_Names,
    SM3_OT_Material_Combiner,
    SM3_OT_Material_Database_Status,
    SM3_OT_Resolve_Materials_From_Database,
    SM3_OT_Texture_Research_Report,
    SM3_OT_Load_Resolved_DDS_Textures,
    SM3_OT_Create_Material_Atlas,
    SM3_OT_Texture_DragDrop_Convert,
    SM3_Texture_FileHandler,
    SM3MAT_PT_Main,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.sm3mat_material_list = CollectionProperty(type=SM3MAT_MaterialListEntry)
    bpy.types.Scene.sm3mat_material_list_index = IntProperty(name="Material List Index", default=0, min=0)
    bpy.types.Scene.sm3mat_list_selected_only = BoolProperty(
        name="Selected Objects Only",
        default=False,
        description="When updating the Materials to Combine list, scan selected mesh objects only",
    )
    bpy.types.Scene.sm3mat_texture_folder = StringProperty(
        name="Resolved DDS Folder",
        description="Folder searched recursively for DDS files matching REAL TEX hash/name",
        subtype="DIR_PATH", default="",
    )
    bpy.types.Scene.sm3mat_atlas_name = StringProperty(name="Atlas Name", default="SM3_Player_Atlas")
    bpy.types.Scene.sm3mat_atlas_padding = IntProperty(name="Atlas Padding", default=8, min=0, max=128)
    bpy.types.Scene.sm3mat_atlas_color_tile_size = IntProperty(name="Color Material Tile Size", default=32, min=1, max=1024)


def unregister():
    for attr in (
        "sm3mat_material_list",
        "sm3mat_material_list_index",
        "sm3mat_list_selected_only",
        "sm3mat_texture_folder",
        "sm3mat_atlas_name",
        "sm3mat_atlas_padding",
        "sm3mat_atlas_color_tile_size",
    ):
        if hasattr(bpy.types.Scene, attr): delattr(bpy.types.Scene, attr)
    for cls in reversed(classes):
        try: bpy.utils.unregister_class(cls)
        except Exception: pass


if __name__ == "__main__":
    register()
