# Merge two GLB files into a single GLB.
#
# Used to combine the section-2 and section-3 track exports.  Rather than
# concatenating the two files and offsetting every index, this rebuilds the
# output from scratch, which lets identical materials be shared.
#
# Materials are deduplicated by CONTENT: two materials merge when their image
# bytes, sampler and alpha/PBR settings all match.  The two section exports
# reference the same shared BIG*.TMS pages, so without this the merged file
# carries two copies of every non-course texture and editing one of them in
# Blender only changes half the track.
#
# Anything not referenced by a surviving primitive is simply never copied, so
# the merged blob contains no orphaned image or accessor data.

import hashlib
from pathlib import Path

from pygltflib import (GLTF2, Scene, Node, Mesh, Primitive, Buffer, BufferView,
                       Accessor, Material, Texture, Sampler, Attributes,
                       Image as GImage, PbrMetallicRoughness, TextureInfo,
                       ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER, TRIANGLES)

# glTF componentType -> bytes per component
_CSIZE = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
_NCOMP = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT4': 16}

_ATTRS = ('POSITION', 'NORMAL', 'TANGENT', 'TEXCOORD_0', 'TEXCOORD_1',
          'COLOR_0', 'JOINTS_0', 'WEIGHTS_0')


def merge_glb(path_a: str, path_b: str, out_path: str):
    """
    Merge two GLB files into a single GLB at out_path.

    All nodes from both files appear in the output scene.  Materials that are
    byte-for-byte equivalent are collapsed into one.
    """
    srcs = [(GLTF2.load(p)) for p in (path_a, path_b)]

    blob = bytearray()
    bviews, accs = [], []
    mats, texs, imgs, samps = [], [], [], []
    meshes, nodes = [], []

    sampler_map = {}
    mat_map = {}

    def add_view(raw: bytes, target=None) -> int:
        off = len(blob)
        blob.extend(raw)
        while len(blob) % 4:
            blob.append(0)
        bv = BufferView(buffer=0, byteOffset=off, byteLength=len(raw))
        if target:
            bv.target = target
        bviews.append(bv)
        return len(bviews) - 1

    def view_bytes(g, gblob, bv_index):
        bv = g.bufferViews[bv_index]
        return bytes(gblob[bv.byteOffset: bv.byteOffset + bv.byteLength])

    def copy_accessor(g, gblob, idx, target):
        a = g.accessors[idx]
        bv = g.bufferViews[a.bufferView]
        start = bv.byteOffset + (a.byteOffset or 0)
        nbytes = a.count * _NCOMP[a.type] * _CSIZE[a.componentType]
        raw = bytes(gblob[start: start + nbytes])
        na = Accessor(bufferView=add_view(raw, target), byteOffset=0,
                      componentType=a.componentType, count=a.count, type=a.type)
        if a.min is not None:
            na.min = list(a.min)
        if a.max is not None:
            na.max = list(a.max)
        accs.append(na)
        return len(accs) - 1

    def get_sampler(g, tex):
        if tex.sampler is None:
            return None
        s = g.samplers[tex.sampler]
        key = (s.magFilter, s.minFilter, s.wrapS, s.wrapT)
        if key not in sampler_map:
            samps.append(Sampler(magFilter=s.magFilter, minFilter=s.minFilter,
                                 wrapS=s.wrapS, wrapT=s.wrapT))
            sampler_map[key] = len(samps) - 1
        return sampler_map[key]

    def get_material(g, gblob, idx):
        m = g.materials[idx]
        pbr = m.pbrMetallicRoughness
        tex_idx = None
        if pbr is not None and pbr.baseColorTexture is not None:
            tex_idx = pbr.baseColorTexture.index

        img_bytes = b''
        skey = None
        mime = 'image/png'
        if tex_idx is not None:
            tex = g.textures[tex_idx]
            img = g.images[tex.source]
            img_bytes = view_bytes(g, gblob, img.bufferView)
            mime = img.mimeType or 'image/png'
            s = g.samplers[tex.sampler] if tex.sampler is not None else None
            skey = (s.magFilter, s.minFilter, s.wrapS, s.wrapT) if s else None

        sig = (hashlib.sha1(img_bytes).digest(), skey, m.alphaMode,
               m.alphaCutoff, m.doubleSided,
               None if pbr is None else pbr.metallicFactor,
               None if pbr is None else pbr.roughnessFactor,
               None if pbr is None else (tuple(pbr.baseColorFactor)
                                         if pbr.baseColorFactor else None))
        if sig in mat_map:
            return mat_map[sig]

        new_pbr = PbrMetallicRoughness()
        if pbr is not None:
            if pbr.baseColorFactor is not None:
                new_pbr.baseColorFactor = list(pbr.baseColorFactor)
            new_pbr.metallicFactor = pbr.metallicFactor
            new_pbr.roughnessFactor = pbr.roughnessFactor
        if tex_idx is not None:
            si = get_sampler(g, g.textures[tex_idx])
            imgs.append(GImage(bufferView=add_view(img_bytes), mimeType=mime))
            texs.append(Texture(sampler=si, source=len(imgs) - 1))
            new_pbr.baseColorTexture = TextureInfo(index=len(texs) - 1)
        nm = Material(name=m.name, pbrMetallicRoughness=new_pbr,
                      alphaMode=m.alphaMode, alphaCutoff=m.alphaCutoff,
                      doubleSided=m.doubleSided)
        mats.append(nm)
        mat_map[sig] = len(mats) - 1
        return mat_map[sig]

    for g in srcs:
        gblob = g.binary_blob() or b''
        for mesh in (g.meshes or []):
            prims = []
            for pr in (mesh.primitives or []):
                new_attrs = Attributes()
                for an in _ATTRS:
                    ai = getattr(pr.attributes, an, None)
                    if ai is None:
                        continue
                    setattr(new_attrs, an,
                            copy_accessor(g, gblob, ai, ARRAY_BUFFER))
                ii = (copy_accessor(g, gblob, pr.indices, ELEMENT_ARRAY_BUFFER)
                      if pr.indices is not None else None)
                mid = (get_material(g, gblob, pr.material)
                       if pr.material is not None else None)
                prims.append(Primitive(attributes=new_attrs, indices=ii,
                                       material=mid,
                                       mode=pr.mode if pr.mode is not None
                                       else TRIANGLES))
            if not prims:
                continue
            meshes.append(Mesh(name=mesh.name, primitives=prims))
            nodes.append(Node(name=mesh.name, mesh=len(meshes) - 1))

    out = GLTF2()
    out.asset = srcs[0].asset
    out.meshes = meshes
    out.nodes = nodes
    out.scenes = [Scene(nodes=list(range(len(nodes))))]
    out.scene = 0
    out.materials = mats
    out.textures = texs
    out.images = imgs
    out.samplers = samps
    out.bufferViews = bviews
    out.accessors = accs
    out.buffers = [Buffer(byteLength=len(blob))]
    out.set_binary_blob(bytes(blob))

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    out.save_binary(out_path)
    print(f'  -> {Path(out_path).name}  (merged: {len(nodes)} nodes, '
          f'{len(mats)} materials)')
