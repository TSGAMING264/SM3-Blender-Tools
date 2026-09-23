from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import struct
from typing import Dict, Tuple


DDS_MAGIC = b"DDS "
DDPF_ALPHAPIXELS = 0x1
DDPF_FOURCC = 0x4
DDPF_RGB = 0x40
DDPF_LUMINANCE = 0x20000

SM3_TEX_IMG_SIZE = 0x44
SM3_TEX_DATA_OFFSET = 0x48  # 0x44-byte IMG + literal PHYS


@dataclass
class DDSInfo:
    width: int
    height: int
    depth: int
    mip_count: int
    pixel_flags: int
    fourcc: bytes
    rgb_bit_count: int
    r_mask: int
    g_mask: int
    b_mask: int
    a_mask: int
    data: bytes


@dataclass
class SM3TEXInfo:
    filename_hash: int
    width: int
    height: int
    depth: int
    mip_count: int
    format_bytes: bytes
    data: bytes


def hash33(name: str) -> int:
    """Beenox name hash used by WoS and SM3 resource names."""
    value = 0
    for ch in str(name):
        if "A" <= ch <= "Z":
            ch = chr(ord(ch) + 0x20)
        value = (value * 33 + ord(ch)) & 0xFFFFFFFF
    return value


def _target_hash_from_stem(stem: str) -> int:
    """Use an explicit 0xHASH prefix, otherwise hash the DDS basename.

    This mirrors the uploaded WoS toolkit: DDS ``spiderman_suit.dds`` becomes a
    TEX whose internal filename hash is Hash("spiderman_suit"). If the DDS is
    already named ``0x12345678.spiderman_suit.dds``, the explicit hash wins.
    """
    m = re.match(r"^0x([0-9A-Fa-f]{8})(?:\.|$)", stem)
    if m:
        return int(m.group(1), 16)
    return hash33(stem)


def read_dds(path: str | Path) -> DDSInfo:
    raw = Path(path).read_bytes()
    if len(raw) < 128 or raw[:4] != DDS_MAGIC:
        raise ValueError("Not a standard DDS file (missing 128-byte DDS header)")

    dw_size = struct.unpack_from("<I", raw, 4)[0]
    if dw_size != 124:
        raise ValueError(f"Unsupported DDS header size {dw_size}; expected 124")

    height = struct.unpack_from("<I", raw, 12)[0]
    width = struct.unpack_from("<I", raw, 16)[0]
    depth = struct.unpack_from("<I", raw, 24)[0]
    mip_count = struct.unpack_from("<I", raw, 28)[0] or 1

    pf_off = 76
    pf_size, pf_flags = struct.unpack_from("<II", raw, pf_off)
    if pf_size != 32:
        raise ValueError(f"Unsupported DDS pixel-format size {pf_size}; expected 32")
    fourcc = raw[pf_off + 8: pf_off + 12]
    rgb_bit_count, r_mask, g_mask, b_mask, a_mask = struct.unpack_from("<5I", raw, pf_off + 12)

    if fourcc == b"DX10":
        raise ValueError(
            "DX10 DDS is not supported by the native SM3 TEX header. "
            "Save as legacy DXT1/DXT3/DXT5 (or another legacy FourCC) first."
        )

    return DDSInfo(
        width=int(width),
        height=int(height),
        depth=int(depth),
        mip_count=int(mip_count),
        pixel_flags=int(pf_flags),
        fourcc=bytes(fourcc),
        rgb_bit_count=int(rgb_bit_count),
        r_mask=int(r_mask),
        g_mask=int(g_mask),
        b_mask=int(b_mask),
        a_mask=int(a_mask),
        data=raw[128:],
    )


def _dds_to_sm3_format(dds: DDSInfo) -> bytes:
    if dds.pixel_flags & DDPF_FOURCC:
        if dds.fourcc == b"\x00\x00\x00\x00":
            raise ValueError("DDS marks FOURCC but contains a zero FourCC")
        # SM3 stores the same legacy 4-byte D3D format/FourCC value.
        return dds.fourcc

    if dds.pixel_flags & DDPF_RGB:
        if dds.rgb_bit_count == 32:
            if dds.pixel_flags & DDPF_ALPHAPIXELS or dds.a_mask:
                return struct.pack("<I", 0x15)  # D3DFMT_A8R8G8B8
            return struct.pack("<I", 0x16)      # D3DFMT_X8R8G8B8

    if dds.pixel_flags & DDPF_LUMINANCE and dds.rgb_bit_count == 8:
        return struct.pack("<I", 0x32)          # D3DFMT_L8

    raise ValueError(
        "Unsupported DDS pixel format for SM3 NativeTEX. "
        "Supported: legacy FourCC (DXT1/DXT3/DXT5/etc.), A8R8G8B8, X8R8G8B8, L8."
    )


def build_sm3_tex_from_dds(path: str | Path, *, filename_hash: int | None = None) -> Tuple[bytes, Dict[str, object]]:
    path = Path(path)
    dds = read_dds(path)
    fmt = _dds_to_sm3_format(dds)
    if filename_hash is None:
        filename_hash = _target_hash_from_stem(path.stem)

    img = bytearray(SM3_TEX_IMG_SIZE)
    # 0x00..0x07 unused = zero
    struct.pack_into("<I", img, 0x08, 0)  # filename pointer (runtime-fixed by game/loader)
    struct.pack_into("<I", img, 0x0C, int(filename_hash) & 0xFFFFFFFF)
    # 0x10..0x17 unused = zero
    struct.pack_into("<I", img, 0x18, dds.width)
    struct.pack_into("<I", img, 0x1C, dds.height)
    struct.pack_into("<I", img, 0x20, dds.depth)
    struct.pack_into("<I", img, 0x24, dds.mip_count)
    img[0x28:0x2C] = fmt
    # 0x2C..0x43 unused = zero

    out = bytes(img) + b"PHYS" + dds.data
    report = {
        "mode": "SM3_NATIVE_TEX_FROM_DDS",
        "source": str(path),
        "filename_hash": f"0x{int(filename_hash) & 0xFFFFFFFF:08X}",
        "hash_from_dds_name_or_explicit_prefix": True,
        "width": dds.width,
        "height": dds.height,
        "depth": dds.depth,
        "mip_count": dds.mip_count,
        "format_bytes_hex": fmt.hex().upper(),
        "format_fourcc": fmt.decode("ascii", errors="replace") if all(32 <= b < 127 for b in fmt) else None,
        "img_size": SM3_TEX_IMG_SIZE,
        "phys_size": len(dds.data),
        "file_size": len(out),
    }
    return out, report


def write_sm3_tex_from_dds(source_path: str | Path, output_path: str | Path | None = None):
    source = Path(source_path)
    if output_path is None:
        output = source.with_suffix(".tex")
    else:
        output = Path(output_path)
    raw, report = build_sm3_tex_from_dds(source)
    output.write_bytes(raw)
    report["output"] = str(output)
    return report


def read_sm3_tex(path: str | Path) -> SM3TEXInfo:
    raw = Path(path).read_bytes()
    if len(raw) < SM3_TEX_DATA_OFFSET:
        raise ValueError("File is too small to be an SM3 raw TEX")
    if raw[0x44:0x48] != b"PHYS":
        raise ValueError("Not an SM3 raw TEX: expected PHYS at offset 0x44")
    filename_hash = struct.unpack_from("<I", raw, 0x0C)[0]
    width, height, depth, mip_count = struct.unpack_from("<4I", raw, 0x18)
    fmt = raw[0x28:0x2C]
    return SM3TEXInfo(
        filename_hash=filename_hash,
        width=width,
        height=height,
        depth=depth,
        mip_count=mip_count,
        format_bytes=fmt,
        data=raw[SM3_TEX_DATA_OFFSET:],
    )


def _sm3_format_to_dds_pf(fmt: bytes):
    # Returns (flags, fourcc, rgb_bits, r, g, b, a)
    if fmt in (b"DXT1", b"DXT3", b"DXT5", b"ATI1", b"ATI2", b"BC4U", b"BC5U"):
        return (DDPF_FOURCC, fmt, 32, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    value = struct.unpack("<I", fmt)[0]
    if value == 0x15:  # A8R8G8B8
        return (DDPF_RGB | DDPF_ALPHAPIXELS, b"\x00" * 4, 32, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    if value == 0x16:  # X8R8G8B8
        return (DDPF_RGB, b"\x00" * 4, 32, 0x00FF0000, 0x0000FF00, 0x000000FF, 0)
    if value == 0x32:  # L8
        return (DDPF_LUMINANCE, b"\x00" * 4, 8, 0xFF, 0, 0, 0)
    # Preserve unknown printable legacy FourCCs on export.
    if all(32 <= b < 127 for b in fmt):
        return (DDPF_FOURCC, fmt, 32, 0, 0, 0, 0)
    raise ValueError(f"Unsupported SM3 TEX format bytes: {fmt.hex().upper()}")


def build_dds_from_sm3_tex(path: str | Path) -> Tuple[bytes, Dict[str, object]]:
    path = Path(path)
    tex = read_sm3_tex(path)
    pf_flags, fourcc, rgb_bits, r, g, b, a = _sm3_format_to_dds_pf(tex.format_bytes)

    header = bytearray(128)
    header[0:4] = DDS_MAGIC
    struct.pack_into("<I", header, 4, 124)
    struct.pack_into("<I", header, 8, 135175)  # mirrors WoS toolkit legacy DDS output
    struct.pack_into("<I", header, 12, tex.height)
    struct.pack_into("<I", header, 16, tex.width)
    struct.pack_into("<I", header, 20, 0)
    struct.pack_into("<I", header, 24, 0)
    struct.pack_into("<I", header, 28, tex.mip_count)
    pf_off = 76
    struct.pack_into("<II", header, pf_off, 32, pf_flags)
    header[pf_off + 8:pf_off + 12] = fourcc
    struct.pack_into("<5I", header, pf_off + 12, rgb_bits, r, g, b, a)
    struct.pack_into("<I", header, 108, 4198408)

    out = bytes(header) + tex.data
    report = {
        "mode": "DDS_FROM_SM3_NATIVE_TEX",
        "source": str(path),
        "filename_hash": f"0x{tex.filename_hash:08X}",
        "width": tex.width,
        "height": tex.height,
        "mip_count": tex.mip_count,
        "format_bytes_hex": tex.format_bytes.hex().upper(),
        "phys_size": len(tex.data),
        "file_size": len(out),
    }
    return out, report


def write_dds_from_sm3_tex(source_path: str | Path, output_path: str | Path | None = None):
    source = Path(source_path)
    if output_path is None:
        output = source.with_suffix(".dds")
    else:
        output = Path(output_path)
    raw, report = build_dds_from_sm3_tex(source)
    output.write_bytes(raw)
    report["output"] = str(output)
    return report


def convert_texture_file(path: str | Path):
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".dds":
        return write_sm3_tex_from_dds(p)
    if ext == ".tex":
        return write_dds_from_sm3_tex(p)
    raise ValueError(f"Unsupported texture extension: {p.suffix}")
