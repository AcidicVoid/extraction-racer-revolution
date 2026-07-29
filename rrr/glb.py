# GLB export for PS1 polygon data.
#
# Geometry arrives as quads and is split into two triangles per quad.
#
# Materials are shared across the whole file. There is one material per
# distinct combination of texture page, palette, pixel mode, texture window and
# alpha mode, built once and referenced by every mesh that uses it, so editing a
# texture affects every place it appears. The image for each material is cropped
# to the union of the UVs over the whole export, so sharing does not make the
# images any larger than they need to be. Vertex coloured polygons share a
# single white material and carry their colour in the COLOR_0 attribute.
#
# Polygons with a texture window get the window rectangle as their image, UVs
# expressed in tile units, and a repeating sampler, which reproduces the way
# the hardware wraps them. See displaylist.py for the window layout.
#
# Coordinates are converted from PS1 space to the glTF right-handed Y-up
# convention:
#
#   glTF X =  world_X * scale
#   glTF Y = -world_Y * scale     PS1 Y points down
#   glTF Z = -world_Z * scale     PS1 Z points into the screen

import hashlib
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

# Stand-in colour for a texture that could not be read out of VRAM.
_PINK = (200, 0, 200, 255)


def export_glb(node_list: list, vram, out_path: str,
               scale: float = 1 / 256.0,
               road_opaque: bool = False):
    """
    Write a GLB file containing one named mesh node per entry in node_list.

    node_list   list of (name, [Poly, ...])
    vram        VramSim with the textures for these polygons already loaded
    out_path    destination file path
    scale       world unit to glTF unit conversion factor
    road_opaque when true the first node uses opaque rather than masked alpha
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
    # Sampler 0 clamps, sampler 1 repeats for textures with a window. Both use
    # nearest filtering, which is what the hardware does.
    samps = [Sampler(magFilter=9728, minFilter=9728, wrapS=33071, wrapT=33071),
             Sampler(magFilter=9728, minFilter=9728, wrapS=10497, wrapT=10497)]
    meshes, nodes = [], []

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

    def _mat_key(p, opaque):
        """Identity of the material a polygon needs."""
        return (p.tpage_x, p.tpage_y, p.clut_x, p.clut_y,
                p.mode, p.twin, bool(opaque))

    # First pass over every node, collecting the union of UVs per material so
    # each image can be cropped once for the whole file.
    bounds = {}
    for node_idx, (_, polys) in enumerate(node_list):
        opaque = road_opaque and node_idx == 0
        for p in polys:
            if not p.has_tex:
                continue
            k = _mat_key(p, opaque)
            us = [u for u, v in p.uvs]
            vs = [v for u, v in p.uvs]
            b = bounds.get(k)
            if b is None:
                bounds[k] = [min(us), min(vs), max(us), max(vs)]
            else:
                b[0] = min(b[0], min(us)); b[1] = min(b[1], min(vs))
                b[2] = max(b[2], max(us)); b[3] = max(b[3], max(vs))

    mat_of = {}     # material key to material index
    crop_of = {}    # material key to (u_off, v_off, tex_w, tex_h, tile)
    # Two different palette addresses can hold the same colours, which yields
    # byte-identical images. Reuse a material when both the image and the crop
    # match, so that the UV mapping stays exact.
    by_content = {}

    for k in sorted(bounds, key=lambda x: str(x)):
        tx, ty, cx, cy, tp, twin, op = k
        if twin:
            # With a texture window the crop is the window itself and the
            # sampler repeats it.
            ox, oy, w, h = twin
            u0, v0, u1, v1, pad, tile = ox, oy, ox + w - 1, oy + h - 1, 0, (w, h)
            label = f'tp{tx}_{ty}_cl{cx}_{cy}_win{ox}_{oy}_{w}x{h}'
        else:
            u0, v0, u1, v1 = bounds[k]
            u0 = max(0, u0); v0 = max(0, v0)
            u1 = min(255, u1); v1 = min(255, v1)
            if u1 <= u0:
                u1 = u0 + 1
            if v1 <= v0:
                v1 = v0 + 1
            pad, tile = 2, None
            label = f'tp{tx}_{ty}_cl{cx}_{cy}'
        if op:
            label += '_opaque'
        try:
            img, uo, vo = vram.extract_texture(
                tx, ty, cx, cy, tp, u0, v0, u1, v1, pad=pad)
        except Exception:
            img = Image.new('RGBA', (4, 4), _PINK)
            uo = vo = 0
        buf = io.BytesIO()
        img.save(buf, 'PNG')
        png = buf.getvalue()
        crop = (uo, vo, max(img.width, 1), max(img.height, 1), tile)
        alpha = 'OPAQUE' if op else 'MASK'

        csig = (hashlib.sha1(png).digest(), crop, alpha)
        if csig in by_content:
            mat_of[k] = by_content[csig]
            crop_of[k] = crop
            continue

        gimgs.append(GltfImage(bufferView=_add_view(png),
                               mimeType='image/png'))
        gtexs.append(Texture(sampler=1 if tile else 0, source=len(gimgs) - 1))
        mats.append(Material(
            name=label,
            pbrMetallicRoughness=PbrMetallicRoughness(
                baseColorTexture=TextureInfo(index=len(gtexs) - 1),
                metallicFactor=0.0, roughnessFactor=1.0),
            alphaMode=alpha,
            alphaCutoff=0.5 if alpha == 'MASK' else None,
            doubleSided=True))
        mat_of[k] = len(mats) - 1
        crop_of[k] = crop
        by_content[csig] = len(mats) - 1

    # One shared material for untextured polygons, which carry their colour in
    # the vertex attribute and only need a white pixel to modulate.
    vc_mat = None
    if any(not p.has_tex for _, polys in node_list for p in polys):
        buf = io.BytesIO()
        Image.new('RGBA', (1, 1), (255, 255, 255, 255)).save(buf, 'PNG')
        gimgs.append(GltfImage(bufferView=_add_view(buf.getvalue()),
                               mimeType='image/png'))
        gtexs.append(Texture(sampler=0, source=len(gimgs) - 1))
        mats.append(Material(
            name='vertex_colors',
            pbrMetallicRoughness=PbrMetallicRoughness(
                baseColorTexture=TextureInfo(index=len(gtexs) - 1),
                metallicFactor=0.0, roughnessFactor=1.0),
            alphaMode='MASK', alphaCutoff=0.5, doubleSided=True))
        vc_mat = len(mats) - 1

    def _build_prims(polys: list, opaque: bool = False) -> list:
        """Convert a list of Poly objects into a list of GLB primitives."""
        groups = defaultdict(list)
        for p in polys:
            groups[_mat_key(p, opaque) if p.has_tex else None].append(p)

        prims = []

        def _flush(group: list, mid: int,
                   u_off: int, v_off: int, tw: int, th: int,
                   tile: tuple = None):
            """
            Emit one primitive for a group of polygons sharing a material.

            When tile is set the polygons wrap inside a texture window, so UVs
            are written in tile units and values above 1 are expected; the
            repeating sampler turns them back into a wrap.
            """
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

        for k in sorted(groups, key=lambda x: str(x)):
            grp = groups[k]
            if k is None:
                if vc_mat is not None:
                    _flush(grp, vc_mat, 0, 0, 1, 1)
                continue
            uo, vo, tw, th, tile = crop_of[k]
            _flush(grp, mat_of[k], uo, vo, tw, th, tile)

        return prims

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
