"""SM3/WoS protected work-copy scope helpers.

The normal Material Combiner intentionally scans visible mesh objects.  That is
useful for ordinary Blender work, but the SM3/WoS workflow creates a protected
``__SM3_COMBINER_WORK`` duplicate and requires the atlas operation to touch only
that duplicate.

This module makes that protection self-contained inside Material Combiner.  It
no longer relies solely on a scene string written by the companion toolkit:
whenever a mesh carrying ``sm3_wos_combiner_work`` exists, the combiner detects
it and hard-scopes itself to one proven work copy.
"""

from __future__ import annotations

from typing import Optional, Tuple

import bpy


WORK_FLAG = "sm3_wos_combiner_work"
SCOPE_PROP = "sm3_smc_scope_object"
SOURCE_LINK_PROP = "sm3_wos_combiner_work_name"


def _eligible(obj: Optional[bpy.types.Object]) -> bool:
    return bool(
        obj
        and obj.type == "MESH"
        and getattr(obj.data, "uv_layers", None)
        and obj.data.uv_layers.active
        and obj.data.materials
    )


def _is_work(obj: Optional[bpy.types.Object]) -> bool:
    return bool(_eligible(obj) and obj.get(WORK_FLAG, False))


def _work_candidates() -> list[bpy.types.Object]:
    return [obj for obj in bpy.data.objects if _is_work(obj)]


def resolve_scope(context: bpy.types.Context) -> Tuple[Optional[bpy.types.Object], str]:
    """Resolve the one protected SM3 work copy Material Combiner may touch.

    Resolution order is deliberately strict and deterministic:
    1. Valid scene scope written by the SM3 toolkit.
    2. Active object if it is a protected work copy.
    3. Exactly one protected work copy anywhere in the .blend.
    4. Exactly one visible protected work copy.
    5. Exactly one work copy referenced by a source mesh's stored work name.

    If multiple candidates remain ambiguous, ``(None, 'AMBIGUOUS')`` is
    returned so callers can refuse to fall back to an unsafe whole-scene scan.
    If there are no SM3 work copies at all, ``(None, 'NONE')`` is returned and
    Material Combiner keeps its normal generic behavior.
    """

    scene = context.scene

    scope_name = str(scene.get(SCOPE_PROP, "")).strip()
    if scope_name:
        scoped = bpy.data.objects.get(scope_name)
        if _is_work(scoped):
            return scoped, "SCENE"
        # A stale name must not disable auto detection.
        try:
            del scene[SCOPE_PROP]
        except Exception:
            pass

    active = getattr(context, "active_object", None)
    if _is_work(active):
        scene[SCOPE_PROP] = active.name
        return active, "ACTIVE"

    candidates = _work_candidates()
    if not candidates:
        return None, "NONE"

    if len(candidates) == 1:
        scene[SCOPE_PROP] = candidates[0].name
        return candidates[0], "AUTO"

    visible = []
    for obj in candidates:
        try:
            hidden = obj.hide_get()
        except Exception:
            hidden = False
        if not hidden and not getattr(obj, "hide_viewport", False):
            visible.append(obj)
    if len(visible) == 1:
        scene[SCOPE_PROP] = visible[0].name
        return visible[0], "VISIBLE"

    referenced_names = set()
    for obj in bpy.data.objects:
        name = str(obj.get(SOURCE_LINK_PROP, "")).strip()
        if name:
            referenced_names.add(name)
    referenced = [obj for obj in candidates if obj.name in referenced_names]
    if len(referenced) == 1:
        scene[SCOPE_PROP] = referenced[0].name
        return referenced[0], "SOURCE_LINK"

    return None, "AMBIGUOUS"


def scope_status(context: bpy.types.Context) -> Tuple[Optional[bpy.types.Object], str]:
    """Alias used by UI code; kept separate for readability."""
    return resolve_scope(context)
