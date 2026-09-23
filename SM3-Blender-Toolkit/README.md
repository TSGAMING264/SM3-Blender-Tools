SM3 Blender Toolkit v1.1.7 - WoS Literal Any-Model Rename
=============================================================

## v1.1.7 rename contract

The vertex-group rename path now mirrors the original WoS toolkit contract:
MESH import creates `bone_<global skeleton index>` groups, SKEL import preserves
game bone order, and Rename maps `bone_N` directly to armature bone `N`.
The rename function returns one integer just like WoS, eliminating the prior
return-shape mismatch that caused `cannot unpack non-iterable int object`.
The Blender extension manifest and add-on version are both 1.1.6.


# Spider-Man 3 Blender Toolkit v1.1.4 — WoS Direct Bone Group Fix

This build intentionally follows the upstream WoS Blender Toolkit's simple object-driven model.


## v1.1.4 direct WoS bone-group fix

- Native SM3 MESH import now **always** creates raw `bone_<global index>` vertex groups first, exactly like the supplied WoS toolkit.
- Skeleton names are no longer injected during MESH import; `Rename Vertex Groups` is the only stage that maps bone indices to the imported SKEL names.
- Weight assignment now uses Blender `VertexGroup.add(...)` directly, matching WoS instead of a BMesh deform-layer recovery path.
- Blend-index and blend-weight rows defensively accept both scalar and sequence representations, fixing the Venom `'int' object is not subscriptable` failure.
- No character tables or special Venom/Spider-Man/Black-Suit cases were added.

## Core rules

- Import any supported raw SM3 `.mesh` into one Blender collection.
- Every imported SM3 section becomes one Blender MESH object.
- Import any raw SM3 `.skel`.
- `Rename Vertex Groups` maps `bone_<index>` and unresolved `bone_<index>_0xHASH` groups using the imported skeleton's real SM3 bone indices.
- Export Mesh uses **the CURRENT Blender MESH objects as the output section layout**.
- Keeping the original pieces preserves their native SM3 section provenance.
- Joining pieces is supported: a joined Blender object exports as one SM3 section (unless the 48-bone palette limit requires a split).
- Blender Join can leave an old `sm3_section_index` on the active object; v1.1.1 detects a changed object layout and ignores that stale provenance automatically.
- The exporter never forces replacement geometry to match the stock target section count.
- An object is split only when its used palette would exceed the proven 48-bone SM3/XESM3 limit.
- The exact original SM3 resource hash and output basename are preserved by the imported target collection.

## Two supported workflows

### Original pieces
1. Import the SM3 MESH.
2. Keep its imported `SM3_MeshObject_*` pieces separate.
3. Edit/weight as needed.
4. Export Mesh.

The complete one-for-one native piece set keeps its exact section profile/material provenance.

### Joined model
1. Import the SM3 MESH.
2. Select the pieces and use Blender Join (`Ctrl+J`), or replace them with one joined mesh.
3. Keep the joined object inside the imported SM3 target collection.
4. Export Mesh.

The joined object becomes the current SM3 output section. Old section IDs retained by Blender Join are ignored.

There is no player-target selector, bind-target step, or forced target-section quota.

## v1.1.3 rename fix

- No character-specific rename tables. Mesh bone indices map directly to the imported skeleton at the same `sm3_index`, matching the WoS toolkit principle.
- Works with non-Spider-Man skeletons such as Venom. Unknown bone-name hashes remain safely represented by their skeleton bone name instead of being skipped.
- Blender duplicate collections such as `ch_venom000.001` now receive the matching skeleton when that collection is selected in the toolkit.
- Joined + original-pieces export behavior from v1.1.1 is unchanged.


## v1.1.3 generic vertex-group recovery
- Rename is driven only by SM3 global bone indices from the MESH palette and imported SKEL order.
- Missing groups on untouched native SM3 sections are rebuilt from the original MESH weights before rename.
- Already-correct skeleton names count as mapped instead of returning a misleading Renamed 0.
- Unrecognized/no-weight states now report the actual problem instead of silently succeeding.


## v1.1.7 any-model rename correction
- Rename uses the original imported `.skel` as the authoritative GAME-index -> bone-name map.
- The armature and vertex groups are repaired from the same index map.
- MESH import stores a hidden Blender-group-index -> SM3 game-bone-index map so renamed/legacy groups can recover.
- `Renamed 0` now means groups were already mapped; truly unrecognized groups raise a diagnostic instead.
- No character-specific selection logic is used.
