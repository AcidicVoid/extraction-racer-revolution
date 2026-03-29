# Debug geometry export - writes road ribbon (from spine) and placed objects as OBJ.
# No textures, no GLB dependencies - use this to verify geometry in Blender
# (File > Import > Wavefront OBJ, then press Z for wireframe).
#
# Usage: python dump_obj.py <game_data_dir> [output.obj]

import sys
import math
import struct
from pathlib import Path

SCALE = 1 / 256.0
GTE_SCALE = 4
F0_FLOOR  = 0xF000   # 61440


def _sin(a): return math.sin(a * math.pi / 2048)
def _cos(a): return math.cos(a * math.pi / 2048)


def _rotate_y(x, z, angle):
    s, c = _sin(angle), _cos(angle)
    return x * c + z * s, -x * s + z * c


def _normalize2(dx, dz):
    mag = math.sqrt(dx * dx + dz * dz)
    if mag < 0.001:
        return 1.0, 0.0
    return dx / mag, dz / mag


def _read_spine(data):
    """Parse road spine from section-5 sub[20]. Returns list of (wx, wy, wz, hw) tuples."""
    sec = struct.unpack_from('<6I', data, 0)
    s5_off = sec[5]
    sub20_rel = struct.unpack_from('<I', data, s5_off + 20 * 4)[0]
    spine_abs = s5_off + sub20_rel
    count = struct.unpack_from('<I', data, spine_abs)[0]
    nodes = []
    for i in range(count):
        off = spine_abs + 4 + i * 20
        f0 = struct.unpack_from('<i', data, off)[0]
        f1 = struct.unpack_from('<i', data, off + 4)[0]
        wy = struct.unpack_from('<h', data, off + 8)[0]
        f5 = struct.unpack_from('<h', data, off + 14)[0]
        if f0 < 0: f0 += 0x3FFF
        if f1 < 0: f1 += 0x3FFF
        nodes.append((F0_FLOOR - (f0 >> 14), wy, f1 >> 14, max(f5 // GTE_SCALE, 100)))
    return nodes


def _parse_obj_lib(data, abs_off, abs_end):
    """Parse one section-1 display list, returning (verts, quads)."""
    verts, quads = [], []
    sd = data[abs_off: min(abs_end, len(data))]
    pos = 0
    while pos + 4 <= len(sd):
        cmd = struct.unpack_from('<H', sd, pos)[0]
        cnt = struct.unpack_from('<H', sd, pos + 2)[0]
        if cnt == 0:
            break
        stride = {0: 40, 1: 48, 2: 32, 3: 64, 4: 72, 5: 56}.get(cmd, 0)
        if not stride:
            break
        if cmd in (0, 1, 3, 4):
            for pi in range(cnt):
                rec = sd[pos + 4 + pi * stride: pos + 4 + (pi + 1) * stride]
                if len(rec) < stride:
                    break
                base = len(verts)
                for j in range(4):
                    x = struct.unpack_from('<h', rec, j * 4)[0]
                    y = struct.unpack_from('<h', rec, j * 4 + 2)[0]
                    z = struct.unpack_from('<h', rec, 16 + j * 2)[0]
                    verts.append((x, y, z))
                quads.append((base, base + 1, base + 2, base + 3))
        pos += 4 + cnt * stride
    return verts, quads


def dump(game_dir, out_path):
    game = Path(game_dir)
    data = (game / 'CRS_EASY.DAT').read_bytes()
    sec  = struct.unpack_from('<6I', data, 0)
    s1_off = sec[1]
    s4_off = sec[4]
    s4_end = sec[5]

    # Road ribbon from spine
    spine = _read_spine(data)
    n = len(spine)

    def tangent(i):
        a = spine[(i - 1) % n]
        b = spine[(i + 1) % n]
        return _normalize2(b[0] - a[0], b[2] - a[2])

    road_v, road_f = [], []
    for i in range(n):
        j = (i + 1) % n
        wx_a, wy_a, wz_a, hw_a = spine[i]
        wx_b, wy_b, wz_b, hw_b = spine[j]
        tax, taz = tangent(i); pax, paz = -taz, tax
        tbx, tbz = tangent(j); pbx, pbz = -tbz, tbx

        corners = [
            ((wx_a + hw_a * pax) * SCALE, -wy_a * SCALE, -(wz_a + hw_a * paz) * SCALE),
            ((wx_a - hw_a * pax) * SCALE, -wy_a * SCALE, -(wz_a - hw_a * paz) * SCALE),
            ((wx_b - hw_b * pbx) * SCALE, -wy_b * SCALE, -(wz_b - hw_b * pbz) * SCALE),
            ((wx_b + hw_b * pbx) * SCALE, -wy_b * SCALE, -(wz_b + hw_b * pbz) * SCALE),
        ]
        base = len(road_v)
        road_v.extend(corners)
        road_f.append((base, base + 1, base + 2, base + 3))

    print(f'Road ribbon: {n} spine nodes, {len(road_f)} quads')
    if road_v:
        xs = [v[0] for v in road_v]; ys = [v[1] for v in road_v]; zs = [v[2] for v in road_v]
        print(f'  GLB bbox X:[{min(xs):.1f},{max(xs):.1f}] Y:[{min(ys):.1f},{max(ys):.1f}] Z:[{min(zs):.1f},{max(zs):.1f}]')

    # Objects (section-1 library + section-4 placements)
    s1_n = struct.unpack_from('<I', data, s1_off)[0]
    s1_offs = [struct.unpack_from('<I', data, s1_off + 4 + i * 4)[0] for i in range(s1_n)]
    lib = []
    for i in range(s1_n):
        abs0 = s1_off + s1_offs[i]
        abs1 = s1_off + (s1_offs[i + 1] if i + 1 < s1_n else sec[2] - s1_off)
        lib.append(_parse_obj_lib(data, abs0, abs1))

    obj_v, obj_f = [], []
    placed = 0
    for i in range((s4_end - s4_off) // 20):
        off   = s4_off + i * 20
        midx  = struct.unpack_from('<H', data, off)[0]
        angle = struct.unpack_from('<H', data, off + 2)[0]
        wx    = struct.unpack_from('<i', data, off + 4)[0]
        wy    = struct.unpack_from('<i', data, off + 8)[0]
        wz    = struct.unpack_from('<i', data, off + 12)[0]
        if midx >= s1_n or (wx == 0 and wz == 0):
            continue
        seg_verts, seg_quads = lib[midx]
        base = len(obj_v)
        for vx, vy, vz in seg_verts:
            rx, rz = _rotate_y(vx, vz, angle)
            obj_v.append((
                (int(rx // GTE_SCALE + wx)) * SCALE,
                -(int(vy // GTE_SCALE + wy)) * SCALE,
                -(int(rz // GTE_SCALE + wz)) * SCALE,
            ))
        for a, b, c, d in seg_quads:
            obj_f.append((base + a, base + b, base + c, base + d))
        placed += 1

    print(f'Objects: {placed} placements, {len(obj_f)} quads')
    if obj_v:
        xs = [v[0] for v in obj_v]; zs = [v[2] for v in obj_v]
        print(f'  GLB bbox X:[{min(xs):.1f},{max(xs):.1f}] Z:[{min(zs):.1f},{max(zs):.1f}]')

    with open(out_path, 'w') as f:
        f.write('# Ridge Racer Revolution - CRS_EASY geometry (road ribbon + objects)\n')
        f.write(f'# spine: {n} nodes  objects: {placed} placements\n\n')

        f.write('o road\n')
        for x, y, z in road_v:
            f.write(f'v {x:.4f} {y:.4f} {z:.4f}\n')
        for a, b, c, d in road_f:
            f.write(f'f {a+1} {b+1} {c+1}\nf {a+1} {c+1} {d+1}\n')

        off = len(road_v)
        f.write('\no objects\n')
        for x, y, z in obj_v:
            f.write(f'v {x:.4f} {y:.4f} {z:.4f}\n')
        for a, b, c, d in obj_f:
            f.write(f'f {a+off+1} {b+off+1} {c+off+1}\n'
                    f'f {a+off+1} {c+off+1} {d+off+1}\n')

    print(f'\nWritten: {out_path}')
    print('Import in Blender: File > Import > Wavefront OBJ, then press Z (wireframe).')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python dump_obj.py <game_data_dir> [output.obj]')
        sys.exit(0)
    out = sys.argv[2] if len(sys.argv) > 2 else 'road_debug.obj'
    dump(sys.argv[1], out)
