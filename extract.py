# Ridge Racer Revolution asset extractor.
#
# Usage: python extract.py <game_data_dir> <output_dir> [--keep-sections]
# Requirements: pip install Pillow numpy pygltflib

import shutil
import sys
import tempfile
from pathlib import Path

from rrr.tms import parse_tms, render_block
from rrr.vram import VramSim
from rrr.car import (parse_car_rso, CAR_TABLE, ALL_CAR_INDICES,
                     ENTRY_LABEL, WHEEL_CARS)
from rrr.track import parse_crs, load_course_textures, compute_section_map, CLUT_FILES
from rrr.merge_glb import merge_glb
from rrr.glb import export_glb
from rrr.displaylist import Poly


# Layout of the combined car grid export.
_GRID_COLS      = 5
_GRID_SPACING_X = 900
_GRID_SPACING_Z = 800
_WHEEL_OFFSETS  = {
    'FL': (-145, -400),
    'FR': ( 145, -400),
    'RL': (-145,  70),
    'RR': ( 145,  70),
}

# Load order for the shared texture files, matching the game's startup
# sequence so that later files overwrite earlier ones.
_BIG_LOAD_ORDER = ('BIG4.TMS', 'BIG0.TMS', 'BIG3.TMS', 'BIG1.TMS', 'BIG2.TMS')


def _offset_polys(polys: list, dx: int, dz: int) -> list:
    """Return a new list of Poly objects shifted by (dx, dz) in X/Z."""
    return [
        Poly([(v[0] + dx, v[1], v[2] + dz) for v in p.verts],
             p.uvs, p.tpage_x, p.tpage_y,
             p.clut_x, p.clut_y, p.mode, p.has_tex, p.color, p.twin)
        for p in polys
    ]


def _export_cars(car_entries: list, vram: VramSim, out: Path):
    """Export individual car GLBs and a combined grid overview."""
    car_dir  = out / 'cars'
    part_dir = out / 'car_parts'
    prop_dir = out / 'props'

    # One file per car body.
    for i, (body, shadow, under, wheel, name) in enumerate(CAR_TABLE):
        polys = car_entries[body] if body < len(car_entries) else []
        if polys:
            export_glb([(name, polys)], vram,
                       str(car_dir / f'{i:02d}_{name}.glb'),
                       scale=1 / 256.0)

    # All car bodies and wheels laid out on a grid in one scene.
    grid_nodes = []
    for idx, (body, shadow, under, wheel, name) in enumerate(CAR_TABLE):
        col = idx % _GRID_COLS
        row = idx // _GRID_COLS
        ox  = col * _GRID_SPACING_X
        oz  = row * _GRID_SPACING_Z
        if body < len(car_entries) and car_entries[body]:
            grid_nodes.append((name, _offset_polys(car_entries[body], ox, oz)))
        if wheel < len(car_entries) and car_entries[wheel]:
            for label, (wx, wz) in _WHEEL_OFFSETS.items():
                grid_nodes.append(
                    (f'{name}_WHEEL_{label}',
                     _offset_polys(car_entries[wheel], ox + wx, oz + wz)))
    if grid_nodes:
        export_glb(grid_nodes, vram,
                   str(car_dir / 'all_cars_grid.glb'), scale=1 / 256.0)

    # Remaining CAR.RSO entries: wheels, special vehicles and scenery props.
    for eid, polys in enumerate(car_entries):
        if eid in ALL_CAR_INDICES or not polys:
            continue
        if eid in ENTRY_LABEL:
            car_name, role = ENTRY_LABEL[eid]
            label  = f'{car_name}_{role}'
            folder = part_dir
        elif 35 <= eid <= 50 or eid in (117, 118):
            cars  = WHEEL_CARS.get(eid, [])
            label = 'wheel_' + ('_'.join(cars) if cars else str(eid))
            folder = part_dir
        elif eid in (51, 64, 65, 66):
            label  = {51: 'blimp', 64: 'plane',
                      65: 'helicopter', 66: 'heli_rotor'}[eid]
            folder = part_dir
        else:
            label  = f'prop_{eid:03d}'
            folder = prop_dir
        export_glb([(label, polys)], vram,
                   str(folder / f'{eid:03d}_{label}.glb'), scale=1 / 256.0)


def extract(game_dir: str, out_dir: str, keep_sections: bool = False):
    """
    Extract every asset from the game data into out_dir.

    Each track is built as two exports, one per course texture, which are then
    merged into a single file. Those per-section exports are intermediate
    products and are discarded unless keep_sections is true.
    """
    game = Path(game_dir)
    out  = Path(out_dir)
    for sub in ('textures', 'cars', 'car_parts', 'props', 'tracks'):
        (out / sub).mkdir(parents=True, exist_ok=True)

    # Textures.
    print('\n=== Loading BIG*.TMS textures ===')
    vram = VramSim()
    all_blocks = []
    for fname in _BIG_LOAD_ORDER:
        path = game / fname
        if not path.exists():
            print(f'  [skip] {fname}')
            continue
        blocks = parse_tms(path.read_bytes(), fname)
        for blk in blocks:
            blk.image = render_block(blk)
            vram.load_tms_block(blk)
            all_blocks.append((fname, blk))

    print(f'\n=== Exporting {len(all_blocks)} texture PNGs ===')
    tex_dir = out / 'textures'
    for fname, blk in all_blocks:
        stem = fname.replace('.', '_')
        dest = tex_dir / (f'{stem}_b{blk.index:03d}'
                          f'_{blk.img_x}x{blk.img_y}'
                          f'_{blk.pixel_width}x{blk.img_h}.png')
        if blk.image and blk.image.width > 0 and blk.image.height > 0:
            blk.image.save(str(dest))

    # Cars, car parts and props.
    print('\n=== Car models ===')
    car_rso = game / 'CAR.RSO'
    if car_rso.exists():
        car_entries = parse_car_rso(car_rso.read_bytes())
        print(f'  {len(car_entries)} entries in CAR.RSO')
        _export_cars(car_entries, vram, out)
    else:
        print('  [CAR.RSO not found]')

    # Tracks.
    print('\n=== Tracks ===')
    # VRAM with only the shared BIG textures, reused as the base for each course.
    base_vram = bytes(vram.mem)

    for crs_path in sorted(game.glob('CRS_*.DAT')):
        print(f'\n  {crs_path.name}')
        crs_data = crs_path.read_bytes()
        stem_upper = crs_path.stem.upper()

        # Palette files, shared by both course texture sections.
        clut_names = CLUT_FILES.get(stem_upper, [])
        clut_data_list = []
        for cname in clut_names:
            cp = game / cname
            if cp.exists():
                clut_data_list.append(cp.read_bytes())
            else:
                print(f'  [skip] {cname} not found')

        road_nodes, named_placements = parse_crs(crs_data)
        node_list = road_nodes + named_placements
        if not any(polys for _, polys in node_list):
            continue

        stem = crs_path.stem.lower()
        track_dir = out / 'tracks'

        # Decide which course texture each node uses.
        sec_map = compute_section_map(crs_data, road_nodes, named_placements)

        # Export each group against its own course texture, then merge.
        s2_nodes = [(n, p) for n, p in node_list if sec_map.get(n, 3) == 2]
        s3_nodes = [(n, p) for n, p in node_list if sec_map.get(n, 3) == 3]

        merged_path = str(track_dir / f'{stem}.glb')

        # The per-section files are inputs to the merge and hold nothing the
        # merged file lacks, so by default they go to a scratch directory and
        # are discarded. keep_sections writes them alongside the merged file.
        with tempfile.TemporaryDirectory() as scratch:
            section_dir = track_dir if keep_sections else Path(scratch)
            s2_path = str(section_dir / f'{stem}_s2.glb')
            s3_path = str(section_dir / f'{stem}_s3.glb')

            if s2_nodes:
                vram.mem[:] = base_vram
                load_course_textures(crs_data, vram, clut_data_list, section=2)
                export_glb(s2_nodes, vram, s2_path, scale=1 / 256.0)

            if s3_nodes:
                vram.mem[:] = base_vram
                load_course_textures(crs_data, vram, clut_data_list, section=3)
                export_glb(s3_nodes, vram, s3_path, scale=1 / 256.0)

            if s2_nodes and s3_nodes:
                merge_glb(s2_path, s3_path, merged_path)
                print(f'  merged -> {merged_path}')
            elif s2_nodes or s3_nodes:
                # Only one section has geometry, so there is nothing to merge.
                single = s2_path if s2_nodes else s3_path
                if keep_sections:
                    shutil.copyfile(single, merged_path)
                else:
                    shutil.move(single, merged_path)

    print(f'\n=== Done. Output: {out} ===')


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    flags = {a for a in sys.argv[1:] if a.startswith('-')}
    unknown = flags - {'--keep-sections'}
    if len(args) < 2 or unknown:
        if unknown:
            print(f'unknown option: {", ".join(sorted(unknown))}')
        print('Usage: python extract.py <game_data_dir> <output_dir> '
              '[--keep-sections]')
        print('  --keep-sections  also write the per-section track exports '
              'that the merged file is built from')
        sys.exit(0)
    extract(args[0], args[1], keep_sections='--keep-sections' in flags)
