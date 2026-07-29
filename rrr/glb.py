# GLB export for PS1 polygon data.
#
# All geometry is stored as quads (4 verts), split into two triangles on export.
# Each unique (TPAGE, CLUT) combination becomes one GLB material with a cropped
# texture atlas extracted from VRAM.  Vertex-colored polys (CMD2/CMD5) share a
# single white-texture material and rely on the COLOR_0 vertex attribute.
#
# Coordinate conversion from PS1 to glTF (Y-up right-hand):
#   glTF X =  world_X * scale
#   glTF Y = -world_Y * scale    (PS1 Y is down)
#   glTF Z = -world_Z * scale    (PS1 Z is into screen)
#
# The 'scale' argument is chosen per asset type:
#   Cars / props:   1/256  (model units -> reasonable glTF meters)
#   Track geometry: 1/256  (world units in the 2000-60000 range -> 8-235 m)

import io
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import pygltflib
    from pygltflib import (
        GLTF2, Scene, Node, Mesh, Primitive, Accessor, BufferView, Buffer,
        Material, Texture, Sampler,
        ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER, FLOAT, UNSIGNED_SHORT,
        SCALAR, VEC2, VEC3, VEC4, TRIANGLES,
    )
    from pygltflib import Image as GltfImage, PbrMetallicRoughness, TextureInfo
    HAS_GLTF = True
except ImportError:
    HAS_GLTF = False

_PINK = (200, 0, 200, 255)   # fallback color for missing textures


def _white_png() -> bytes:
    buf = io.BytesIO()
    Image.new('RGBA', (1, 1), (255, 255, 255, 255)).save(buf, 'PNG')
    return buf.getvalue()


def _cluster_by_uv(polys: list, gap: int = 4) -> list:
    """
    Split a (TPAGE, CLUT, mode) group into clusters of polys whose UV rects
    overlap or nearly touch.

    Taking one union bounding box for the whole group is wrong when a model
    samples several widely-separated regions of the same texture page: the
    resulting crop is a large slab of the page holding many unrelated
    textures, i.e. a texture atlas gets baked onto the object.  That happens
    whenever CMD 0 and CMD 1 polys of one model share a page and CLUT --
    CMD 1 quads routinely sample far-away parts of the page.

    Clustering keeps the crop tight around each actual texture while still
    letting polys that genuinely share one texture share one material, so the
    material count stays far below one-material-per-polygon.

    *gap* is the slack in texels; it should be at least the pad used when
    extracting, so neighbouring rects that the pad would merge anyway end up
    in the same cluster.
    """
    n = len(polys)
    if n < 2:
        return [polys] if polys else []

    rects = []
    for p in polys:
        us = [u for u, v in p.uvs]
        vs = [v for u, v in p.uvs]
        rects.append((min(us), min(vs), max(us), max(vs)))

    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def merge(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Sort by u0 so we can stop scanning once rects are out of reach.
    order = sorted(range(n), key=lambda i: rects[i][0])
    for oi in range(n):
        i = order[oi]
        au0, av0, au1, av1 = rects[i]
        for oj in range(oi + 1, n):
            j = order[oj]
            bu0, bv0, bu1, bv1 = rects[j]
            if bu0 - gap > au1:
                break            # sorted by u0: nothing further can overlap
            if av0 - gap <= bv1 and bv0 - gap <= av1:
                merge(i, j)

    groups = {}
    for i, p in enumerate(polys):
        groups.setdefault(find(i), []).append(p)
    return list(groups.values())


def export_glb(node_list: list, vram, out_path: str,
               scale: float = 1 / 256.0,
               road_opaque: bool = False):
    """
    Write a GLB file containing multiple named mesh nodes.

    node_list   - list of (name, [Poly, ...])
    vram        - VramSim instance with textures already loaded
    out_path    - destination file path
    scale       - world-unit to glTF-meter conversion factor
    road_opaque - if True, the first node uses OPAQUE alpha (road surface)
    """
    if not HAS_GLTF:
        print('  [GLB skipped: pygltflib not installed]')
        return

    node_list = [(n, p) for n, p in node_list if p]
    if not node_list:
        return

    gltf = GLTF2()
    gltf.asset = pygltflib.Asset(version='2.0')
    blob = bytearray()
    bviews, accs, mats, gtexs, gimgs = [], [], [], [], []
    # 0 = clamped (normal polys), 1 = repeating (polys with a texture window).
    samps = [Sampler(magFilter=9728, minFilter=9728, wrapS=33071, wrapT=33071),
             Sampler(magFilter=9728, minFilter=9728, wrapS=10497, wrapT=10497)]
    meshes, nodes = [], []

    # -- binary buffer helpers -----------------------------------------------

    def _add_view(raw: bytes, target=None) -> int:
        off = len(blob)
        blob.extend(raw)
        while len(blob) % 4:
            blob.append(0)
        bv = BufferView(buffer=0, byteOffset=off, byteLength=len(raw))
        if target:
            bv.target = target
        bviews.append(bv)
        return len(bviews) - 1

    def _add_acc(bv: int, ctype, atype, count: int,
                 mn=None, mx=None) -> int:
        a = Accessor(bufferView=bv, byteOffset=0,
                     componentType=ctype, count=count, type=atype)
        if mn is not None:
            a.min = mn
        if mx is not None:
            a.max = mx
        accs.append(a)
        return len(accs) - 1

    # -- mesh builder --------------------------------------------------------

    def _build_prims(polys: list, opaque: bool = False) -> list:
        """Convert a list of Poly objects into a list of GLB Primitive objects."""
        tex_groups = defaultdict(list)
        col_polys = []
        for p in polys:
            if p.has_tex:
                # The texture window is part of the material identity: polys
                # sharing a page and CLUT but wrapping inside different
                # windows need different tiles.
                tex_groups[(p.tpage_x, p.tpage_y,
                            p.clut_x,  p.clut_y, p.mode, p.twin)].append(p)
            else:
                col_polys.append(p)

        prims = []
        alpha = 'OPAQUE' if opaque else 'MASK'

        def _flush(group: list, img: Image.Image,
                   u_off: int, v_off: int, mat_name: str,
                   tile: tuple = None):
            # tile = (w, h) when the polys wrap inside a texture window.  UVs
            # are then emitted in tile units (values above 1 are intended) and
            # the sampler repeats, reproducing the PS1 wrap.
            tw = max(img.width, 1)
            th = max(img.height, 1)
            buf = io.BytesIO()
            img.save(buf, 'PNG')
            gimgs.append(GltfImage(bufferView=_add_view(buf.getvalue()),
                                   mimeType='image/png'))
            gtexs.append(Texture(sampler=1 if tile else 0,
                                 source=len(gimgs) - 1))
            mats.append(Material(
                name=mat_name,
                pbrMetallicRoughness=PbrMetallicRoughness(
                    baseColorTexture=TextureInfo(index=len(gtexs) - 1),
                    metallicFactor=0.0, roughnessFactor=1.0),
                alphaMode=alpha,
                alphaCutoff=0.5 if alpha == 'MASK' else None,
                doubleSided=True))
            mid = len(mats) - 1

            cache = {}
            pos_list, uv_list, col_list, idx_list = [], [], [], []

            def _v(x, y, z, u, v, r, g, b):
                key = (x, y, z, u, v, r, g, b)
                if key not in cache:
                    cache[key] = len(pos_list)
                    pos_list.append([x * scale, -y * scale, -z * scale])
                    if tile:
                        uv_list.append([u / tile[0], v / tile[1]])
                    else:
                        uv_list.append([(u - u_off) / tw, (v - v_off) / th])
                    col_list.append([r / 255, g / 255, b / 255, 1.0])
                return cache[key]

            for p in group:
                cr, cg, cb = p.color
                vi = [_v(p.verts[j][0], p.verts[j][1], p.verts[j][2],
                         p.uvs[j][0],   p.uvs[j][1],
                         cr, cg, cb) for j in range(4)]
                idx_list += [vi[0], vi[1], vi[2], vi[1], vi[3], vi[2]]

            if not pos_list:
                return
            pa = np.array(pos_list, np.float32)
            ua = np.array(uv_list,  np.float32)
            ca = np.array(col_list, np.float32)
            ia = np.array(idx_list, np.uint16)

            ap = _add_acc(_add_view(pa.tobytes(), ARRAY_BUFFER),
                          FLOAT, VEC3, len(pos_list),
                          pa.min(0).tolist(), pa.max(0).tolist())
            au = _add_acc(_add_view(ua.tobytes(), ARRAY_BUFFER),
                          FLOAT, VEC2, len(uv_list))
            ac = _add_acc(_add_view(ca.tobytes(), ARRAY_BUFFER),
                          FLOAT, VEC4, len(col_list))
            ai = _add_acc(_add_view(ia.tobytes(), ELEMENT_ARRAY_BUFFER),
                          UNSIGNED_SHORT, SCALAR, len(idx_list))
            prims.append(Primitive(
                attributes=pygltflib.Attributes(
                    POSITION=ap, TEXCOORD_0=au, COLOR_0=ac),
                indices=ai, material=mid, mode=TRIANGLES))

        for (tx, ty, cx, cy, tp, twin), grp in sorted(
                tex_groups.items(), key=lambda kv: str(kv[0])):

            # Texture-window polys: extract exactly the window and let the
            # sampler repeat it.  No UV clustering -- the tile is the crop.
            if twin:
                ox, oy, w, h = twin
                try:
                    img, uo, vo = vram.extract_texture(
                        tx, ty, cx, cy, tp,
                        ox, oy, ox + w - 1, oy + h - 1, pad=0)
                except Exception:
                    img = Image.new('RGBA', (max(w, 1), max(h, 1)), _PINK)
                    uo, vo = ox, oy
                _flush(grp, img, uo, vo,
                       f'tp{tx}_{ty}_cl{cx}_{cy}_win{ox}_{oy}_{w}x{h}',
                       tile=(w, h))
                continue

            # One material per cluster of touching UV rects, not one per
            # (TPAGE, CLUT) union -- see _cluster_by_uv.
            for ci, cluster in enumerate(_cluster_by_uv(grp, gap=4)):
                uvs = [uv for p in cluster for uv in p.uvs]
                u0 = max(0,   min(u for u, v in uvs))
                u1 = min(255, max(u for u, v in uvs))
                v0 = max(0,   min(v for u, v in uvs))
                v1 = min(255, max(v for u, v in uvs))
                if u1 <= u0:
                    u1 = u0 + 1
                if v1 <= v0:
                    v1 = v0 + 1
                try:
                    img, uo, vo = vram.extract_texture(
                        tx, ty, cx, cy, tp, u0, v0, u1, v1, pad=2)
                except Exception:
                    img = Image.new('RGBA', (4, 4), _PINK)
                    uo = vo = 0
                _flush(cluster, img, uo, vo,
                       f'tp{tx}_{ty}_cl{cx}_{cy}_{ci}')

        if col_polys:
            white = Image.new('RGBA', (1, 1), (255, 255, 255, 255))
            _flush(col_polys, white, 0, 0, 'vertex_colors')

        return prims

    # -- assemble scene ------------------------------------------------------

    scene_node_indices = []
    for node_idx, (node_name, polys) in enumerate(node_list):
        opaque = road_opaque and node_idx == 0
        prims = _build_prims(polys, opaque=opaque)
        if not prims:
            continue
        meshes.append(Mesh(name=node_name, primitives=prims))
        nodes.append(Node(name=node_name, mesh=len(meshes) - 1))
        scene_node_indices.append(len(nodes) - 1)

    if not meshes:
        return

    gltf.meshes = meshes
    gltf.nodes = nodes
    gltf.scenes = [Scene(nodes=scene_node_indices)]
    gltf.scene = 0
    gltf.materials = mats
    gltf.textures = gtexs
    gltf.images = gimgs
    gltf.samplers = samps
    gltf.bufferViews = bviews
    gltf.accessors = accs
    gltf.buffers = [Buffer(byteLength=len(blob))]
    gltf.set_binary_blob(bytes(blob))
    gltf.save_binary(out_path)

    total = sum(len(p) for _, p in node_list)
    print(f'  -> {Path(out_path).name}  ({len(node_list)} nodes, {total} polys)')
