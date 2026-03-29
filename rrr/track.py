# CRS_*.DAT track format parser.
#
# Each course file has six sections, pointed to by six u32 offsets at byte 0:
#
#   Section 0  Road display lists (147 segments, streaming cache).
#   Section 1  Track-local object library (122 entries, display list format).
#   Section 2  Course texture upload: 384 halfwords wide x 256 rows,
#              placed into VRAM at (640, 256).
#   Section 3  Alternate texture upload (double-buffered companion to sec 2).
#   Section 4  Object placement table (stride 20 bytes per record).
#   Section 5  Sub-section pointer table with 25 entries.
#              sub[20] = the road spine (264 nodes, stride 20 bytes).
#
# --- ROAD SPINE (section 5, sub[20]) ---
#
# The spine defines the road centerline as a closed loop of 264 nodes.
# Each node is 20 bytes (confirmed from RIDGE.EXE decompile FUN_80038314):
#
#   [0..3]   s32  f0:  world X = 0xF000 - (f0 >> 14)
#   [4..7]   s32  f1:  world Z = f1 >> 14
#   [8..9]   s16  wy:  world Y (elevation, world units)
#   [10..11] s16  heading: PS1 angle (4096 = 360 deg), road direction
#   [12..13] s16  f4:  small width deviation (-11..12)
#   [14..15] s16  f5:  road half-width (world units / 4)
#   [16..19]       reserved / flags
#
# Road ribbon half-width = f5 / 4 world units (typical: 3504/4 = 876).
# The road is closed: node 263 connects back to node 0.
#
# The spine is the authoritative source for road geometry.
# Section-0 display lists are streaming cache segments (one per camera tile),
# each covering a large overlapping area around its tile position.
# They are NOT directly usable as world-space geometry tiles.
#
# --- TILE GRID (at file offset 0x18) ---
#
# 32x32 signed 16-bit values, each is a section-0 segment index (or -1).
# Grid index formula (from RIDGE.EXE decompile FUN_80042f7c):
#   gi = (tile_row * 32 + 30) - tile_col
# Tile world origin: tile_col = 30 - (gi % 32), tile_row = gi // 32.
# Used by the game to pick which display list to stream near the camera.
#
# --- OBJECT PLACEMENTS (section 4) ---
#
# 20-byte records:
#   [0..1]   u16  section-1 entry index
#   [2..3]   u16  Y-axis rotation angle (4096 = 360 deg)
#   [4..7]   s32  world X
#   [8..11]  s32  world Y
#   [12..15] s32  world Z
#   [16..19] s32  flags (meaning unknown)
# Sentinel: entry_index >= s1_count OR (world_X == 0 AND world_Z == 0).
#
# Object vertex placement (section-1 vertices are in GTE units, divide by 4):
#   rotated_X, rotated_Z = rotate_y(vertex_X, vertex_Z, angle)
#   world_X = rotated_X / 4 + placement_X
#   world_Y = vertex_Y   / 4 + placement_Y
#   world_Z = rotated_Z  / 4 + placement_Z
#
# --- *_PCT.DAT / *_CT.DAT - Course CLUT Banks ---
#
# Sequence of upload records, terminated by size == 0:
#   [u32 size][u16 vram_x][u16 vram_y][u16 w][u16 h][raw ABGR1555 data]
#
# Load order per course (PCT first, CT last -- last write wins):
#   CRS_EASY  -> EASY_PCT.DAT, EASY_CT.DAT
#   CRS_MID   -> MID_PCT.DAT
#   CRS_HIGH  -> HIGH_PCT.DAT
#   CRS_OLDE  -> OLD_PCT.DAT
#   CRS_OLDH  -> OLD_PCT.DAT

import struct
import math
from rrr.displaylist import parse_display_list, Poly, _decode_tpage, _decode_clut

GTE_SCALE = 4      # GTE units per world unit (for object vertices)
GRID_SIZE = 32
F0_FLOOR  = 0xF000 # constant from RIDGE.EXE (DAT_801dc9b0 = 61440)

CLUT_FILES = {
    'CRS_EASY': ['EASY_PCT.DAT', 'EASY_CT.DAT'],
    'CRS_MID':  ['MID_PCT.DAT'],
    'CRS_HIGH': ['HIGH_PCT.DAT'],
    'CRS_OLDE': ['OLD_PCT.DAT'],
    'CRS_OLDH': ['OLD_PCT.DAT'],
}


def _sin(angle):
    """PS1 angle to sin. 4096 units = 360 degrees."""
    return math.sin(angle * math.pi / 2048)


def _cos(angle):
    return math.cos(angle * math.pi / 2048)


def _rotate_y(x, z, angle):
    """Rotate a (x, z) pair around Y by a PS1 angle."""
    s, c = _sin(angle), _cos(angle)
    return x * c + z * s, -x * s + z * c


def _normalize2(dx, dz):
    mag = math.sqrt(dx * dx + dz * dz)
    if mag < 0.001:
        return 1.0, 0.0
    return dx / mag, dz / mag


def load_course_textures(crs_data, vram, clut_files=None):
    """
    Upload course-specific textures into an existing VramSim.

    Section 2 contains 384x256 halfwords of raw VRAM data placed at (640, 256).
    Each CLUT file is a sequence of upload records loaded on top.
    """
    sec = struct.unpack_from('<6I', crs_data, 0)
    sec2 = crs_data[sec[2]: sec[3]]
    expected = 384 * 256 * 2
    if len(sec2) == expected:
        vram.load_rect(640, 256, 384, 256, sec2)
        print('  course textures: 384x256 -> VRAM(640,256)')
    else:
        print(f'  course textures: unexpected size {len(sec2)} (expected {expected})')

    for clut_data in (clut_files or []):
        pos = 0; count = 0
        while pos + 12 <= len(clut_data):
            sz = struct.unpack_from('<I', clut_data, pos)[0]
            if sz == 0 or sz > 10_000_000:
                break
            x = struct.unpack_from('<H', clut_data, pos + 4)[0]
            y = struct.unpack_from('<H', clut_data, pos + 6)[0]
            w = struct.unpack_from('<H', clut_data, pos + 8)[0]
            h = struct.unpack_from('<H', clut_data, pos + 10)[0]
            if x < 1024 and y < 512 and w < 2048 and h < 512:
                vram.load_pct_block(x, y, w, h, clut_data[pos + 12: pos + sz])
                count += 1
            pos += sz
        if count:
            print(f'  CLUT file: {count} records loaded')


def _read_spine(crs_data):
    """
    Parse the road spine from section-5 sub[20].

    Returns a list of dicts with keys:
        wx, wy, wz  -- world X/Y/Z position
        heading     -- PS1 angle (4096 = 360 deg)
        hw          -- road half-width in world units (f5 / 4)
    """
    sec = struct.unpack_from('<6I', crs_data, 0)
    s5_off = sec[5]
    sub20_rel = struct.unpack_from('<I', crs_data, s5_off + 20 * 4)[0]
    spine_abs = s5_off + sub20_rel
    count = struct.unpack_from('<I', crs_data, spine_abs)[0]

    nodes = []
    for i in range(count):
        off = spine_abs + 4 + i * 20
        f0 = struct.unpack_from('<i', crs_data, off)[0]
        f1 = struct.unpack_from('<i', crs_data, off + 4)[0]
        wy = struct.unpack_from('<h', crs_data, off + 8)[0]
        heading = struct.unpack_from('<h', crs_data, off + 10)[0]
        f5 = struct.unpack_from('<h', crs_data, off + 14)[0]
        if f0 < 0: f0 += 0x3FFF
        if f1 < 0: f1 += 0x3FFF
        nodes.append({
            'wx': F0_FLOOR - (f0 >> 14),
            'wy': wy,
            'wz': f1 >> 14,
            'heading': heading,
            'hw': f5 // GTE_SCALE,   # convert from GTE units to world units
        })
    return nodes


def _build_road_polys(spine):
    """
    Build road ribbon quads from the spine centerline.

    Each pair of consecutive spine nodes becomes one quad spanning the road
    width. The perpendicular direction is derived from the tangent between
    adjacent nodes, so the ribbon follows the road smoothly.

    Returns a list of Poly objects (no texture -- just geometry).
    """
    n = len(spine)
    polys = []

    def tangent(i):
        a = spine[(i - 1) % n]
        b = spine[(i + 1) % n]
        return _normalize2(b['wx'] - a['wx'], b['wz'] - a['wz'])

    for i in range(n):
        j = (i + 1) % n
        a = spine[i]
        b = spine[j]
        # Perpendicular at each node (road width direction)
        tax, taz = tangent(i)
        tbx, tbz = tangent(j)
        pax, paz = -taz, tax
        pbx, pbz = -tbz, tbx
        hw_a = max(a['hw'], 100)   # minimum reasonable width
        hw_b = max(b['hw'], 100)

        # Four corners of the road quad
        al = (a['wx'] + hw_a * pax, a['wy'], a['wz'] + hw_a * paz)
        ar = (a['wx'] - hw_a * pax, a['wy'], a['wz'] - hw_a * paz)
        bl = (b['wx'] + hw_b * pbx, b['wy'], b['wz'] + hw_b * pbz)
        br = (b['wx'] - hw_b * pbx, b['wy'], b['wz'] - hw_b * pbz)

        polys.append(Poly(
            [al, ar, bl, br],
            [(0, 0)] * 4,
            has_tex=False,
            color=(80, 80, 80),
        ))
    return polys


def parse_crs(data):
    """
    Parse a CRS_*.DAT file.

    Returns:
        road_polys       - list of Poly for the road surface (spine ribbon).
        named_placements - list of (name, [Poly]) for placed objects (section-4).
    """
    sec = struct.unpack_from('<6I', data, 0)
    s1_off = sec[1]
    s4_off = sec[4]
    s4_end = sec[5]

    # Road centerline ribbon from spine
    spine = _read_spine(data)
    road_polys = _build_road_polys(spine)
    print(f'  road spine: {len(spine)} nodes -> {len(road_polys)} ribbon quads')

    # Object library (section 1)
    s1_n = struct.unpack_from('<I', data, s1_off)[0]
    s1_offsets = [struct.unpack_from('<I', data, s1_off + 4 + i * 4)[0]
                  for i in range(s1_n)]
    obj_library = []
    for i in range(s1_n):
        start = s1_off + s1_offsets[i]
        end = s1_off + (s1_offsets[i + 1] if i + 1 < s1_n else sec[2] - s1_off)
        obj_library.append(parse_display_list(data[start: min(end, len(data))]))

    # Object placements (section 4)
    named_placements = []
    num_placements = (s4_end - s4_off) // 20
    for i in range(num_placements):
        off = s4_off + i * 20
        midx  = struct.unpack_from('<H', data, off)[0]
        angle = struct.unpack_from('<H', data, off + 2)[0]
        wx    = struct.unpack_from('<i', data, off + 4)[0]
        wy    = struct.unpack_from('<i', data, off + 8)[0]
        wz    = struct.unpack_from('<i', data, off + 12)[0]

        if midx >= s1_n:
            continue
        if wx == 0 and wz == 0:
            continue    # sentinel record

        placed = []
        for p in obj_library[midx]:
            new_verts = []
            for vx, vy, vz in p.verts:
                rx, rz = _rotate_y(vx, vz, angle)
                new_verts.append((
                    int(rx // GTE_SCALE + wx),
                    int(vy // GTE_SCALE + wy),
                    int(rz // GTE_SCALE + wz),
                ))
            placed.append(Poly(new_verts, p.uvs,
                               p.tpage_x, p.tpage_y,
                               p.clut_x, p.clut_y,
                               p.mode, p.has_tex, p.color))
        if placed:
            named_placements.append((f'obj{i:03d}_s{midx:03d}', placed))

    total_obj_polys = sum(len(pl) for _, pl in named_placements)
    print(f'  objects: {total_obj_polys} polys in {len(named_placements)} placements')
    return road_polys, named_placements
