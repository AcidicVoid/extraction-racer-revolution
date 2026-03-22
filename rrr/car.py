# CAR.RSO - car and vehicle model archive.
#
# File layout:
#   [0x00] u32  entry_count  (119 total)
#   [0x04] entry_count * u32  absolute file offsets to each entry
#   [...] entry data - each entry is a display list (see displaylist.py)
#
# Car selection table maps 15 playable cars to their model entries:
#   (body_idx, shadow_idx, underside_idx, wheel_idx, car_name)
# The same wheel model is often shared between multiple cars.
#
# Notable non-car entries (verified by manual inspection):
#   51  - blimp
#   64  - plane
#   65  - helicopter body
#   66  - helicopter rotor

import struct
from collections import defaultdict
from rrr.displaylist import parse_display_list


# Each row: (body, shadow, underside, wheel, name)
CAR_TABLE = [
    ( 0, 23,  1, 35, 'FA_RACING'),
    ( 6, 24,  1, 35, 'RT_RYUKYU'),
    (21, 33, 17, 43, 'RT_YELLOW_SOLVALOU'),
    (22, 34, 17, 43, 'RT_BLUE_SOLVALOU'),
    ( 7, 25,  8, 39, 'RT_PINK_MAPPY'),
    (11, 26,  8, 39, 'RT_BLUE_MAPPY'),
    (12, 27,  8, 39, 'GALAGA_RT_PLIDS'),
    (13, 28,  8, 39, 'GALAGA_RT_CARROT'),
    (14, 29,  1, 35, 'RT_BOSCONIAN'),
    (15, 30,  1, 35, 'RT_NEBULASRAY'),
    (16, 31, 17, 43, 'RT_XEVIOUS_RED'),
    (20, 32, 17, 43, 'RT_XEVIOUS_GREEN'),
    (52, 55, 52, 47, '13_RACING'),
    (60, 63, 60, 117, '13_RACING_KID'),
    (56, 59, 56, 49, 'WHITE_ANGEL'),
]

# Flat set of all entry indices that belong to a car.
ALL_CAR_INDICES = {idx for b, s, u, w, _ in CAR_TABLE for idx in (b, s, u, w)}

# Map entry index -> (car_name, role) for labelling exports.
ENTRY_LABEL = {}
for _b, _s, _u, _w, _n in CAR_TABLE:
    ENTRY_LABEL[_b] = (_n, 'body')
    ENTRY_LABEL[_s] = (_n, 'shadow')
    ENTRY_LABEL[_u] = (_n, 'underside')
    ENTRY_LABEL[_w] = (_n, 'wheel')

# Map wheel entry index -> list of car names that use it.
WHEEL_CARS = defaultdict(list)
for _b, _s, _u, _w, _n in CAR_TABLE:
    WHEEL_CARS[_w].append(_n)


def parse_car_rso(data: bytes) -> list:
    """
    Parse CAR.RSO and return a list of display-list polygon arrays,
    one per entry (index matches CAR_TABLE indices).
    """
    n = struct.unpack_from('<I', data, 0)[0]
    offsets = [struct.unpack_from('<I', data, 4 + i * 4)[0] for i in range(n)]
    ends = offsets[1:] + [len(data)]
    return [parse_display_list(data[offsets[i]: ends[i]]) for i in range(n)]
