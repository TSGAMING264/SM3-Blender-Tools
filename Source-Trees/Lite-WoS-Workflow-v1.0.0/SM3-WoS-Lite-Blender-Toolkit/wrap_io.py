from __future__ import annotations
from dataclasses import dataclass, asdict
from pathlib import Path
import struct
from typing import Iterable, List, Sequence

WRAP_MAGIC=b"WRAP"
MAX_COMPONENTS=64
MAX_PATCHES=1<<20
MAX_FLAT_BYTES=128*1024*1024


def _align(v,a=16): return (int(v)+(a-1)) & ~(a-1)
def _u32(b,o):
    if o<0 or o+4>len(b): raise ValueError(f"u32 outside file at 0x{o:X}")
    return struct.unpack_from('<I',b,o)[0]
def _s32(b,o):
    if o<0 or o+4>len(b): raise ValueError(f"s32 outside file at 0x{o:X}")
    return struct.unpack_from('<i',b,o)[0]
def _rel_target(b,field):
    t=field+_s32(b,field)
    if t<0 or t>len(b): raise ValueError(f"relative pointer at 0x{field:X} resolves outside file")
    return t

@dataclass
class WrapComponent:
    wrapper_offset:int
    size:int
    flat_offset:int
    separator_before:str=""
@dataclass
class WrapInfo:
    archive_hash:int
    component_count:int
    patch_table_offset:int
    component_table_offset:int
    external_count:int
    internal_count:int
    global_count:int
    components:List[WrapComponent]
    flat_size:int
    def to_dict(self):
        d=asdict(self); d['archive_hash']=f"0x{self.archive_hash:08X}"; return d


def is_wrap_bytes(data:bytes)->bool: return len(data)>=4 and data[:4]==WRAP_MAGIC
def is_wrap_file(path)->bool:
    try:
        with open(path,'rb') as f: return f.read(4)==WRAP_MAGIC
    except OSError: return False


def _map_wrap_to_flat(comps, wrapper_offset, need=1):
    for c in comps:
        if wrapper_offset < c.wrapper_offset: continue
        rel=wrapper_offset-c.wrapper_offset
        if rel <= c.size and need <= c.size-rel:
            return c.flat_offset+rel
    raise ValueError(f"WRAP offset 0x{wrapper_offset:X} is outside components")


def _map_flat_to_wrap(layout, flat_offset, need=1):
    for flat_start,size,wrap_start in layout:
        if flat_offset < flat_start: continue
        rel=flat_offset-flat_start
        if rel <= size and need <= size-rel:
            return wrap_start+rel
    raise ValueError(f"flat offset 0x{flat_offset:X} is outside components")


def unwrap_bytes(wrapped:bytes):
    if not is_wrap_bytes(wrapped): raise ValueError("not a WRAP resource")
    if len(wrapped)<0x14: raise ValueError("WRAP header truncated")
    archive_hash=_u32(wrapped,4)
    patch_off=_rel_target(wrapped,8)
    n=_u32(wrapped,0x0C)
    comp_table=_rel_target(wrapped,0x10)
    if not (1<=n<=MAX_COMPONENTS): raise ValueError(f"unsafe WRAP component count {n}")
    if comp_table+n*8>len(wrapped): raise ValueError("WRAP component table outside file")
    comps=[]; flat_size=0
    for i in range(n):
        e=comp_table+i*8; size=_u32(wrapped,e); off=_rel_target(wrapped,e+4)
        if size<=0 or off+size>len(wrapped): raise ValueError(f"component {i} outside file")
        if flat_size+size>MAX_FLAT_BYTES: raise ValueError("flattened WRAP exceeds safety ceiling")
        sep="PHYS" if off>=4 and wrapped[off-4:off]==b"PHYS" else ""
        comps.append(WrapComponent(off,size,flat_size,sep)); flat_size+=size
    flat=bytearray(flat_size)
    for c in comps: flat[c.flat_offset:c.flat_offset+c.size]=wrapped[c.wrapper_offset:c.wrapper_offset+c.size]
    if patch_off+0x18>len(wrapped): raise ValueError("WRAP patch table outside file")
    ext_n=_u32(wrapped,patch_off); ext_off=_rel_target(wrapped,patch_off+4)
    int_n=_u32(wrapped,patch_off+8); int_off=_rel_target(wrapped,patch_off+0x0C)
    glob_n=_u32(wrapped,patch_off+0x10); glob_off=_rel_target(wrapped,patch_off+0x14)
    if max(ext_n,int_n,glob_n)>MAX_PATCHES: raise ValueError("WRAP patch count exceeds ceiling")
    for off,count,stride,label in ((ext_off,ext_n,16,'external'),(int_off,int_n,4,'internal'),(glob_off,glob_n,16,'global')):
        if off<0 or off+count*stride>len(wrapped): raise ValueError(f"WRAP {label} table outside file")
    for i in range(int_n):
        pe=int_off+i*4
        field_wrap=_rel_target(wrapped,pe)
        field_flat=_map_wrap_to_flat(comps,field_wrap,4)
        target_wrap=field_wrap+_s32(wrapped,field_wrap)
        if target_wrap<0 or target_wrap>len(wrapped): raise ValueError("WRAP internal reference outside file")
        target_flat=_map_wrap_to_flat(comps,target_wrap,1)
        if target_flat>0xFFFFFFFF: raise ValueError("flat pointer exceeds uint32")
        struct.pack_into('<I',flat,field_flat,target_flat)
    info=WrapInfo(archive_hash,n,patch_off,comp_table,ext_n,int_n,glob_n,comps,flat_size)
    return bytes(flat),info


def unwrap_file(path): return unwrap_bytes(Path(path).read_bytes())


def _valid_local_pointer(raw:bytes, field:int):
    if field<0 or field+4>len(raw): return False
    value=_u32(raw,field)
    return value!=0 and value < len(raw)


def collect_mesh_internal_pointer_fields(raw:bytes):
    fields=[]
    if len(raw)<0x50: return fields
    section_count=_u32(raw,0x0C); section_table=_u32(raw,0x10)
    def add(field):
        if _valid_local_pointer(raw,field) and field not in fields: fields.append(field)
    add(0x10)
    if section_count>4096 or section_table+section_count*8>len(raw): return fields
    for i in range(section_count):
        pf=section_table+i*8+4; add(pf)
        info=_u32(raw,pf) if pf+4<=len(raw) else 0
        if not info or info+0x50>len(raw): continue
        for off in (0x24,0x2C,0x3C,0x4C): add(info+off)
        schema=_u32(raw,info+0x4C)
        if schema and schema+8<=len(raw): add(schema+4)
    return fields


def collect_skeleton_internal_pointer_fields(raw:bytes):
    return [0x0C] if len(raw)>=0x10 and _valid_local_pointer(raw,0x0C) else []


def build_wrap(flat:bytes, component_ranges:Sequence[tuple], *, internal_fields:Sequence[int]=(),
               external_patches:Sequence[dict]=(), global_patches:Sequence[dict]=(),
               archive_hash:int=0, separators=None):
    """Serialize a real WRAP with signed relative pointers and patch tables.

    component_ranges: [(flat_start, size), ...]. Components must cover the flat
    resource in order. separators maps component index -> bytes placed immediately
    before that component (MESH/TEX component 1 normally uses b'PHYS').
    """
    flat=bytes(flat); separators=dict(separators or {})
    ranges=[(int(a),int(s)) for a,s in component_ranges]
    if not ranges: raise ValueError("WRAP needs at least one component")
    expected=0
    for a,s in ranges:
        if a!=expected or s<=0 or a+s>len(flat): raise ValueError("components must contiguously cover flat bytes")
        expected=a+s
    if expected!=len(flat): raise ValueError("components do not cover entire flat resource")
    n=len(ranges)
    comp_table=0x14
    patch_off=_align(comp_table+n*8,16)
    # WoS WRAP writer aligns once after the 0x18-byte patch-table header.
    ext_start=_align(patch_off+0x18,16)
    int_start=ext_start+16*len(external_patches)
    glob_start=int_start+4*len(internal_fields)
    comp_data_start=_align(glob_start+16*len(global_patches),16)
    layout=[]; cur=comp_data_start
    for i,(flat_start,size) in enumerate(ranges):
        sep=bytes(separators.get(i,b'')); cur+=len(sep)
        layout.append((flat_start,size,cur)); cur+=size
    out=bytearray(cur)
    out[:4]=WRAP_MAGIC
    struct.pack_into('<I',out,4,int(archive_hash)&0xFFFFFFFF)
    struct.pack_into('<i',out,8,patch_off-8)
    struct.pack_into('<I',out,0x0C,n)
    struct.pack_into('<i',out,0x10,comp_table-0x10)
    for i,(flat_start,size,wrap_start) in enumerate(layout):
        e=comp_table+i*8; struct.pack_into('<I',out,e,size); struct.pack_into('<i',out,e+4,wrap_start-(e+4))
    # Patch table. Pointer fields are relative even for empty arrays and must resolve safely.
    struct.pack_into('<I',out,patch_off,len(external_patches)); struct.pack_into('<i',out,patch_off+4,ext_start-(patch_off+4))
    struct.pack_into('<I',out,patch_off+8,len(internal_fields)); struct.pack_into('<i',out,patch_off+0x0C,int_start-(patch_off+0x0C))
    struct.pack_into('<I',out,patch_off+0x10,len(global_patches)); struct.pack_into('<i',out,patch_off+0x14,glob_start-(patch_off+0x14))
    # Patch component-local pointers into WRAP-relative form and emit internal patch entries.
    comp_copies=[bytearray(flat[a:a+s]) for a,s in ranges]
    for i,field_flat in enumerate(internal_fields):
        field_flat=int(field_flat)
        if field_flat+4>len(flat): raise ValueError(f"internal field 0x{field_flat:X} outside flat resource")
        target_flat=_u32(flat,field_flat)
        if target_flat>=len(flat): raise ValueError(f"internal target 0x{target_flat:X} outside flat resource")
        field_wrap=_map_flat_to_wrap(layout,field_flat,4); target_wrap=_map_flat_to_wrap(layout,target_flat,1)
        rel=target_wrap-field_wrap
        # locate field in its component copy
        for ci,(fs,sz,ws) in enumerate(layout):
            if fs<=field_flat and field_flat+4<=fs+sz:
                struct.pack_into('<i',comp_copies[ci],field_flat-fs,rel); break
        pe=int_start+i*4; struct.pack_into('<i',out,pe,field_wrap-pe)
    def write_ext_like(start, entries):
        for i,p in enumerate(entries):
            e=start+i*16
            struct.pack_into('<III',out,e,int(p.get('type',0))&0xFFFFFFFF,int(p.get('filename_hash',0))&0xFFFFFFFF,int(p.get('expected_index',0xFFFFFFFF))&0xFFFFFFFF)
            tw=_map_flat_to_wrap(layout,int(p['target_flat_offset']),4)
            struct.pack_into('<i',out,e+0x0C,tw-(e+0x0C))
    write_ext_like(ext_start,external_patches); write_ext_like(glob_start,global_patches)
    # Place separators + components.
    for i,(fs,sz,ws) in enumerate(layout):
        sep=bytes(separators.get(i,b''))
        if sep: out[ws-len(sep):ws]=sep
        out[ws:ws+sz]=comp_copies[i]
    # Strong self-check: current xeSM3-style normalization must recover exact bytes.
    recovered,info=unwrap_bytes(bytes(out))
    if recovered!=flat: raise ValueError("WRAP round-trip normalization mismatch")
    return bytes(out),info


def wrap_mesh_bytes(raw:bytes, img_size:int, *, archive_hash:int=0):
    img_size=int(img_size)
    if img_size<=0 or img_size>=len(raw): raise ValueError("invalid MESH IMG/PHYS split")
    fields=collect_mesh_internal_pointer_fields(raw)
    return build_wrap(raw,[(0,img_size),(img_size,len(raw)-img_size)],internal_fields=fields,
                      archive_hash=archive_hash,separators={1:b"PHYS"})


def wrap_skeleton_bytes(raw:bytes, *, archive_hash:int=0):
    return build_wrap(raw,[(0,len(raw))],internal_fields=collect_skeleton_internal_pointer_fields(raw),archive_hash=archive_hash)
