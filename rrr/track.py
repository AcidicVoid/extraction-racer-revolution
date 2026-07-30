# CRS_*.DAT track format parser.
#
# Each course file has six sections, pointed to by six u32 offsets at byte 0:
#
#   Section 0  Road display lists, one per occupied tile.
#   Section 1  Track-local object library, display list format.
#   Section 2  Course texture: 384 halfwords wide by 256 rows, uploaded to
#              VRAM at (640, 256).
#   Section 3  Second course texture, same size and destination as section 2.
#   Section 4  Object placement table, 20 bytes per record.
#   Section 5  Sub-section pointer table.
#              sub[10] = course descriptor, 0x1B0 bytes.
#              sub[20] = road spine, u32 node count then 20 bytes per node.
#
# COURSE DESCRIPTOR (section 5, sub[10])
#
# A table of spine positions describing course features. Positions are stored
# in node<<8 units, so a value of 0x1900 is spine node 25. Known fields:
#
#   [+0x90] s32  tunnel_b start        (named by the loader's own debug print)
#   [+0x94] s32  tunnel_b end
#   [+0xA8] s32  jump start
#   [+0xAC] s32  jump end
#
# Texture switch table, two banks, stride 0x0C:
#
#   [+0xD0 + bank*0xC]  s32  first switch point, spine position
#   [+0xD4 + bank*0xC]  s32  second switch point, spine position
#   [+0xD8 + bank*0xC]  s32  direction flag, only the sign is used
#
# The two switch points divide the spine into the arc that uses section 2 and
# the arc that uses section 3. See compute_section_map.
#
# ROAD SEGMENTS (section 0)
#
# One display list per occupied tile, holding the geometry for that tile.
# The road renderer uses its own command stride table, so section-0 lists are
# parsed with parse_road_display_list rather than parse_display_list.
#
# Vertex coordinates are in GTE units, four per world unit, relative to the
# tile world origin:
#
#   world_X = tile_col * 2048 + vertex_X / 4
#   world_Y = vertex_Y / 4
#   world_Z = tile_row * 2048 + vertex_Z / 4
#
# TILE GRID (at file offset 0x18)
#
# 32 by 32 signed 16-bit values, each a section-0 segment index, or -1 for an
# empty tile. The renderer computes the grid index as
#
#   gi = tile_row * 32 + 30 - tile_col
#
# so a grid index maps back to tile_col = 30 - (gi % 32), tile_row = gi // 32.
#
# OBJECT PLACEMENTS (section 4)
#
# 20-byte records:
#
#   [0..1]   u16  model word, see parse_crs for the flag bits
#   [2..3]   u16  Y-axis rotation, 4096 units per full turn
#   [4..7]   s32  world X
#   [8..11]  s32  world Y
#   [12..15] s32  world Z
#   [16..19] s32  flags, always a value shifted left by 16
#
# Placing a library object:
#
#   rotated_X, rotated_Z = rotate_y(vertex_X, vertex_Z, angle)
#   world_X = rotated_X / 4 + placement_X
#   world_Y = vertex_Y   / 4 + placement_Y
#   world_Z = rotated_Z  / 4 + placement_Z
#
# *_PCT.DAT / *_CT.DAT COURSE PALETTE FILES
#
# A sequence of VRAM upload records terminated by size == 0:
#
#   [u32 size][u16 vram_x][u16 vram_y][u16 w][u16 h][raw halfword data]
#
# size is the payload size, equal to w * h * 2, and excludes the 12-byte
# header, so the next record starts at pos + 12 + size.
#
# Each *_PCT.DAT holds three records:
#
#   (0, 494)   256x18    the palette row
#   (320, 256) 320x256   course-specific replacement for part of the shared
#                        BIG0/BIG3/BIG4 region
#   (768, 0)   256x256   course-specific replacement for part of the shared
#                        BIG1/BIG2 region
#
# All records are uploaded. The last two replace roughly 7 percent of the
# shared pages with course-specific art, and the amount of geometry that
# depends on them varies enormously per course: none of CRS_EASY, about 5
# percent of CRS_MID and CRS_HIGH, and nearly half of CRS_OLDE and CRS_OLDH.
#
# *_CT.DAT holds the palette row only. CRS_EASY uses it in place of its PCT
# file; see CLUT_FILES.

import struct
import math
from rrr.displaylist import (parse_display_list, parse_road_display_list,
                             Poly)

GTE_SCALE = 4      # GTE units per world unit (for object vertices)
GRID_SIZE = 32
TILE_WORLD = 0x800  # 2048 world units per tile

# Location of the course descriptor within section 5, and of the texture
# switch table within the descriptor.
_DESC_SUBPTR      = 10
_DESC_SWITCH      = 0xD0
_DESC_BANK_STRIDE = 0x0C

# The descriptor holds two banks of switch points. The game picks between them
# with the flag that also selects the reverse driving direction, so bank 0 is
# the normal course and bank 1 is its Extra (reverse) variant.
#
# The two banks describe the same partition offset by a few spine nodes, and
# that offset falls between the nodes any geometry maps to, so both banks
# produce an identical section assignment on all five courses. The Extra
# courses therefore extract to exactly the same geometry and textures as the
# normal ones, and no separate export is needed.
_DESC_BANK = 0

# Mapping from boundary value to CRS section number. Rows below the boundary
# hold section 2 and rows above hold section 3, so a boundary of 256 means the
# whole texture window is section 2 and a boundary of 0 means section 3.
_SECTION_AT_HIGH_B = 2
_SECTION_AT_LOW_B  = 3

# Palette file used by each course. CRS_EASY is the only course with its own
# *_CT.DAT, and it uses that instead of the *_PCT.DAT: VRAM dumps taken during
# an EASY race hold the plain BIG*.TMS content in the regions EASY_PCT would
# overwrite, while dumps from MID and HIGH hold their PCT content there.
CLUT_FILES = {
    'CRS_EASY': ['EASY_CT.DAT'],
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
    """
    Rotate an (x, z) pair around the Y axis by a PS1 angle.

    Uses the same matrix as the game:
        new_x = cos*x - sin*z
        new_z = sin*x + cos*z
    """
    s, c = _sin(angle), _cos(angle)
    return x * c - z * s, x * s + z * c


def load_course_textures(crs_data, vram, clut_files=None, section=2):
    """
    Upload the course texture and palette into an existing VramSim.

    section selects which of the two course textures to use, 2 or 3. Both are
    384 by 256 halfwords and both go to VRAM (640, 256).

    Every record of every palette file is uploaded, in file order, so later
    records overwrite earlier ones.
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
        pos = 0
        count = 0
        while pos + 12 <= len(clut_data):
            sz = struct.unpack_from('<I', clut_data, pos)[0]
            if sz == 0:
                break
            x = struct.unpack_from('<H', clut_data, pos + 4)[0]
            y = struct.unpack_from('<H', clut_data, pos + 6)[0]
            w = struct.unpack_from('<H', clut_data, pos + 8)[0]
            h = struct.unpack_from('<H', clut_data, pos + 10)[0]
            # A record whose size does not match its dimensions means the walk
            # has lost sync, so stop rather than upload garbage.
            if sz != w * h * 2:
                print(f'  [PCT/CT] record at {pos}: size {sz} does not match '
                      f'{w}x{h}, stopping')
                break
            if x < 1024 and y < 512 and w < 2048 and h < 512:
                vram.load_pct_block(x, y, w, h,
                                    clut_data[pos + 12: pos + 12 + sz])
                count += 1
            pos += 12 + sz
        if count:
            print(f'  palette file: {count} records uploaded')


def _parse_road_segments(data):
    """
    Parse road geometry from the section-0 display lists.

    The tile grid at file offset 0x18 maps each cell to a section-0 segment
    index. Each segment is a display list whose vertices are in GTE units
    relative to its tile origin.

    Returns a list of (name, [Poly]) tuples, one per segment, with vertices
    already converted to world coordinates.
    """
    sec = struct.unpack_from('<6I', data, 0)
    s0_off = sec[0]

    # Section 0 header: entry count followed by that many offsets, each
    # relative to the start of the section.
    s0_count = struct.unpack_from('<I', data, s0_off)[0]
    s0_offsets = [struct.unpack_from('<I', data, s0_off + 4 + i * 4)[0]
                  for i in range(s0_count)]

    # Invert the grid to get the tile each segment belongs to.
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
                p.mode, p.has_tex, p.color, p.twin))

        name = f'road_seg{seg_idx:03d}_t{tile_col:02d}x{tile_row:02d}'
        road_nodes.append((name, world_polys))
        total_polys += len(world_polys)

    print(f'  road: {total_polys} polys in {len(road_nodes)} segments '
          f'(of {s0_count} total)')
    return road_nodes


def parse_crs(data):
    """
    Parse a CRS_*.DAT file.

    Returns a pair of lists, both holding (name, [Poly]) tuples with vertices
    in world coordinates: the road segments from section 0, and the objects
    placed by section 4.
    """
    sec = struct.unpack_from('<6I', data, 0)
    s1_off = sec[1]
    s4_off = sec[4]
    s4_end = sec[5]

    road_nodes = _parse_road_segments(data)

    # Object library (section 1): entry count followed by that many offsets.
    s1_n = struct.unpack_from('<I', data, s1_off)[0]
    s1_offsets = [struct.unpack_from('<I', data, s1_off + 4 + i * 4)[0]
                  for i in range(s1_n)]
    obj_library = []
    for i in range(s1_n):
        start = s1_off + s1_offsets[i]
        end = s1_off + (s1_offsets[i + 1] if i + 1 < s1_n else sec[2] - s1_off)
        obj_library.append(parse_display_list(data[start: min(end, len(data))]))

    # Object placements (section 4).
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

        # Only the low 12 bits of the model word are the library index. Bit 12
        # marks a conditional-render variant, which is still a real placement.
        # Bits 13 to 15 mark control records, which are not.
        midx = raw & 0xFFF
        if (raw >> 13) & 0x7:
            continue
        if midx >= s1_n:
            continue
        if wx == 0 and wz == 0:
            continue

        # Records whose flags field is negative are control records rather
        # than placements. The positive values vary widely between courses, so
        # only the sign can be tested.
        if flags < 0:
            continue

        # Some models are placed twice at the same spot with the same rotation,
        # differing only in the flags field. Exporting both would leave two
        # coincident copies that z-fight.
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
                               p.mode, p.has_tex, p.color, p.twin))
        if placed:
            named_placements.append((f'obj{i:03d}_s{midx:03d}', placed))

    total_obj_polys = sum(len(pl) for _, pl in named_placements)
    print(f'  objects: {total_obj_polys} polys in {len(named_placements)} placements')
    return road_nodes, named_placements


def _parse_texture_switch(crs_data, bank=_DESC_BANK):
    """
    Read the texture switch table from the course descriptor.

    Returns (ref1, ref2, direction). ref1 and ref2 are spine positions in
    node<<8 units marking the two points where the course texture changes.
    Only the sign of direction is meaningful.
    """
    s5 = struct.unpack_from('<6I', crs_data, 0)[5]
    desc = s5 + struct.unpack_from('<I', crs_data, s5 + _DESC_SUBPTR * 4)[0]
    off = desc + _DESC_SWITCH + bank * _DESC_BANK_STRIDE
    ref1, ref2, direction = struct.unpack_from('<3i', crs_data, off)
    return ref1, ref2, direction


def compute_section_map(crs_data, road_nodes, named_placements,
                        bank=_DESC_BANK):
    """
    Decide which course texture section, 2 or 3, each geometry node uses.

    The game keeps one 384 by 256 window in VRAM and fills it from either
    section. It tracks a row boundary that runs from 0 to 256: rows below the
    boundary come from section 2 and rows at or above it come from section 3,
    so a boundary of 256 means the whole window is section 2 and 0 means it is
    all section 3. Row indices are preserved when the window is refilled,
    because the UVs are baked into the display lists.

    The boundary is a function of where the camera is on the spine, derived
    from the signed wrap-around distance to the two switch points. It sits at
    0 or 256 everywhere except a narrow band of about three nodes around each
    switch point.

    Road segments are assigned by matching their tile to the nearest spine
    node, placed objects by matching their centroid to the nearest spine node.

    bank selects which set of switch points to use: 0 for the normal course
    and 1 for its Extra (reverse) variant. Both give the same result on every
    course, so the Extra courses need no separate export.

    Returns a dict mapping node name to section number.
    """
    sec_offsets = struct.unpack_from('<6I', crs_data, 0)

    # Spine nodes (section 5, sub[20]). The two leading s32 fields hold the
    # node position in a fixed-point form; X is measured back from a fixed
    # origin while Z is measured forward.
    s5 = sec_offsets[5]
    sub20_off_rel = struct.unpack_from('<I', crs_data, s5 + 20 * 4)[0]
    spine_off = s5 + sub20_off_rel
    node_count = struct.unpack_from('<I', crs_data, spine_off)[0]
    spine_data_off = spine_off + 4

    _BASE_X = 0xF000

    spine_world = []
    for i in range(node_count):
        off = spine_data_off + i * 20
        d0 = struct.unpack_from('<i', crs_data, off)[0]
        d4 = struct.unpack_from('<i', crs_data, off + 4)[0]
        spine_world.append((_BASE_X - (d0 >> 14), d4 >> 14))

    ref1, ref2, direction = _parse_texture_switch(crs_data, bank)
    wrap = node_count * 256

    def _signed_wrap(cam, ref):
        """Shortest signed distance from ref to cam around the closed spine."""
        cm, rm = cam % wrap, ref % wrap
        ahead = rm < cm
        d = (cm - rm) if ahead else (rm - cm)
        if d > wrap // 2:
            d = wrap - d
            ahead = not ahead
        return d if ahead else -d

    def _boundary(n):
        """
        Row boundary at spine node n, in the range 0 to 256.

        Within half a page of a switch point the boundary ramps linearly, which
        is the transition band. Outside that range it saturates, and which way
        it saturates depends on which side of each switch point the node is on
        and on the sign of the direction field.
        """
        cam = n * 256
        d1 = _signed_wrap(cam, ref1)
        if abs(d1) <= 512:
            return max(0, min(256, (d1 + 512) // 4))
        d2 = _signed_wrap(cam, ref2)
        if abs(d2) <= 512:
            return max(0, min(256, (-d2 + 512) // 4))
        if d1 < 1:
            return 256 if (direction < 0 and d2 < 1) else 0
        return 0 if (direction > 0 and d2 >= 0) else 256

    boundaries = [_boundary(n) for n in range(node_count)]
    n_hi = sum(1 for b in boundaries if b == 256)
    n_lo = sum(1 for b in boundaries if b == 0)
    print(f'  texture switch: bank {bank} ref1=node{ref1 // 256} '
          f'ref2=node{ref2 // 256} dir={direction}  '
          f'({n_hi} nodes B=256, {n_lo} nodes B=0, '
          f'{node_count - n_hi - n_lo} in transition)')

    tile_grid = {}
    for gi in range(GRID_SIZE * GRID_SIZE):
        seg_idx = struct.unpack_from('<h', crs_data, 0x18 + gi * 2)[0]
        if seg_idx >= 0:
            tc = 30 - (gi % GRID_SIZE)
            tr = gi // GRID_SIZE
            tile_grid[(tc, tr)] = seg_idx

    spine_tiles = [(wx >> 11, wz >> 11) for wx, wz in spine_world]

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

    result = {}

    # Road segment names carry their tile as a 'tCCxRR' field.
    for name, _ in road_nodes:
        tp = name.split('_')[2]
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
