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
# --- ROAD SEGMENTS (section 0) ---
#
# Section 0 contains streaming display list segments.  Each segment holds
# the full visible road/scenery geometry for a camera tile position.
# Segments overlap heavily (each covers ~8 tiles radius).
#
# The road renderer (FUN_80055a58) uses a different dispatch table than
# the object renderer (FUN_80054654), so section-0 display lists have
# different command strides -- see ROAD_CMD_STRIDE in displaylist.py.
#
# Vertex coordinates are in GTE units (4× world units), relative to
# the tile world origin.  To convert to world space:
#
#   world_X = tile_col * 2048 + vertex_X / 4
#   world_Y = vertex_Y / 4
#   world_Z = tile_row * 2048 + vertex_Z / 4
#
# --- TILE GRID (at file offset 0x18) ---
#
# 32×32 signed 16-bit values, each is a section-0 segment index (or -1).
# Grid index formula (from RIDGE.EXE decompile FUN_80042f7c):
#   gi = (tile_row * 32 + 30) - tile_col
# Tile world origin: tile_col = 30 - (gi % 32), tile_row = gi // 32.
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
from rrr.displaylist import (parse_display_list, parse_road_display_list,
                             Poly, _decode_tpage, _decode_clut)

GTE_SCALE = 4      # GTE units per world unit (for object vertices)
GRID_SIZE = 32
TILE_WORLD = 0x800  # 2048 world units per tile

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
    """Rotate a (x, z) pair around Y by a PS1 angle.

    Matrix confirmed from RIDGE.EXE FUN_800527dc (Y-rotation builder):
        [ cos   0  -sin ]
        [  0    1    0  ]
        [ sin   0   cos ]
    Applied to column vector: new_x = cos*x - sin*z,  new_z = sin*x + cos*z
    """
    s, c = _sin(angle), _cos(angle)
    return x * c - z * s, x * s + z * c


def load_course_textures(crs_data, vram, clut_files=None, section=2):
    """
    Upload course-specific textures into an existing VramSim.

    Section 2 (default) or 3 contains 384x256 halfwords of raw VRAM data
    placed at (640, 256).  Each CLUT file is a sequence of upload records
    loaded on top.  The *section* parameter selects which texture buffer
    to use (2 = default, 3 = alternate / double-buffered companion).
    """
    sec = struct.unpack_from('<6I', crs_data, 0)
    if section == 3:
        raw = crs_data[sec[3]: sec[4]]
    else:
        raw = crs_data[sec[2]: sec[3]]
    expected = 384 * 256 * 2
    if len(raw) == expected:
        vram.load_rect(640, 256, 384, 256, raw)
        print(f'  course textures: section {section} 384x256 -> VRAM(640,256)')
    else:
        print(f'  course textures: unexpected size {len(raw)} (expected {expected})')

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


def _parse_road_segments(data):
    """
    Parse road geometry from section-0 display lists placed via the tile grid.

    The 32×32 tile grid at file offset 0x18 maps each cell to a section-0
    segment index.  Each segment is a display list whose vertices are in
    GTE units relative to the tile world origin.

    Returns a list of (name, [Poly]) tuples -- one per segment, with
    vertices already in world coordinates.
    """
    sec = struct.unpack_from('<6I', data, 0)
    s0_off = sec[0]

    # Section 0 header: count + count offsets (relative to section start)
    s0_count = struct.unpack_from('<I', data, s0_off)[0]
    s0_offsets = [struct.unpack_from('<I', data, s0_off + 4 + i * 4)[0]
                  for i in range(s0_count)]

    # Build segment-to-tile mapping from the 32×32 grid at offset 0x18.
    seg_tile = {}
    for gi in range(GRID_SIZE * GRID_SIZE):
        seg_idx = struct.unpack_from('<h', data, 0x18 + gi * 2)[0]
        if seg_idx < 0 or seg_idx >= s0_count:
            continue
        tile_col = 30 - (gi % GRID_SIZE)
        tile_row = gi // GRID_SIZE
        seg_tile[seg_idx] = (tile_col, tile_row)

    # Parse each segment and place it in world space.
    road_nodes = []
    total_polys = 0

    for seg_idx in range(s0_count):
        if seg_idx not in seg_tile:
            continue
        tile_col, tile_row = seg_tile[seg_idx]
        tile_wx = tile_col * TILE_WORLD
        tile_wz = tile_row * TILE_WORLD

        # Byte range for this segment within section 0.
        start = s0_off + s0_offsets[seg_idx]
        if seg_idx + 1 < s0_count:
            end = s0_off + s0_offsets[seg_idx + 1]
        else:
            end = sec[1]   # section 1 starts right after section 0 data
        seg_data = data[start: min(end, len(data))]

        local_polys = parse_road_display_list(seg_data)
        if not local_polys:
            continue

        # Convert GTE-local vertices to world coordinates.
        world_polys = []
        for p in local_polys:
            wv = []
            for vx, vy, vz in p.verts:
                wv.append((
                    tile_wx + vx / GTE_SCALE,
                    vy / GTE_SCALE,
                    tile_wz + vz / GTE_SCALE,
                ))
            world_polys.append(Poly(
                wv, p.uvs,
                p.tpage_x, p.tpage_y,
                p.clut_x, p.clut_y,
                p.mode, p.has_tex, p.color))

        name = f'road_seg{seg_idx:03d}_t{tile_col:02d}x{tile_row:02d}'
        road_nodes.append((name, world_polys))
        total_polys += len(world_polys)

    print(f'  road: {total_polys} polys in {len(road_nodes)} segments '
          f'(of {s0_count} total)')
    return road_nodes


def parse_crs(data):
    """
    Parse a CRS_*.DAT file.

    Returns:
        road_nodes       - list of (name, [Poly]) for road segments.
        named_placements - list of (name, [Poly]) for placed objects (section-4).
    """
    sec = struct.unpack_from('<6I', data, 0)
    s1_off = sec[1]
    s4_off = sec[4]
    s4_end = sec[5]

    # --- Road geometry from section-0 display lists ---
    road_nodes = _parse_road_segments(data)

    # --- Object library (section 1) ---
    s1_n = struct.unpack_from('<I', data, s1_off)[0]
    s1_offsets = [struct.unpack_from('<I', data, s1_off + 4 + i * 4)[0]
                  for i in range(s1_n)]
    obj_library = []
    for i in range(s1_n):
        start = s1_off + s1_offsets[i]
        end = s1_off + (s1_offsets[i + 1] if i + 1 < s1_n else sec[2] - s1_off)
        obj_library.append(parse_display_list(data[start: min(end, len(data))]))

    # --- Object placements (section 4) ---
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
    return road_nodes, named_placements
