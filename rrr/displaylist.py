# Display list format shared by CAR.RSO and CRS_*.DAT.
#
# A display list is a sequence of command blocks:
#
#   [+0x00] u16  command type
#   [+0x02] u16  number of records that follow
#   [+0x04] ...  count * stride bytes of polygon records
#
# The list ends at the first block whose count is zero.
#
# Command types and record sizes for the object renderer, used by CAR.RSO and
# CRS section 1:
#
#   0   40   flat textured quad
#   1   48   flat textured quad with texture window
#   2   32   flat coloured quad
#   3   64   gouraud textured quad
#   4   72   gouraud textured quad with texture window
#   5   56   gouraud coloured quad
#
# CRS section 0 is drawn by a separate renderer that treats every command as a
# 40-byte textured quad, so it needs ROAD_CMD_STRIDE instead.
#
# All records begin with the same 24 bytes of geometry:
#
#   [0..15]  four (s16 X, s16 Y) pairs, one per vertex
#   [16..23] four s16 Z values, one per vertex
#
# Textured records then hold UVs and material words. The flat commands put
# them at offset 24; the gouraud commands 3 and 4 put them at 48, after their
# 24 bytes of per-vertex colour:
#
#   [+0] u8 U0, [+1] u8 V0, [+2..3] u16 CLUT
#   [+4] u8 U1, [+5] u8 V1, [+6..7] u16 TPAGE
#   [+8] u8 U2, [+9] u8 V2
#   [+12] u8 U3, [+13] u8 V3
#
# Coloured records hold one u32 {R, G, B, 0} at offset 24 for command 2 and at
# offset 48 for command 5.
#
# Decoding the material words:
#
#   tpage_x  = (tpage & 0x0F) * 64
#   tpage_y  = ((tpage >> 4) & 1) * 256
#   tex_mode = (tpage >> 7) & 3        0 = 4bpp, 1 = 8bpp, 2 = 15bpp
#   clut_x   = (clut & 0x3F) * 16
#   clut_y   = (clut >> 6) & 0x1FF

import struct
from dataclasses import dataclass


# Record stride per command for the object renderer.
CMD_STRIDE = {0: 40, 1: 48, 2: 32, 3: 64, 4: 72, 5: 56}

# Record stride per command for the road renderer.
ROAD_CMD_STRIDE = {0: 40, 1: 40, 2: 40}

# Commands that carry a vertex colour instead of a texture.
_OBJ_COLORED_CMDS = frozenset({2, 5})


@dataclass
class Poly:
    """One textured or coloured quad extracted from a display list."""
    verts: list          # four (x, y, z) tuples, local or world space
    uvs: list            # four (u, v) tuples, 0 to 255 each
    tpage_x: int = 0     # VRAM X of the texture page
    tpage_y: int = 0     # VRAM Y of the texture page
    clut_x: int = 0      # VRAM X of the palette row
    clut_y: int = 0      # VRAM Y of the palette row
    mode: int = 0        # 0 = 4bpp, 1 = 8bpp, 2 = 15bpp
    has_tex: bool = True
    color: tuple = (128, 128, 128)   # used when has_tex is False
    twin: tuple = None   # texture window (off_x, off_y, w, h), or None


def _decode_tpage(t: int) -> tuple:
    """Split a TPAGE word into (vram_x, vram_y, pixel_mode)."""
    return (t & 0xF) * 64, ((t >> 4) & 1) * 256, (t >> 7) & 3


def _decode_clut(c: int) -> tuple:
    """Split a CLUT word into (vram_x, vram_y)."""
    return (c & 0x3F) * 16, (c >> 6) & 0x1FF


def _parse_verts(rec: bytes):
    """Extract the four (X, Y, Z) vertices common to every record type."""
    xs = [struct.unpack_from('<h', rec, j * 4)[0]     for j in range(4)]
    ys = [struct.unpack_from('<h', rec, j * 4 + 2)[0] for j in range(4)]
    zs = [struct.unpack_from('<h', rec, 16 + j * 2)[0] for j in range(4)]
    return list(zip(xs, ys, zs))


def _parse_tex(rec: bytes, base: int):
    """Extract the UVs and decoded TPAGE/CLUT from a textured record."""
    w0 = struct.unpack_from('<I', rec, base)[0]
    w1 = struct.unpack_from('<I', rec, base + 4)[0]
    u0, v0 = w0 & 0xFF, (w0 >> 8) & 0xFF
    clut_word = (w0 >> 16) & 0xFFFF
    u1, v1 = w1 & 0xFF, (w1 >> 8) & 0xFF
    tpage_word = (w1 >> 16) & 0xFFFF
    u2, v2 = rec[base + 8], rec[base + 9]
    u3, v3 = rec[base + 12], rec[base + 13]

    tx, ty, tp = _decode_tpage(tpage_word)
    cx, cy = _decode_clut(clut_word)
    return [(u0, v0), (u1, v1), (u2, v2), (u3, v3)], tx, ty, cx, cy, tp


def _parse_record(rec: bytes, cmd: int, colored_cmds: frozenset) -> Poly:
    """Build a Poly from one raw display-list record."""
    verts = _parse_verts(rec)

    if cmd in colored_cmds:
        col_off = 48 if cmd == 5 else 24
        w = struct.unpack_from('<I', rec, col_off)[0]
        color = (w & 0xFF, (w >> 8) & 0xFF, (w >> 16) & 0xFF)
        return Poly(verts, [(0, 0)] * 4, has_tex=False, color=color)

    # The gouraud commands carry 24 bytes of per-vertex colour between the
    # geometry and the UV block, so their material data starts at 48.
    base = 48 if cmd in (3, 4) else 24
    uvs, tx, ty, cx, cy, tp = _parse_tex(rec, base)

    # Commands 1 and 4 end with eight bytes holding the PS1 texture window:
    # four u16 giving offset_x, offset_y, width and height in texels. The GPU
    # wraps texture coordinates inside that rectangle, so a quad whose U runs
    # to 252 against a 64-wide window repeats a 64-pixel texture four times
    # instead of stretching a quarter of the page across the face. Width and
    # height are always powers of two and the rectangle always lies inside the
    # 256 by 256 page.
    twin = None
    if cmd in (1, 4):
        wbase = 40 if cmd == 1 else 64
        if len(rec) >= wbase + 8:
            ox, oy, w, h = struct.unpack_from('<4H', rec, wbase)
            if w and h and ox + w <= 256 and oy + h <= 256:
                twin = (ox, oy, w, h)

    return Poly(verts, uvs, tx, ty, cx, cy, tp, True, twin=twin)


def _parse_display_list_impl(data: bytes, stride_table: dict,
                             colored_cmds: frozenset) -> list:
    """
    Parse a display list using the given stride table.

    Stops at a zero record count or at an unrecognised command type.
    """
    polys = []
    pos = 0
    while pos + 4 <= len(data):
        cmd = struct.unpack_from('<H', data, pos)[0]
        cnt = struct.unpack_from('<H', data, pos + 2)[0]
        if cnt == 0:
            break
        stride = stride_table.get(cmd, 0)
        if not stride:
            break
        base = pos + 4
        for i in range(cnt):
            rec = data[base + i * stride: base + (i + 1) * stride]
            if len(rec) >= stride:
                try:
                    polys.append(_parse_record(rec, cmd, colored_cmds))
                except Exception:
                    pass
        pos = base + cnt * stride
    return polys


def parse_display_list(data: bytes) -> list:
    """
    Parse an object display list, as used by CAR.RSO and CRS section 1.

    Commands 2 and 5 are vertex coloured; the rest are textured.
    """
    return _parse_display_list_impl(data, CMD_STRIDE, _OBJ_COLORED_CMDS)


def parse_road_display_list(data: bytes) -> list:
    """
    Parse a road segment display list, as used by CRS section 0.

    Every command is a 40-byte textured quad here.
    """
    return _parse_display_list_impl(data, ROAD_CMD_STRIDE, frozenset())
