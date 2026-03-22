# Display list format shared by CAR.RSO and CRS_*.DAT section-1 objects.
#
# A display list is a sequence of command blocks:
#   [+0x00] u16  cmd    - command type (see CMD_STRIDE)
#   [+0x02] u16  count  - number of records that follow
#   [+0x04] ...  count * stride bytes of polygon data
# Terminated by any block where count == 0.
#
# Command types and their per-record sizes:
#
#   CMD  Stride  Description
#    0     40    Flat textured quad
#    1     48    Flat textured quad + LOD variant
#    2     32    Flat colored quad (no texture)
#    3     64    Gouraud textured quad
#    4     72    Gouraud textured quad + LOD variant
#    5     56    Gouraud colored quad (no texture)
#
# Common record layout (all commands share the first 24 bytes):
#   [0..15]  4x (s16 X, s16 Y) - screen X/Y for vertices 0-3
#   [16..23] 4x s16 Z          - depth for vertices 0-3
#
# Textured commands (CMD 0, 1, 3, 4) - UV and material words:
#   For CMD 0, 1, 4:
#     [24] u8 U0, [25] u8 V0, [26..27] u16 CLUT
#     [28] u8 U1, [29] u8 V1, [30..31] u16 TPAGE
#     [32] u8 U2, [33] u8 V2
#     [36] u8 U3, [37] u8 V3
#   For CMD 3:
#     [48] u8 U0, [49] u8 V0, [50..51] u16 CLUT
#     [52] u8 U1, [53] u8 V1, [54..55] u16 TPAGE
#     [56] u8 U2, [57] u8 V2
#     [60] u8 U3, [61] u8 V3
#
# Colored commands (CMD 2, 5) - color word:
#   CMD 2: [24..27] u32  {R, G, B, 0}
#   CMD 5: [48..51] u32  {R, G, B, 0}
#
# TPAGE / CLUT word decoding:
#   tpage_x  = (tpage & 0x0F) * 64   - VRAM X of texture page
#   tpage_y  = ((tpage >> 4) & 1) * 256
#   tex_mode = (tpage >> 7) & 3      - 0=4bpp 1=8bpp 2=15bpp
#   clut_x   = (clut & 0x3F) * 16    - VRAM X of CLUT row
#   clut_y   = (clut >> 6) & 0x1FF   - VRAM Y of CLUT row

import struct
from dataclasses import dataclass, field


CMD_STRIDE = {0: 40, 1: 48, 2: 32, 3: 64, 4: 72, 5: 56}


@dataclass
class Poly:
    """One textured or colored quad extracted from a display list."""
    verts: list          # list of 4 (x, y, z) tuples in local/world space
    uvs: list            # list of 4 (u, v) tuples (0..255 each)
    tpage_x: int = 0     # VRAM X of the texture page
    tpage_y: int = 0     # VRAM Y of the texture page
    clut_x: int = 0      # VRAM X of the CLUT
    clut_y: int = 0      # VRAM Y of the CLUT
    mode: int = 0        # texture mode: 0=4bpp 1=8bpp 2=15bpp
    has_tex: bool = True
    color: tuple = (128, 128, 128)   # fallback RGB for untextured polys


def _decode_tpage(t: int) -> tuple:
    return (t & 0xF) * 64, ((t >> 4) & 1) * 256, (t >> 7) & 3


def _decode_clut(c: int) -> tuple:
    return (c & 0x3F) * 16, (c >> 6) & 0x1FF


def _parse_record(rec: bytes, cmd: int) -> Poly:
    """Build a Poly from one raw display-list record."""
    xs = [struct.unpack_from('<h', rec, j * 4)[0]     for j in range(4)]
    ys = [struct.unpack_from('<h', rec, j * 4 + 2)[0] for j in range(4)]
    zs = [struct.unpack_from('<h', rec, 16 + j * 2)[0] for j in range(4)]
    verts = list(zip(xs, ys, zs))

    if cmd == 2:
        w = struct.unpack_from('<I', rec, 24)[0]
        color = (w & 0xFF, (w >> 8) & 0xFF, (w >> 16) & 0xFF)
        return Poly(verts, [(0, 0)] * 4, has_tex=False, color=color)

    if cmd == 5:
        w = struct.unpack_from('<I', rec, 48)[0]
        color = (w & 0xFF, (w >> 8) & 0xFF, (w >> 16) & 0xFF)
        return Poly(verts, [(0, 0)] * 4, has_tex=False, color=color)

    # Textured: CMD 0, 1, 4 share the same UV offset; CMD 3 uses offset 48.
    if cmd == 3:
        base = 48
    else:
        base = 24

    w0 = struct.unpack_from('<I', rec, base)[0]
    w1 = struct.unpack_from('<I', rec, base + 4)[0]
    u0, v0 = w0 & 0xFF, (w0 >> 8) & 0xFF
    clut_word = (w0 >> 16) & 0xFFFF
    u1, v1 = w1 & 0xFF, (w1 >> 8) & 0xFF
    tpage_word = (w1 >> 16) & 0xFFFF
    stride = CMD_STRIDE[cmd]
    u2, v2 = rec[base + 8], rec[base + 9]
    u3, v3 = rec[base + 12], rec[base + 13]

    tx, ty, tp = _decode_tpage(tpage_word)
    cx, cy = _decode_clut(clut_word)
    return Poly(verts, [(u0, v0), (u1, v1), (u2, v2), (u3, v3)],
                tx, ty, cx, cy, tp, True)


def parse_display_list(data: bytes) -> list:
    """
    Parse a complete display list and return a list of Poly objects.
    Stops at the first block with count == 0 or an unknown command type.
    """
    polys = []
    pos = 0
    while pos + 4 <= len(data):
        cmd = struct.unpack_from('<H', data, pos)[0]
        cnt = struct.unpack_from('<H', data, pos + 2)[0]
        if cnt == 0:
            break
        stride = CMD_STRIDE.get(cmd, 0)
        if not stride:
            break
        base = pos + 4
        for i in range(cnt):
            rec = data[base + i * stride: base + (i + 1) * stride]
            if len(rec) >= stride:
                try:
                    polys.append(_parse_record(rec, cmd))
                except Exception:
                    pass
        pos = base + cnt * stride
    return polys
