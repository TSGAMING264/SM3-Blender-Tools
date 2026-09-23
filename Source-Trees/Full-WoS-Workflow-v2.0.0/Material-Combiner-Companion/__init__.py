"""Material Combiner - WoS workflow compatibility build for modern Blender.

Compatibility wrapper for the historical Material Combiner workflow used by
WoS modding.  The legacy self-updater is intentionally disabled; Blender's
extension manager owns installation/update handling.
"""

from .compat_metadata import ADDON_INFO
from .registration import register_all, unregister_all


def register() -> None:
    print("Loading Material Combiner (WoS compatibility build / SM3 WoS Workflow Clone v2.0.0)..")
    # IMPORTANT: positional call only.  The original 2.1.3 registrar accepts
    # exactly one metadata argument, so this remains compatible with both the
    # original registrar and our modernized one.
    register_all(ADDON_INFO)


def unregister() -> None:
    unregister_all()
