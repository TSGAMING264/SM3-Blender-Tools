from __future__ import annotations

"""Minimal SM3 DDS -> WRAP TEX converter.

The layout mirrors stock CH_SPIDERMAN WRAP TEX resources:
- WRAP archive hash: 0xCFB154CD
- TEX IMG block: 0x44 bytes
- PHYS payload: original DDS block-compressed bytes

Supported DDS FourCC: DXT1 / DXT3 / DXT5.
"""

from pathlib import Path
import re
import struct

SPIDERMAN_ARCHIVE_HASH = 0xCFB154CD
SUPPORTED_FOURCC = {b"DXT1", b"DXT3", b"DXT5"}


def sm3_hash(text: str) -> int:
    h = 0
    for ch in str(text):
        h = ((h * 33) + ord(ch.lower())) & 0xFFFFFFFF
    return h


def _parse_dds(path: str | Path) -> dict:
    p = Path(path)
    data = p.read_bytes()
    if len(data) < 128 or data[:4] != b"DDS ":
        raise ValueError("Not a standard DDS file")
    header_size = struct.unpack_from("<I", data, 4)[0]
    if header_size != 124:
        raise ValueError(f"Unsupported DDS header size: {header_size}")

    height = struct.unpack_from("<I", data, 12)[0]
    width = struct.unpack_from("<I", data, 16)[0]
    depth = struct.unpack_from("<I", data, 24)[0]
    mips = struct.unpack_from("<I", data, 28)[0]
    pf_size = struct.unpack_from("<I", data, 76)[0]
    pf_flags = struct.unpack_from("<I", data, 80)[0]
    fourcc = data[84:88]

    if pf_size != 32 or not (pf_flags & 0x4):
        raise ValueError("DDS must use a FourCC compressed format")
    if fourcc == b"DX10":
        raise ValueError("DX10 DDS headers are not supported; save as DXT1/DXT3/DXT5")
    if fourcc not in SUPPORTED_FOURCC:
        raise ValueError(f"Unsupported DDS FourCC: {fourcc!r}")
    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid DDS dimensions: {width}x{height}")

    payload = data[128:]
    if not payload:
        raise ValueError("DDS has no texture payload")

    return {
        "width": int(width),
        "height": int(height),
        "depth": max(1, int(depth)),
        "mips": max(1, int(mips)),
        "fourcc": bytes(fourcc),
        "payload": payload,
    }


def _resource_identity_from_dds_name(path: str | Path) -> tuple[int, str]:
    """Resolve the TEX hash from a WoS-style DDS filename.

    Preferred filename:
      0x8E661B33.ch_spiderman_spider.dds

    If no explicit 0x hash is present, the filename stem is hashed with the
    game's lowercase *33 string hash, matching the WoS Blender workflow.
    """
    stem = Path(path).stem
    m = re.match(r"^0x([0-9A-Fa-f]{8})(?:\.(.+))?$", stem)
    if m:
        tex_hash = int(m.group(1), 16)
        resource = (m.group(2) or f"0x{tex_hash:08X}").strip()
        return tex_hash, resource
    return sm3_hash(stem), stem


def build_wrap_tex_bytes(dds_path: str | Path, *, archive_hash: int = SPIDERMAN_ARCHIVE_HASH) -> tuple[bytes, dict]:
    info = _parse_dds(dds_path)
    tex_hash, resource_name = _resource_identity_from_dds_name(dds_path)
    payload = info["payload"]

    out = bytearray()

    # WRAP header, matching stock CH_SPIDERMAN TEX layout.
    out += b"WRAP"
    out += struct.pack("<I", int(archive_hash) & 0xFFFFFFFF)
    out += struct.pack("<I", 0x28)       # patch table pointer
    out += struct.pack("<I", 2)          # component count: IMG + PHYS
    out += struct.pack("<I", 4)          # components pointer
    out += struct.pack("<I", 0x44)       # IMG size
    out += struct.pack("<I", 0x48)       # IMG pointer
    out += struct.pack("<I", len(payload))
    out += struct.pack("<I", 0x88)       # PHYS pointer
    out += bytes(12)                       # align header to 0x30

    # Patch table used by stock SM3 TEX resources.
    out += struct.pack("<6I", 1, 0x1C, 0, 0x24, 0, 0x1C)
    out += bytes(8)                        # align external patch to 0x50

    # One NAME external patch.
    out += b"NAME"
    out += struct.pack("<I", 0)
    out += struct.pack("<I", 0xFFFFFFFF)
    out += struct.pack("<I", 0x0C)

    # TEX IMG block (0x44 bytes).
    out += bytes(8)
    out += struct.pack("<I", 0xFC0001FF)  # stock filename-pointer marker
    out += struct.pack("<I", tex_hash)
    out += bytes(8)
    out += struct.pack("<I", info["width"])
    out += struct.pack("<I", info["height"])
    out += struct.pack("<I", info["depth"])
    out += struct.pack("<I", info["mips"])
    out += info["fourcc"]
    out += bytes(24)
    out += b"PHYS"
    out += payload

    if out[:4] != b"WRAP" or out[0xA4:0xA8] != b"PHYS":
        raise AssertionError("Internal WRAP TEX build layout check failed")
    if struct.unpack_from("<I", out, 0x6C)[0] != tex_hash:
        raise AssertionError("Internal TEX hash verification failed")

    report = {
        "tex_hash": tex_hash,
        "resource_name": resource_name,
        "archive_hash": int(archive_hash) & 0xFFFFFFFF,
        "width": info["width"],
        "height": info["height"],
        "depth": info["depth"],
        "mips": info["mips"],
        "fourcc": info["fourcc"].decode("ascii"),
        "payload_size": len(payload),
        "file_size": len(out),
    }
    return bytes(out), report


def dds_to_wrap_tex(dds_path: str | Path, output_path: str | Path | None = None, *, archive_hash: int = SPIDERMAN_ARCHIVE_HASH) -> tuple[str, dict]:
    src = Path(dds_path)
    raw, report = build_wrap_tex_bytes(src, archive_hash=archive_hash)
    if output_path is None:
        output = src.with_name(src.stem + ".wrap.tex")
    else:
        output = Path(output_path)
    output.write_bytes(raw)
    report["output_path"] = str(output)
    return str(output), report
