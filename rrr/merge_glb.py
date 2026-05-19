# Merge two GLB files into a single GLB.
#
# This combines the binary blobs, materials, textures, meshes, and nodes
# from two separate GLBs into one output.  Used to merge S2-textured and
# S3-textured track exports.

from pathlib import Path
from pygltflib import GLTF2, Scene, Node, Mesh, Primitive, Buffer, \
    BufferView, Accessor, Material, Texture, Sampler, Image as GImage


def merge_glb(path_a: str, path_b: str, out_path: str):
    """
    Merge two GLB files into a single GLB at out_path.

    All nodes from both files appear in the output scene.
    Buffer data is concatenated; all indices in file B are offset
    by the counts from file A.
    """
    a = GLTF2.load(path_a)
    b = GLTF2.load(path_b)

    blob_a = bytearray(a.binary_blob() or b'')
    blob_b = bytearray(b.binary_blob() or b'')

    # Pad blob_a to 4-byte alignment before appending blob_b.
    while len(blob_a) % 4:
        blob_a.append(0)
    blob_b_offset = len(blob_a)

    merged_blob = blob_a + blob_b

    # Count offsets for index remapping.
    n_bv  = len(a.bufferViews  or [])
    n_acc = len(a.accessors     or [])
    n_mat = len(a.materials     or [])
    n_tex = len(a.textures      or [])
    n_img = len(a.images        or [])
    n_smp = len(a.samplers      or [])
    n_msh = len(a.meshes        or [])
    n_nod = len(a.nodes         or [])

    # --- Merge buffer views (offset B's byte offsets) ---
    bviews = list(a.bufferViews or [])
    for bv in (b.bufferViews or []):
        new_bv = BufferView(
            buffer=0,
            byteOffset=bv.byteOffset + blob_b_offset,
            byteLength=bv.byteLength,
        )
        if bv.target is not None:
            new_bv.target = bv.target
        if bv.byteStride is not None:
            new_bv.byteStride = bv.byteStride
        bviews.append(new_bv)

    # --- Merge accessors (offset B's bufferView indices) ---
    accs = list(a.accessors or [])
    for ac in (b.accessors or []):
        new_ac = Accessor(
            bufferView=ac.bufferView + n_bv if ac.bufferView is not None else None,
            componentType=ac.componentType,
            type=ac.type,
            count=ac.count,
        )
        if ac.byteOffset is not None:
            new_ac.byteOffset = ac.byteOffset
        if ac.min is not None:
            new_ac.min = ac.min
        if ac.max is not None:
            new_ac.max = ac.max
        accs.append(new_ac)

    # --- Merge images (offset B's bufferView indices) ---
    imgs = list(a.images or [])
    for im in (b.images or []):
        new_im = GImage(
            bufferView=im.bufferView + n_bv if im.bufferView is not None else None,
            mimeType=im.mimeType,
        )
        if im.uri is not None:
            new_im.uri = im.uri
        if im.name is not None:
            new_im.name = im.name
        imgs.append(new_im)

    # --- Merge samplers ---
    samps = list(a.samplers or [])
    samps.extend(b.samplers or [])

    # --- Merge textures (offset B's source/sampler indices) ---
    texs = list(a.textures or [])
    for tx in (b.textures or []):
        new_tx = Texture(
            source=tx.source + n_img if tx.source is not None else None,
            sampler=tx.sampler + n_smp if tx.sampler is not None else None,
        )
        texs.append(new_tx)

    # --- Merge materials (offset B's texture indices in PBR) ---
    mats = list(a.materials or [])
    for mt in (b.materials or []):
        # Deep-copy the material and offset texture indices.
        new_mt = Material()
        new_mt.name = mt.name
        new_mt.doubleSided = mt.doubleSided
        new_mt.alphaMode = mt.alphaMode
        new_mt.alphaCutoff = mt.alphaCutoff

        if mt.pbrMetallicRoughness is not None:
            pbr = dict(mt.pbrMetallicRoughness)
            if 'baseColorTexture' in pbr and pbr['baseColorTexture'] is not None:
                bct = dict(pbr['baseColorTexture'])
                if 'index' in bct and bct['index'] is not None:
                    bct['index'] = bct['index'] + n_tex
                pbr['baseColorTexture'] = bct
            if 'baseColorFactor' in pbr:
                pbr['baseColorFactor'] = pbr['baseColorFactor']
            new_mt.pbrMetallicRoughness = pbr
        mats.append(new_mt)

    # --- Merge meshes (offset B's accessor/material indices in primitives) ---
    meshes = list(a.meshes or [])
    for msh in (b.meshes or []):
        new_prims = []
        for p in (msh.primitives or []):
            new_attrs = {}
            for attr_name, acc_idx in (p.attributes.__dict__.items()
                                        if hasattr(p.attributes, '__dict__')
                                        else p.attributes.items()
                                        if isinstance(p.attributes, dict)
                                        else []):
                if acc_idx is not None:
                    new_attrs[attr_name] = acc_idx + n_acc
            new_p = Primitive(
                attributes=new_attrs,
                indices=p.indices + n_acc if p.indices is not None else None,
                material=p.material + n_mat if p.material is not None else None,
            )
            new_prims.append(new_p)
        new_msh = Mesh(name=msh.name, primitives=new_prims)
        meshes.append(new_msh)

    # --- Merge nodes (offset B's mesh indices) ---
    nodes = list(a.nodes or [])
    for nd in (b.nodes or []):
        new_nd = Node(
            name=nd.name,
            mesh=nd.mesh + n_msh if nd.mesh is not None else None,
        )
        nodes.append(new_nd)

    # --- Build combined scene with all nodes ---
    all_node_indices = list(range(len(nodes)))

    # --- Assemble output ---
    out = GLTF2()
    out.bufferViews = bviews
    out.accessors = accs
    out.images = imgs
    out.samplers = samps
    out.textures = texs
    out.materials = mats
    out.meshes = meshes
    out.nodes = nodes
    out.scenes = [Scene(nodes=all_node_indices)]
    out.scene = 0
    out.buffers = [Buffer(byteLength=len(merged_blob))]
    out.set_binary_blob(bytes(merged_blob))

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    out.save_binary(out_path)
    print(f'  -> {Path(out_path).name}  (merged: {len(nodes)} nodes)')
