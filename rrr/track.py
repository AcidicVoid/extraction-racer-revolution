# CRS_*.DAT track format parser.
#
# Each course file has six sections, pointed to by six u32 offsets at byte 0:
#
#   Section 0  Road display lists (147 segments, one per occupied tile).
#   Section 1  Track-local object library (122 entries, display list format).
#   Section 2  Course texture upload: 384 halfwords wide x 256 rows,
#              placed into VRAM at (640, 256).
#   Section 3  Alternate texture upload (double-buffered companion to sec 2).
#   Section 4  Object placement table (stride 20 bytes per record).
#   Section 5  Sub-section pointer table (used internally by the game).
#
# Tile grid (32x32 signed 16-bit values) starts at file offset 0x18.
# Each cell stores a segment index (0..146) or -1 for empty.
# Index formula:  gi = row * 32 + col   (simple row-major order, confirmed).
# Tile world origin: (col * 2048, row * 2048) in world units.
#
# Road vertex coordinate system:
#   Vertices in section-0 CMD0 records are in GTE units.
#   World position = vertex_value / GTE_SCALE + tile_origin
#   GTE_SCALE = 4  (confirmed: raw X range ~+-11000 / 4 + tile = world values
#                   that align with section-4 placement coords)
#
# Section-0 CMD types:
#   CMD0 (40B) - close-up road surface -> extract
#   CMD1 (48B) - road surface (same vertex layout as CMD0, 8 extra bytes).
#               Two sub-types based on vertex magnitude:
#                 near-field  (max abs component < 32000) -> extract
#                 horizon     (any component == +-32767)  -> skip
#               39 tiles have ONLY CMD1, so skipping all CMD1 leaves those
#               tiles empty and breaks road continuity.
#   CMD2 (32B) - colored kerb strips -> skip
#
# Sentinel threshold: PS1 uses raw s16 value +-32767 as a sky/horizon marker.
# Any CMD1 record with a vertex component >= this value is a horizon poly.
CMD1_HORIZON_THRESHOLD = 32000
#
# Section-4 placement record (20 bytes, little-endian):
#   [0..1]   u16  section-1 entry index
#   [2..3]   u16  Y-axis rotation angle (PS1 units: 4096 = 360 degrees)
#   [4..7]   s32  world X
#   [8..11]  s32  world Y
#   [12..15] s32  world Z
#   [16..19] s32  flags / extra (meaning not fully known)
# Records where (world_X == 0 and world_Z == 0) or entry_index >= s1_count
# are sentinels and must be skipped.
#
# *_PCT.DAT / *_CT.DAT files contain CLUT upload records:
#   Repeating: [u32 size][u16 vram_x][u16 vram_y][u16 w][u16 h][data]
#   Terminated by size == 0.
# Load order per course:  PCT first, then CT (last write wins in VRAM).
# CLUT map:
#   CRS_EASY  -> EASY_PCT.DAT, EASY_CT.DAT
#   CRS_MID   -> MID_PCT.DAT
#   CRS_HIGH  -> HIGH_PCT.DAT
#   CRS_OLDE  -> OLD_PCT.DAT
#   CRS_OLDH  -> OLD_PCT.DAT

import struct
import math
from rrr.displaylist import parse_display_list, Poly, _decode_tpage, _decode_clut

GTE_SCALE = 4          # GTE units per world unit for road/object vertices
TILE_SIZE = 2048       # world units per tile edge
GRID_COLS = 32
GRID_ROWS = 32

CLUT_FILES = {
    'CRS_EASY': ['EASY_PCT.DAT', 'EASY_CT.DAT'],
    'CRS_MID':  ['MID_PCT.DAT'],
    'CRS_HIGH': ['HIGH_PCT.DAT'],
    'CRS_OLDE': ['OLD_PCT.DAT'],
    'CRS_OLDH': ['OLD_PCT.DAT'],
}


def _sin(angle: int) -> float:
    """PS1 angle to sin.  4096 units = 360 degrees."""
    return math.sin(angle * math.pi / 2048)


def _cos(angle: int) -> float:
    return math.cos(angle * math.pi / 2048)


def _rotate_y(x: float, z: float, angle: int) -> tuple:
    """Rotate a (x, z) pair by a PS1 angle around the Y axis."""
    s, c = _sin(angle), _cos(angle)
    return x * c + z * s, -x * s + z * c


def load_course_textures(crs_data: bytes, vram, clut_files: list = None):
    """
    Upload course-specific textures into an existing VramSim.

    Section 2 of the CRS file contains 384x256 halfwords of raw VRAM data
    placed at VRAM (640, 256).  The CLUT files are loaded on top of that.
    """
    sec = struct.unpack_from('<6I', crs_data, 0)
    sec2 = crs_data[sec[2]: sec[3]]
    expected = 384 * 256 * 2
    if len(sec2) == expected:
        vram.load_rect(640, 256, 384, 256, sec2)
        print('  course textures: 384x256 block -> VRAM(640,256)')
    else:
        print(f'  course textures: unexpected section-2 size {len(sec2)} (expected {expected})')

    for clut_data in (clut_files or []):
        pos = 0
        count = 0
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


def parse_crs(data: bytes) -> tuple:
    """
    Parse a CRS_*.DAT file.

    Returns:
        road_polys      - list of Poly for the road surface (section-0 CMD0).
        named_placements - list of (name, [Poly]) for placed objects (section-4).
    """
    sec = struct.unpack_from('<6I', data, 0)
    s0_off = sec[0]
    s1_off = sec[1]
    s4_off = sec[4]
    s4_end = sec[5]

    # --- road geometry (section 0) ---
    s0_n = struct.unpack_from('<I', data, s0_off)[0]
    s0_offsets = [struct.unpack_from('<I', data, s0_off + 4 + i * 4)[0]
                  for i in range(s0_n)]

    road_polys = []
    for row in range(GRID_ROWS):
        for col in range(GRID_COLS):
            gi = row * GRID_COLS + col
            seg_idx = struct.unpack_from('<h', data, 0x18 + gi * 2)[0]
            if seg_idx < 0 or seg_idx >= s0_n:
                continue

            tile_wx = col * TILE_SIZE
            tile_wz = row * TILE_SIZE

            seg_start = s0_off + s0_offsets[seg_idx]
            seg_end = s0_off + (s0_offsets[seg_idx + 1]
                                if seg_idx + 1 < s0_n
                                else s1_off - s0_off)
            sd = data[seg_start: min(seg_end, len(data))]

            pos = 0
            while pos + 4 <= len(sd):
                cmd = struct.unpack_from('<H', sd, pos)[0]
                cnt = struct.unpack_from('<H', sd, pos + 2)[0]
                if cnt == 0:
                    break
                if cmd == 2:    # colored kerb strip - skip
                    pos += 4 + cnt * 32
                    continue
                if cmd not in (0, 1):
                    break       # unexpected command, stop this segment

                stride = 40 if cmd == 0 else 48
                for pi in range(cnt):
                    rec = sd[pos + 4 + pi * stride: pos + 4 + (pi + 1) * stride]
                    if len(rec) < stride:
                        break

                    xs = [struct.unpack_from('<h', rec, j * 4)[0]      for j in range(4)]
                    ys = [struct.unpack_from('<h', rec, j * 4 + 2)[0]  for j in range(4)]
                    zs = [struct.unpack_from('<h', rec, 16 + j * 2)[0] for j in range(4)]

                    # CMD1 horizon sentinel: any component at PS1 s16 max means
                    # this is a sky/far-field poly, not road surface.
                    if cmd == 1:
                        if any(abs(v) >= CMD1_HORIZON_THRESHOLD
                               for v in xs + zs):
                            continue

                    verts = [
                        (xs[j] // GTE_SCALE + tile_wx,
                         ys[j] // GTE_SCALE,
                         zs[j] // GTE_SCALE + tile_wz)
                        for j in range(4)
                    ]

                    w24 = struct.unpack_from('<I', rec, 24)[0]
                    w28 = struct.unpack_from('<I', rec, 28)[0]
                    u0, v0 = w24 & 0xFF, (w24 >> 8) & 0xFF
                    clut_word  = (w24 >> 16) & 0xFFFF
                    u1, v1 = w28 & 0xFF, (w28 >> 8) & 0xFF
                    tpage_word = (w28 >> 16) & 0xFFFF
                    u2, v2 = rec[32], rec[33]
                    u3, v3 = rec[36], rec[37]

                    tx, ty, tp = _decode_tpage(tpage_word)
                    cx, cy    = _decode_clut(clut_word)
                    road_polys.append(Poly(
                        verts, [(u0, v0), (u1, v1), (u2, v2), (u3, v3)],
                        tx, ty, cx, cy, tp, True))

                pos += 4 + cnt * stride

    print(f'  road: {len(road_polys)} quads from {s0_n} segments')

    # --- object library (section 1) ---
    s1_n = struct.unpack_from('<I', data, s1_off)[0]
    s1_offsets = [struct.unpack_from('<I', data, s1_off + 4 + i * 4)[0]
                  for i in range(s1_n)]
    obj_library = []
    for i in range(s1_n):
        start = s1_off + s1_offsets[i]
        end = s1_off + (s1_offsets[i + 1] if i + 1 < s1_n else sec[2] - s1_off)
        obj_library.append(parse_display_list(data[start: min(end, len(data))]))

    # --- object placements (section 4) ---
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
    print(f'  objects: {total_obj_polys} quads in {len(named_placements)} placements')
    return road_polys, named_placements
