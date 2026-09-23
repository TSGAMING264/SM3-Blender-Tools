from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional, Sequence

import bpy

from .sm3_format import SM3Mesh, SM3MeshSection, VertexDecl, choose_position_divisors, parse_mesh

CACHE_PROP = "sm3_target_template_snapshot_v1"
DIVISOR_PROP = "sm3_target_position_divisors_v1"
CACHE_VERSION = 1

PLAYER000_HASH = 0xAC92103D
PLAYER001_HASH = 0xAC92103E


def bundled_template_path_for_hash(mesh_hash: int) -> str:
    """Return a stock template bundled with Lite when one exists.

    v0.8 copies the important WoS exporter rule: exporting replacement geometry
    must not depend on re-importing the source model after every add-on update.
    The two player MESH templates are bundled because they are the current WoS
    conversion targets and are small enough to keep Lite fast.
    """
    h = int(mesh_hash) & 0xFFFFFFFF
    names = {
        PLAYER000_HASH: "0xAC92103D.ch_spiderman000.mesh",
        PLAYER001_HASH: "0xAC92103E.ch_spiderman001.mesh",
    }
    filename = names.get(h)
    if not filename:
        return ""
    path = Path(__file__).resolve().parent / "samples" / "CH_SPIDERMAN" / filename
    return str(path) if path.is_file() else ""


def _template_vault_dir() -> Path:
    """Persistent per-user template vault that survives add-on replacement."""
    try:
        root = bpy.utils.user_resource(
            "CONFIG", path="sm3_blender_toolkit_lite/templates", create=True
        )
        if root:
            path = Path(root)
            path.mkdir(parents=True, exist_ok=True)
            return path
    except Exception:
        pass
    # Fallback is outside the add-on folder so replacing the extension does not
    # delete the user's learned templates.
    path = Path.home() / ".sm3_blender_toolkit_lite" / "templates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def register_template_file(filepath: str, mesh_hash: int = 0, resource_name: str = "") -> str:
    """Copy an imported original SM3 MESH into the persistent template vault.

    This is intentionally a COPY. It never edits or moves the user's source file.
    Once a target has been imported once, later add-on updates can resolve its
    export schema without asking for that model again.
    """
    if not filepath or not os.path.isfile(filepath):
        return ""
    try:
        parsed = parse_mesh(filepath)
        h = int(mesh_hash or parsed.filename_hash) & 0xFFFFFFFF
        if h != int(parsed.filename_hash) & 0xFFFFFFFF:
            return ""
        # v0.8.2: player 000/001 have immutable bundled templates. Never learn
        # them from an arbitrary source/export path into the persistent vault.
        # This prevents a previous replacement export from becoming the schema
        # template for every future export.
        if h in (PLAYER000_HASH, PLAYER001_HASH):
            return bundled_template_path_for_hash(h)
        clean = str(resource_name or Path(filepath).stem).strip()
        # Keep names portable and filesystem-safe.
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in clean)[:80]
        if not safe:
            safe = f"mesh_{h:08X}"
        dst = _template_vault_dir() / f"0x{h:08X}.{safe}.mesh"
        src_bytes = Path(filepath).read_bytes()
        if not dst.is_file() or dst.read_bytes() != src_bytes:
            dst.write_bytes(src_bytes)
        return str(dst)
    except Exception:
        return ""


def vault_template_path_for_hash(mesh_hash: int) -> str:
    h = int(mesh_hash) & 0xFFFFFFFF
    try:
        matches = sorted(_template_vault_dir().glob(f"0x{h:08X}.*.mesh"))
    except Exception:
        return ""
    for path in matches:
        try:
            if parse_mesh(str(path)).filename_hash == h:
                return str(path)
        except Exception:
            continue
    return ""


def fallback_template_path_for_hash(mesh_hash: int) -> str:
    """Return a safe export template.

    v0.8.2 safety rule: the two player targets ALWAYS use the pristine template
    shipped inside the add-on. A persistent vault entry may have been created by
    an older build after the user accidentally exported over a sample/template
    path, so player templates must never be sourced from the vault. Other game
    models still use the persistent vault first so they survive add-on updates.
    """
    h = int(mesh_hash) & 0xFFFFFFFF
    if h in (PLAYER000_HASH, PLAYER001_HASH):
        return bundled_template_path_for_hash(h)
    return vault_template_path_for_hash(h) or bundled_template_path_for_hash(h)


def _hash_from_target_or_objects(target, mesh_objects=()) -> int:
    if target is not None:
        h = _u32(target.get("sm3_mesh_hash"), 0)
        if h:
            return h
        snap = cached_snapshot_text(target)
        if snap:
            try:
                h = int(json.loads(snap).get("filename_hash", 0)) & 0xFFFFFFFF
                if h:
                    return h
            except Exception:
                pass
    for obj in mesh_objects or ():
        if obj is None:
            continue
        data = getattr(obj, "data", None)
        for key in ("sm3_export_target_mesh_hash", "sm3_weight_source_mesh_hash", "sm3_mesh_hash"):
            h = _u32(obj.get(key), 0)
            if not h and data is not None:
                h = _u32(data.get(key), 0)
            if h:
                return h
        snap = cached_snapshot_text(obj) or (cached_snapshot_text(data) if data is not None else "")
        if snap:
            try:
                h = int(json.loads(snap).get("filename_hash", 0)) & 0xFFFFFFFF
                if h:
                    return h
            except Exception:
                pass
    return 0


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


def _snapshot_dict(mesh: SM3Mesh) -> dict:
    return {
        "version": CACHE_VERSION,
        "filename_pointer_serialized": int(mesh.filename_pointer_serialized),
        "filename_hash": int(mesh.filename_hash) & 0xFFFFFFFF,
        "parsed_flags": int(mesh.parsed_flags),
        "section_count": int(mesh.section_count),
        "section_table_pointer_serialized": int(mesh.section_table_pointer_serialized),
        "skeleton_ref_serialized": int(mesh.skeleton_ref_serialized),
        "external_mesh_count": int(mesh.external_mesh_count),
        "external_mesh_table_pointer_serialized": int(mesh.external_mesh_table_pointer_serialized),
        "model_offset": list(mesh.model_offset),
        "sphere_radius": float(mesh.sphere_radius),
        "model_bbox": list(mesh.model_bbox),
        "img_size": int(mesh.img_size),
        "phys_size": int(mesh.phys_size),
        "header_40_50_hex": bytes(mesh.raw[0x40:0x50]).hex() if len(mesh.raw) >= 0x50 else "",
        "sections": [
            {
                "index": int(s.index),
                "info_offset": int(s.info_offset),
                "mesh_offset": list(s.mesh_offset),
                "sphere_radius": float(s.sphere_radius),
                "mesh_bbox": list(s.mesh_bbox),
                "material_ref_serialized": int(s.material_ref_serialized) & 0xFFFFFFFF,
                "bone_palette_pointer_serialized": int(s.bone_palette_pointer_serialized),
                "bone_palette": [int(x) for x in s.bone_palette],
                "vertex_buffer_pointer_serialized": int(s.vertex_buffer_pointer_serialized),
                "unknown_30": int(s.unknown_30),
                "vertex_count": int(s.vertex_count),
                "unknown_38": int(s.unknown_38),
                "index_buffer_pointer_serialized": int(s.index_buffer_pointer_serialized),
                "unknown_40": int(s.unknown_40),
                "index_count": int(s.index_count),
                "index_size": int(s.index_size),
                "schema_pointer_serialized": int(s.schema_pointer_serialized),
                "primitive_type": int(s.primitive_type),
                "primitive_unknown": int(s.primitive_unknown),
                "vertex_stride": int(s.vertex_stride),
                "vertex_schema_pointer_serialized": int(s.vertex_schema_pointer_serialized),
                "schema_unknown": int(s.schema_unknown),
                "decl": [
                    [int(d.stream), int(d.offset), int(d.dtype), int(d.method), int(d.usage), int(d.usage_index)]
                    for d in s.decl
                ],
                "vertex_buffer_offset": int(s.vertex_buffer_offset),
                "index_buffer_offset": int(s.index_buffer_offset),
            }
            for s in mesh.sections
        ],
    }


def cache_target_template(idblock, mesh: SM3Mesh) -> str:
    """Store a compact export-template snapshot in the .blend itself.

    This survives add-on upgrades and does not require the original target to be
    re-imported. It stores target identity/schema/material pointers, not the
    original target geometry buffers.
    """
    text = json.dumps(_snapshot_dict(mesh), separators=(",", ":"))
    idblock[CACHE_PROP] = text
    try:
        divisors = choose_position_divisors(mesh)
    except Exception:
        divisors = []
    if divisors:
        idblock[DIVISOR_PROP] = ",".join(f"{float(v):.9g}" for v in divisors)
    idblock["sm3_target_cache_version"] = CACHE_VERSION
    idblock["sm3_target_cache_ready"] = True
    return text


def cached_snapshot_text(idblock) -> str:
    if idblock is None:
        return ""
    return str(idblock.get(CACHE_PROP, "") or "")


def cached_position_divisors(idblock, expected_count: int = 0):
    if idblock is None:
        return None
    raw = str(idblock.get(DIVISOR_PROP, "") or "")
    if not raw:
        # Backward compatibility: import already stored a compact divisor list.
        raw = str(idblock.get("sm3_position_divisors", "") or "")
        if raw and ":" in raw:
            vals = []
            try:
                for item in raw.split(","):
                    _idx, value = item.split(":", 1)
                    vals.append(float(value))
                if not expected_count or len(vals) == expected_count:
                    return vals
            except Exception:
                return None
    if not raw:
        return None
    try:
        vals = [float(x) for x in raw.split(",") if x.strip()]
    except Exception:
        return None
    if expected_count and len(vals) != expected_count:
        return None
    return vals


def mesh_from_snapshot(text: str, *, path: str = "<SM3_TARGET_CACHE>") -> SM3Mesh:
    data = json.loads(text)
    if int(data.get("version", 0)) != CACHE_VERSION:
        raise ValueError("Unsupported SM3 target cache version")
    sections = []
    for sd in data.get("sections", []):
        decl = [VertexDecl(*[int(v) for v in row]) for row in sd.get("decl", [])]
        sections.append(SM3MeshSection(
            index=int(sd["index"]),
            info_offset=int(sd["info_offset"]),
            mesh_offset=tuple(float(x) for x in sd.get("mesh_offset", (0,0,0))),
            sphere_radius=float(sd.get("sphere_radius", 0.0)),
            mesh_bbox=tuple(float(x) for x in sd.get("mesh_bbox", (0,0,0,0))),
            material_ref_serialized=int(sd.get("material_ref_serialized", 0)),
            bone_palette_pointer_serialized=int(sd.get("bone_palette_pointer_serialized", 0)),
            bone_palette=[int(x) for x in sd.get("bone_palette", [])],
            vertex_buffer_pointer_serialized=int(sd.get("vertex_buffer_pointer_serialized", 0)),
            unknown_30=int(sd.get("unknown_30", 0)),
            vertex_count=int(sd.get("vertex_count", 0)),
            unknown_38=int(sd.get("unknown_38", 0)),
            index_buffer_pointer_serialized=int(sd.get("index_buffer_pointer_serialized", 0)),
            unknown_40=int(sd.get("unknown_40", 0)),
            index_count=int(sd.get("index_count", 0)),
            index_size=int(sd.get("index_size", 2)),
            schema_pointer_serialized=int(sd.get("schema_pointer_serialized", 0)),
            primitive_type=int(sd.get("primitive_type", 4)),
            primitive_unknown=int(sd.get("primitive_unknown", 1)),
            vertex_stride=int(sd.get("vertex_stride", 52)),
            vertex_schema_pointer_serialized=int(sd.get("vertex_schema_pointer_serialized", 0)),
            schema_unknown=int(sd.get("schema_unknown", 0)),
            decl=decl,
            vertex_buffer_offset=int(sd.get("vertex_buffer_offset", 0)),
            index_buffer_offset=int(sd.get("index_buffer_offset", 0)),
        ))
    raw = bytearray(0x50)
    h = str(data.get("header_40_50_hex", "") or "")
    if h:
        try:
            b = bytes.fromhex(h)
            raw[0x40:0x40 + min(16, len(b))] = b[:16]
        except Exception:
            pass
    return SM3Mesh(
        path=str(path),
        filename_pointer_serialized=int(data.get("filename_pointer_serialized", 0)),
        filename_hash=int(data.get("filename_hash", 0)) & 0xFFFFFFFF,
        parsed_flags=int(data.get("parsed_flags", 0)),
        section_count=int(data.get("section_count", len(sections))),
        section_table_pointer_serialized=int(data.get("section_table_pointer_serialized", 0)),
        skeleton_ref_serialized=int(data.get("skeleton_ref_serialized", 0)),
        external_mesh_count=int(data.get("external_mesh_count", 0)),
        external_mesh_table_pointer_serialized=int(data.get("external_mesh_table_pointer_serialized", 0)),
        model_offset=tuple(float(x) for x in data.get("model_offset", (0,0,0))),
        sphere_radius=float(data.get("sphere_radius", 0.0)),
        model_bbox=tuple(float(x) for x in data.get("model_bbox", (0,0,0,0))),
        sections=sections,
        img_size=int(data.get("img_size", 0)),
        phys_size=int(data.get("phys_size", 0)),
        raw=bytes(raw),
    )


def ensure_target_cache(target, mesh_objects: Sequence[bpy.types.Object] = ()):  # returns snapshot text or ""
    if target is None:
        return ""
    existing = cached_snapshot_text(target)
    if existing:
        return existing

    # 1) Original source path, when it still exists.
    source = str(target.get("sm3_source_mesh", "") or "")
    if source and os.path.isfile(source):
        mesh = parse_mesh(source)
        text = cache_target_template(target, mesh)
        register_template_file(source, mesh.filename_hash, str(target.get("sm3_resource_name", "") or ""))
        for obj in mesh_objects or ():
            if obj is not None and obj.type == "MESH":
                obj[CACHE_PROP] = text
                if getattr(obj, "data", None) is not None:
                    obj.data[CACHE_PROP] = text
                if target.get(DIVISOR_PROP):
                    obj[DIVISOR_PROP] = target.get(DIVISOR_PROP)
                    if getattr(obj, "data", None) is not None:
                        obj.data[DIVISOR_PROP] = target.get(DIVISOR_PROP)
        return text

    # 2) A snapshot already carried by the replacement object/data.
    for obj in mesh_objects or ():
        text = cached_snapshot_text(obj)
        if not text and getattr(obj, "data", None) is not None:
            text = cached_snapshot_text(obj.data)
        if text:
            target[CACHE_PROP] = text
            div = obj.get(DIVISOR_PROP)
            if not div and getattr(obj, "data", None) is not None:
                div = obj.data.get(DIVISOR_PROP)
            if div:
                target[DIVISOR_PROP] = div
            target["sm3_target_cache_ready"] = True
            return text

    # 3) v0.8: persistent user vault / bundled player templates. This is the
    # path that finally removes the re-import-after-update dependency.
    mesh_hash = _hash_from_target_or_objects(target, mesh_objects)
    fallback = fallback_template_path_for_hash(mesh_hash) if mesh_hash else ""
    if fallback:
        mesh = parse_mesh(fallback)
        text = cache_target_template(target, mesh)
        if not target.get("sm3_mesh_hash"):
            target["sm3_mesh_hash"] = f"0x{mesh.filename_hash:08X}"
        if not target.get("sm3_source_basename"):
            target["sm3_source_basename"] = Path(fallback).name
        target["sm3_template_fallback_path"] = fallback
        target["sm3_template_fallback_mode"] = (
            "USER_TEMPLATE_VAULT" if vault_template_path_for_hash(mesh_hash) else "BUNDLED_PLAYER_TEMPLATE"
        )
        return text
    return ""


def load_target_template(target, mesh_objects: Sequence[bpy.types.Object] = ()) -> SM3Mesh:
    """Load export template without requiring an imported target collection.

    Priority mirrors WoS's export independence as closely as SM3's format allows:
      original path -> .blend snapshot -> replacement snapshot -> persistent vault
      -> bundled player template.
    """
    source = str(target.get("sm3_source_mesh", "") or "") if target is not None else ""
    if source and os.path.isfile(source):
        mesh = parse_mesh(source)
        text = cache_target_template(target, mesh)
        register_template_file(source, mesh.filename_hash, str(target.get("sm3_resource_name", "") or ""))
        for obj in mesh_objects or ():
            if obj is not None and obj.type == "MESH":
                obj[CACHE_PROP] = text
                if getattr(obj, "data", None) is not None:
                    obj.data[CACHE_PROP] = text
                if target.get(DIVISOR_PROP):
                    obj[DIVISOR_PROP] = target.get(DIVISOR_PROP)
                    if getattr(obj, "data", None) is not None:
                        obj.data[DIVISOR_PROP] = target.get(DIVISOR_PROP)
        return mesh

    text = cached_snapshot_text(target)
    if not text:
        for obj in mesh_objects or ():
            text = cached_snapshot_text(obj)
            if not text and getattr(obj, "data", None) is not None:
                text = cached_snapshot_text(obj.data)
            if text:
                break
    if text:
        return mesh_from_snapshot(text, path=source or "<SM3_TARGET_CACHE_IN_BLEND>")

    mesh_hash = _hash_from_target_or_objects(target, mesh_objects)
    fallback = fallback_template_path_for_hash(mesh_hash) if mesh_hash else ""
    if fallback:
        mesh = parse_mesh(fallback)
        if target is not None:
            cache_target_template(target, mesh)
            target["sm3_template_fallback_path"] = fallback
            target["sm3_template_fallback_mode"] = (
                "USER_TEMPLATE_VAULT" if vault_template_path_for_hash(mesh_hash) else "BUNDLED_PLAYER_TEMPLATE"
            )
        for obj in mesh_objects or ():
            if obj is not None and obj.type == "MESH":
                snap = cache_target_template(obj, mesh)
                if getattr(obj, "data", None) is not None:
                    obj.data[CACHE_PROP] = snap
        return mesh

    raise ValueError(
        "No export template is available for this target hash. v0.8 no longer requires an imported target collection: "
        "player 000/001 templates are bundled, and any other SM3 target imported once is copied to the persistent "
        "user template vault for future updates."
    )


def stamp_target_cache_to_objects(target, mesh_objects: Iterable[bpy.types.Object]):
    objects = [o for o in mesh_objects if o is not None and o.type == "MESH"]
    text = ensure_target_cache(target, objects)
    if not text:
        return
    div = target.get(DIVISOR_PROP) if target is not None else None
    for obj in objects:
        obj[CACHE_PROP] = text
        if getattr(obj, "data", None) is not None:
            obj.data[CACHE_PROP] = text
        if div:
            obj[DIVISOR_PROP] = div
            if getattr(obj, "data", None) is not None:
                obj.data[DIVISOR_PROP] = div
        obj["sm3_target_cache_ready"] = True
        if getattr(obj, "data", None) is not None:
            obj.data["sm3_target_cache_ready"] = True


def target_has_template(target, mesh_objects: Sequence[bpy.types.Object] = ()) -> bool:
    if target is None:
        return False
    source = str(target.get("sm3_source_mesh", "") or "")
    if source and os.path.isfile(source):
        return True
    if cached_snapshot_text(target):
        return True
    for obj in mesh_objects or ():
        if cached_snapshot_text(obj):
            return True
        data = getattr(obj, "data", None)
        if data is not None and cached_snapshot_text(data):
            return True
    h = _hash_from_target_or_objects(target, mesh_objects)
    return bool(h and fallback_template_path_for_hash(h))


def stamp_target_cache_to_collection(source_target, owner_collection):
    """Copy target identity/schema onto the REPLACEMENT collection.

    This is the real WoS-style ownership model introduced in v0.8. The
    replacement collection survives add-on updates and can export without the
    original imported target collection remaining in the scene.
    """
    if source_target is None or owner_collection is None:
        return ""
    mesh = load_target_template(source_target)
    text = cache_target_template(owner_collection, mesh)
    source = str(source_target.get("sm3_source_mesh", "") or "")
    resource = str(source_target.get("sm3_resource_name", source_target.name) or source_target.name)
    basename = str(source_target.get("sm3_source_basename", "") or "")
    if not basename:
        basename = Path(source).name if source else f"0x{mesh.filename_hash:08X}.{resource}.mesh"
    owner_collection["sm3_export_owner"] = True
    owner_collection["sm3_mesh_hash"] = f"0x{mesh.filename_hash:08X}"
    owner_collection["sm3_resource_name"] = resource
    owner_collection["sm3_source_basename"] = basename
    owner_collection["sm3_source_mesh"] = source
    owner_collection["sm3_export_target_source_mesh"] = source
    owner_collection["sm3_export_target_resource_name"] = resource
    owner_collection["sm3_export_target_source_basename"] = basename
    owner_collection["sm3_toolkit_version"] = "0.8.0-lite"
    if source_target.get(DIVISOR_PROP):
        owner_collection[DIVISOR_PROP] = source_target.get(DIVISOR_PROP)
    return text


def _object_stamp(obj):
    data = getattr(obj, "data", None)

    def get_any(key, default=""):
        v = obj.get(key)
        if v in (None, "") and data is not None:
            v = data.get(key)
        return default if v in (None, "") else v

    h = 0
    for key in ("sm3_export_target_mesh_hash", "sm3_weight_source_mesh_hash", "sm3_mesh_hash"):
        h = _u32(get_any(key, 0), 0)
        if h:
            break
    source = str(get_any("sm3_export_target_source_mesh", get_any("sm3_source_mesh", "")) or "")
    resource = str(get_any("sm3_export_target_resource_name", get_any("sm3_resource_name", "")) or "")
    basename = str(get_any("sm3_export_target_source_basename", "") or "")
    snapshot = cached_snapshot_text(obj)
    if not snapshot and data is not None:
        snapshot = cached_snapshot_text(data)
    if snapshot and not h:
        try:
            h = int(json.loads(snapshot).get("filename_hash", 0)) & 0xFFFFFFFF
        except Exception:
            pass
    if snapshot and not resource:
        resource = f"0x{h:08X}"
    if not basename:
        basename = Path(source).name if source else (f"{resource}.mesh" if resource else f"0x{h:08X}.mesh")
    return source, h, resource, basename, snapshot


def recover_target_collection_from_objects(mesh_objects: Sequence[bpy.types.Object], scene=None):
    """Rebuild target ownership from the replacement mesh's persistent stamps.

    This is the v0.7.4 upgrade fix: replacing/updating the add-on clears Blender
    PointerProperty UI selections, but custom properties on the mesh survive.
    We reconstruct the target collection automatically instead of forcing a
    re-import or a second weight transfer.
    """
    stamps = [_object_stamp(o) for o in mesh_objects if o is not None and o.type == "MESH"]
    stamps = [s for s in stamps if s[1] or s[4] or s[0]]
    if not stamps:
        return None
    hashes = {s[1] for s in stamps if s[1]}
    if len(hashes) > 1:
        raise ValueError("Selected replacement meshes contain different saved SM3 target hashes")
    mesh_hash = next(iter(hashes), 0)
    sources = {os.path.abspath(s[0]) for s in stamps if s[0]}
    if len(sources) > 1:
        raise ValueError("Selected replacement meshes contain different saved SM3 target source paths")
    source = next(iter(sources), "")
    snapshots = [s[4] for s in stamps if s[4]]
    snapshot = snapshots[0] if snapshots else ""
    if snapshot and not mesh_hash:
        try: mesh_hash = int(json.loads(snapshot).get("filename_hash", 0)) & 0xFFFFFFFF
        except Exception: pass
    if not mesh_hash:
        return None
    resource = next((s[2] for s in stamps if s[2]), f"0x{mesh_hash:08X}")
    basename = next((s[3] for s in stamps if s[3]), f"{resource}.mesh")

    # Reuse any existing imported/recovered target with this hash and matching source.
    for col in bpy.data.collections:
        ch = _u32(col.get("sm3_mesh_hash"), 0)
        if ch != mesh_hash:
            continue
        csource = str(col.get("sm3_source_mesh", "") or "")
        if source and csource and os.path.abspath(csource) != source:
            continue
        if bool(col.get("sm3_is_export_target", False)):
            if snapshot and not cached_snapshot_text(col):
                col[CACHE_PROP] = snapshot
            return col

    base_name = f"SM3_TargetCache_0x{mesh_hash:08X}_{resource}"[:63]
    name = base_name
    i = 1
    while bpy.data.collections.get(name) is not None:
        i += 1
        name = f"{base_name}_{i}"[:63]
    col = bpy.data.collections.new(name)
    if scene is not None:
        try:
            scene.collection.children.link(col)
        except Exception:
            pass
    col["sm3_is_export_target"] = True
    col["sm3_target_cache_recovered"] = True
    col["sm3_toolkit_version"] = "0.7.5-lite"
    col["sm3_source_mesh"] = source
    col["sm3_source_basename"] = basename
    col["sm3_resource_name"] = resource
    col["sm3_mesh_hash"] = f"0x{mesh_hash:08X}"
    if snapshot:
        col[CACHE_PROP] = snapshot
        col["sm3_target_cache_ready"] = True
    elif source and os.path.isfile(source):
        try:
            cache_target_template(col, parse_mesh(source))
        except Exception:
            pass
    for obj in mesh_objects:
        if obj is None or obj.type != "MESH":
            continue
        if obj.name not in col.objects:
            col.objects.link(obj)
        obj["sm3_export_target_collection"] = col.name
        obj["sm3_export_target_mesh_hash"] = f"0x{mesh_hash:08X}"
        obj["sm3_export_target_source_mesh"] = source
        obj["sm3_export_target_source_basename"] = basename
        obj["sm3_export_target_resource_name"] = resource
        obj["sm3_export_target_locked"] = True
        if getattr(obj, "data", None) is not None:
            obj.data["sm3_export_target_collection"] = col.name
            obj.data["sm3_export_target_mesh_hash"] = f"0x{mesh_hash:08X}"
            obj.data["sm3_export_target_source_mesh"] = source
            obj.data["sm3_export_target_source_basename"] = basename
            obj.data["sm3_export_target_resource_name"] = resource
            obj.data["sm3_export_target_locked"] = True
        if cached_snapshot_text(col):
            obj[CACHE_PROP] = cached_snapshot_text(col)
            if getattr(obj, "data", None) is not None:
                obj.data[CACHE_PROP] = cached_snapshot_text(col)
            if col.get(DIVISOR_PROP):
                obj[DIVISOR_PROP] = col.get(DIVISOR_PROP)
                if getattr(obj, "data", None) is not None:
                    obj.data[DIVISOR_PROP] = col.get(DIVISOR_PROP)
    return col
