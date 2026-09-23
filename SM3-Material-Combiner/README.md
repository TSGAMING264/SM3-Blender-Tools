# SM3 Material Combiner Toolkit v1.2.0

This is the **standalone Spider-Man 3 clone** of the material-combiner workflow. It remains separate from the original `material-combiner-addon-master` / `MatCombiner` addon.

## v1.2.0 — Slot-Safe Atlas Fix

This build fixes the confirmed atlas bug from the WOS -> SM3 Spider-Man test where S2 (`0x3EF08B55`) had its own TEX2 image connected to Base Color and previewed correctly in Blender, but the generated atlas still omitted that image.

### Fixed atlas collection
- Atlas sources are collected by **object + material slot**.
- S0/S1/S2/S3 are independent inputs even when hashes repeat.
- The image feeding Principled BSDF Base Color is traced per slot.
- Checked zero-face slots are included in the atlas/report instead of being silently discarded.
- Atlas UV remap resolves the correct rectangle by object+slot.

### SM3-safe post-atlas behavior
When the **Materials to Combine** list drives the operation, the tool no longer collapses the model to one material slot. It preserves the original SM3 slot count and polygon material indices, and points cloned slot materials at the shared generated atlas. This keeps SM3 section/material routing available for export.

### Better diagnostics
**Update Material List** now shows:
- `S#` slot index
- REAL MAT hash
- face count (`F#`)
- source image name / `NO IMAGE`

The generated `.atlas.json` also records every checked slot, image, face count, UV range, repeat range, and atlas rectangle.

## Current 000 test layout
- S0 `0xAC933008` -> WOS BODY
- S1 `0xAC933008` -> WOS BODY
- S2 `0x3EF08B55` -> WOS ARMS/LEGS (TEX2)
- S3 `0x1E7B964E` -> WOS BODY

After **Create SLOT-SAFE Atlas + Remap UVs + DDS**, the atlas should visibly contain both BODY and ARMS/LEGS artwork while the Blender mesh still retains all four SM3 slots.

## Existing SM3 features retained
- REAL SM3 Material Database
- Resolve serialized refs -> REAL MAT hashes
- MAT -> TEX report
- Legacy material-name upgrade
- Duplicate REAL MAT cleanup
- Load resolved DDS textures
- DDS/TEX conversion
- Atlas + UV remap
- SM3 database CSVs
