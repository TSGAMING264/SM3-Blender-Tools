"""Metadata for the modern Blender extension build.

Kept separate from Blender's legacy ``bl_info`` convention so the extension
loader and partially initialized package imports cannot trip over that global.
"""

ADDON_NAME = "Material Combiner - WoS Workflow Compatibility"
ADDON_VERSION = (2, 3, 0, 0)
ADDON_INFO = {
    "name": ADDON_NAME,
    "description": "Advanced Texture Atlas Generation System (WoS workflow compatibility build; SM3 WoS Clone v2.0 protected hard scope)",
    "author": "shotariya / Grim-es",
    "version": ADDON_VERSION,
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > MatCombiner",
    "wiki_url": "https://github.com/Grim-es/material-combiner-addon",
    "tracker_url": "https://github.com/Grim-es/material-combiner-addon/issues",
    "category": "Object",
}
