from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import math
import re
import os
import struct
from typing import Dict, List, Optional, Sequence, Tuple, Set

import bpy
import numpy as np


@dataclass
class AtlasEntry:
    object_ptr: int
    object_name: str
    slot_index: int
    material: bpy.types.Material
    image: Optional[bpy.types.Image]
    source_size: Tuple[int, int]
    uv_origin: Tuple[int, int]
    uv_repeat: Tuple[int, int]
    content_size: Tuple[int, int]
    face_count: int = 0
    rect_xy: Tuple[int, int] = (0, 0)
    padded_size: Tuple[int, int] = (0, 0)


def _next_pow2(value: int) -> int:
    value = max(1, int(value))
    return 1 << (value - 1).bit_length()


def _find_image_upstream(socket, visited=None) -> Optional[bpy.types.Image]:
    """Trace a shader input upstream and return the first real Image Texture.

    The reference Material Combiner works from the image actually feeding the
    material, not from a global/first material image.  This tracer handles a
    direct Image Texture -> Base Color link and simple nodes inserted between
    the image and Principled BSDF.
    """
    if socket is None:
        return None
    if visited is None:
        visited = set()
    for link in getattr(socket, "links", ()):
        node = getattr(link, "from_node", None)
        if node is None:
            continue
        ptr = node.as_pointer() if hasattr(node, "as_pointer") else id(node)
        if ptr in visited:
            continue
        visited.add(ptr)
        if node.bl_idname == "ShaderNodeTexImage" and node.image:
            return node.image
        for input_socket in getattr(node, "inputs", ()):
            image = _find_image_upstream(input_socket, visited)
            if image is not None:
                return image
    return None


def _find_material_image(mat: bpy.types.Material) -> Optional[bpy.types.Image]:
    if mat is None or not mat.use_nodes or not mat.node_tree:
        return None

    nodes = mat.node_tree.nodes
    # First use the image that actually feeds Principled Base Color.
    for node in nodes:
        if node.bl_idname != "ShaderNodeBsdfPrincipled":
            continue
        base = node.inputs.get("Base Color")
        image = _find_image_upstream(base)
        if image is not None:
            return image

    # Fallback only when there is no connected image path.
    for node in nodes:
        if node.bl_idname == "ShaderNodeTexImage" and node.image:
            return node.image
    return None


# Public alias used by the UI/material-list diagnostics.
find_material_image = _find_material_image


def _image_pixels_rgba(image: bpy.types.Image) -> np.ndarray:
    width, height = int(image.size[0]), int(image.size[1])
    if width <= 0 or height <= 0:
        raise ValueError(f"Image '{image.name}' has invalid size {width}x{height}")
    arr = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(arr)
    arr = arr.reshape((height, width, 4))
    return np.clip(arr, 0.0, 1.0)


def _material_color_rgba(mat: bpy.types.Material) -> Tuple[float, float, float, float]:
    try:
        col = tuple(float(x) for x in mat.diffuse_color)
        if len(col) >= 4:
            return col[:4]
    except Exception:
        pass
    return (1.0, 1.0, 1.0, 1.0)


def _active_uv_layer(obj: bpy.types.Object):
    if obj.type != "MESH" or not obj.data.uv_layers:
        return None
    return obj.data.uv_layers.active or obj.data.uv_layers[0]


def _used_material_indices(obj: bpy.types.Object) -> List[int]:
    return sorted({int(poly.material_index) for poly in obj.data.polygons if int(poly.material_index) < len(obj.material_slots)})


def _collect_slot_entries(
    objects: Sequence[bpy.types.Object],
    slot_selection: Optional[Dict[int, Set[int]]],
    color_tile_size: int,
) -> List[AtlasEntry]:
    """Collect atlas inputs by OBJECT + SLOT, never by material identity.

    This is the critical SM3 fix.  Duplicate hashes/material datablocks may be
    valid separate game sections, so merging by material pointer before atlas
    construction can silently discard a checked slot or its image.
    """
    entries: List[AtlasEntry] = []

    for obj in objects:
        if obj.type != "MESH":
            continue
        obj_ptr = obj.as_pointer()
        selected_slots = None if slot_selection is None else set(slot_selection.get(obj_ptr, set()))
        if slot_selection is not None and not selected_slots:
            continue

        uv_layer = _active_uv_layer(obj)
        if uv_layer is None:
            raise ValueError(f"{obj.name}: no UV map. Atlas combine requires an active UV map.")
        uv_data = uv_layer.data

        if selected_slots is None:
            candidate_slots = _used_material_indices(obj)
        else:
            # Every checked slot is an atlas source.  Even a zero-face slot is
            # kept in the atlas/report so the tool never silently drops an
            # explicitly selected image during diagnostic/porting work.
            candidate_slots = sorted(i for i in selected_slots if 0 <= i < len(obj.material_slots))

        for slot_index in candidate_slots:
            mat = obj.material_slots[slot_index].material
            if mat is None:
                continue

            min_u = min_v = float("inf")
            max_u = max_v = float("-inf")
            face_count = 0
            for poly in obj.data.polygons:
                if int(poly.material_index) != int(slot_index):
                    continue
                face_count += 1
                for li in poly.loop_indices:
                    uv = uv_data[li].uv
                    u, v = float(uv.x), float(uv.y)
                    if math.isfinite(u) and math.isfinite(v):
                        min_u = min(min_u, u)
                        min_v = min(min_v, v)
                        max_u = max(max_u, u)
                        max_v = max(max_v, v)

            if not all(math.isfinite(x) for x in (min_u, min_v, max_u, max_v)):
                min_u = min_v = 0.0
                max_u = max_v = 1.0

            # Preserve repeating UVs like the reference Material Combiner.
            u0 = math.floor(min(min_u, 0.0))
            v0 = math.floor(min(min_v, 0.0))
            u1 = math.ceil(max(max_u, 1.0))
            v1 = math.ceil(max(max_v, 1.0))
            repeat_x = max(1, int(u1 - u0))
            repeat_y = max(1, int(v1 - v0))

            image = _find_material_image(mat)
            if image is not None:
                sw, sh = int(image.size[0]), int(image.size[1])
            else:
                sw = sh = max(1, int(color_tile_size))

            entries.append(AtlasEntry(
                object_ptr=obj_ptr,
                object_name=obj.name,
                slot_index=int(slot_index),
                material=mat,
                image=image,
                source_size=(sw, sh),
                uv_origin=(int(u0), int(v0)),
                uv_repeat=(repeat_x, repeat_y),
                content_size=(sw * repeat_x, sh * repeat_y),
                face_count=face_count,
            ))

    return entries


def _pack_shelves(entries: List[AtlasEntry], padding: int) -> Tuple[int, int]:
    if not entries:
        raise ValueError("No material entries to atlas")

    pad2 = int(padding) * 2
    total_area = sum((e.content_size[0] + pad2) * (e.content_size[1] + pad2) for e in entries)
    max_w = max(e.content_size[0] + pad2 for e in entries)
    target_w = _next_pow2(max(max_w, int(math.sqrt(total_area))))

    # Large textures may create tall atlases; shelf packing is deterministic and
    # intentionally simple for the small number of player materials we expect.
    x = y = row_h = 0
    used_w = used_h = 0
    for entry in sorted(entries, key=lambda e: (e.content_size[1], e.content_size[0]), reverse=True):
        rw = entry.content_size[0] + pad2
        rh = entry.content_size[1] + pad2
        if x and x + rw > target_w:
            y += row_h
            x = 0
            row_h = 0
        entry.rect_xy = (x, y)
        entry.padded_size = (rw, rh)
        x += rw
        row_h = max(row_h, rh)
        used_w = max(used_w, x)
        used_h = max(used_h, y + row_h)

    return _next_pow2(used_w), _next_pow2(used_h)


def _build_atlas_pixels(entries: Sequence[AtlasEntry], atlas_size: Tuple[int, int], padding: int) -> np.ndarray:
    atlas_w, atlas_h = atlas_size
    atlas = np.zeros((atlas_h, atlas_w, 4), dtype=np.float32)
    atlas[:, :, 3] = 0.0

    for entry in entries:
        sw, sh = entry.source_size
        rx, ry = entry.uv_repeat
        if entry.image is not None:
            source = _image_pixels_rgba(entry.image)
        else:
            source = np.empty((sh, sw, 4), dtype=np.float32)
            source[:, :, :] = np.asarray(_material_color_rgba(entry.material), dtype=np.float32)

        if rx > 1 or ry > 1:
            content = np.tile(source, (ry, rx, 1))
        else:
            content = source

        if padding > 0:
            padded = np.pad(content, ((padding, padding), (padding, padding), (0, 0)), mode="edge")
        else:
            padded = content

        x, y = entry.rect_xy
        ph, pw = padded.shape[0], padded.shape[1]
        atlas[y:y + ph, x:x + pw, :] = padded

    return atlas


def _write_uncompressed_dds(path: str | Path, rgba: np.ndarray) -> None:
    """Write an A8R8G8B8 DDS readable by the SM3 DDS->TEX converter."""
    path = Path(path)
    height, width, channels = rgba.shape
    if channels != 4:
        raise ValueError("Atlas DDS writer requires RGBA pixels")

    header = bytearray(128)
    header[0:4] = b"DDS "
    struct.pack_into("<I", header, 4, 124)
    # CAPS | HEIGHT | WIDTH | PITCH | PIXELFORMAT
    struct.pack_into("<I", header, 8, 0x0000100F)
    struct.pack_into("<I", header, 12, int(height))
    struct.pack_into("<I", header, 16, int(width))
    struct.pack_into("<I", header, 20, int(width) * 4)
    struct.pack_into("<I", header, 24, 0)
    struct.pack_into("<I", header, 28, 1)
    pf = 76
    struct.pack_into("<I", header, pf + 0, 32)
    struct.pack_into("<I", header, pf + 4, 0x41)  # DDPF_RGB | DDPF_ALPHAPIXELS
    header[pf + 8:pf + 12] = b"\x00\x00\x00\x00"
    struct.pack_into("<I", header, pf + 12, 32)
    struct.pack_into("<I", header, pf + 16, 0x00FF0000)
    struct.pack_into("<I", header, pf + 20, 0x0000FF00)
    struct.pack_into("<I", header, pf + 24, 0x000000FF)
    struct.pack_into("<I", header, pf + 28, 0xFF000000)
    struct.pack_into("<I", header, 108, 0x1000)  # DDSCAPS_TEXTURE

    rgba8 = np.clip(np.rint(rgba * 255.0), 0, 255).astype(np.uint8)
    bgra8 = rgba8[:, :, [2, 1, 0, 3]]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header) + bgra8.tobytes(order="C"))


_ATLAS_MATERIAL_SUFFIX_RE = re.compile(r"\.\d{3}$")
_ATLAS_HASH_RE = re.compile(r"^0x([0-9A-Fa-f]{1,8})$", re.IGNORECASE)
_ATLAS_LEGACY_RE = re.compile(r"^SM3_MATREF_([0-9A-Fa-f]{1,8})$", re.IGNORECASE)


def _inherited_sm3_material_hash(
    objects: Sequence[bpy.types.Object],
    slot_selection: Optional[Dict[int, Set[int]]] = None,
):
    """Return the first REAL MAT hash represented by the selected materials."""
    for obj in objects:
        selected_slots = None if slot_selection is None else slot_selection.get(obj.as_pointer(), set())
        for slot_index, slot in enumerate(obj.material_slots):
            if selected_slots is not None and slot_index not in selected_slots:
                continue
            mat = slot.material
            if mat is None:
                continue

            base = _ATLAS_MATERIAL_SUFFIX_RE.sub("", str(mat.name or ""))
            m = _ATLAS_HASH_RE.match(base)
            if m:
                return int(m.group(1), 16) & 0xFFFFFFFF

            prop = mat.get("sm3_real_mat_hash")
            if prop is not None:
                try:
                    return int(prop, 0) if isinstance(prop, str) else int(prop)
                except Exception:
                    pass
    return None

def _create_atlas_material(name: str, atlas_image: bpy.types.Image, inherited_mat_hash) -> bpy.types.Material:
    # If the atlas inherited a REAL SM3 MAT hash, display it WoS-style.
    # Use a new datablock so creating the atlas never destroys the source material.
    display_name = f"0x{int(inherited_mat_hash) & 0xFFFFFFFF:08X}" if inherited_mat_hash is not None else name
    mat = bpy.data.materials.new(name=display_name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()
    out = nodes.new("ShaderNodeOutputMaterial")
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = atlas_image
    tex.interpolation = "Linear"
    tex.extension = "EXTEND"
    links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    if "Alpha" in tex.outputs and "Alpha" in bsdf.inputs:
        links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    if inherited_mat_hash is not None:
        mat["sm3_real_mat_hash"] = f"0x{int(inherited_mat_hash) & 0xFFFFFFFF:08X}"
    mat["sm3_atlas_material"] = True
    return mat


def _material_with_atlas_image(source: bpy.types.Material, atlas_image: bpy.types.Image) -> bpy.types.Material:
    """Clone one source material and point its Base Color image to atlas.

    Keeping one material slot per SM3 section is intentional: mesh export needs
    S0/S1/S2/S3 (and other character section counts) even though every material
    can sample the same combined atlas after UV remap.
    """
    mat = source.copy()
    mat.use_nodes = True
    mat["sm3_atlas_material"] = True
    mat["sm3_atlas_image"] = atlas_image.name
    mat["sm3_atlas_source_material"] = source.name
    # Preserve REAL MAT identity explicitly because Blender will suffix copied
    # datablock names (.001/.002). Exporters can read this stable property.
    base_name = _ATLAS_MATERIAL_SUFFIX_RE.sub("", str(source.name or ""))
    hash_match = _ATLAS_HASH_RE.match(base_name)
    if hash_match and mat.get("sm3_real_mat_hash") is None:
        mat["sm3_real_mat_hash"] = f"0x{int(hash_match.group(1), 16) & 0xFFFFFFFF:08X}"

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    principled = next((n for n in nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"), None)
    if principled is None:
        principled = nodes.new("ShaderNodeBsdfPrincipled")
        out = next((n for n in nodes if n.bl_idname == "ShaderNodeOutputMaterial"), None)
        if out is None:
            out = nodes.new("ShaderNodeOutputMaterial")
        links.new(principled.outputs["BSDF"], out.inputs["Surface"])

    base = principled.inputs.get("Base Color")
    tex_node = None
    if base is not None:
        for link in list(base.links):
            if link.from_node and link.from_node.bl_idname == "ShaderNodeTexImage":
                tex_node = link.from_node
                break
    if tex_node is None:
        tex_node = nodes.new("ShaderNodeTexImage")
        if base is not None:
            for link in list(base.links):
                links.remove(link)
            links.new(tex_node.outputs["Color"], base)
    tex_node.image = atlas_image
    tex_node.interpolation = "Linear"
    tex_node.extension = "EXTEND"
    return mat


def _slot_key(obj: bpy.types.Object, slot_index: int) -> Tuple[int, int]:
    return (int(obj.as_pointer()), int(slot_index))


def create_material_atlas(
    objects: Sequence[bpy.types.Object],
    output_directory: str | Path,
    *,
    atlas_name: str = "SM3_Player_Atlas",
    padding: int = 8,
    color_tile_size: int = 32,
    slot_selection: Optional[Dict[int, Set[int]]] = None,
) -> Dict[str, object]:
    objects = [obj for obj in objects if obj and obj.type == "MESH"]
    if not objects:
        raise ValueError("Select one or more mesh objects")
    padding = max(0, int(padding))
    color_tile_size = max(1, int(color_tile_size))

    entries = _collect_slot_entries(objects, slot_selection, color_tile_size)
    if not entries:
        raise ValueError("Selected meshes have no usable checked material slots")

    atlas_size = _pack_shelves(entries, padding)
    if max(atlas_size) > 16384:
        raise ValueError(f"Atlas would be {atlas_size[0]}x{atlas_size[1]}; reduce texture sizes or UV repeat ranges")

    atlas_pixels = _build_atlas_pixels(entries, atlas_size, padding)
    atlas_w, atlas_h = atlas_size

    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    safe_name = atlas_name.strip() or "SM3_Player_Atlas"
    png_path = output_directory / f"{safe_name}.png"
    dds_path = output_directory / f"{safe_name}.dds"
    report_path = output_directory / f"{safe_name}.atlas.json"

    image_name = safe_name
    old = bpy.data.images.get(image_name)
    if old and (int(old.size[0]) != atlas_w or int(old.size[1]) != atlas_h):
        bpy.data.images.remove(old)
        old = None
    atlas_image = old or bpy.data.images.new(image_name, width=atlas_w, height=atlas_h, alpha=True)
    atlas_image.pixels.foreach_set(atlas_pixels.ravel())
    atlas_image.update()
    atlas_image.filepath_raw = str(png_path)
    atlas_image.file_format = "PNG"
    atlas_image.save()
    _write_uncompressed_dds(dds_path, atlas_pixels)

    entry_by_slot = {(e.object_ptr, e.slot_index): e for e in entries}

    # Remap UVs by object + material slot.  Never resolve an atlas rectangle by
    # material pointer/hash because SM3 legitimately repeats hashes across slots.
    for obj in objects:
        obj_ptr = obj.as_pointer()
        selected_slots = None if slot_selection is None else set(slot_selection.get(obj_ptr, set()))
        if slot_selection is not None and not selected_slots:
            continue
        uv_layer = _active_uv_layer(obj)
        uv_data = uv_layer.data
        for poly in obj.data.polygons:
            mi = int(poly.material_index)
            if selected_slots is not None and mi not in selected_slots:
                continue
            entry = entry_by_slot.get((obj_ptr, mi))
            if entry is None:
                continue
            x, y = entry.rect_xy
            sw, sh = entry.source_size
            u0, v0 = entry.uv_origin
            for li in poly.loop_indices:
                uv = uv_data[li].uv
                px = x + padding + (float(uv.x) - u0) * sw
                py = y + padding + (float(uv.y) - v0) * sh
                uv.x = px / atlas_w
                uv.y = py / atlas_h

    inherited_mat_hash = _inherited_sm3_material_hash(objects, slot_selection)
    material_counts_after = {}

    if slot_selection is None:
        # Legacy/fallback mode when no SM3 list was used: collapse selected meshes
        # to one atlas material, matching v1.0 behavior.
        atlas_mat = _create_atlas_material(f"SM3_ATLAS_{safe_name}", atlas_image, inherited_mat_hash)
        for obj in objects:
            obj.data.materials.clear()
            obj.data.materials.append(atlas_mat)
            for poly in obj.data.polygons:
                poly.material_index = 0
            material_counts_after[obj.name] = 1
        atlas_material_name = atlas_mat.name
        slot_mode = "LEGACY_COLLAPSE"
    else:
        # SM3 slot-safe mode: preserve the exact S# layout and polygon indices.
        # Each selected source material is cloned once and pointed at the atlas.
        clone_cache = {}
        for obj in objects:
            obj_ptr = obj.as_pointer()
            selected_slots = set(slot_selection.get(obj_ptr, set()))
            for slot_index in sorted(selected_slots):
                if not (0 <= slot_index < len(obj.material_slots)):
                    continue
                source_mat = obj.material_slots[slot_index].material
                if source_mat is None:
                    continue
                key = source_mat.as_pointer()
                atlas_mat = clone_cache.get(key)
                if atlas_mat is None:
                    atlas_mat = _material_with_atlas_image(source_mat, atlas_image)
                    clone_cache[key] = atlas_mat
                obj.material_slots[slot_index].material = atlas_mat
            material_counts_after[obj.name] = len(obj.material_slots)
        atlas_material_name = "SM3_SLOT_SAFE_SHARED_ATLAS"
        slot_mode = "SM3_PRESERVE_SLOTS"

    zero_face_slots = [f"{e.object_name}:S{e.slot_index}" for e in entries if e.face_count == 0]
    missing_images = [f"{e.object_name}:S{e.slot_index}" for e in entries if e.image is None]

    report = {
        "mode": "SM3_SLOT_SAFE_MATERIAL_ATLAS",
        "slot_mode": slot_mode,
        "atlas_name": safe_name,
        "atlas_size": [atlas_w, atlas_h],
        "padding": padding,
        "objects": [obj.name for obj in objects],
        "slot_count_before": len(entries),
        "material_count_after_per_object": material_counts_after,
        "atlas_material": atlas_material_name,
        "inherited_sm3_real_mat_hash": None if inherited_mat_hash is None else f"0x{int(inherited_mat_hash) & 0xFFFFFFFF:08X}",
        "png": str(png_path),
        "dds": str(dds_path),
        "dds_format": "A8R8G8B8 uncompressed",
        "zero_face_checked_slots": zero_face_slots,
        "checked_slots_without_image": missing_images,
        "next_step": "Inspect the atlas and remapped model. SM3 material slots/hashes remain separate for mesh export.",
        "slots": [
            {
                "object": e.object_name,
                "slot": e.slot_index,
                "material": e.material.name,
                "image": e.image.name if e.image else None,
                "face_count": e.face_count,
                "source_size": list(e.source_size),
                "uv_origin": list(e.uv_origin),
                "uv_repeat": list(e.uv_repeat),
                "content_size": list(e.content_size),
                "rect_xy": list(e.rect_xy),
                "padded_size": list(e.padded_size),
            }
            for e in entries
        ],
        "preserved": ["geometry", "armatures", "vertex groups", "weights", "object transforms", "SM3 material slot count", "polygon material indices"],
        "changed": ["active UV coordinates for checked slots", "checked materials cloned to sample the shared atlas"],
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["report"] = str(report_path)
    return report

