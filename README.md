# Spider-Man 3 Blender Tools

Blender import, export, material, texture, and atlas tools for **Spider-Man 3 PC** modding, created by **TSGAMING264**.

This repository keeps the Blender tooling separate from the [xeSM3 loader](https://github.com/TSGAMING264/xeSM3) so each project has a clear purpose:

- **xeSM3** loads loose Spider-Man 3 resources and provides the tested runtime and renderer restoration.
- **SM3 Blender Tools** handles Blender-side mesh, skeleton, material, texture, and atlas workflows.

## Original source tree (preserved)

The original repository source remains at the root and has not been replaced or mixed with the newer workflows.

### [SM3 Blender Toolkit v1.1.7](SM3-Blender-Toolkit/)

Provides:

- SM3 MESH import and export
- SM3 SKEL import and skeleton export support
- WoS-style index-based vertex-group renaming
- Original-piece and joined-model workflows
- Object/section-aware export behavior
- Imported target hash and basename preservation

The imported SKEL is the authoritative game-index-to-bone-name map. The workflow is intended to support different Spider-Man 3 skeletons without character-specific rename tables.

### [SM3 Material Combiner Toolkit v1.2.0](SM3-Material-Combiner/)

Provides:

- REAL SM3 material database access
- Serialized-reference to REAL MAT hash resolution
- MAT-to-TEX reporting
- DDS/TEX loading and conversion workflows
- Legacy material-name upgrades and duplicate cleanup
- Slot-safe atlas generation and UV remapping
- Preservation of SM3 material slots and polygon material indices

## Additional workflow source trees

Two self-contained alternatives are available under [`Source-Trees/`](Source-Trees/README.md). Their files stay separate from the original source and from each other.

### [Full WoS Workflow Clone v2.0.0](Source-Trees/Full-WoS-Workflow-v2.0.0/)

Contains:

- SM3 WoS Workflow Clone v2.0.0
- Its separately installed Material Combiner companion
- The full source-material routing, protected atlas work-copy, clean-output, and weight-safe export workflow

### [Lite WoS Workflow v1.0.0](Source-Trees/Lite-WoS-Workflow-v1.0.0/)

Contains:

- SM3 WoS Lite Blender Toolkit v1.0.0
- Its own copy of the separately installed Material Combiner companion
- A smaller import, weight-transfer, material-routing, texture-conversion, and Spider-Man 000 export workflow

The two supplied Material Combiner companion ZIPs are byte-for-byte identical. A copy is retained inside each workflow tree so both trees are complete and understandable on their own.

## Requirements

- A supported Blender version for the selected tree; the supplied add-ons declare minimums ranging from Blender 4.1 to 4.5
- Original Spider-Man 3 resources appropriate to the workflow you are using
- [xeSM3](https://github.com/TSGAMING264/xeSM3) when testing exported loose resources in game

## Installation

Each toolkit and companion folder is an independent Blender extension. Choose one source tree, follow its included README, and install its add-ons separately. Do not merge files between the original, Full, and Lite trees.

When packaged releases are published:

1. Download the individual tool ZIP from this repository's Releases page.
2. Keep that ZIP intact.
3. In Blender, open **Edit → Preferences → Add-ons / Extensions → Install from Disk**.
4. Select the individual tool ZIP and enable it.

For source development, keep each tool directory intact with `__init__.py` and `blender_manifest.toml` at its root. Do not install the ZIP of this entire repository as one Blender extension.

The tools appear in Blender as:

- **SM3 Blender Toolkit:** File Import/Export and the 3D View N-panel under **SM3 Tools**
- **SM3 Material Combiner Toolkit:** 3D View N-panel under **SM3 Materials**

## Workflow notes

- Preserve original target files before exporting replacements.
- Test one model or material change at a time.
- Keep PACK, APKF, hash, and output filename information with the exported resource.
- A successful Blender export still requires the correct xeSM3 mod path for in-game testing.
- Material previews do not prove that every in-game material route is correct.
- Keep personal game archives, Blender projects, and extracted copyrighted assets outside this repository.

Read each add-on's own README before using its advanced workflows:

- [SM3 Blender Toolkit documentation](SM3-Blender-Toolkit/README.md)
- [SM3 Material Combiner documentation](SM3-Material-Combiner/README.md)
- [Full and Lite workflow source-tree guide](Source-Trees/README.md)

## Project status and community help

The tools are usable foundations and are still open to improvement. Contributions are especially welcome for:

- Mesh import and export
- Skeleton workflows
- Materials and textures
- Blender compatibility
- Exporter reliability
- Usability and documentation

> I have a lot of faith in the Spider-Man modding community, and I hope people can continue improving the Blender side while building on the foundation these tools provide.

## Acknowledgments

- **Devryx** — for the Web of Shadows Blender Kit, an important reference and foundation for this work.
- **Haruse** — for Web of Shadows Blender tooling, texture-conversion workflows, and material/model research.
- The wider Spider-Man modding community for years of experimentation, testing, and shared knowledge.

## License

The extension manifests declare **GPL-3.0-or-later**. See [LICENSE](LICENSE).
