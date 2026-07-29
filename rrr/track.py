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
#              sub[10] = course descriptor / texture-streaming table (0x1B0 bytes).
#              sub[20] = the road spine (264 nodes, stride 20 bytes).
#
# --- COURSE DESCRIPTOR (section 5, sub[10]) ---
#
# This is the struct the game reaches through *(DAT_801dd79c + 0x2c).
# Identified from three independent constraints:
#   * FUN_8001d974 walks an array at +0x190 terminated by -1; sub[10] holds
#     -1, -1, -1, -1 at +0x190..+0x19C in all five courses.
#   * FUN_8001f4c0 compares (camera_spine_pos % (N*256)) & 0xFFFFFF00 against
#     struct[0]; every value at +0x00, +0x04 and +0xD0..+0xE4 is an exact
#     multiple of 256 and lies inside [0, node_count * 256) -- i.e. they are
#     spine positions in node<<8 units.
#   * Its size (0x1B0) is the only sub-section large enough to hold +0x190.
#
# Texture switch table, two banks (double buffer), stride 0x0C:
#   [+0xD0 + bank*0xC]  s32  ref1  - first switch point  (spine pos, node<<8)
#   [+0xD4 + bank*0xC]  s32  ref2  - second switch point (spine pos, node<<8)
#   [+0xD8 + bank*0xC]  s32  dir   - direction flag; only its sign is used
#
# NOTE: the s16 at spine-node offset +10 is NOT a texture reference -- it is
# the node's heading angle (4096 units = 360 deg).  FUN_8001d974 loads those
# headings for nodes n, n-1, n-2, n+1 into the *entity* struct at +0xCC..+0xD8
# and derives a turn-direction flag from them.  FUN_80031510 reads +0xD0..+0xD8
# off the *course descriptor* instead.  The two structs are unrelated; only
# their Ghidra offsets coincide.
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
# --- *_PCT.DAT / *_CT.DAT - Course VRAM Override Banks ---
#
# Sequence of upload records, terminated by size == 0:
#   [u32 size][u16 vram_x][u16 vram_y][u16 w][u16 h][raw halfword data]
#
# *size* is the PAYLOAD size (== w * h * 2); it does NOT include the 12-byte
# header, so structurally the next record begins at pos + 12 + size.  Walking
# the file that way, each *_PCT.DAT parses cleanly into three records:
#   (0, 494)   256x18    the CLUT row
#   (320, 256) 320x256   a variant of the BIG0/BIG3/BIG4 region
#   (768, 0)   256x256   a variant of the BIG1/BIG2 region
# *_CT.DAT contains the CLUT row only.
#
# ONLY THE FIRST RECORD IS UPLOADED.  Verified against a VRAM dump (CRS_EASY):
# on the halfwords where the BIG*.TMS content and the PCT block differ, real
# VRAM matches BIG 98-100% and the PCT block 0.0%, in both regions.  The game's
# own loader appears to advance by *size* rather than 12 + size, so it walks
# into the middle of record 0's payload, reads a garbage header that the GPU
# discards, and stops.  Records 1 and 2 are effectively dead data.
#
# Do not "fix" the walk to 12 + size and upload the extra blocks -- that makes
# the output diverge from the hardware.
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

# Section-5 sub-pointer index of the course descriptor, and the offsets of
# the texture switch table inside it.
_DESC_SUBPTR    = 10
_DESC_SWITCH    = 0xD0   # {ref1, ref2, dir} triple, stride _DESC_BANK_STRIDE
_DESC_BANK_STRIDE = 0x0C

# Which texture bank the game keeps resident is selected by DAT_801dc9c4.
# Bank 0 produces a clean contiguous partition on all five courses; bank 1 is
# the same partition shifted a few nodes (double-buffer lead/lag) and is
# fragmented on CRS_OLDH.
_DESC_BANK = 0

# Boundary value -> CRS section number.  FUN_80031510 returns 256 for one page
# and 0 for the other; which page is CRS section 2 versus section 3 depends on
# the bank parity at track load and is not settled by the decompile alone.
# Flip these two values to invert the polarity (Step 2: confirm from a VRAM dump).
_SECTION_AT_HIGH_B = 2   # boundary >= 128
_SECTION_AT_LOW_B  = 3   # boundary <  128

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
        # Only the first record is uploaded -- see the note in the header
        # comment.  Records past the first are present in the file but are
        # never reached by the game's loader; uploading them makes the
        # extracted textures diverge from real VRAM.
        if len(clut_data) < 12:
            continue
        sz = struct.unpack_from('<I', clut_data, 0)[0]
        x = struct.unpack_from('<H', clut_data, 4)[0]
        y = struct.unpack_from('<H', clut_data, 6)[0]
        w = struct.unpack_from('<H', clut_data, 8)[0]
        h = struct.unpack_from('<H', clut_data, 10)[0]
        if sz and x < 1024 and y < 512 and w < 2048 and h < 512:
            vram.load_pct_block(x, y, w, h, clut_data[12: 12 + sz])
            print(f'  CLUT record {w}x{h} -> VRAM({x},{y})')


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
    seen = set()
    for i in range(num_placements):
        off = s4_off + i * 20
        raw   = struct.unpack_from('<H', data, off)[0]
        angle = struct.unpack_from('<H', data, off + 2)[0]
        wx    = struct.unpack_from('<i', data, off + 4)[0]
        wy    = struct.unpack_from('<i', data, off + 8)[0]
        wz    = struct.unpack_from('<i', data, off + 12)[0]
        flags = struct.unpack_from('<i', data, off + 16)[0]

        # The model word carries flag bits above bit 11.  Bit 12 marks a
        # conditional-render variant; bits 13-15 mark control/sentinel records.
        # Testing the unmasked word against s1_n silently discarded every
        # bit-12 record -- 31 of 138 on CRS_EASY, leaving two whole regions of
        # the course with no scenery at all.
        midx = raw & 0xFFF
        if (raw >> 13) & 0x7:
            continue    # control / sentinel record
        if midx >= s1_n:
            continue
        if wx == 0 and wz == 0:
            continue    # sentinel record

        # The flags field is always a value shifted left by 16.  Every real
        # placement has a non-negative one; EASY/MID/HIGH each carry exactly
        # one record with 0xFE390000 (negative as s32).  On CRS_EASY that is
        # record 89, independently reported as rendering wrongly.
        # NOTE: do not filter on specific flags values -- OLDE/OLDH legitimately
        # use many (0x60000, 0x480000, 0x840000, 0xF60000, 0x2040000, ...).
        if flags < 0:
            continue

        # The same model is placed twice at the same spot in several places
        # (11 such pairs on CRS_EASY), differing only in the flags field.
        # Exporting both leaves two coincident copies that z-fight.
        key = (midx, angle, wx, wy, wz)
        if key in seen:
            continue
        seen.add(key)

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


def _parse_texture_switch(crs_data, bank=_DESC_BANK):
    """
    Read the texture switch triple {ref1, ref2, dir} from the course
    descriptor (section 5, sub[10]).

    ref1 and ref2 are spine positions in node<<8 units marking the two points
    where the course texture page swaps.  Only the sign of *dir* is used.

    Returns (ref1, ref2, dir).
    """
    s5 = struct.unpack_from('<6I', crs_data, 0)[5]
    desc = s5 + struct.unpack_from('<I', crs_data, s5 + _DESC_SUBPTR * 4)[0]
    off = desc + _DESC_SWITCH + bank * _DESC_BANK_STRIDE
    ref1, ref2, direction = struct.unpack_from('<3i', crs_data, off)
    return ref1, ref2, direction


def compute_section_map(crs_data, road_nodes, named_placements):
    """
    Determine which course texture section (2 or 3) each geometry node
    should use, based on the course descriptor's texture switch table.

    The game streams course textures per-VRAM-row using a sliding boundary B
    (FUN_80031510, clamped to 0..256):
        rows 0..B-1   = one 384x256 page
        rows B..255   = the other page
    Row index is preserved across the swap, because UVs are baked into the
    display lists.  FUN_800319f8 confirms the streaming model: it tracks B
    frame to frame and uploads only the band of rows that changed.

    B is computed from the signed wrap-around distance between the camera's
    position on the spine and the two switch points {ref1, ref2} stored in
    the course descriptor (section 5, sub[10], +0xD0), with the sign of the
    third field used as a direction flag.  Away from a switch point B is 0 or
    256; a narrow ~3-node transition band exists at each switch point where B
    takes intermediate values.

    Spine world positions are decoded using the FUN_800160b4 formula:
        world_X = 0xF000 - (s32@0 >> 14)
        world_Z = s32@4 >> 14
    These are converted to tile coordinates (>>11) and matched to the tile
    grid to assign road segments.  Placed objects are matched to the nearest
    spine node by Euclidean distance.

    Returns
    -------
    dict : str -> int
        Mapping from node name to section number (2 or 3).
    """
    sec_offsets = struct.unpack_from('<6I', crs_data, 0)

    # -- Spine data (section 5, sub[20]) ----------------------------------
    s5 = sec_offsets[5]
    sub20_off_rel = struct.unpack_from('<I', crs_data, s5 + 20 * 4)[0]
    spine_off = s5 + sub20_off_rel
    node_count = struct.unpack_from('<I', crs_data, spine_off)[0]
    spine_data_off = spine_off + 4

    _BASE_X = 0xF000   # DAT_801dc9b0, confirmed from FUN_800368f8

    spine_world = []   # (world_x, world_z) per node
    for i in range(node_count):
        off = spine_data_off + i * 20
        d0 = struct.unpack_from('<i', crs_data, off)[0]       # s32 @ 0
        d4 = struct.unpack_from('<i', crs_data, off + 4)[0]   # s32 @ 4
        spine_world.append((_BASE_X - (d0 >> 14), d4 >> 14))

    # -- Boundary computation (replicates FUN_80031510) -------------------
    ref1, ref2, direction = _parse_texture_switch(crs_data)
    wrap = node_count * 256

    def _signed_wrap(cam, ref):
        cm, rm = cam % wrap, ref % wrap
        ahead = rm < cm
        d = (cm - rm) if ahead else (rm - cm)
        if d > wrap // 2:
            d = wrap - d
            ahead = not ahead
        return d if ahead else -d

    def _boundary(n):
        """Compute texture boundary B at spine node *n*."""
        cam = n * 256
        d1 = _signed_wrap(cam, ref1)
        if abs(d1) <= 512:
            return max(0, min(256, (d1 + 512) // 4))
        d2 = _signed_wrap(cam, ref2)
        if abs(d2) <= 512:
            return max(0, min(256, (-d2 + 512) // 4))
        dd = direction               # sign only (course descriptor +0xD8)
        if d1 < 1:
            return 256 if (dd < 0 and d2 < 1) else 0
        else:
            return 0 if (dd > 0 and d2 >= 0) else 256

    boundaries = [_boundary(n) for n in range(node_count)]
    n_hi = sum(1 for b in boundaries if b == 256)
    n_lo = sum(1 for b in boundaries if b == 0)
    print(f'  texture switch: bank {_DESC_BANK} ref1=node{ref1 // 256} '
          f'ref2=node{ref2 // 256} dir={direction}  '
          f'({n_hi} nodes B=256, {n_lo} nodes B=0, '
          f'{node_count - n_hi - n_lo} in transition)')

    # -- Tile grid --------------------------------------------------------
    tile_grid = {}
    for gi in range(GRID_SIZE * GRID_SIZE):
        seg_idx = struct.unpack_from('<h', crs_data, 0x18 + gi * 2)[0]
        if seg_idx >= 0:
            tc = 30 - (gi % GRID_SIZE)
            tr = gi // GRID_SIZE
            tile_grid[(tc, tr)] = seg_idx

    spine_tiles = [(wx >> 11, wz >> 11) for wx, wz in spine_world]

    # For each tile in the grid, find the nearest spine node.
    tile_to_spine = {}
    for (tc, tr) in tile_grid:
        best_d2 = 999999
        best_n = 0
        for i, (sc, sr) in enumerate(spine_tiles):
            d2 = (sc - tc) ** 2 + (sr - tr) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best_n = i
        tile_to_spine[(tc, tr)] = best_n

    # -- Assign each geometry node to section 2 or 3 ---------------------
    result = {}

    for name, _ in road_nodes:
        parts = name.split('_')
        tp = parts[2]                       # e.g. 't17x08'
        tc = int(tp[1:tp.index('x')])
        tr = int(tp[tp.index('x') + 1:])
        sn = tile_to_spine.get((tc, tr), 0)
        b = boundaries[sn]
        result[name] = _SECTION_AT_HIGH_B if b >= 128 else _SECTION_AT_LOW_B

    for name, polys in named_placements:
        xs = [v[0] for p in polys for v in p.verts]
        zs = [v[2] for p in polys for v in p.verts]
        if not xs:
            result[name] = _SECTION_AT_LOW_B
            continue
        cx = sum(xs) / len(xs)
        cz = sum(zs) / len(zs)
        best_d2 = float('inf')
        best_n = 0
        for i, (wx, wz) in enumerate(spine_world):
            d2 = (wx - cx) ** 2 + (wz - cz) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best_n = i
        b = boundaries[best_n]
        result[name] = _SECTION_AT_HIGH_B if b >= 128 else _SECTION_AT_LOW_B

    n2 = sum(1 for v in result.values() if v == 2)
    n3 = sum(1 for v in result.values() if v == 3)
    print(f'  section map: {n2} nodes -> S2, {n3} nodes -> S3')
    return result
