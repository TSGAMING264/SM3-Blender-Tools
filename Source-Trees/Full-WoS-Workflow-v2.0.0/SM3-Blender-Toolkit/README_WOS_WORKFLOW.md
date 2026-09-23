# v1.7.7 source-capture target isolation fix

After AUTO RESTORE SOURCE FACE ROUTING succeeds, **CAPTURE SOURCE UV + RIG** and **VALIDATE COMBINER READY** now operate on the proven restored target only. Hidden `__SM3_ROUTING_BACKUP__...` objects and original donor pieces are ignored for these source-workflow checks. This fixes the false `MATERIAL FACE ROUTING COLLAPSED` error that could name the backup even when the live 7372-face target had the correct 4 restored regions.

# v1.7.6 routing + face-inspection fix

**Do not use ACTIVE-last selection anymore.** Select the clean joined/rigged mesh and all original WoS/custom source mesh parts in any order, then press **AUTO RESTORE SOURCE FACE ROUTING**. v1.7.6 retains the v1.7.5 auto-target proof and also fixes material-region inspection: **SHOW ONLY THIS MATERIAL'S FACES** automatically enters Edit Mode, switches to Face Select, clears the previous selection, and highlights only the active material region.

Keep the hidden `__SM3_ROUTING_BACKUP__...` until the restored source-material regions are verified.

# SM3 Blender Toolkit v1.6.0 — WoS 1:1 Material Workflow


## v1.7.4 Batman-tutorial correction: preserve source face routing first

The Batman Part II tutorial confirms the missing rule: **Material Combiner does not decide which faces belong to which texture.** The source model already has face -> material assignments. In Edit Mode the tutorial uses Blender's **Select** button to reveal the faces owned by each existing material slot, assigns the correct image to that material, and only then runs Material Combiner.

If a clean custom mesh has several slots but every face is in slot 0, stop before atlasing. Re-import/select the original unmodified WoS/custom source part(s), keep the clean rigged target active, and use **RESTORE SOURCE FACE ROUTING**. v1.7.4 matches identical triangles geometrically across one or more source objects, recreates the original source-material regions on the clean target, and makes a hidden pre-routing backup. It does not touch the rig, weights, geometry, or UVs.

After routing is restored, keep the source/WoS materials through the atlas stage. Do **not** replace them with final SM3 hash slots before combining. The protected Material Combiner work copy may collapse to one atlas material. The final clean game-output stage is where the one safe SM3 material route is applied.

This build focuses on the workflow shown by the original Web of Shadows tools/tutorial process rather than a hard-coded Spider-Man atlas.

## Workflow

1. Import the SM3 MESH and skeleton.
2. Select a mesh. The panel shows the **actual material slots used by that model**.
3. Pick any material slot and choose its image (DDS/PNG/TGA/JPG or SM3 `.tex` / `.wrap.tex`).
4. Repeat until the model looks correct with its original separate materials.
5. Capture `SM3_SRC_MASTER` + rig state.
6. Run the **original Material Combiner** addon. It creates the atlas and repacks the working UVs.
7. Capture the result as `SM3_ATLAS`.
8. Validate that geometry, armature and vertex groups survived.
9. Export the SM3 MESH for testing.

## What v1.6.0 deliberately does NOT do

- It does not force WHITE/SPIDER/SIDEWEB/TOPWEB.
- It does not force TEX1/TEX2.
- It does not force `0x7D5E2562`.
- It does not invent atlas UVs.

Those resources can be used if the model/material requires them, but they are not the workflow. The workflow is **material -> source image -> original Material Combiner -> atlas + UV**.

## SM3 TEX preview

The material image chooser can decode the two SM3 texture containers established in the current research set:

- WRAP TEX with a PHYS payload.
- Loose 68-byte TEX header + DXT payload.

The source game texture is not rewritten. A temporary DDS is created only so Blender/Material Combiner can see the pixels.

## Bones

Bones do not determine texture placement. Material assignments and UVs do. Bones/weights still matter to SM3 export, so the toolkit records the rig before combining and validates it again after the atlas step.

## Vertex color

The existing BLACK / WHITE export selector remains available for controlled tests.

## v1.6.9 Clean Game Output
After the original Material Combiner creates the atlas and you capture `SM3_ATLAS`, use **BUILD / REFRESH CLEAN GAME OUTPUT**.

The editable source keeps every material slot. The generated `__SM3_CLEAN_EXPORT` copy keeps the rig/weights/atlas UV but collapses the final game mesh to one safe Spider route, preventing stock SIDEWEB/TOPWEB/BACKSPIDER sections from surviving into the final export.

For Spider 000 the clean output uses the proven one-section `0x0614` route (`0xE52A3DF4 / ch_spidermanspider`). The Blender preview material uses only the atlas image and deliberately does not add normal/spec/overlay nodes.


## v1.7.1 correction: remove the donor body after weight transfer

If the stock SM3 webs/back spider appear over the custom/WoS model, first verify that the original SM3 donor BODY mesh was removed or hidden after weight transfer. Keep the armature/skeleton. The toolkit no longer deletes polygons based on web/back-spider material slots.


## v1.7.3 protected Material Combiner copy + target isolation

The original Material Combiner is intentionally destructive: after a successful combine it replaces the selected original material slots with one atlas material. v1.7.3 therefore never runs it on the editable source and disables every other visible mesh in the combiner list. Use **PREPARE COMBINER WORK COPY** first. The generated `__SM3_COMBINER_WORK` duplicate is the only object that should collapse to one material; the source retains every slot.


## v1.8.5 neutral game-shading test
The weight-safe geometry test passed in-game, but the clean mesh used full-white
vertex colors. Use **Game Shading Test -> Neutral Black (Recommended)** before
rebuilding/exporting the clean Spider 000 mesh. This changes only serialized
vertex color; it does not change the atlas, UVs, material 0x0614, rig, weights,
or smart-section layout. The export JSON records the actual color mode.


## Fresh redo after an older save
1. Select the editable Spider mesh (or its old work/clean output).
2. Press **RESET ATLAS STAGE (KEEP RIG / ROUTING)**.
3. Confirm the four routed source materials/images still look right.
4. Press **PREPARE COMBINER WORK COPY**.
5. **Update Material List** then **Generate Texture Atlas**.
6. Press **CAPTURE COMBINED ATLAS UV**. v1.8.5 captures `SM3_SRC` from the protected work copy only.
7. Validate, build clean output, inspect it, then weight-safe export.
