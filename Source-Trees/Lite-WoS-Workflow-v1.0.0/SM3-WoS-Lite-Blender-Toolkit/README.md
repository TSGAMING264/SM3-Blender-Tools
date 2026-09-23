# SM3 WoS Lite BlenderToolkit v1.0.0

A deliberately small Spider-Man 3 PC model-port workflow modeled after the practical Web of Shadows Blender workflow: import the stock model/skeleton, align a replacement model, transfer weights, bind to the stock armature, use real material hash slots, choose a vertex-color test, and export.

## The only workflow

1. **Import Mesh** — import stock `0xAC92103D.ch_spiderman000.mesh` (raw or WRAP).
2. **Import Skeleton** if it was not paired automatically.
3. Import your replacement model with Blender, align it over the stock Spider-Man.
4. Make the replacement mesh active and press **TRANSFER WEIGHTS + ARMATURE**.
5. The original stock body is hidden; the SM3 armature is kept and attached to the replacement.
6. Press **ADD / VERIFY 6 MATERIALS** and assign the correct faces to the hash slots using Blender's normal Material > Assign controls.
7. Press **BLACK** or **WHITE** for the vertex-color test you want.
8. Select the replacement and press **EXPORT SPIDER-MAN 000 MESH**.

The exporter preserves weights and splits material regions into 32-bone-safe sections instead of collapsing/remapping bones.

## Canonical Spider-Man material routing

| MAT hash | Stock name | Workflow role |
|---|---|---|
| `0xAC933008` | `ch_spidermanred` | PRIMARY BODY APPEARANCE |
| `0x3EF08B55` | `ch_spidermanblue` | SECONDARY BODY APPEARANCE |
| `0x1E7B964E` | `ch_spidermanwhite` | EYES |
| `0xE52A3DF4` | `ch_spidermanspider` | FRONT SPIDER + EYELIDS |
| `0xE771757E` | `ch_spidermantopweb` | WEBS / WEB COLOR |
| `0x654DD425` | `ch_spidermanbackspider` | BACK SPIDER |

Stock Spider-Man also has `0x79C43D30 ch_spidermansideweb`; the exporter accepts it, but the handoff UI keeps the requested six-slot workflow simple.

### Verified stock Spider-Man 000 section routing

- Sections 0-1 -> `0xAC933008`, local ref `0x000004E4`
- Section 2 -> `0x3EF08B55`, local ref `0x00000128`
- Section 3 -> `0x654DD425`, local ref `0x000002BC`
- Section 4 -> `0xE52A3DF4`, local ref `0x00000614`
- Sections 5-6 -> `0x79C43D30`, local ref `0x0000037C`
- Sections 7-8 -> `0xE771757E`, local ref `0x00000640`
- Section 9 -> `0x1E7B964E`, local ref `0x000000A0`

## DDS -> WRAP TEX

Like the WoS Blender workflow, drag a DDS into Blender's **3D Viewport**. The add-on creates a `.wrap.tex` beside the DDS.

Use a filename such as:

`0x8E661B33.ch_spiderman_spider.dds`

The explicit `0xXXXXXXXX` prefix becomes the internal SM3 TEX hash. If the prefix is absent, the filename stem is hashed with the game's lowercase `hash = hash * 33 + char` routine.

Supported DDS formats in this Lite build: **DXT1, DXT3, DXT5**. The converter writes the verified `CH_SPIDERMAN` WRAP archive hash `0xCFB154CD` and preserves DDS dimensions, mip count, compression, and payload.

## Important export rule

The material hash assigned to each face region is what decides which stock SM3 material route that region receives in-game. Do not rename materials to random names before export; keep a `0xXXXXXXXX` hash in the material name or its `sm3_real_mat_hash` property.

## Credits / workflow references

This Lite workflow was intentionally shaped after the public Web of Shadows Blender workflows, especially the simple model import/export, Data Transfer, armature-parenting, material-hash, vertex-color, and DDS/TEX conversion approach documented by **haruse23** and the broader **Devryx505 / kirbystealer WoS tools** ecosystem. This build is an SM3-specific implementation and uses the SM3 format research/backends developed for the TSGAMING264 workflow.
