# Material Combiner — SM3 WoS Workflow Clone Companion v2.0

This is the **separate Material Combiner companion** for `SM3 WoS Workflow Clone v2.0.0`.

It preserves the classic Material Combiner behavior used in WoS-style workflows: selected source materials are packed into one atlas and their UVs are repacked to match that atlas.

## Important SM3 hard-scope behavior

The SM3 toolkit creates a protected `__SM3_COMBINER_WORK` duplicate. This companion build honors that scope so donor meshes, backups, old clean exports, and unrelated scene objects do not accidentally enter the atlas.

Normal workflow:

1. In **SM3 WoS Clone Workflow**, make the editable source model look correct with its separate source materials.
2. Capture source UV + rig.
3. Press **PREPARE COMBINER WORK COPY**.
4. Press **Update Material List**.
5. Confirm only the intended work-copy materials are included.
6. Press **Generate Texture Atlas**.
7. Return to SM3 Tools and press **CAPTURE COMBINED ATLAS UV**.

## Canonical Spider 000 source roles

- `0xAC933008` — PRIMARY BODY APPEARANCE
- `0x3EF08B55` — SECONDARY BODY APPEARANCE
- `0x1E7B964E` — EYES
- `0xE52A3DF4` — FRONT SPIDER + EYELIDS
- `0xE771757E` — WEBS / WEB COLOR
- `0x654DD425` — BACK SPIDER

Material Combiner **does not create these face assignments**. The source model must already have correct face-to-material routing before combining.

Original Material Combiner project credit remains with shotariya / Grim-es. This package is a compatibility/hard-scope companion for the SM3 WoS workflow.
