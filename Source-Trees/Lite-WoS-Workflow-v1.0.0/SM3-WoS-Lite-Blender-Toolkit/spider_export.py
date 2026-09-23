from __future__ import annotations

"""Simple Spider-Man 000 material-routed exporter.

This is intentionally closer to the WoS workflow than the research/atlas UI:
- Blender material slots decide face routing.
- Material hash names are mapped back to stock Spider-Man 000 material refs.
- Each material region is split only as needed to keep bone palettes <= 32.
- Existing weights are preserved; no bone collapse/remap is performed.
"""

import json
import os
import re
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

from .sm3_export import (
    _build_triangle_records,
    _records_to_geometry,
    _split_records_for_bones_smart,
    _write_raw_mesh_spider_uv1024,
)
from .sm3_format import choose_position_divisors
from .sm3_material_names import resolve_mesh_material
from .sm3_mesh_writer import ExportSection
from .target_cache import cached_snapshot_text, load_target_template, mesh_from_snapshot

SPIDER_000_HASH = 0xAC92103D

# Canonical handoff routing requested for the lean WoS-style workflow.
CANONICAL_SPIDER_MATERIALS = (
    (0xAC933008, "ch_spidermanred", "PRIMARY BODY APPEARANCE"),
    (0x3EF08B55, "ch_spidermanblue", "SECONDARY BODY APPEARANCE"),
    (0x1E7B964E, "ch_spidermanwhite", "EYES"),
    (0xE52A3DF4, "ch_spidermanspider", "FRONT SPIDER + EYELIDS"),
    (0xE771757E, "ch_spidermantopweb", "WEBS / WEB COLOR"),
    (0x654DD425, "ch_spidermanbackspider", "BACK SPIDER"),
)

# Stock Spider-Man 000 also contains a separate side-web material.  It is
# accepted by the exporter even though the six-slot handoff UI intentionally
# keeps the user's simpler WEBS slot as the main route.
STOCK_EXTRA_MATERIALS = (
    (0x79C43D30, "ch_spidermansideweb", "SIDE WEBS (STOCK EXTRA)"),
)


def _u32(value, default=0) -> int:
    if value in (None, ""):
        return int(default) & 0xFFFFFFFF
    if isinstance(value, str):
        try:
            return int(value, 0) & 0xFFFFFFFF
        except Exception:
            m = re.search(r"0x([0-9A-Fa-f]{8})", value)
            if m:
                return int(m.group(1), 16)
            return int(default) & 0xFFFFFFFF
    try:
        return int(value) & 0xFFFFFFFF
    except Exception:
        return int(default) & 0xFFFFFFFF


def material_hash(material) -> int:
    if material is None:
        return 0
    for key in ("sm3_real_mat_hash", "sm3_mat_hash", "sm3_lite_mat_hash"):
        try:
            value = material.get(key)
        except Exception:
            value = None
        h = _u32(value, 0)
        if h:
            return h
    m = re.search(r"0x([0-9A-Fa-f]{8})", str(getattr(material, "name", "")))
    return int(m.group(1), 16) if m else 0


def _template_from_target(target_collection, objects):
    # Prefer the .blend snapshot first.  This keeps export working after the
    # original donor body is deleted and also handles a source imported from a
    # WRAP file, whose outer bytes cannot be passed straight to parse_mesh().
    text = cached_snapshot_text(target_collection)
    if not text:
        for obj in objects:
            text = cached_snapshot_text(obj) or cached_snapshot_text(getattr(obj, "data", None))
            if text:
                break
    if text:
        return mesh_from_snapshot(text, path="<SM3_LITE_TARGET_CACHE_IN_BLEND>")
    return load_target_template(target_collection, objects)


def _template_material_profiles(template) -> Dict[int, dict]:
    profiles: Dict[int, dict] = {}
    for sec in template.sections:
        resolved = resolve_mesh_material(
            int(template.filename_hash),
            int(sec.info_offset) + 0x20,
            int(sec.material_ref_serialized),
        )
        if resolved is None:
            continue
        mat_hash, mat_name, mode = resolved
        mat_hash = int(mat_hash) & 0xFFFFFFFF
        if mat_hash not in profiles:
            profiles[mat_hash] = {
                "section_index": int(sec.index),
                "material_ref": int(sec.material_ref_serialized) & 0xFFFFFFFF,
                "name": str(mat_name),
                "mode": str(mode),
            }

    # Verified Spider-Man 000 fallback route.  These values were recovered from
    # the stock 10-section target and are only used if the compact resolver is
    # unavailable for some reason.
    if int(template.filename_hash) == SPIDER_000_HASH:
        fallback = {
            0xAC933008: (0, 0x000004E4, "ch_spidermanred"),
            0x3EF08B55: (2, 0x00000128, "ch_spidermanblue"),
            0x654DD425: (3, 0x000002BC, "ch_spidermanbackspider"),
            0xE52A3DF4: (4, 0x00000614, "ch_spidermanspider"),
            0x79C43D30: (5, 0x0000037C, "ch_spidermansideweb"),
            0xE771757E: (7, 0x00000640, "ch_spidermantopweb"),
            0x1E7B964E: (9, 0x000000A0, "ch_spidermanwhite"),
        }
        for h, (idx, ref, name) in fallback.items():
            profiles.setdefault(h, {
                "section_index": idx,
                "material_ref": ref,
                "name": name,
                "mode": "SPIDER_000_VERIFIED_FALLBACK",
            })
    return profiles


def _active_uv_name(obj) -> str:
    layers = getattr(getattr(obj, "data", None), "uv_layers", None)
    if not layers or len(layers) == 0:
        return ""
    for layer in layers:
        if getattr(layer, "active_render", False):
            return str(layer.name)
    active = getattr(layers, "active", None)
    if active is not None:
        return str(active.name)
    return str(layers[0].name)


def export_spider_000_material_routed(
    objects: Sequence,
    target_collection,
    output_path: str,
    *,
    flip_uv_v: bool = True,
    reverse_winding: bool = True,
    write_report: bool = True,
):
    objects = [obj for obj in objects if obj is not None and getattr(obj, "type", None) == "MESH"]
    if not objects:
        raise ValueError("Select at least one weighted replacement MESH")
    if target_collection is None:
        raise ValueError("No saved SM3 target was found; transfer weights from the imported stock Spider-Man first")

    template = _template_from_target(target_collection, objects)
    if int(template.filename_hash) != SPIDER_000_HASH:
        raise ValueError(
            f"SM3 WoS Lite currently exports Spider-Man 000 only; target is 0x{int(template.filename_hash):08X}"
        )
    if len(template.sections) != 10:
        raise ValueError(f"Spider-Man 000 target cache must contain 10 stock sections; found {len(template.sections)}")

    profiles = _template_material_profiles(template)
    if not profiles:
        raise ValueError("Could not resolve the stock Spider-Man material routing table")
    divisors = choose_position_divisors(template)

    warnings: List[str] = []
    routed_records: Dict[int, List[dict]] = {}
    route_objects: Dict[int, set] = {}

    for obj in objects:
        uv_name = _active_uv_name(obj)
        if not uv_name:
            raise ValueError(f"{obj.name}: no UV map found")
        records = _build_triangle_records(
            obj,
            flip_uv_v=bool(flip_uv_v),
            reverse_winding=bool(reverse_winding),
            warnings=warnings,
            preferred_uv_layer_name=uv_name,
            wos_compatible_tbn=True,
        )
        mats = list(obj.data.materials)
        for rec in records:
            slot = int(rec.get("material_index", 0))
            if slot < 0 or slot >= len(mats) or mats[slot] is None:
                raise ValueError(f"{obj.name}: triangle uses material slot {slot}, but that slot is empty")
            h = material_hash(mats[slot])
            if not h:
                raise ValueError(
                    f"{obj.name}: material slot {slot} ('{mats[slot].name}') has no 0xXXXXXXXX SM3 material hash"
                )
            if h not in profiles:
                raise ValueError(
                    f"{obj.name}: material 0x{h:08X} is not part of the stock Spider-Man 000 routing table"
                )
            routed_records.setdefault(h, []).append(rec)
            route_objects.setdefault(h, set()).add(obj.name)

    if not routed_records:
        raise ValueError("No routed triangles were produced")

    sections: List[ExportSection] = []
    section_report = []
    total_triangles = 0

    # Stable order follows the canonical six first, then stock extras.
    preferred_order = [h for h, _name, _role in CANONICAL_SPIDER_MATERIALS]
    preferred_order += [h for h, _name, _role in STOCK_EXTRA_MATERIALS]
    ordered_hashes = [h for h in preferred_order if h in routed_records]
    ordered_hashes += sorted(h for h in routed_records if h not in ordered_hashes)

    for h in ordered_hashes:
        profile = profiles[h]
        profile_index = int(profile["section_index"])
        template_sec = template.sections[profile_index]
        divisor = float(divisors[profile_index]) if profile_index < len(divisors) else 512.0
        records = routed_records[h]
        chunks = _split_records_for_bones_smart(records, 32)
        if not chunks:
            raise ValueError(f"0x{h:08X}: weight-safe splitter returned no sections")

        for chunk_index, (chunk_records, bone_set) in enumerate(chunks):
            vertices, triangles = _records_to_geometry(chunk_records)
            if not vertices or not triangles:
                continue
            palette = sorted(int(x) for x in bone_set) or [0]
            if len(palette) > 32:
                raise ValueError(f"0x{h:08X}: chunk {chunk_index} still uses {len(palette)} bones")
            sec = ExportSection(
                source_object=" + ".join(sorted(route_objects.get(h, ()))),
                source_material_index=0,
                material_ref_serialized=int(profile["material_ref"]),
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
            sections.append(sec)
            total_triangles += len(triangles)
            section_report.append({
                "output_section": len(sections) - 1,
                "material_hash": f"0x{h:08X}",
                "material_name": profile["name"],
                "material_ref_serialized": f"0x{int(profile['material_ref']):08X}",
                "stock_profile_section": profile_index,
                "bone_chunk": chunk_index,
                "bone_palette_count": len(palette),
                "vertex_count": len(vertices),
                "triangle_count": len(triangles),
                "weights_preserved": True,
            })

    if not sections:
        raise ValueError("Export became empty")

    result = _write_raw_mesh_spider_uv1024(
        output_path,
        template,
        sections,
        filename_hash=template.filename_hash,
        player_target="AUTO",
        geometry_profile="TARGET_NATIVE",
    )
    result.report.update({
        "export_mode": "SM3_WOS_LITE_SPIDER_000_NATIVE_MATERIAL_ROUTING",
        "target_filename_hash": f"0x{int(template.filename_hash):08X}",
        "target_stock_section_count": len(template.sections),
        "output_section_count": len(sections),
        "triangle_count": total_triangles,
        "bone_palette_limit_per_section": 32,
        "weights_preserved": True,
        "bone_remap_count": 0,
        "uv_divisor": 1024.0,
        "material_routes": section_report,
        "warnings": warnings,
    })

    if write_report:
        report_path = str(Path(output_path).with_suffix(Path(output_path).suffix + ".export.json"))
        Path(report_path).write_text(json.dumps(result.report, indent=2), encoding="utf-8")
        result.report["report_path"] = report_path
    return result
