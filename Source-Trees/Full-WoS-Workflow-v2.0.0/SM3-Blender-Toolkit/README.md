# SM3 WoS Workflow Clone v2.0.0

A clean Spider-Man 3 Blender workflow organized to feel much closer to the classic Web of Shadows modding process:

**import -> rig -> source material regions -> assign source textures -> Material Combiner -> atlas + packed UV -> capture atlas UV -> clean game output -> weight-safe export**

This is a workflow tool, not a magic automatic port button. The important WoS rule is preserved: **face-to-material routing must already be correct before Material Combiner runs.** Material Combiner packs the images and repacks UVs; it does not decide what faces belong to what texture.

## Canonical Spider 000 source slots

Keep these roles visible while preparing the model:

| Hash | Stock material | Workflow role |
|---|---|---|
| `0xAC933008` | `ch_spidermanred` | **PRIMARY BODY APPEARANCE** — changes most of the suit/body |
| `0x3EF08B55` | `ch_spidermanblue` | **SECONDARY BODY APPEARANCE** — remaining body regions |
| `0x1E7B964E` | `ch_spidermanwhite` | **EYES** |
| `0xE52A3DF4` | `ch_spidermanspider` | **FRONT SPIDER + EYELIDS** |
| `0xE771757E` | `ch_spidermantopweb` | **WEBS / WEB COLOR** |
| `0x654DD425` | `ch_spidermanbackspider` | **BACK SPIDER** |

The panel contains the same guide. **ADD / VERIFY 6 SLOTS** safely appends a missing material without reordering existing slots or changing face assignments. **WRITE SLOT GUIDE** writes the mapping into Blender's Text Editor so a handed-off `.blend` still explains itself.

## Recommended workflow

1. Import the SM3 target mesh and skeleton.
2. Rig/weight the custom model to the SM3 skeleton. Rename vertex groups when needed.
3. On the editable custom mesh, use **ADD / VERIFY 6 SLOTS** and inspect each region with **SHOW ONLY THIS MATERIAL'S FACES**.
4. Assign the correct source image to every material that actually owns faces. The model should look correct before atlasing.
5. Run **CAPTURE SOURCE UV + RIG**, then validate.
6. Run **PREPARE COMBINER WORK COPY**. Only the protected work copy should enter Material Combiner.
7. In the separate Material Combiner add-on, refresh the material list and generate the atlas. One material after combining is expected on the work copy.
8. Run **CAPTURE COMBINED ATLAS UV**. Validate again.
9. Run **BUILD / REFRESH CLEAN GAME OUTPUT**.
10. Export with **EXPORT CLEAN SPIDER 000 MESH (WEIGHT SAFE)**.

## Important preservation rules

- Do not collapse the six source roles before Material Combiner.
- Do not let the original SM3 donor body remain visible after weight transfer; keep the skeleton/armature.
- Do not repair a texture problem by changing the proven skeleton, weights, position divisor, or UV divisor.
- The weight-safe exporter may make multiple game sections. That is intentional so each section stays within the 32-bone palette limit without remapping bones.
- Final atlas UV serialization remains `SHORT2 * 1024`.
- The protected source object is kept intact. Material Combiner works on a duplicate.

See `START_HERE_WOS_CLONE.txt`, `MATERIAL_SLOT_MAP.txt`, and `SENDOFF_CHECKLIST.txt` for a copy/paste handoff.
