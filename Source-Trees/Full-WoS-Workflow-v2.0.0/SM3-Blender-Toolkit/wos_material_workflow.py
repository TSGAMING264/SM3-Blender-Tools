from __future__ import annotations

import hashlib
import struct
import tempfile
from pathlib import Path
from typing import Optional, Tuple

import bpy

_SUPPORTED_FOURCC = {b"DXT1", b"DXT3", b"DXT5", b"BC1 ", b"BC2 ", b"BC3 "}


def _u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        return 0
    return struct.unpack_from("<I", data, offset)[0]


def _dds_header(width: int, height: int, mip_count: int, fourcc: bytes) -> bytes:
    if fourcc not in _SUPPORTED_FOURCC:
        raise ValueError(f"Unsupported compressed texture format: {fourcc!r}")
    block_bytes = 8 if fourcc in {b"DXT1", b"BC1 "} else 16
    linear = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * block_bytes

    DDSD_CAPS = 0x1
    DDSD_HEIGHT = 0x2
    DDSD_WIDTH = 0x4
    DDSD_PIXELFORMAT = 0x1000
    DDSD_MIPMAPCOUNT = 0x20000
    DDSD_LINEARSIZE = 0x80000
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_LINEARSIZE
    if mip_count > 1:
        flags |= DDSD_MIPMAPCOUNT

    caps = 0x1000
    if mip_count > 1:
        caps |= 0x400008

    out = bytearray(b"DDS ")
    out += struct.pack("<I", 124)
    out += struct.pack("<I", flags)
    out += struct.pack("<I", height)
    out += struct.pack("<I", width)
    out += struct.pack("<I", linear)
    out += struct.pack("<I", 0)  # depth
    out += struct.pack("<I", mip_count)
    out += bytes(44)
    out += struct.pack("<I", 32)
    out += struct.pack("<I", 0x4)  # DDPF_FOURCC
    out += fourcc
    out += struct.pack("<I", 0) * 5
    out += struct.pack("<I", caps)
    out += struct.pack("<I", 0) * 4
    if len(out) != 128:
        raise AssertionError("DDS header construction failed")
    return bytes(out)


def parse_sm3_tex(path: str) -> dict:
    """Parse the two texture containers seen in the SM3 research set.

    Supported preview inputs:
    - WRAP TEX: WRAP container + TEX header + PHYS + compressed payload.
    - Loose TEX: 68-byte TEX header followed directly by the DXT payload.

    This function is intentionally a *preview decoder*.  It does not rewrite the
    source game texture and it does not change dimensions, compression or mips.
    """
    p = Path(path)
    raw = p.read_bytes()
    if len(raw) < 68:
        raise ValueError("Texture file is too small")

    if raw[:4] == b"WRAP":
        # Layout proven by the supplied SM3 WRAP TEX files.
        tex_hash = _u32(raw, 0x6C)
        width = _u32(raw, 0x78)
        height = _u32(raw, 0x7C)
        depth = _u32(raw, 0x80)
        mips = _u32(raw, 0x84)
        fourcc = raw[0x88:0x8C]
        phys = raw.find(b"PHYS", 0x80, min(len(raw), 0x400))
        if phys < 0:
            raise ValueError("WRAP TEX has no PHYS payload marker")
        payload_offset = phys + 4
        container = "WRAP_TEX"
    else:
        # Loose SM3 TEX header used by the preserved research files.
        tex_hash = _u32(raw, 0x0C)
        width = _u32(raw, 0x18)
        height = _u32(raw, 0x1C)
        depth = _u32(raw, 0x20)
        mips = _u32(raw, 0x24)
        fourcc = raw[0x28:0x2C]
        payload_offset = 0x44
        container = "LOOSE_TEX"

    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid texture dimensions: {width}x{height}")
    if mips <= 0:
        mips = 1
    if fourcc not in _SUPPORTED_FOURCC:
        raise ValueError(f"Unsupported TEX FourCC: {fourcc!r}")
    if payload_offset >= len(raw):
        raise ValueError("Texture payload offset is outside the file")

    payload = raw[payload_offset:]
    return {
        "path": str(p),
        "container": container,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "tex_hash": tex_hash,
        "width": width,
        "height": height,
        "depth": depth,
        "mips": mips,
        "fourcc": fourcc,
        "payload_offset": payload_offset,
        "payload": payload,
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
    }


def decode_sm3_tex_to_temp_dds(path: str) -> Tuple[str, dict]:
    info = parse_sm3_tex(path)
    header = _dds_header(
        int(info["width"]),
        int(info["height"]),
        int(info["mips"]),
        bytes(info["fourcc"]),
    )
    digest = info["payload_sha256"][:12]
    source_name = Path(path).name.replace(" ", "_")
    out = Path(tempfile.gettempdir()) / f"SM3_WOS_PREVIEW_{digest}_{source_name}.dds"
    out.write_bytes(header + bytes(info["payload"]))
    info["preview_dds"] = str(out)
    return str(out), info


def load_image_for_preview(path: str) -> Tuple[bpy.types.Image, dict]:
    p = Path(path)
    if p.suffix.lower() == ".tex":
        dds_path, info = decode_sm3_tex_to_temp_dds(path)
        image = bpy.data.images.load(dds_path, check_existing=True)
        image.name = f"SM3::{p.name}"
        image["sm3_source_tex"] = str(p)
        image["sm3_tex_hash"] = f"0x{int(info['tex_hash']) & 0xFFFFFFFF:08X}"
        image["sm3_tex_container"] = info["container"]
        image["sm3_tex_payload_sha256"] = info["payload_sha256"]
    else:
        image = bpy.data.images.load(str(p), check_existing=True)
        info = {
            "path": str(p),
            "container": "IMAGE",
            "width": int(image.size[0]) if image.size else 0,
            "height": int(image.size[1]) if image.size else 0,
            "mips": 0,
            "fourcc": b"",
        }

    try:
        image.colorspace_settings.name = "sRGB"
    except Exception:
        pass
    return image, info


def get_diffuse_image(material: Optional[bpy.types.Material]) -> Optional[bpy.types.Image]:
    if material is None or not material.use_nodes or material.node_tree is None:
        return None
    nodes = material.node_tree.nodes

    # Match Material Combiner's preferred modern path: an image feeding the
    # Principled BSDF Base Color input.
    for node in nodes:
        if node.bl_idname != "ShaderNodeBsdfPrincipled":
            continue
        base = node.inputs.get("Base Color")
        if base and base.links:
            for link in base.links:
                src = link.from_node
                if src and src.bl_idname == "ShaderNodeTexImage" and getattr(src, "image", None):
                    return src.image

    # Fallback for imported/custom materials.
    for node in nodes:
        if node.bl_idname == "ShaderNodeTexImage" and getattr(node, "image", None):
            return node.image
    return None


def assign_image_to_material(
    material: bpy.types.Material,
    image: bpy.types.Image,
    uv_name: str = "",
) -> None:
    """Build a simple WoS/Material-Combiner-compatible diffuse material.

    The Material Combiner searches for an image connected to Base Color.  This
    helper deliberately creates exactly that ordinary Blender material setup.
    It does not change the model UVs.
    """
    if material is None:
        raise ValueError("No material selected")
    if image is None:
        raise ValueError("No image selected")

    material.use_nodes = True
    tree = material.node_tree
    nodes = tree.nodes
    links = tree.links

    output = next((n for n in nodes if n.bl_idname == "ShaderNodeOutputMaterial"), None)
    if output is None:
        output = nodes.new("ShaderNodeOutputMaterial")
        output.location = (420, 0)

    bsdf = next((n for n in nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"), None)
    if bsdf is None:
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.location = (120, 0)

    if not output.inputs["Surface"].links:
        links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])

    tex = nodes.get("SM3_WOS_DIFFUSE")
    if tex is None or tex.bl_idname != "ShaderNodeTexImage":
        tex = nodes.new("ShaderNodeTexImage")
        tex.name = "SM3_WOS_DIFFUSE"
        tex.label = "WoS Diffuse / Material Combiner Source"
    tex.location = (-420, 30)
    tex.image = image
    try:
        tex.interpolation = "Linear"
        tex.extension = "REPEAT"
    except Exception:
        pass

    base = bsdf.inputs.get("Base Color")
    if base is not None:
        for link in list(base.links):
            links.remove(link)
        links.new(tex.outputs["Color"], base)

    alpha = bsdf.inputs.get("Alpha")
    if alpha is not None and tex.outputs.get("Alpha") is not None:
        for link in list(alpha.links):
            links.remove(link)
        links.new(tex.outputs["Alpha"], alpha)

    if uv_name:
        uv = nodes.get("SM3_WOS_UV")
        if uv is None or uv.bl_idname != "ShaderNodeUVMap":
            uv = nodes.new("ShaderNodeUVMap")
            uv.name = "SM3_WOS_UV"
            uv.label = "Current Source UV"
        uv.location = (-650, 30)
        uv.uv_map = uv_name
        vec = tex.inputs.get("Vector")
        if vec is not None:
            for link in list(vec.links):
                links.remove(link)
            links.new(uv.outputs["UV"], vec)

    # Avoid tinting the selected diffuse image in the Material Combiner route.
    try:
        bsdf.inputs["Base Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    except Exception:
        pass
    try:
        material.diffuse_color = (1.0, 1.0, 1.0, 1.0)
    except Exception:
        pass

    material["sm3_wos_image_name"] = image.name
    material["sm3_wos_image_path"] = str(getattr(image, "filepath", "") or "")
    if image.get("sm3_source_tex"):
        material["sm3_wos_source_tex"] = str(image.get("sm3_source_tex"))


def material_face_count(obj: bpy.types.Object, slot_index: int) -> int:
    if obj is None or obj.type != "MESH":
        return 0
    return sum(1 for poly in obj.data.polygons if poly.material_index == slot_index)
